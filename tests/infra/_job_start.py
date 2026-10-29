"""Shared reading of Container Apps job-start invocations for structural gates.

A job start that carries any container override — an image, command, argument,
environment, resource or container-name flag, or a ``--yaml`` execution
template — makes the CLI send a ``containers`` execution template instead of a
bodyless start. Such a start rebuilds the execution's container from the flags
alone, and it needs ``Microsoft.App/jobs/write`` in addition to
``Microsoft.App/jobs/start/action``. A plain start inherits the deployed
template and needs only the start action. These helpers decide which flags a
start invocation carries so gates can require the plain form.

What they read: ``az containerapp job start`` in shell text (any spacing,
backslash or backtick continuations, YAML folded scalars once parsed), in
Python string literals and f-strings, and as Python argv (``az(...)`` helper
calls and list or tuple literals); ``az rest`` calls naming a ``/start`` path;
and ``.arm("POST", …/start)`` calls. Word-list expansions and non-literal argv
elements in flag position are refused as unreadable. What they cannot read: a
scalar shell variable whose unquoted value expands into flags, and a command
assembled at run time from pieces none of which is a literal naming it.
"""

from __future__ import annotations

import ast
import re
from typing import Final

# The command words, tolerant of any run of spaces or tabs between them.
REST_COMMAND: Final[re.Pattern[str]] = re.compile(r"\baz[ \t]+rest\b")
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

# A job-start URL path: ``/start`` at the end, before a query string, or before
# a closing quote in shell text.
START_PATH: Final[re.Pattern[str]] = re.compile(r"/start(?:[?\s\"']|$)")

# A shell expansion that can supply any number of words, such as an array
# ``"${ARGS[@]}"`` or ``"$@"``: its flags cannot be read statically.
_WORD_LIST: Final[re.Pattern[str]] = re.compile(r"\$\{?\w*\[[@*]\]\}?|\$[@*]|\$\{[@*]\}")

_FLAG: Final[re.Pattern[str]] = re.compile(r"(?<![\w$-])(--?[A-Za-z][A-Za-z0-9-]*)(?==|\s|$|\))")


def shell_invocations(text: str, command: re.Pattern[str] = START_COMMAND) -> list[str]:
    """Every shell invocation of ``command`` in ``text``: the line from the
    command on, plus each continued line (a trailing backslash, or a trailing
    backtick in PowerShell), so flags are read per command."""
    found: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = command.search(line)
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
    """The flags a shell job-start invocation passes after the command words.
    A word-list expansion is reported as ``*`` so a gate refuses it."""
    return _FLAG.findall(invocation) + ["*" for _ in _WORD_LIST.finditer(invocation)]


def rest_start_invocations(text: str) -> list[str]:
    """Every ``az rest`` invocation in ``text`` whose words name a job-start
    path: a direct ARM start, which is plain only without a request body."""
    return [i for i in shell_invocations(text, REST_COMMAND) if START_PATH.search(i)]


def rest_sends_body(invocation: str) -> bool:
    """Whether an ``az rest`` start passes ``--body``/``-b`` (or an expansion
    that could)."""
    flags = shell_flags(invocation)
    return any(flag in {"--body", "-b", "*"} for flag in flags)


def yaml_strings(document: object) -> list[str]:
    """Every string value in a parsed YAML document. A folded scalar arrives
    already joined, so a command split across folded lines reads as one."""
    if isinstance(document, str):
        return [document]
    if isinstance(document, dict):
        return [s for value in document.values() for s in yaml_strings(value)]
    if isinstance(document, list):
        return [s for value in document for s in yaml_strings(value)]
    return []


def python_command_strings(source: str) -> list[tuple[int, str]]:
    """Every string literal in Python source, f-strings included (each
    interpolation rendered as ``${X}``), for a command written as one string
    and run through a shell."""
    found: list[tuple[int, str]] = []
    tree = ast.parse(source)
    pieces = {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for value in node.values
    }
    for node in ast.walk(tree):
        if id(node) in pieces:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = [
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "${X}"
                for v in node.values
            ]
            found.append((node.lineno, "".join(parts)))
    return found


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


# Allowlisted flags that take no value, so the next element is a flag again.
_SWITCHES: Final[frozenset[str]] = frozenset(
    {"--no-wait", "--only-show-errors", "--verbose", "--debug"}
)


def python_site_flags(elements: list[ast.expr]) -> list[str]:
    """The flags a Python argv job start passes. An element in flag position
    that is not a string literal (a starred expansion, or a name that could
    hold a flag) cannot be read statically and is reported as ``*`` so a gate
    refuses it; a non-literal element after a value-taking flag is its value."""
    flags: list[str] = []
    expecting_value = False
    for element in elements:
        literal = (
            element.value
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
            else None
        )
        if isinstance(element, ast.Starred):
            flags.append("*")
            expecting_value = False
        elif literal is not None and literal.startswith("-"):
            found = _FLAG.findall(literal)
            flags.extend(found)
            expecting_value = "=" not in literal and not (set(found) <= _SWITCHES)
        elif expecting_value:
            expecting_value = False
        elif literal is None:
            flags.append("*")
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
            and START_PATH.search(part.value)
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
    """The line of every string literal (f-strings included) naming a
    ``/start`` path: each one is a candidate direct job-start path that must
    belong to a recognized ARM start."""
    return [line for line, text in python_command_strings(source) if START_PATH.search(text)]


def overrides(flags: list[str]) -> list[str]:
    """The flags outside the plain-start allowlist."""
    return [flag for flag in flags if flag not in PLAIN_START_FLAGS]
