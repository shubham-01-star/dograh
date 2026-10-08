"""Vobiz telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.
"""

import json
from datetime import UTC, datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id
from starlette.responses import HTMLResponse

from api.db import db_client
from api.services.telephony.call_transfer_manager import get_call_transfer_manager
from api.services.telephony.factory import (
    get_telephony_provider_for_run,
)
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)
from api.services.telephony.transfer_event_protocol import (
    TransferEvent,
    TransferEventType,
)
from api.utils.common import get_backend_endpoints
from api.utils.telephony_helper import (
    parse_webhook_request,
)

router = APIRouter()


def _hangup_xml_response() -> HTMLResponse:
    return HTMLResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
        media_type="application/xml",
    )


def _conference_xml_response(
    conference_name: str, is_original_leg: bool = False
) -> HTMLResponse:
    safe_conference_name = escape(conference_name)
    if is_original_leg:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Conference endConferenceOnExit="true">{safe_conference_name}</Conference>
</Response>"""
    else:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>You have answered a transfer call. Connecting you now.</Speak>
    <Conference endConferenceOnExit="true">{safe_conference_name}</Conference>
</Response>"""
    return HTMLResponse(content=xml, media_type="application/xml")


async def _verify_vobiz_callback(
    provider,
    webhook_url: str,
    callback_data: dict,
    headers: dict,
    raw_body: str,
    *,
    log_prefix: str,
) -> None:
    """Verify a Vobiz callback signature, failing closed.

    Vobiz signs every callback, so a missing signature header is an invalid
    request — ``provider.verify_inbound_signature`` returns ``False`` for both
    missing and forged signatures. Reject with HTTP 403 (per Vobiz's
    callback-validation docs) so the caller never reaches status processing.
    """
    is_valid = await provider.verify_inbound_signature(
        webhook_url, callback_data, headers, raw_body
    )
    if not is_valid:
        logger.warning(f"{log_prefix} Invalid or missing Vobiz callback signature")
        raise HTTPException(status_code=403, detail="Invalid webhook signature")


@router.post("/vobiz-xml", include_in_schema=False)
async def handle_vobiz_xml_webhook(
    workflow_id: int, workflow_run_id: int, organization_id: int
):
    """
    Handle initial webhook from Vobiz when call is answered.
    Returns Vobiz XML response with Stream element.

    Vobiz uses Plivo-compatible XML format similar to Twilio's TwiML.
    """
    set_current_run_id(workflow_run_id)
    logger.info(
        f"[run {workflow_run_id}] Vobiz XML webhook called - "
        f"workflow_id={workflow_id}, org_id={organization_id}"
    )

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    provider = await get_telephony_provider_for_run(workflow_run, organization_id)

    logger.debug(f"[run {workflow_run_id}] Using provider: {provider.PROVIDER_NAME}")

    response_content = await provider.get_webhook_response(
        workflow_id, organization_id, workflow_run_id
    )

    logger.debug(
        f"[run {workflow_run_id}] Vobiz XML response generated:\n{response_content}"
    )

    return HTMLResponse(content=response_content, media_type="application/xml")


@router.post("/vobiz/hangup-callback/{workflow_run_id}")
async def handle_vobiz_hangup_callback(
    workflow_run_id: int,
    request: Request,
):
    """Handle Vobiz hangup callback (sent when call ends).

    Vobiz sends callbacks to hangup_url when the call terminates.
    This includes call duration, status, and billing information.
    """
    set_current_run_id(workflow_run_id)

    all_headers = dict(request.headers)

    # Parse the callback data from the raw body so signed webhooks can verify
    # the exact bytes Vobiz sent without draining the request stream first.
    callback_data, raw_body = await parse_webhook_request(request)

    logger.info(
        f"[run {workflow_run_id}] Received Vobiz hangup callback {json.dumps(callback_data)}"
    )

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(
            f"[run {workflow_run_id}] Workflow run not found for Vobiz hangup callback"
        )
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"[run {workflow_run_id}] Workflow not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    # Fail closed: Vobiz signs every callback, so reject unsigned/forged ones
    # before they can mutate call state.
    backend_endpoint, _ = await get_backend_endpoints()
    webhook_url = (
        f"{backend_endpoint}/api/v1/telephony/vobiz/hangup-callback/{workflow_run_id}"
    )
    await _verify_vobiz_callback(
        provider,
        webhook_url,
        callback_data,
        all_headers,
        raw_body,
        log_prefix=f"[run {workflow_run_id}]",
    )

    logger.debug(
        f"[run {workflow_run_id}] Processing Vobiz hangup with provider: {provider.PROVIDER_NAME}"
    )

    # Parse the callback data into generic format
    parsed_data = provider.parse_status_callback(callback_data)

    # Create StatusCallbackRequest from parsed data
    status_update = StatusCallbackRequest(
        call_id=parsed_data["call_id"],
        status=parsed_data["status"],
        from_number=parsed_data.get("from_number"),
        to_number=parsed_data.get("to_number"),
        direction=parsed_data.get("direction"),
        duration=parsed_data.get("duration"),
        extra=parsed_data.get("extra", {}),
    )

    # Process the status update
    await _process_status_update(workflow_run_id, status_update)

    logger.info(f"[run {workflow_run_id}] Vobiz hangup callback processed successfully")

    return {"status": "success"}


