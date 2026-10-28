"""Shared reading of Container Apps job-start invocations for structural gates.

A job start that carries any container override — an image, command, argument,
environment, resource or container-name flag, or a ``--yaml`` execution
template — makes the CLI send a ``containers`` execution template instead of a
bodyless start. Such a start rebuilds the execution's container from the flags
alone, and it needs ``Microsoft.App/jobs/write`` in addition to
``Microsoft.App/jobs/start/action``. A plain start inherits the deployed
template and needs only the start action. These helpers decide which flags a
start invocation carries so gates can require the plain form.
"""

from __future__ import annotations

import ast
import re
from typing import Final

# The command words, tolerant of any run of spaces or tabs between them.
START_COMMAND: Final[re.Pattern[str]] = re.compile(r"\baz[ \t]+containerapp[ \t]+job[ \t]+start\b")

# Every flag a plain start may carry: job addressing and the CLI's global
# output and diagnostic flags. Anything else — including an abbreviation such
# as ``--env``, which the CLI's argument parser expands to ``--env-vars`` — is
# treated as an override.
PLAIN_START_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--name",
        "-n",
        "--resource-group",
        "-g",
        "--ids",
        "--subscription",
        "--no-wait",
        "--query",
        "--output",
        "-o",
        "--only-show-errors",
        "--verbose",
        "--debug",
    }
)

_FLAG: Final[re.Pattern[str]] = re.compile(r"(?<![\w$-])(--?[A-Za-z][A-Za-z0-9-]*)(?==|\s|$|\))")


def shell_invocations(text: str) -> list[str]:
    """Every shell job-start invocation in ``text``: the line from the command
    on, plus each continued line (a trailing backslash, or a trailing backtick
    in PowerShell), so flags are read per command."""
    found: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = START_COMMAND.search(line)
        if match is None:
            continue
        taken = [line[match.end() :]]
        cursor = index
        while taken[-1].rstrip().endswith(("\\", "`")) and cursor + 1 < len(lines):
            cursor += 1
            taken.append(lines[cursor])
        found.append("\n".join(taken))
    return found


def shell_flags(invocation: str) -> list[str]:
    """The flags a shell job-start invocation passes after the command words."""
    return _FLAG.findall(invocation)


def fenced_code(markdown: str) -> str:
    """The contents of every fenced code block in ``markdown``; prose that
    names a command, such as a description of a broken form, is excluded."""
    blocks: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            inside = not inside
            continue
        if inside:
            blocks.append(line)
    return "\n".join(blocks)


def python_start_sites(source: str) -> list[tuple[int, list[ast.expr]]]:
    """Every Python argv job start: a call's positional arguments, or a list or
    tuple literal, holding the consecutive strings ``containerapp``, ``job``,
    ``start``. Each site is its line and the elements after those words, which
    covers both an ``az(...)`` helper call and a ``["az", ...]`` subprocess argv."""
    sites: list[tuple[int, list[ast.expr]]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            elements = list(node.args)
        elif isinstance(node, (ast.List, ast.Tuple)):
            elements = list(node.elts)
        else:
            continue
        words = [e.value if isinstance(e, ast.Constant) else None for e in elements]
        for at in range(len(words) - 2):
            if words[at : at + 3] == ["containerapp", "job", "start"]:
                sites.append((node.lineno, elements[at + 3 :]))
                break
    return sites


def python_site_flags(elements: list[ast.expr]) -> list[str]:
    """The flags a Python argv job start passes. A starred element cannot be
    read statically and is reported as ``*`` so a gate refuses it."""
    flags: list[str] = []
    for element in elements:
        if isinstance(element, ast.Starred):
            flags.append("*")
        elif (
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and element.value.startswith("-")
        ):
            flags.extend(_FLAG.findall(element.value))
    return flags


def arm_start_posts(source: str) -> list[ast.Call]:
    """Every ``.arm("POST", <path ending in /start>, ...)`` call: a direct ARM
    job start, which is plain only when it sends no request body."""
    posts: list[ast.Call] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "arm"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "POST"
        ):
            continue
        if any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and part.value.endswith("/start")
            for part in ast.walk(node.args[1])
        ):
            posts.append(node)
    return posts


def arm_post_sends_body(call: ast.Call) -> bool:
    """Whether an ARM start call passes a body: any ``body``/``raw`` keyword
    other than a literal ``None``, any ``**`` expansion, or a third positional."""
    if len(call.args) > 2:
        return True
    for keyword in call.keywords:
        if keyword.arg is None:
            return True
        if keyword.arg in {"body", "raw"} and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        ):
            return True
    return False


def start_path_lines(source: str) -> list[int]:
    """The line of every string literal ending in ``/start``: each one is a
    candidate direct job-start path that must belong to a recognized ARM start."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith("/start")
    ]


def overrides(flags: list[str]) -> list[str]:
    """The flags outside the plain-start allowlist."""
    return [flag for flag in flags if flag not in PLAIN_START_FLAGS]
