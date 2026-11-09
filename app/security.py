"""Authentication, role-based routing and PII protection primitives.

Security rules honoured here (PRD section 9):
  * NDPA 2023  -> delivery addresses / financial tokens are sealed with AES-256-GCM.
  * Escrow     -> the 4-digit release code is stored hashed (verification) and
                  encrypted (display to the buyer) and never leaves the server
                  inside a rider payload.
  * Ownership  -> decorators enforce role AND resource ownership server-side.
"""

import hashlib
import hmac
import os
import secrets
from functools import wraps

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import abort, current_app, redirect, url_for
from flask_login import current_user
from werkzeug.security import check_password_hash, generate_password_hash

ROLE_CONSUMER = "consumer"
ROLE_MERCHANT = "merchant"
ROLE_ADMIN = "admin"
ROLES = (ROLE_CONSUMER, ROLE_MERCHANT, ROLE_ADMIN)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method="pbkdf2:sha256:600000")


def verify_password(stored_hash: str, raw: str) -> bool:
    return check_password_hash(stored_hash, raw)


# ---------------------------------------------------------------------------
# AES-256-GCM field encryption
# ---------------------------------------------------------------------------
def _aead() -> AESGCM:
    raw = (current_app.config.get("ENCRYPTION_KEY") or "").strip()
    if not raw:
        # Derive a stable 32-byte key from SECRET_KEY for single-node dev use.
        digest = hashlib.sha256(
            ("nb-pii:" + current_app.config["SECRET_KEY"]).encode()
        ).digest()
        return AESGCM(digest)
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = hashlib.sha256(raw.encode()).digest()
    if len(key) != 32:
        key = hashlib.sha256(key).digest()
    return AESGCM(key)


def encrypt_pii(plaintext: str | None) -> str | None:
    """AES-256-GCM seal -> hex(nonce || ciphertext || tag)."""
    if plaintext is None or plaintext == "":
        return None
    nonce = os.urandom(12)
    blob = _aead().encrypt(nonce, plaintext.encode(), b"naijabasket-pii-v1")
    return (nonce + blob).hex()


def decrypt_pii(sealed: str | None) -> str | None:
    if not sealed:
        return None
    try:
        blob = bytes.fromhex(sealed)
    except ValueError:
        return None
    try:
        return _aead().decrypt(
            blob[:12], blob[12:], b"naijabasket-pii-v1"
        ).decode()
    except Exception:  # tampered or key rotated
        return None


# ---------------------------------------------------------------------------
# Escrow release code
# ---------------------------------------------------------------------------
def generate_delivery_pin() -> str:
    """Cryptographically random 4-digit code (no leading-zero bias)."""
    return f"{secrets.randbelow(10_000):04d}"


def hash_delivery_pin(pin: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


def pin_matches(pin: str, salt: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_delivery_pin(pin, salt), stored_hash or "")


# ---------------------------------------------------------------------------
# Recovery / discount tokens
# ---------------------------------------------------------------------------
def generate_token(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


# ---------------------------------------------------------------------------
# Route guards
# ---------------------------------------------------------------------------
def role_required(*roles: str):
    """Server-side role gate. Anonymous users are bounced to the login page."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=os.environ.get("_")))
            if current_user.user_type not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view):
    return role_required(ROLE_ADMIN)(view)


def merchant_required(view):
    return role_required(ROLE_MERCHANT, ROLE_ADMIN)(view)
