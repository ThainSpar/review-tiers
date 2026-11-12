"""Code hosts. Each maps one host's API onto the same small set of operations."""

from __future__ import annotations

from dataclasses import dataclass


class NotSupported(RuntimeError):
    """The host class does not implement this operation in this version."""


@dataclass(frozen=True)
class Comment:
    id: int
    body: str
    author: str
    author_is_bot: bool = False
