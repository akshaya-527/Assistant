from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agent import build_investigation, run_investigation_stream
from db import finding_review_drafts, init_db, rows, save_finding_review, save_review
from models import FindingReview, ReviewRequest
from tools import call_tool, list_tools

DATA_DIR = Path(__file__).parent / "data"

app = FastAPI(title="Assistant API")

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/site")
def site() -> Any:
    location_rows = rows("select * from locations")
    zone_rows = rows("select * from zones")
    return {
        "locations": location_rows,
        "zones": [{**zone, "points": json.loads(zone["points"])} for zone in zone_rows],
    }


@app.get("/api/events")
def events() -> Any:
    event_rows = rows("select * from events order by timestamp")
    return [{**event, "is_false_alarm": bool(event["is_false_alarm"])} for event in event_rows]


@app.get("/api/drones")
def drones() -> Any:
    patrols = rows("select * from drone_patrols")
    return [
        {**patrol, "route": json.loads(patrol["route"]), "observations": json.loads(patrol["observations"])}
        for patrol in patrols
    ]


@app.get("/api/tools")
def tools() -> Any:
    return [tool.model_dump() for tool in list_tools()]


@app.get("/api/mcp/tools/list")
def mcp_list_tools() -> Any:
    return {"tools": [tool.model_dump() for tool in list_tools()]}


@app.post("/api/tools/{name}/call")
def invoke_tool(name: str, arguments: dict[str, Any]) -> Any:
    try:
        return call_tool(name, arguments)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/mcp/tools/call")
def mcp_call_tool(payload: dict[str, Any]) -> Any:
    name = payload.get("name")
    arguments = payload.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="Expected name and arguments")
    try:
        return {"content": [{"type": "json", "json": call_tool(name, arguments)}]}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/investigate")
async def run_investigation(payload: dict[str, str] | None = None) -> Any:
    review_note = payload.get("review_note") if payload else None
    result = await build_investigation(review_note=review_note)
    return result.model_dump()


@app.get("/api/investigate/stream")
async def stream_investigation(review_note: str = "") -> StreamingResponse:
    async def event_stream():
        async for event in run_investigation_stream(review_note=review_note or None):
            yield f"event: {event['type']}\n"
            yield f"data: {json.dumps(event['data'])}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/review")
async def review(request: ReviewRequest) -> Any:
    review_payload = [item.model_dump() for item in request.finding_reviews]
    if request.action == "refine":
        result = await build_investigation(review_note=request.note, status="draft", finding_reviews=review_payload)
    elif request.action == "approve":
        result = await build_investigation(review_note=request.note, status="approved", finding_reviews=review_payload)
    else:
        result = await build_investigation(review_note=request.note, status="rejected", finding_reviews=review_payload)

    save_review(request.action, request.note, review_payload, result.model_dump())
    return result.model_dump()


@app.get("/api/reviews/findings")
def saved_finding_reviews() -> Any:
    return finding_review_drafts()


@app.post("/api/reviews/finding")
def save_finding_decision(review: FindingReview) -> Any:
    save_finding_review(review.finding_id, review.status, review.note)
    return {
        "saved": True,
        "finding_id": review.finding_id,
        "status": review.status,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
