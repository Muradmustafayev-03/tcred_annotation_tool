from __future__ import annotations

import hmac
import mimetypes
import secrets
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import orjson

from .service import (
    SCHEDULE_INTERVAL_HOURS,
    AnnotationApiClient,
    BackupManager,
    DashboardSettings,
    DownloadedArchive,
    RemoteAnnotationError,
    load_dashboard_settings,
)

STATIC_ROOT = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 64 * 1024


@dataclass
class AnnotationAdminDashboard:
    settings: DashboardSettings
    client: AnnotationApiClient
    backups: BackupManager
    csrf_token: str

    def config_payload(self) -> dict[str, object]:
        return {
            "annotation_base_url": self.settings.base_url,
            "annotation_docs_url": f"{self.settings.base_url}/api/docs",
            "backup_directory_name": self.settings.backup_dir.name,
            "schedule_interval_hours": list(SCHEDULE_INTERVAL_HOURS),
            "csrf_token": self.csrf_token,
        }

    def status_payload(self) -> dict[str, object]:
        try:
            health = self.client.health()
            progress = self.client.progress()
            connection = {
                "connected": health.get("status") == "ok",
                "error": None,
            }
        except RemoteAnnotationError as exc:
            progress = None
            connection = {
                "connected": False,
                "error": str(exc),
            }
        return {
            "connection": connection,
            "progress": progress,
            "backups": self.backups.status(),
        }

    def prepare_clear(self) -> dict[str, object]:
        return self.client.prepare_clear()

    def confirm_clear(self, payload: dict[str, object]) -> dict[str, object]:
        backup_before_clear = payload.get("backup_before_clear", True)
        if not isinstance(backup_before_clear, bool):
            raise ValueError("backup_before_clear must be a boolean")
        return self.client.confirm_clear(
            {
                "challenge_token": str(payload.get("challenge_token", "")),
                "state_sha256": str(payload.get("state_sha256", "")),
                "confirmation_phrase": str(payload.get("confirmation_phrase", "")),
                "backup_before_clear": backup_before_clear,
            }
        )


def serve_annotation_admin_ui(
    *,
    base_url: str | None = None,
    backup_dir: Path | None = None,
    backup_retention: int | None = None,
    host: str = "127.0.0.1",
    port: int = 8772,
    open_browser: bool = True,
    admin_token: str | None = None,
) -> str:
    """Serve the owner dashboard on loopback and keep credentials in the backend."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "The annotation admin dashboard contains privileged controls and must bind only "
            "to a loopback host"
        )
    settings = load_dashboard_settings(
        base_url=base_url,
        backup_dir=backup_dir,
        backup_retention=backup_retention,
        admin_token=admin_token,
    )
    client = AnnotationApiClient(
        base_url=settings.base_url,
        admin_token=settings.admin_token,
    )
    backups = BackupManager(
        client=client,
        backup_dir=settings.backup_dir,
        retention=settings.backup_retention,
    )
    dashboard = AnnotationAdminDashboard(
        settings=settings,
        client=client,
        backups=backups,
        csrf_token=secrets.token_urlsafe(32),
    )
    handler_cls = _handler_factory(dashboard)
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
        backups.close()
        client.close()
    return url


def _handler_factory(
    dashboard: AnnotationAdminDashboard,
) -> type[BaseHTTPRequestHandler]:
    class AnnotationAdminRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "T-CRED-Admin"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            try:
                self._require_loopback_host()
                if parsed.path == "/api/health":
                    self._write_json({"status": "ok"})
                    return
                if parsed.path == "/api/config":
                    self._write_json(dashboard.config_payload())
                    return
                if parsed.path == "/api/status":
                    self._write_json(dashboard.status_payload())
                    return
                self._serve_static(parsed.path)
            except Exception as exc:  # noqa: BLE001 - return a sanitized owner-facing error
                self._handle_error(exc)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path not in {
                "/api/download",
                "/api/backup/run",
                "/api/backup/schedule",
                "/api/clear/prepare",
                "/api/clear/confirm",
            }:
                self._write_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                self._require_loopback_host()
                self._require_same_origin()
                self._require_csrf()
                payload = self._read_json_body(required=parsed.path not in {
                    "/api/download",
                    "/api/backup/run",
                    "/api/clear/prepare",
                })
                if parsed.path == "/api/download":
                    self._write_archive(dashboard.client.download_export())
                    return
                if parsed.path == "/api/backup/run":
                    self._write_json(dashboard.backups.run_backup(trigger="manual"))
                    return
                if parsed.path == "/api/backup/schedule":
                    raw_interval = payload.get("interval_hours")
                    if isinstance(raw_interval, bool):
                        raise ValueError("interval_hours must be an integer")
                    try:
                        interval = int(raw_interval)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("interval_hours must be an integer") from exc
                    self._write_json(dashboard.backups.configure(interval))
                    return
                if parsed.path == "/api/clear/prepare":
                    self._write_json(dashboard.prepare_clear())
                    return
                self._write_json(dashboard.confirm_clear(payload))
            except Exception as exc:  # noqa: BLE001 - return a sanitized owner-facing error
                self._handle_error(exc)

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
            self._write_json(
                {"error": "Cross-origin access is not supported"},
                status=HTTPStatus.METHOD_NOT_ALLOWED,
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _require_same_origin(self) -> None:
            origin = self.headers.get("Origin")
            if not origin:
                return
            parsed_origin = urlparse(origin)
            request_host = self.headers.get("Host", "").casefold()
            if (
                parsed_origin.scheme not in {"http", "https"}
                or parsed_origin.netloc.casefold() != request_host
            ):
                raise RemoteAnnotationError(
                    "Cross-origin administrative request rejected",
                    status_code=HTTPStatus.FORBIDDEN,
                )

        def _require_loopback_host(self) -> None:
            host = self.headers.get("Host", "")
            hostname = urlparse(f"//{host}").hostname
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise RemoteAnnotationError(
                    "Untrusted dashboard Host header rejected",
                    status_code=HTTPStatus.FORBIDDEN,
                )

        def _require_csrf(self) -> None:
            supplied = self.headers.get("X-Tcred-Admin-Csrf", "")
            if not hmac.compare_digest(supplied, dashboard.csrf_token):
                raise RemoteAnnotationError(
                    "Administrative session token is missing or invalid; refresh the dashboard",
                    status_code=HTTPStatus.FORBIDDEN,
                )

        def _read_json_body(self, *, required: bool) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length == 0 and not required:
                return {}
            if length <= 0:
                raise ValueError("Request body is required")
            if length > MAX_REQUEST_BYTES:
                raise RemoteAnnotationError(
                    "Request body is too large",
                    status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
            content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            payload = orjson.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _serve_static(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
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

        def _write_json(
            self,
            payload: dict[str, object],
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            content = orjson.dumps(payload)
            self.send_response(status)
            self._write_security_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _write_archive(self, archive: DownloadedArchive) -> None:
            self.send_response(HTTPStatus.OK)
            self._write_security_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{archive.filename}"',
            )
            self.send_header("X-Content-SHA256", archive.sha256)
            self.send_header("Content-Length", str(len(archive.content)))
            self.end_headers()
            self.wfile.write(archive.content)

        def _write_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors "
                "'none'; form-action 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'",
            )

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, RemoteAnnotationError):
                status = HTTPStatus(exc.status_code)
                self._write_json({"error": str(exc)}, status=status)
                return
            if isinstance(exc, (ValueError, orjson.JSONDecodeError)):
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(
                {"error": "The dashboard could not complete the request"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    return AnnotationAdminRequestHandler


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
