"""The preflight's expected-vaults list and its operator-input validation.

The split of ``PREFLIGHT_EXPECTED_VAULTS`` is a property of the shell rather
than of the script, so these scenarios run under each ``bash`` the host offers.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from typing import Final

import pytest

from tests.deploy._preflight_harness import (
    _BASH,
    _BASH_BINS,
    _NEEDS_BASH,
    _NEEDS_RUNTIME,
    _base_env,
    _bash_inventory,
    _detail,
    _green,
    _run,
    _verdicts,
    _write_stub_cmd,
    serve,
)

# --------------------------------------------------------------------------- #
# D2. The expected-vaults comma list (vault_load's per-id loop)               #
#                                                                             #
# ``check_vault_load`` splits PREFLIGHT_EXPECTED_VAULTS under ``local IFS=','``
# and asserts each id came back from /sage_vaults. A tenant starts life with one
# vault and grows: the deploy variable is operator-edited, and the multi-id path
# is the one an operator reaches first when adding the second vault. These
# scenarios hold the split's seven decided behaviours -- iterate every id, skip a
# null element, name only the absent id, treat surrounding whitespace as part of
# the id, match a whole id rather than a substring of one, compare an id
# literally rather than as a pattern, and take an id as written rather than as a
# pattern over the working directory -- and they run under each interpreter the
# host offers, because the split's semantics are a property of the shell rather
# than of the script.
#
# One input never reaches the split: a control character anywhere in the list
# is refused at required-input validation, since no entry carrying one can be an
# id SAGE advertises, and the scenario that pins it runs under the same matrix.
#
# The stub advertises two vaults (``_VAULTS_BODY``), so a multi-id expectation is
# satisfiable without a bespoke responder.
# --------------------------------------------------------------------------- #
#: One case per inventoried interpreter, named by the version it proves rather
#: than by a filesystem path that says nothing about the floor. A label can repeat
#: -- two installs may report the same major.minor -- so when it does, *every*
#: case sharing it takes a positional suffix (``bash5.2#0``, ``bash5.2#1``), which
#: keeps the ids distinct and keeps the pair visibly a pair. A lone label is left
#: bare, so the ordinary single-interpreter host reads as ``bash3.2``.
_BASH_BIN_PARAMS: Final[list[pytest.param]] = [
    pytest.param(
        path,
        id=f"bash{label}" if [lb for lb, _ in _BASH_BINS].count(label) == 1 else f"bash{label}#{i}",
    )
    for i, (label, path) in enumerate(_BASH_BINS)
]

#: The detail line a satisfied vault_load emits against ``_VAULTS_BODY``.
_VAULT_LOAD_CLEAN_DETAIL: Final[str] = "2 vault(s) loaded; expected id(s) present"


def _green_one_vault(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
    """``_green`` with the second vault withdrawn from the registry.

    A healthy tenant that simply holds fewer vaults than the deploy variable
    expects. Used as the differential against ``_green``: the same multi-id
    expectation must pass on one and fail on the other, which is what shows the
    trailing id is genuinely consulted rather than carried along.
    """
    if path.split("?", 1)[0] == "/sage_vaults":
        return 200, '{"vaults":[{"id":"cas","name":"CAS"}],"count":1}', {}
    return _green(method, path, body)


def _green_metacharacter_vault(
    method: str, path: str, body: bytes
) -> tuple[int, str, dict[str, str]]:
    """``_green`` with a vault whose id carries a regex metacharacter.

    The only fixture in which an *advertised* id is not slug-shaped, and the
    only one that can separate comparing an id literally from refusing to
    compare a metacharacter-bearing one at all.
    """
    if path.split("?", 1)[0] == "/sage_vaults":
        return (
            200,
            '{"vaults":[{"id":"c.s","name":"CAS"},{"id":"test","name":"Test"}],"count":2}',
            {},
        )
    return _green(method, path, body)


def test_bash_inventory_is_populated_and_labelled() -> None:
    """The interpreter inventory found something, and labelled it credibly.

    Every scenario below is parametrized over :data:`_BASH_BINS`. An inventory
    that resolved to nothing would collect zero cases from each of them -- a
    green suite covering none of the behaviour it claims -- and pytest reports a
    parametrization over an empty sequence as a skip, not a failure. This is the
    detector control that makes that state loud.
    """
    assert _BASH_BINS, "no bash interpreter resolved; the split scenarios would collect nothing"
    labels = [label for label, _ in _BASH_BINS]
    assert all(re.fullmatch(r"\d+\.\d+", label) for label in labels), labels
    # Uniqueness is asserted on the real path, which is what the resolution
    # deduplicates on. Labels are deliberately not required to be unique: two
    # separate installs can report the same major.minor, and both should run.
    reals = [Path(path).resolve() for _, path in _BASH_BINS]
    assert len(set(reals)) == len(reals), f"an interpreter was inventoried twice: {reals}"
    if _BASH is not None:
        assert Path(_BASH).resolve() in set(reals), "the PATH bash is missing from the inventory"


def _write_version_stub(tmp_path: Path, name: str, version: str) -> str:
    """An interpreter-shaped stub that reports ``version`` and ignores its args.

    Stands in for a second bash at a distinct path. A symlink would not do: the
    resolution deduplicates by real path, so a link to the host's own bash is
    correctly collapsed rather than counted twice.
    """
    return _write_stub_cmd(tmp_path, name, f'printf %s "{version}"\n')


def test_bash_inventory_keeps_distinct_interpreters_and_collapses_aliases(tmp_path: Path) -> None:
    """The resolution counts interpreters by real path and trusts only real versions.

    Held against synthetic candidates rather than against the host's own
    interpreters, because a single-bash host -- this suite's normal case, and
    CI's -- cannot distinguish any of these outcomes from any other. Without this
    the sibling scenario above is the only check on the resolution, and it passes
    unchanged against a resolution that silently drops one interpreter of two,
    which is the one bug that would quietly stop exercising the declared floor.
    """
    first = _write_version_stub(tmp_path, "bash-first", "3.2")
    second = _write_version_stub(tmp_path, "bash-second", "5.2")
    same_as_second = _write_version_stub(tmp_path, "bash-second-again", "5.2")
    unparseable = _write_version_stub(tmp_path, "bash-mute", "not-a-version")

    assert _bash_inventory([first, second]) == (("3.2", first), ("5.2", second)), (
        "two interpreters at distinct real paths must both survive"
    )
    # The case that separates keying on the real path from keying on the version
    # label. Two separate installs reporting the same major.minor are two
    # interpreters and must both run; a label-keyed resolution would silently keep
    # one, and the distinct-version case above cannot tell the two policies apart.
    assert _bash_inventory([second, same_as_second]) == (
        ("5.2", second),
        ("5.2", same_as_second),
    ), "two installs reporting the same version are still two interpreters"
    assert _bash_inventory([first, first]) == (("3.2", first),), (
        "one interpreter named twice must be inventoried once"
    )
    assert _bash_inventory([unparseable]) == (), "a candidate with no parseable version is dropped"
    assert _bash_inventory([None, str(tmp_path / "absent"), second]) == (("5.2", second),), (
        "an unresolved name and a nonexistent path are skipped without losing a real candidate"
    )


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
def test_vault_load_passes_with_every_expected_id_present(bash_bin: str) -> None:
    """Two expected ids, both advertised -> PASS with the clean detail line.

    The happy path the deploy variable reaches as soon as a tenant holds a second
    vault, and the source of the message the scenarios below compare against.

    Asserted as a *differential*, because the pass on its own is not evidence of
    anything: the single-id expectation this suite already carried produces a
    byte-identical PASS and detail line, so a run that consulted only the first
    id would look exactly like this one. Withdrawing the second vault from the
    registry and re-running the same expectation is what makes the trailing id
    load-bearing -- the second arm can only fail if that id was looked up.
    """
    env = {"PREFLIGHT_EXPECTED_VAULTS": "cas,test", "PREFLIGHT_CHECKS": "vault_load"}
    with serve(_green) as url:
        proc = _run(_base_env(url, **env), bash_bin=bash_bin)
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"both ids are present:\n{proc.stdout}\n{proc.stderr}"
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert _detail(proc.stdout, "vault_load") == _VAULT_LOAD_CLEAN_DETAIL, proc.stdout

    with serve(_green_one_vault) as url:
        withdrawn = _run(_base_env(url, **env), bash_bin=bash_bin)
    detail = _detail(withdrawn.stdout, "vault_load")
    assert withdrawn.returncode != 0, (
        f"the same expectation must fail once a vault it names is gone:\n{withdrawn.stdout}"
    )
    assert _verdicts(withdrawn.stdout).get("vault_load") == "FAIL", withdrawn.stdout
    assert "test" in detail, f"the withdrawn id must be named: {detail!r}"


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["cas,,test", ",cas,test"], ids=["interior", "leading"])
def test_vault_load_skips_null_elements_in_the_expected_list(expected: str, bash_bin: str) -> None:
    """A null element in the comma list is skipped, not looked up.

    With IFS holding only a comma, an interior ``,,`` and a leading ``,`` each
    produce a genuine empty field, which the loop's ``[ -z "$v" ] && continue``
    guard drops. Without that guard the empty id is searched for, never found,
    and the run fails while naming nothing -- the guard is what keeps a stray
    comma in an operator-edited variable from failing a healthy deploy.

    A *trailing* comma is deliberately not among these shapes: it yields exactly
    one field, so it never reaches the guard, and including it would read as
    coverage while proving nothing.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"a null element must not fail the gate:\n{proc.stdout}"
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert _detail(proc.stdout, "vault_load") == _VAULT_LOAD_CLEAN_DETAIL, proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["cas,nosuch", "nosuch,cas"], ids=["last", "first"])
def test_vault_load_names_only_the_absent_id(expected: str, bash_bin: str) -> None:
    """One absent id among several fails, and the detail names *that* id.

    Both directions of the message are asserted. That the absent id appears
    proves the report is specific; that the present id does *not* appear proves
    it reports what is missing rather than echoing what was expected -- the
    difference between an operator reading which vault failed to load and
    re-checking the whole list by hand.

    Both *positions* are exercised too, and that is what makes this the
    loop-completeness scenario: an absence in the trailing position is only
    detected by a loop that did not stop early, and one in the leading position
    only by a loop that did not skip its first iteration. Either shape alone
    leaves half the loop unproven.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, "an absent expected id must fail the run"
    assert verdicts.get("vault_load") == "FAIL", verdicts
    assert "nosuch" in detail, f"the absent id must be named: {detail!r}"
    assert "cas" not in detail, f"the present id must not be reported missing: {detail!r}"


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    "expected", [" cas , test ", " cas,test "], ids=["both-sides", "opposite-sides"]
)
def test_vault_load_does_not_trim_whitespace_around_ids(expected: str, bash_bin: str) -> None:
    """Whitespace around an id is part of the id, and fails the lookup.

    IFS holds only a comma, so a space is not a delimiter: `` cas `` is a
    distinct id from ``cas`` and its lookup fails even though ``cas`` is
    advertised. This is the decided contract rather than an oversight -- the
    documentation-side half of it is held by
    ``tests/sage/test_cloud_test_vault_seed_config.py``, which splits a
    documented value exactly as this loop does and refuses to trim, on the
    grounds that a check more permissive than the thing it checks would pass a
    value the deploy then rejects. Pinning the behaviour here keeps the two
    halves from drifting apart.

    The two shapes are not redundant, and the second is what gives the scenario
    teeth. ``" cas , test "`` pads both ids on both sides, which is the shape an
    operator actually types -- but a trim that strips only one side leaves each id
    still padded, still missing, and still reported once, so that shape alone is
    satisfied identically by a one-sided trim. ``" cas,test "`` pads the two ids on
    opposite sides, so a leading-only strip rescues ``cas`` while a trailing-only
    strip rescues ``test``: either drops an id out of the missing list and fails
    the assertion below.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, "padded ids are not the advertised ids; the gate must fail"
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # Both ids stay missing. Under the opposite-sides shape a trim of either side
    # rescues exactly one of them, so requiring both is what separates no-trim
    # from a partial trim.
    assert detail.count("cas") == 1 and detail.count("test") == 1, detail
    assert "missing expected id(s)" in detail, detail


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("expected", ["ca", "as", "Test"], ids=["prefix", "suffix", "name"])
def test_vault_load_requires_a_whole_id_not_a_substring(expected: str, bash_bin: str) -> None:
    """Only a whole advertised *id* satisfies the gate.

    Three shapes that must not be credited, each pinning a different part of the
    lookup pattern, and each verified by the mutation that admits it:

    * ``ca``, a prefix of the advertised ``cas`` -- held by the closing quote.
      Dropping it admits the prefix while the other two shapes stay green.
    * ``as``, a suffix of the same id -- held by the left bound as a whole.
      Reducing the pattern to a bare ``$v"`` admits the suffix while the prefix
      stays green.
    * ``Test``, which is no id at all: it is the *name* of the advertised ``test``
      vault. Held by the pattern being keyed on ``"id"``. A rival matching any
      quoted value, ``grep -qE "\\"$v\\""``, passes every other scenario in this
      section and credits this expectation through ``"name":"Test"`` -- so without
      this shape nothing pins that the gate reads ids rather than whatever the
      registry happens to quote.

    One rival is deliberately *not* claimed as excluded by any single shape here.
    Dropping only the opening quote (``…:[[:space:]]*$v"``) leaves a pattern that
    cannot match any JSON string value, since the quote opening the advertised
    value still sits between the colon and the id; it is caught by the happy-path
    and null-element scenarios above rather than by this one.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, "a substring must not satisfy the expected-id lookup"
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # The rendered id, not a substring of it: `"ca" in detail` is also satisfied
    # by a run that reported the advertised `cas` missing, which is a different
    # defect entirely and must not be credited as this one.
    assert detail.endswith(f"missing expected id(s): {expected}"), detail


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    "expected",
    ["c.s", ".*", "cas|test", "ca[s]"],
    ids=["dot", "wildcard", "alternation", "bracket"],
)
def test_vault_load_treats_a_metacharacter_id_as_a_literal(expected: str, bash_bin: str) -> None:
    """An expected id is compared as a string, never evaluated as a pattern.

    The lookup has to assert the surrounding JSON shape, and a regular
    expression is how it does so -- but an expected id interpolated into that
    expression is read as pattern syntax too. An id carrying a metacharacter
    then matches an advertised id it is not equal to, and the gate credits a
    vault that never loaded. That is the one direction of error a preflight
    cannot afford: an operator reads a green vault_load as proof the named vault
    is serving.

    None of these shapes is an id anyone means to type; they are what an
    operator-edited list can acquire by accident. Each pins a different escape
    class, so no partial fix passes the set:

    * ``c.s`` -- an unescaped ``.`` spans the advertised ``cas``. Excluded by
      escaping ``.`` alone.
    * ``.*`` -- the same defect at its widest: satisfied by *whatever* loaded,
      so a gate naming a specific vault stops naming one at all. Also excluded
      by escaping ``.``, and carried because it shows the severity rather than
      the mechanism.
    * ``cas|test`` -- alternation, whose left branch matches through the
      advertised ``cas``. Survives an escape covering only ``.`` and ``*``, so
      it separates a partial escape from a literal comparison.
    * ``ca[s]`` -- a bracket expression. Survives an escape covering ``.``,
      ``*`` and ``|``, and is the shape a hand-rolled character-class escape is
      likeliest to mangle.

    Every shape is credited by an unescaped interpolation, so the set is red
    against a pattern-matching lookup and green only once the comparison is
    literal. The scenario is a differential in the same sense as its siblings:
    a lookup that simply failed everything would satisfy it, and what excludes
    that is the happy-path scenario above, which can only stay green if a
    genuine id still resolves.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, (
        f"a metacharacter id must not be credited by the ids it patterns over:\n{proc.stdout}"
    )
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # The rendered id, as in the sibling scenario: a bare containment check is
    # also satisfied by a run that reported some *other* id missing, which is a
    # different defect and must not be credited as this one.
    assert detail.endswith(f"missing expected id(s): {expected}"), detail


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    ("expected", "escaped_entries"),
    [
        ("zzz\ncas", ("$'zzz\\ncas'",)),
        ("cas\nzzz", ("$'cas\\nzzz'",)),
        ("zzz\rcas", ("$'zzz\\rcas'",)),
        ("zzz\tcas", ("$'zzz\\tcas'",)),
        ("cas\n", ("$'cas\\n'",)),
        ("cas,zzz\ncas", ("$'zzz\\ncas'",)),
        ("zzz\ncas,te\rst", ("$'zzz\\ncas'", "$'te\\rst'")),
    ],
    ids=[
        "match-second",
        "match-first",
        "carriage-return",
        "tab",
        "trailing-newline",
        "second-entry",
        "two-entries",
    ],
)
def test_vault_load_does_not_credit_a_multi_line_id_by_one_of_its_lines(
    expected: str, escaped_entries: tuple[str, ...], bash_bin: str
) -> None:
    """A control character in the expected list is refused before any check runs.

    A vault id is a lowercase slug, so an entry carrying a newline, carriage
    return, tab, or any other control character is not an unusual id but a
    malformed list: no such entry can equal an id SAGE advertises. ``IFS=','``
    keeps a newline inside its field, and the variable is supplied by a GitHub
    Actions repository variable, which admits multi-line values -- so the shape
    is reachable, and a trailing newline is the one it most often takes.

    Refusal rather than rendering, because rendering has no legible form. A
    comparison that let the entry through would print it across as many matrix
    lines as it has, and collapsing the newline to a space would read as two
    missing ids -- the misreading that a whole-value comparison exists to
    prevent. Refusing at the required-input stage also spends no network call
    on a value that could never pass.

    What each assertion excludes:

    * the usage exit code, no banner, no matrix row, and no request reaching
      the stub -- a refusal raised from inside ``check_vault_load``, or after the
      checks start, fails all four; the stub is served so that a run which got
      past validation would have something to call, which is what makes its
      silence evidence;
    * the message anchored on the ``preflight:`` prefix -- the usage text itself
      lists the variable, so a bare containment check is met by the boilerplate;
    * the escaped entry whole on that one line -- an unescaped entry splits
      across lines and a two-line value reads as two ids;
    * ``second-entry`` showing no ``cas,`` -- the message names the offending
      entry, not the whole value;
    * ``two-entries`` naming both -- a refusal that stops at the first offending
      entry passes every single-entry case;
    * ``carriage-return`` and ``tab`` -- a refusal keyed on the newline alone
      passes the first two cases and neither of these.

    The refusal's other boundary -- that a space, a dot, a glob character, or an
    empty element is *not* refused -- is held by the sibling scenarios, each of
    which needs ``check_vault_load`` to return a verdict.
    """
    requested: list[str] = []

    def _recording(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        requested.append(path)
        return _green(method, path, body)

    with serve(_recording) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS=expected, PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    assert not requested, f"the refusal must precede every network call: {requested}"
    assert proc.returncode == 2, (
        f"a control character must exit through the usage path:\n{proc.stdout}{proc.stderr}"
    )
    assert not _verdicts(proc.stdout) and not _verdicts(proc.stderr), proc.stdout
    assert "=== CAS cloud preflight" not in proc.stderr, "the checks must not have started"
    messages = [
        line
        for line in proc.stderr.splitlines()
        if line.startswith("preflight: PREFLIGHT_EXPECTED_VAULTS")
    ]
    assert len(messages) == 1, proc.stderr
    for entry in escaped_entries:
        assert entry in messages[0], messages[0]
    assert "cas," not in messages[0], f"only the offending entry is shown: {messages[0]!r}"


@_NEEDS_BASH
def test_usage_states_expected_vaults_refuses_control_characters() -> None:
    """The usage text states that a control character in the list is refused, so
    an operator reading ``--help`` learns the rule before a deploy meets it.
    """
    proc = _run({}, "--help")
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stderr.splitlines() if "control character" in line]
    assert any("PREFLIGHT_EXPECTED_VAULTS" in line for line in lines), proc.stderr


