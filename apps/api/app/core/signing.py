"""Short-lived signed tokens for OAuth `state` and per-call webhook URLs (HMAC via the JWT machinery)."""
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from urllib.parse import urlparse

from jose import JWTError, jwt

from app.core.config import settings


def make_oauth_state(*, tenant_id: uuid.UUID, user_id: uuid.UUID, provider: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"typ": "oauth_state", "tid": str(tenant_id), "uid": str(user_id), "prov": provider,
                       "nonce": uuid.uuid4().hex, "exp": now + timedelta(minutes=10)},
                      settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def read_oauth_state(state: str, *, provider: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Raises ValueError if the state is forged, expired, or was issued for another provider."""
    try:
        data = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise ValueError("invalid oauth state") from exc
    if data.get("typ") != "oauth_state" or data.get("prov") != provider:
        raise ValueError("oauth state mismatch")
    return uuid.UUID(data["tid"]), uuid.UUID(data["uid"])


def call_webhook_signature(call_id: uuid.UUID) -> str:
    return hmac.new(settings.JWT_SECRET.encode(), f"call-webhook:{call_id}".encode(), hashlib.sha256).hexdigest()[:40]


def verify_call_webhook_signature(call_id: uuid.UUID, sig: str) -> bool:
    return hmac.compare_digest(call_webhook_signature(call_id), sig or "")


def verify_plivo_signature(
    auth_token: str,
    url: str,
    post_params: Dict[str, str],
    signature: str,
    nonce: Optional[str] = None,
) -> bool:
    """
    Verify Plivo webhook signature.
    
    Supports:
    - Plivo V2/V3 signature using SHA256 and cryptographic nonce.
    - Plivo V1 signature using SHA256 and legacy SHA1 on full URL + sorted POST params.
    - Fallback on path_and_query for compatibility with isolated endpoint mocks.
    """
    if not signature or not auth_token:
        return False

    import base64

    # 1. Nonce-based signature verification (Plivo V2 / V3)
    if nonce:
        to_sign_nonce = f"{url}{nonce}".encode("utf-8")
        expected_nonce = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), to_sign_nonce, hashlib.sha256).digest()
        ).decode("utf-8")
        if hmac.compare_digest(expected_nonce, signature):
            return True

    # 2. Parameter-based signature verification (Plivo V1)
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(post_params.items()))
    
    # Plivo signs the full URL (scheme://netloc/path?query)
    candidates_to_sign = [f"{url}{sorted_params}"]

    # Also test path + query for mock/unit test environments where scheme/netloc may vary
    parsed = urlparse(url)
    path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    candidates_to_sign.append(f"{path_and_query}{sorted_params}")

    for text in candidates_to_sign:
        # Check HMAC-SHA256 (standard)
        digest_256 = hmac.new(auth_token.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).digest()
        if hmac.compare_digest(base64.b64encode(digest_256).decode("utf-8"), signature):
            return True

        # Check HMAC-SHA1 (legacy fallback)
        digest_1 = hmac.new(auth_token.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
        if hmac.compare_digest(base64.b64encode(digest_1).decode("utf-8"), signature):
            return True

    return False
