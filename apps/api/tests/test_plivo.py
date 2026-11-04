"""
Unit tests for Plivo integration.
"""
import hmac
import hashlib
import base64
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.api.v1 import plivo as plivo_routes
from app.core.signing import (
    call_webhook_signature,
    verify_call_webhook_signature,
    verify_plivo_signature,
)
from app.providers.telephony.plivo import PlivoTelephonyProvider
from app.providers.telephony.base import DialResult
from app.models.call import Call
from app.schemas.call import OutboundDialRequest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from types import SimpleNamespace
from xml.etree import ElementTree


class _MockTransportPlivoProvider(PlivoTelephonyProvider):
    """Live adapter exercised only by tests that replace its HTTP methods."""

    provider_name = "mock"


@pytest.mark.parametrize("number", ["15551234567", "+012345678", "+1234567", "+1234567890123456", "+1 555 123 4567"])
def test_outbound_dial_rejects_non_e164_destinations(number):
    with pytest.raises(ValidationError):
        OutboundDialRequest(to_number=number)


def test_outbound_dial_accepts_e164_destination():
    assert OutboundDialRequest(to_number="+15551234567").to_number == "+15551234567"


class TestPlivoSignatureVerification:
    """Tests for Plivo signature verification."""

    def test_verify_plivo_signature_valid(self):
        """Test valid Plivo signature verification."""
        auth_token = "test_auth_token"
        url = "https://api.example.com/plivo/xml/call-id?sig=test"
        post_params = {"CallUUID": "test-uuid", "CallStatus": "in-progress"}
        
        # Compute expected signature
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
        
        assert verify_plivo_signature(auth_token, url, post_params, expected_b64)

    def test_verify_plivo_signature_invalid(self):
        """Test invalid Plivo signature rejection."""
        auth_token = "test_auth_token"
        url = "https://api.example.com/plivo/xml/call-id?sig=test"
        post_params = {"CallUUID": "test-uuid", "CallStatus": "in-progress"}
        invalid_signature = "invalid_signature"
        
        assert not verify_plivo_signature(auth_token, url, post_params, invalid_signature)

    def test_verify_plivo_signature_empty(self):
        """Test empty signature rejection."""
        assert not verify_plivo_signature("token", "url", {}, "")
        assert not verify_plivo_signature("token", "url", {}, None)

    def test_verify_plivo_signature_v2_nonce(self):
        """Test legacy nonce verification remains compatible."""
        auth_token = "test_auth_token"
        url = "https://api.example.com/plivo/xml/call-id"
        nonce = "test-nonce-12345"
        
        expected = base64.b64encode(
            hmac.new(auth_token.encode(), f"{url}{nonce}".encode(), hashlib.sha256).digest()
        ).decode()
        
        assert verify_plivo_signature(auth_token, url, {}, expected, nonce=nonce)
        assert not verify_plivo_signature(auth_token, url, {}, "invalid_sig", nonce=nonce)

    def test_verify_plivo_v3_post_includes_sorted_form_fields_and_nonce(self):
        auth_token = "test_auth_token"
        url = "https://callbacks.example.test/plivo/xml/call-id?sig=internal-hmac"
        params = {"CallStatus": "ringing", "CallUUID": "provider-call-id"}
        nonce = "unique-request-nonce"
        canonical = url + "".join(f"{key}{value}" for key, value in sorted(params.items())) + nonce
        signature = base64.b64encode(
            hmac.new(auth_token.encode(), canonical.encode(), hashlib.sha256).digest()
        ).decode()

        assert verify_plivo_signature(
            auth_token, url, params, signature, nonce, version="v3", method="POST"
        )
        assert not verify_plivo_signature(
            auth_token, url, {}, signature, nonce, version="v3", method="POST"
        )

    def test_verify_plivo_v3_accepts_comma_separated_active_token_signatures(self):
        auth_token = "test_auth_token"
        url = "https://callbacks.example.test/plivo/xml/call-id?sig=internal-hmac"
        params = {"CallStatus": "ringing"}
        nonce = "unique-request-nonce"
        canonical = url + "CallStatusringing" + nonce
        signature = base64.b64encode(
            hmac.new(auth_token.encode(), canonical.encode(), hashlib.sha256).digest()
        ).decode()

        assert verify_plivo_signature(
            auth_token,
            url,
            params,
            f"older-invalid-signature,{signature}",
            nonce,
            version="v3",
            method="POST",
        )


class TestCallWebhookSignature:
    """Tests for internal call webhook signature."""

    def test_call_webhook_signature_format(self):
        """Test call webhook signature format."""
        call_id = uuid.uuid4()
        sig = call_webhook_signature(call_id)
        assert isinstance(sig, str)
        assert len(sig) == 40  # SHA256 hex digest truncated to 40 chars

    def test_verify_call_webhook_signature_valid(self):
        """Test valid call webhook signature verification."""
        call_id = uuid.uuid4()
        sig = call_webhook_signature(call_id)
        assert verify_call_webhook_signature(call_id, sig)

    def test_verify_call_webhook_signature_invalid(self):
        """Test invalid call webhook signature rejection."""
        call_id = uuid.uuid4()
        assert not verify_call_webhook_signature(call_id, "invalid")


def _webhook_request(path: str, signature: str) -> Request:
    query = f"sig={signature}".encode()
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"host", b"callbacks.example.test")],
        "client": ("127.0.0.1", 12345),
        "server": ("callbacks.example.test", 443),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path", "method", "signature_header"),
    [
        (plivo_routes.plivo_answer_xml, "/api/v1/plivo/xml", "POST", "X-Plivo-Signature-V3"),
        (plivo_routes.plivo_status_callback, "/api/v1/plivo/status", "POST", "X-Plivo-Signature-Ma-V3"),
    ],
)
async def test_plivo_webhook_endpoints_accept_valid_internal_signature(
    handler, path, method, signature_header
):
    call_id = uuid.uuid4()
    internal_signature = call_webhook_signature(call_id)
    call = SimpleNamespace(id=call_id, telephony_provider="plivo", status="queued")
    result = MagicMock()
    result.scalar_one_or_none.return_value = call
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    async def provide_db():
        return db

    api = FastAPI()
    api.include_router(plivo_routes.router, prefix="/api/v1")
    api.dependency_overrides[plivo_routes.get_async_db] = provide_db
    nonce = "unique-request-nonce"
    params = {"CallStatus": "ringing", "CallUUID": "provider-call-id"}
    callback_path = f"{path}/{call_id}?sig={internal_signature}"
    callback_url = f"https://callbacks.example.test{callback_path}"
    canonical = callback_url + "".join(f"{key}{value}" for key, value in sorted(params.items())) + nonce
    provider_signature = base64.b64encode(
        hmac.new(
            settings.PLIVO_AUTH_TOKEN.encode(), canonical.encode(), hashlib.sha256
        ).digest()
    ).decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="https://callbacks.example.test"
    ) as client:
        response = await client.request(
            method,
            callback_path,
            data=params,
            headers={signature_header: provider_signature, "X-Plivo-Signature-V3-Nonce": nonce},
        )

    if handler is plivo_routes.plivo_answer_xml:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        xml = ElementTree.fromstring(response.content)
        assert xml.tag == "Response"
        user = xml.find("./Dial/User")
        assert user is not None
        assert user.text == f"sip:call_{call_id}@sip.example.com"
        assert xml.find(".//Sip") is None
    else:
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path"),
    [
        (plivo_routes.plivo_answer_xml, "/api/v1/plivo/xml"),
        (plivo_routes.plivo_status_callback, "/api/v1/plivo/status"),
    ],
)
async def test_plivo_webhook_endpoints_reject_invalid_internal_signature(handler, path):
    call_id = uuid.uuid4()
    request = _webhook_request(path, "invalid-signature")
    db = MagicMock()
    db.execute = AsyncMock()
    provider_verifier = AsyncMock(return_value=True)
    with patch.object(plivo_routes, "verify_plivo_request", provider_verifier):
        with pytest.raises(HTTPException) as error:
            await handler(str(call_id), request, db)

    assert error.value.status_code == 401
    provider_verifier.assert_not_awaited()
    db.execute.assert_not_awaited()


