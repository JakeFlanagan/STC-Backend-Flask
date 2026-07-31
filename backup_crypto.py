"""Encryption for backup/restore files (server-secret-keyed) and
export/import files (password-keyed). Two separate schemes, both built on
cryptography's Fernet (AES-128-CBC + HMAC-SHA256, authenticated so a
tampered/corrupted file always fails loudly rather than decrypting to
garbage):

  - *_with_secret: derives one fixed key from config.DB_ENCRYPTION_KEY -
    used for backups/restore, which happen with no human present (e.g.
    app.py's before_request auto-backup hook) so there's no opportunity to
    ask for a password.
  - *_with_password: derives a key from a caller-supplied password plus a
    random per-call salt (stored alongside the ciphertext) via PBKDF2 -
    used for export/import, so a file can be decrypted on a different
    machine with no shared secret beyond the password itself.
"""

import base64
import hashlib
import secrets
import struct

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

SALT_LENGTH = 16
ITERATIONS_LENGTH = 4  # bytes - struct "I" (unsigned int, big-endian)
PBKDF2_ITERATIONS = 600_000
SQLITE_HEADER = b"SQLite format 3\x00"


class DecryptionError(Exception):
    """Raised by decrypt_with_secret/decrypt_with_password when the
    ciphertext can't be decrypted - wrong password/key, corrupted file, or
    not a Fernet token at all. Callers never need to distinguish which."""


def _fernet_key_from_secret(secret: str) -> bytes:
    """A Fernet key deterministically derived from an arbitrary string -
    same secret always produces the same key, so encrypt_with_secret and
    decrypt_with_secret always agree without storing anything extra."""
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet_key_from_password(password: str, salt: bytes, iterations: int) -> bytes:
    """A Fernet key derived from a password and a salt via PBKDF2 - unlike
    _fernet_key_from_secret, the salt must be generated fresh per
    encryption and stored alongside the ciphertext (see
    encrypt_with_password), since PBKDF2 needs it again to re-derive the
    same key for decryption. iterations is likewise read back from the
    blob rather than assumed, so raising PBKDF2_ITERATIONS later doesn't
    break decrypting files written under a lower count."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_with_secret(data: bytes, secret: str) -> bytes:
    """Encrypt data with a key derived from secret (config.DB_ENCRYPTION_KEY).
    Used for backups - the same secret always decrypts what it encrypted."""
    return Fernet(_fernet_key_from_secret(secret)).encrypt(data)


def decrypt_with_secret(data: bytes, secret: str) -> bytes:
    """Decrypt data encrypted by encrypt_with_secret with the same secret.
    Raises DecryptionError if the secret is wrong or data is corrupted."""
    try:
        return Fernet(_fernet_key_from_secret(secret)).decrypt(data)
    except InvalidToken:
        raise DecryptionError("could not decrypt: wrong secret or corrupted data") from None


def encrypt_with_password(data: bytes, password: str) -> bytes:
    """Encrypt data with a key derived from password and a fresh random
    salt. Returns iteration-count + salt + ciphertext concatenated - the
    iteration count actually used (PBKDF2_ITERATIONS at the time of this
    call) travels with the file so decrypt_with_password derives the same
    key even if PBKDF2_ITERATIONS is raised later; the salt itself isn't
    secret, only the password is."""
    salt = secrets.token_bytes(SALT_LENGTH)
    token = Fernet(_fernet_key_from_password(password, salt, PBKDF2_ITERATIONS)).encrypt(data)
    return struct.pack(">I", PBKDF2_ITERATIONS) + salt + token


def decrypt_with_password(blob: bytes, password: str) -> bytes:
    """Decrypt a blob produced by encrypt_with_password, given the same
    password. Raises DecryptionError if the password is wrong, the blob is
    corrupted, or the blob is too short to even contain an iteration count
    and salt."""
    header_len = ITERATIONS_LENGTH + SALT_LENGTH
    if len(blob) < header_len:
        raise DecryptionError("could not decrypt: data too short to be valid")
    (iterations,) = struct.unpack(">I", blob[:ITERATIONS_LENGTH])
    salt = blob[ITERATIONS_LENGTH:header_len]
    token = blob[header_len:]
    try:
        return Fernet(_fernet_key_from_password(password, salt, iterations)).decrypt(token)
    except InvalidToken:
        raise DecryptionError("could not decrypt: wrong password or corrupted data") from None
