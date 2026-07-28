from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import orjson
from dotenv import load_dotenv

from .io import read_jsonl
from .protocol import PROTOCOL_VERSION

ANNOTATOR_COUNT = 36
UNITS_PER_ANNOTATOR = 20
LOCAL_ENV_FILE = ".env"
ASSIGNMENT_NAME_PATTERN = re.compile(r"annotator_(\d{2})\.jsonl\Z")
RAILWAY_MARKERS = (
    "RAILWAY_PROJECT_ID",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_SERVICE_ID",
)


class ConfigurationError(RuntimeError):
    """Raised when deployment configuration could risk annotation data."""


def load_local_environment() -> None:
    """Load an optional repository-root .env without replacing deployed variables."""

    configured = os.environ.get("TCRED_ENV_FILE", "").strip()
    env_path = (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.cwd() / LOCAL_ENV_FILE).resolve()
    )
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


@dataclass(frozen=True)
class AppSettings:
    assignment_dir: Path
    deployment_manifest: Path
    state_dir: Path
    output_dir: Path
    backup_dir: Path
    host: str
    port: int
    annotator_count: int
    railway: bool
    durable_storage: bool
    allow_non_loopback: bool

    @classmethod
    def from_environment(cls) -> AppSettings:
        railway = any(os.environ.get(name) for name in RAILWAY_MARKERS)
        repository_root = _repository_root()
        assignment_dir = _configured_path(
            "TCRED_ASSIGNMENT_DIR",
            repository_root / "data" / "assignments",
        )
        deployment_manifest = _configured_path(
            "TCRED_DEPLOYMENT_MANIFEST",
            repository_root / "data" / "deployment_manifest.json",
        )

        railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        explicit_state = os.environ.get("TCRED_STATE_DIR", "").strip()
        state_dir = Path(explicit_state or railway_volume or repository_root / "runtime").resolve()
        durable_storage = bool(railway_volume) or _boolean_environment(
            "TCRED_STORAGE_IS_DURABLE",
            default=False,
        )
        allow_ephemeral = _boolean_environment(
            "TCRED_ALLOW_EPHEMERAL_STORAGE",
            default=not railway,
        )
        if railway and not durable_storage and not allow_ephemeral:
            raise ConfigurationError(
                "Railway storage is ephemeral and no volume is attached. Attach a Railway "
                "volume at /data before accepting annotations. This fail-safe prevents a "
                "redeploy from deleting completed research labels."
            )

        host = os.environ.get("HOST", "").strip() or ("0.0.0.0" if railway else "127.0.0.1")
        port = _integer_environment("PORT", default=8770, minimum=1, maximum=65535)
        allow_non_loopback = railway or _boolean_environment(
            "TCRED_ALLOW_NON_LOOPBACK",
            default=False,
        )
        return cls(
            assignment_dir=assignment_dir,
            deployment_manifest=deployment_manifest,
            state_dir=state_dir,
            output_dir=state_dir / "labels_raw",
            backup_dir=state_dir / "backups",
            host=host,
            port=port,
            annotator_count=ANNOTATOR_COUNT,
            railway=railway,
            durable_storage=durable_storage,
            allow_non_loopback=allow_non_loopback,
        )