@router.post("/vobiz/ring-callback/{workflow_run_id}")
async def handle_vobiz_ring_callback(
    workflow_run_id: int,
    request: Request,
):
    """Handle Vobiz ring callback (sent when call starts ringing).

    Vobiz can send callbacks to ring_url when the call starts ringing.
    This is optional and used for tracking ringing status.
    """
    set_current_run_id(workflow_run_id)

    all_headers = dict(request.headers)

    # Parse the callback data from the raw body so signed webhooks can verify
    # the exact bytes Vobiz sent without draining the request stream first.
    callback_data, raw_body = await parse_webhook_request(request)

    logger.info(
        f"[run {workflow_run_id}] Received Vobiz ring callback {json.dumps(callback_data)}"
    )

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(
            f"[run {workflow_run_id}] Workflow run not found for Vobiz ring callback"
        )
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"[run {workflow_run_id}] Workflow not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    # Fail closed: reject unsigned/forged ring callbacks before logging them.
    backend_endpoint, _ = await get_backend_endpoints()
    webhook_url = (
        f"{backend_endpoint}/api/v1/telephony/vobiz/ring-callback/{workflow_run_id}"
    )
    await _verify_vobiz_callback(
        provider,
        webhook_url,
        callback_data,
        all_headers,
        raw_body,
        log_prefix=f"[run {workflow_run_id}]",
    )

    # Log the ringing event
    telephony_callback_logs = workflow_run.logs.get("telephony_status_callbacks", [])
    ring_log = {
        "status": "ringing",
        "timestamp": datetime.now(UTC).isoformat(),
        "call_id": callback_data.get("call_uuid", callback_data.get("CallUUID", "")),
        "event_type": "ring",
        "raw_data": callback_data,
    }
    telephony_callback_logs.append(ring_log)

    # Update workflow run logs
    await db_client.update_workflow_run(
        run_id=workflow_run_id,
        logs={"telephony_status_callbacks": telephony_callback_logs},
    )

    logger.info(f"[run {workflow_run_id}] Vobiz ring callback logged")

    return {"status": "success"}


