from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from dotenv import dotenv_values

ANNOTATION_ENV_PATH = Path.cwd() / ".env"
ADMIN_TOKEN_ENV = "TCRED_ADMIN_TOKEN"
ADMIN_TOKEN_FILE = ".admin-token"
MIN_ADMIN_TOKEN_LENGTH = 43
ASSIGNMENT_SESSION_TTL_SECONDS = 24 * 60 * 60


class SecurityConfigurationError(RuntimeError):
    """Raised when the annotation service has no safe administrative credential."""


def load_admin_token(env_path: Path = ANNOTATION_ENV_PATH) -> str:
    """Load the admin token, preferring a deployment-injected environment variable."""
    file_values = dotenv_values(env_path) if env_path.exists() else {}
    token = str(os.environ.get(ADMIN_TOKEN_ENV) or file_values.get(ADMIN_TOKEN_ENV) or "").strip()
    validate_admin_token(token)
    return token


def load_or_create_admin_token(
    *,
    state_dir: Path,
    env_path: Path = ANNOTATION_ENV_PATH,
) -> str:
    """Resolve an injected token or create one in the private persistent state directory."""

    file_values = dotenv_values(env_path) if env_path.exists() else {}
    configured = str(
        os.environ.get(ADMIN_TOKEN_ENV) or file_values.get(ADMIN_TOKEN_ENV) or ""
    ).strip()
    if configured:
        validate_admin_token(configured)
        return configured

    state_dir.mkdir(parents=True, exist_ok=True)
    token_path = state_dir / ADMIN_TOKEN_FILE
    if token_path.exists():
        token = token_path.read_text(encoding="ascii").strip()
        validate_admin_token(token)
        _restrict_token_file(token_path)
        return token

    token = secrets.token_urlsafe(48)
    validate_admin_token(token)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(token_path, flags, 0o600)
    except FileExistsError:
        token = token_path.read_text(encoding="ascii").strip()
        validate_admin_token(token)
        _restrict_token_file(token_path)
        return token
    try:
        os.write(descriptor, f"{token}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _restrict_token_file(token_path)
    return token


def validate_admin_token(token: str) -> None:
    if not token:
        raise SecurityConfigurationError(
            f"{ADMIN_TOKEN_ENV} is required; configure it in {ANNOTATION_ENV_PATH} "
            "or inject it through the deployment secret manager"
        )
    if not token.isascii() or len(token) < MIN_ADMIN_TOKEN_LENGTH:
        raise SecurityConfigurationError(
            f"{ADMIN_TOKEN_ENV} must be an ASCII secret of at least "
            f"{MIN_ADMIN_TOKEN_LENGTH} characters"
        )
    if token.lower() in {
        "change-me",
        "replace-with-a-random-secret",
        "replace-with-at-least-32-random-bytes",
    }:
        raise SecurityConfigurationError(f"{ADMIN_TOKEN_ENV} still contains a placeholder")


def _restrict_token_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise SecurityConfigurationError(
            f"Could not restrict the generated admin token file: {path}"
        ) from exc


@dataclass
class AdminSecurity:
    admin_token: str = field(repr=False)
    assignment_ttl_seconds: int = ASSIGNMENT_SESSION_TTL_SECONDS
    _assignment_key: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_admin_token(self.admin_token)
        self._assignment_key = hmac.new(
            self.admin_token.encode("ascii"),
            b"tcred-assignment-session-v1",
            hashlib.sha256,
        ).digest()

    def admin_authorized(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, separator, supplied = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not supplied:
            return False
        try:
            return hmac.compare_digest(supplied.strip(), self.admin_token)
        except TypeError:
            return False

    def issue_assignment_token(
        self,
        *,
        annotator_id: int,
        assignment_id: str,
        now: int | None = None,
    ) -> str:
        issued_at = int(time.time()) if now is None else now
        expires_at = issued_at + self.assignment_ttl_seconds
        signature = self._assignment_signature(
            annotator_id=annotator_id,
            assignment_id=assignment_id,
            expires_at=expires_at,
        )
        return f"v1.{expires_at}.{signature}"

    def assignment_token_valid(
        self,
        token: str | None,
        *,
        annotator_id: int,
        assignment_id: str,
        now: int | None = None,
    ) -> bool:
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return False
        try:
            expires_at = int(parts[1])
        except ValueError:
            return False
        current_time = int(time.time()) if now is None else now
        latest_valid_expiry = current_time + self.assignment_ttl_seconds + 60
        if expires_at < current_time or expires_at > latest_valid_expiry:
            return False
        expected = self._assignment_signature(
            annotator_id=annotator_id,
            assignment_id=assignment_id,
            expires_at=expires_at,
        )
        try:
            return hmac.compare_digest(parts[2], expected)
        except TypeError:
            return False

    def _assignment_signature(
        self,
        *,
        annotator_id: int,
        assignment_id: str,
        expires_at: int,
    ) -> str:
        message = f"{annotator_id}:{assignment_id}:{expires_at}".encode()
        return hmac.new(self._assignment_key, message, hashlib.sha256).hexdigest()


class SlidingWindowLimiter:
    """Small in-process limiter for privileged endpoint authentication attempts."""

    def __init__(self, *, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()
        self._lock = Lock()

    def allowed(self, *, now: float | None = None) -> bool:
        current_time = time.monotonic() if now is None else now
        with self._lock:
            self._discard_expired(current_time)
            return len(self._events) < self.max_events

    def record(self, *, now: float | None = None) -> None:
        current_time = time.monotonic() if now is None else now
        with self._lock:
            self._discard_expired(current_time)
            self._events.append(current_time)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def _discard_expired(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()
