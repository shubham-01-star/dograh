"""Tests for Rumik TTS integration into Dograh.

Covers:
- RumikTTSConfiguration model (defaults, custom values, JSON schema)
- Service factory Rumik branch
- API key validation
- Pipeline integration (mocked)
- Error handling
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import ValidationError

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    REGISTRY,
    RUMIK_TTS_MODELS,
    RumikTTSConfiguration,
    ServiceProviders,
    ServiceType,
)
from api.services.pipecat.rumik_tts import RumikTTSService
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame


# ---------------------------------------------------------------------------
# 1. RumikTTSConfiguration model tests
# ---------------------------------------------------------------------------


class TestRumikTTSConfiguration:
    def test_defaults(self):
        cfg = RumikTTSConfiguration(api_key="test-key")
        assert cfg.provider == ServiceProviders.RUMIK
        assert cfg.model == "muga"
        assert cfg.voice == "[neutral]"
        assert cfg.description is None
        assert cfg.speaker is None
        assert cfg.f0_up_key == 0
        assert cfg.temperature == 0.6
        assert cfg.top_p == 0.95
        assert cfg.top_k == 50
        assert cfg.repetition_penalty == 1.2
        assert cfg.max_new_tokens == 2048

    def test_custom_values(self):
        cfg = RumikTTSConfiguration(
            api_key="k",
            model="mulberry",
            voice="speaker_2",
            description="warm, friendly voice",
            speaker="speaker_2",
            f0_up_key=5,
            temperature=0.8,
            top_p=0.9,
            top_k=40,
            repetition_penalty=1.0,
            max_new_tokens=1024,
        )
        assert cfg.model == "mulberry"
        assert cfg.voice == "speaker_2"
        assert cfg.description == "warm, friendly voice"
        assert cfg.speaker == "speaker_2"
        assert cfg.f0_up_key == 5
        assert cfg.temperature == 0.8
        assert cfg.top_p == 0.9
        assert cfg.top_k == 40
        assert cfg.repetition_penalty == 1.0
        assert cfg.max_new_tokens == 1024

    def test_json_schema_has_model_examples(self):
        schema = RumikTTSConfiguration.model_json_schema()
        model_field = schema["properties"]["model"]
        assert model_field["examples"] == RUMIK_TTS_MODELS

    def test_registered_in_tts_registry(self):
        assert ServiceProviders.RUMIK in REGISTRY[ServiceType.TTS]
        assert REGISTRY[ServiceType.TTS][ServiceProviders.RUMIK] is RumikTTSConfiguration

    def test_api_key_required(self):
        with pytest.raises(ValidationError):
            RumikTTSConfiguration()


# ---------------------------------------------------------------------------
# 2. Service factory tests
# ---------------------------------------------------------------------------


class TestServiceFactoryRumik:
    def test_create_tts_service_rumik(self):
        import sys

        # Mock missing modules (custom pipecat fork modules, etc.)
        dograh_modules = [
            "pipecat.services.dograh",
            "pipecat.services.dograh.llm",
            "pipecat.services.dograh.stt",
            "pipecat.services.dograh.tts",
            "pipecat.utils.text.xml_function_tag_filter",
        ]
        mocks = {}
        for mod in dograh_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()

        with patch.dict(sys.modules, mocks):
            import importlib

            if "api.services.pipecat.service_factory" in sys.modules:
                importlib.reload(sys.modules["api.services.pipecat.service_factory"])
            from api.services.pipecat.service_factory import create_tts_service

            user_config = SimpleNamespace(
                tts=SimpleNamespace(
                    provider=ServiceProviders.RUMIK.value,
                    api_key="test-api-key",
                    model="muga",
                    voice="[neutral]",
                    description=None,
                    speaker=None,
                    f0_up_key=0,
                    temperature=0.6,
                    top_p=0.95,
                    top_k=50,
                    repetition_penalty=1.2,
                    max_new_tokens=2048,
                )
            )
            audio_config = SimpleNamespace(
                transport_out_sample_rate=24000,
                transport_in_sample_rate=16000,
            )

            tts = create_tts_service(user_config, audio_config)
            assert isinstance(tts, RumikTTSService)
            assert tts._api_key == "test-api-key"
            assert tts._settings.model == "muga"
            assert tts._settings.voice == "[neutral]"


# ---------------------------------------------------------------------------
# 3. API key validation tests
# ---------------------------------------------------------------------------


class TestRumikAPIKeyValidation:
    def test_rumik_validator_returns_true(self):
        validator = UserConfigurationValidator()
        assert validator._check_rumik_api_key("muga", "any-key") is True

    def test_rumik_in_validator_map(self):
        validator = UserConfigurationValidator()
        assert ServiceProviders.RUMIK.value in validator._validator_map

    def test_check_api_key_delegates_to_rumik(self):
        validator = UserConfigurationValidator()
        assert validator._check_api_key(ServiceProviders.RUMIK.value, "test-key") is True


# ---------------------------------------------------------------------------
# 4. Pipeline integration tests (mocking aiohttp session and websocket)
# ---------------------------------------------------------------------------


class TestRumikPipelineIntegration:
    @pytest.mark.asyncio
    async def test_run_tts_success(self):
        # Setup mocks
        mock_ws = AsyncMock()

        # Prepare mock binary messages and text done message
        msg_binary = MagicMock()
        msg_binary.type = aiohttp.WSMsgType.BINARY
        msg_binary.data = b"audio_chunk"

        msg_text = MagicMock()
        msg_text.type = aiohttp.WSMsgType.TEXT
        msg_text.data = '{"type": "done"}'

        # Make the async for loop over ws yield binary, then text
        mock_ws.__aiter__.return_value = [msg_binary, msg_text]

        mock_session = MagicMock()

        # Mock ws_connect context manager
        mock_session.ws_connect.return_value.__aenter__.return_value = mock_ws

        # Mock post context manager for ws-connect
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"ws_url": "wss://test-ws", "token": "test-token"}
        mock_session.post.return_value.__aenter__.return_value = mock_response

        # Instantiate service
        service = RumikTTSService(api_key="rk_test", aiohttp_session=mock_session)
        service._settings.voice = "[happy]"

        # Set metrics mock to avoid real tracking side-effects
        service.start_ttfb_metrics = AsyncMock()
        service.stop_ttfb_metrics = AsyncMock()
        service.start_tts_usage_metrics = AsyncMock()

        # Run
        frames = []
        async for frame in service.run_tts("Hello world", "context_1"):
            if frame:
                frames.append(frame)

        # Asserts
        assert len(frames) == 1
        assert isinstance(frames[0], TTSAudioRawFrame)
        assert frames[0].audio == b"audio_chunk"
        assert frames[0].sample_rate == 24000
        assert frames[0].num_channels == 1

        # Verify request parameters
        mock_session.post.assert_called_once_with(
            "https://silk-api.rumik.ai/v1/tts/ws-connect",
            headers={"Authorization": "Bearer rk_test", "Content-Type": "application/json"},
            json={"model": "muga", "text": "Hello world"},
        )
        # Verify we prepended tone tag because we chose [happy] tone and model is muga
        mock_ws.send_str.assert_called_once()
        sent_payload = json.loads(mock_ws.send_str.call_args[0][0])
        assert sent_payload["text"] == "[happy] Hello world"

    @pytest.mark.asyncio
    async def test_run_tts_post_error(self):
        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text.return_value = "Unauthorized API Key"
        mock_session.post.return_value.__aenter__.return_value = mock_response

        service = RumikTTSService(api_key="bad_key", aiohttp_session=mock_session)
        service.start_ttfb_metrics = AsyncMock()
        service.stop_ttfb_metrics = AsyncMock()

        frames = []
        async for frame in service.run_tts("Hello", "context_1"):
            if frame:
                frames.append(frame)

        assert len(frames) == 1
        assert isinstance(frames[0], ErrorFrame)
        assert "HTTP 401: Unauthorized API Key" in frames[0].error