def _refusal_message(variable: str, bash_bin: str, **overrides: str) -> str:
    """Run the harness against a recording stub, assert the run was refused at
    required-input validation, and return the one ``preflight: <variable>`` line.

    Refused means: the usage exit code, no banner, no matrix row on either
    stream, and no request reaching the stub. The stub is served so that a run
    which got past validation would have something to call, which is what makes
    its silence evidence rather than an absence of opportunity.
    """
    requested: list[str] = []

    def _recording(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        requested.append(path)
        return _green(method, path, body)

    with serve(_recording) as url:
        proc = _run(_base_env(url, **overrides), bash_bin=bash_bin)
    assert not requested, f"the refusal must precede every network call: {requested}"
    assert proc.returncode == 2, f"expected the usage path:\n{proc.stdout}{proc.stderr}"
    assert not _verdicts(proc.stdout) and not _verdicts(proc.stderr), proc.stdout
    assert "=== CAS cloud preflight" not in proc.stderr, "the checks must not have started"
    messages = [
        line for line in proc.stderr.splitlines() if line.startswith(f"preflight: {variable} ")
    ]
    assert len(messages) == 1, proc.stderr
    return messages[0]


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    ("variable", "value", "escaped_entry"),
    [
        ("PREFLIGHT_VAULT_SOURCE", "document_store\n", "$'document_store\\n'"),
        ("PREFLIGHT_EXPECTED_ASUID", "zzz\nabc", "$'zzz\\nabc'"),
        ("PREFLIGHT_CHECKS", "vault_load\n", "$'vault_load\\n'"),
        ("PREFLIGHT_SKIP", "vault_load\r", "$'vault_load\\r'"),
        ("PREFLIGHT_EXPECTED_ASUID", "a,b\nc", "$'a,b\\nc'"),
        ("BASE_DOMAIN", "test.invalid\n", "$'test.invalid\\n'"),
        ("SAGE_FQDN", "sage.test.invalid\n", "$'sage.test.invalid\\n'"),
        ("CAS_FQDN", "cas.test\tinvalid", "$'cas.test\\tinvalid'"),
        ("SAGE_BASE_URL", "https://sage.test.invalid\n", "$'https://sage.test.invalid\\n'"),
        ("CAS_BASE_URL", "https://cas.test.invalid\r", "$'https://cas.test.invalid\\r'"),
        ("PREFLIGHT_RESOURCE_GROUP", "rg-cas\n", "$'rg-cas\\n'"),
        ("PREFLIGHT_EXPECTED_PG_MAJOR", "16\n", "$'16\\n'"),
        ("EXPECTED_SAGE_CNAME_SUFFIX", "azure-api.net\n", "$'azure-api.net\\n'"),
        ("EXPECTED_CAS_CNAME_SUFFIX", "azurecontainerapps.io\r", "$'azurecontainerapps.io\\r'"),
    ],
    ids=[
        "vault-source",
        "expected-asuid",
        "checks",
        "skip",
        "scalar-with-comma",
        "base-domain",
        "sage-fqdn",
        "cas-fqdn",
        "sage-base-url",
        "cas-base-url",
        "resource-group",
        "pg-major",
        "sage-cname-suffix",
        "cas-cname-suffix",
    ],
)
def test_a_control_character_in_any_operator_input_is_refused(
    variable: str, value: str, escaped_entry: str, bash_bin: str
) -> None:
    """A control character is refused in each tenant parameter parametrised
    here -- ids, lists, a backend name, a token, hosts and URLs -- not only in
    the expected-vaults list.

    Each variable fails differently when one is let through, which is why each
    is carried rather than one standing for the rest:

    * ``PREFLIGHT_VAULT_SOURCE`` is supplied by a repository variable and
      interpolated into a matrix detail, so a trailing newline splits a row;
    * ``PREFLIGHT_EXPECTED_ASUID`` is compared against resolver output read
      one record per line, so a multi-line value can never equal a record and
      would fail the check without saying why;
    * ``PREFLIGHT_CHECKS`` compared whole selects no check at all, and the run
      passes on an empty matrix;
    * ``PREFLIGHT_SKIP`` compared whole is silently not honoured;
    * the hosts and URLs build every probe and the banner, so a trailing newline
      splits the banner and fails each check with an opaque curl exit after the
      warm-up budget; ``PREFLIGHT_RESOURCE_GROUP`` and
      ``PREFLIGHT_EXPECTED_PG_MAJOR`` are rendered into detail rows, and the
      CNAME suffixes are compared against every resolved target.

    ``scalar-with-comma`` holds the other half of the message contract: a
    single-valued variable is shown whole, so a refusal that split it on commas
    as it does the lists would report the fragment ``$'b\\nc'`` as the entry.

    A refusal applied only to the expected-vaults list passes the sibling
    scenario and fails the vault-source and asuid cases. The two check-list
    cases are held by this refusal and by the unknown-id refusal alike, since an
    entry carrying a control character names no registered check either; which
    one answers changes the message's wording, not the escaped entry it shows.
    """
    message = _refusal_message(variable, bash_bin, **{variable: value})
    assert escaped_entry in message, message


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize("variable", ["PREFLIGHT_CHECKS", "PREFLIGHT_SKIP"])
@pytest.mark.parametrize(
    ("value", "unknown"),
    [("nosuch", "nosuch"), ("vault_load,nosuch", "nosuch"), ("vault_loa", "vault_loa")],
    ids=["alone", "mixed", "prefix"],
)
def test_a_check_list_naming_no_registered_check_is_refused(
    variable: str, value: str, unknown: str, bash_bin: str
) -> None:
    """An allowlist or denylist entry naming no registered check is refused.

    Unrefused, an allowlist of unknown ids selects nothing and the preflight
    exits 0 on an empty matrix -- a gate passing vacuously -- and a denylist
    entry that names nothing skips nothing while reading as a skip.

    What each shape excludes:

    * ``alone`` -- the vacuous pass itself;
    * ``mixed`` -- a guard that fails only when *zero* checks are selected, which
      the registered ``vault_load`` would satisfy while ``nosuch`` goes unnoticed;
    * ``prefix`` -- a lookup matching a substring of the registered ids rather
      than a whole id, which credits ``vault_loa`` through ``vault_load``;
    * ``vault_load`` absent from the message -- the refusal names the unknown
      entry, not the whole value.
    """
    message = _refusal_message(variable, bash_bin, **{variable: value})
    assert unknown in message, message
    assert "vault_load" not in message, f"only the unknown entry is named: {message!r}"


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    ("checks", "skip"),
    [("vault_load,,", ""), ("vault_load", ",")],
    ids=["allowlist", "denylist"],
)
def test_empty_elements_in_the_check_lists_are_not_unknown_ids(
    checks: str, skip: str, bash_bin: str
) -> None:
    """An empty element in either check list is not refused.

    The guard against the refusal over-reaching: an operator-edited list
    acquires stray commas, and an empty field is no id at all rather than an
    unknown one. A refusal that looked up every field would fail this run while
    passing every refusal scenario above.
    """
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=checks, PREFLIGHT_SKIP=skip),
            bash_bin=bash_bin,
        )
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"
    assert _verdicts(proc.stdout) == {"vault_load": "PASS"}, proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
