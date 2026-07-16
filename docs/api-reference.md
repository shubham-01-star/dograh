# Dograh API Reference

Dograh exposes a FastAPI backend mounted under `/api/v1`.

## Interactive Docs

When the backend is running locally on port `8000`, open:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Raw OpenAPI JSON: `http://localhost:8000/api/v1/openapi.json`
- Health check: `http://localhost:8000/api/v1/health`

No separate Swagger package is required. The project already depends on FastAPI, and FastAPI serves Swagger UI and ReDoc automatically. In this repo the OpenAPI JSON path is customized in `api/app.py`:

```python
openapi_url="/api/v1/openapi.json"
```

The docs UI paths are not customized, so they stay at `/docs` and `/redoc`.

## Start The API Locally

From the repo root, use the documented dev setup:

```bash
bash scripts/start_services_dev.sh
```

Or run the backend directly after activating the Python environment and loading the backend env file:

```bash
source venv/bin/activate
set -a && source api/.env && set +a
uvicorn api.app:app --reload --port 8000
```

On Windows PowerShell, the equivalent direct run is usually:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn api.app:app --reload --port 8000
```

## Authentication

Most authenticated API routes use one of these headers:

```http
Authorization: Bearer <jwt-token>
```

or:

```http
X-API-Key: <dograh-api-key>
```

Public agent trigger routes require `X-API-Key`.

## Basic Examples

### Health Check

```bash
curl http://localhost:8000/api/v1/health
```

### Sign Up

```bash
curl -X POST http://localhost:8000/api/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "change-me",
    "name": "Example User"
  }'
```

### Login

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "change-me"
  }'
```

Use the returned `token` in later requests:

```bash
curl http://localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer <jwt-token>"
```

### Create An API Key

```bash
curl -X POST http://localhost:8000/api/v1/user/api-keys \
  -H "Authorization: Bearer <jwt-token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Local test key"}'
```

### List Workflows

```bash
curl http://localhost:8000/api/v1/workflow/fetch \
  -H "Authorization: Bearer <jwt-token>"
```

### Trigger A Public Agent Call

```bash
curl -X POST http://localhost:8000/api/v1/public/agent/<agent-uuid> \
  -H "X-API-Key: <dograh-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+15551234567",
    "variables": {
      "name": "Example Caller"
    }
  }'
```

Use Swagger UI for the exact request schema for each endpoint. The backend Pydantic models are the source of truth and Swagger displays required fields, optional fields, response models, and validation errors.

## Endpoint Inventory

All paths below are mounted under the local API server, for example `http://localhost:8000/api/v1/health`.

### Auth

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/auth/signup` | `signup` |
| POST | `/api/v1/auth/login` | `login` |
| GET | `/api/v1/auth/me` | `get_current_user` |

### Campaigns

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/campaign/create` | `create_campaign` |
| GET | `/api/v1/campaign/` | `get_campaigns` |
| GET | `/api/v1/campaign/{campaign_id}` | `get_campaign` |
| POST | `/api/v1/campaign/{campaign_id}/start` | `start_campaign` |
| POST | `/api/v1/campaign/{campaign_id}/pause` | `pause_campaign` |
| PATCH | `/api/v1/campaign/{campaign_id}` | `update_campaign` |
| GET | `/api/v1/campaign/{campaign_id}/runs` | `get_campaign_runs` |
| POST | `/api/v1/campaign/{campaign_id}/redial` | `redial_campaign` |
| POST | `/api/v1/campaign/{campaign_id}/resume` | `resume_campaign` |
| GET | `/api/v1/campaign/{campaign_id}/progress` | `get_campaign_progress` |
| GET | `/api/v1/campaign/{campaign_id}/source-download-url` | `get_campaign_source_download_url` |
| GET | `/api/v1/campaign/{campaign_id}/report` | `download_campaign_report` |

