from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .models import ProjectMapGenerateRequest, ProjectMapImpactRequest
from .service import ProjectMapService
from .storage import TERMINAL_STATUSES


def _sse(event: dict) -> str:
    seq = int(event.get("seq") or 0)
    event_type = str(event.get("type") or "message")
    return f"id: {seq}\nevent: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_project_map_router(service: ProjectMapService) -> APIRouter:
    router = APIRouter(tags=["code-project-map"])

    @router.get("/api/sessions/{session_id}/project-map")
    async def get_project_map(session_id: str):
        return await service.get_map(session_id)

    @router.get("/api/sessions/{session_id}/project-map/freshness")
    async def get_project_map_freshness(session_id: str):
        return await service.freshness(session_id)

    @router.post("/api/sessions/{session_id}/project-map/generate", status_code=202)
    async def generate_project_map(session_id: str, payload: ProjectMapGenerateRequest):
        return await service.start_run(
            session_id,
            model=payload.model or "",
            effort=payload.effort or "",
            preferred_language=payload.preferred_language,
        )

    @router.post("/api/sessions/{session_id}/project-map/refresh", status_code=202)
    async def refresh_project_map(session_id: str, payload: ProjectMapGenerateRequest):
        return await service.start_run(
            session_id,
            model=payload.model or "",
            effort=payload.effort or "",
            preferred_language=payload.preferred_language,
        )

    @router.post("/api/sessions/{session_id}/project-map/impact")
    async def analyze_project_map_impact(session_id: str, payload: ProjectMapImpactRequest):
        return await service.impact(session_id, payload.paths)

    @router.get("/api/sessions/{session_id}/project-map/runs/{run_id}")
    async def get_project_map_run(session_id: str, run_id: str):
        run, _, _, storage_key = service.validate_run_access(session_id, run_id)
        return {
            "ok": True,
            "storage_key": storage_key,
            "run": service._public_run(run),
        }

    @router.post("/api/sessions/{session_id}/project-map/runs/{run_id}/cancel")
    async def cancel_project_map_run(session_id: str, run_id: str):
        return await service.cancel_run(session_id, run_id)

    @router.get("/api/sessions/{session_id}/project-map/runs/{run_id}/stream")
    async def stream_project_map_run(
        request: Request,
        session_id: str,
        run_id: str,
        from_seq: int = Query(default=0, ge=0),
        last_event_id: str = Header(default="", alias="Last-Event-ID"),
    ):
        service.validate_run_access(session_id, run_id)
        try:
            cursor = max(from_seq, int(last_event_id or 0))
        except ValueError:
            cursor = from_seq

        async def events() -> AsyncIterator[str]:
            nonlocal cursor
            heartbeat_at = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                items = await asyncio.to_thread(service.storage.events_after, run_id, cursor)
                for item in items:
                    cursor = max(cursor, int(item.get("seq") or 0))
                    yield _sse(item)
                run = await asyncio.to_thread(service.storage.run, run_id)
                if run is None:
                    raise HTTPException(status_code=404, detail="项目地图任务不存在")
                if run["status"] in TERMINAL_STATUSES and not items:
                    return
                if time.monotonic() - heartbeat_at >= 15:
                    heartbeat_at = time.monotonic()
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
