import hmac
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from huntweave.config import runner_token
from huntweave.contracts.capabilities import Capabilities


def create_runner(token: str | None = None) -> FastAPI:
    token_bytes = (token or runner_token()).encode("utf-8")
    app = FastAPI(title="HuntWeave Runner", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response: Response
        if request.url.path == "/health/live" and request.method in {"GET", "HEAD"}:
            response = JSONResponse({"status": "alive"})
        elif not hmac.compare_digest(
            request.headers.get("authorization", "").encode("utf-8"), b"Bearer " + token_bytes
        ):
            response = JSONResponse(
                {"reason_code": "runner_authentication_required"}, status_code=401
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/capabilities")
    def capabilities() -> Capabilities:
        return Capabilities()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def unsupported(path: str) -> JSONResponse:
        return JSONResponse({"reason_code": "execution_not_implemented"}, status_code=501)

    return app
