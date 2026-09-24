"""A stateful stand-in for the Azure CLI, run as ``az`` by the convergence tests.

Each call appends its argument vector to ``$AZURE_CALLS`` and answers from, or
writes to, the JSON state in ``$AZURE_STATE``. It is written to disk and
executed once per stubbed call, so it imports only the standard library: the
test writes it behind an interpreter line that disables site-packages, which a
dependency on anything else would fail.
"""

import json
import os
import sys
from pathlib import Path


def fake_azure() -> None:
    path = Path(os.environ["AZURE_STATE"])
    state = json.loads(path.read_text())
    args = sys.argv[1:]
    with Path(os.environ["AZURE_CALLS"]).open("a") as stream:
        stream.write(json.dumps(args) + "\n")

    def arg(name: str) -> str:
        return args[args.index(name) + 1]

    if state.get("read_failure") and args[:3] == ["containerapp", "revision", "list"]:
        sys.exit(7)
    if args[:3] == ["deployment", "sub", "show"]:
        result = state["deployment"]
        if "--query" in args:
            result = result["properties"]["outputs"][arg("--query").split(".")[2]]["value"]
    elif args[:2] == ["group", "show"]:
        result = {"tags": {"casPostgresMigration": state["fence"]}}
    elif args[:2] == ["containerapp", "show"]:
        result = {"name": arg("--name"), "properties": state["apps"][arg("--name")]["properties"]}
    elif args[:3] == ["containerapp", "secret", "list"]:
        result = state["apps"][arg("--name")]["secrets"]
        if "--show-values" not in args:
            result = [{"name": s["name"]} for s in result]
    elif args[:3] == ["containerapp", "revision", "list"]:
        result = state["apps"][arg("--name")]["revisions"]
        if "--all" not in args:
            result = [r for r in result if r["properties"]["active"]]
        if "--query" in args:
            result = next((r["name"] for r in result if r["properties"]["active"]), "")
    elif args[:3] in (
        ["containerapp", "revision", "restart"],
        ["containerapp", "revision", "activate"],
    ):
        revisions = state["apps"][arg("--name")]["revisions"]
        revision = next((r for r in revisions if r["name"] == arg("--revision")), None)
        if revision is None or (args[2] == "restart" and not revision["properties"]["active"]):
            print("revision does not exist or is inactive", file=sys.stderr)
            sys.exit(8)
        if state.get("fail_app") == arg("--name"):
            sys.exit(9)
        if not state.get("no_effect"):
            revision["properties"]["active"] = True
        result = {}
        path.write_text(json.dumps(state))
    else:
        raise AssertionError(args)
    print(result if isinstance(result, str) else json.dumps(result))


if __name__ == "__main__":
    fake_azure()
