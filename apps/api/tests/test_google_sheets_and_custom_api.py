async def test_google_sheets_connect_fails_clearly_without_credentials(client, tenant_a):
    r = await client.post("/api/v1/crm/connect", headers=tenant_a, json={"provider": "google_sheets"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert "GOOGLE_CLIENT_ID" in r.json()["error"]["message"]


async def test_google_sheets_authorization_url_is_real_google_oauth_when_configured(monkeypatch, client, tenant_a):
    from app.core import config as config_module
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config_module.settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    r = await client.post("/api/v1/crm/connect", headers=tenant_a, json={"provider": "google_sheets"})
    assert r.status_code == 200 and r.json()["connected"] is False
    url = r.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth") and "test-client-id" in url
    assert "spreadsheets.readonly" in url


async def test_custom_api_sync_uses_saved_config(client, tenant_a, monkeypatch):
    import httpx
    import socket
    from app.providers.crm import google_sheets as gs_module

    captured = {}
    real_get = httpx.AsyncClient.get

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"items": [{"id": "1", "name": "Vendor Lead", "phone": "9999900001", "email": "", "company": "", "status": "New"}]}

    async def fake_get(self, url, params=None, headers=None):
        if "vendor.example.com" not in str(url):
            return await real_get(self, url, params=params, headers=headers)
        captured["url"] = url
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    # Keep the SSRF guard active while making DNS deterministic for the HTTP mock.
    real_getaddrinfo = socket.getaddrinfo
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "vendor.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]
        return real_getaddrinfo(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    await client.post("/api/v1/crm/connect", headers=tenant_a, json={
        "provider": "custom_api", "config": {"api_key": "secret-token", "base_url": "https://vendor.example.com/api"}})
    cfg = {"base_url": "https://vendor.example.com/api", "list_path": "/v1/leads", "auth_header": "X-Api-Key",
           "field_map": {"id": "id", "name": "name", "phone": "phone", "email": "email", "company": "company", "status": "status"}}
    r = await client.put("/api/v1/crm/custom_api/config", headers=tenant_a, json=cfg)
    assert r.status_code == 200

    job = await client.post("/api/v1/crm/custom_api/sync?initial=true", headers=tenant_a)
    assert job.status_code == 200, job.text
    assert job.json()["status"] == "succeeded" and job.json()["records_created"] == 1
    assert captured["url"] == "https://vendor.example.com/api/v1/leads"
    leads = await client.get("/api/v1/leads", headers=tenant_a)
    assert leads.json()["total"] == 1 and leads.json()["items"][0]["name"] == "Vendor Lead"


async def test_custom_api_config_requires_existing_connection(client, tenant_a):
    r = await client.put("/api/v1/crm/custom_api/config", headers=tenant_a, json={"base_url": "https://x"})
    assert r.status_code == 404
