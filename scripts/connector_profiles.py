"""Prepare production/MVP connector files, or inspect them without printing secrets."""

import argparse
import json
from pathlib import Path

from app.connectors.profiles import initialize, inspect_profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "status", "check"))
    parser.add_argument("--directory", type=Path, default=Path(".runtime/connector-profiles"))
    parser.add_argument("--webhook-domain", default="")
    parser.add_argument("--chatwoot-url", default="")
    parser.add_argument("--image-version", default="pending")
    args = parser.parse_args()
    if args.action == "init":
        try:
            paths = initialize(args.directory, webhook_domain=args.webhook_domain,
                               chatwoot_url=args.chatwoot_url, image_version=args.image_version)
        except (OSError, ValueError) as error:
            raise SystemExit(f"profile initialization failed: {type(error).__name__}") from None
        print(f"Created {len(paths)} private profiles in {args.directory.absolute()}")
    reports = inspect_profiles(args.directory)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    if args.action == "check" and not all(report["ready"] for report in reports):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
