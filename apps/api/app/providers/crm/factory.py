from app.models.enums import CRMProviderType
from app.providers.crm.base import CRMProvider
from app.providers.crm.google_sheets import CustomAPICRMProvider, GoogleSheetsCRMProvider
from app.providers.crm.hubspot import HubSpotCRMProvider
from app.providers.crm.mock import MockCRMProvider
from app.providers.crm.pipedrive import PipedriveCRMProvider
from app.providers.crm.salesforce import SalesforceCRMProvider
from app.providers.crm.zoho import ZohoCRMProvider

_REGISTRY: dict[str, type[CRMProvider]] = {
    CRMProviderType.MOCK.value: MockCRMProvider,
    CRMProviderType.HUBSPOT.value: HubSpotCRMProvider,
    CRMProviderType.SALESFORCE.value: SalesforceCRMProvider,
    CRMProviderType.ZOHO.value: ZohoCRMProvider,
    CRMProviderType.PIPEDRIVE.value: PipedriveCRMProvider,
    CRMProviderType.GOOGLE_SHEETS.value: GoogleSheetsCRMProvider,
    CRMProviderType.CUSTOM_API.value: CustomAPICRMProvider,
}


def get_crm_provider(provider_name: str) -> CRMProvider:
    cls = _REGISTRY.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown CRM provider: {provider_name}")
    return cls()
