import os
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote_from_bytes, unquote_to_bytes, urlsplit

_TEST_PROJECT = "voxai-riya-test"
_TEST_NETWORK = "voxai-riya-test_test_only"
_EXPECTED_CONTAINERS = {
    "postgres-test": "voxai-riya-test-postgres-test-1",
    "redis-test": "voxai-riya-test-redis-test-1",
}
_EXPECTED_PROJECT_VOLUMES = {
    "voxai-riya-test_postgres_test_data",
    "voxai-riya-test_redis_test_data",
}
_COMPOSE_CONTAINER_LABELS = {
    "com.docker.compose.config-hash",
    "com.docker.compose.container-number",
    "com.docker.compose.depends_on",
    "com.docker.compose.image",
    "com.docker.compose.oneoff",
    "com.docker.compose.project",
    "com.docker.compose.project.config_files",
    "com.docker.compose.project.working_dir",
    "com.docker.compose.service",
    "com.docker.compose.version",
}
_URL_SPECS = {
    "TEST_DATABASE_URL": ("postgresql+psycopg2", 55432, "/voxai_test"),
    "TEST_ASYNC_DATABASE_URL": ("postgresql+asyncpg", 55432, "/voxai_test"),
    "TEST_REDIS_URL": ("redis", 56379, "/0"),
    "TEST_CELERY_BROKER_URL": ("redis", 56379, "/0"),
    "TEST_CELERY_RESULT_BACKEND": ("redis", 56379, "/0"),
}
_SECRET_MOUNTS = {
    "VOXAI_TEST_POSTGRES_PASSWORD_FILE": ("postgres-test", "/run/secrets/test_postgres_password"),
    "VOXAI_TEST_REDIS_ACL_FILE": ("redis-test", "/run/secrets/test_redis_acl"),
    "VOXAI_TEST_REDIS_PASSWORD_FILE": ("redis-test", "/run/secrets/test_redis_password"),
}
_VOLUMES = {
    "postgres-test": ("voxai-riya-test_postgres_test_data", "postgres_test_data", "/var/lib/postgresql/data"),
    "redis-test": ("voxai-riya-test_redis_test_data", "redis_test_data", "/data"),
}
_EXPECTED_MOUNTS = {
    "postgres-test": {
        "/var/lib/postgresql/data": ("volume", "voxai-riya-test_postgres_test_data", True),
        "/run/secrets/test_postgres_password": ("bind", None, False),
    },
    "redis-test": {
        "/data": ("volume", "voxai-riya-test_redis_test_data", True),
        "/run/secrets/test_redis_acl": ("bind", None, False),
        "/run/secrets/test_redis_password": ("bind", None, False),
    },
}
_ACL_TOKENS = [
    "-@all", "~ratelimit:*", "~lead-call-lock:*", "&events:*",
    "+ping", "+flushdb", "+incr", "+expire", "+set", "+del", "+publish",
]


def _fail_preflight():
    raise RuntimeError(
        "Isolated test preflight failed; verify test-only endpoints, Docker "
        "identity, and secret configuration."
    ) from None


def _validated_url(name):
    value = os.environ.get(name)
    if not value or not value.isascii():
        _fail_preflight()
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        _fail_preflight()
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        _fail_preflight()

    try:
        parts = urlsplit(value)
        scheme, port, path = _URL_SPECS[name]
        username = "voxai_test"
        if parts.password is None:
            _fail_preflight()
        password_bytes = unquote_to_bytes(parts.password)
        password_text = password_bytes.decode("utf-8", errors="strict")
        if not password_bytes or any(
            ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F
            for ch in password_text
        ):
            _fail_preflight()

        # Require one canonical URL spelling. This rejects malformed or
        # alternate percent encodings, extra authority syntax, queries, and
        # fragments without exposing the credential.
        expected = (
            f"{scheme}://{username}:{quote_from_bytes(password_bytes, safe='')}"
            f"@127.0.0.1:{port}{path}"
        )
        if (
            value != expected
            or parts.scheme != scheme
            or parts.hostname != "127.0.0.1"
            or parts.port != port
            or parts.username != username
            or parts.path != path
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
        ):
            _fail_preflight()
        return password_bytes
    except (UnicodeError, ValueError):
        _fail_preflight()


def _docker_run(args):
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            _fail_preflight()
        return result.stdout
    except Exception:
        _fail_preflight()


