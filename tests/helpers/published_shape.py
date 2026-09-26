"""Find the ``PublishedShape`` a typed alias carries."""

from __future__ import annotations

import typing

from sage.models.schemas import PublishedShape


def published_shape_of(annotation: object) -> PublishedShape | None:
    """The ``PublishedShape`` reachable from ``annotation``, if any.

    Walks nested ``Annotated`` and union arms, because an optional alias
    places its marker on the inner string arm so the null arm survives.
    """
    stack = [annotation]
    while stack:
        node = stack.pop()
        for meta in getattr(node, "__metadata__", ()):
            if isinstance(meta, PublishedShape):
                return meta
        stack.extend(typing.get_args(node))
    return None
