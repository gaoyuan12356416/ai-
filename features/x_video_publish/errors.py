"""Safe errors shared by the HTTP boundary and read-only source adapter."""
class PublishingError(RuntimeError):
    def __init__(self, code, message, status=400, *, accepted=False):
        super().__init__(message)
        self.code = code
        self.status = self.status_code = status
        self.accepted = accepted