@pytest.mark.parametrize(
    ("checks", "skip"),
    [(",", ""), ("vault_load", "vault_load")],
    ids=["empty-elements-only", "skip-covers-allowlist"],
)
def test_a_selection_leaving_no_check_to_run_is_refused(
    checks: str, skip: str, bash_bin: str
) -> None:
    """A selection that leaves no check to run is refused rather than passed.

    Neither shape names an unknown id, so the unknown-id refusal admits both,
    and both would otherwise reach an empty matrix and exit 0. ``,`` is an
    allowlist of empty elements -- set, so it filters, yet naming nothing;
    ``vault_load`` skipped from an allowlist of ``vault_load`` is a denylist
    covering the whole selection. The guard above holds the other boundary: an
    allowlist with stray commas that still names a check runs it.
    """
    message = _refusal_message(
        "PREFLIGHT_CHECKS", bash_bin, PREFLIGHT_CHECKS=checks, PREFLIGHT_SKIP=skip
    )
    assert "select no check" in message, message


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
def test_vault_load_credits_an_advertised_id_that_carries_a_metacharacter(bash_bin: str) -> None:
    """A metacharacter in an id is not itself disqualifying: an id equal to one
    the registry advertises is satisfied, whatever characters it holds.

    The guard against over-correcting the scenario above. Refusing any id
    carrying a metacharacter would fail every shape that one asserts, and pass
    every other scenario in this section, because no other fixture advertises an
    id that is not slug-shaped -- so the two rules are indistinguishable across
    the whole suite without this case. They part here, on the one input that
    separates them, and they part in the direction that matters: a rule keyed on
    the characters in an id reports a vault absent while it is serving.

    Unlike its siblings this scenario is not red against the pattern-matching
    lookup it replaces -- a regular expression also matches its own literal
    text. It is a guard rather than a detector, and the mutation it answers is
    a comparison narrowed to slug-shaped ids rather than one made literal.
    """
    with serve(_green_metacharacter_vault) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS="c.s", PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"an advertised id must be credited as written:\n{proc.stdout}"
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert _detail(proc.stdout, "vault_load") == _VAULT_LOAD_CLEAN_DETAIL, proc.stdout


