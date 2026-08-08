import asyncio
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
    persistent_session: bool | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    speaker: str | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    f0_up_key: int | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    temperature: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    top_p: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    top_k: int | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    repetition_penalty: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    max_new_tokens: int | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)



def _extract_rumik_error_message(msg_json: dict) -> str:
    """Extract a user-friendly error message from a Rumik WS or HTTP error response."""
    detail = (
        msg_json.get("message")
        or msg_json.get("detail")
        or msg_json.get("reason")
        or msg_json.get("error_message")
    )
    code = msg_json.get("code")

    if not detail or detail is True:
        raw_error = msg_json.get("error")
        if isinstance(raw_error, str):
            detail = raw_error
        else:
            detail = "Insufficient credits or account quota exceeded."

    detail_str = str(detail)

    if (
        code == "insufficient_credits"
        or "credit" in detail_str.lower()
        or "balance" in detail_str.lower()
        or "quota" in detail_str.lower()
        or "payment" in detail_str.lower()
    ):
        return f"Rumik TTS Error: Credits exhausted. Please top up your balance. ({detail_str})"

    return f"Rumik WS error: {detail_str}"


class RumikTTSService(TTSService):
    """Rumik TTS service that streams synthesized audio over a WebSocket.

    Supports two modes controlled by the `persistent_session` setting:
    - **Persistent** (default): Opens a single WebSocket at call start and reuses
      it for every utterance. Provides lower latency and native barge-in via the
      Rumik "latest-wins" protocol.
    - **One-shot**: Mints a new WebSocket session per utterance. Simpler but adds
      HTTP + WS handshake overhead on each sentence.
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
            persistent_session=True,
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
        self._persistent_ws = None
        self._receive_task = None
        self._current_queue = None
        self._persistent_ws_broken = False

    def can_generate_metrics(self) -> bool:
        return True

    async def _connect_persistent_ws(self):
        url = f"{self._base_url}/v1/tts/ws-connect"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        model = self._settings.model or "muga"
        payload = {"model": model, "text": "."}
        try:
            async with self._session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Failed to open persistent WS: HTTP {response.status}: {error_text}")
                    return
                resp_data = await response.json()
                ws_url = resp_data.get("ws_url")
                token = resp_data.get("token")
                
            if ws_url and token:
                ws_endpoint = f"{ws_url}?token={token}"
                self._persistent_ws = await self._session.ws_connect(ws_endpoint)
                self._receive_task = asyncio.create_task(self._receive_loop())
        except Exception as e:
            logger.error(f"Exception connecting persistent WS: {e}")

    async def _receive_loop(self):
        try:
            async for msg in self._persistent_ws:
                if self._current_queue is None:
                    logger.debug(f"Rumik WS message received while no queue active (type={msg.type})")
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._current_queue.put(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        msg_json = json.loads(msg.data)
                        if msg_json.get("type") in ("done", "cancelled"):
                            await self._current_queue.put(None)
                        elif msg_json.get("type") == "timeout":
                            logger.warning("Rumik persistent WS idle timeout")
                            self._persistent_ws_broken = True
                            await self._current_queue.put(None)
                        elif msg_json.get("error"):
                            logger.error(f"Rumik WS error (full response): {json.dumps(msg_json)}")
                            self._persistent_ws_broken = True
                            self._last_error = _extract_rumik_error_message(msg_json)
                            await self._current_queue.put(None)
                    except Exception as e:
                        logger.error(f"Error parsing Rumik text message: {e}")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("Rumik WS connection error in receive loop")
                    self._persistent_ws_broken = True
                    await self._current_queue.put(None)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Rumik receive loop error: {e}")
            self._persistent_ws_broken = True
            if self._current_queue:
                await self._current_queue.put(None)

    def _get_ws_payload(self, text: str, model: str) -> dict:
        synthesized_text = text
        if model == "muga":
            # If text doesn't already start with a tone tag, prepend one.
            # Use the voice setting as tone if set, otherwise default to [neutral].
            if not text.lstrip().startswith("["):
                tone = self._settings.voice
                if tone:
                    tone_clean = tone.strip("[]").lower()
                    if tone_clean in ["neutral", "happy", "sad", "excited", "angry", "whisper"]:
                        synthesized_text = f"[{tone_clean}] {text}"
                    else:
                        synthesized_text = f"[neutral] {text}"
                else:
                    synthesized_text = f"[neutral] {text}"

        ws_payload = {
            "text": synthesized_text,
        }

        if model == "mulberry":
            if self._settings.description:
                ws_payload["description"] = self._settings.description

            voice_str = self._settings.voice
            if voice_str and voice_str != "custom":
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
            
        return ws_payload

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
            
        if self._settings.persistent_session:
            await self._connect_persistent_ws()

    async def cleanup(self) -> None:
        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None
        if self._persistent_ws and not getattr(self._persistent_ws, "closed", True):
            await self._persistent_ws.close()
            self._persistent_ws = None
            
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

        model = self._settings.model or "muga"
        ws_payload = self._get_ws_payload(text, model)

        use_persistent = self._settings.persistent_session and not self._persistent_ws_broken

        if use_persistent:
            if self._persistent_ws is None or getattr(self._persistent_ws, "closed", True):
                self._persistent_ws_broken = False
                for attempt in range(3):
                    await self._connect_persistent_ws()
                    if self._persistent_ws and not getattr(self._persistent_ws, "closed", True):
                        break
                    wait = 0.5 * (2 ** attempt)
                    logger.warning(f"Rumik persistent WS reconnect attempt {attempt + 1}/3 failed, retrying in {wait}s")
                    await asyncio.sleep(wait)
                
            if self._persistent_ws and not getattr(self._persistent_ws, "closed", True):
                self._current_queue = asyncio.Queue()
                got_audio = False
                try:
                    await self.start_ttfb_metrics()
                    await self.start_tts_usage_metrics(text)
                    await self._persistent_ws.send_str(json.dumps(ws_payload))
                    
                    while True:
                        chunk = await self._current_queue.get()
                        if chunk is None:
                            break
                        got_audio = True
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            audio=chunk,
                            sample_rate=24000,
                            num_channels=1,
                            context_id=context_id,
                        )
                except asyncio.CancelledError:
                    if self._persistent_ws and not getattr(self._persistent_ws, "closed", True):
                        await self._persistent_ws.send_str(json.dumps({"type": "cancel"}))
                    raise
                except Exception as e:
                    logger.warning(f"Persistent WS error, falling back to one-shot: {e}")
                    self._persistent_ws_broken = True
                    use_persistent = False
                finally:
                    await self.stop_ttfb_metrics()
                    self._current_queue = None

                # If persistent WS returned an error (no audio chunks received),
                # surface the exact error message instead of retrying a broken request.
                if not got_audio and self._persistent_ws_broken:
                    err_msg = getattr(self, "_last_error", None) or "Rumik TTS Error: Credits exhausted or WS connection failed."
                    logger.warning(f"Persistent WS returned error without audio: {err_msg}")
                    yield ErrorFrame(error=err_msg)
                    return
            else:
                logger.warning("Persistent WS unavailable, falling back to one-shot mode")
                use_persistent = False

        if not use_persistent:
            # One-shot session mode (fallback or explicit)
            url = f"{self._base_url}/v1/tts/ws-connect"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            
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
                                    err_msg = _extract_rumik_error_message(msg_json)
                                    yield ErrorFrame(error=err_msg)
                                    break
                            except Exception as e:
                                logger.error(f"Error parsing Rumik text message: {e}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            yield ErrorFrame(error="Rumik WS connection error")
                            break
            except Exception as e:
                err_str = str(e)
                if any(k in err_str.lower() for k in ["credit", "balance", "quota", "402", "429"]):
                    yield ErrorFrame(error=f"Rumik TTS Error: Credits exhausted. Please top up your account balance ({err_str})")
                else:
                    yield ErrorFrame(error=f"Error during Rumik TTS: {err_str}")
            finally:
                await self.stop_ttfb_metrics()
