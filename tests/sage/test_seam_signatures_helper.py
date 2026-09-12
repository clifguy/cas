"""The signature-conformance rule the adapter-seam gates rest on.

Every seam gate asks one question per (binding, method): does the binding's
signature match the port's? Asked naively, the question has two ways to pass
for the wrong reason. A binding that inherits a defaulted port method yields
the port's own function, so a signature comparison compares a method to
itself. And a comparison that reads both sides from the port -- or skips
annotation evaluation where one module stringizes and the other does not --
agrees with anything. These tests pin the helper against synthetic ports and
bindings, one drift shape per test, so the seam gates' green means the
comparison actually ran against the binding.

This module deliberately does not stringize its annotations: the synthetic
port carries resolved types, and the one binding written with quoted
annotations is what makes annotation evaluation load-bearing.
"""

import functools
import inspect
from abc import ABC, abstractmethod

import pytest

from tests.helpers import seam_signatures
from tests.helpers.seam_signatures import (
    assert_signature_conforms,
    parametrized_values,
    port_surface,
    public_members,
)


class _Port(ABC):
    @abstractmethod
    def op(self, key: str, *, rule: frozenset[str]) -> dict[str, str]: ...

    def probe(self) -> bool:
        return True

    def locate(self, key: str) -> str | None:
        return None


class _Conforming(_Port):
    def op(self, key: str, *, rule: frozenset[str]) -> dict[str, str]:
        return {}


class _Unimplemented(_Port):
    pass


class _PositionalRule(_Port):
    def op(self, key: str, rule: frozenset[str]) -> dict[str, str]:  # type: ignore[override]
        return {}


class _DefaultedRule(_Port):
    def op(self, key: str, *, rule: frozenset[str] = frozenset()) -> dict[str, str]:
        return {}


class _DriftedReturn(_Port):
    def op(self, key: str, *, rule: frozenset[str]) -> dict[str, int]:  # type: ignore[override]
        return {}


class _Intermediate(_Port):
    def probe(self, x: int) -> bool:  # type: ignore[override]
        return bool(x)


class _InheritsDriftedProbe(_Intermediate):
    def op(self, key: str, *, rule: frozenset[str]) -> dict[str, str]:
        return {}


class _NarrowedReturn(_Port):
    def op(self, key: str, *, rule: frozenset[str]) -> dict[str, str]:
        return {}

    def locate(self, key: str) -> str:
        return key


class _NarrowedReturnRenamedParam(_Port):
    def op(self, key: str, *, rule: frozenset[str]) -> dict[str, str]:
        return {}

    def locate(self, name: str) -> str:  # type: ignore[override]
        return name


class _Hidden:
    """Stands in for a type a module imports only for type checkers."""


class _OtherHidden:
    pass


class _HiddenPort(ABC):
    @abstractmethod
    def fetch(self, owner: "_Hidden") -> None: ...


class _HiddenBinding(_HiddenPort):
    def fetch(self, owner: "_Hidden") -> None:
        return None


class _HiddenDriftBinding(_HiddenPort):
    def fetch(self, owner: "_OtherHidden") -> None:  # type: ignore[override]
        return None


def _forwarder(port_fn, body):
    """A ``functools.wraps`` delegate, shaped like a generated forwarding binding."""
    delegate = functools.wraps(port_fn)(body)
    delegate.__isabstractmethod__ = False
    return delegate


class _Forwarding(_Port):
    op = _forwarder(_Port.op, lambda self, *args, **kwargs: {})


class _NarrowForwarding(_Port):
    op = _forwarder(_Port.op, lambda self, key: {})


class _DescriptorPort(ABC):
    @abstractmethod
    def op(self) -> None: ...

    @classmethod
    def build(cls, key: str) -> str:
        return key

    @staticmethod
    def version() -> int:
        return 1

    @property
    def name(self) -> str:
        return ""


class _DescriptorConforming(_DescriptorPort):
    def op(self) -> None:
        return None

    @classmethod
    def build(cls, key: str) -> str:
        return key

    @staticmethod
    def version() -> int:
        return 2

    @property
    def name(self) -> str:
        return "conforming"


class _DescriptorDrift(_DescriptorPort):
    def op(self) -> None:
        return None

    @classmethod
    def build(cls, key: int) -> str:  # type: ignore[override]
        return str(key)

    @staticmethod
    def version(major: int) -> int:  # type: ignore[override]
        return major

    @property
    def name(self) -> int:  # type: ignore[override]
        return 0


class _DescriptorKindMismatch(_DescriptorPort):
    def op(self) -> None:
        return None

    def build(self, key: str) -> str:  # type: ignore[override]
        return key


class _Stringized(_Port):
    def op(self, key: "str", *, rule: "frozenset[str]") -> "dict[str, str]":
        return {}


# --------------------------------------------------------------------------- #
# Comparison against the binding (H1-H4)
# --------------------------------------------------------------------------- #


def test_h1_conforming_binding_passes():
    """H1: an override with the port's exact signature conforms."""
    assert_signature_conforms(_Port, _Conforming, "op")


