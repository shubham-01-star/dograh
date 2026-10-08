# VoiceLink

India-only TRAI-compliant cloud telephony provider. WebSocket framing is Twilio-compatible (base64 PCMA/PCMU at 8 kHz), so the serializer extends `TwilioFrameSerializer` with snake_case ↔ camelCase normalization.

## Provider-specific notes

### Authentication

Token-based. Every REST call carries:

```
Authorization: Bearer <auth_token>
X-Auth-Token: <auth_token>
X-Client-ID: <client_id>
```

Webhook signature verification uses `hmac.compare_digest(signature, auth_token)`. No HMAC digest — the token itself is the shared secret.

### Wire format

8 kHz, PCMA/PCMU. The `transport_sample_rate` is `8000`. Audio arrives as base64 in `{"event": "media", "media": {"payload": "..."}}` — identical to Twilio's stream protocol.

### Serializer

`VoiceLinkFrameSerializer` subclasses `TwilioFrameSerializer`. The only additions:

1. Swallows `{"event": "connected"}` (VoiceLink sends this before `start`; Twilio doesn't).
2. Normalizes `stream_sid` → `streamSid`, `call_sid` → `callSid`, `media_format` → `mediaFormat` in the `start` payload so the parent serializer finds them.

### WebSocket handshake

VoiceLink sends two events before media:

```
→ {"event": "connected"}            ← acknowledged, discarded
→ {"event": "start", "start": {...}}  ← stream_sid + call_sid extracted here
→ {"event": "media", "media": {...}}  ← audio frames begin
```

`provider.handle_websocket` reads the first message; if it's `connected`, it reads again for `start`. Both `stream_sid`/`streamSid` and `call_sid`/`callSid` are accepted.

### Phone numbers

India-specific. `parse_inbound_webhook` hardcodes `country_hint="IN"` for `normalize_telephony_address`. Input formats `09876543210`, `9876543210`, `+919876543210` all normalize to `+919876543210`.

### Status mapping

```python
{
    "call.created": INITIATED,    "initiated": INITIATED,
    "ringing":      RINGING,
    "connected":    ANSWERED,     "call.connected": ANSWERED,
    "answer":       ANSWERED,     "answered":       ANSWERED,
    "call.completed": COMPLETED,  "hangup": COMPLETED,  "completed": COMPLETED,
    "failed":       FAILED,
    "busy":         BUSY,
    "no_answer":    NO_ANSWER,    "noanswer": NO_ANSWER,
}
```

Unrecognized statuses fall through to `TelephonyCallStatus.from_raw()`.

### Outbound calls

REST `POST` to `{api_base_url}/api/v2/call/start`. The event callback URL is auto-constructed as:

```
{backend_endpoint}/api/v1/telephony/voicelink/events/{workflow_run_id}
```

A fallback `call_id` (`vl_{uuid4_hex[:16]}`) is generated eagerly; the API response's `call_id` / `callId` / `data.call_id` overwrites it if present.

### Routes

Two endpoints in `routes.py`, auto-mounted via `_mount_provider_routers`:

| Endpoint                                        | Purpose                        |
| ----------------------------------------------- | ------------------------------ |
| `POST /voicelink/events/{workflow_run_id}`       | Status callback with run ID    |
| `POST /voicelink/events`                         | Status callback, run from body |
| `POST /voicelink-event`                          | Generic catch-all webhook      |

### Inbound webhook detection

`can_handle_webhook` checks two signals:

1. `User-Agent` header contains `"voicelink"` (case-insensitive).
2. Body has `callId` **and** (`fromNumber` or `callStatus`).

### Config shape

```python
{
    "provider": "voicelink",
    "client_id": "VL_12345",          # ProviderSpec.account_id_credential_field
    "auth_token": "token_abc123",     # sensitive
    "api_base_url": "https://app.voicelink.co.in",  # default
    "bot_id": "bot_999",              # optional, for WS provisioning
    "from_numbers": ["+919876543210"],
    "default_from_number": "+919876543210",  # resolved by select_from_number()
}
```

### What's different from Vobiz

VoiceLink is closest to Vobiz in shape but:

- Uses `client_id` + `auth_token` (not `auth_id` + `auth_token`).
- No auto-created Application concept — `bot_id` is optional and pre-provisioned in the VoiceLink portal.
- Serializer inherits `TwilioFrameSerializer` directly (Vobiz has its own `VobizFrameSerializer` with custom `InputParams`).
- Status callbacks use a broader mapping (14 variants vs Vobiz's simpler set).

## Tests

```bash
pytest api/tests/telephony/test_voicelink.py -v
```

Covers: registration, config loading, validation, webhook detection, inbound parsing, status parsing, outbound initiation, and serializer connected-event handling.
