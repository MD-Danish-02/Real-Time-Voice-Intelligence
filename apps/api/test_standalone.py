#!/usr/bin/env python
"""
Standalone unit tests for Plivo integration - no database required.
"""
import hmac
import hashlib
import base64
import uuid
from urllib.parse import urlparse

# Test 1: Plivo signature verification
def verify_plivo_signature(auth_token: str, url: str, post_params: dict, signature: str) -> bool:
    """Verify Plivo webhook signature."""
    if not signature:
        return False
    parsed = urlparse(url)
    path_and_query = parsed.path
    if parsed.query:
        path_and_query += "?" + parsed.query
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(post_params.items()))
    to_sign = path_and_query + sorted_params
    expected = hmac.new(
        auth_token.encode(),
        to_sign.encode(),
        hashlib.sha256
    ).digest()
    expected_b64 = base64.b64encode(expected).decode()
    return hmac.compare_digest(expected_b64, signature)

def test_signature_verification():
    auth_token = "test_auth_token"
    url = "https://api.example.com/plivo/xml/call-id?sig=test"
    post_params = {"CallUUID": "test-uuid", "CallStatus": "in-progress"}
    
    # Valid signature
    parsed = urlparse(url)
    path_and_query = parsed.path
    if parsed.query:
        path_and_query += "?" + parsed.query
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(post_params.items()))
    to_sign = path_and_query + sorted_params
    expected = hmac.new(auth_token.encode(), to_sign.encode(), hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected).decode()
    
    assert verify_plivo_signature(auth_token, url, post_params, expected_b64), "Valid signature should pass"
    assert not verify_plivo_signature(auth_token, url, post_params, "invalid"), "Invalid signature should fail"
    assert not verify_plivo_signature(auth_token, url, post_params, ""), "Empty signature should fail"
    assert not verify_plivo_signature(auth_token, url, post_params, None), "None signature should fail"
    print("[PASS] Plivo signature verification tests passed")

# Test 2: Call webhook signature
def call_webhook_signature(call_id: uuid.UUID) -> str:
    """Generate call webhook signature."""
    return hmac.new(
        b"test-secret-key-for-testing-only",
        str(call_id).encode(),
        hashlib.sha256
    ).hexdigest()[:40]

def verify_call_webhook_signature(call_id: uuid.UUID, signature: str) -> bool:
    """Verify call webhook signature."""
    expected = call_webhook_signature(call_id)
    return hmac.compare_digest(expected, signature)

def test_call_webhook_signature():
    call_id = uuid.uuid4()
    sig = call_webhook_signature(call_id)
    assert isinstance(sig, str), "Signature should be string"
    assert len(sig) == 40, "Signature should be 40 chars"
    assert verify_call_webhook_signature(call_id, sig), "Valid signature should pass"
    assert not verify_call_webhook_signature(call_id, "invalid"), "Invalid signature should fail"
    print("[PASS] Call webhook signature tests passed")

# Test 3: Plivo XML generation
def generate_plivo_xml(sip_host: str) -> str:
    """Generate Plivo XML for dialing Asterisk SIP endpoint."""
    if not sip_host:
        return '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN" language="en-IN">Service temporarily unavailable. Please try again later.</Speak>
    <Hangup/>
</Response>'''
    sip_uri = f"sip:voxai_voice_app@{sip_host}"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Sip>{sip_uri}</Sip>
    </Dial>
</Response>'''

def test_xml_generation():
    # With SIP host configured
    xml = generate_plivo_xml("161.118.180.20")
    assert '<?xml version="1.0" encoding="UTF-8"?>' in xml
    assert "<Response>" in xml
    assert "<Dial>" in xml
    assert "<Sip>sip:voxai_voice_app@161.118.180.20</Sip>" in xml
    assert "</Dial>" in xml
    assert "</Response>" in xml
    
    # Fallback when SIP host not configured
    fallback_xml = generate_plivo_xml(None)
    assert "<Speak" in fallback_xml
    assert "Service temporarily unavailable" in fallback_xml
    assert "<Hangup/>" in fallback_xml
    print("[PASS] Plivo XML generation tests passed")

# Test 4: Configuration values
def test_config():
    import os
    os.environ.update({
        'PLIVO_AUTH_ID': 'TEST',
        'PLIVO_AUTH_TOKEN': 'TEST',
        'PLIVO_CALLER_ID': '+15551234567',
        'SIP_PUBLIC_HOST': '161.118.180.20',
        'VOICE_MEDIA_HOST': 'backend',
        'VOICE_MEDIA_HOST_EXTERNAL': '161.118.180.20',
        'JWT_SECRET': 'test-secret-key-for-testing-only',
        'ENV': 'test',
    })
    
    from app.core.config import settings
    assert settings.SIP_PUBLIC_HOST == "161.118.180.20"
    assert settings.VOICE_MEDIA_HOST == "backend"
    assert settings.VOICE_MEDIA_HOST_EXTERNAL == "161.118.180.20"
    print("[PASS] Configuration tests passed")

if __name__ == "__main__":
    test_signature_verification()
    test_call_webhook_signature()
    test_xml_generation()
    test_config()
    print("\n[PASS] All standalone tests passed!")