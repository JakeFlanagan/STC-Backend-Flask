"""Tests for backup_crypto.py: Fernet-based encryption for backup files
(server-secret-keyed) and export/import files (password-keyed, salted).
Called directly (pure functions, no Flask/HTTP involved).
"""

import backup_crypto
from helpers import expect


def test_encrypt_decrypt_with_secret_round_trip():
    data = b"hello world, this is sqlite-shaped data"
    encrypted = backup_crypto.encrypt_with_secret(data, "some-server-secret")
    decrypted = backup_crypto.decrypt_with_secret(encrypted, "some-server-secret")
    expect("round trip preserves data", data, decrypted)


def test_decrypt_with_secret_wrong_secret_raises():
    data = b"sensitive bytes"
    encrypted = backup_crypto.encrypt_with_secret(data, "secret-a")
    raised = False
    try:
        backup_crypto.decrypt_with_secret(encrypted, "secret-b")
    except backup_crypto.DecryptionError:
        raised = True
    expect("wrong secret raises DecryptionError", True, raised)


def test_encrypt_with_secret_output_is_not_plaintext():
    data = b"SQLite format 3\x00" + b"plaintext content"
    encrypted = backup_crypto.encrypt_with_secret(data, "some-server-secret")
    expect("encrypted output does not contain the plaintext", False, data in encrypted)


def test_encrypt_decrypt_with_password_round_trip():
    data = b"another chunk of sqlite-shaped bytes"
    encrypted = backup_crypto.encrypt_with_password(data, "correct-password-123")
    decrypted = backup_crypto.decrypt_with_password(encrypted, "correct-password-123")
    expect("round trip preserves data", data, decrypted)


def test_decrypt_with_password_wrong_password_raises():
    data = b"exported data"
    encrypted = backup_crypto.encrypt_with_password(data, "correct-password-123")
    raised = False
    try:
        backup_crypto.decrypt_with_password(encrypted, "wrong-password-456")
    except backup_crypto.DecryptionError:
        raised = True
    expect("wrong password raises DecryptionError", True, raised)


def test_encrypt_with_password_uses_a_random_salt_each_time():
    data = b"same plaintext, encrypted twice"
    first = backup_crypto.encrypt_with_password(data, "same-password")
    second = backup_crypto.encrypt_with_password(data, "same-password")
    expect("two encryptions of the same data+password differ", True, first != second)


def test_decrypt_with_password_rejects_truncated_blob():
    raised = False
    try:
        backup_crypto.decrypt_with_password(b"too-short", "any-password")
    except backup_crypto.DecryptionError:
        raised = True
    expect("truncated blob raises DecryptionError", True, raised)


def test_decrypt_with_password_survives_iteration_count_bump(monkeypatch):
    """A file encrypted under the old PBKDF2_ITERATIONS must still decrypt
    correctly after the constant is raised, because the count actually used
    travels in the blob (see encrypt_with_password)."""
    data = b"exported before the iteration count was raised"
    monkeypatch.setattr(backup_crypto, "PBKDF2_ITERATIONS", 100_000)
    old_encrypted = backup_crypto.encrypt_with_password(data, "correct-password-123")

    monkeypatch.setattr(backup_crypto, "PBKDF2_ITERATIONS", 600_000)
    decrypted = backup_crypto.decrypt_with_password(old_encrypted, "correct-password-123")
    expect("old blob still decrypts after the constant changes", data, decrypted)
