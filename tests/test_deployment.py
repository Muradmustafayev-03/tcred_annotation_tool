from __future__ import annotations

import hashlib
import io
import os
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import orjson
import pytest

from tcred_annotation_tool.admin import AnnotationAdminService
from tcred_annotation_tool.config import (
    AppSettings,
    ConfigurationError,
    load_local_environment,
    verify_assignment_bundle,
)
from tcred_annotation_tool.io import read_jsonl, write_jsonl
from tcred_annotation_tool.protocol import PROTOCOL_VERSION
from tcred_annotation_tool.security import (
    ADMIN_TOKEN_FILE,
    AdminSecurity,
    load_or_create_admin_token,
)
from tcred_annotation_tool.server import (
    AnnotationStore,
    _handler_factory,
    assignment_payload,
)
from tcred_annotation_tool.tutorial import TUTORIAL_VERSION, tutorial_payload

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ADMIN_TOKEN = "test-admin-token-" + "x" * 48


def test_frozen_deployment_bundle_is_complete_and_checksum_verified(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    summary = verify_assignment_bundle(settings)

    assert summary == {
        "annotation_protocol_version": "4.2",
        "annotators": 36,
        "assignments_per_annotator": 20,
        "total_assignments": 720,
        "source_release_package_sha256": (
            "f6d37685f027a582911eae92c17adef0aa73774206f96104468597da1b6862a6"
        ),
    }


def test_deployment_repository_excludes_private_evaluation_artifacts() -> None:
    forbidden = {
        "human_eval_key.jsonl",
        "human_eval_units.jsonl",
        "entities.jsonl",
        "facts.jsonl",
        "questions.jsonl",
        "answer_variants.jsonl",
    }
    present = {
        path.name
        for path in REPOSITORY_ROOT.rglob("*")
        if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts
    }

    assert forbidden.isdisjoint(present)
    assert len(list((REPOSITORY_ROOT / "data" / "assignments").glob("*.jsonl"))) == 36


def test_railway_refuses_ephemeral_research_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_environment(monkeypatch)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "project")

    with pytest.raises(ConfigurationError, match="no volume is attached"):
        AppSettings.from_environment()


def test_railway_uses_attached_volume_and_injected_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_environment(monkeypatch)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "project")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "volume"))
    monkeypatch.setenv("PORT", "9080")

    settings = AppSettings.from_environment()

    assert settings.state_dir == (tmp_path / "volume").resolve()
    assert settings.output_dir == (tmp_path / "volume" / "labels_raw").resolve()
    assert settings.host == "0.0.0.0"
    assert settings.port == 9080
    assert settings.durable_storage is True
    assert settings.allow_non_loopback is True


def test_generated_admin_token_is_private_and_stable(tmp_path: Path) -> None:
    first = load_or_create_admin_token(state_dir=tmp_path, env_path=tmp_path / "missing.env")
    second = load_or_create_admin_token(state_dir=tmp_path, env_path=tmp_path / "missing.env")

    assert first == second
    assert len(first) >= 43
    assert (tmp_path / ADMIN_TOKEN_FILE).read_text(encoding="ascii").strip() == first
    if os.name == "posix":
        assert (tmp_path / ADMIN_TOKEN_FILE).stat().st_mode & 0o777 == 0o600


def test_local_dotenv_does_not_override_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HOST=127.0.0.2\nPORT=9000\nTCRED_STATE_DIR=dotenv-state\n",
        encoding="ascii",
    )
    monkeypatch.setenv("TCRED_ENV_FILE", str(env_path))
    monkeypatch.setenv("PORT", "9100")
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("TCRED_STATE_DIR", raising=False)

    load_local_environment()

    assert os.environ["HOST"] == "127.0.0.2"
    assert os.environ["PORT"] == "9100"
    assert os.environ["TCRED_STATE_DIR"] == "dotenv-state"


