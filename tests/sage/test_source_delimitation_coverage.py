"""Conformance gate: delimitation coverage at LLM message-construction sites.

CAS-ADR-020 binds every seam that places document text before a model to
supply the source as explicitly delimited data (clause (i)). The delimitation
authority is ``sage.adapters.abstraction_prompt.wrap_source_document``; its
docstring states that every provider routes the source through it. This gate
makes that statement enforced rather than asserted: it enumerates the
message-construction sites under ``sage/`` and fails when one places
non-literal text before a model without the wrapper.

Two detectors share one AST walk:

1. Message-dict content vetting. Every dict literal carrying a literal
   ``"role"`` key -- and every ``dict(role=...)`` call -- must have a content
   expression that is a string constant, a ``wrap_source_document(...)``
   call, or a ``_format_system_prompt(...)`` call. Anything else offends:
   the rule is stated over what the walk can read, so an opaque content
   expression is a violation to fix or allowlist, not a blind spot.

2. Messages shape at the model seam. Every ``apply_chat_template(...)``
   call and every call carrying a ``messages=`` keyword must receive a list
   literal of message dicts, or a name whose single binding in the enclosing
   function is such a list literal. This converts the silent bypass -- a
   message array assembled by a helper and handed straight to the model --
   into a failure at the seam, where detector 1 has no dict literal to see.

The gate is a recorded proxy for the obligation, not a proof: it raises the
cost of an undelimited call site rather than making one impossible -- the
same standing CAS-ADR-020 clause (k) assigns its other detectors. Shapes the
walk cannot see:

- Message dicts built without a literal ``"role"`` key at the construction
  site: ``{**base}`` unpacking, ``dict(**kwargs)``, item assignment,
  comprehensions, and typed message objects are invisible.
- Mutation of a vetted messages list between binding and the model call
  (``append``/``extend``/index assignment) is not tracked.
- A ``system=`` keyword whose value is a bare name is not traced to its
  origin; document text routed through a system prompt assembled elsewhere
  escapes the gate.
- Acceptance is by spelling, not binding: shadowing or aliasing
  ``wrap_source_document`` passes, and the wrapper's argument is not
  checked to be the document text.
- Raw-prompt entry points (a prompt string handed straight to a generate
  function) are covered only via the chat-template call that produced the
  prompt.
- The walk scopes to ``sage/``; message arrays assembled elsewhere and
  passed across the boundary are out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final, NamedTuple

import pytest

# ---------------------------------------------------------------------------
# Paths and scan scope
# ---------------------------------------------------------------------------

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WALK_ROOT: Final[Path] = _REPO_ROOT / "sage"

# ---------------------------------------------------------------------------
# Acceptance vocabulary and anti-vacuity floors
# ---------------------------------------------------------------------------

# Content expressions accepted at a message-dict site: calls to either
# function defined in sage/adapters/abstraction_prompt.py. Matching is by
# spelling (Name id or Attribute tail), never by substring.
ACCEPTED_CONTENT_CALLS: Final[frozenset[str]] = frozenset(
    {"wrap_source_document", "_format_system_prompt"}
)

# Files that must each contribute at least one message-dict site. If either
# adapter stops appearing, the scan's reach has regressed, not the tree.
EXPECTED_SITE_FILES: Final[frozenset[str]] = frozenset(
    {
        "sage/adapters/abstraction_anthropic.py",
        "sage/adapters/abstraction_qwen3.py",
    }
)

# Floors are read from the scanner's own output over the real tree, so a
# scan-scope regression (or acceptance logic that silently matches nothing)
# trips them. Current tree: 6 message-dict sites, 3 wrapped contents, 4 seams.
MIN_MESSAGE_DICT_SITES: Final[int] = 4
MIN_WRAPPED_CONTENTS: Final[int] = 2
MIN_MODEL_SEAMS: Final[int] = 3

# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

# Empty by default. Every entry added later requires a rationale alongside
# it: the reason string names which detector rule is being excepted and why
# the site is legitimately outside the delimitation obligation.
# Key: ("sage/path/to/file.py", "ClassName.method_name"); value: reason.
KNOWN_UNDELIMITED_SITES: Final[dict[tuple[str, str], str]] = {}

# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class _Site(NamedTuple):
    """One examined construction site in a scanned source file."""

    qualname: str
    lineno: int
    detector: str  # "message-dict" | "model-seam"
    ok: bool
    accepted_by: str | None  # acceptance form for ok message-dict sites


def _iter_with_scope(
    node: ast.AST, stack: tuple[ast.AST, ...] = ()
) -> "list[tuple[ast.AST, tuple[ast.AST, ...]]]":
    """Yield every node with its class/function ancestry stack."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        stack = (*stack, node)
    out = [(node, stack)]
    for child in ast.iter_child_nodes(node):
        out.extend(_iter_with_scope(child, stack))
    return out


