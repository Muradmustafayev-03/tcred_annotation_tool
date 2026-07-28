from __future__ import annotations

import hashlib
import mimetypes
import secrets
import threading
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import orjson

from .admin import (
    AnnotationAdminService,
    ensure_private_directory,
    restrict_private_file,
)
from .io import read_jsonl, write_json_atomic, write_jsonl
from .presentation import displayed_evidence, require_visible_citations
from .protocol import (
    ANSWER_STAGE_FIELDS,
    CATEGORICAL_FIELDS,
    EVIDENCE_STAGE_FIELDS,
    PROTOCOL_VERSION,
    VISIBLE_LABEL_OPTIONS,
    AnnotationState,
    annotation_complete,
    applicable_fields,
    blank_state,
    issue_allowed,
    issue_requires_detail,
    logical_conflicts,
    protocol_payload,
    reason_allowed,
    reason_required,
    stage_complete,
)
from .security import AdminSecurity, SlidingWindowLimiter, load_admin_token
from .tutorial import tutorial_payload

STATIC_ROOT = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_COMMENT_CHARACTERS = 2000
ADMIN_AUTH_FAILURE_LIMIT = 10
ADMIN_AUTH_WINDOW_SECONDS = 60
COLLECTION_STATE_FILE = ".collection.json"
ADMIN_CORS_PATH_PREFIX = "/api/admin/"
PUBLIC_CORS_PATHS = frozenset(
    {
        "/api/health",
        "/api/config",
        "/api/openapi.json",
    }
)
PUBLIC_ANNOTATION_FIELDS = frozenset(
    {
        "unit_id",
        "question",
        "reference_answer",
        "answer_text",
        "cited_evidence_ids",
        "cited_evidence",
        "retrieved_evidence",
        "displayed_evidence",
        "unresolved_citation_ids",
        "graph_paths",
        "context_note",
        "applicable_fields",
        "annotation",
        "debug_source_annotator_id",
    }
)
PUBLIC_EVIDENCE_FIELDS = frozenset({"evidence_id", "text", "publication_time", "valid_time"})
PUBLIC_GRAPH_NODE_FIELDS = frozenset({"id", "label", "type"})
PUBLIC_GRAPH_EDGE_FIELDS = frozenset(
    {
        "source",
        "target",
        "relation",
        "relation_label",
        "symmetric",
        "directional",
        "traversal_direction",
        "valid_time",
        "evidence_text",
    }
)


@dataclass(frozen=True)
class AnnotationAssignment:
    annotator_id: int
    annotator_key: str
    source_path: Path
    output_path: Path
    rows: list[dict[str, object]]
    collection_id: str
    debug: bool = False
    submitted_at: str | None = None