def test_assignment_stages_blind_then_lock_reference_and_submit(tmp_path: Path) -> None:
    assignment_dir = tmp_path / "assignments"
    output_dir = tmp_path / "labels"
    assignment_dir.mkdir()
    write_jsonl(assignment_dir / "annotator_01.jsonl", [_assignment_row()])
    store = AnnotationStore(
        assignment_dir=assignment_dir,
        output_dir=output_dir,
        annotator_count=1,
    )

    loaded = assignment_payload(store.load_assignment(1))
    assert "reference_answer" not in loaded["rows"][0]
    assert loaded["rows"][0]["reference_revealed"] is False

    evidence_saved = store.save_annotations(
        annotator_id=1,
        annotations_by_unit_id={"unit_1": _annotation()},
        expected_revision=str(loaded["revision"]),
    )
    revealed = store.save_annotations(
        annotator_id=1,
        annotations_by_unit_id={"unit_1": _annotation()},
        expected_revision=str(assignment_payload(evidence_saved)["revision"]),
        reveal_reference_for="unit_1",
    )
    public_revealed = assignment_payload(revealed)
    assert public_revealed["rows"][0]["reference_answer"] == "Mira Chen"
    assert public_revealed["rows"][0]["reference_revealed"] is True

    completed = store.save_annotations(
        annotator_id=1,
        annotations_by_unit_id={
            "unit_1": _annotation(workflow_stage="answer_review", answer_correct="yes")
        },
        expected_revision=str(public_revealed["revision"]),
    )
    submitted = store.submit_assignment(
        annotator_id=1,
        expected_revision=str(assignment_payload(completed)["revision"]),
    )
    assert submitted.submitted_at is not None
    with pytest.raises(ValueError, match="read-only"):
        store.save_annotations(
            annotator_id=1,
            annotations_by_unit_id={
                "unit_1": _annotation(workflow_stage="answer_review", answer_correct="no")
            },
        )


