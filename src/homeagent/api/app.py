from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

StatusFn = Callable[[], Awaitable[str]]


def build_app(status: StatusFn) -> FastAPI:
    app = FastAPI(title="homeagent", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": await status()}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
