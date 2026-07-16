import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import ClassVar, Any

import aiohttp
from loguru import logger

from pipecat.frames.frames import Frame, ErrorFrame, TTSAudioRawFrame, StartFrame
from pipecat.services.settings import NOT_GIVEN, TTSSettings, _NotGiven
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts


@dataclass
class RumikTTSSettings(TTSSettings):
    """Runtime-updatable settings for Rumik TTS service."""

    description: str | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    speaker: str | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    f0_up_key: int | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    temperature: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    top_p: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    top_k: int | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    repetition_penalty: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    max_new_tokens: int | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)


class RumikTTSService(TTSService):
    """Rumik TTS service that streams synthesized audio chunk-by-chunk over a WebSocket.

    Since Rumik provides a low-latency API, we connect to its one-shot WebSockets
    to stream audio in real time.
    """

    Settings = RumikTTSSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str,
        aiohttp_session: aiohttp.ClientSession | None = None,
        settings: Settings | None = None,
        base_url: str = "https://silk-api.rumik.ai",
        **kwargs,
    ):
        default_settings = self.Settings(
            model="muga",
            voice=None,
            language=None,
            description=None,
            speaker=None,
            f0_up_key=0,
            temperature=0.6,
            top_p=0.95,
            top_k=50,
            repetition_penalty=1.2,
            max_new_tokens=2048,
        )

        if settings is not None:
            default_settings.apply_update(settings)

        super().__init__(
            sample_rate=24000,
            push_stop_frames=True,
            push_start_frame=True,
            settings=default_settings,
            **kwargs,
        )

        self._api_key = api_key
        self._session = aiohttp_session
        self._owns_session = aiohttp_session is None
        self._base_url = base_url

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def cleanup(self) -> None:
        await super().cleanup()
        if self._owns_session and self._session and getattr(self._session, "closed", False) is not True:
            await self._session.close()
            self._session = None

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        logger.debug(f"{self}: Generating TTS [{text}]")

        if self._session is None or getattr(self._session, "closed", False) is True:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        # 1. Prepare WebSocket connection session by posting to /v1/tts/ws-connect
        url = f"{self._base_url}/v1/tts/ws-connect"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        model = self._settings.model or "muga"

        payload = {
            "model": model,
            "text": text,
        }

        try:
            await self.start_ttfb_metrics()

            async with self._session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

                resp_data = await response.json()
                ws_url = resp_data.get("ws_url")
                token = resp_data.get("token")

            if not ws_url or not token:
                raise Exception("Missing ws_url or token in ws-connect response")

            ws_endpoint = f"{ws_url}?token={token}"

            # Steer the text and parameters based on models
            synthesized_text = text
            if model == "muga":
                tone = self._settings.voice
                if tone:
                    tone_clean = tone.strip("[]").lower()
                    if tone_clean in ["neutral", "happy", "sad", "excited", "angry", "whisper"]:
                        tag = f"[{tone_clean}]"
                        if not text.lstrip().startswith("["):
                            synthesized_text = f"{tag} {text}"

            ws_payload = {
                "text": synthesized_text,
            }

            if model == "mulberry":
                if self._settings.description:
                    ws_payload["description"] = self._settings.description

                voice_str = self._settings.voice
                if voice_str and voice_str.startswith("speaker_"):
                    ws_payload["speaker"] = voice_str
                elif self._settings.speaker:
                    ws_payload["speaker"] = self._settings.speaker

                if self._settings.f0_up_key is not None:
                    ws_payload["f0_up_key"] = self._settings.f0_up_key

            if self._settings.temperature is not None:
                ws_payload["temperature"] = self._settings.temperature
            if self._settings.top_p is not None:
                ws_payload["top_p"] = self._settings.top_p
            if self._settings.top_k is not None:
                ws_payload["top_k"] = self._settings.top_k
            if self._settings.repetition_penalty is not None:
                ws_payload["repetition_penalty"] = self._settings.repetition_penalty
            if self._settings.max_new_tokens is not None:
                ws_payload["max_new_tokens"] = self._settings.max_new_tokens

            await self.start_tts_usage_metrics(text)

            async with self._session.ws_connect(ws_endpoint) as ws:
                await ws.send_str(json.dumps(ws_payload))

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            audio=msg.data,
                            sample_rate=24000,
                            num_channels=1,
                            context_id=context_id,
                        )
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            msg_json = json.loads(msg.data)
                            if msg_json.get("type") == "done":
                                break
                            elif msg_json.get("error"):
                                yield ErrorFrame(error=f"Rumik WS error: {msg_json.get('error')}")
                                break
                        except Exception as e:
                            logger.error(f"Error parsing Rumik text message: {e}")
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        yield ErrorFrame(error="Rumik WS connection error")
                        break

        except Exception as e:
            yield ErrorFrame(error=f"Error during Rumik TTS: {str(e)}")
        finally:
            await self.stop_ttfb_metrics()
