"""Password hashing, session tokens, and API key generation.

v0 uses stdlib-only primitives (hashlib.scrypt + hmac) so the server runs with
zero native dependencies. Production deployments should swap in argon2/bcrypt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

# scrypt parameters (N, r, p). N=2**14 is a reasonable interactive-work factor.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32


def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash: scrypt$N$r$p$salt$hash."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_HASH_BYTES,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored scrypt hash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def new_id(prefix: str) -> str:
    """Generate a prefixed random id, e.g. acc_01HZ2..."""
    return f"{prefix}_{secrets.token_hex(8)}"


def new_api_key(scope: str) -> str:
    """Generate a scoped API key, e.g. sk-worker-<random>."""
    return f"sk-{scope}-{secrets.token_urlsafe(24)}"


def new_session_token() -> str:
    return f"sess_{secrets.token_urlsafe(24)}"


def sign_session(token: str, secret: str, expires_at: int) -> str:
    """Return a signed session token: <token>.<expiry>.<hmac>."""
    payload = f"{token}.{expires_at}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(signed: str, secret: str, now: int | None = None) -> tuple[str, int] | None:
    """Verify a signed session token. Returns (token, expires_at) or None."""
    now = now if now is not None else int(time.time())
    try:
        token, exp_str, sig = signed.rsplit(".", 2)
        expires_at = int(exp_str)
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), f"{token}.{expires_at}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if expires_at < now:
        return None
    return token, expires_at