### Credentials

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/credentials/` | `list_credentials` |
| POST | `/api/v1/credentials/` | `create_credential` |
| GET | `/api/v1/credentials/{credential_uuid}` | `get_credential` |
| PUT | `/api/v1/credentials/{credential_uuid}` | `update_credential` |
| DELETE | `/api/v1/credentials/{credential_uuid}` | `delete_credential` |

### Folders

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/folder/` | `list_folders` |
| POST | `/api/v1/folder/` | `create_folder` |
| PUT | `/api/v1/folder/{folder_id}` | `rename_folder` |
| DELETE | `/api/v1/folder/{folder_id}` | `delete_folder` |

### Knowledge Base

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/knowledge-base/upload-url` | `get_upload_url` |
| POST | `/api/v1/knowledge-base/process-document` | `process_document` |
| GET | `/api/v1/knowledge-base/documents` | `list_documents` |
| GET | `/api/v1/knowledge-base/documents/{document_uuid}` | `get_document` |
| DELETE | `/api/v1/knowledge-base/documents/{document_uuid}` | `delete_document` |
| POST | `/api/v1/knowledge-base/search` | `search_chunks` |

### Main

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/health` | `health` |

### Node Types

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/node-types` | `list_node_types` |
| GET | `/api/v1/node-types/{name}` | `get_node_type` |

### Organizations

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/organizations/context` | `get_current_organization_context` |
| GET | `/api/v1/organizations/telephony-providers/metadata` | `get_telephony_providers_metadata` |
| GET | `/api/v1/organizations/telephony-config-warnings` | `get_telephony_config_warnings` |
| GET | `/api/v1/organizations/model-configurations/v2/defaults` | `get_model_configuration_v2_defaults` |
| GET | `/api/v1/organizations/model-configurations/v2` | `get_model_configuration_v2` |
| PUT | `/api/v1/organizations/model-configurations/v2` | `save_model_configuration_v2` |
| GET | `/api/v1/organizations/model-configurations/v2/migration-preview` | `preview_model_configuration_v2_migration` |
| POST | `/api/v1/organizations/model-configurations/v2/migrate` | `migrate_model_configuration_v2` |
| GET | `/api/v1/organizations/preferences` | `get_preferences` |
| PUT | `/api/v1/organizations/preferences` | `save_preferences` |
| GET | `/api/v1/organizations/model-configurations/preferences` | `get_model_configuration_preferences_legacy` |
| PUT | `/api/v1/organizations/model-configurations/preferences` | `save_model_configuration_preferences_legacy` |
| GET | `/api/v1/organizations/telephony-configs` | `list_telephony_configurations` |
| POST | `/api/v1/organizations/telephony-configs` | `create_telephony_configuration` |
| GET | `/api/v1/organizations/telephony-configs/{config_id}` | `get_telephony_configuration_by_id` |
| PUT | `/api/v1/organizations/telephony-configs/{config_id}` | `update_telephony_configuration` |
| POST | `/api/v1/organizations/telephony-configs/{config_id}/set-default-outbound` | `set_default_outbound` |
| DELETE | `/api/v1/organizations/telephony-configs/{config_id}` | `delete_telephony_configuration` |
| GET | `/api/v1/organizations/telephony-configs/{config_id}/phone-numbers` | `list_phone_numbers` |
| POST | `/api/v1/organizations/telephony-configs/{config_id}/phone-numbers` | `create_phone_number` |
| GET | `/api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}` | `get_phone_number` |
| PUT | `/api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}` | `update_phone_number` |
| POST | `/api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}/set-default-caller` | `set_default_caller_id` |
| DELETE | `/api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}` | `delete_phone_number` |
| GET | `/api/v1/organizations/telephony-config` | `get_telephony_configuration` |
| POST | `/api/v1/organizations/telephony-config` | `save_telephony_configuration` |
| GET | `/api/v1/organizations/langfuse-credentials` | `get_langfuse_credentials` |
| POST | `/api/v1/organizations/langfuse-credentials` | `save_langfuse_credentials` |
| DELETE | `/api/v1/organizations/langfuse-credentials` | `delete_langfuse_credentials` |
| GET | `/api/v1/organizations/campaign-defaults` | `get_campaign_defaults` |

