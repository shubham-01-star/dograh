"""VoiceLink transport factory.

VoiceLink uses real-time WebSocket protocol:
- 8kHz audio sample rate (PCMA / PCMU)
- Base64-encoded audio in JSON messages
"""

from fastapi import WebSocket
from loguru import logger
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.audio_mixer import build_audio_out_mixer
from api.services.pipecat.transport_params import realtime_param_overrides
from api.services.telephony.factory import load_credentials_for_transport

from .serializers import VoiceLinkFrameSerializer


async def create_transport(
    websocket: WebSocket,
    workflow_run_id: int,
    audio_config: AudioConfig,
    organization_id: int,
    *,
    ambient_noise_config: dict | None = None,
    telephony_configuration_id: int | None = None,
    is_realtime: bool = False,
    stream_id: str,
    call_id: str,
):
    """Create a transport for VoiceLink connections."""
    logger.info(
        f"[run {workflow_run_id}] Creating VoiceLink transport - "
        f"stream_id={stream_id}, call_id={call_id}"
    )

    config = await load_credentials_for_transport(
        organization_id, telephony_configuration_id, expected_provider="voicelink"
    )

    client_id = config.get("client_id")
    auth_token = config.get("auth_token")

    if not client_id or not auth_token:
        raise ValueError(
            f"Incomplete VoiceLink configuration for organization {organization_id}"
        )

    serializer = VoiceLinkFrameSerializer(
        stream_sid=stream_id,
        call_sid=call_id,
        account_sid=client_id,
        auth_token=auth_token,
    )

    mixer = await build_audio_out_mixer(
        audio_config.transport_out_sample_rate, ambient_noise_config
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_config.transport_in_sample_rate,
            audio_out_sample_rate=audio_config.transport_out_sample_rate,
            audio_out_mixer=mixer,
            serializer=serializer,
            **realtime_param_overrides(is_realtime),
        ),
    )

    logger.info(f"[run {workflow_run_id}] VoiceLink transport created successfully")
    return transport