def _qualname(stack: tuple[ast.AST, ...]) -> str:
    names = [n.name for n in stack if hasattr(n, "name")]
    return ".".join(names) or "<module>"


def _call_spelling(func: ast.expr) -> str | None:
    """The name a call is spelled with: bare id or attribute tail."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _content_acceptance(node: ast.expr | None) -> str | None:
    """The acceptance form of a content expression, or None if it offends."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "constant"
    if isinstance(node, ast.Call):
        spelling = _call_spelling(node.func)
        if spelling in ACCEPTED_CONTENT_CALLS:
            return spelling
    return None


def _message_dict_content(node: ast.expr) -> tuple[bool, ast.expr | None]:
    """(is_message_dict, content_expression) for a candidate node.

    A message dict is a dict literal with a literal ``"role"`` key, or a
    ``dict(role=...)`` call. The content expression is the value at the
    literal ``"content"`` key (or ``content=`` keyword); None when the dict
    qualifies but carries no readable content key.
    """
    if isinstance(node, ast.Dict):
        keys = [
            k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if "role" not in keys:
            return False, None
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "content":
                return True, value
        return True, None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        kwarg_names = {kw.arg for kw in node.keywords}
        if "role" not in kwarg_names:
            return False, None
        for kw in node.keywords:
            if kw.arg == "content":
                return True, kw.value
        return True, None
    return False, None


def _messages_shape_ok(expr: ast.expr, enclosing_function: ast.AST | None) -> bool:
    """Whether a model seam receives a readable list of message dicts."""
    if isinstance(expr, ast.List):
        return all(_message_dict_content(elt)[0] for elt in expr.elts)
    if isinstance(expr, ast.Name) and enclosing_function is not None:
        bindings: list[ast.expr] = []
        rebound_otherwise = False
        for sub in ast.walk(enclosing_function):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name) and target.id == expr.id:
                        bindings.append(sub.value)
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
                target = sub.target
                if isinstance(target, ast.Name) and target.id == expr.id:
                    rebound_otherwise = True
        if rebound_otherwise or len(bindings) != 1:
            return False
        return _messages_shape_ok(bindings[0], None)
    return False


def _seam_messages_expr(node: ast.Call) -> ast.expr | None:
    """The messages expression a model seam receives, or None if not a seam."""
    spelling = _call_spelling(node.func)
    if spelling == "apply_chat_template":
        for kw in node.keywords:
            if kw.arg == "messages":
                return kw.value
        if node.args:
            return node.args[0]
        return None
    for kw in node.keywords:
        if kw.arg == "messages":
            return kw.value
    return None


def _message_sites(source: str) -> list[_Site]:
    """Every message-dict and model-seam site in one source text."""
    sites: list[_Site] = []
    tree = ast.parse(source)
    for node, stack in _iter_with_scope(tree):
        if not isinstance(node, (ast.Dict, ast.Call)):
            continue
        qualname = _qualname(stack)
        is_message, content = _message_dict_content(node)
        if is_message:
            accepted_by = _content_acceptance(content)
            sites.append(
                _Site(
                    qualname,
                    node.lineno,
                    "message-dict",
                    accepted_by is not None,
                    accepted_by,
                )
            )
        if isinstance(node, ast.Call):
            messages_expr = _seam_messages_expr(node)
            if messages_expr is not None:
                enclosing = next(
                    (
                        n
                        for n in reversed(stack)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ),
                    None,
                )
                sites.append(
                    _Site(
                        qualname,
                        node.lineno,
                        "model-seam",
                        _messages_shape_ok(messages_expr, enclosing),
                        None,
                    )
                )
    return sites


def _walk_sites() -> list[tuple[str, _Site]]:
    """(posix relpath, site) for every examined site under the walk root."""
    found: list[tuple[str, _Site]] = []
    for path in sorted(WALK_ROOT.rglob("*.py")):
        relpath = path.relative_to(_REPO_ROOT).as_posix()
        for site in _message_sites(path.read_text(encoding="utf-8")):
            found.append((relpath, site))
    return found


