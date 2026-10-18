"""Credential primitives. Human and machine credentials have separate hashes."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

password_hasher = PasswordHasher()  # argon2-cffi defaults to Argon2id.


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def verify_token(token: str, stored_digest: str) -> bool:
    return hmac.compare_digest(token_digest(token), stored_digest)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    return password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return password_hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False
