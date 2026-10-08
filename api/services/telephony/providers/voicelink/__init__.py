"""VoiceLink telephony provider package."""

from typing import Any, Dict

from api.services.telephony.registry import (
    ProviderSpec,
    ProviderUIField,
    ProviderUIMetadata,
    register,
)

from .config import VoiceLinkConfigurationRequest, VoiceLinkConfigurationResponse
from .provider import VoiceLinkProvider
from .transport import create_transport


def _config_loader(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider": "voicelink",
        "client_id": value.get("client_id"),
        "auth_token": value.get("auth_token"),
        "api_base_url": value.get("api_base_url") or "https://app.voicelink.co.in",
        "bot_id": value.get("bot_id"),
        "from_numbers": value.get("from_numbers", []),
        "default_from_number": value.get("default_from_number"),
    }


_UI_METADATA = ProviderUIMetadata(
    display_name="VoiceLink",
    docs_url="https://app.voicelink.co.in/documentation",
    fields=[
        ProviderUIField(
            name="client_id",
            label="Client ID / Account ID",
            type="text",
            sensitive=True,
            description="VoiceLink Client or Account ID",
        ),
        ProviderUIField(
            name="auth_token",
            label="API Key / Auth Token",
            type="password",
            sensitive=True,
            description="VoiceLink API Key or Bearer Token",
        ),
        ProviderUIField(
            name="api_base_url",
            label="API Base URL",
            type="text",
            required=False,
            description="Default: https://app.voicelink.co.in",
        ),
        ProviderUIField(
            name="bot_id",
            label="WebSocket Bot ID",
            type="text",
            required=False,
            description="Optional VoiceLink WebSocket Bot ID",
        ),
        ProviderUIField(
            name="from_numbers",
            label="Phone Numbers",
            type="string-array",
            description="Configured VoiceLink numbers (140/160/mobile DIDs)",
        ),
    ],
)


SPEC = ProviderSpec(
    name="voicelink",
    provider_cls=VoiceLinkProvider,
    config_loader=_config_loader,
    transport_factory=create_transport,
    transport_sample_rate=8000,
    config_request_cls=VoiceLinkConfigurationRequest,
    config_response_cls=VoiceLinkConfigurationResponse,
    ui_metadata=_UI_METADATA,
    account_id_credential_field="client_id",
)

register(SPEC)

__all__ = [
    "SPEC",
    "VoiceLinkConfigurationRequest",
    "VoiceLinkConfigurationResponse",
    "VoiceLinkProvider",
    "create_transport",
]
