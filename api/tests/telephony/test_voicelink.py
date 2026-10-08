"""Unit tests for VoiceLink telephony provider integration."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from api.enums import TelephonyCallStatus, WorkflowRunMode
from api.services.telephony import registry
from api.services.telephony.providers.voicelink import (
    SPEC,
    VoiceLinkConfigurationRequest,
    VoiceLinkConfigurationResponse,
    VoiceLinkProvider,
    create_transport,
)
from api.services.telephony.providers.voicelink.serializers import (
    VoiceLinkFrameSerializer,
)


def test_voicelink_spec_registered():
    """Verify VoiceLink provider is registered in the telephony registry."""
    spec = registry.get("voicelink")
    assert spec is not None
    assert spec.name == "voicelink"
    assert spec.transport_sample_rate == 8000
    assert spec.provider_cls == VoiceLinkProvider
    assert spec.config_request_cls == VoiceLinkConfigurationRequest
    assert spec.config_response_cls == VoiceLinkConfigurationResponse
    assert spec.account_id_credential_field == "client_id"
    assert spec.ui_metadata is not None
    assert spec.ui_metadata.display_name == "VoiceLink"


def test_voicelink_config_loader():
    """Test config loader extracts fields correctly."""
    raw = {
        "client_id": "VL_12345",
        "auth_token": "token_abc123",
        "api_base_url": "https://app.voicelink.co.in",
        "bot_id": "bot_999",
        "from_numbers": ["+919876543210"],
        "default_from_number": "+919876543210",
    }
    loaded = SPEC.config_loader(raw)
    assert loaded["provider"] == "voicelink"
    assert loaded["client_id"] == "VL_12345"
    assert loaded["auth_token"] == "token_abc123"
    assert loaded["bot_id"] == "bot_999"
    assert loaded["from_numbers"] == ["+919876543210"]


def test_voicelink_validation():
    """Test VoiceLink configuration validation."""
    valid_provider = VoiceLinkProvider(
        {"client_id": "VL_123", "auth_token": "key_456", "from_numbers": ["+919876543210"]}
    )
    assert valid_provider.validate_config() is True

    invalid_provider = VoiceLinkProvider({"client_id": "", "auth_token": None})
    assert invalid_provider.validate_config() is False


def test_voicelink_can_handle_webhook():
    """Test inbound webhook detection."""
    # Matches on User-Agent
    assert (
        VoiceLinkProvider.can_handle_webhook({}, {"user-agent": "VoiceLink-Webhook-Agent/1.0"})
        is True
    )

    # Matches on VoiceLink payload format
    assert (
        VoiceLinkProvider.can_handle_webhook(
            {"callId": "123.456", "fromNumber": "+919876543210", "callStatus": "connected"},
            {},
        )
        is True
    )

    # Rejects unrelated webhook
    assert (
        VoiceLinkProvider.can_handle_webhook(
            {"SomeRandomKey": "value"}, {"user-agent": "Mozilla/5.0"}
        )
        is False
    )


def test_voicelink_parse_inbound_webhook():
    """Test normalization of inbound webhook data."""
    webhook_data = {
        "callId": "vl_call_789",
        "fromNumber": "09876543210",
        "toNumber": "01123456789",
        "direction": "inbound",
        "callStatus": "ringing",
        "client_id": "VL_ACC_1",
    }
    normalized = VoiceLinkProvider.parse_inbound_webhook(webhook_data)
    assert normalized.provider == WorkflowRunMode.VOICELINK.value
    assert normalized.call_id == "vl_call_789"
    assert normalized.from_number == "+919876543210"
    assert normalized.to_number == "+911123456789"
    assert normalized.account_id == "VL_ACC_1"
    assert normalized.direction == "inbound"


def test_voicelink_parse_status_callback():
    """Test parsing status callbacks from VoiceLink."""
    provider = VoiceLinkProvider({"client_id": "VL_123", "auth_token": "key_456"})

    # Answered event
    ans_data = {
        "callId": "call_123",
        "callStatus": "connected",
        "fromNumber": "+919876543210",
        "toNumber": "+911123456789",
        "duration": 15,
    }
    parsed = provider.parse_status_callback(ans_data)
    assert parsed["call_id"] == "call_123"
    assert parsed["status"] == TelephonyCallStatus.ANSWERED
    assert parsed["duration"] == 15

    # Completed event
    comp_data = {
        "callId": "call_123",
        "callStatus": "call.completed",
        "duration": 45,
    }
    parsed_comp = provider.parse_status_callback(comp_data)
    assert parsed_comp["status"] == TelephonyCallStatus.COMPLETED

    # Failed event
    fail_data = {"callId": "call_123", "callStatus": "failed"}
    parsed_fail = provider.parse_status_callback(fail_data)
    assert parsed_fail["status"] == TelephonyCallStatus.FAILED


@pytest.mark.asyncio
async def test_voicelink_initiate_call_success():
    """Test initiate_call dispatches to VoiceLink API endpoint."""
    provider = VoiceLinkProvider(
        {
            "client_id": "VL_123",
            "auth_token": "secret_key",
            "bot_id": "bot_test",
            "from_numbers": ["+919876543210"],
        }
    )

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text.return_value = json.dumps(
        {"status": "success", "call_id": "vl_api_call_555"}
    )

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value.__aenter__.return_value = mock_response
        with patch(
            "api.services.telephony.providers.voicelink.provider.get_backend_endpoints",
            return_value=("https://dograh.example.com", None),
        ):
            result = await provider.initiate_call(
                to_number="+919876543211",
                webhook_url="https://dograh.example.com/webhook",
                workflow_run_id=42,
            )

            assert result.call_id == "vl_api_call_555"
            assert result.status == "initiated"
            assert result.caller_number == "+919876543210"
            assert result.provider_metadata["client_id"] == "VL_123"


@pytest.mark.asyncio
async def test_voicelink_frame_serializer_connected_event():
    """Test VoiceLinkFrameSerializer handles connected handshake event without crashing."""
    serializer = VoiceLinkFrameSerializer(stream_sid="stream_test", call_sid="call_test")
    connected_msg = json.dumps({"event": "connected"})

    # The connected event should be safely acknowledged and return None
    frame = await serializer.deserialize(connected_msg)
    assert frame is None