@router.post("/vobiz/hangup-callback/workflow/{workflow_id}")
async def handle_vobiz_hangup_callback_by_workflow(
    workflow_id: int,
    request: Request,
):
    """Handle Vobiz hangup callback with workflow_id - finds workflow run by call_id."""

    all_headers = dict(request.headers)

    try:
        callback_data, raw_body = await parse_webhook_request(request)
    except ValueError:
        callback_data = {}
        raw_body = ""

    call_uuid = callback_data.get("CallUUID") or callback_data.get("call_uuid")
    logger.info(
        f"[workflow {workflow_id}] Received Vobiz hangup callback for call {call_uuid}: {json.dumps(callback_data)}"
    )

    if not call_uuid:
        logger.warning(
            f"[workflow {workflow_id}] No call_uuid found in Vobiz hangup callback"
        )
        return {"status": "error", "message": "No call_uuid found"}

    workflow = await db_client.get_workflow_by_id(workflow_id)
    if not workflow:
        logger.warning(f"[workflow {workflow_id}] Workflow not found")
        return {"status": "error", "message": "workflow_not_found"}

    try:
        workflow_run = await db_client.get_workflow_run_by_call_id(call_uuid)
    except Exception as e:
        logger.error(
            f"[workflow {workflow_id}] Error finding workflow run for call {call_uuid}: {e}"
        )
        return {"status": "error", "message": str(e)}

    if not workflow_run or workflow_run.workflow_id != workflow_id:
        logger.warning(
            f"[workflow {workflow_id}] No workflow run found for call {call_uuid}"
        )
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    workflow_run_id = workflow_run.id
    set_current_run_id(workflow_run_id)
    logger.info(
        f"[workflow {workflow_id}] Found workflow run {workflow_run_id} for call {call_uuid}"
    )

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    # Fail closed: Vobiz signs every callback, so reject unsigned/forged ones
    # before they can mutate call state.
    backend_endpoint, _ = await get_backend_endpoints()
    webhook_url = f"{backend_endpoint}/api/v1/telephony/vobiz/hangup-callback/workflow/{workflow_id}"
    await _verify_vobiz_callback(
        provider,
        webhook_url,
        callback_data,
        all_headers,
        raw_body,
        log_prefix=f"[workflow {workflow_id}]",
    )

    try:
        parsed_data = provider.parse_status_callback(callback_data)

        status = StatusCallbackRequest(
            call_id=parsed_data["call_id"],
            status=parsed_data["status"],
            from_number=parsed_data.get("from_number"),
            to_number=parsed_data.get("to_number"),
            direction=parsed_data.get("direction"),
            duration=parsed_data.get("duration"),
            extra=parsed_data.get("extra", {}),
        )

        await _process_status_update(workflow_run_id, status)

        logger.info(
            f"[run {workflow_run_id}] Vobiz hangup callback processed successfully"
        )
        return {"status": "success"}

    except Exception as e:
        logger.error(
            f"[run {workflow_run_id}] Error processing Vobiz hangup callback: {e}"
        )
        return {"status": "error", "message": str(e)}


@router.post(
    "/vobiz/transfer-xml/{conference_name}/{transfer_id}", include_in_schema=False
)
async def handle_vobiz_transfer_xml(
    conference_name: str, transfer_id: str, request: Request
):
    """Return conference XML for either leg of a Vobiz transfer.

    The destination callback publishes DESTINATION_ANSWERED after placing
    that leg in the conference. Pipeline teardown then invokes the Vobiz
    transfer strategy, which redirects the original caller back to this XML.
    """
    try:
        data, raw_body = await parse_webhook_request(request)
    except Exception:
        data = {}
        raw_body = ""

    callback_call_uuid = data.get("CallUUID") or data.get("call_uuid", "")
    is_original_leg = request.query_params.get("leg") == "aleg"
    leg_name = "original" if is_original_leg else "destination"

    logger.info(
        f"Vobiz transfer XML requested (transfer_id={transfer_id}, leg={leg_name}): "
        f"CallUUID={callback_call_uuid} conference={conference_name}"
    )

    call_transfer_manager = await get_call_transfer_manager()
    transfer_context = await call_transfer_manager.get_transfer_context(transfer_id)
    if not transfer_context:
        return _hangup_xml_response()

    if conference_name != transfer_context.conference_name:
        logger.warning(
            f"Conference mismatch for Vobiz transfer {transfer_id}: "
            f"requested={conference_name} expected={transfer_context.conference_name}"
        )
        return _hangup_xml_response()

    workflow_run_id = transfer_context.workflow_run_id
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        return _hangup_xml_response()

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        return _hangup_xml_response()

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    backend_endpoint, _ = await get_backend_endpoints()
    webhook_url = f"{backend_endpoint}/api/v1/telephony/vobiz/transfer-xml/{conference_name}/{transfer_id}"
    if is_original_leg:
        webhook_url += "?leg=aleg"

    all_headers = dict(request.headers)
    is_valid = await provider.verify_inbound_signature(
        webhook_url, data, all_headers, raw_body
    )
    if not is_valid:
        logger.warning(f"Invalid Vobiz signature for transfer XML {transfer_id}")
        return _hangup_xml_response()

    if not is_original_leg:
        destination_call_uuid = callback_call_uuid
        if destination_call_uuid and transfer_context.call_sid != destination_call_uuid:
            transfer_context.call_sid = destination_call_uuid
            await call_transfer_manager.store_transfer_context(transfer_context)

        if await call_transfer_manager.claim_transfer_step(
            transfer_id, "destination_answered"
        ):
            await call_transfer_manager.publish_transfer_event(
                TransferEvent(
                    type=TransferEventType.DESTINATION_ANSWERED,
                    transfer_id=transfer_id,
                    original_call_sid=transfer_context.original_call_sid,
                    transfer_call_sid=destination_call_uuid,
                    conference_name=transfer_context.conference_name,
                    status="success",
                    action="destination_answered",
                    message="Destination answered — bridging into conference.",
                )
            )

    return _conference_xml_response(
        transfer_context.conference_name, is_original_leg=is_original_leg
    )


