"""Encryption of sensitive fields at rest (todo: encrypt bank details, KYC documents, phones).

``FIELD_ENCRYPTION_KEY`` = 32 random bytes, base64 (``python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"``).
``encrypt(text)`` → ``enc:v1:<base64(nonce + AES-256-GCM ciphertext)>``; ``decrypt`` reverses
it and passes plain (legacy, unencrypted) values through, so a database written before the key
was set still reads. Without a key ``encrypt`` returns the text unchanged (readiness warns).

Used for the supplier bank details kept on a hand-added supplier (bank name, IFSC, last 4
digits — the full account number is never stored). Phone numbers stay in clear: duplicate
detection and the do-not-contact match need them, and they are masked in every log.
"""
from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)
PREFIX = "enc:v1:"


def _key() -> bytes | None:
    raw = os.getenv("FIELD_ENCRYPTION_KEY", "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        logger.error("FieldCrypto: FIELD_ENCRYPTION_KEY is not base64 — fields stay unencrypted")
        return None
    if len(key) != 32:
        logger.error("FieldCrypto: FIELD_ENCRYPTION_KEY must be 32 bytes — fields stay unencrypted")
        return None
    return key


def configured() -> bool:
    return _key() is not None


def encrypt(text: str | None) -> str | None:
    if text is None or text == "" or str(text).startswith(PREFIX):
        return text
    key = _key()
    if key is None:
        return text
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, str(text).encode("utf-8"), b"portless-field")
    return PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(value: str | None) -> str | None:
    if not value or not str(value).startswith(PREFIX):
        return value
    key = _key()
    if key is None:
        logger.error("FieldCrypto: an encrypted field cannot be read — FIELD_ENCRYPTION_KEY is not set")
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        blob = base64.b64decode(str(value)[len(PREFIX):])
        return AESGCM(key).decrypt(blob[:12], blob[12:], b"portless-field").decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - a wrong key never crashes a page
        logger.error("FieldCrypto: field not decrypted (wrong key?): %s", type(exc).__name__)
        return None
