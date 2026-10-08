"""Vobiz Media Streams WebSocket protocol serializer.

Handles converting between Pipecat frames and Vobiz's WebSocket media streams
protocol, including audio conversion (8000Hz G.711 μ-law), DTMF events,
call transfer delegation, and automatic call termination.
"""

import base64
import json
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy
from pipecat.utils.enums import EndTaskReason


class VobizFrameSerializer(FrameSerializer):
    """Serializer for Vobiz Media Streams WebSocket protocol.

    This serializer handles converting between Pipecat frames and Vobiz's WebSocket
    media streams protocol. It supports audio conversion, DTMF events, and automatic
    call termination or call transfer.
    """

    class InputParams(BaseModel):
        """Configuration parameters for VobizFrameSerializer."""

        vobiz_sample_rate: int = 8000
        sample_rate: int | None = None
        auto_hang_up: bool = True

    def __init__(
        self,
        stream_id: str,
        call_id: str | None = None,
        auth_id: str | None = None,
        auth_token: str | None = None,
        transfer_strategy: Optional[TransferStrategy] = None,
        hangup_strategy: Optional[HangupStrategy] = None,
        params: Optional[InputParams] = None,
    ):
        """Initialize the VobizFrameSerializer."""
        params = params or VobizFrameSerializer.InputParams()
        super().__init__(params)
        self._params: VobizFrameSerializer.InputParams = params

        # Validate hangup-related parameters if auto_hang_up is enabled
        if self._params.auto_hang_up:
            missing_credentials = []
            if not call_id:
                missing_credentials.append("call_id")
            if not auth_id:
                missing_credentials.append("auth_id")
            if not auth_token:
                missing_credentials.append("auth_token")

            if missing_credentials:
                raise ValueError(
                    f"auto_hang_up is enabled but missing required parameters: {', '.join(missing_credentials)}"
                )

        self._stream_id = stream_id
        self._call_id = call_id
        self._auth_id = auth_id
        self._auth_token = auth_token
        self._transfer_strategy = transfer_strategy
        self._hangup_strategy = hangup_strategy

        self._vobiz_sample_rate = self._params.vobiz_sample_rate
        self._sample_rate = 0  # Pipeline input rate

        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()
        self._hangup_attempted = False
        self._transfer_attempted = False

    async def setup(self, frame: StartFrame):
        """Sets up the serializer with pipeline configuration."""
        self._sample_rate = self._params.sample_rate or getattr(
            frame, "audio_in_sample_rate", getattr(frame, "sample_rate", 8000)
        )

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Serializes a Pipecat frame to Vobiz WebSocket format."""
        if isinstance(frame, (EndFrame, CancelFrame)):
            frame_reason = getattr(frame, "reason", None)
            context = {
                "call_id": self._call_id,
                "auth_id": self._auth_id,
                "auth_token": self._auth_token,
            }

            is_transfer = frame_reason in (
                EndTaskReason.TRANSFER_CALL.value,
                EndTaskReason.TRANSFER_CALL,
            )

            if is_transfer and not self._transfer_attempted:
                self._transfer_attempted = True
                if self._transfer_strategy:
                    success = await self._transfer_strategy.execute_transfer(context)
                    if not success:
                        logger.error(f"Transfer strategy failed for Vobiz call {self._call_id}")
                else:
                    logger.warning(
                        f"No transfer strategy configured for Vobiz call {self._call_id}"
                    )
                return None

            if self._params.auto_hang_up and not self._hangup_attempted:
                self._hangup_attempted = True
                if self._hangup_strategy:
                    success = await self._hangup_strategy.execute_hangup(context)
                    if not success:
                        logger.error(f"Hangup strategy failed for Vobiz call {self._call_id}")
                else:
                    await self._hang_up_call()
            return None
        elif isinstance(frame, InterruptionFrame):
            answer = {"event": "clearAudio", "streamId": self._stream_id}
            return json.dumps(answer)
        elif isinstance(frame, AudioRawFrame):
            data = frame.audio

            serialized_data = await pcm_to_ulaw(
                data, frame.sample_rate, self._vobiz_sample_rate, self._output_resampler
            )
            if serialized_data is None or len(serialized_data) == 0:
                return None

            payload = base64.b64encode(serialized_data).decode("utf-8")
            answer = {
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate": self._vobiz_sample_rate,
                    "payload": payload,
                },
                "streamId": self._stream_id,
            }
            return json.dumps(answer)
        elif isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)

        return None

    async def _hang_up_call(self):
        """Hang up the Vobiz call using Vobiz's REST API."""
        try:
            import aiohttp

            auth_id = self._auth_id
            auth_token = self._auth_token
            call_id = self._call_id

            if not call_id or not auth_id or not auth_token:
                logger.warning("Cannot hang up Vobiz call: missing required parameters")
                return

            endpoint = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_id}/"
            headers = {
                "X-Auth-ID": auth_id,
                "X-Auth-Token": auth_token,
            }

            async with aiohttp.ClientSession() as session:
                async with session.delete(endpoint, headers=headers) as response:
                    if response.status == 204:
                        logger.info(f"Successfully terminated Vobiz call {call_id}")
                    elif response.status == 404:
                        logger.debug(f"Vobiz call {call_id} already terminated")
                    else:
                        error_text = await response.text()
                        logger.error(f"Vobiz call termination failed: {response.status} {error_text}")

        except Exception as e:
            logger.error(f"Exception during Vobiz call termination: {e}")

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserializes Vobiz WebSocket data to Pipecat frames."""
        try:
            message = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            preview = data[:100] if isinstance(data, (str, bytes)) else str(type(data))
            logger.warning(f"Failed to parse Vobiz JSON message: {preview}")
            return None

        event = message.get("event")

        if event == "media":
            media = message.get("media", {})
            payload_base64 = media.get("payload")
            if not payload_base64:
                return None

            try:
                payload = base64.b64decode(payload_base64)
            except Exception as e:
                logger.error(f"Failed to base64-decode Vobiz media payload: {e}")
                return None

            deserialized_data = await ulaw_to_pcm(
                payload, self._vobiz_sample_rate, self._sample_rate, self._input_resampler
            )
            if deserialized_data is None or len(deserialized_data) == 0:
                return None

            audio_frame = InputAudioRawFrame(
                audio=deserialized_data, num_channels=1, sample_rate=self._sample_rate
            )
            return audio_frame
        elif event == "dtmf":
            digit = message.get("dtmf", {}).get("digit")
            if not digit:
                return None

            try:
                return InputDTMFFrame(KeypadEntry(digit))
            except ValueError:
                return None
        else:
            return None


__all__ = ["VobizFrameSerializer"]
