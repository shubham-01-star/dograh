"""Vobiz call operations invoked when the media pipeline shuts down."""

from typing import Any, Dict

import aiohttp
from loguru import logger
from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy

from api.services.telephony.call_transfer_manager import get_call_transfer_manager
from api.utils.common import get_backend_endpoints


class VobizConferenceStrategy(TransferStrategy):
    """Redirect the original caller to XML that joins the transfer conference.

    Uses Vobiz's Transfer Call API:
    https://www.vobiz.ai/docs/call/transfer-call
    """

    async def execute_transfer(self, context: Dict[str, Any]) -> bool:
        original_call_uuid = context["call_id"]
        auth_id = context["auth_id"]
        auth_token = context["auth_token"]

        manager = await get_call_transfer_manager()
        transfer_context = await manager.find_transfer_context_for_call(
            original_call_uuid
        )
        if not transfer_context:
            logger.error(
                f"[Vobiz Transfer] No active transfer context for call "
                f"{original_call_uuid}"
            )
            return False

        backend_endpoint, _ = await get_backend_endpoints()
        conference_xml_url = (
            f"{backend_endpoint}/api/v1/telephony/vobiz/transfer-xml/"
            f"{transfer_context.conference_name}/{transfer_context.transfer_id}"
            "?leg=aleg"
        )
        call_endpoint = (
            f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{original_call_uuid}/"
        )
        payload = {
            "legs": "aleg",
            "aleg_url": conference_xml_url,
            "aleg_method": "POST",
        }
        headers = {
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    call_endpoint, json=payload, headers=headers
                ) as response:
                    body = await response.text()
                    if response.status not in (200, 201, 202):
                        logger.error(
                            f"[Vobiz Transfer] Failed to redirect caller "
                            f"{original_call_uuid}: status={response.status} body={body}"
                        )
                        await manager.remove_transfer_context(
                            transfer_context.transfer_id
                        )
                        return False

            logger.info(
                f"[Vobiz Transfer] Redirected caller {original_call_uuid} "
                f"to conference {transfer_context.conference_name}"
            )
            return True
        except Exception as exc:
            logger.error(f"[Vobiz Transfer] Failed to redirect caller: {exc}")
            await manager.remove_transfer_context(transfer_context.transfer_id)
            return False


class VobizHangupStrategy(HangupStrategy):
    """Terminate a Vobiz call through the Calls REST API.

    https://www.vobiz.ai/docs/call/hangup-call
    """

    async def execute_hangup(self, context: Dict[str, Any]) -> bool:
        call_uuid = context["call_id"]
        auth_id = context["auth_id"]
        auth_token = context["auth_token"]

        if not call_uuid or not auth_id or not auth_token:
            logger.warning("Cannot hang up Vobiz call: missing call ID or credentials")
            return False

        endpoint = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_uuid}/"
        headers = {
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(endpoint, headers=headers) as response:
                    if response.status in (200, 204, 404):
                        return True
                    body = await response.text()
                    logger.error(
                        f"Failed to hang up Vobiz call {call_uuid}: "
                        f"{response.status} {body}"
                    )
                    return False
        except Exception as exc:
            logger.error(f"Failed to hang up Vobiz call {call_uuid}: {exc}")
            return False
