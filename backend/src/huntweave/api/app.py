from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from huntweave.config import AppSettings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_env()
    app = FastAPI(title="HuntWeave", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def access_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # P0-B will add login and server-side sessions. Nothing grants implicit access in P0-A.
        if request.url.path == "/health/live" and request.method in {"GET", "HEAD"}:
            response = JSONResponse({"status": "alive"})
        elif not settings.access_key:
            response = JSONResponse({"reason_code": "access_key_missing"}, status_code=503)
        else:
            response = JSONResponse({"reason_code": "authentication_required"}, status_code=401)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app