def _format_violations(violations: list[tuple[str, _Site]]) -> str:
    return "\n".join(
        f"  {relpath}:{site.lineno} ({site.qualname}) [{site.detector}]"
        for relpath, site in violations
    )


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


def test_no_undelimited_message_sites() -> None:
    """Every construction site routes readable text through the wrapper.

    Fails when a message-dict content expression, or the messages shape at a
    model seam, is not one the delimitation rule accepts and the site is not
    allowlisted.
    """
    violations = [(relpath, site) for relpath, site in _walk_sites() if not site.ok]
    unexplained = [
        (relpath, site)
        for relpath, site in violations
        if (relpath, site.qualname) not in KNOWN_UNDELIMITED_SITES
    ]
    assert not unexplained, (
        "Text reaches an LLM message construction without delimitation:\n"
        f"{_format_violations(unexplained)}\n"
        "Route the content through "
        "sage.adapters.abstraction_prompt.wrap_source_document, or add "
        "(path, qualname) to KNOWN_UNDELIMITED_SITES with a reason "
        "explaining why the site is outside the obligation."
    )


def test_allowlist_entries_are_live() -> None:
    """Every allowlist entry matches a currently-detected violation.

    A stale or phantom entry would quietly pre-authorize a future violation
    at that key; remove entries whose sites no longer offend. Vacuously
    green while the allowlist is empty: this test discriminates nothing
    until an entry lands, and exists so that the first entry arrives with
    its liveness check already armed.
    """
    violation_keys = {(relpath, site.qualname) for relpath, site in _walk_sites() if not site.ok}
    stale = sorted(set(KNOWN_UNDELIMITED_SITES) - violation_keys)
    assert not stale, (
        f"Stale KNOWN_UNDELIMITED_SITES entries (no matching site): {stale}. "
        "Remove each stale entry."
    )


def test_gate_walk_covers_the_abstraction_adapters() -> None:
    """The scan reaches the real provider surface (anti-vacuity).

    Without this, every assertion above would pass over an empty enumeration
    and the gate would be silently disarmed by a scan-scope regression.
    """
    assert WALK_ROOT.name == "sage" and WALK_ROOT.is_dir()
    sites = _walk_sites()
    message_dicts = [s for s in sites if s[1].detector == "message-dict"]
    seams = [s for s in sites if s[1].detector == "model-seam"]
    contributing = {relpath for relpath, _ in message_dicts}
    missing = EXPECTED_SITE_FILES - contributing
    assert not missing, (
        f"Expected message-dict sites in {sorted(missing)}; none found. "
        "Did the scan scope or the detector trigger regress?"
    )
    assert len(message_dicts) >= MIN_MESSAGE_DICT_SITES
    wrapped = [s for _, s in message_dicts if s.accepted_by == "wrap_source_document"]
    assert len(wrapped) >= MIN_WRAPPED_CONTENTS, (
        "Fewer wrap_source_document acceptances than the known floor. "
        "Either the wrapper fell out of use or acceptance matching broke."
    )
    assert len(seams) >= MIN_MODEL_SEAMS


# ---------------------------------------------------------------------------
# Anti-coincidental detector self-tests
# ---------------------------------------------------------------------------
# Synthetic sources exercised as strings, never as tree mutations, so no
# real undelimited site is ever committed. Each acceptance rule is paired
# with a near-miss that must flag, so the gate cannot pass by matching
# nothing.

