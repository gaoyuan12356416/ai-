"""Small shared primitives for the independent analysis and planning module."""
import json


class SmartError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def require(ok, code, message, status=400):
    if not ok:
        raise SmartError(code, message, status)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