@pytest.mark.parametrize(
    "binding",
    [_PositionalRule, _DefaultedRule, _DriftedReturn],
    ids=["positional-parameter", "added-default", "return-annotation"],
)
def test_h2_h4_each_drift_shape_fails_naming_binding_and_method(binding):
    """H2-H4: a keyword-only parameter made positional, a default the port does
    not carry, and a drifted return annotation each fail.

    Trap: a comparison that read both sides from the port -- the shape of a
    gate that silently compares a method to itself -- passes all three.
    """
    with pytest.raises(AssertionError) as excinfo:
        assert_signature_conforms(_Port, binding, "op")
    assert f"{binding.__name__}.op" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Inherited defaults (H5-H6)
# --------------------------------------------------------------------------- #


def test_h5_inherited_default_conforms_by_identity_without_comparing(monkeypatch):
    """H5: a binding that inherits a defaulted port method conforms because it
    holds the port's own function, and the helper says so rather than
    comparing the function to itself.

    Trap: a helper that fell through to a signature comparison would still
    pass, vacuously; refusing ``inspect.signature`` is what distinguishes the
    identity claim from the self-comparison.
    """
    assert _Conforming.probe is _Port.probe  # precondition: genuinely inherited

    def refuse(*args, **kwargs):
        raise AssertionError("inherited method was compared, not identity-checked")

    monkeypatch.setattr(seam_signatures.inspect, "signature", refuse)
    assert_signature_conforms(_Port, _Conforming, "probe")


def test_h5b_unimplemented_abstract_method_fails_rather_than_passing_by_identity():
    """H5b: a binding that never implemented an abstract method also resolves to
    the port's own function, and must fail rather than conform by identity.

    Trap: an identity rule that did not exclude abstract methods would report
    every unimplemented port method as conforming -- the gate would be green on
    exactly the binding that satisfies the least of the port.
    """
    assert _Unimplemented.op is _Port.op  # precondition: same object as H5's case
    with pytest.raises(AssertionError, match="_Unimplemented.op"):
        assert_signature_conforms(_Port, _Unimplemented, "op")


def test_h6_override_arriving_through_an_intermediate_base_is_compared():
    """H6: a method the binding does not declare itself, but resolves to a
    different function through an intermediate base, is compared and fails.

    Trap: an inheritance rule keyed on ``name not in vars(binding)`` would wave
    this through as inherited; only resolved-object identity catches it.
    """
    assert "probe" not in vars(_InheritsDriftedProbe)  # precondition
    with pytest.raises(AssertionError):
        assert_signature_conforms(_Port, _InheritsDriftedProbe, "probe")


# --------------------------------------------------------------------------- #
# Pinned divergences and sanctioned narrowing (H7-H9)
# --------------------------------------------------------------------------- #


def test_h7_pinned_divergence_passes():
    """H7: a drift pinned in the divergence set is tolerated."""
    assert_signature_conforms(
        _Port, _PositionalRule, "op", divergences=frozenset({(_PositionalRule, "op")})
    )


def test_h8_stale_pin_fails():
    """H8: a pin whose binding no longer diverges fails, so the set can only
    shrink.

    Trap: a pin that merely skipped the comparison would let a fixed drift keep
    its exemption indefinitely, and a later re-drift would pass unseen.
    """
    with pytest.raises(AssertionError, match="stale"):
        assert_signature_conforms(
            _Port, _Conforming, "op", divergences=frozenset({(_Conforming, "op")})
        )


def test_h8b_stale_pin_on_an_inherited_method_fails():
    """H8b: a pin on a method the binding inherits unchanged fails as stale.

    An inherited method is the port's own function and cannot diverge from it.
    Trap: an identity check that returned before consulting the pin set would
    let such a pin sit in the set forever.
    """
    assert _Conforming.probe is _Port.probe  # precondition: inherited
    with pytest.raises(AssertionError, match="stale"):
        assert_signature_conforms(
            _Port, _Conforming, "probe", divergences=frozenset({(_Conforming, "probe")})
        )


def test_h9_narrowed_return_exempts_the_return_annotation_only():
    """H9: a sanctioned return narrowing exempts the return annotation and
    nothing else.

    Trap: an exemption that skipped the whole comparison would pass the second
    arm, where the parameters drift too.
    """
    with pytest.raises(AssertionError):
        assert_signature_conforms(_Port, _NarrowedReturn, "locate")
    assert_signature_conforms(
        _Port, _NarrowedReturn, "locate", return_narrowed=frozenset({(_NarrowedReturn, "locate")})
    )

    with pytest.raises(AssertionError):
        assert_signature_conforms(
            _Port,
            _NarrowedReturnRenamedParam,
            "locate",
            return_narrowed=frozenset({(_NarrowedReturnRenamedParam, "locate")}),
        )


# --------------------------------------------------------------------------- #
# Annotation evaluation (H10)
# --------------------------------------------------------------------------- #


def test_h10_stringized_annotations_are_resolved_before_comparing():
    """H10: a binding whose annotations are strings conforms when they resolve
    to the port's types.

    The graph and content Postgres bindings stringize their annotations while
    the port module does not, so comparing spellings would fail every method on
    them. The precondition proves the unevaluated signatures really differ.
    """
    assert inspect.signature(_Stringized.op) != inspect.signature(_Port.op)
    assert_signature_conforms(_Port, _Stringized, "op")


