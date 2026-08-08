import sys
import json
from api.app import app
import uvicorn
from fastapi.openapi.utils import get_openapi

with open("ui/openapi.json", "w") as f:
    json.dump(get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    ), f)