def verify_assignment_bundle(settings: AppSettings) -> dict[str, object]:
    if not settings.deployment_manifest.is_file():
        raise ConfigurationError(
            f"Deployment manifest is missing: {settings.deployment_manifest}"
        )
    manifest = orjson.loads(settings.deployment_manifest.read_bytes())
    if not isinstance(manifest, dict):
        raise ConfigurationError("Deployment manifest must be a JSON object")
    expected_protocol = str(manifest.get("annotation_protocol_version", ""))
    if expected_protocol != PROTOCOL_VERSION:
        raise ConfigurationError(
            "Assignment protocol mismatch: "
            f"manifest={expected_protocol or 'missing'}, application={PROTOCOL_VERSION}"
        )
    if manifest.get("annotators") != settings.annotator_count:
        raise ConfigurationError("Deployment manifest has an unexpected annotator count")
    if manifest.get("assignments_per_annotator") != UNITS_PER_ANNOTATOR:
        raise ConfigurationError("Deployment manifest has an unexpected assignment size")

    artifact_hashes = manifest.get("assignment_sha256")
    if not isinstance(artifact_hashes, dict):
        raise ConfigurationError("Deployment manifest has no assignment checksums")
    expected_names = {
        f"annotator_{annotator_id:02d}.jsonl"
        for annotator_id in range(1, settings.annotator_count + 1)
    }
    actual_names = {path.name for path in settings.assignment_dir.glob("annotator_*.jsonl")}
    if actual_names != expected_names or set(artifact_hashes) != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        raise ConfigurationError(
            f"Frozen assignment set is incomplete or unexpected; missing={missing[:3]}, "
            f"unexpected={unexpected[:3]}"
        )

    total_rows = 0
    for name in sorted(expected_names):
        path = settings.assignment_dir / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact_hashes[name]:
            raise ConfigurationError(f"Frozen assignment checksum mismatch: {name}")
        rows = read_jsonl(path)
        if len(rows) != UNITS_PER_ANNOTATOR:
            raise ConfigurationError(
                f"{name} contains {len(rows)} units; expected {UNITS_PER_ANNOTATOR}"
            )
        annotator_id = ASSIGNMENT_NAME_PATTERN.fullmatch(name)
        assert annotator_id is not None
        expected_key = f"annotator_{annotator_id.group(1)}"
        _verify_assignment_rows(path, rows, expected_key=expected_key)
        total_rows += len(rows)

    expected_total = settings.annotator_count * UNITS_PER_ANNOTATOR
    if total_rows != expected_total or manifest.get("total_assignments") != expected_total:
        raise ConfigurationError("Deployment manifest has an unexpected total assignment count")
    return {
        "annotation_protocol_version": PROTOCOL_VERSION,
        "annotators": settings.annotator_count,
        "assignments_per_annotator": UNITS_PER_ANNOTATOR,
        "total_assignments": total_rows,
        "source_release_package_sha256": manifest.get("source_release_package_sha256"),
    }


def prepare_state_directory(settings: AppSettings) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "posix" or os.geteuid() != 0:
        return
    if not _boolean_environment("TCRED_DROP_PRIVILEGES", default=False):
        return
    uid = _integer_environment("TCRED_RUNTIME_UID", default=10001, minimum=1, maximum=2**31 - 1)
    gid = _integer_environment("TCRED_RUNTIME_GID", default=10001, minimum=1, maximum=2**31 - 1)
    for root, directories, files in os.walk(settings.state_dir):
        os.chown(root, uid, gid)
        for name in [*directories, *files]:
            os.chown(Path(root) / name, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _verify_assignment_rows(
    path: Path,
    rows: list[dict[str, object]],
    *,
    expected_key: str,
) -> None:
    unit_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if row.get("annotator_id") != expected_key:
            raise ConfigurationError(f"{path.name}:{index} has the wrong annotator pseudonym")
        if row.get("assignment_index") != index:
            raise ConfigurationError(f"{path.name}:{index} is out of frozen assignment order")
        unit_id = str(row.get("unit_id", "")).strip()
        if not unit_id or unit_id in unit_ids:
            raise ConfigurationError(f"{path.name}:{index} has a missing or duplicate unit id")
        unit_ids.add(unit_id)
        for required in ("question", "reference_answer", "answer_text", "applicable_fields"):
            if required not in row:
                raise ConfigurationError(f"{path.name}:{index} is missing {required}")
        if "annotation" in row:
            raise ConfigurationError(f"{path.name}:{index} contains mutable annotation state")
        _verify_graph_paths(path, index=index, graph_paths=row.get("graph_paths"))


def _verify_graph_paths(path: Path, *, index: int, graph_paths: object) -> None:
    if graph_paths is None:
        return
    if not isinstance(graph_paths, list):
        raise ConfigurationError(f"{path.name}:{index} graph_paths must be a list")
    for graph_path in graph_paths:
        if not isinstance(graph_path, dict):
            raise ConfigurationError(f"{path.name}:{index} contains a malformed graph path")
        nodes = graph_path.get("nodes")
        edges = graph_path.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes or not edges:
            raise ConfigurationError(f"{path.name}:{index} contains an empty graph path")
        if any(
            not isinstance(node, dict) or not str(node.get("label", "")).strip()
            for node in nodes
        ):
            raise ConfigurationError(f"{path.name}:{index} has an unenriched graph node")
        required_edge_fields = {"source", "target", "relation_label", "directional", "symmetric"}
        if any(
            not isinstance(edge, dict) or not required_edge_fields.issubset(edge)
            for edge in edges
        ):
            raise ConfigurationError(f"{path.name}:{index} has an unenriched graph edge")


def _repository_root() -> Path:
    candidate = Path.cwd().resolve()
    if (candidate / "data" / "assignments").is_dir():
        return candidate
    return Path(__file__).resolve().parents[2]


def _configured_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default.resolve()


def _boolean_environment(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer_environment(name: str, *, default: int, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    try:
        parsed = default if value is None or not value.strip() else int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed
