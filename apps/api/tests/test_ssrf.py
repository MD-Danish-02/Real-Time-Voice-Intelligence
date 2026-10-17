import pytest

from app.security.ssrf import UnsafeOutboundURL, validate_public_url


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "https://10.2.3.4/",
    "https://[::1]/",
    "https://localhost/",
    "https://service.internal/",
    "https://user:password@example.com/",
])
async def test_tenant_urls_cannot_target_local_or_private_hosts(url):
    with pytest.raises(UnsafeOutboundURL):
        await validate_public_url(url)


@pytest.mark.asyncio
async def test_custom_api_url_requires_https():
    with pytest.raises(UnsafeOutboundURL):
        await validate_public_url("http://example.com/leads", require_https=True)
