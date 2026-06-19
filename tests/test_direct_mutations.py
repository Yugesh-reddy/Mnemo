"""Guarded direct mutations, durable retry receipts and scoped reads."""


def test_structured_error_serialization() -> None:
    from mnemo.errors import ErrorCode, MnemoError

    error = MnemoError(ErrorCode.NOT_FOUND, "Memory not found", context="read")
    assert error.to_dict() == {
        "code": "NOT_FOUND",
        "message": "Memory not found",
        "details": {"context": "read"},
    }
