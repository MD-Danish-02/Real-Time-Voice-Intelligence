"""
Fernet-based symmetric encryption for secrets stored at rest (CRM OAuth
tokens, webhook secrets). ENCRYPTION_KEY must be a 32-byte urlsafe-base64
key in real deployments - see .env.example for how to generate one.
"""
import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import settings


def _derive_fernet_key(raw_key: str) -> bytes:
    # Accepts any string and derives a valid 32-byte urlsafe-base64 Fernet
    # key from it via SHA-256, so a human-friendly ENCRYPTION_KEY in .env
    # still works without the operator needing to hand-generate one.
    digest = hashlib.sha256(raw_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_derive_fernet_key(settings.ENCRYPTION_KEY))


def encrypt_secret(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
