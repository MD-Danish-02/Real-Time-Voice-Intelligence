"""Fail-closed provider selection guards for the isolated pytest environment."""
from contextlib import contextmanager
from contextvars import ContextVar
import os


_mocked_transport_providers: ContextVar[frozenset[str]] = ContextVar(
    "mocked_transport_providers", default=frozenset()
)
_MOCK_TRANSPORT_UNIT_PROVIDERS = frozenset({
    "custom_api", "google_sheets", "groq_whisper", "plivo",
})


def is_test_safety_active() -> bool:
    return os.environ.get("ENV", "").strip().lower() == "test"


def require_mock_provider(provider_name: str, category: str) -> None:
    """Reject live/unknown provider construction or selection in test mode."""
    if not is_test_safety_active() or provider_name == "mock":
        return
    if provider_name not in _mocked_transport_providers.get():
        raise RuntimeError(
            f"{category} provider is disabled by test safety mode; select mock."
        )


@contextmanager
def allow_mocked_provider_transport(*provider_names: str):
    """Narrow opt-in for unit tests whose provider transport is fully mocked.

    This does not patch networking. Callers must replace the provider's
    transport with a deterministic fake before entering this context.
    """
    if (
        not is_test_safety_active()
        or not provider_names
        or any(not isinstance(name, str) or not name for name in provider_names)
        or not set(provider_names).issubset(_MOCK_TRANSPORT_UNIT_PROVIDERS)
    ):
        raise RuntimeError("Mocked provider transport override is unavailable.")
    token = _mocked_transport_providers.set(frozenset(provider_names))
    try:
        yield
    finally:
        _mocked_transport_providers.reset(token)
