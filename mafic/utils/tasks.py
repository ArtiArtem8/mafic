"""Internal helpers for owning asynchronous cleanup tasks."""

# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from asyncio import Task

T = TypeVar("T")

_log = getLogger(__name__)


async def await_owned(task: Task[T]) -> T:
    """Wait for an owned task without letting caller cancellation cancel it.

    Repeated cancellation is remembered and re-raised only after the task has
    settled. A failure observed after the caller was cancelled is logged because
    the caller will receive :class:`asyncio.CancelledError` instead.
    """
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancelled = exc
            continue
        except Exception:
            if cancelled is None:
                raise
            _log.exception("Owned cleanup failed after caller cancellation.")
            raise cancelled from None
        if cancelled is not None:
            raise cancelled
        return result


__all__ = ("await_owned",)
