"""Independent video editing metadata, direct uploads and GPU orchestration."""


class VideoEditingError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def require(condition, code, message, status=400):
    if not condition:
        raise VideoEditingError(code, message, status)
