"""Signature conformance between a port ABC and each of its bindings.

The adapter-seam gates ask, for every (binding, method) pair, whether the
binding's signature is the port's. Four rules keep that question from being
answered for the wrong reason, and they live here once so the seam modules
cannot paraphrase them apart:

* **Inherited unchanged is identity, not comparison.** A binding that does not
  override a defaulted port method resolves to the port's own function, and
  comparing its signature to the port's compares a method to itself. The
  helper asserts the identity instead, which still catches a method arriving
  through an intermediate base with a different shape. Identity is accepted
  only for defaulted methods: an abstract one the binding never implemented
  resolves the same way and fails.
* **A forwarder is declared, not compared.** A ``functools.wraps`` delegate of
  the port's own method reports the port's signature, because
  ``inspect.signature`` follows ``__wrapped__``. Such a pair conforms only when
  the gate declares it a forwarder, and only while the delegate's own
  parameters accept everything, so it cannot narrow what the port admits.
* **A pin must still hold.** A divergence pin whose drift was fixed, or that
  names a method the binding inherits, fails; so does a forwarder pin on a
  method the binding implements outright. Pin sets can only shrink.
* **Annotations are resolved before comparing.** Binding modules differ in
  whether they stringize annotations, so spellings are not comparable; types
  are. A name a module imports only for type checkers has no run-time binding
  to resolve against, so the gate supplies it explicitly; resolution never
  falls back to spellings.
"""

import inspect
from collections.abc import Callable, Collection, Mapping
from typing import Any

_DESCRIPTOR_KINDS = (classmethod, staticmethod, property)


def public_members(cls: type) -> frozenset[str]:
    """Public callable members defined directly on ``cls`` (not inherited).

    Plain functions, and members defined as a classmethod, staticmethod, or
    property -- each of which ``vars`` yields as a descriptor rather than a
    function, so a filter on functions alone would not see it.
    """
    return frozenset(
        name
        for name, value in vars(cls).items()
        if not name.startswith("_")
        and (inspect.isfunction(value) or isinstance(value, _DESCRIPTOR_KINDS))
    )


def port_surface(port: type) -> frozenset[str]:
    """The port's abstract members plus the public members it defines with a default.

    A defaulted port member is port surface even though it is not abstract: a
    binding may override it, and that override must carry the port's shape.
    """
    return frozenset(port.__abstractmethods__) | public_members(port)


def _resolve(cls: type, name: str) -> tuple[type | None, Callable[..., Any]]:
    """The member's descriptor kind (``None`` for a plain function) and its function."""
    static = inspect.getattr_static(cls, name)
    if isinstance(static, (classmethod, staticmethod)):
        return type(static), static.__func__
    if isinstance(static, property):
        return property, static.fget
    return None, static


def _accepts_everything(fn: Callable[..., Any]) -> bool:
    """True when ``fn``'s own parameters, past the first, are ``*args, **kwargs``."""
    parameters = list(inspect.signature(fn, follow_wrapped=False).parameters.values())[1:]
    return [p.kind for p in parameters] == [
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ]


def assert_signature_conforms(
    port: type,
    binding: type,
    method: str,
    *,
    return_narrowed: Collection[tuple[type, str]] = frozenset(),
    divergences: Collection[tuple[type, str]] = frozenset(),
    forwarders: Collection[tuple[type, str]] = frozenset(),
    annotation_names: Mapping[str, Any] | None = None,
) -> None:
    """Assert ``binding.method`` carries ``port.method``'s signature.

    ``return_narrowed`` names pairs whose return annotation may legitimately
    narrow the port's; their parameters are still compared. ``divergences``
    names pairs whose signatures are known to differ; each must still differ.
    ``forwarders`` names pairs whose binding member is a ``functools.wraps``
    delegate of the port's own function. ``annotation_names`` supplies types
    that annotations name but their modules import only for type checkers.
    """
    port_kind, port_fn = _resolve(port, method)
    binding_kind, binding_fn = _resolve(binding, method)
    label = f"{binding.__name__}.{method}"
    pinned = (binding, method) in divergences
    forwarded = (binding, method) in forwarders

    if binding_fn is port_fn:
        assert not pinned and not forwarded, (
            f"{label}: stale pin -- the binding inherits the port's own member"
        )
        assert method not in port.__abstractmethods__, (
            f"{label}: abstract port method is not implemented on the binding"
        )
        return

    assert binding_kind is port_kind, (
        f"{label}: member kind {binding_kind} != port kind {port_kind}"
    )

    if inspect.unwrap(binding_fn) is port_fn:
        assert not pinned, f"{label}: stale divergence pin -- a forwarder cannot diverge"
        assert forwarded, (
            f"{label}: wraps the port's own function, so its signature reads as the "
            f"port's; declare it a forwarder"
        )
        assert _accepts_everything(binding_fn), (
            f"{label}: declared forwarder narrows its own parameters to "
            f"{inspect.signature(binding_fn, follow_wrapped=False)}"
        )
        return
    assert not forwarded, f"{label}: stale forwarder pin -- the binding does not forward"

    port_sig = inspect.signature(port_fn, eval_str=True, locals=annotation_names)
    binding_sig = inspect.signature(binding_fn, eval_str=True, locals=annotation_names)

    if (binding, method) in divergences:
        assert binding_sig != port_sig, (
            f"{label}: stale divergence pin -- the signature now matches the port "
            f"{port_sig}; remove the pin"
        )
        return

    if (binding, method) in return_narrowed:
        assert binding_sig.parameters == port_sig.parameters, (
            f"{label}: parameters {binding_sig} != port {port_sig}"
        )
        return

    assert binding_sig == port_sig, f"{label}: {binding_sig} != port {port_sig}"


def parametrized_values(test_fn: Callable[..., Any], argname: str) -> list[Any]:
    """The values ``test_fn`` is parametrized over for ``argname``.

    Lets a seam gate assert which bindings and methods it actually covers, so a
    parametrization that silently drops a binding fails rather than shrinking
    the gate.
    """
    for mark in getattr(test_fn, "pytestmark", []):
        if mark.name == "parametrize" and mark.args[0] == argname:
            return list(mark.args[1])
    raise LookupError(f"{test_fn.__name__} is not parametrized over {argname!r}")
