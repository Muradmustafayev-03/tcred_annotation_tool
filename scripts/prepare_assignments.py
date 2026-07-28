from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

import orjson


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy a frozen blinded assignment release into the deployment repository"
    )
    parser.add_argument("source_release", type=Path)
    parser.add_argument("--destination", type=Path, default=Path("data"))
    args = parser.parse_args()

    source = args.source_release.resolve()
    destination = args.destination.resolve()
    source_manifest_path = source / "assignment_manifest.json"
    source_manifest = orjson.loads(source_manifest_path.read_bytes())
    expected_files = {
        f"annotator_{annotator_id:02d}.jsonl"
        for annotator_id in range(1, int(source_manifest["annotators"]) + 1)
    }
    source_hashes = source_manifest["artifact_sha256"]

    assignment_destination = destination / "assignments"
    assignment_destination.mkdir(parents=True, exist_ok=True)
    for stale in assignment_destination.glob("annotator_*.jsonl"):
        if stale.name not in expected_files:
            stale.unlink()

    assignment_hashes: dict[str, str] = {}
    for name in sorted(expected_files):
        source_path = source / "assignments" / name
        expected_digest = source_hashes[f"assignments/{name}"]
        actual_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(f"Source release checksum mismatch: {name}")
        shutil.copy2(source_path, assignment_destination / name)
        assignment_hashes[name] = actual_digest

    deployment_manifest = {
        "schema_version": "1.0",
        "annotation_protocol_version": source_manifest["annotation_protocol_version"],
        "annotators": source_manifest["annotators"],
        "assignments_per_annotator": source_manifest["assignments_per_annotator"],
        "total_assignments": source_manifest["total_assignments"],
        "source_release_package_sha256": source_manifest["package_sha256"],
        "source_assignment_manifest_sha256": hashlib.sha256(
            source_manifest_path.read_bytes()
        ).hexdigest(),
        "assignment_sha256": assignment_hashes,
        "excluded_private_artifacts": [
            "human_eval_key.jsonl",
            "human_eval_units.jsonl",
            "generated datasets",
            "QA checkpoints",
        ],
    }
    (destination / "deployment_manifest.json").write_bytes(
        orjson.dumps(
            deployment_manifest,
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
