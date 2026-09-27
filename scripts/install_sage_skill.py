"""Explicit SAGE personal installation adapter for the pinned shared engine."""

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / ("support" if (HERE / "support").is_dir() else "sage_installer")))
from sage_package import SageVerifier  # noqa: E402
from standalone_layout import PersonalLayouts  # noqa: E402
from transaction_engine import TransactionEngine  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "command", choices=["plan", "apply", "verify", "inventory", "recover", "rollback"]
    )
    p.add_argument("--root", type=Path, help="Exact existing personal configuration root")
    p.add_argument("--target-kind", choices=PersonalLayouts.kinds)
    p.add_argument("--bundle", type=Path)
    p.add_argument("--plan", type=Path)
    p.add_argument("--transaction")
    p.add_argument("--adopt", action="append", default=[])
    a = p.parse_args()
    engine = TransactionEngine(SageVerifier(), PersonalLayouts())
    try:
        if a.command == "apply":
            if a.plan is None or any([a.root, a.target_kind, a.bundle, a.transaction, a.adopt]):
                raise ValueError("apply requires only --plan")
            result = engine.apply(json.loads(a.plan.read_text()))
        else:
            if a.root is None or a.target_kind is None:
                raise ValueError("explicit --root and --target-kind required")
            if a.command == "plan":
                if a.bundle is None:
                    raise ValueError("plan requires --bundle")
                result = engine.plan(a.root, a.bundle, a.target_kind, a.adopt)
            elif a.command in ["verify", "inventory"]:
                result = engine.inventory(a.root, a.target_kind)
                if a.command == "verify" and result["status"] != "verified":
                    raise ValueError("no managed SAGE installation")
            elif a.command == "recover":
                result = engine.recover(a.root, a.target_kind)
            else:
                result = engine.rollback(a.root, a.transaction, a.target_kind)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (ValueError, OSError) as exc:
        p.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
