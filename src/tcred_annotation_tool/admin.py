from __future__ import annotations

import hashlib
import hmac
import io
import os
import secrets
import stat
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import orjson

from .io import read_jsonl, write_json_atomic
from .protocol import (
    CATEGORICAL_FIELDS,
    AnnotationState,
    annotation_complete,
)
from .tutorial import TUTORIAL_VERSION, independent_tutorial_ids

ACTIVITY_EVENTS = {"assignment_opened", "tutorial_completed"}
ADMIN_STATE_DIRECTORY = ".admin"
MAX_EXPORT_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
CLEAR_CONFIRMATION_PHRASE = "DELETE ALL ANNOTATION DATA"
CLEAR_CHALLENGE_TTL_SECONDS = 5 * 60


class AssignmentLike(Protocol):
    rows: list[dict[str, object]]
    submitted_at: str | None


class AnnotationStoreLike(Protocol):
    output_dir: Path
    annotator_count: int
    lock: object

    def load_assignment(self, annotator_id: int) -> AssignmentLike: ...

    def rotate_collection(self) -> str: ...


@dataclass(frozen=True)
class ExportArchive:
    filename: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ClearChallenge:
    token: str
    state_sha256: str
    expires_at: datetime


class AnnotationAdminService:
    """Persist pseudonymous activity and create privacy-minimized admin views."""

    def __init__(
        self,
        store: AnnotationStoreLike,
        *,
        backup_dir: Path | None = None,
        clear_challenge_ttl_seconds: int = CLEAR_CHALLENGE_TTL_SECONDS,
    ) -> None:
        self.store = store
        self.state_dir = store.output_dir / ADMIN_STATE_DIRECTORY
        self.activity_path = self.state_dir / "activity.json"
        self.admin_audit_path = self.state_dir / "admin-events.jsonl"
        self.backup_dir = backup_dir or store.output_dir.parent / ".annotations-backup"
        self.clear_challenge_ttl_seconds = clear_challenge_ttl_seconds
        self._clear_challenge: ClearChallenge | None = None

    def record_activity(
        self,
        *,
        annotator_id: int,
        event: str,
        tutorial_summary: object = None,
    ) -> None:
        if event not in ACTIVITY_EVENTS:
            raise ValueError(f"Unsupported annotation activity event: {event}")
        if annotator_id < 0 or annotator_id > self.store.annotator_count:
            raise ValueError("Invalid annotator id")
        with self.store.lock:  # type: ignore[attr-defined]
            payload = self._read_activity()
            annotators = payload.setdefault("annotators", {})
            if not isinstance(annotators, dict):
                raise ValueError("Malformed activity state")
            key = _annotator_key(annotator_id)
            current = annotators.get(key, {})
            record = dict(current) if isinstance(current, dict) else {}
            timestamp = _utc_now()
            if event == "assignment_opened":
                record.setdefault("first_opened_at", timestamp)
                record["last_opened_at"] = timestamp
                record["open_count"] = int(record.get("open_count", 0)) + 1
            elif event == "tutorial_completed":
                record.setdefault("tutorial_completed_at", timestamp)
                record["last_tutorial_completed_at"] = timestamp
                if tutorial_summary is not None:
                    cleaned = _validate_tutorial_summary(tutorial_summary)
                    existing = record.get("tutorial_summary")
                    record["tutorial_summary"] = _merge_tutorial_summaries(existing, cleaned)
            annotators[key] = record
            payload["updated_at"] = timestamp
            self._write_activity(payload)

    def reset_debug_activity(self) -> None:
        """Remove only the reserved annotator-00 activity record."""

        with self.store.lock:  # type: ignore[attr-defined]
            payload = self._read_activity()
            annotators = payload.get("annotators")
            if not isinstance(annotators, dict) or "annotator_00" not in annotators:
                return
            annotators.pop("annotator_00", None)
            payload["updated_at"] = _utc_now()
            self._write_activity(payload)

    def record_admin_event(self, *, action: str, outcome: str) -> None:
        """Audit privileged operations without retaining IPs, headers, or credentials."""
        with self.store.lock:  # type: ignore[attr-defined]
            ensure_private_directory(self.state_dir)
            event = {
                "schema_version": "1.0",
                "timestamp": _utc_now(),
                "action": action,
                "outcome": outcome,
            }
            with self.admin_audit_path.open("ab") as stream:
                stream.write(orjson.dumps(event, option=orjson.OPT_APPEND_NEWLINE))
            restrict_private_file(self.admin_audit_path)

    def progress_payload(self) -> dict[str, object]:
        with self.store.lock:  # type: ignore[attr-defined]
            return self._progress_payload_unlocked()

    def build_export(self) -> ExportArchive:
        with self.store.lock:  # type: ignore[attr-defined]
            generated_at = _utc_now()
            progress = self._progress_payload_unlocked(generated_at=generated_at)
            files: dict[str, bytes] = {
                "progress.json": _pretty_json(progress),
                "quality_control/tutorial_activity.json": _pretty_json(
                    self._quality_control_activity_payload()
                ),
            }
            included_annotators: list[str] = []
            for annotator_id in range(1, self.store.annotator_count + 1):
                annotator_key = _annotator_key(annotator_id)
                label_path = self.store.output_dir / f"{annotator_key}.jsonl"
                if label_path.exists():
                    files[f"labels_raw/{label_path.name}"] = _minimal_label_bytes(
                        label_path,
                        annotator_key=annotator_key,
                    )
                    included_annotators.append(annotator_key)
                save_audit_path = self.store.output_dir / "audit" / f"{annotator_key}.events.jsonl"
                if save_audit_path.exists():
                    files[f"save_audit/{save_audit_path.name}"] = save_audit_path.read_bytes()
                submission_path = self.store.output_dir / "submissions" / f"{annotator_key}.json"
                if submission_path.exists():
                    files[f"submissions/{submission_path.name}"] = submission_path.read_bytes()

            uncompressed_size = sum(len(content) for content in files.values())
            if uncompressed_size > MAX_EXPORT_UNCOMPRESSED_BYTES:
                raise ValueError("Annotation export exceeds the configured safety limit")

            manifest = {
                "schema_version": "1.0",
                "generated_at": generated_at,
                "expected_annotators": self.store.annotator_count,
                "included_annotators": included_annotators,
                "progress_summary": progress["summary"],
                "privacy": {
                    "annotator_identity": "pseudonymous annotator_XX identifier only",
                    "excluded": [
                        "debug annotator 00",
                        "assignment source files",
                        "question, answer, evidence, and graph content",
                        "IP addresses and user-agent strings",
                        "administrator credentials and administrator audit events",
                        "local filesystem paths",
                    ],
                    "quality_control_activity": (
                        "aggregate independent-tutorial attempt counts only; no tutorial labels, "
                        "interaction traces, or network identifiers"
                    ),
                },
                "files": [
                    {
                        "path": name,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    for name, content in sorted(files.items())
                ],
            }
            files["export_manifest.json"] = _pretty_json(manifest)
            archive_bytes = _zip_bytes(files)
            timestamp = generated_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
            return ExportArchive(
                filename=f"tcred-annotations-{timestamp}.zip",
                content=archive_bytes,
                sha256=hashlib.sha256(archive_bytes).hexdigest(),
            )

    def prepare_clear(self) -> dict[str, object]:
        """Create a short-lived challenge bound to the current annotation state."""

        with self.store.lock:  # type: ignore[attr-defined]
            state_sha256 = self._clear_state_sha256()
            expires_at = datetime.now(UTC) + timedelta(
                seconds=self.clear_challenge_ttl_seconds
            )
            challenge = ClearChallenge(
                token=secrets.token_urlsafe(32),
                state_sha256=state_sha256,
                expires_at=expires_at,
            )
            self._clear_challenge = challenge
            live_files = self._mutable_files()
            return {
                "schema_version": "1.0",
                "challenge_token": challenge.token,
                "state_sha256": challenge.state_sha256,
                "expires_at": challenge.expires_at.isoformat(),
                "confirmation_phrase": CLEAR_CONFIRMATION_PHRASE,
                "live_file_count": len(live_files),
                "live_bytes": sum(path.stat().st_size for path in live_files),
                "progress_summary": self._progress_payload_unlocked()["summary"],
                "warning": (
                    "This permanently removes live annotation labels, progress, save audits, "
                    "submissions, and activity records. Assignments and dataset artifacts are "
                    "never deleted."
                ),
            }

    def clear_all(
        self,
        *,
        challenge_token: str,
        state_sha256: str,
        confirmation_phrase: str,
        backup_before_clear: bool,
    ) -> dict[str, object]:
        """Delete every known mutable annotation record after a state-bound confirmation."""

        with self.store.lock:  # type: ignore[attr-defined]
            challenge = self._clear_challenge
            if challenge is None:
                raise ValueError("No active clear-data challenge; prepare the operation again")
            if datetime.now(UTC) >= challenge.expires_at:
                self._clear_challenge = None
                raise ValueError("The clear-data challenge expired; prepare the operation again")
            if not hmac.compare_digest(challenge_token, challenge.token):
                raise ValueError("The clear-data challenge token is invalid")
            if not hmac.compare_digest(state_sha256, challenge.state_sha256):
                raise ValueError("The clear-data state fingerprint is invalid")
            if not hmac.compare_digest(confirmation_phrase, CLEAR_CONFIRMATION_PHRASE):
                raise ValueError("The clear-data confirmation phrase is incorrect")

            current_state_sha256 = self._clear_state_sha256()
            if not hmac.compare_digest(current_state_sha256, challenge.state_sha256):
                self._clear_challenge = None
                raise ValueError(
                    "Annotation data changed after the warning was displayed; prepare the "
                    "operation again before deleting anything"
                )

            backup: dict[str, object] | None = None
            if backup_before_clear:
                backup = self._write_recovery_export(self.build_export(), challenge=challenge)

            files = self._mutable_files()
            deleted_bytes = sum(path.stat().st_size for path in files)
            self._clear_challenge = None
            self.store.rotate_collection()
            for path in files:
                path.unlink(missing_ok=True)
            for directory in (
                self.store.output_dir / "audit",
                self.store.output_dir / "submissions",
                self.state_dir,
            ):
                with suppress(OSError):
                    directory.rmdir()

            return {
                "schema_version": "1.0",
                "cleared": True,
                "cleared_at": _utc_now(),
                "deleted_files": len(files),
                "deleted_bytes": deleted_bytes,
                "recovery_backup": backup,
                "assignments_deleted": False,
                "dataset_artifacts_deleted": False,
                "collection_generation_rotated": True,
            }

    def _progress_payload_unlocked(
        self,
        *,
        generated_at: str | None = None,
    ) -> dict[str, object]:
        activity = self._read_activity()
        activity_by_annotator = activity.get("annotators", {})
        if not isinstance(activity_by_annotator, dict):
            activity_by_annotator = {}
        annotators: list[dict[str, object]] = []
        for annotator_id in range(1, self.store.annotator_count + 1):
            annotator_key = _annotator_key(annotator_id)
            raw_activity = activity_by_annotator.get(annotator_key, {})
            record = raw_activity if isinstance(raw_activity, dict) else {}
            annotators.append(
                self._annotator_progress(
                    annotator_id=annotator_id,
                    annotator_key=annotator_key,
                    activity=record,
                )
            )

        started = sum(bool(row["started"]) for row in annotators)
        opened = sum(bool(row["opened"]) for row in annotators)
        tutorial_completed = sum(bool(row["tutorial_completed"]) for row in annotators)
        research_started = sum(bool(row["research_started"]) for row in annotators)
        finished = sum(bool(row["finished"]) for row in annotators)
        ready_to_submit = sum(bool(row["ready_to_submit"]) for row in annotators)
        answers_completed = sum(int(row["answers_completed"]) for row in annotators)
        answers_total = sum(int(row["answers_total"]) for row in annotators)
        expected = self.store.annotator_count
        return {
            "schema_version": "1.0",
            "generated_at": generated_at or _utc_now(),
            "summary": {
                "expected_annotators": expected,
                "started": started,
                "not_started": max(expected - started, 0),
                "opened_assignment": opened,
                "tutorial_completed": tutorial_completed,
                "research_started": research_started,
                "in_progress": max(started - finished, 0),
                "ready_to_submit": ready_to_submit,
                "finished": finished,
                "annotator_completion_percent": _percent(finished, expected),
                "answers_completed": answers_completed,
                "answers_total": answers_total,
                "answer_completion_percent": _percent(answers_completed, answers_total),
            },
            "annotators": annotators,
            "privacy": {
                "contains_answer_content": False,
                "contains_network_identifiers": False,
                "identity": "pseudonymous annotator_XX identifiers",
                "debug_annotator_excluded": True,
            },
        }

    def _annotator_progress(
        self,
        *,
        annotator_id: int,
        annotator_key: str,
        activity: dict[str, object],
    ) -> dict[str, object]:
        try:
            assignment = self.store.load_assignment(annotator_id)
            rows = assignment.rows
            assignment_available = True
        except FileNotFoundError:
            rows = []
            assignment_available = False
        answers_total = len(rows)
        answers_completed = sum(_row_complete(row) for row in rows)
        answers_started = sum(_row_started(row) for row in rows)
        research_started = answers_started > 0
        ready_to_submit = (
            assignment_available
            and answers_total > 0
            and answers_completed == answers_total
            and assignment.submitted_at is None
        )
        finished = assignment_available and assignment.submitted_at is not None
        opened_at = _optional_text(activity.get("first_opened_at"))
        tutorial_completed_at = _optional_text(activity.get("tutorial_completed_at"))
        first_saved_at, last_saved_at = self._save_times(annotator_key)
        opened = opened_at is not None or research_started
        started = opened or research_started
        tutorial_completed = tutorial_completed_at is not None
        if not assignment_available:
            status = "assignment_missing"
        elif finished:
            status = "finished"
        elif ready_to_submit:
            status = "ready_to_submit"
        elif research_started:
            status = "research_in_progress"
        elif tutorial_completed:
            status = "tutorial_completed"
        elif opened:
            status = "opened"
        else:
            status = "not_started"
        timestamps = [
            value
            for value in (
                _optional_text(activity.get("last_opened_at")),
                _optional_text(activity.get("last_tutorial_completed_at")),
                last_saved_at,
                assignment.submitted_at if assignment_available else None,
            )
            if value is not None
        ]
        return {
            "annotator_key": annotator_key,
            "status": status,
            "assignment_available": assignment_available,
            "started": started,
            "opened": opened,
            "tutorial_completed": tutorial_completed,
            "research_started": research_started,
            "finished": finished,
            "ready_to_submit": ready_to_submit,
            "answers_started": answers_started,
            "answers_completed": answers_completed,
            "answers_total": answers_total,
            "completion_percent": _percent(answers_completed, answers_total),
            "first_opened_at": opened_at,
            "tutorial_completed_at": tutorial_completed_at,
            "first_research_save_at": first_saved_at,
            "last_activity_at": max(timestamps) if timestamps else None,
            "submitted_at": assignment.submitted_at if assignment_available else None,
        }

    def _save_times(self, annotator_key: str) -> tuple[str | None, str | None]:
        path = self.store.output_dir / "audit" / f"{annotator_key}.events.jsonl"
        if not path.exists():
            return None, None
        timestamps = [
            timestamp
            for row in read_jsonl(path)
            if (timestamp := _optional_text(row.get("saved_at"))) is not None
        ]
        if not timestamps:
            return None, None
        return min(timestamps), max(timestamps)

    def _read_activity(self) -> dict[str, object]:
        if not self.activity_path.exists():
            return {"schema_version": "1.0", "annotators": {}}
        payload = orjson.loads(self.activity_path.read_bytes())
        if not isinstance(payload, dict):
            raise ValueError("Malformed activity state")
        return payload

    def _quality_control_activity_payload(self) -> dict[str, object]:
        activity = self._read_activity()
        raw_annotators = activity.get("annotators", {})
        if not isinstance(raw_annotators, dict):
            raw_annotators = {}
        annotators: dict[str, dict[str, object]] = {}
        for annotator_id in range(1, self.store.annotator_count + 1):
            key = _annotator_key(annotator_id)
            raw = raw_annotators.get(key, {})
            if not isinstance(raw, dict) or not isinstance(raw.get("tutorial_summary"), dict):
                continue
            annotators[key] = {"tutorial_summary": raw["tutorial_summary"]}
        return {
            "schema_version": "1.0",
            "tutorial_version": TUTORIAL_VERSION,
            "annotators": annotators,
            "privacy": {
                "identity": "pseudonymous annotator_XX identifiers",
                "contains_tutorial_labels": False,
                "contains_interaction_trace": False,
                "contains_network_identifiers": False,
            },
        }

    def _write_activity(self, payload: dict[str, object]) -> None:
        ensure_private_directory(self.state_dir)
        write_json_atomic(self.activity_path, payload, sort_keys=True)
        restrict_private_file(self.activity_path)

    def _mutable_files(self) -> list[Path]:
        candidates = [
            *self.store.output_dir.glob("annotator_*.jsonl"),
            *self.store.output_dir.glob("annotator_*.jsonl.tmp"),
            *(self.store.output_dir / "audit").glob("annotator_*.events.jsonl"),
            *(self.store.output_dir / "submissions").glob("annotator_*.json"),
            self.activity_path,
            self.admin_audit_path,
        ]
        return sorted({path for path in candidates if path.is_file()})

    def _clear_state_sha256(self) -> str:
        """Fingerprint participant state while ignoring administrative read-only audit traffic."""

        state_files = [
            path
            for path in self._mutable_files()
            if path != self.admin_audit_path and not path.name.endswith(".tmp")
        ]
        digest = hashlib.sha256()
        for path in state_files:
            relative = path.relative_to(self.store.output_dir).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(hashlib.sha256(content).digest())
        return digest.hexdigest()

    def _write_recovery_export(
        self,
        archive: ExportArchive,
        *,
        challenge: ClearChallenge,
    ) -> dict[str, object]:
        output_root = self.store.output_dir.resolve()
        backup_root = self.backup_dir.resolve()
        if backup_root == output_root or output_root in backup_root.parents:
            raise ValueError("The recovery backup directory must be outside the live output store")
        ensure_private_directory(backup_root)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        filename = f"pre-clear-{timestamp}-{challenge.token[:8]}.zip"
        path = backup_root / filename
        temporary_path = backup_root / f".{filename}.tmp"
        temporary_path.write_bytes(archive.content)
        restrict_private_file(temporary_path)
        temporary_path.replace(path)
        restrict_private_file(path)
        return {
            "filename": filename,
            "bytes": len(archive.content),
            "sha256": archive.sha256,
        }


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(stat.S_IRWXU)


def restrict_private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _minimal_label_bytes(path: Path, *, annotator_key: str) -> bytes:
    stream = io.BytesIO()
    for row in read_jsonl(path):
        annotation = row.get("annotation")
        unit_id = str(row.get("unit_id", "")).strip()
        if not unit_id or not isinstance(annotation, dict):
            raise ValueError(f"Malformed saved label row for {annotator_key}")
        minimal = {
            "schema_version": "2.0",
            "annotation_protocol_version": str(row.get("annotation_protocol_version", "")),
            "annotator_id": annotator_key,
            "unit_id": unit_id,
            "annotation": annotation,
        }
        saved_at = _optional_text(row.get("saved_at"))
        if saved_at is not None:
            minimal["saved_at"] = saved_at
        stream.write(orjson.dumps(minimal, option=orjson.OPT_APPEND_NEWLINE))
    return stream.getvalue()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(filename=name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IRUSR | stat.S_IWUSR) << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _row_complete(row: dict[str, object]) -> bool:
    raw = row.get("annotation")
    if not isinstance(raw, dict):
        return False
    return annotation_complete(row, AnnotationState.model_validate(raw))


def _row_started(row: dict[str, object]) -> bool:
    raw = row.get("annotation")
    if not isinstance(raw, dict):
        return False
    state = AnnotationState.model_validate(raw)
    return any(
        state.labels.get(field, "") not in {"", "not_applicable"} for field in CATEGORICAL_FIELDS
    )


def _annotator_key(annotator_id: int) -> str:
    return f"annotator_{annotator_id:02d}"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _percent(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2) if denominator else 0.0


def _pretty_json(value: dict[str, object]) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_tutorial_summary(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("version") != TUTORIAL_VERSION:
        raise ValueError("Tutorial summary has an unsupported version")
    raw_items = value.get("items")
    if not isinstance(raw_items, dict) or set(raw_items) != set(independent_tutorial_ids()):
        raise ValueError("Tutorial summary must contain every independent tutorial item")
    items: dict[str, dict[str, int]] = {}
    for tutorial_id in independent_tutorial_ids():
        raw = raw_items.get(tutorial_id)
        if not isinstance(raw, dict):
            raise ValueError(f"Malformed tutorial summary for {tutorial_id}")
        attempts = raw.get("attempts")
        failed_attempts = raw.get("failed_attempts")
        if (
            isinstance(attempts, bool)
            or isinstance(failed_attempts, bool)
            or not isinstance(attempts, int)
            or not isinstance(failed_attempts, int)
            or attempts < 1
            or attempts > 100
            or failed_attempts < 0
            or failed_attempts >= attempts
        ):
            raise ValueError(f"Invalid tutorial attempt counts for {tutorial_id}")
        items[tutorial_id] = {
            "attempts": attempts,
            "failed_attempts": failed_attempts,
        }
    return {"version": TUTORIAL_VERSION, "items": items}


def _merge_tutorial_summaries(existing: object, new: dict[str, object]) -> dict[str, object]:
    if not isinstance(existing, dict) or existing.get("version") != TUTORIAL_VERSION:
        return new
    existing_items = existing.get("items")
    new_items = new["items"]
    if not isinstance(existing_items, dict) or not isinstance(new_items, dict):
        return new
    merged: dict[str, dict[str, int]] = {}
    for tutorial_id in independent_tutorial_ids():
        old = existing_items.get(tutorial_id, {})
        current = new_items[tutorial_id]
        old_attempts = int(old.get("attempts", 0)) if isinstance(old, dict) else 0
        old_failed = int(old.get("failed_attempts", 0)) if isinstance(old, dict) else 0
        current_attempts = int(current["attempts"])
        current_failed = int(current["failed_attempts"])
        merged[tutorial_id] = {
            "attempts": max(old_attempts, current_attempts),
            "failed_attempts": max(old_failed, current_failed),
        }
    return {"version": TUTORIAL_VERSION, "items": merged}
