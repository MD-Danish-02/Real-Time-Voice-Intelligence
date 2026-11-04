from app.providers.telephony.base import TelephonyProvider
from app.providers.telephony.exotel import ExotelTelephonyProvider
from app.providers.telephony.generic_sip import GenericSIPTelephonyProvider
from app.providers.telephony.mock import MockTelephonyProvider
from app.providers.telephony.plivo import PlivoTelephonyProvider
from app.providers.provider_safety import require_mock_provider

_REGISTRY: dict[str, type[TelephonyProvider]] = {
    "mock": MockTelephonyProvider,
    "exotel": ExotelTelephonyProvider,
    "plivo": PlivoTelephonyProvider,
    "generic_sip": GenericSIPTelephonyProvider,
}


def get_telephony_provider(provider_name: str) -> TelephonyProvider:
    require_mock_provider(provider_name, "Telephony")
    cls = _REGISTRY.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown telephony provider: {provider_name}")
    return cls()
