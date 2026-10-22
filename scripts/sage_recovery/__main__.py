"""Recovery commands default to help; only control --execute enables Azure mutations."""

import argparse
import importlib
import json

from .config import configure, load

COMMANDS = [
    "progress",
    "snapshot",
    "monitor",
    "verify-sources",
    "verify-projections",
    "metrics",
    "inspect-inputs",
    "export",
    "control",
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", help="Absolute path to the private, approved recovery configuration"
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Enable the guarded control command; requires operator authorization",
    )
    parser.add_argument(
        "--expected-config-sha256", help="Controller-pinned effective configuration"
    )
    parser.add_argument("--run-id", help="Current observer run for source/projection verification")
    parser.add_argument("--start-time", help="UTC timestamp for metrics queries")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return
    if not args.config:
        parser.error("--config is required")
    if args.command == "control" and not args.execute:
        parser.error("control requires --execute and explicit operator authorization")
    if args.execute and args.command != "control":
        parser.error("--execute is only valid for control")
    if args.command.startswith("verify-") and not args.run_id:
        parser.error("source/projection verification requires --run-id")
    if args.command == "metrics" and not args.start_time:
        parser.error("metrics requires --start-time")
    loaded = load(args.config, control=args.command == "control" and args.execute)
    if args.expected_config_sha256 and loaded.fingerprint != args.expected_config_sha256:
        raise ValueError("Recovery configuration changed since controller startup")
    configure(loaded)
    module_name = {
        "control": "advance",
        "export": "save_evidence",
        "inspect-inputs": "inspect_inputs",
    }.get(args.command, args.command.replace("-", "_"))
    module = importlib.import_module("scripts.sage_recovery." + module_name)
    if args.command == "progress":
        print(json.dumps(module.summarize(), indent=2))
    elif args.command == "export":
        print(json.dumps(module.export(), indent=2))
    elif args.command.startswith("verify-"):
        module.main(args.run_id)
    elif args.command == "metrics":
        module.main(args.start_time)
    else:
        module.main()


if __name__ == "__main__":
    main()
