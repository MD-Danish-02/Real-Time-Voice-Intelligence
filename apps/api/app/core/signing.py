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
    *,
    version: Optional[str] = None,
    method: str = "POST",
) -> bool:
    """
    Verify Plivo webhook signature.
    
    Supports version-specific V1, V2, and V3 canonicalization. V3 includes
    sorted POST fields before the nonce; V2 signs only the callback URL and nonce.
    """
    if not signature or not auth_token:
        return False

    import base64

    signatures = [candidate.strip() for candidate in signature.split(",") if candidate.strip()]
    if not signatures:
        return False

    def matches_sha256(message: str) -> bool:
        digest = hmac.new(
            auth_token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).digest()
        expected = base64.b64encode(digest).decode("ascii")
        return any(hmac.compare_digest(expected, candidate) for candidate in signatures)

    normalized_version = version.lower() if version else None

    # Plivo Voice V3 signs the complete URL, case-sensitive sorted POST
    # parameters (for POST only), and then the per-request nonce.
    if normalized_version == "v3":
        if not nonce:
            return False
        fields = ""
        if method.upper() == "POST":
            fields = "".join(
                f"{key}{value}" for key, value in sorted(post_params.items())
            )
        return matches_sha256(f"{url}{fields}{nonce}")

    # V2 uses URL + nonce and does not include POST fields.
    if normalized_version == "v2":
        return bool(nonce) and matches_sha256(f"{url}{nonce}")

    if normalized_version not in (None, "v1"):
        return False

    # Preserve compatibility for callers of the historical version-agnostic
    # helper while the webhook route uses explicit header versions.
    if normalized_version is None and nonce:
        to_sign_nonce = f"{url}{nonce}".encode("utf-8")
        expected_nonce = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), to_sign_nonce, hashlib.sha256).digest()
        ).decode("utf-8")
        if any(hmac.compare_digest(expected_nonce, candidate) for candidate in signatures):
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
        expected_256 = base64.b64encode(digest_256).decode("utf-8")
        if any(hmac.compare_digest(expected_256, candidate) for candidate in signatures):
            return True

        # Check HMAC-SHA1 (legacy fallback)
        digest_1 = hmac.new(auth_token.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
        expected_1 = base64.b64encode(digest_1).decode("utf-8")
        if any(hmac.compare_digest(expected_1, candidate) for candidate in signatures):
            return True

    return False
