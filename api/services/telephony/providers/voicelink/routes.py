"""VoiceLink telephony routes (webhooks, status callbacks, event URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry.
"""

import json
from typing import Optional

from fastapi import APIRouter, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id

from api.db import db_client
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)
from api.utils.telephony_helper import parse_webhook_request

router = APIRouter()


@router.post("/voicelink/events/{workflow_run_id}")
@router.post("/voicelink/events")
async def handle_voicelink_events(
    request: Request,
    workflow_run_id: Optional[int] = None,
):
    """Handle VoiceLink Call Event API callbacks (initiated, ringing, connected, hangup, failed)."""
    callback_data, _ = await parse_webhook_request(request)
    logger.info(
        f"[VoiceLink Callback] Received event (run={workflow_run_id}): {json.dumps(callback_data)}"
    )

    # Resolve workflow_run_id from path or payload
    if not workflow_run_id:
        workflow_run_id = callback_data.get("workflow_run_id") or callback_data.get(
            "customParameters", {}
        ).get("workflow_run_id")

    if not workflow_run_id:
        logger.warning(
            "[VoiceLink Callback] Received event without workflow_run_id; ignoring"
        )
        return {"status": "ignored", "reason": "missing_workflow_run_id"}

    set_current_run_id(workflow_run_id)

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(
            f"[run {workflow_run_id}] Workflow run not found for VoiceLink callback"
        )
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"[run {workflow_run_id}] Workflow not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    parsed_data = provider.parse_status_callback(callback_data)

    status_update = StatusCallbackRequest(
        call_id=parsed_data["call_id"],
        status=parsed_data["status"],
        from_number=parsed_data.get("from_number"),
        to_number=parsed_data.get("to_number"),
        direction=parsed_data.get("direction"),
        duration=parsed_data.get("duration"),
        extra=parsed_data.get("extra", {}),
    )

    await _process_status_update(workflow_run_id, status_update)
    logger.info(
        f"[run {workflow_run_id}] VoiceLink event {parsed_data['status']} processed successfully"
    )

    return {"status": "success"}


@router.post("/voicelink-event")
async def handle_voicelink_generic_event(request: Request):
    """Handle generic VoiceLink webhook events."""
    callback_data, _ = await parse_webhook_request(request)
    logger.info(f"[VoiceLink Generic Webhook] Received: {json.dumps(callback_data)}")
    return {"status": "success"}