def test_h13_names_a_module_imports_only_for_type_checkers_are_supplied_explicitly():
    """H13: an annotation naming a type its module never binds at run time
    resolves through ``annotation_names``, and is still compared once resolved.

    The failure without the mapping is a resolution error, not a pass: a
    helper that swallowed it, or fell back to comparing spellings, would report
    two different types spelled alike as conforming. The drift arm resolves
    both names and must still fail.
    """
    names = {"_Hidden": _Hidden, "_OtherHidden": _OtherHidden}
    real_globals = dict(globals())
    for hidden in names:
        del globals()[hidden]
    try:
        with pytest.raises(NameError):
            assert_signature_conforms(_HiddenPort, _HiddenBinding, "fetch")
        assert_signature_conforms(_HiddenPort, _HiddenBinding, "fetch", annotation_names=names)
        with pytest.raises(AssertionError, match="_HiddenDriftBinding.fetch"):
            assert_signature_conforms(
                _HiddenPort, _HiddenDriftBinding, "fetch", annotation_names=names
            )
    finally:
        globals().update(real_globals)


# --------------------------------------------------------------------------- #
# Forwarding delegates (H14)
# --------------------------------------------------------------------------- #


def test_h14_forwarder_to_the_port_is_declared_and_its_own_shape_checked():
    """H14: a ``functools.wraps`` delegate of the port's own method conforms only
    when declared a forwarder, and only while it genuinely forwards.

    ``inspect.signature`` follows ``__wrapped__`` by default, so comparing such
    a delegate reads the port's signature on both sides -- the self-comparison
    the identity rule exists to prevent, arriving through a second door.

    Trap, per arm: an undeclared forwarder would pass by self-comparison; a
    declared "forwarder" that narrows its own parameters would pass on the
    followed signature while rejecting calls the port admits; and a forwarder
    pin on a binding that implements the method outright would never expire.
    """
    assert inspect.unwrap(_Forwarding.op) is _Port.op  # precondition: it forwards
    with pytest.raises(AssertionError, match="_Forwarding.op: .*forwarder"):
        assert_signature_conforms(_Port, _Forwarding, "op")

    assert_signature_conforms(_Port, _Forwarding, "op", forwarders=frozenset({(_Forwarding, "op")}))

    with pytest.raises(AssertionError, match="_NarrowForwarding.op"):
        assert_signature_conforms(
            _Port, _NarrowForwarding, "op", forwarders=frozenset({(_NarrowForwarding, "op")})
        )

    with pytest.raises(AssertionError, match="stale"):
        assert_signature_conforms(
            _Port, _Conforming, "op", forwarders=frozenset({(_Conforming, "op")})
        )


# --------------------------------------------------------------------------- #
# Descriptor members (H15)
# --------------------------------------------------------------------------- #


def test_h15_classmethod_staticmethod_and_property_members_are_port_surface():
    """H15: a port member defined as a classmethod, staticmethod, or property is
    port surface, is compared through its underlying function, and must keep its
    kind on the binding.

    Trap: a surface read filtered on plain functions sees none of these, so a
    drifting override is never compared and the binding-side surface bounds
    cannot see the member either.
    """
    members = {"op", "build", "version", "name"}
    assert port_surface(_DescriptorPort) == frozenset(members)
    assert public_members(_DescriptorConforming) == frozenset(members)

    for member in sorted(members):
        assert_signature_conforms(_DescriptorPort, _DescriptorConforming, member)
    for member in ("build", "version", "name"):
        with pytest.raises(AssertionError, match=f"_DescriptorDrift.{member}"):
            assert_signature_conforms(_DescriptorPort, _DescriptorDrift, member)
    with pytest.raises(AssertionError, match="_DescriptorKindMismatch.build: .*kind"):
        assert_signature_conforms(_DescriptorPort, _DescriptorKindMismatch, "build")


# --------------------------------------------------------------------------- #
# Surface and wiring helpers (H11-H12)
# --------------------------------------------------------------------------- #


def test_h11_port_surface_is_abstract_plus_defaulted_methods():
    """H11: the port surface carries both abstract and defaulted methods.

    Trap: a surface read from ``__abstractmethods__`` alone would drop
    ``probe`` and ``locate``, and every binding override of a defaulted method
    would go unchecked.
    """
    assert port_surface(_Port) == frozenset({"op", "probe", "locate"})


@pytest.mark.parametrize("sample", ["b", "a"])
def test_h12_parametrized_values_reads_the_collected_parametrization(sample):
    """H12: the wiring reader returns exactly the values a test is parametrized
    over, in declaration order, so a seam gate can assert its own coverage."""
    assert parametrized_values(
        test_h12_parametrized_values_reads_the_collected_parametrization, "sample"
    ) == ["b", "a"]
    with pytest.raises(LookupError):
        parametrized_values(
            test_h12_parametrized_values_reads_the_collected_parametrization, "missing"
        )
