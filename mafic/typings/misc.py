# SPDX-License-Identifier: MIT

from __future__ import annotations

from typing import Any, Coroutine, Dict, List, Literal, TypedDict, TypeVar, Union

__all__ = (
    "Coro",
    "LavalinkException",
    "ExceptionSeverity",
    "JSONObject",
    "JSONValue",
    "PayloadWithGuild",
)
T = TypeVar("T")

Coro = Coroutine[Any, Any, T]
JSONValue = Union[
    None, bool, int, float, str, List["JSONValue"], Dict[str, "JSONValue"]
]
JSONObject = Dict[str, JSONValue]
ExceptionSeverity = Literal[
    # V3
    "COMMON",
    "SUSPICIOUS",
    "FAULT",
    # V4
    "common",
    "suspicious",
    "fault",
]


class LavalinkException(TypedDict):
    severity: ExceptionSeverity
    message: str
    cause: str


class PayloadWithGuild(TypedDict):
    guildId: str
