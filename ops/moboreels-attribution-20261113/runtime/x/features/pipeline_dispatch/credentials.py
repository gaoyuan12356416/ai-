"""Short-lived, node/claim/operation bound credentials; never persist plaintext."""
from __future__ import annotations

import contextlib
import json
import secrets
import time

from features.tt_gpu.credentials import (
    CredentialEnvelopeError, _aesgcm, _b64url_decode, _b64url_encode, decode_seal_key,
)

VERSION = "pipeline-v1"
BINDING_FIELDS = frozenset({"job_id", "generation", "node_id", "account_resource", "operation_id", "request_sha256"})


def _binding(value):
    if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
        raise CredentialEnvelopeError("credential_binding_invalid", "operation binding is invalid")
    result = {key: str(value[key]) for key in sorted(BINDING_FIELDS)}
    if any(not item or len(item) > 512 or any(ord(c) < 32 for c in item) for item in result.values()):
        raise CredentialEnvelopeError("credential_binding_invalid", "operation binding is invalid")
    return result


def _aad(binding):
    return (VERSION + "|" + json.dumps(_binding(binding), sort_keys=True, separators=(",", ":"))).encode()


def seal_credentials(seal_key, credentials, binding, *, ttl_seconds=120, now=None):
    """Called by the authority after durable operation intent has been accepted."""
    if not isinstance(credentials, dict) or not credentials or set(credentials) - {"access_token", "credential_id", "fb_user_id", "page_id"}:
        raise CredentialEnvelopeError("credential_payload_invalid", "unsupported credential fields")
    token = str(credentials.get("access_token") or "")
    if not token or len(token) > 12288 or any(c in token for c in "\r\n\0"):
        raise CredentialEnvelopeError("credential_payload_invalid", "access token is invalid")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
        raise CredentialEnvelopeError("credential_lifetime_invalid", "credential lifetime must not exceed 120 seconds")
    issued = int(time.time() if now is None else now)
    payload = {"binding": _binding(binding), "credentials": dict(credentials), "iat": issued, "exp": issued + ttl_seconds}
    plaintext = bytearray(json.dumps(payload, separators=(",", ":")).encode())
    try:
        nonce = secrets.token_bytes(12)
        ciphertext = _aesgcm(decode_seal_key(seal_key)).encrypt(nonce, bytes(plaintext), _aad(binding))
        return ".".join((VERSION, _b64url_encode(nonce), _b64url_encode(ciphertext)))
    finally:
        plaintext[:] = b"\0" * len(plaintext)
        payload["credentials"].clear()


@contextlib.contextmanager
def open_credentials(envelope, seal_key, binding, *, now=None):
    """TTL gates opening the envelope, not the lifetime of an in-flight request."""
    parts = str(envelope or "").split(".")
    if len(parts) != 3 or parts[0] != VERSION or len(str(envelope)) > 24576:
        raise CredentialEnvelopeError("credential_envelope_invalid", "credential envelope is invalid")
    payload = {}
    plaintext = bytearray()
    def clear():
        plaintext[:] = b"\0" * len(plaintext)
        if isinstance(payload, dict) and isinstance(payload.get("credentials"), dict):
            payload["credentials"].clear()
    try:
        decoded = _aesgcm(decode_seal_key(seal_key)).decrypt(_b64url_decode(parts[1]), _b64url_decode(parts[2]), _aad(binding))
        plaintext = bytearray(decoded)
        payload = json.loads(plaintext.decode())
        current = int(time.time() if now is None else now)
        if payload.get("binding") != _binding(binding):
            raise ValueError("binding")
        issued, expires = int(payload["iat"]), int(payload["exp"])
        if not 0 < expires - issued <= 120 or issued > current + 5 or current >= expires:
            raise CredentialEnvelopeError("credential_envelope_expired", "credential envelope has expired")
        credentials = payload.get("credentials")
        if not isinstance(credentials, dict) or not credentials.get("access_token"):
            raise ValueError("payload")
    except CredentialEnvelopeError:
        clear()
        raise
    except (ValueError, TypeError, KeyError):
        clear()
        raise CredentialEnvelopeError("credential_envelope_invalid", "credential authentication failed") from None
    except Exception:
        clear()
        raise CredentialEnvelopeError("credential_envelope_invalid", "credential authentication failed") from None
    try:
        yield credentials
    finally:
        clear()