def test_http_security_admin_export_and_health(tmp_path: Path) -> None:
    assignment_dir = tmp_path / "assignments"
    output_dir = tmp_path / "labels"
    assignment_dir.mkdir()
    write_jsonl(assignment_dir / "annotator_01.jsonl", [_assignment_row()])
    store = AnnotationStore(
        assignment_dir=assignment_dir,
        output_dir=output_dir,
        annotator_count=1,
    )
    admin = AnnotationAdminService(store, backup_dir=tmp_path / "backups")
    handler = _handler_factory(
        store,
        admin=admin,
        security=AdminSecurity(admin_token=ADMIN_TOKEN),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=base_url, timeout=5) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert health.headers["x-content-type-options"] == "nosniff"
            assert health.headers["x-frame-options"] == "DENY"

            assignment_response = client.get("/api/assignment?annotator_id=1")
            assert assignment_response.status_code == 200
            assignment = assignment_response.json()
            assert "reference_answer" not in assignment["rows"][0]
            assert assignment["assignment_token"].startswith("v1.")

            missing_session = client.post(
                "/api/save",
                json={
                    "annotator_id": 1,
                    "revision": assignment["revision"],
                    "annotations_by_unit_id": {"unit_1": _annotation()},
                },
            )
            assert missing_session.status_code == 401

            cross_origin = client.post(
                "/api/save",
                headers={"Origin": "https://attacker.invalid"},
                json={
                    "annotator_id": 1,
                    "assignment_token": assignment["assignment_token"],
                    "revision": assignment["revision"],
                    "annotations_by_unit_id": {"unit_1": _annotation()},
                },
            )
            assert cross_origin.status_code == 403

            saved = client.post(
                "/api/save",
                json={
                    "annotator_id": 1,
                    "assignment_token": assignment["assignment_token"],
                    "revision": assignment["revision"],
                    "annotations_by_unit_id": {"unit_1": _annotation()},
                },
            )
            assert saved.status_code == 200

            unauthorized = client.get("/api/admin/progress")
            assert unauthorized.status_code == 401
            progress = client.get(
                "/api/admin/progress",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
            assert progress.status_code == 200
            assert progress.json()["privacy"]["contains_answer_content"] is False

            exported = client.get(
                "/api/admin/export",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
            assert exported.status_code == 200
            assert hashlib.sha256(exported.content).hexdigest() == exported.headers[
                "x-content-sha256"
            ]
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                names = archive.namelist()
                assert "labels_raw/annotator_01.jsonl" in names
                assert not any("assignment" in name or ".admin-token" in name for name in names)

            guide = client.get("/annotation-guidelines.pdf")
            assert guide.status_code == 200
            assert guide.headers["content-type"] == "application/pdf"
            assert guide.content.startswith(b"%PDF")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_tutorial_and_ui_keep_protocol_rules_synchronized() -> None:
    tutorial = tutorial_payload()
    assert tutorial["version"] == TUTORIAL_VERSION == "5.2"
    assert len(tutorial["items"]) == 6
    assert PROTOCOL_VERSION == "4.2"
    assert {row["mode"] for row in tutorial["items"]} == {"guided", "independent"}

    html = (
        REPOSITORY_ROOT
        / "src"
        / "tcred_annotation_tool"
        / "static"
        / "annotate.html"
    ).read_text(encoding="utf-8")
    javascript = (
        REPOSITORY_ROOT / "src" / "tcred_annotation_tool" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "Partial" in html and "Cannot judge" in html
    assert "Do not search the web, use AI tools" in html
    assert "/annotation-guidelines.pdf" in html
    assert "const SERVER_AUTOSAVE_DELAY_MS = 1500;" in javascript


def test_admin_export_rows_are_minimized(tmp_path: Path) -> None:
    assignment_dir = tmp_path / "assignments"
    output_dir = tmp_path / "labels"
    assignment_dir.mkdir()
    write_jsonl(assignment_dir / "annotator_01.jsonl", [_assignment_row()])
    store = AnnotationStore(
        assignment_dir=assignment_dir,
        output_dir=output_dir,
        annotator_count=1,
    )
    store.save_annotations(
        annotator_id=1,
        annotations_by_unit_id={"unit_1": _annotation()},
    )

    persisted = read_jsonl(output_dir / "annotator_01.jsonl")[0]

    assert set(persisted) == {
        "schema_version",
        "annotation_protocol_version",
        "annotator_id",
        "unit_id",
        "annotation",
        "saved_at",
    }
    serialized = orjson.dumps(persisted)
    assert b"Who held the role" not in serialized
    assert b"Mira Chen" not in serialized


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        assignment_dir=REPOSITORY_ROOT / "data" / "assignments",
        deployment_manifest=REPOSITORY_ROOT / "data" / "deployment_manifest.json",
        state_dir=tmp_path,
        output_dir=tmp_path / "labels_raw",
        backup_dir=tmp_path / "backups",
        host="127.0.0.1",
        port=8770,
        annotator_count=36,
        railway=False,
        durable_storage=False,
        allow_non_loopback=False,
    )


def _assignment_row() -> dict[str, object]:
    return {
        "annotator_id": "annotator_01",
        "assignment_index": 1,
        "unit_id": "unit_1",
        "answer_id": "answer_1",
        "answer_source": "test",
        "question": "Who held the role on 2020-01-01?",
        "reference_answer": "Mira Chen",
        "answer_text": "Mira Chen",
        "cited_evidence_ids": [],
        "cited_evidence": [],
        "retrieved_evidence": [],
        "graph_paths": [],
        "context_note": "",
        "applicable_fields": [
            "answer_correct",
            "temporal_correct",
            "evidence_supports_answer",
            "citation_temporally_valid",
        ],
    }


def _annotation(
    *,
    workflow_stage: str = "evidence_review",
    answer_correct: str = "",
) -> dict[str, object]:
    return {
        "labels": {
            "answer_correct": answer_correct,
            "temporal_correct": "yes",
            "evidence_supports_answer": "yes",
            "citation_temporally_valid": "yes",
            "graph_evidence_sufficient": "not_applicable",
            "response_decision_appropriate": "not_applicable",
        },
        "reasons": {},
        "issue_code": "",
        "issue_detail": "",
        "comment": "",
        "workflow_stage": workflow_stage,
    }


def _clear_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_VOLUME_MOUNT_PATH",
        "TCRED_STATE_DIR",
        "TCRED_STORAGE_IS_DURABLE",
        "TCRED_ALLOW_EPHEMERAL_STORAGE",
        "TCRED_ALLOW_NON_LOOPBACK",
        "HOST",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)
