"""Tests for Vobiz call-transfer flow.

Covers:
- VobizProvider.transfer_call()   — initiates the outbound B-leg
- VobizProvider.supports_transfers()
- handle_vobiz_transfer_xml()     — conference XML for A-leg and B-leg
- handle_vobiz_transfer_result()  — hangup cause routing (USER_BUSY, NO_ANSWER, etc.)
- VobizConferenceStrategy         — redirects A-leg into conference
- VobizHangupStrategy             — terminates call via REST API
"""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.services.telephony.providers.vobiz.provider import VobizProvider
from api.services.telephony.providers.vobiz.routes import (
    handle_vobiz_transfer_result,
    handle_vobiz_transfer_xml,
)
from api.services.telephony.providers.vobiz.strategies import (
    VobizConferenceStrategy,
    VobizHangupStrategy,
)
from api.services.telephony.transfer_event_protocol import (
    TransferContext,
    TransferEvent,
    TransferEventType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider() -> VobizProvider:
    return VobizProvider(
        {
            "auth_id": "MA_TEST",
            "auth_token": "test-auth-token-secret",
            "application_id": "APP123",
            "from_numbers": ["+919876543210"],
        }
    )


def _transfer_context(
    *,
    transfer_id: str = "txfr-001",
    conference_name: str = "conf-abc",
    original_call_sid: str = "orig-call-uuid",
    workflow_run_id: int = 100,
) -> TransferContext:
    return TransferContext(
        transfer_id=transfer_id,
        call_sid=None,
        target_number="+919999000001",
        tool_uuid="tool-uuid-1",
        original_call_sid=original_call_sid,
        conference_name=conference_name,
        initiated_at=time.time(),
        workflow_run_id=workflow_run_id,
    )


class _StubResponse:
    def __init__(self, status: int, body: str = "", json_data: dict | None = None):
        self.status = status
        self._body = body
        self._json = json_data

    async def text(self) -> str:
        return self._body or json.dumps(self._json or {})

    async def json(self) -> dict:
        if self._json is not None:
            return self._json
        return json.loads(self._body) if self._body else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _StubSession:
    def __init__(self, responses: list[_StubResponse]):
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict | None]] = []

    def post(self, url: str, *, json: dict = None, headers: dict = None):
        self.requests.append(("POST", url, json))
        return self.responses.pop(0)

    def delete(self, url: str, *, headers: dict = None):
        self.requests.append(("DELETE", url, None))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _signed_headers(provider: VobizProvider, *, url: str) -> dict[str, str]:
    nonce = "12345678901234567890"
    base_url = url.split("?")[0]
    signature = base64.b64encode(
        hmac.new(
            provider.auth_token.encode("utf-8"),
            f"{base_url}.{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return {
        "x-vobiz-signature-v3": signature,
        "x-vobiz-signature-v3-nonce": nonce,
    }


def _request(
    *,
    path: str,
    form_data: dict[str, str],
    headers: dict[str, str] | None = None,
    query_string: str = "",
) -> Request:
    body = urlencode(form_data).encode("utf-8")
    request_headers = [
        (b"content-type", b"application/x-www-form-urlencoded"),
        *[
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in (headers or {}).items()
        ],
    ]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("example.test", 443),
            "path": path,
            "query_string": query_string.encode("utf-8"),
            "headers": request_headers,
        },
        receive,
    )


# ---------------------------------------------------------------------------
# VobizProvider.supports_transfers()
# ---------------------------------------------------------------------------


class TestSupportsTransfers:
    def test_supports_transfers_returns_true(self):
        provider = _provider()
        assert provider.supports_transfers() is True


# ---------------------------------------------------------------------------
# VobizProvider.transfer_call()
# ---------------------------------------------------------------------------


