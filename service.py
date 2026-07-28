from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import orjson
from dotenv import dotenv_values

from tcred.annotation_ui.admin import ensure_private_directory, restrict_private_file
from tcred.annotation_ui.security import ADMIN_TOKEN_ENV, validate_admin_token

ANNOTATION_ADMIN_ENV_PATH = Path(__file__).resolve().with_name(".env")
ANNOTATION_UI_ENV_PATH = Path(__file__).resolve().parents[1] / "annotation_ui" / ".env"
ANNOTATION_BASE_URL_ENV = "TCRED_ANNOTATION_BASE_URL"
ANNOTATION_BACKUP_DIR_ENV = "TCRED_ANNOTATION_BACKUP_DIR"
ANNOTATION_BACKUP_RETENTION_ENV = "TCRED_ANNOTATION_BACKUP_RETENTION"
DEFAULT_ANNOTATION_BASE_URL = "http://127.0.0.1:8770"
DEFAULT_BACKUP_DIR = Path(".annotations-backup")
DEFAULT_BACKUP_RETENTION = 168
SCHEDULE_INTERVAL_HOURS = (0, 1, 2, 6, 24)
MAX_REMOTE_ARCHIVE_BYTES = 64 * 1024 * 1024
_ARCHIVE_NAME = re.compile(r"^tcred-annotations-[A-Za-z0-9T+_.-]+\.zip$")


