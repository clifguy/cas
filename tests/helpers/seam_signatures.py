"""Signature conformance between a port ABC and each of its bindings.

The adapter-seam gates ask, for every (binding, method) pair, whether the
binding's signature is the port's. Three rules keep that question from being
answered for the wrong reason, and they live here once so the seam modules
cannot paraphrase them apart:

* **Inherited unchanged is identity, not comparison.** A binding that does not
  override a defaulted port method resolves to the port's own function, and
  comparing its signature to the port's compares a method to itself. The
  helper asserts the identity instead, which still catches a method arriving
  through an intermediate base with a different shape. Identity is accepted
  only for defaulted methods: an abstract one the binding never implemented
  resolves the same way and fails.
* **A pinned divergence must still diverge.** A pin whose drift was fixed
  fails, so a divergence set can only shrink.
* **Annotations are resolved before comparing.** Binding modules differ in
  whether they stringize annotations, so spellings are not comparable; types
  are. A name a module imports only for type checkers has no run-time binding
  to resolve against, so the gate supplies it explicitly; resolution never
  falls back to spellings.
"""

import inspect
from collections.abc import Callable, Collection, Mapping
from typing import Any


def port_surface(port: type) -> frozenset[str]:
    """The port's abstract methods plus the public methods it defines with a default.

    A defaulted port method is port surface even though it is not abstract: a
    binding may override it, and that override must carry the port's shape.
    """
    defaulted = {
        name
        for name, value in vars(port).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    return frozenset(port.__abstractmethods__) | frozenset(defaulted)


def assert_signature_conforms(
    port: type,
    binding: type,
    method: str,
    *,
    return_narrowed: Collection[tuple[type, str]] = frozenset(),
    divergences: Collection[tuple[type, str]] = frozenset(),
    annotation_names: Mapping[str, Any] | None = None,
) -> None:
    """Assert ``binding.method`` carries ``port.method``'s signature.

    ``return_narrowed`` names pairs whose return annotation may legitimately
    narrow the port's; their parameters are still compared. ``divergences``
    names pairs whose signatures are known to differ; each must still differ.
    ``annotation_names`` supplies types that annotations name but their modules
    import only for type checkers.
    """
    port_fn = getattr(port, method)
    binding_fn = getattr(binding, method)
    label = f"{binding.__name__}.{method}"

    if binding_fn is port_fn:
        assert method not in port.__abstractmethods__, (
            f"{label}: abstract port method is not implemented on the binding"
        )
        return

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