class TestTransferCall:
    @pytest.mark.asyncio
    async def test_transfer_call_dials_destination_with_correct_urls(self):
        """transfer_call() should POST to Vobiz Call API with answer_url pointing
        to the transfer-xml route and hangup_url pointing to transfer-result."""
        provider = _provider()
        api_response = _StubResponse(
            201, json_data={"call_uuid": "dest-call-uuid", "message": "call fired"}
        )
        session = _StubSession([api_response])

        with (
            patch(
                "api.services.telephony.providers.vobiz.provider.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "api.services.telephony.providers.vobiz.provider.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            result = await provider.transfer_call(
                destination="+919999000001",
                transfer_id="txfr-001",
                conference_name="conf-abc",
                timeout=25,
            )

        assert result["call_sid"] == "dest-call-uuid"
        assert result["status"] == "queued"
        assert result["provider"] == "vobiz"

        # Verify the API call
        method, url, body = session.requests[0]
        assert method == "POST"
        assert url == "https://api.vobiz.ai/api/v1/Account/MA_TEST/Call/"
        assert body["to"] == "919999000001"
        assert body["from"] == "919876543210"
        assert "transfer-xml/conf-abc/txfr-001" in body["answer_url"]
        assert "transfer-result/txfr-001" in body["hangup_url"]
        assert body["ring_timeout"] == 25

    @pytest.mark.asyncio
    async def test_transfer_call_raises_on_api_failure(self):
        """transfer_call() should raise when Vobiz API returns non-2xx."""
        provider = _provider()
        api_response = _StubResponse(500, body='{"error": "server error"}')
        session = _StubSession([api_response])

        with (
            patch(
                "api.services.telephony.providers.vobiz.provider.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "api.services.telephony.providers.vobiz.provider.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            with pytest.raises(Exception, match="Vobiz API call failed"):
                await provider.transfer_call(
                    destination="+919999000001",
                    transfer_id="txfr-002",
                    conference_name="conf-xyz",
                )

    @pytest.mark.asyncio
    async def test_transfer_call_raises_on_missing_call_uuid(self):
        """transfer_call() should raise when response lacks call_uuid."""
        provider = _provider()
        api_response = _StubResponse(
            201, json_data={"message": "call fired"}  # no call_uuid!
        )
        session = _StubSession([api_response])

        with (
            patch(
                "api.services.telephony.providers.vobiz.provider.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "api.services.telephony.providers.vobiz.provider.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            with pytest.raises(Exception, match="missing call identifier"):
                await provider.transfer_call(
                    destination="+919999000001",
                    transfer_id="txfr-003",
                    conference_name="conf-xyz",
                )

    @pytest.mark.asyncio
    async def test_transfer_call_raises_when_not_configured(self):
        """transfer_call() should raise ValueError when credentials are missing."""
        provider = VobizProvider({"auth_id": "", "auth_token": "", "from_numbers": []})

        with pytest.raises(ValueError, match="not properly configured"):
            await provider.transfer_call(
                destination="+919999000001",
                transfer_id="txfr-004",
                conference_name="conf-xyz",
            )


# ---------------------------------------------------------------------------
# handle_vobiz_transfer_xml — Destination leg (B-leg)
# ---------------------------------------------------------------------------


class TestTransferXmlRoute:
    @pytest.mark.asyncio
    async def test_transfer_xml_bleg_returns_conference_and_publishes_event(self):
        """B-leg (no ?leg=aleg) should return conference XML and publish
        DESTINATION_ANSWERED."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {"CallUUID": "dest-call-uuid", "CallStatus": "answered"}
        url = "https://example.test/api/v1/telephony/vobiz/transfer-xml/conf-abc/txfr-001"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-xml/conf-abc/txfr-001",
            form_data=form_data,
            headers=headers,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)
        mock_manager.claim_transfer_step = AsyncMock(return_value=True)
        mock_manager.store_transfer_context = AsyncMock()
        mock_manager.publish_transfer_event = AsyncMock()

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_xml(
                conference_name="conf-abc",
                transfer_id="txfr-001",
                request=request,
            )

        assert result.status_code == 200
        body = result.body.decode()
        assert "conf-abc" in body
        assert "<Conference" in body
        # B-leg should publish DESTINATION_ANSWERED
        mock_manager.publish_transfer_event.assert_awaited_once()
        event = mock_manager.publish_transfer_event.call_args[0][0]
        assert event.type == TransferEventType.DESTINATION_ANSWERED

    @pytest.mark.asyncio
    async def test_transfer_xml_aleg_returns_conference_without_publishing(self):
        """A-leg (?leg=aleg) should return conference XML without publishing."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {"CallUUID": "orig-call-uuid"}
        url = "https://example.test/api/v1/telephony/vobiz/transfer-xml/conf-abc/txfr-001?leg=aleg"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-xml/conf-abc/txfr-001",
            form_data=form_data,
            headers=headers,
            query_string="leg=aleg",
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_xml(
                conference_name="conf-abc",
                transfer_id="txfr-001",
                request=request,
            )

        body = result.body.decode()
        assert "conf-abc" in body
        assert 'endConferenceOnExit="true"' in body
        # A-leg should NOT publish events
        mock_manager.publish_transfer_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transfer_xml_hangup_on_missing_context(self):
        """Should return hangup XML when transfer context is not found."""
        provider = _provider()
        form_data = {"CallUUID": "some-call"}
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-xml/conf-abc/txfr-missing",
            form_data=form_data,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=None)

        with patch(
            "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
            new_callable=AsyncMock,
            return_value=mock_manager,
        ):
            result = await handle_vobiz_transfer_xml(
                conference_name="conf-abc",
                transfer_id="txfr-missing",
                request=request,
            )

        body = result.body.decode()
        assert "<Hangup/>" in body

    @pytest.mark.asyncio
    async def test_transfer_xml_hangup_on_conference_mismatch(self):
        """Should return hangup XML when conference name doesn't match."""
        provider = _provider()
        ctx = _transfer_context(conference_name="conf-real")
        form_data = {"CallUUID": "some-call"}
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-xml/conf-fake/txfr-001",
            form_data=form_data,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)

        with patch(
            "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
            new_callable=AsyncMock,
            return_value=mock_manager,
        ):
            result = await handle_vobiz_transfer_xml(
                conference_name="conf-fake",
                transfer_id="txfr-001",
                request=request,
            )

        body = result.body.decode()
        assert "<Hangup/>" in body


# ---------------------------------------------------------------------------
# handle_vobiz_transfer_result — hangup cause routing
# ---------------------------------------------------------------------------


class TestTransferResultRoute:
    @pytest.mark.asyncio
    async def test_user_busy_publishes_transfer_failed(self):
        """USER_BUSY hangup cause should publish TRANSFER_FAILED with reason=busy."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {
            "CallUUID": "dest-call-uuid",
            "Event": "Hangup",
            "HangupCause": "USER_BUSY",
        }
        url = "https://example.test/api/v1/telephony/vobiz/transfer-result/txfr-001"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-result/txfr-001",
            form_data=form_data,
            headers=headers,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)
        mock_manager.claim_transfer_step = AsyncMock(return_value=True)
        mock_manager.publish_transfer_event = AsyncMock()
        mock_manager.remove_transfer_context = AsyncMock()

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_result(
                transfer_id="txfr-001", request=request
            )

        assert result == {"status": "success"}
        event = mock_manager.publish_transfer_event.call_args[0][0]
        assert event.type == TransferEventType.TRANSFER_FAILED
        assert event.reason == "busy"
        mock_manager.remove_transfer_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_answer_publishes_transfer_failed(self):
        """NO_ANSWER hangup cause should publish TRANSFER_FAILED with reason=no_answer."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {
            "CallUUID": "dest-call-uuid",
            "Event": "Hangup",
            "HangupCause": "NO_ANSWER",
        }
        url = "https://example.test/api/v1/telephony/vobiz/transfer-result/txfr-001"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-result/txfr-001",
            form_data=form_data,
            headers=headers,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)
        mock_manager.claim_transfer_step = AsyncMock(return_value=True)
        mock_manager.publish_transfer_event = AsyncMock()
        mock_manager.remove_transfer_context = AsyncMock()

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_result(
                transfer_id="txfr-001", request=request
            )

        event = mock_manager.publish_transfer_event.call_args[0][0]
        assert event.reason == "no_answer"

    @pytest.mark.asyncio
    async def test_normal_clearing_cleans_up_without_publishing(self):
        """NORMAL_CLEARING (successful hangup after bridge) should clean up
        without publishing a failure event."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {
            "CallUUID": "dest-call-uuid",
            "Event": "Hangup",
            "HangupCause": "NORMAL_CLEARING",
        }
        url = "https://example.test/api/v1/telephony/vobiz/transfer-result/txfr-001"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-result/txfr-001",
            form_data=form_data,
            headers=headers,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)
        mock_manager.remove_transfer_context = AsyncMock()
        mock_manager.publish_transfer_event = AsyncMock()

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_result(
                transfer_id="txfr-001", request=request
            )

        assert result == {"status": "success"}
        mock_manager.remove_transfer_context.assert_awaited_once_with("txfr-001")
        mock_manager.publish_transfer_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_hangup_cause_publishes_call_failed(self):
        """An unknown hangup cause should publish TRANSFER_FAILED with reason=call_failed."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {
            "CallUUID": "dest-call-uuid",
            "Event": "Hangup",
            "HangupCause": "NETWORK_ERROR",
        }
        url = "https://example.test/api/v1/telephony/vobiz/transfer-result/txfr-001"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-result/txfr-001",
            form_data=form_data,
            headers=headers,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)
        mock_manager.claim_transfer_step = AsyncMock(return_value=True)
        mock_manager.publish_transfer_event = AsyncMock()
        mock_manager.remove_transfer_context = AsyncMock()

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_result(
                transfer_id="txfr-001", request=request
            )

        event = mock_manager.publish_transfer_event.call_args[0][0]
        assert event.reason == "call_failed"
        assert "NETWORK_ERROR" in event.message

    @pytest.mark.asyncio
    async def test_missing_transfer_context_returns_error(self):
        """Should return error when transfer_id is not found in Redis."""
        form_data = {"CallUUID": "dest-call-uuid", "HangupCause": "USER_BUSY"}
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-result/txfr-ghost",
            form_data=form_data,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=None)

        with patch(
            "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
            new_callable=AsyncMock,
            return_value=mock_manager,
        ):
            result = await handle_vobiz_transfer_result(
                transfer_id="txfr-ghost", request=request
            )

        assert result["status"] == "error"
        assert result["reason"] == "invalid_transfer_id"

    @pytest.mark.asyncio
    async def test_no_hangup_cause_returns_pending(self):
        """When hangup cause is missing, should return pending status."""
        provider = _provider()
        ctx = _transfer_context()
        form_data = {"CallUUID": "dest-call-uuid", "Event": "Ringing"}
        url = "https://example.test/api/v1/telephony/vobiz/transfer-result/txfr-001"
        headers = _signed_headers(provider, url=url)
        request = _request(
            path="/api/v1/telephony/vobiz/transfer-result/txfr-001",
            form_data=form_data,
            headers=headers,
        )

        mock_manager = AsyncMock()
        mock_manager.get_transfer_context = AsyncMock(return_value=ctx)

        with (
            patch(
                "api.services.telephony.providers.vobiz.routes.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch("api.services.telephony.providers.vobiz.routes.db_client") as db_client,
            patch(
                "api.services.telephony.providers.vobiz.routes.get_telephony_provider_for_run",
                new_callable=AsyncMock,
                return_value=provider,
            ),
            patch(
                "api.services.telephony.providers.vobiz.routes.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            db_client.get_workflow_run_by_id = AsyncMock(
                return_value=SimpleNamespace(workflow_id=7)
            )
            db_client.get_workflow_by_id = AsyncMock(
                return_value=SimpleNamespace(organization_id=11)
            )

            result = await handle_vobiz_transfer_result(
                transfer_id="txfr-001", request=request
            )

        assert result == {"status": "pending"}


# ---------------------------------------------------------------------------
# VobizConferenceStrategy
# ---------------------------------------------------------------------------


class TestVobizConferenceStrategy:
    @pytest.mark.asyncio
    async def test_redirects_caller_to_conference(self):
        """Should POST to Vobiz Transfer Call API to redirect A-leg."""
        strategy = VobizConferenceStrategy()
        ctx = _transfer_context()
        api_response = _StubResponse(202, body='{"message": "call transferred"}')
        session = _StubSession([api_response])

        mock_manager = AsyncMock()
        mock_manager.find_transfer_context_for_call = AsyncMock(return_value=ctx)

        with (
            patch(
                "api.services.telephony.providers.vobiz.strategies.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch(
                "api.services.telephony.providers.vobiz.strategies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "api.services.telephony.providers.vobiz.strategies.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            result = await strategy.execute_transfer(
                {
                    "call_id": "orig-call-uuid",
                    "auth_id": "MA_TEST",
                    "auth_token": "test-auth-token-secret",
                }
            )

        assert result is True
        method, url, body = session.requests[0]
        assert method == "POST"
        assert "orig-call-uuid" in url
        assert body["legs"] == "aleg"
        assert "transfer-xml" in body["aleg_url"]
        assert "leg=aleg" in body["aleg_url"]

    @pytest.mark.asyncio
    async def test_returns_false_when_no_transfer_context(self):
        """Should return False when no active transfer context exists."""
        strategy = VobizConferenceStrategy()

        mock_manager = AsyncMock()
        mock_manager.find_transfer_context_for_call = AsyncMock(return_value=None)

        with patch(
            "api.services.telephony.providers.vobiz.strategies.get_call_transfer_manager",
            new_callable=AsyncMock,
            return_value=mock_manager,
        ):
            result = await strategy.execute_transfer(
                {
                    "call_id": "orig-call-uuid",
                    "auth_id": "MA_TEST",
                    "auth_token": "test-secret",
                }
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_and_cleans_up_on_api_failure(self):
        """Should return False and remove context when Vobiz API rejects the redirect."""
        strategy = VobizConferenceStrategy()
        ctx = _transfer_context()
        api_response = _StubResponse(400, body='{"error": "invalid call"}')
        session = _StubSession([api_response])

        mock_manager = AsyncMock()
        mock_manager.find_transfer_context_for_call = AsyncMock(return_value=ctx)
        mock_manager.remove_transfer_context = AsyncMock()

        with (
            patch(
                "api.services.telephony.providers.vobiz.strategies.get_call_transfer_manager",
                new_callable=AsyncMock,
                return_value=mock_manager,
            ),
            patch(
                "api.services.telephony.providers.vobiz.strategies.aiohttp.ClientSession",
                return_value=session,
            ),
            patch(
                "api.services.telephony.providers.vobiz.strategies.get_backend_endpoints",
                new_callable=AsyncMock,
                return_value=("https://example.test", "wss://example.test"),
            ),
        ):
            result = await strategy.execute_transfer(
                {
                    "call_id": "orig-call-uuid",
                    "auth_id": "MA_TEST",
                    "auth_token": "test-secret",
                }
            )

        assert result is False
        mock_manager.remove_transfer_context.assert_awaited_once_with("txfr-001")


# ---------------------------------------------------------------------------
# VobizHangupStrategy
# ---------------------------------------------------------------------------


class TestVobizHangupStrategy:
    @pytest.mark.asyncio
    async def test_deletes_call_via_api(self):
        """Should send DELETE to Vobiz Call endpoint."""
        strategy = VobizHangupStrategy()
        api_response = _StubResponse(204)
        session = _StubSession([api_response])

        with patch(
            "api.services.telephony.providers.vobiz.strategies.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await strategy.execute_hangup(
                {
                    "call_id": "call-to-hang-up",
                    "auth_id": "MA_TEST",
                    "auth_token": "test-secret",
                }
            )

        assert result is True
        method, url, _ = session.requests[0]
        assert method == "DELETE"
        assert "call-to-hang-up" in url

    @pytest.mark.asyncio
    async def test_treats_404_as_success(self):
        """404 means call already ended — should return True."""
        strategy = VobizHangupStrategy()
        api_response = _StubResponse(404)
        session = _StubSession([api_response])

        with patch(
            "api.services.telephony.providers.vobiz.strategies.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await strategy.execute_hangup(
                {
                    "call_id": "already-ended",
                    "auth_id": "MA_TEST",
                    "auth_token": "test-secret",
                }
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_api_failure(self):
        """Non-2xx/404 should return False."""
        strategy = VobizHangupStrategy()
        api_response = _StubResponse(500, body="server error")
        session = _StubSession([api_response])

        with patch(
            "api.services.telephony.providers.vobiz.strategies.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await strategy.execute_hangup(
                {
                    "call_id": "call-xyz",
                    "auth_id": "MA_TEST",
                    "auth_token": "test-secret",
                }
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_missing_credentials(self):
        """Should return False when call_id or credentials are missing."""
        strategy = VobizHangupStrategy()
        result = await strategy.execute_hangup(
            {"call_id": "", "auth_id": "MA_TEST", "auth_token": "test-secret"}
        )
        assert result is False