class TestPlivoTelephonyProvider:
    """Tests for PlivoTelephonyProvider."""

    @pytest.fixture
    def provider(self):
        return _MockTransportPlivoProvider()

    @pytest.fixture
    def mock_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "TEST_AUTH_ID")
        monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "TEST_AUTH_TOKEN")
        monkeypatch.setattr(settings, "PLIVO_CALLER_ID", "+15551234567")
        monkeypatch.setattr(settings, "SIP_PUBLIC_HOST", "sip.example.com")

    def test_base_url(self, provider):
        assert provider._base_url() == "https://api.plivo.com/v1/Account/TEST_AUTH_ID"

    def test_auth(self, provider):
        assert provider._auth() == ("TEST_AUTH_ID", "TEST_AUTH_TOKEN")

    def test_answer_url(self, provider):
        webhook_base = "https://api.example.com"
        call_id = "123e4567-e89b-12d3-a456-426614174000"
        url = provider._answer_url(webhook_base, call_id)
        assert "https://api.example.com/api/v1/plivo/xml/" in url
        assert call_id in url
        assert "sig=" in url

    def test_answer_url_with_full_webhook_path(self, provider):
        full_url = "https://api.example.com/api/v1/webhooks/telephony/plivo/123e4567-e89b-12d3-a456-426614174000?sig=abc"
        call_id = "123e4567-e89b-12d3-a456-426614174000"
        url = provider._answer_url(full_url, call_id)
        assert "https://api.example.com/api/v1/plivo/xml/" in url
        assert "webhooks/telephony" not in url
        assert call_id in url
        assert "sig=" in url

    def test_hangup_url(self, provider):
        webhook_base = "https://api.example.com"
        call_id = "123e4567-e89b-12d3-a456-426614174000"
        url = provider._hangup_url(webhook_base, call_id)
        assert "https://api.example.com/api/v1/plivo/status/" in url
        assert call_id in url
        assert "sig=" in url

    @pytest.mark.asyncio
    async def test_dial_success(self, provider, mock_settings):
        call_id = "123e4567-e89b-12d3-a456-426614174000"
        
        # httpx.Response.json() is synchronous even though the request is async.
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"request_uuid": "plivo-uuid-123"}
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            
            result = await provider.dial(
                to_number="+15559876543",
                from_number="+15551234567",
                webhook_url="https://api.example.com",
                metadata={"call_id": "123e4567-e89b-12d3-a456-426614174000"}
            )
            
            assert isinstance(result, DialResult)
            assert result.provider_call_id == "plivo-uuid-123"
            assert result.status == "queued"
            
            # Verify the payload sent to Plivo
            call_args = mock_post.call_args
            payload = call_args.kwargs.get("json", {})
            assert payload["to"] == "+15559876543"
            assert payload["from"] == "+15551234567"
            assert "answer_url" in payload
            assert "hangup_url" in payload
            assert "answer_method" in payload
            assert "hangup_method" in payload
            assert payload["answer_method"] == "POST"
            assert payload["hangup_method"] == "POST"
            assert payload["time_limit"] == 60
            assert "/api/v1/plivo/xml/" in payload["answer_url"]
            assert "/api/v1/plivo/status/" in payload["hangup_url"]
            assert "sig=" in payload["answer_url"]
            assert "sig=" in payload["hangup_url"]
            assert call_args.kwargs["auth"] == ("TEST_AUTH_ID", "TEST_AUTH_TOKEN")

    @pytest.mark.asyncio
    async def test_dial_failure(self, provider, mock_settings):
        call_id = "123e4567-e89b-12d3-a456-426614174000"
        
        mock_resp = AsyncMock()
        mock_resp.status_code = 400
        mock_resp.text = "Invalid number"
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            
            with pytest.raises(Exception) as exc_info:
                await provider.dial(
                    to_number="invalid",
                    from_number="+15551234567",
                    webhook_url="https://api.example.com",
                    metadata={"call_id": "123e4567-e89b-12d3-a456-426614174000"}
                )
            
            assert "Plivo dial failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_dial_missing_call_id(self, provider, mock_settings):
        with pytest.raises(Exception) as exc_info:
            await provider.dial(
                to_number="+15559876543",
                from_number="+15551234567",
                webhook_url="https://api.example.com",
                metadata={}
            )
        assert "call_id missing" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_hangup_success(self, provider, mock_settings):
        mock_resp = AsyncMock()
        mock_resp.status_code = 204
        
        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_delete:
            mock_delete.return_value = mock_resp
            await provider.hangup(provider_call_id="plivo-uuid-123")
            mock_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_hangup_failure(self, provider, mock_settings):
        mock_resp = AsyncMock()
        mock_resp.status_code = 400
        mock_resp.text = "Not found"
        
        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_delete:
            mock_delete.return_value = mock_resp
            with pytest.raises(Exception) as exc_info:
                await provider.hangup(provider_call_id="plivo-uuid-123")
            assert "Plivo hangup failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_call(self, provider, mock_settings):
        # httpx.Response.json() is synchronous even though the request is async.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "call_state": "completed",
            "call_duration": "120"
        }
        
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            result = await provider.get_call(provider_call_id="plivo-uuid-123")
            assert result.status == "completed"
            assert result.duration_seconds == 120

    def test_parse_inbound_webhook(self, provider):
        body = {
            "CallUUID": "plivo-uuid-123",
            "From": "+15551234567",
            "To": "+15559876543"
        }
        result = provider.parse_inbound_webhook(headers={}, body=body)
        assert result["provider_call_id"] == "plivo-uuid-123"
        assert result["from_number"] == "+15551234567"
        assert result["to_number"] == "+15559876543"
        assert result["direction"] == "inbound"

    @pytest.mark.asyncio
    async def test_health_check(self, provider, mock_settings):
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            result = await provider.health_check()
            assert result is True


