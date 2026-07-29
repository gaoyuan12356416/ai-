"""Short-lived encrypted credential envelopes for the TT GPU sidecar.

Raw TikTok credentials are never accepted as ordinary JSON request fields.
Instead, the CPU process seals one access token for one account, one job, and
one operation using AES-256-GCM.  The GPU process decrypts it only around the
single upstream API call and never persists the plaintext.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import secrets
import time


ENVELOPE_VERSION = "v1"
ALLOWED_OPERATIONS = frozenset({"creator_info", "publish", "reconcile"})
JOB_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{11,127}\Z")
ACCOUNT_ID_RE = re.compile(r"\A[1-9][0-9]{0,30}\Z")
MAX_ENVELOPE_BYTES = 16 * 1024
DEFAULT_MAX_TTL_SECONDS = 300


class CredentialEnvelopeError(RuntimeError):
    """Stable error that never contains credential plaintext."""

    def __init__(self, code, message, status=400):
        self.code = str(code or "credential_envelope_invalid")
        self.status = int(status or 400)
        super().__init__(str(message or "credential envelope is invalid"))


def _b64url_encode(value):
    return base64.urlsafe_b64encode(bytes(value)).rstrip(b"=").decode("ascii")


def _b64url_decode(value):
    text = str(value or "")
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope encoding is invalid",
        )
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope encoding is invalid",
        ) from None


def decode_seal_key(value):
    """Decode a base64url/base64 32-byte AES key without echoing its value."""

    if isinstance(value, bytes):
        decoded = bytes(value)
    else:
        raw = str(value or "").strip()
        if not raw or len(raw) > 256 or re.search(r"\s", raw):
            raise CredentialEnvelopeError(
                "credential_seal_not_configured",
                "credential seal key is not configured",
                500,
            )
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (ValueError, TypeError):
            decoded = b""
    if len(decoded) != 32:
        raise CredentialEnvelopeError(
            "credential_seal_not_configured",
            "credential seal key must decode to 32 bytes",
            500,
        )
    return decoded


class _PyCryptoAESGCM:
    """Small adapter matching cryptography's ciphertext||tag AESGCM format."""

    def __init__(self, key):
        self.key = bytes(key)

    def encrypt(self, nonce, plaintext, associated_data):
        from Crypto.Cipher import AES

        cipher = AES.new(self.key, AES.MODE_GCM, nonce=bytes(nonce), mac_len=16)
        cipher.update(bytes(associated_data))
        ciphertext, tag = cipher.encrypt_and_digest(bytes(plaintext))
        return ciphertext + tag

    def decrypt(self, nonce, ciphertext_and_tag, associated_data):
        from Crypto.Cipher import AES

        value = bytes(ciphertext_and_tag)
        if len(value) < 17:
            raise ValueError("invalid AES-GCM payload")
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=bytes(nonce), mac_len=16)
        cipher.update(bytes(associated_data))
        return cipher.decrypt_and_verify(value[:-16], value[-16:])


def _aesgcm(key, force_fallback=False):
    if force_fallback:
        try:
            from Crypto.Cipher import AES  # noqa: F401
        except ImportError:
            raise CredentialEnvelopeError(
                "credential_crypto_unavailable",
                "AES-GCM support is unavailable",
                500,
            ) from None
        return _PyCryptoAESGCM(key)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        try:
            from Crypto.Cipher import AES  # noqa: F401
        except ImportError:
            raise CredentialEnvelopeError(
                "credential_crypto_unavailable",
                "AES-GCM support is unavailable",
                500,
            ) from None
        return _PyCryptoAESGCM(key)
    return AESGCM(bytes(key))


def _validate_binding(job_id, source_account_id, operation):
    job_id = str(job_id or "").strip()
    source_account_id = str(source_account_id or "").strip()
    operation = str(operation or "").strip()
    if not JOB_ID_RE.fullmatch(job_id):
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential job binding is invalid",
        )
    if not ACCOUNT_ID_RE.fullmatch(source_account_id):
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential account binding is invalid",
        )
    if operation not in ALLOWED_OPERATIONS:
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential operation binding is invalid",
        )
    return job_id, source_account_id, operation


def _aad(job_id, source_account_id, operation):
    return (
        "tt-post-gpu|%s|%s|%s|%s"
        % (ENVELOPE_VERSION, job_id, source_account_id, operation)
    ).encode("utf-8")