class ApiRequestError(ValueError):
    def __init__(self, message: str, *, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class AnnotationStore:
    def __init__(
        self,
        *,
        assignment_dir: Path,
        output_dir: Path,
        annotator_count: int = 36,
    ) -> None:
        self.assignment_dir = assignment_dir
        self.output_dir = output_dir
        self.annotator_count = annotator_count
        self._collection_id: str | None = None
        self.lock = threading.RLock()

    @property
    def collection_id(self) -> str:
        with self.lock:
            if self._collection_id is None:
                self._collection_id = self._load_or_create_collection_id()
            return self._collection_id

    def rotate_collection(self) -> str:
        """Invalidate outstanding sessions and browser drafts after a full data clear."""

        with self.lock:
            self._collection_id = secrets.token_urlsafe(24)
            self._write_collection_state(self._collection_id)
            return self._collection_id

    def _load_or_create_collection_id(self) -> str:
        path = self.output_dir / COLLECTION_STATE_FILE
        if path.exists():
            payload = orjson.loads(path.read_bytes())
            if not isinstance(payload, dict):
                raise ValueError("Malformed annotation collection state")
            collection_id = str(payload.get("collection_id", "")).strip()
            if not collection_id.isascii() or len(collection_id) < 24:
                raise ValueError("Malformed annotation collection id")
            return collection_id
        collection_id = secrets.token_urlsafe(24)
        self._write_collection_state(collection_id)
        return collection_id

    def _write_collection_state(self, collection_id: str) -> None:
        ensure_private_directory(self.output_dir)
        path = self.output_dir / COLLECTION_STATE_FILE
        write_json_atomic(
            path,
            {
                "schema_version": "1.0",
                "collection_id": collection_id,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        restrict_private_file(path)

    def load_assignment(self, annotator_id: int) -> AnnotationAssignment:
        return self._load_assignment(annotator_id, public=True)

    def save_annotations(
        self,
        *,
        annotator_id: int,
        annotations_by_unit_id: dict[str, dict[str, object]],
        expected_revision: str | None = None,
        reveal_reference_for: str | None = None,
    ) -> AnnotationAssignment:
        with self.lock:
            assignment = self._load_assignment(annotator_id, public=False)
            if assignment.submitted_at is not None:
                raise ValueError("This assignment has already been submitted and is read-only")
            known_unit_ids = {str(row.get("unit_id", "")) for row in assignment.rows}
            unknown_unit_ids = sorted(set(annotations_by_unit_id) - known_unit_ids)
            if unknown_unit_ids:
                raise ValueError(
                    "Unknown annotation unit IDs; tutorial or foreign rows cannot be saved: "
                    + ", ".join(unknown_unit_ids[:5])
                )
            if reveal_reference_for is not None and reveal_reference_for not in known_unit_ids:
                raise ValueError("Cannot reveal the reference for an unknown annotation unit")
            current_revision = _file_revision(assignment.output_path)
            if expected_revision is not None and expected_revision != current_revision:
                raise ValueError(
                    "The server copy changed since this page loaded. Reload before saving "
                    "to avoid overwriting newer annotations."
                )
            rows = []
            saved_at = datetime.now(UTC).isoformat()
            for row in assignment.rows:
                unit_id = str(row.get("unit_id", ""))
                current_state = _annotation_state(row)
                raw_annotation = annotations_by_unit_id.get(unit_id)
                state = (
                    _clean_annotation_state(row, raw_annotation, existing=current_state)
                    if raw_annotation is not None
                    else current_state
                )
                if reveal_reference_for == unit_id:
                    if current_state.workflow_stage != "evidence_review":
                        raise ValueError("The reference answer has already been revealed")
                    if not stage_complete(row, state, "evidence_review"):
                        raise ValueError(
                            "Complete every evidence-stage judgment and required reason before "
                            "revealing the reference answer"
                        )
                    state = state.model_copy(update={"workflow_stage": "answer_review"})
                row = {
                    **row,
                    "annotation": state.model_dump(mode="json"),
                    "saved_at": saved_at,
                }
                rows.append(row)

            ensure_private_directory(self.output_dir)
            temp_path = assignment.output_path.with_suffix(".jsonl.tmp")
            persisted_rows = [
                _persisted_annotation_row(
                    row=row,
                    annotator_key=assignment.annotator_key,
                    saved_at=saved_at,
                )
                for row in rows
            ]
            write_jsonl(temp_path, persisted_rows)
            restrict_private_file(temp_path)
            temp_path.replace(assignment.output_path)
            restrict_private_file(assignment.output_path)
            new_revision = _file_revision(assignment.output_path)
            _append_save_event(
                output_dir=self.output_dir,
                annotator_key=assignment.annotator_key,
                previous_revision=current_revision,
                new_revision=new_revision,
                saved_at=saved_at,
                rows=rows,
            )
        return self._load_assignment(annotator_id, public=True)

    def submit_assignment(
        self,
        *,
        annotator_id: int,
        expected_revision: str,
    ) -> AnnotationAssignment:
        with self.lock:
            assignment = self._load_assignment(annotator_id, public=False)
            if assignment.submitted_at is not None:
                return self._load_assignment(annotator_id, public=True)
            actual_revision = _file_revision(assignment.output_path)
            if expected_revision != actual_revision:
                raise ValueError(
                    "The server copy changed before submission. Reload and review it again."
                )
            incomplete = [
                str(row.get("unit_id", ""))
                for row in assignment.rows
                if not annotation_complete(row, _annotation_state(row))
            ]
            if incomplete:
                raise ValueError(
                    "The assignment cannot be submitted until every annotation is complete and "
                    f"logically consistent; incomplete units={incomplete[:5]}"
                )
            submitted_at = datetime.now(UTC).isoformat()
            submission_path = self._submission_path(annotator_id)
            ensure_private_directory(submission_path.parent)
            write_json_atomic(
                submission_path,
                {
                    "schema_version": "1.0",
                    "annotation_protocol_version": PROTOCOL_VERSION,
                    "annotator_id": assignment.annotator_key,
                    "assignment_id": _assignment_id(assignment),
                    "label_file_revision": actual_revision,
                    "completed_units": len(assignment.rows),
                    "submitted_at": submitted_at,
                },
                sort_keys=True,
            )
            restrict_private_file(submission_path)
            _append_submission_event(
                output_dir=self.output_dir,
                annotator_key=assignment.annotator_key,
                submitted_at=submitted_at,
                revision=actual_revision,
                unit_count=len(assignment.rows),
            )
        return self._load_assignment(annotator_id, public=True)

    def reset_debug_progress(self, *, annotator_id: int) -> None:
        """Delete server-side state for the reserved debug annotator only."""

        if annotator_id != 0:
            raise ValueError("Progress reset is available only for debug annotator 0")
        with self.lock:
            annotator_key = self._annotator_key(annotator_id)
            paths = (
                self.output_dir / f"{annotator_key}.jsonl",
                self._submission_path(annotator_id),
                self.output_dir / "audit" / f"{annotator_key}.events.jsonl",
            )
            for path in paths:
                path.unlink(missing_ok=True)

    def _load_assignment(
        self,
        annotator_id: int,
        *,
        public: bool,
    ) -> AnnotationAssignment:
        self._validate_annotator_id(annotator_id)
        source_path = self._source_path(annotator_id)
        annotator_key = self._annotator_key(annotator_id)
        output_path = self.output_dir / f"{annotator_key}.jsonl"
        source_rows = read_jsonl(source_path)
        saved_rows = _rows_by_unit_id(output_path) if output_path.exists() else {}
        merged_rows = [
            _merge_saved_annotation(
                row=row,
                saved=saved_rows.get(str(row.get("unit_id", ""))),
                ignore_incompatible_protocol=annotator_id == 0,
            )
            for row in source_rows
        ]
        if annotator_id == 0:
            merged_rows = [
                {
                    **row,
                    "debug_source_annotator_id": str(row.get("annotator_id", "")),
                }
                for row in merged_rows
            ]
        if public:
            merged_rows = [_sanitize_annotation_row(row) for row in merged_rows]
        if annotator_id == 0:
            merged_rows = [
                {
                    **row,
                    "annotator_id": annotator_key,
                }
                for row in merged_rows
            ]
        submitted_at = _validated_submission_timestamp(
            self._submission_path(annotator_id),
            annotator_key=annotator_key,
            source_path=source_path,
            output_path=output_path,
            expected_units=len(source_rows),
        )
        return AnnotationAssignment(
            annotator_id=annotator_id,
            annotator_key=annotator_key,
            source_path=source_path,
            output_path=output_path,
            rows=merged_rows,
            collection_id=self.collection_id,
            debug=annotator_id == 0,
            submitted_at=submitted_at,
        )

    def _submission_path(self, annotator_id: int) -> Path:
        return self.output_dir / "submissions" / f"{self._annotator_key(annotator_id)}.json"

    def _source_path(self, annotator_id: int) -> Path:
        if annotator_id == 0:
            candidates = sorted(self.assignment_dir.glob("annotator_*.jsonl"))
            if not candidates:
                raise FileNotFoundError(f"No assignment files found in {self.assignment_dir}")
            return candidates[0]
        path = self.assignment_dir / f"{self._annotator_key(annotator_id)}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Assignment file not found: {path}")
        return path

    def _annotator_key(self, annotator_id: int) -> str:
        if annotator_id == 0:
            return "annotator_00"
        return f"annotator_{annotator_id:02d}"

    def _validate_annotator_id(self, annotator_id: int) -> None:
        if annotator_id < 0 or annotator_id > self.annotator_count:
            raise ValueError(f"annotator_id must be between 0 and {self.annotator_count}")

def serve_annotation_ui(
    *,
    assignment_dir: Path,
    output_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8770,
    annotator_count: int = 36,
    open_browser: bool = True,
    admin_token: str | None = None,
    backup_dir: Path | None = None,
    allow_non_loopback: bool = False,
) -> str:
    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_non_loopback:
        raise ValueError(
            "Bind the annotation application to loopback and publish it through an HTTPS "
            "reverse proxy, or explicitly enable a trusted proxy deployment"
        )
    security = AdminSecurity(admin_token=admin_token or load_admin_token())
    store = AnnotationStore(
        assignment_dir=assignment_dir,
        output_dir=output_dir,
        annotator_count=annotator_count,
    )
    ensure_private_directory(output_dir)
    admin = AnnotationAdminService(store, backup_dir=backup_dir)
    handler_cls = _handler_factory(store, admin=admin, security=security)
    server = ThreadingHTTPServer((host, port), handler_cls)
    url = f"http://{host}:{server.server_port}/"
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url


def assignment_payload(
    assignment: AnnotationAssignment,
    *,
    assignment_token: str | None = None,
) -> dict[str, object]:
    output_saved_at = None
    if assignment.output_path.exists():
        output_saved_at = datetime.fromtimestamp(
            assignment.output_path.stat().st_mtime,
            tz=UTC,
        ).isoformat()
    payload: dict[str, object] = {
        "annotator_id": assignment.annotator_id,
        "annotator_key": assignment.annotator_key,
        "debug": assignment.debug,
        "assignment_id": _assignment_id(assignment),
        "collection_id": assignment.collection_id,
        "output_saved_at": output_saved_at,
        "revision": _file_revision(assignment.output_path),
        "protocol": protocol_payload(),
        "tutorial": tutorial_payload(),
        "rows": assignment.rows,
        "progress": _progress(assignment.rows),
        "submitted": assignment.submitted_at is not None,
        "submitted_at": assignment.submitted_at,
    }
    if assignment_token is not None:
        payload["assignment_token"] = assignment_token
    return payload


def _handler_factory(
    store: AnnotationStore,
    *,
    admin: AnnotationAdminService,
    security: AdminSecurity,
) -> type[BaseHTTPRequestHandler]:
    admin_auth_failures = SlidingWindowLimiter(
        max_events=ADMIN_AUTH_FAILURE_LIMIT,
        window_seconds=ADMIN_AUTH_WINDOW_SECONDS,
    )

    class AnnotationRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "T-CRED"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/health":
                    self._write_json({"status": "ok"})
                    return
                if parsed.path == "/api/config":
                    self._write_json({"annotator_count": store.annotator_count})
                    return
                if parsed.path == "/api/openapi.json":
                    self._write_json(_openapi_payload())
                    return
                if parsed.path == "/api/docs":
                    self._write_api_docs()
                    return
                if parsed.path == "/api/assignment":
                    self._require_same_origin()
                    query = parse_qs(parsed.query)
                    assignment = store.load_assignment(_int_param(query, "annotator_id", 0))
                    assignment_token = security.issue_assignment_token(
                        annotator_id=assignment.annotator_id,
                        assignment_id=_assignment_session_id(assignment),
                    )
                    admin.record_activity(
                        annotator_id=assignment.annotator_id,
                        event="assignment_opened",
                    )
                    self._write_json(
                        assignment_payload(
                            assignment,
                            assignment_token=assignment_token,
                        )
                    )
                    return
                if parsed.path == "/api/admin/progress":
                    if not self._authorize_admin(action="progress"):
                        return
                    payload = admin.progress_payload()
                    admin.record_admin_event(action="progress", outcome="success")
                    self._write_json(payload)
                    return
                if parsed.path == "/api/admin/export":
                    if not self._authorize_admin(action="export"):
                        return
                    archive = admin.build_export()
                    admin.record_admin_event(action="export", outcome="success")
                    self._write_binary(
                        archive.content,
                        content_type="application/zip",
                        headers={
                            "Content-Disposition": f'attachment; filename="{archive.filename}"',
                            "X-Content-SHA256": archive.sha256,
                        },
                    )
                    return
                self._serve_static(parsed.path)
            except Exception as exc:  # noqa: BLE001 - convert to a non-sensitive API error
                self._handle_error(exc)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path in {
                "/api/admin/clear/prepare",
                "/api/admin/clear/confirm",
            }:
                action = (
                    "clear_prepare"
                    if parsed.path == "/api/admin/clear/prepare"
                    else "clear_confirm"
                )
                try:
                    if not self._authorize_admin(action=action):
                        return
                    payload = self._read_json_body()
                    if parsed.path == "/api/admin/clear/prepare":
                        admin.record_admin_event(action=action, outcome="success")
                        self._write_json(admin.prepare_clear())
                        return
                    backup_before_clear = payload.get("backup_before_clear", True)
                    if not isinstance(backup_before_clear, bool):
                        raise ApiRequestError("backup_before_clear must be a boolean")
                    result = admin.clear_all(
                        challenge_token=str(payload.get("challenge_token", "")),
                        state_sha256=str(payload.get("state_sha256", "")),
                        confirmation_phrase=str(payload.get("confirmation_phrase", "")),
                        backup_before_clear=backup_before_clear,
                    )
                    admin.record_admin_event(action=action, outcome="success")
                    self._write_json(result)
                except Exception as exc:  # noqa: BLE001 - convert to non-sensitive API error
                    with suppress(OSError):
                        admin.record_admin_event(action=action, outcome="rejected")
                    self._handle_error(exc)
                return
            if parsed.path not in {
                "/api/save",
                "/api/reveal-reference",
                "/api/submit",
                "/api/activity",
                "/api/debug/reset-progress",
            }:
                self._write_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._read_json_body()
                self._require_same_origin()
                annotator_id, _ = self._authenticated_assignment(payload)
                if parsed.path == "/api/debug/reset-progress":
                    store.reset_debug_progress(annotator_id=annotator_id)
                    admin.reset_debug_activity()
                    self._write_json({"cleared": True})
                    return
                if parsed.path == "/api/activity":
                    event = str(payload.get("event", ""))
                    if event != "tutorial_completed":
                        raise ApiRequestError("Unsupported activity event")
                    admin.record_activity(
                        annotator_id=annotator_id,
                        event=event,
                        tutorial_summary=payload.get("tutorial_summary"),
                    )
                    self._write_json({"recorded": True})
                    return
                if parsed.path == "/api/submit":
                    assignment = store.submit_assignment(
                        annotator_id=annotator_id,
                        expected_revision=str(payload.get("revision", "")),
                    )
                    self._write_json(
                        {
                            "submitted": True,
                            "submitted_at": assignment.submitted_at,
                            "revision": _file_revision(assignment.output_path),
                            "progress": _progress(assignment.rows),
                        }
                    )
                    return
                annotations_raw = payload.get("annotations_by_unit_id", {})
                if not isinstance(annotations_raw, dict):
                    raise ApiRequestError("annotations_by_unit_id must be an object")
                if not all(isinstance(annotation, dict) for annotation in annotations_raw.values()):
                    raise ApiRequestError("Every annotation unit must contain an annotation object")
                annotations_by_unit_id = {
                    str(unit_id): annotation for unit_id, annotation in annotations_raw.items()
                }
                reveal_unit_id = None
                if parsed.path == "/api/reveal-reference":
                    reveal_unit_id = str(payload.get("unit_id", "")).strip()
                    if not reveal_unit_id:
                        raise ApiRequestError("unit_id is required to reveal a reference answer")
                assignment = store.save_annotations(
                    annotator_id=annotator_id,
                    annotations_by_unit_id=annotations_by_unit_id,
                    expected_revision=(
                        str(payload["revision"]) if payload.get("revision") is not None else None
                    ),
                    reveal_reference_for=reveal_unit_id,
                )
                revealed_row = None
                if reveal_unit_id is not None:
                    revealed_row = next(
                        row
                        for row in assignment.rows
                        if str(row.get("unit_id", "")) == reveal_unit_id
                    )
                self._write_json(
                    {
                        "saved": True,
                        "output_saved_at": datetime.fromtimestamp(
                            assignment.output_path.stat().st_mtime,
                            tz=UTC,
                        ).isoformat(),
                        "revision": _file_revision(assignment.output_path),
                        "progress": _progress(assignment.rows),
                        "revealed_row": revealed_row,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - convert to a non-sensitive API error
                self._handle_error(exc)

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if not _cors_enabled(parsed.path):
                self._write_json(
                    {"error": "Cross-origin requests are not supported for annotator data"},
                    status=HTTPStatus.METHOD_NOT_ALLOWED,
                )
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self._write_security_headers()
            self._write_cors_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type",
            )
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, object]:
            content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != "application/json":
                raise ApiRequestError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiRequestError("Invalid Content-Length") from exc
            if length <= 0:
                raise ApiRequestError("Request body is required")
            if length > MAX_REQUEST_BYTES:
                raise ApiRequestError(
                    "Request body is too large",
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
            payload = orjson.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ApiRequestError("JSON body must be an object")
            return payload

        def _authenticated_assignment(
            self,
            payload: dict[str, object],
        ) -> tuple[int, AnnotationAssignment]:
            try:
                annotator_id = int(payload.get("annotator_id", -1))
            except (TypeError, ValueError) as exc:
                raise ApiRequestError("Invalid annotator id") from exc
            assignment = store.load_assignment(annotator_id)
            valid = security.assignment_token_valid(
                str(payload.get("assignment_token", "")),
                annotator_id=annotator_id,
                assignment_id=_assignment_session_id(assignment),
            )
            if not valid:
                raise ApiRequestError(
                    "Assignment session is missing or expired; reload the page",
                    status=HTTPStatus.UNAUTHORIZED,
                )
            return annotator_id, assignment

        def _authorize_admin(self, *, action: str) -> bool:
            if security.admin_authorized(self.headers.get("Authorization")):
                admin_auth_failures.reset()
                return True
            if not admin_auth_failures.allowed():
                self._write_json(
                    {"error": "Too many failed authentication attempts"},
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    headers={"Retry-After": str(ADMIN_AUTH_WINDOW_SECONDS)},
                )
                return False
            admin_auth_failures.record()
            with suppress(OSError):
                admin.record_admin_event(action=action, outcome="authentication_failed")
            self._write_json(
                {"error": "Administrative authentication required"},
                status=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": 'Bearer realm="tcred-admin"'},
            )
            return False

        def _require_same_origin(self) -> None:
            if self.headers.get("Sec-Fetch-Site", "").casefold() == "cross-site":
                raise ApiRequestError("Cross-origin request rejected", status=HTTPStatus.FORBIDDEN)
            origin = self.headers.get("Origin")
            if not origin:
                return
            parsed_origin = urlparse(origin)
            request_host = self.headers.get("Host", "").casefold()
            if (
                parsed_origin.scheme not in {"http", "https"}
                or parsed_origin.netloc.casefold() != request_host
            ):
                raise ApiRequestError("Cross-origin request rejected", status=HTTPStatus.FORBIDDEN)

        def _serve_static(self, request_path: str) -> None:
            if request_path in {"", "/"}:
                relative = "index.html"
            elif request_path == "/annotate":
                relative = "annotate.html"
            else:
                relative = request_path.lstrip("/")
            path = (STATIC_ROOT / relative).resolve()
            if not _is_within(path, STATIC_ROOT.resolve()) or not path.exists():
                self._write_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._write_security_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _write_api_docs(self) -> None:
            content = _swagger_document()
            self.send_response(HTTPStatus.OK)
            self._write_security_headers(api_docs=True)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _write_json(
            self,
            payload: dict[str, object],
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            content = orjson.dumps(payload)
            self.send_response(status)
            self._write_security_headers()
            self._write_cors_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(content)

        def _write_binary(
            self,
            content: bytes,
            *,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(HTTPStatus.OK)
            self._write_security_headers()
            self._write_cors_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(content)

        def _write_cors_headers(self) -> None:
            if not _cors_enabled(urlparse(self.path).path):
                return
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Expose-Headers",
                "Content-Disposition, X-Content-SHA256",
            )

        def _write_security_headers(self, *, api_docs: bool = False) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            resource_policy = (
                "cross-origin" if _cors_enabled(urlparse(self.path).path) else "same-origin"
            )
            self.send_header("Cross-Origin-Resource-Policy", resource_policy)
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if api_docs:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors "
                    "'none'; form-action 'none'; script-src 'self' https://cdn.jsdelivr.net; "
                    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                    "img-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self'",
                )
            else:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors "
                    "'none'; form-action 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'",
                )
            forwarded_protocol = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
            if forwarded_protocol == "https":
                self.send_header(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, ApiRequestError):
                self._write_json({"error": str(exc)}, status=exc.status)
                return
            if isinstance(exc, FileNotFoundError):
                self._write_json(
                    {"error": "Requested assignment is unavailable"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            if isinstance(exc, (ValueError, orjson.JSONDecodeError)):
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(
                {"error": "The server could not complete the request"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    return AnnotationRequestHandler


def _cors_enabled(path: str) -> bool:
    """Allow cross-origin administration without exposing annotator assignments cross-site."""

    return path.startswith(ADMIN_CORS_PATH_PREFIX) or path in PUBLIC_CORS_PATHS


def _openapi_payload() -> dict[str, object]:
    """Return a documentation-only OpenAPI description with no deployment secrets."""

    admin_security = [{"adminBearer": []}]
    json_content = {"application/json": {"schema": {"type": "object"}}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "T-CRED Annotation API",
            "version": "1.0.0",
            "description": (
                "Annotation workflow and administrative monitoring API. The interactive "
                "execution controls are disabled in the bundled documentation. Administrative "
                "requests require a bearer token supplied only through the Authorization header."
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": "Service"},
            {"name": "Annotation"},
            {"name": "Administration"},
        ],
        "paths": {
            "/api/health": {
                "get": {
                    "tags": ["Service"],
                    "summary": "Minimal liveness check",
                    "responses": {"200": {"description": "Service is running"}},
                }
            },
            "/api/config": {
                "get": {
                    "tags": ["Service"],
                    "summary": "Public annotator-count configuration",
                    "responses": {"200": {"description": "Non-sensitive public configuration"}},
                }
            },
            "/api/assignment": {
                "get": {
                    "tags": ["Annotation"],
                    "summary": "Load one pseudonymous assignment",
                    "parameters": [
                        {
                            "name": "annotator_id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 0},
                        }
                    ],
                    "responses": {
                        "200": {"description": "Blinded assignment and short-lived save token"}
                    },
                }
            },
            "/api/save": _annotation_post_operation(
                "Autosave research judgments with optimistic revision checking"
            ),
            "/api/reveal-reference": _annotation_post_operation(
                "Lock Stage 1 and reveal the reference for Stage 2"
            ),
            "/api/submit": _annotation_post_operation(
                "Validate, finalize, and freeze one complete assignment"
            ),
            "/api/activity": _annotation_post_operation(
                "Record minimized tutorial-completion metadata"
            ),
            "/api/debug/reset-progress": _annotation_post_operation(
                "Reset only reserved debug annotator 0"
            ),
            "/api/admin/progress": {
                "get": {
                    "tags": ["Administration"],
                    "summary": "Read pseudonymous progress statistics",
                    "security": admin_security,
                    "responses": {
                        "200": {"description": "Aggregate and per-pseudonym progress"},
                        "401": {"description": "Bearer authentication required"},
                    },
                }
            },
            "/api/admin/export": {
                "get": {
                    "tags": ["Administration"],
                    "summary": "Download a checksum-manifested minimized ZIP snapshot",
                    "security": admin_security,
                    "responses": {
                        "200": {
                            "description": "Annotation export",
                            "content": {
                                "application/zip": {
                                    "schema": {"type": "string", "format": "binary"}
                                }
                            },
                        },
                        "401": {"description": "Bearer authentication required"},
                    },
                }
            },
            "/api/admin/clear/prepare": {
                "post": {
                    "tags": ["Administration"],
                    "summary": "Prepare a short-lived, state-bound clear-data challenge",
                    "security": admin_security,
                    "requestBody": {
                        "required": True,
                        "content": json_content,
                    },
                    "responses": {
                        "200": {"description": "Deletion warning and confirmation challenge"},
                        "401": {"description": "Bearer authentication required"},
                    },
                }
            },
            "/api/admin/clear/confirm": {
                "post": {
                    "tags": ["Administration"],
                    "summary": "Permanently clear live annotation data",
                    "description": (
                        "Dangerous operation. Requires the unexpired challenge token, matching "
                        "state fingerprint, exact confirmation phrase, and bearer credential. "
                        "Assignments and dataset artifacts are never deleted."
                    ),
                    "security": admin_security,
                    "x-dangerous-operation": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "challenge_token",
                                        "state_sha256",
                                        "confirmation_phrase",
                                    ],
                                    "properties": {
                                        "challenge_token": {"type": "string"},
                                        "state_sha256": {"type": "string"},
                                        "confirmation_phrase": {"type": "string"},
                                        "backup_before_clear": {
                                            "type": "boolean",
                                            "default": True,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Live data cleared"},
                        "400": {"description": "Challenge rejected or state changed"},
                        "401": {"description": "Bearer authentication required"},
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "adminBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "TCRED_ADMIN_TOKEN. Never place this credential in a URL, source file, "
                        "screenshot, or browser storage."
                    ),
                }
            }
        },
    }


def _annotation_post_operation(summary: str) -> dict[str, object]:
    return {
        "post": {
            "tags": ["Annotation"],
            "summary": summary,
            "description": (
                "Same-origin only. The request body must include the short-lived assignment "
                "session token returned by GET /api/assignment."
            ),
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
            "responses": {
                "200": {"description": "Operation accepted"},
                "401": {"description": "Assignment session missing or expired"},
                "403": {"description": "Cross-origin request rejected"},
            },
        }
    }


def _swagger_document() -> bytes:
    return b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>T-CRED Annotation API</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css">
  <link rel="stylesheet" href="/swagger-overrides.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
  <script src="/swagger-init.js"></script>
</body>
</html>
"""


def _rows_by_unit_id(path: Path) -> dict[str, dict[str, object]]:
    return {str(row.get("unit_id", "")): row for row in read_jsonl(path)}


def _merge_saved_annotation(
    *,
    row: dict[str, object],
    saved: dict[str, object] | None,
    ignore_incompatible_protocol: bool = False,
) -> dict[str, object]:
    if not saved:
        return {**row, "annotation": blank_state(row).model_dump(mode="json")}
    saved_protocol = str(saved.get("annotation_protocol_version", "")).strip()
    if saved_protocol != PROTOCOL_VERSION:
        if ignore_incompatible_protocol:
            return {**row, "annotation": blank_state(row).model_dump(mode="json")}
        raise ValueError(
            "Saved annotations use an incompatible protocol version "
            f"({saved_protocol or 'missing'}; expected {PROTOCOL_VERSION}). "
            "Do not continue this assignment until the pilot labels have been archived or cleared."
        )
    raw_annotation = saved.get("annotation")
    if not isinstance(raw_annotation, dict):
        raise ValueError("Saved annotation row has no annotation object")
    persisted = AnnotationState.model_validate(raw_annotation)
    state = _clean_annotation_state(row, raw_annotation, existing=persisted)
    return {**row, "annotation": state.model_dump(mode="json")}


def _persisted_annotation_row(
    *,
    row: dict[str, object],
    annotator_key: str,
    saved_at: str,
) -> dict[str, object]:
    """Persist only the identifiers and judgments needed for label import."""
    state = _annotation_state(row)
    unit_id = str(row.get("unit_id", "")).strip()
    if not unit_id:
        raise ValueError("Annotation row has no unit id")
    return {
        "schema_version": "2.0",
        "annotation_protocol_version": PROTOCOL_VERSION,
        "annotator_id": annotator_key,
        "unit_id": unit_id,
        "annotation": state.model_dump(mode="json"),
        "saved_at": saved_at,
    }


def _sanitize_annotation_row(row: dict[str, object]) -> dict[str, object]:
    sanitized = {key: value for key, value in row.items() if key in PUBLIC_ANNOTATION_FIELDS}
    state = _annotation_state(sanitized)
    reference_revealed = state.workflow_stage == "answer_review"
    if not reference_revealed:
        sanitized.pop("reference_answer", None)
    sanitized["reference_revealed"] = reference_revealed
    sanitized["logical_conflicts"] = list(logical_conflicts(sanitized, state))
    for evidence_key in ("cited_evidence", "retrieved_evidence"):
        evidence_rows = sanitized.get(evidence_key)
        if isinstance(evidence_rows, list):
            sanitized[evidence_key] = [
                {key: value for key, value in evidence.items() if key in PUBLIC_EVIDENCE_FIELDS}
                for evidence in evidence_rows
                if isinstance(evidence, dict)
            ]
    sanitized["displayed_evidence"] = displayed_evidence(sanitized)
    require_visible_citations(sanitized)
    sanitized["graph_paths"] = _sanitize_graph_paths(sanitized.get("graph_paths"))
    return sanitized


def _sanitize_graph_paths(raw_paths: object) -> list[dict[str, object]]:
    if not isinstance(raw_paths, list):
        return []
    sanitized_paths: list[dict[str, object]] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, dict):
            continue
        nodes = raw_path.get("nodes")
        edges = raw_path.get("edges")
        public_nodes = (
            [
                {key: value for key, value in node.items() if key in PUBLIC_GRAPH_NODE_FIELDS}
                for node in nodes
                if isinstance(node, dict)
            ]
            if isinstance(nodes, list)
            else []
        )
        public_edges: list[dict[str, object]] = []
        if isinstance(edges, list):
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                public_edge = {
                    key: value for key, value in edge.items() if key in PUBLIC_GRAPH_EDGE_FIELDS
                }
                for endpoint in ("source", "target"):
                    value = public_edge.get(endpoint)
                    if isinstance(value, dict):
                        public_edge[endpoint] = {
                            key: endpoint_value
                            for key, endpoint_value in value.items()
                            if key in PUBLIC_GRAPH_NODE_FIELDS
                        }
                public_edges.append(public_edge)
        sanitized_paths.append(
            {
                "path_id": raw_path.get("path_id", ""),
                "nodes": public_nodes,
                "edges": public_edges,
            }
        )
    return sanitized_paths


def _clean_annotation_state(
    row: dict[str, object],
    raw: dict[str, object],
    *,
    existing: AnnotationState,
) -> AnnotationState:
    parsed = AnnotationState.model_validate(raw)
    if parsed.workflow_stage != existing.workflow_stage:
        raise ValueError("Workflow stages can change only through the reference-reveal endpoint")
    applicable = set(applicable_fields(row))
    labels: dict[str, str] = {}
    for field in CATEGORICAL_FIELDS:
        value = str(parsed.labels.get(field, "")).strip()
        if field not in applicable:
            labels[field] = "not_applicable"
            continue
        if value not in {"", *VISIBLE_LABEL_OPTIONS}:
            raise ValueError(f"Invalid visible value for {field}: {value}")
        labels[field] = value

    reasons: dict[str, str] = {}
    for field in applicable:
        label = labels[field]
        reason = str(parsed.reasons.get(field, "")).strip()
        if not reason_required(label):
            continue
        if reason and not reason_allowed(field, label, reason):
            raise ValueError(f"Invalid reason code for {field}={label}: {reason}")
        if reason:
            reasons[field] = reason

    if existing.workflow_stage == "evidence_review":
        premature = [
            field for field in ANSWER_STAGE_FIELDS if field in applicable and labels[field]
        ]
        if premature:
            raise ValueError(
                "Answer-stage judgments cannot be saved before the reference is revealed: "
                + ", ".join(premature)
            )
    else:
        changed_locked = [
            field
            for field in EVIDENCE_STAGE_FIELDS
            if field in applicable
            and (
                labels[field] != existing.labels.get(field, "")
                or reasons.get(field, "") != existing.reasons.get(field, "")
            )
        ]
        if changed_locked:
            raise ValueError(
                "Evidence-stage judgments and reasons are locked after the reference is "
                "revealed: " + ", ".join(changed_locked)
            )

    issue_code = str(parsed.issue_code).strip()
    if not issue_allowed(issue_code):
        raise ValueError(f"Invalid issue code: {issue_code}")
    issue_detail = _clean_text(parsed.issue_detail, field="Issue detail")
    if issue_requires_detail(issue_code) and not issue_detail:
        # Autosave may capture the issue selection before the annotator finishes typing.
        issue_detail = ""
    comment = _clean_text(parsed.comment, field="Comment")
    return AnnotationState(
        labels=labels,
        reasons=reasons,
        issue_code=issue_code,
        issue_detail=issue_detail,
        comment=comment,
        workflow_stage=existing.workflow_stage,
    )


def _clean_text(value: object, *, field: str) -> str:
    text = str(value).strip()
    if len(text) > MAX_COMMENT_CHARACTERS:
        raise ValueError(f"{field} exceeds the {MAX_COMMENT_CHARACTERS}-character limit")
    has_control_character = any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in text
    )
    if has_control_character:
        raise ValueError(f"{field} contains unsupported control characters")
    return text


def _annotation_state(row: dict[str, object]) -> AnnotationState:
    raw = row.get("annotation")
    if not isinstance(raw, dict):
        return blank_state(row)
    return AnnotationState.model_validate(raw)


def _progress(rows: list[dict[str, object]]) -> dict[str, int]:
    completed = sum(_row_complete(row) for row in rows)
    return {
        "total": len(rows),
        "completed": completed,
        "remaining": max(len(rows) - completed, 0),
    }


def _row_complete(row: dict[str, object]) -> bool:
    return annotation_complete(row, _annotation_state(row))


def _file_revision(path: Path) -> str:
    if not path.exists():
        return "none"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assignment_id(assignment: AnnotationAssignment) -> str:
    return _source_assignment_id(
        annotator_key=assignment.annotator_key,
        source_path=assignment.source_path,
    )


def _assignment_session_id(assignment: AnnotationAssignment) -> str:
    return f"{_assignment_id(assignment)}:{assignment.collection_id}"


def _source_assignment_id(*, annotator_key: str, source_path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(annotator_key.encode())
    digest.update(b"\0")
    digest.update(source_path.read_bytes())
    return f"assignment_{digest.hexdigest()[:20]}"


def _append_save_event(
    *,
    output_dir: Path,
    annotator_key: str,
    previous_revision: str,
    new_revision: str,
    saved_at: str,
    rows: list[dict[str, object]],
) -> None:
    audit_dir = output_dir / "audit"
    ensure_private_directory(audit_dir)
    path = audit_dir / f"{annotator_key}.events.jsonl"
    event = {
        "event": "annotation_save",
        "annotator_key": annotator_key,
        "saved_at": saved_at,
        "previous_revision": previous_revision,
        "new_revision": new_revision,
        "completed_units": _progress(rows)["completed"],
        "total_units": len(rows),
    }
    with path.open("ab") as stream:
        stream.write(orjson.dumps(event))
        stream.write(b"\n")
    restrict_private_file(path)


def _append_submission_event(
    *,
    output_dir: Path,
    annotator_key: str,
    submitted_at: str,
    revision: str,
    unit_count: int,
) -> None:
    audit_dir = output_dir / "audit"
    ensure_private_directory(audit_dir)
    path = audit_dir / f"{annotator_key}.events.jsonl"
    event = {
        "event": "annotation_submission",
        "annotator_key": annotator_key,
        "submitted_at": submitted_at,
        "revision": revision,
        "completed_units": unit_count,
        "total_units": unit_count,
    }
    with path.open("ab") as stream:
        stream.write(orjson.dumps(event))
        stream.write(b"\n")
    restrict_private_file(path)


def _validated_submission_timestamp(
    path: Path,
    *,
    annotator_key: str,
    source_path: Path,
    output_path: Path,
    expected_units: int,
) -> str | None:
    if not path.exists():
        return None
    payload = orjson.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError(f"Malformed annotation submission record: {path}")
    expected_values = {
        "schema_version": "1.0",
        "annotation_protocol_version": PROTOCOL_VERSION,
        "annotator_id": annotator_key,
        "assignment_id": _source_assignment_id(
            annotator_key=annotator_key,
            source_path=source_path,
        ),
        "completed_units": expected_units,
    }
    mismatched = [
        field for field, expected in expected_values.items() if payload.get(field) != expected
    ]
    if mismatched:
        raise ValueError(
            "Annotation submission record does not match the frozen assignment: "
            + ", ".join(mismatched)
        )
    submitted_at = str(payload.get("submitted_at", "")).strip()
    if not submitted_at:
        raise ValueError(f"Annotation submission has no timestamp: {path}")
    try:
        parsed_timestamp = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Annotation submission has an invalid timestamp: {path}") from exc
    if parsed_timestamp.tzinfo is None:
        raise ValueError(f"Annotation submission timestamp has no timezone: {path}")
    if not output_path.exists():
        raise ValueError(f"Submitted annotation label file is missing: {output_path}")
    if str(payload.get("label_file_revision", "")) != _file_revision(output_path):
        raise ValueError(
            f"Submitted annotation labels changed after final submission: {annotator_key}"
        )
    return submitted_at


def _int_param(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        values = query.get(name)
        return int(values[0]) if values else default
    except ValueError:
        return default


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