class RemoteAnnotationError(RuntimeError):
    """A sanitized error returned by the remote annotation service."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DashboardSettings:
    base_url: str
    admin_token: str
    backup_dir: Path
    backup_retention: int


@dataclass(frozen=True)
class DownloadedArchive:
    filename: str
    content: bytes
    sha256: str


def load_dashboard_settings(
    *,
    base_url: str | None = None,
    backup_dir: Path | None = None,
    backup_retention: int | None = None,
    admin_token: str | None = None,
    env_path: Path = ANNOTATION_ADMIN_ENV_PATH,
) -> DashboardSettings:
    """Load owner-client settings without ever returning the token to browser code."""

    own_values = dotenv_values(env_path) if env_path.exists() else {}
    annotation_values = (
        dotenv_values(ANNOTATION_UI_ENV_PATH) if ANNOTATION_UI_ENV_PATH.exists() else {}
    )

    def configured(name: str) -> str:
        return str(
            os.environ.get(name)
            or own_values.get(name)
            or annotation_values.get(name)
            or ""
        ).strip()

    resolved_token = str(admin_token or configured(ADMIN_TOKEN_ENV)).strip()
    validate_admin_token(resolved_token)
    resolved_base_url = _validated_base_url(
        base_url or configured(ANNOTATION_BASE_URL_ENV) or DEFAULT_ANNOTATION_BASE_URL
    )
    configured_backup_dir = configured(ANNOTATION_BACKUP_DIR_ENV)
    resolved_backup_dir = (
        backup_dir
        if backup_dir is not None
        else Path(configured_backup_dir) if configured_backup_dir else DEFAULT_BACKUP_DIR
    ).resolve()
    retention_text = configured(ANNOTATION_BACKUP_RETENTION_ENV)
    resolved_retention = (
        backup_retention
        if backup_retention is not None
        else int(retention_text) if retention_text else DEFAULT_BACKUP_RETENTION
    )
    if resolved_retention < 1 or resolved_retention > 10_000:
        raise ValueError("Backup retention must be between 1 and 10000 archives")
    return DashboardSettings(
        base_url=resolved_base_url,
        admin_token=resolved_token,
        backup_dir=resolved_backup_dir,
        backup_retention=resolved_retention,
    )


class AnnotationApiClient:
    def __init__(self, *, base_url: str, admin_token: str, timeout_seconds: float = 30) -> None:
        self.base_url = _validated_base_url(base_url)
        validate_admin_token(admin_token)
        self._client = httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict[str, object]:
        return self._json_request("GET", "api/health")

    def progress(self) -> dict[str, object]:
        return self._json_request("GET", "api/admin/progress")

    def prepare_clear(self) -> dict[str, object]:
        return self._json_request("POST", "api/admin/clear/prepare", json={})

    def confirm_clear(self, payload: dict[str, object]) -> dict[str, object]:
        return self._json_request("POST", "api/admin/clear/confirm", json=payload)

    def download_export(self) -> DownloadedArchive:
        try:
            response = self._client.get("api/admin/export")
        except httpx.HTTPError as exc:
            raise RemoteAnnotationError("The annotation service could not be reached") from exc
        self._raise_for_status(response)
        content = response.content
        if len(content) > MAX_REMOTE_ARCHIVE_BYTES:
            raise RemoteAnnotationError("The annotation export exceeds the dashboard safety limit")
        expected_sha256 = response.headers.get("X-Content-SHA256", "").strip().lower()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if not expected_sha256 or expected_sha256 != actual_sha256:
            raise RemoteAnnotationError("The annotation export checksum is missing or invalid")
        _validate_zip(content)
        filename = _response_filename(response) or (
            f"tcred-annotations-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
        )
        return DownloadedArchive(
            filename=filename,
            content=content,
            sha256=actual_sha256,
        )

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise RemoteAnnotationError("The annotation service could not be reached") from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteAnnotationError("The annotation service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RemoteAnnotationError("The annotation service returned an invalid response")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        message = "The annotation service rejected the request"
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                message = payload["error"]
        except ValueError:
            pass
        status_code = response.status_code if 400 <= response.status_code < 600 else 502
        raise RemoteAnnotationError(message, status_code=status_code)


class BackupManager:
    """Persist verified exports and optionally schedule periodic snapshots."""

    def __init__(
        self,
        *,
        client: AnnotationApiClient,
        backup_dir: Path,
        retention: int = DEFAULT_BACKUP_RETENTION,
    ) -> None:
        self.client = client
        self.backup_dir = backup_dir.resolve()
        self.retention = retention
        self.state_path = self.backup_dir / ".scheduler-state.json"
        self._state_lock = threading.RLock()
        self._backup_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._interval_hours = 0
        self._next_run_at: datetime | None = None
        self._last_run_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._load_state()
        self._thread = threading.Thread(
            target=self._worker,
            name="tcred-annotation-backup",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)

    def configure(self, interval_hours: int) -> dict[str, object]:
        if interval_hours not in SCHEDULE_INTERVAL_HOURS:
            raise ValueError(
                "Backup interval must be one of "
                + ", ".join(str(value) for value in SCHEDULE_INTERVAL_HOURS)
            )
        with self._state_lock:
            self._interval_hours = interval_hours
            self._next_run_at = (
                datetime.now(UTC) + timedelta(hours=interval_hours)
                if interval_hours
                else None
            )
            self._persist_state()
        self._wake.set()
        return self.status()

    def run_backup(self, *, trigger: str) -> dict[str, object]:
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("Unsupported backup trigger")
        if not self._backup_lock.acquire(blocking=False):
            raise ValueError("A backup is already running")
        try:
            archive = self.client.download_export()
            result = self._store_archive(archive, trigger=trigger)
            with self._state_lock:
                now = datetime.now(UTC).isoformat()
                self._last_run_at = now
                self._last_success_at = now
                self._last_error = None
                self._persist_state()
            return result
        except Exception as exc:
            with self._state_lock:
                self._last_run_at = datetime.now(UTC).isoformat()
                self._last_error = str(exc)
                self._persist_state()
            raise
        finally:
            self._backup_lock.release()

    def status(self) -> dict[str, object]:
        with self._state_lock:
            ensure_private_directory(self.backup_dir)
            archives = sorted(
                self.backup_dir.glob("*.zip"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            return {
                "enabled": self._interval_hours > 0,
                "interval_hours": self._interval_hours,
                "next_run_at": self._next_run_at.isoformat() if self._next_run_at else None,
                "last_run_at": self._last_run_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "archive_count": len(archives),
                "total_bytes": sum(path.stat().st_size for path in archives),
                "retention": self.retention,
                "recent": [
                    {
                        "filename": path.name,
                        "bytes": path.stat().st_size,
                        "created_at": datetime.fromtimestamp(
                            path.stat().st_mtime,
                            tz=UTC,
                        ).isoformat(),
                    }
                    for path in archives[:8]
                ],
            }

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                next_run_at = self._next_run_at
            if next_run_at is None:
                timeout = None
            else:
                timeout = max((next_run_at - datetime.now(UTC)).total_seconds(), 0)
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._state_lock:
                due = self._next_run_at is not None and datetime.now(UTC) >= self._next_run_at
            if not due:
                continue
            try:
                self.run_backup(trigger="scheduled")
            except Exception:
                pass
            finally:
                with self._state_lock:
                    self._next_run_at = (
                        datetime.now(UTC) + timedelta(hours=self._interval_hours)
                        if self._interval_hours
                        else None
                    )
                    self._persist_state()

    def _store_archive(
        self,
        archive: DownloadedArchive,
        *,
        trigger: str,
    ) -> dict[str, object]:
        ensure_private_directory(self.backup_dir)
        filename = archive.filename if _ARCHIVE_NAME.fullmatch(archive.filename) else (
            f"tcred-annotations-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
        )
        target = self.backup_dir / filename
        if target.exists():
            target = self.backup_dir / (
                f"{target.stem}-{datetime.now(UTC).strftime('%f')}{target.suffix}"
            )
        temporary = self.backup_dir / f".{target.name}.tmp"
        temporary.write_bytes(archive.content)
        restrict_private_file(temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != archive.sha256:
            temporary.unlink(missing_ok=True)
            raise ValueError("The backup checksum changed while writing the local archive")
        temporary.replace(target)
        restrict_private_file(target)
        self._apply_retention()
        return {
            "stored": True,
            "trigger": trigger,
            "filename": target.name,
            "bytes": target.stat().st_size,
            "sha256": archive.sha256,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def _apply_retention(self) -> None:
        scheduled_archives = sorted(
            self.backup_dir.glob("tcred-annotations-*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in scheduled_archives[self.retention :]:
            path.unlink(missing_ok=True)

    def _load_state(self) -> None:
        ensure_private_directory(self.backup_dir)
        if not self.state_path.exists():
            return
        try:
            payload = orjson.loads(self.state_path.read_bytes())
            interval = int(payload.get("interval_hours", 0))
            if interval not in SCHEDULE_INTERVAL_HOURS:
                raise ValueError("Unsupported persisted interval")
            self._interval_hours = interval
            self._next_run_at = (
                datetime.now(UTC) + timedelta(hours=interval) if interval else None
            )
            self._last_run_at = _optional_text(payload.get("last_run_at"))
            self._last_success_at = _optional_text(payload.get("last_success_at"))
            self._last_error = _optional_text(payload.get("last_error"))
        except (ValueError, TypeError, orjson.JSONDecodeError):
            self._interval_hours = 0
            self._next_run_at = None
            self._last_error = "Stored scheduler state was invalid; scheduling was disabled"

    def _persist_state(self) -> None:
        ensure_private_directory(self.backup_dir)
        payload = {
            "schema_version": "1.0",
            "interval_hours": self._interval_hours,
            "next_run_at": self._next_run_at.isoformat() if self._next_run_at else None,
            "last_run_at": self._last_run_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
        }
        temporary = self.backup_dir / ".scheduler-state.json.tmp"
        temporary.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        restrict_private_file(temporary)
        temporary.replace(self.state_path)
        restrict_private_file(self.state_path)


def _validated_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Annotation base URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Annotation base URL cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("A remote annotation base URL must use HTTPS")
    return base_url


def _response_filename(response: httpx.Response) -> str | None:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename="([^"]+)"', disposition)
    if match is None:
        return None
    filename = Path(match.group(1)).name
    return filename if _ARCHIVE_NAME.fullmatch(filename) else None


def _validate_zip(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                path = Path(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise RemoteAnnotationError("The annotation export contains an unsafe path")
            if archive.testzip() is not None:
                raise RemoteAnnotationError("The annotation export ZIP is corrupt")
    except zipfile.BadZipFile as exc:
        raise RemoteAnnotationError("The annotation service returned an invalid ZIP") from exc


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
