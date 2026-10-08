"""VoiceLink frame serializer.

VoiceLink WebSocket streaming protocol:
- Supports 8000Hz wire rate (PCMA / PCMU)
- Bidirectional base64 audio frames in {"event": "media", "media": {"payload": "..."}}
- Interruption/barge-in via {"event": "clear", "stream_sid": "..."}
- Turn completion markers via {"event": "mark", "mark": {"name": "..."}}
- Disconnect via {"event": "stop", ...}
"""

import json
from typing import Any, Optional

from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.serializers.twilio import TwilioFrameSerializer


class VoiceLinkFrameSerializer(TwilioFrameSerializer):
    """Frame serializer for VoiceLink WebSocket stream protocol.

    Inherits from TwilioFrameSerializer while normalizing VoiceLink's
    naming conventions (snake_case and camelCase compatibility).
    """

    def __init__(
        self,
        stream_sid: str,
        call_sid: Optional[str] = None,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        params: Optional[TwilioFrameSerializer.InputParams] = None,
        **kwargs: Any,
    ):
        if params is None:
            params = TwilioFrameSerializer.InputParams(
                auto_hang_up=bool(account_sid and auth_token)
            )
        super().__init__(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=account_sid,
            auth_token=auth_token,
            params=params,
            **kwargs,
        )
        self._stream_id = stream_sid
        self._call_id = call_sid

    async def deserialize(self, data: str | bytes) -> Optional[Frame]:
        """Deserialize incoming VoiceLink message.

        Gracefully handles VoiceLink's 'connected' event and normalizes
        'stream_sid'/'call_sid' keys before passing to TwilioFrameSerializer.
        """
        if isinstance(data, (str, bytes)):
            try:
                msg = (
                    json.loads(data)
                    if isinstance(data, str)
                    else json.loads(data.decode("utf-8"))
                )
                event = msg.get("event")
                if event == "connected":
                    logger.debug("[VoiceLink] WebSocket connected event acknowledged")
                    return None

                # Normalize snake_case stream_sid and call_sid if present
                if "stream_sid" in msg and "streamSid" not in msg:
                    msg["streamSid"] = msg["stream_sid"]
                if "start" in msg and isinstance(msg["start"], dict):
                    start_data = msg["start"]
                    if "stream_sid" in start_data and "streamSid" not in start_data:
                        start_data["streamSid"] = start_data["stream_sid"]
                    if "call_sid" in start_data and "callSid" not in start_data:
                        start_data["callSid"] = start_data["call_sid"]
                    if "account_sid" in start_data and "accountSid" not in start_data:
                        start_data["accountSid"] = start_data["account_sid"]
                    if "media_format" in start_data and "mediaFormat" not in start_data:
                        start_data["mediaFormat"] = start_data["media_format"]
                if "stop" in msg and isinstance(msg["stop"], dict):
                    stop_data = msg["stop"]
                    if "call_sid" in stop_data and "callSid" not in stop_data:
                        stop_data["callSid"] = stop_data["call_sid"]

                normalized_data = json.dumps(msg)
                return await super().deserialize(normalized_data)
            except Exception as e:
                logger.warning(f"[VoiceLink] Deserialization fallback error: {e}")

        return await super().deserialize(data)


__all__ = ["VoiceLinkFrameSerializer"]