def seal_access_token(
    seal_key,
    access_token,
    *,
    job_id,
    source_account_id,
    operation,
    ttl_seconds=120,
    now=None,
):
    """Return an opaque envelope suitable for exactly one sidecar operation."""

    job_id, source_account_id, operation = _validate_binding(
        job_id,
        source_account_id,
        operation,
    )
    token = str(access_token or "")
    if (
        not token
        or len(token.encode("utf-8")) > 12 * 1024
        or "\x00" in token
        or "\r" in token
        or "\n" in token
    ):
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "access token is invalid",
        )
    try:
        ttl_seconds = int(ttl_seconds)
    except (TypeError, ValueError, OverflowError):
        ttl_seconds = 0
    if ttl_seconds < 1 or ttl_seconds > DEFAULT_MAX_TTL_SECONDS:
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope TTL is invalid",
        )
    issued_at = int(time.time() if now is None else now)
    payload = {
        "access_token": token,
        "exp": issued_at + ttl_seconds,
        "iat": issued_at,
        "jti": secrets.token_hex(16),
        "job_id": job_id,
        "operation": operation,
        "source_account_id": source_account_id,
    }
    plaintext = bytearray(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    nonce = secrets.token_bytes(12)
    try:
        ciphertext = _aesgcm(decode_seal_key(seal_key)).encrypt(
            nonce,
            bytes(plaintext),
            _aad(job_id, source_account_id, operation),
        )
        return "%s.%s.%s" % (
            ENVELOPE_VERSION,
            _b64url_encode(nonce),
            _b64url_encode(ciphertext),
        )
    finally:
        for index in range(len(plaintext)):
            plaintext[index] = 0
        payload["access_token"] = ""
        token = ""


@contextlib.contextmanager
def open_access_token(
    envelope,
    seal_key,
    *,
    job_id,
    source_account_id,
    operation,
    max_ttl_seconds=DEFAULT_MAX_TTL_SECONDS,
    now=None,
):
    """Yield a token briefly after authenticating and validating its binding."""

    job_id, source_account_id, operation = _validate_binding(
        job_id,
        source_account_id,
        operation,
    )
    envelope = str(envelope or "").strip()
    if not envelope or len(envelope.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope is invalid",
        )
    parts = envelope.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_VERSION:
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope version is invalid",
        )
    nonce = _b64url_decode(parts[1])
    ciphertext = _b64url_decode(parts[2])
    if len(nonce) != 12 or len(ciphertext) < 17:
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope is invalid",
        )
    try:
        plaintext_bytes = _aesgcm(decode_seal_key(seal_key)).decrypt(
            nonce,
            ciphertext,
            _aad(job_id, source_account_id, operation),
        )
    except CredentialEnvelopeError:
        raise
    except Exception:
        raise CredentialEnvelopeError(
            "credential_envelope_invalid",
            "credential envelope authentication failed",
        ) from None
    plaintext = bytearray(plaintext_bytes)
    token = ""
    payload = {}
    try:
        try:
            payload = json.loads(bytes(plaintext).decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise CredentialEnvelopeError(
                "credential_envelope_invalid",
                "credential envelope payload is invalid",
            ) from None
        if not isinstance(payload, dict):
            raise CredentialEnvelopeError(
                "credential_envelope_invalid",
                "credential envelope payload is invalid",
            )
        expected_fields = {
            "access_token",
            "exp",
            "iat",
            "jti",
            "job_id",
            "operation",
            "source_account_id",
        }
        if set(payload) != expected_fields:
            raise CredentialEnvelopeError(
                "credential_envelope_invalid",
                "credential envelope payload is invalid",
            )
        if (
            payload.get("job_id") != job_id
            or str(payload.get("source_account_id") or "") != source_account_id
            or payload.get("operation") != operation
        ):
            raise CredentialEnvelopeError(
                "credential_binding_mismatch",
                "credential envelope binding does not match the request",
                403,
            )
        try:
            issued_at = int(payload.get("iat"))
            expires_at = int(payload.get("exp"))
        except (TypeError, ValueError, OverflowError):
            raise CredentialEnvelopeError(
                "credential_envelope_invalid",
                "credential envelope lifetime is invalid",
            ) from None
        current = int(time.time() if now is None else now)
        try:
            max_ttl_seconds = int(max_ttl_seconds)
        except (TypeError, ValueError, OverflowError):
            max_ttl_seconds = DEFAULT_MAX_TTL_SECONDS
        if (
            issued_at > current + 30
            or expires_at <= current
            or expires_at <= issued_at
            or expires_at - issued_at > max_ttl_seconds
        ):
            raise CredentialEnvelopeError(
                "credential_envelope_expired",
                "credential envelope has expired or exceeds the allowed TTL",
                403,
            )
        token = str(payload.get("access_token") or "")
        if (
            not token
            or len(token.encode("utf-8")) > 12 * 1024
            or "\x00" in token
            or "\r" in token
            or "\n" in token
        ):
            raise CredentialEnvelopeError(
                "credential_envelope_invalid",
                "credential envelope payload is invalid",
            )
        yield token
    finally:
        token = ""
        if isinstance(payload, dict):
            payload["access_token"] = ""
        for index in range(len(plaintext)):
            plaintext[index] = 0
