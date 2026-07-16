"""Typed, transport-neutral errors for the isolated V3 control service."""

from __future__ import annotations

from typing import Any, Dict, Optional


class AdControlV3Error(Exception):
    """A safe error that may be returned by the HTTP integration layer."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "ad_control_v3_error")
        self.message = str(message or self.code)
        self.status = int(status or 400)
        self.details = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def fail(
    code: str,
    message: str,
    status: int = 400,
    **details: Any,
) -> "None":
    raise AdControlV3Error(code, message, status=status, details=details)