### Organization Usage And Reports

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/organizations/usage/current-period` | `get_current_period_usage` |
| GET | `/api/v1/organizations/usage/mps-credits` | `get_mps_credits` |
| GET | `/api/v1/organizations/billing/credits` | `get_billing_credits` |
| POST | `/api/v1/organizations/usage/mps-credits/purchase-url` | `create_mps_credit_purchase_url` |
| GET | `/api/v1/organizations/usage/runs` | `get_usage_history` |
| GET | `/api/v1/organizations/usage/runs/report` | `download_usage_runs_report` |
| GET | `/api/v1/organizations/usage/daily-breakdown` | `get_daily_usage_breakdown` |
| GET | `/api/v1/organizations/reports/daily` | `get_daily_report` |
| GET | `/api/v1/organizations/reports/workflows` | `get_workflow_options` |
| GET | `/api/v1/organizations/reports/daily/runs` | `get_daily_runs_detail` |

### Public Routes

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/public/agent/{uuid}` | `initiate_call` |
| POST | `/api/v1/public/agent/test/{uuid}` | `initiate_call_test` |
| POST | `/api/v1/public/agent/workflow/{workflow_uuid}` | `initiate_call_by_workflow_uuid` |
| POST | `/api/v1/public/agent/test/workflow/{workflow_uuid}` | `initiate_call_test_by_workflow_uuid` |
| GET | `/api/v1/public/download/workflow/{token}/{artifact_type}` | `download_workflow_artifact` |
| POST | `/api/v1/public/embed/init` | `initialize_embed_session` |
| GET | `/api/v1/public/embed/config/{token}` | `get_embed_config` |
| GET | `/api/v1/public/embed/turn-credentials/{session_token}` | `get_public_turn_credentials` |

### Storage

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/s3/signed-url` | `get_signed_url` |
| GET | `/api/v1/s3/file-metadata` | `get_file_metadata` |
| POST | `/api/v1/s3/presigned-upload-url` | `get_presigned_upload_url` |

### Service Keys And API Keys

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/user/service-keys` | `get_service_keys` |
| POST | `/api/v1/user/service-keys` | `create_service_key` |
| DELETE | `/api/v1/user/service-keys/{service_key_id}` | `archive_service_key` |
| PUT | `/api/v1/user/service-keys/{service_key_id}/reactivate` | `reactivate_service_key` |
| GET | `/api/v1/user/api-keys` | `get_api_keys` |
| POST | `/api/v1/user/api-keys` | `create_api_key` |
| DELETE | `/api/v1/user/api-keys/{api_key_id}` | `archive_api_key` |
| PUT | `/api/v1/user/api-keys/{api_key_id}/reactivate` | `reactivate_api_key` |

### Superuser

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/superuser/impersonate` | `impersonate` |
| GET | `/api/v1/superuser/workflow-runs` | `get_workflow_runs` |

### Telephony

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/telephony/initiate-call` | `initiate_call` |
| POST | `/api/v1/telephony/inbound/run` | `handle_inbound_run` |
| POST | `/api/v1/telephony/inbound/fallback` | `handle_inbound_fallback` |
| POST | `/api/v1/telephony/inbound/{workflow_id}` | `handle_inbound_telephony` |
| POST | `/api/v1/telephony/transfer-result/{transfer_id}` | `complete_transfer_function_call` |

`/api/v1/telephony/inbound/{workflow_id}` is marked deprecated in the route file.

### Tools

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/tools/` | `list_tools` |
| POST | `/api/v1/tools/` | `create_tool` |
| GET | `/api/v1/tools/{tool_uuid}` | `get_tool` |
| POST | `/api/v1/tools/{tool_uuid}/mcp/refresh` | `refresh_mcp_tools` |
| PUT | `/api/v1/tools/{tool_uuid}` | `update_tool` |
| DELETE | `/api/v1/tools/{tool_uuid}` | `delete_tool` |
| POST | `/api/v1/tools/{tool_uuid}/unarchive` | `unarchive_tool` |

### TURN

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/turn/credentials` | `get_turn_credentials` |

