from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import (
    AppSettings,
    ConfigurationError,
    load_local_environment,
    prepare_state_directory,
    verify_assignment_bundle,
)
from .security import load_or_create_admin_token
from .server import serve_annotation_ui

LOGGER = logging.getLogger("tcred_annotation_tool")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the T-CRED annotation service")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "check", "admin-token"),
        default="serve",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        load_local_environment()
        settings = AppSettings.from_environment()
        bundle = verify_assignment_bundle(settings)
        if args.command == "check":
            print(json.dumps({**bundle, "status": "ok"}, indent=2))
            return 0

        prepare_state_directory(settings)
        admin_token = load_or_create_admin_token(state_dir=settings.state_dir)
        if args.command == "admin-token":
            print(admin_token)
            return 0

        LOGGER.info(
            "Starting protocol %s for %s annotators; persistent_storage=%s",
            bundle["annotation_protocol_version"],
            bundle["annotators"],
            settings.durable_storage,
        )
        serve_annotation_ui(
            assignment_dir=settings.assignment_dir,
            output_dir=settings.output_dir,
            host=settings.host,
            port=settings.port,
            annotator_count=settings.annotator_count,
            open_browser=False,
            admin_token=admin_token,
            backup_dir=settings.backup_dir,
            allow_non_loopback=settings.allow_non_loopback,
        )
        return 0
    except (ConfigurationError, OSError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