_D1_CASES: Final[dict[str, tuple[str, bool]]] = {
    "wrapped_name": (
        'def f(text):\n    return [{"role": "user", "content": wrap_source_document(text)}]\n',
        True,
    ),
    "wrapped_attribute": (
        "def f(text):\n"
        '    return [{"role": "user",'
        ' "content": prompt_mod.wrap_source_document(text)}]\n',
        True,
    ),
    "system_prompt_call": (
        "def f(doc_type):\n"
        '    return {"role": "system", "content": _format_system_prompt(doc_type)}\n',
        True,
    ),
    "constant_content": (
        'def f():\n    return {"role": "user", "content": "Probe."}\n',
        True,
    ),
    "bare_name": (
        'def f(text):\n    return {"role": "user", "content": text}\n',
        False,
    ),
    "fstring": (
        'def f(text):\n    return {"role": "user", "content": f"Document: {text}"}\n',
        False,
    ),
    "concat_smuggle": (
        "def f(text, suffix):\n"
        '    return {"role": "user", "content": wrap_source_document(text) + suffix}\n',
        False,
    ),
    "other_call": (
        'def f(text):\n    return {"role": "user", "content": build_prompt(text)}\n',
        False,
    ),
    "near_name": (
        'def f(text):\n    return {"role": "user", "content": wrap_source(text)}\n',
        False,
    ),
    "conditional": (
        "def f(text, flag):\n"
        '    return {"role": "user",'
        ' "content": wrap_source_document(text) if flag else text}\n',
        False,
    ),
    "dict_call": (
        'def f(text):\n    return dict(role="user", content=text)\n',
        False,
    ),
    "prebound_content": (
        "def f(text):\n"
        "    content = wrap_source_document(text)\n"
        '    return {"role": "user", "content": content}\n',
        False,
    ),
    "list_content_blocks": (
        'def f(block):\n    return {"role": "user", "content": [block]}\n',
        False,
    ),
    "missing_content_key": (
        'def f():\n    return {"role": "user"}\n',
        False,
    ),
}

_D2_CASES: Final[dict[str, tuple[str, bool]]] = {
    "template_inline_list": (
        "def f(tok, text):\n"
        "    return tok.apply_chat_template(\n"
        '        [{"role": "user", "content": wrap_source_document(text)}],\n'
        "        tokenize=False,\n"
        "    )\n",
        True,
    ),
    "template_bound_name": (
        "def f(self, text):\n"
        "    messages = [\n"
        '        {"role": "system", "content": _format_system_prompt(None)},\n'
        '        {"role": "user", "content": wrap_source_document(text)},\n'
        "    ]\n"
        "    return self._tokenizer.apply_chat_template(messages, tokenize=False)\n",
        True,
    ),
    "messages_kwarg_inline_list": (
        "async def f(client, text):\n"
        "    return await client.messages.create(\n"
        '        model="m",\n'
        '        messages=[{"role": "user", "content": wrap_source_document(text)}],\n'
        "    )\n",
        True,
    ),
    "template_opaque_call": (
        "def f(tok, text):\n"
        "    return tok.apply_chat_template(build_messages(text), tokenize=False)\n",
        False,
    ),
    "rebound_name": (
        "def f(tok, text):\n"
        "    messages = build(text)\n"
        "    return tok.apply_chat_template(messages, tokenize=False)\n",
        False,
    ),
    "shadowed_rebinding": (
        "def f(tok, text):\n"
        '    messages = [{"role": "user", "content": wrap_source_document(text)}]\n'
        "    messages = build(text)\n"
        "    return tok.apply_chat_template(messages, tokenize=False)\n",
        False,
    ),
    "messages_kwarg_opaque": (
        "async def f(client, text):\n"
        "    return await client.messages.create(messages=assemble(text))\n",
        False,
    ),
    "list_of_names": (
        "def f(tok, m1, m2):\n    return tok.apply_chat_template([m1, m2], tokenize=False)\n",
        False,
    ),
}


@pytest.mark.parametrize(
    ("source", "expected_ok"),
    [pytest.param(src, ok, id=name) for name, (src, ok) in _D1_CASES.items()],
)
def test_message_dict_detector(source: str, expected_ok: bool) -> None:
    """Detector 1 accepts exactly the delimited content forms."""
    sites = [s for s in _message_sites(source) if s.detector == "message-dict"]
    assert len(sites) == 1, f"expected one message-dict site, saw {sites}"
    assert sites[0].ok is expected_ok


@pytest.mark.parametrize(
    ("source", "expected_ok"),
    [pytest.param(src, ok, id=name) for name, (src, ok) in _D2_CASES.items()],
)
def test_model_seam_detector(source: str, expected_ok: bool) -> None:
    """Detector 2 requires a readable message-dict list at the model seam."""
    sites = [s for s in _message_sites(source) if s.detector == "model-seam"]
    assert len(sites) == 1, f"expected one model-seam site, saw {sites}"
    assert sites[0].ok is expected_ok


def test_roleless_dict_is_a_documented_blind_spot() -> None:
    """A dict with no literal role key yields no site (limit pin).

    This pins the first stated limit in the module docstring: widening the
    detector trigger to catch role-less construction must update both.
    """
    source = 'def f(text):\n    return {"content": text}\n'
    assert _message_sites(source) == []
