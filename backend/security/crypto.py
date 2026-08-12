"""Encryption for credentials at rest.

OAuth refresh tokens are long-lived keys to the user's entire mailbox. They sit
in the same SQLite file as the messages, so a stolen `gary.db` would otherwise
hand over not just the mail already synced but continuing access to everything
that arrives after.

AES-256-GCM via `cryptography`. Authenticated encryption matters here rather
than being a nicety: without it, a tampered ciphertext decrypts to garbage that
gets used as a token, and the failure surfaces as a confusing API error instead
of "this file was modified".

The key lives in `CREDENTIAL_ENCRYPTION_KEY` in `.env` — deliberately *not* in
the database, so the database alone is not sufficient to decrypt.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-GCM nonce. 96 bits is the standard size and the one the mode is
#: designed around; other lengths force an internal rehash and are slower.
NONCE_BYTES = 12
KEY_BYTES = 32

#: Prefix so an encrypted blob is self-identifying, and a future scheme change
#: can be detected rather than mis-decrypted.
VERSION_PREFIX = b"g1:"


class EncryptionError(RuntimeError):
    """Encryption or decryption failed."""


class MissingKeyError(EncryptionError):
    def __init__(self) -> None:
        super().__init__(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "import secrets,base64;'
            'print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"\n'
            "then add it to .env. Without it, OAuth tokens cannot be stored."
        )


def generate_key() -> str:
    """A fresh urlsafe-base64 key, ready to paste into `.env`."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def load_key(encoded: Optional[str]) -> bytes:
    if not encoded or not encoded.strip():
        raise MissingKeyError()
    try:
        raw = base64.urlsafe_b64decode(encoded.strip())
    except Exception as exc:  # noqa: BLE001
        raise EncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not valid urlsafe base64."
        ) from exc
    if len(raw) != KEY_BYTES:
        raise EncryptionError(
            f"CREDENTIAL_ENCRYPTION_KEY must decode to {KEY_BYTES} bytes, got {len(raw)}."
        )
    return raw


@dataclass
class Cipher:
    """Encrypts and decrypts credential blobs."""

    key: bytes

    @classmethod
    def from_settings(cls, encoded_key: Optional[str]) -> "Cipher":
        return cls(load_key(encoded_key))

    def encrypt(self, plaintext: str, *, aad: str = "") -> bytes:
        """Encrypt, binding the ciphertext to `aad`.

        `aad` (additional authenticated data) is not encrypted but is
        authenticated: passing the account address means a token blob copied
        from one account's row to another fails to decrypt rather than
        silently authorising the wrong mailbox.
        """
        if plaintext is None:
            raise EncryptionError("Cannot encrypt None.")
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self.key).encrypt(
            nonce, plaintext.encode("utf-8"), aad.encode("utf-8")
        )
        return VERSION_PREFIX + nonce + ciphertext

    def decrypt(self, blob: bytes, *, aad: str = "") -> str:
        if not blob:
            raise EncryptionError("Cannot decrypt an empty value.")
        if not blob.startswith(VERSION_PREFIX):
            raise EncryptionError(
                "Credential blob has an unrecognised format — it was written by a "
                "different version, or the column is corrupt."
            )
        body = blob[len(VERSION_PREFIX) :]
        if len(body) <= NONCE_BYTES:
            raise EncryptionError("Credential blob is truncated.")

        nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
        try:
            plaintext = AESGCM(self.key).decrypt(nonce, ciphertext, aad.encode("utf-8"))
        except InvalidTag as exc:
            raise EncryptionError(
                "Could not decrypt the stored credential. Either "
                "CREDENTIAL_ENCRYPTION_KEY changed, or the value was tampered with. "
                "Reconnect the account to store a fresh token."
            ) from exc
        return plaintext.decode("utf-8")


_cipher: Optional[Cipher] = None


def get_cipher(encoded_key: Optional[str] = None) -> Cipher:
    global _cipher
    if _cipher is None:
        if encoded_key is None:
            from backend.config import get_settings

            encoded_key = get_settings().credential_encryption_key
        _cipher = Cipher.from_settings(encoded_key)
    return _cipher


def reset_cipher() -> None:
    """Test hook, and used when the key changes at runtime."""
    global _cipher
    _cipher = None
