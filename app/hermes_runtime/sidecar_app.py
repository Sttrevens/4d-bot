from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI

from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse
from app.hermes_runtime.worker import health as worker_health
from app.hermes_runtime.worker import run_runtime_turn


def create_app(*, worker=None) -> FastAPI:
    app = FastAPI(title="Hermes Runtime Sidecar")
    turn_worker = worker or run_runtime_turn

    @app.get("/health")
    def health() -> dict[str, Any]:
        return worker_health(probe_sidecar=False)

    @app.post("/v1/runtime/turn")
    async def runtime_turn(payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload.get("run_id") or "")
        try:
            request = RuntimeRequest.from_dict(payload)
        except Exception as exc:
            return RuntimeResponse.failed(
                run_id,
                "runtime_bad_request",
                f"Invalid Hermes runtime request: {exc}",
                retryable=False,
            ).to_dict()

        response = await turn_worker(request)
        return response.to_dict()

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.getenv("HERMES_RUNTIME_HOST", "127.0.0.1")
    port = int(os.getenv("HERMES_RUNTIME_PORT", "8765"))
    uvicorn.run("app.hermes_runtime.sidecar_app:app", host=host, port=port)


if __name__ == "__main__":
    main()
