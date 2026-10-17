from tests.conftest import make_tenant


async def test_signup_login_me_refresh(client):
    headers, email = await make_tenant(client)
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["email"] == email and me.json()["role"] == "owner"
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "Sup3rSecret!"})
    assert r.status_code == 200
    assert "refresh_token" not in r.json()
    ref = client.cookies.get("voxai_refresh")
    assert ref
    r2 = await client.post("/api/v1/auth/refresh")
    assert r2.status_code == 200
    r3 = await client.post("/api/v1/auth/refresh", json={"refresh_token": ref})  # rotated: reuse must fail
    assert r3.status_code == 401


async def test_bad_login_and_duplicate_signup(client):
    _, email = await make_tenant(client)
    assert (await client.post("/api/v1/auth/login", json={"email": email, "password": "wrongwrong"})).status_code == 401
    dup = await client.post("/api/v1/auth/signup", json={"company_name": "X Co", "full_name": "Y Y", "email": email, "password": "Sup3rSecret!"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_requires_auth_and_refresh_token_not_accepted_as_access(client):
    assert (await client.get("/api/v1/leads")).status_code == 401
    _, email = await make_tenant(client)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "Sup3rSecret!"})
    bad = {"Authorization": f"Bearer {client.cookies.get('voxai_refresh')}"}
    assert (await client.get("/api/v1/leads", headers=bad)).status_code == 401


async def test_rbac_viewer_cannot_admin(client, tenant_a):
    r = await client.post("/api/v1/users", headers=tenant_a, json={
        "email": "viewer@example.com", "full_name": "View Er", "password": "Sup3rSecret!", "role": "agent_viewer"})
    assert r.status_code == 200
    login = await client.post("/api/v1/auth/login", json={"email": "viewer@example.com", "password": "Sup3rSecret!"})
    vh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/leads", headers=vh)).status_code == 200
    assert (await client.put("/api/v1/tenants/business-rules", headers=vh, json={"max_attempts": 9})).status_code == 403
    assert (await client.post("/api/v1/crm/connect", headers=vh, json={"provider": "mock"})).status_code == 403
    assert (await client.post("/api/v1/users", headers=vh, json={
        "email": "z@example.com", "full_name": "Z Z", "password": "Sup3rSecret!"})).status_code == 403


async def test_error_format_has_request_id(client):
    r = await client.get("/api/v1/leads")
    assert r.json()["error"]["code"] == "MISSING_TOKEN" and r.json()["error"]["request_id"]


async def test_signup_refresh_token_is_usable(client):
    r = await client.post("/api/v1/auth/signup", json={"company_name": "Fresh Co", "full_name": "Fre Sh", "email": "fresh@example.com", "password": "Sup3rSecret!"})
    assert r.status_code == 200 and "refresh_token" not in r.json()
    assert (await client.post("/api/v1/auth/refresh")).status_code == 200


async def test_logout_revokes_refresh_cookie(client):
    r = await client.post("/api/v1/auth/signup", json={"company_name": "Logout Co", "full_name": "Log Out", "email": "logout@example.com", "password": "Sup3rSecret!"})
    assert r.status_code == 200
    refresh_cookie = client.cookies.get("voxai_refresh")
    assert refresh_cookie
    logged_out = await client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 204
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_cookie})
    assert replay.status_code == 401


async def test_auth_endpoints_are_rate_limited(client):
    codes = [(await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrongwrong"})).status_code for _ in range(13)]
    assert 429 in codes and codes[0] == 401
