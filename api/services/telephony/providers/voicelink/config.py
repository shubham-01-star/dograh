"""VoiceLink telephony configuration schemas."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class VoiceLinkConfigurationRequest(BaseModel):
    """Request schema for VoiceLink configuration."""

    provider: Literal["voicelink"] = Field(default="voicelink")
    client_id: str = Field(..., description="VoiceLink Client / Account ID")
    auth_token: str = Field(..., description="VoiceLink API Key or Auth Token")
    api_base_url: str = Field(
        default="https://app.voicelink.co.in",
        description="VoiceLink API Base URL",
    )
    bot_id: Optional[str] = Field(
        default=None,
        description="VoiceLink WebSocket Bot ID configured in VoiceLink portal",
    )
    from_numbers: List[str] = Field(
        default_factory=list,
        description="List of VoiceLink phone numbers / DIDs",
    )


class VoiceLinkConfigurationResponse(BaseModel):
    """Response schema for VoiceLink configuration with masked sensitive fields."""

    provider: Literal["voicelink"] = Field(default="voicelink")
    client_id: str  # Masked by UI metadata sensitive flag
    auth_token: str  # Masked
    api_base_url: str
    bot_id: Optional[str] = None
    from_numbers: List[str]