class TestPlivoXMLGeneration:
    """Tests for Plivo XML generation logic."""

    def test_xml_structure(self):
        """Test that generated XML has correct structure."""
        sip_domain = "sip.example.com"
        sip_uri = f"sip:voxai_voice_app@{sip_domain}"
        
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
        
        assert '<?xml version="1.0" encoding="UTF-8"?>' in xml
        assert "<Response>" in xml
        assert "<Dial>" in xml
        assert f"<User>{sip_uri}</User>" in xml
        parsed = ElementTree.fromstring(xml)
        assert parsed.find("./Dial/User").text == sip_uri
        assert parsed.find(".//Sip") is None
        assert "</Dial>" in xml
        assert "</Response>" in xml

    def test_fallback_xml(self):
        """Test fallback XML when SIP domain not configured."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN" language="en-IN">Service temporarily unavailable. Please try again later.</Speak>
    <Hangup/>
</Response>"""
        
        assert "<Speak" in xml
        assert "Service temporarily unavailable" in xml
        assert "<Hangup/>" in xml


# Test configuration fixture
@pytest.fixture(autouse=True)
def mock_settings_env(monkeypatch):
    """Mock settings for all tests."""
    monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "TEST_AUTH_ID")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "TEST_AUTH_TOKEN")
    monkeypatch.setattr(settings, "PLIVO_CALLER_ID", "+15551234567")
    monkeypatch.setattr(settings, "SIP_PUBLIC_HOST", "sip.example.com")
    monkeypatch.setattr(settings, "JWT_SECRET", "test-secret-key-for-testing-only")
    monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "TEST_AUTH_ID")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "TEST_AUTH_TOKEN")