def _docker_json(args):
    try:
        return json.loads(_docker_run(args))
    except Exception:
        _fail_preflight()


def _resolved_secret_path(value):
    try:
        path = Path(value)
        if not path.is_absolute():
            _fail_preflight()
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            _fail_preflight()
        return resolved
    except (OSError, RuntimeError, ValueError):
        _fail_preflight()


def _verify_secret_mount(container, target, configured_path):
    expected = _resolved_secret_path(configured_path)
    matches = [
        mount for mount in container.get("Mounts", [])
        if mount.get("Destination") == target
    ]
    if (
        len(matches) != 1
        or matches[0].get("Type") != "bind"
        or matches[0].get("RW") is not False
    ):
        _fail_preflight()
    try:
        # Do not guess at Docker Desktop path translations. If the daemon's
        # reported source cannot be resolved to the configured host path,
        # reject the preflight.
        actual = Path(matches[0].get("Source", "")).resolve(strict=True)
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
            _fail_preflight()
        return expected.read_bytes()
    except (OSError, RuntimeError, ValueError):
        _fail_preflight()


def _verify_acl(acl_bytes, redis_password):
    try:
        lines = [
            line.split()
            for line in acl_bytes.decode("ascii", errors="strict").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        expected_user = [
            "user", "voxai_test", "on",
            "#" + hashlib.sha256(redis_password).hexdigest(),
            *_ACL_TOKENS,
        ]
        if lines != [["user", "default", "off"], expected_user]:
            _fail_preflight()
    except (UnicodeError, ValueError):
        _fail_preflight()


def _verify_container(container, service):
    if service not in _EXPECTED_CONTAINERS:
        _fail_preflight()
    config = container.get("Config") or {}
    labels = config.get("Labels")
    state = container.get("State") or {}
    if (
        container.get("Name") != f"/{_EXPECTED_CONTAINERS[service]}"
        or not isinstance(labels, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items())
        or any(
            key not in _COMPOSE_CONTAINER_LABELS
            and not key.startswith("org.opencontainers.image.")
            for key in labels
        )
        or labels.get("com.docker.compose.project") != _TEST_PROJECT
        or labels.get("com.docker.compose.service") != service
        or labels.get("com.docker.compose.container-number") != "1"
        or labels.get("com.docker.compose.oneoff") != "False"
        or state.get("Running") is not True
        or (state.get("Health") or {}).get("Status") != "healthy"
    ):
        _fail_preflight()

    port, container_port = {
        "postgres-test": (55432, "5432/tcp"),
        "redis-test": (56379, "6379/tcp"),
    }[service]
    exposed_ports = config.get("ExposedPorts") or {}
    host_config = container.get("HostConfig") or {}
    host_bindings = host_config.get("PortBindings") or {}
    runtime_bindings = (container.get("NetworkSettings") or {}).get("Ports") or {}
    if (
        set(exposed_ports) != {container_port}
        or set(host_bindings) != {container_port}
        or set(runtime_bindings) != {container_port}
        or host_config.get("PublishAllPorts") is not False
        or host_config.get("NetworkMode") != _TEST_NETWORK
    ):
        _fail_preflight()
    expected_binding = {"HostIp": "127.0.0.1", "HostPort": str(port)}
    for binding_set in (host_bindings[container_port], runtime_bindings[container_port]):
        if not isinstance(binding_set, list) or len(binding_set) != 1:
            _fail_preflight()
        if any(binding_set[0].get(key) != value for key, value in expected_binding.items()):
            _fail_preflight()

    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    if set(networks) != {_TEST_NETWORK}:
        _fail_preflight()

    actual_mounts = container.get("Mounts")
    expected_mounts = _EXPECTED_MOUNTS[service]
    if not isinstance(actual_mounts, list):
        _fail_preflight()
    mounts_by_destination = {}
    for mount in actual_mounts:
        destination = mount.get("Destination")
        if not isinstance(destination, str) or destination in mounts_by_destination:
            _fail_preflight()
        mounts_by_destination[destination] = mount
    if set(mounts_by_destination) != set(expected_mounts):
        _fail_preflight()
    for destination, (expected_type, expected_name, expected_rw) in expected_mounts.items():
        mount = mounts_by_destination[destination]
        if (
            mount.get("Type") != expected_type
            or mount.get("RW") is not expected_rw
            or (expected_name is not None and mount.get("Name") != expected_name)
        ):
            _fail_preflight()

    volume_name, logical_name, destination = _VOLUMES[service]
    volumes = _docker_json(["volume", "inspect", volume_name])
    if not isinstance(volumes, list) or len(volumes) != 1:
        _fail_preflight()
    volume = volumes[0]
    volume_labels = volume.get("Labels") or {}
    if (
        volume.get("Name") != volume_name
        or volume_labels.get("com.docker.compose.project") != _TEST_PROJECT
        or volume_labels.get("com.docker.compose.volume") != logical_name
    ):
        _fail_preflight()


def _perform_isolated_preflight():
    passwords = {name: _validated_url(name) for name in _URL_SPECS}
    configured_paths = {}
    for name in _SECRET_MOUNTS:
        value = os.environ.get(name)
        if not value:
            _fail_preflight()
        configured_paths[name] = value

    ids = [
        item for item in _docker_run([
            "ps", "-aq", "--filter",
            f"label=com.docker.compose.project={_TEST_PROJECT}",
        ]).splitlines() if item
    ]
    if len(ids) != 2 or len(set(ids)) != 2:
        _fail_preflight()
    containers = _docker_json(["inspect", *ids])
    if not isinstance(containers, list) or len(containers) != 2:
        _fail_preflight()
    by_service = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if service in by_service:
            _fail_preflight()
        by_service[service] = container
    if set(by_service) != {"postgres-test", "redis-test"}:
        _fail_preflight()

    project_volumes = {
        item for item in _docker_run([
            "volume", "ls", "-q", "--filter",
            f"label=com.docker.compose.project={_TEST_PROJECT}",
        ]).splitlines() if item
    }
    if project_volumes != _EXPECTED_PROJECT_VOLUMES:
        _fail_preflight()

    project_networks = {
        item for item in _docker_run([
            "network", "ls", "--format", "{{.Name}}", "--filter",
            f"label=com.docker.compose.project={_TEST_PROJECT}",
        ]).splitlines() if item
    }
    if project_networks != {_TEST_NETWORK}:
        _fail_preflight()

    postgres = by_service["postgres-test"]
    redis_container = by_service["redis-test"]
    _verify_container(postgres, "postgres-test")
    _verify_container(redis_container, "redis-test")

    network_list = _docker_json(["network", "inspect", _TEST_NETWORK])
    if not isinstance(network_list, list) or len(network_list) != 1:
        _fail_preflight()
    network = network_list[0]
    network_labels = network.get("Labels") or {}
    if (
        network.get("Name") != _TEST_NETWORK
        or network.get("Internal") is not True
        or network_labels.get("com.docker.compose.project") != _TEST_PROJECT
        or network_labels.get("com.docker.compose.network") != "test_only"
    ):
        _fail_preflight()
    expected_container_ids = {container.get("Id") for container in containers}
    attached_container_ids = set((network.get("Containers") or {}))
    if (
        len(expected_container_ids) != 2
        or None in expected_container_ids
        or attached_container_ids != expected_container_ids
    ):
        _fail_preflight()

    pg_secret = _verify_secret_mount(
        postgres, "/run/secrets/test_postgres_password",
        configured_paths["VOXAI_TEST_POSTGRES_PASSWORD_FILE"],
    )
    acl_bytes = _verify_secret_mount(
        redis_container, "/run/secrets/test_redis_acl",
        configured_paths["VOXAI_TEST_REDIS_ACL_FILE"],
    )
    redis_secret = _verify_secret_mount(
        redis_container, "/run/secrets/test_redis_password",
        configured_paths["VOXAI_TEST_REDIS_PASSWORD_FILE"],
    )
    if (
        not pg_secret
        or pg_secret != passwords["TEST_DATABASE_URL"]
        or pg_secret != passwords["TEST_ASYNC_DATABASE_URL"]
        or not redis_secret
        or any(
            passwords[name] != redis_secret
            for name in (
                "TEST_REDIS_URL",
                "TEST_CELERY_BROKER_URL",
                "TEST_CELERY_RESULT_BACKEND",
            )
        )
    ):
        _fail_preflight()
    _verify_acl(acl_bytes, redis_secret)

    if redis_container.get("Config", {}).get("Cmd") != [
        "redis-server", "--appendonly", "yes",
        "--aclfile", "/run/secrets/test_redis_acl",
    ]:
        _fail_preflight()

    connection = None
    client = None
    try:
        import psycopg2

        pg_parts = urlsplit(os.environ["TEST_DATABASE_URL"])
        pg_password = passwords["TEST_DATABASE_URL"].decode("utf-8", errors="strict")
        connection = psycopg2.connect(
            host=pg_parts.hostname,
            port=pg_parts.port,
            dbname="voxai_test",
            user="voxai_test",
            password=pg_password,
            connect_timeout=5,
        )
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            if cursor.fetchone() != ("voxai_test", "voxai_test"):
                _fail_preflight()
    finally:
        if connection is not None:
            connection.close()

    try:
        import redis

        client = redis.Redis.from_url(
            os.environ["TEST_REDIS_URL"], socket_timeout=5,
        )
        # This confirms authentication and connectivity only, not the
        # Redis server's live in-memory ACL state.
        if client.ping() is not True:
            _fail_preflight()
    finally:
        if client is not None:
            client.close()


try:
    _perform_isolated_preflight()
except Exception:
    _fail_preflight()


os.environ.update({
    "ENV": "test",
    "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
    "ASYNC_DATABASE_URL": os.environ["TEST_ASYNC_DATABASE_URL"],
    "REDIS_URL": os.environ["TEST_REDIS_URL"],
    "TELEPHONY_PROVIDER_MODE": "mock",
    "CRM_PROVIDER_MODE": "mock",
    "CALENDAR_PROVIDER_MODE": "mock",
    "WHATSAPP_PROVIDER_MODE": "mock",
    "WHATSAPP_PROVIDER": "mock",
    "LLM_PROVIDER_MODE": "mock",
    "STT_PROVIDER_MODE": "mock",
    "TTS_PROVIDER_MODE": "mock",
    "TTS_PROVIDER_NAME": "mock",
    "KNOWLEDGE_EMBEDDING_MODE": "local",
    "ALLOW_DEMO_SEED": "true",
    "CELERY_BROKER_URL": os.environ["TEST_CELERY_BROKER_URL"],
    "CELERY_RESULT_BACKEND": os.environ["TEST_CELERY_RESULT_BACKEND"],
})
import uuid
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from app.core.config import settings

_sync = create_engine(settings.DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def migrated():
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean(migrated):
    with _sync.begin() as c:
        tables = [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version'"))]
        c.execute(text("TRUNCATE " + ",".join(tables) + " RESTART IDENTITY CASCADE"))
    import redis
    redis.Redis.from_url(settings.REDIS_URL).flushdb()
    yield


@pytest_asyncio.fixture
async def client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def make_tenant(client, name="Acme"):
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/api/v1/auth/signup", json={"company_name": name, "full_name": "Owner", "email": email, "password": "Sup3rSecret!"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, email


@pytest_asyncio.fixture
async def tenant_a(client):
    return (await make_tenant(client, "Tenant A"))[0]


@pytest_asyncio.fixture
async def tenant_b(client):
    return (await make_tenant(client, "Tenant B"))[0]


async def open_hours(client, headers):
    r = await client.put("/api/v1/tenants/business-rules", headers=headers, json={
        "calling_hours_start": "00:00:00", "calling_hours_end": "23:59:59", "calling_days_mask": 127,
        "auto_call_enabled": True})
    assert r.status_code == 200, r.text


async def synced_leads(client, headers):
    # Keep tests that exercise unrelated lead behavior from auto-queueing calls
    # as a side effect of sync. Dedicated automation tests opt in explicitly.
    await client.put("/api/v1/tenants/business-rules", headers=headers,
                     json={"auto_call_enabled": False})

    class FakeRedis:
        async def set(self, *args, **kwargs):
            return True

        async def delete(self, *args, **kwargs):
            return 1

        async def publish(self, *args, **kwargs):
            return 1

    from unittest.mock import patch
    with patch("app.workers.tasks.run_crm_sync.delay", return_value=None), \
            patch("app.services.eligibility_service._get_redis", return_value=FakeRedis()), \
            patch("app.events.bus._r", return_value=FakeRedis()):
        r = await client.post("/api/v1/crm/connect", headers=headers, json={"provider": "mock"})
        assert r.status_code == 200, r.text
        r = await client.post("/api/v1/crm/mock/sync?initial=true", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()