### User Configuration

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/api/v1/user/configurations/defaults` | `get_default_configurations` |
| GET | `/api/v1/user/auth/user` | `get_auth_user` |
| GET | `/api/v1/user/configurations/user` | `get_user_configurations` |
| PUT | `/api/v1/user/configurations/user` | `update_user_configurations` |
| GET | `/api/v1/user/configurations/user/validate` | `validate_user_configurations` |
| GET | `/api/v1/user/configurations/voices/{provider}` | `get_voices` |

### Workflows

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/workflow/{workflow_id}/validate` | `validate_workflow` |
| POST | `/api/v1/workflow/create/definition` | `create_workflow` |
| POST | `/api/v1/workflow/create/template` | `create_workflow_from_template` |
| GET | `/api/v1/workflow/count` | `get_workflow_count` |
| GET | `/api/v1/workflow/fetch` | `get_workflows` |
| GET | `/api/v1/workflow/fetch/{workflow_id}` | `get_workflow` |
| GET | `/api/v1/workflow/{workflow_id}/versions` | `get_workflow_versions` |
| POST | `/api/v1/workflow/{workflow_id}/publish` | `publish_workflow` |
| POST | `/api/v1/workflow/{workflow_id}/create-draft` | `create_workflow_draft` |
| GET | `/api/v1/workflow/summary` | `get_workflows_summary` |
| PUT | `/api/v1/workflow/{workflow_id}/status` | `update_workflow_status` |
| PUT | `/api/v1/workflow/{workflow_id}/folder` | `move_workflow_to_folder` |
| PUT | `/api/v1/workflow/{workflow_id}` | `update_workflow` |
| POST | `/api/v1/workflow/{workflow_id}/duplicate` | `duplicate_workflow_endpoint` |
| POST | `/api/v1/workflow/{workflow_id}/runs` | `create_workflow_run` |
| GET | `/api/v1/workflow/{workflow_id}/runs/{run_id}` | `get_workflow_run` |
| GET | `/api/v1/workflow/{workflow_id}/runs` | `get_workflow_runs` |
| GET | `/api/v1/workflow/{workflow_id}/report` | `download_workflow_report` |
| GET | `/api/v1/workflow/templates` | `get_workflow_templates` |
| POST | `/api/v1/workflow/templates/duplicate` | `duplicate_workflow_template` |
| POST | `/api/v1/workflow/ambient-noise/upload-url` | `get_ambient_noise_upload_url` |
| POST | `/api/v1/workflow/{workflow_id}/embed-token` | `create_or_update_embed_token` |
| GET | `/api/v1/workflow/{workflow_id}/embed-token` | `get_embed_token` |
| DELETE | `/api/v1/workflow/{workflow_id}/embed-token` | `deactivate_embed_token` |

### Workflow Recordings

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/workflow-recordings/upload-url` | `get_upload_urls` |
| POST | `/api/v1/workflow-recordings/` | `create_recordings` |
| GET | `/api/v1/workflow-recordings/` | `list_recordings` |
| DELETE | `/api/v1/workflow-recordings/{recording_id}` | `delete_recording` |
| PATCH | `/api/v1/workflow-recordings/{id}` | `update_recording` |
| POST | `/api/v1/workflow-recordings/transcribe` | `transcribe_audio` |

### Workflow Text Chat

| Method | Path | Handler |
| --- | --- | --- |
| POST | `/api/v1/workflow/{workflow_id}/text-chat/sessions` | `create_text_chat_session` |
| GET | `/api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}` | `get_text_chat_session` |
| POST | `/api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}/messages` | `append_text_chat_message` |
| POST | `/api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}/rewind` | `rewind_text_chat_session` |

### MCP

The MCP server is mounted under:

```text
/api/v1/mcp
```

Agents use the same `X-API-Key` authentication style for this mounted MCP surface.