@router.post("/vobiz/transfer-result/{transfer_id}", include_in_schema=False)
async def handle_vobiz_transfer_result(transfer_id: str, request: Request):
    """Handle Vobiz transfer call completion and failure status callbacks."""
    try:
        data, raw_body = await parse_webhook_request(request)
    except Exception:
        data = {}
        raw_body = ""

    event = data.get("Event") or data.get("event", "")
    destination_call_uuid = data.get("CallUUID") or data.get("call_uuid", "")
    hangup_cause = data.get("HangupCause") or data.get("hangup_cause", "")

    logger.info(
        f"Vobiz transfer-result webhook (transfer_id={transfer_id}): "
        f"Event={event} CallUUID={destination_call_uuid} HangupCause={hangup_cause}"
    )

    call_transfer_manager = await get_call_transfer_manager()
    transfer_context = await call_transfer_manager.get_transfer_context(transfer_id)
    if not transfer_context:
        return {"status": "error", "reason": "invalid_transfer_id"}

    workflow_run_id = transfer_context.workflow_run_id
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        return {"status": "error", "reason": "invalid_run"}

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        return {"status": "error", "reason": "invalid_workflow"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    backend_endpoint, _ = await get_backend_endpoints()
    webhook_url = f"{backend_endpoint}/api/v1/telephony/vobiz/transfer-result/{transfer_id}"
    all_headers = dict(request.headers)
    is_valid = await provider.verify_inbound_signature(
        webhook_url, data, all_headers, raw_body
    )
    if not is_valid:
        logger.warning(f"Invalid Vobiz signature for transfer result {transfer_id}")
        return {"status": "error", "reason": "invalid_signature"}

    original_call_uuid = transfer_context.original_call_sid
    conference_name = transfer_context.conference_name

    if not hangup_cause:
        return {"status": "pending"}

    if hangup_cause == "USER_BUSY":
        transfer_event = TransferEvent(
            type=TransferEventType.TRANSFER_FAILED,
            transfer_id=transfer_id,
            original_call_sid=original_call_uuid,
            transfer_call_sid=destination_call_uuid,
            conference_name=conference_name,
            status="transfer_failed",
            action="transfer_failed",
            reason="busy",
            message="The transfer call encountered a busy signal.",
        )
    elif hangup_cause == "NORMAL_CLEARING":
        await call_transfer_manager.remove_transfer_context(transfer_id)
        return {"status": "success"}
    elif hangup_cause in ("NO_ANSWER", "ORIGINATOR_CANCEL"):
        transfer_event = TransferEvent(
            type=TransferEventType.TRANSFER_FAILED,
            transfer_id=transfer_id,
            original_call_sid=original_call_uuid,
            transfer_call_sid=destination_call_uuid,
            conference_name=conference_name,
            status="transfer_failed",
            action="transfer_failed",
            reason="no_answer",
            message="The transfer call was not answered.",
        )
    else:
        transfer_event = TransferEvent(
            type=TransferEventType.TRANSFER_FAILED,
            transfer_id=transfer_id,
            original_call_sid=original_call_uuid,
            transfer_call_sid=destination_call_uuid,
            conference_name=conference_name,
            status="transfer_failed",
            action="transfer_failed",
            reason="call_failed",
            message=f"Transfer call failed: {hangup_cause}",
        )

    if not await call_transfer_manager.claim_transfer_step(
        transfer_id, "failure_reported"
    ):
        return {"status": "success"}

    await call_transfer_manager.publish_transfer_event(transfer_event)
    await call_transfer_manager.remove_transfer_context(transfer_id)
    return {"status": "success"}

