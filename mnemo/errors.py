"""Structured errors for the additive direct-memory API."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    INVALID_RESTORE_TARGET = "INVALID_RESTORE_TARGET"
    UNSUPPORTED_STATE = "UNSUPPORTED_STATE"
    REQUEST_ID_REUSED = "REQUEST_ID_REUSED"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"


class MnemoError(Exception):
    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "details": self.details}
