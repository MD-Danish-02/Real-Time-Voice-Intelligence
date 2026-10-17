from urllib.parse import parse_qs, urlparse

from tests.conftest import make_tenant


async def test_password_reset_is_single_use_revokes_sessions_and_does_not_enumerate(client, monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.auth.send_password_reset_email", lambda email, url: sent.append((email, url)))
    headers, email = await make_tenant(client)
    old_refresh = client.cookies.get("voxai_refresh")

    known = await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    unknown = await client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json()["message"] == unknown.json()["message"]
    assert known.json()["delivery_mode"] == "development_log"
    assert len(sent) == 1 and sent[0][0] == email

    token = parse_qs(urlparse(sent[0][1]).fragment)["token"][0]
    reset = await client.post("/api/v1/auth/password-reset/confirm", json={
        "token": token, "new_password": "NewPassword123!",
    })
    assert reset.status_code == 200
    refresh_reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_reuse.status_code == 401
    old_login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Sup3rSecret!"})
    new_login = await client.post("/api/v1/auth/login", json={"email": email, "password": "NewPassword123!"})
    assert old_login.status_code == 401
    assert new_login.status_code == 200

    replay = await client.post("/api/v1/auth/password-reset/confirm", json={
        "token": token, "new_password": "AnotherPassword123!",
    })
    assert replay.status_code == 400


async def test_password_reset_rejects_invalid_token(client):
    response = await client.post("/api/v1/auth/password-reset/confirm", json={
        "token": "not-a-valid-token", "new_password": "NewPassword123!",
    })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_OR_EXPIRED_RESET_TOKEN"