@_NEEDS_RUNTIME
@pytest.mark.parametrize("bash_bin", _BASH_BIN_PARAMS)
def test_vault_load_does_not_expand_an_id_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bash_bin: str
) -> None:
    """An expected id is never replaced by a filename.

    The split reads the list through an *unquoted* expansion, which is how the
    comma split is obtained -- and pathname expansion applies to that same
    expansion. An id carrying a glob character is therefore replaced by whatever
    the deploy runner's working directory holds, so the ids the loop looks up
    are not the ids the operator wrote.

    That reverses the gate's direction of error. Here ``ca?`` is no vault, but a
    file named ``cas`` sits beside the run, so an expanding split hands the loop
    the advertised ``cas`` and reports every expected vault present -- a green
    vault_load standing on a filename. The failure needs no adversary: the
    variable is operator-edited, and CI runs the harness from a checkout full of
    short slug-shaped names.

    The file is what makes the scenario load-bearing. Without it ``ca?`` matches
    nothing on disk, stays literal, and fails for a reason that has nothing to
    do with expansion -- so the run must happen in a directory that contains the
    bait, which is what ``monkeypatch.chdir`` supplies.
    """
    (tmp_path / "cas").touch()
    monkeypatch.chdir(tmp_path)
    # Positive control on the bait. A bait that does not actually match reverts
    # this scenario to one an expanding split satisfies -- the pattern finds
    # nothing, stays literal, and the assertion below is met for the wrong
    # reason -- and nothing else here would say so.
    assert glob.glob("ca?") == ["cas"], "the bait is not a live glob match; the scenario is inert"
    with serve(_green) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_EXPECTED_VAULTS="ca?", PREFLIGHT_CHECKS="vault_load"),
            bash_bin=bash_bin,
        )
    verdicts = _verdicts(proc.stdout)
    detail = _detail(proc.stdout, "vault_load")
    assert proc.returncode != 0, (
        f"a filename beside the run must not satisfy an expected id:\n{proc.stdout}"
    )
    assert verdicts.get("vault_load") == "FAIL", verdicts
    # The id as written, not the filename it would have expanded to: a detail
    # naming `cas` would mean the expansion happened and the loop merely failed
    # to find it, which is a different script than the one this pins.
    assert detail.endswith("missing expected id(s): ca?"), detail
