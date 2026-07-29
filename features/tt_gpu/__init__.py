"""GPU preparation and TikTok Content Posting sidecar.

The package is deliberately independent from the AI backend HTTP process.  The
CPU-side publisher talks to it only through the loopback/reverse-tunnel
transport exposed by :mod:`features.tt_gpu.worker`.
"""

from .credentials import (
    CredentialEnvelopeError,
    open_access_token,
    seal_access_token,
)
from .worker import (
    TTGPUError,
    TTPostGPUProcessor,
    WorkerConfig,
    serve,
)

__all__ = [
    "CredentialEnvelopeError",
    "TTGPUError",
    "TTPostGPUProcessor",
    "WorkerConfig",
    "open_access_token",
    "seal_access_token",
    "serve",
]
