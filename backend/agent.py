from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any, AsyncGenerator

import httpx
from dotenv import load_dotenv

from models import Finding, FollowUpMission, Investigation, ReasoningStep, ToolCall
from tools import TOOLS, call_tool

load_dotenv()

INVESTIGATION_SYSTEM_PROMPT = """
You are an overnight incident analyst for Ridgeway Site, an industrial facility that runs 24/7.
It is 6:10 AM. You are helping Maya prepare the 8:00 AM leadership briefing.

Your job is to investigate overnight signals and separate noise from incidents that need attention.
Most alerts are false alarms. Your value is distinguishing the two with evidence.

Noise patterns:
- Gate 3 fence alerts around 22:00-02:00 often correlate with wind and adjacent sensor sequence.
- One or two failed badge swipes can be operator error; three or more at one access point within 10 minutes needs review.
- Storage Yard C vehicle activity can match Tuesday/Thursday contractor routes, but route deviation matters.
- A late drone pass reduces risk but does not erase a coverage gap.

Escalation triggers:
- Restricted-zone signal without clear personnel or contractor authorization.
- Three or more failed badge swipes at the same point within 10 minutes.
- Vehicle path that deviates from a known contractor route.
- Drone patrol gap over 40 minutes covering a flagged zone.
- Raghav's Block C note makes Storage Yard C / Block C priority.

Classify every signal as:
- CLEARED: explain exactly why it is consistent with noise and cite the evidence.
- NEEDS_REVIEW: explain what is suspicious, what is missing, and what Maya should physically verify.
- ESCALATE: state the evidence chain and the action required before 8:00 AM.

Never claim certainty about an intruder; say unknown individual, contractor, or unverified activity.
Never say definitely harmless; say consistent with a normal pattern based on specific evidence.
Always state whether the drone coverage gap overlaps the suspicious window.
Always state what you know, what you do not know, and what Maya should verify.
"""

DEFAULT_PLAN = [
    {
        "thought": "Checking overnight warning and critical signals first, before reading everything manually.",
        "tool": "search_events",
        "arguments": {"severities": ["warning", "critical"], "since": "00:00", "until": "06:10"},
        "rationale": "Find the signals that could matter for Maya's morning briefing.",
    },
    {
        "thought": "Cross-referencing the flagged locations with site risk and zone context.",
        "tool": "get_site_context",
        "arguments": {"location_ids": ["gate-3", "access-a", "storage-c", "block-c"]},
        "rationale": "Understand whether the locations form a spatially meaningful cluster.",
    },
    {
        "thought": "Checking badge history because three Access Point A failures may be more than a tired worker.",
        "tool": "get_badge_history",
        "arguments": {"event_ids": ["evt-004", "evt-005", "evt-006"]},
        "rationale": "Access-control context decides whether the badge failures are noise or signal.",
    },
    {
        "thought": "Checking whether the drone actually covered Storage Yard C after the suspicious sequence.",
        "tool": "get_drone_patrols",
        "arguments": {"location_id": "storage-c"},
        "rationale": "Separate checked evidence from unresolved gaps.",
    },
    {
        "thought": "Scoring risk with false-alarm context and partial drone coverage included.",
        "tool": "calculate_risk",
        "arguments": {},
        "rationale": "Turn the correlated evidence into an escalation recommendation.",
    },
    {
        "thought": "Planning a follow-up sweep only for the unresolved high-value area.",
        "tool": "plan_follow_up_mission",
        "arguments": {"focus_locations": ["storage-c", "access-a", "gate-3"]},
        "rationale": "Give Maya a lightweight action, not another dashboard.",
    },
]


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _tool(name: str, arguments: dict[str, Any], rationale: str) -> ToolCall:
    result = call_tool(name, arguments)
    return ToolCall(name=name, arguments=arguments, result=result, rationale=rationale)


async def _gemini_json(prompt: str, timeout: int = 5) -> dict[str, Any] | None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
    except Exception:
        return None


async def _plan_with_gemini(review_note: str | None) -> list[dict[str, Any]]:
    tools = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in TOOLS.values()
    ]
    prompt = f"""
{INVESTIGATION_SYSTEM_PROMPT}

You are the investigation planner for Maya at Ridgeway Site.
Choose a short ReAct-style tool plan. Return JSON only:
{{"steps":[{{"thought":"...","tool":"search_events","arguments":{{...}},"rationale":"..."}}]}}

Rules:
- Use only these tools: {json.dumps(tools)}
- Include search_events first.
- Decide whether badge history, drone patrols, risk scoring, and follow-up mission planning are useful based on the evidence.
- Use get_badge_history for badge failures before calculate_risk.
- Do not invent data. The backend will execute tools.
- Prefer identifying false alarms and uncertainty, not just escalation.
- Human review note: {review_note or "none"}
"""
    data = await _gemini_json(prompt)
    steps = data.get("steps") if data else None
    if not isinstance(steps, list):
        return DEFAULT_PLAN

    validated = []
    for step in steps[:6]:
        if not isinstance(step, dict):
            continue
        name = step.get("tool")
        args = step.get("arguments") or {}
        if name not in TOOLS or not isinstance(args, dict):
            continue
        validated.append(
            {
                "thought": str(step.get("thought") or f"Using {name}."),
                "tool": name,
                "arguments": args,
                "rationale": str(step.get("rationale") or TOOLS[name].description),
            }
        )
    return validated or DEFAULT_PLAN


def _prepare_calculate_args(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    events = context.get("important_events", [])
    locations = context.get("site_context", {}).get("locations", [])
    patrols = context.get("drone_context", {}).get("patrols", [])
    badge_history = context.get("badge_context", {}).get("badge_histories", [])
    return {
        **arguments,
        "events": events,
        "location_weights": {location["id"]: location["risk_weight"] for location in locations},
        "drone_coverage": patrols[0]["coverage_quality"] if patrols else "low",
        "badge_history": badge_history,
    }


def _summarize_observation(tool: ToolCall) -> str:
    if tool.name == "search_events":
        count = tool.result["count"]
        return f"Found {count} warning/critical signals for review."
    if tool.name == "get_site_context":
        names = ", ".join(location["name"] for location in tool.result["locations"])
        return f"Mapped the cluster around {names}."
    if tool.name == "get_drone_patrols":
        patrols = tool.result["patrols"]
        if not patrols:
            return "No drone coverage found for the selected area."
        return f"Drone coverage exists but quality is {patrols[0]['coverage_quality']}."
    if tool.name == "get_badge_history":
        histories = tool.result["badge_histories"]
        if not histories:
            return "No badge identity context found for the access failures."
        return f"Badge context found: {histories[0]['badge_id']} had {histories[0]['failures_at_access_a_within_10min']} Access Point A failures."
    if tool.name == "calculate_risk":
        return f"Risk score {tool.result['score']} with {tool.result['confidence']}% confidence."
    if tool.name == "plan_follow_up_mission":
        return f"Proposed {tool.result['title']} in {tool.result['eta_minutes']} minutes."
    return "Tool result captured."


async def _synthesize_with_gemini(base: dict[str, Any], review_note: str | None) -> dict[str, Any] | None:
    prompt = f"""
{INVESTIGATION_SYSTEM_PROMPT}

You are Maya's assistant. Use only this structured evidence to produce final JSON.
Do not invent facts. Explicitly separate false alarms, unknowns, and escalation.

Evidence:
{json.dumps(base)}

Human review note:
{review_note or "none"}

Return JSON with:
headline, summary, harmless[], needs_escalation[], open_questions[].
"""
    return await _gemini_json(prompt)


async def build_investigation(
    review_note: str | None = None,
    status: str = "draft",
    finding_reviews: list[dict[str, Any]] | None = None,
) -> Investigation:
    steps, investigation = None, None
    async for event in run_investigation_stream(review_note, status, finding_reviews):
        if event["type"] == "complete":
            investigation = Investigation(**event["data"])
    if investigation is None:
        raise RuntimeError("Investigation did not complete")
    return investigation


async def run_investigation_stream(
    review_note: str | None = None,
    status: str = "draft",
    finding_reviews: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    reasoning_steps: list[ReasoningStep] = []
    tool_calls: list[ToolCall] = []
    context: dict[str, Any] = {}
    review_map = {item.get("finding_id"): item for item in (finding_reviews or [])}

    yield {"type": "thought", "data": {"text": "Creating a guarded Planner + ReAct investigation plan."}}
    await asyncio.sleep(0.08)
    plan = await _plan_with_gemini(review_note)

    for step in plan:
        thought = step["thought"]
        reasoning_steps.append(ReasoningStep(kind="thought", text=thought))
        yield {"type": "thought", "data": {"text": thought}}
        await asyncio.sleep(0.08)

        name = step["tool"]
        args = step.get("arguments") or {}
        if name == "calculate_risk":
            args = _prepare_calculate_args(args, context)

        tool_call = _tool(name, args, step["rationale"])
        tool_calls.append(tool_call)
        reasoning_steps.append(ReasoningStep(kind="tool", text=f"Called {name}.", tool_call=tool_call))
        yield {"type": "tool", "data": tool_call.model_dump()}
        await asyncio.sleep(0.08)

        if name == "search_events":
            context["important_events"] = tool_call.result["events"]
        elif name == "get_site_context":
            context["site_context"] = tool_call.result
        elif name == "get_drone_patrols":
            context["drone_context"] = tool_call.result
        elif name == "get_badge_history":
            context["badge_context"] = tool_call.result
        elif name == "calculate_risk":
            context["risk"] = tool_call.result
        elif name == "plan_follow_up_mission":
            context["follow_up"] = tool_call.result

        observation = _summarize_observation(tool_call)
        reasoning_steps.append(ReasoningStep(kind="observation", text=observation))
        yield {"type": "observation", "data": {"text": observation}}
        await asyncio.sleep(0.08)

    important_events = context.get("important_events", [])
    site_context = context.get("site_context", {"locations": [], "zones": []})
    drone_context = context.get("drone_context", {"patrols": []})
    risk = context.get("risk", {"confidence": 78, "escalation_level": "review"})
    badge_context = context.get("badge_context", {"badge_histories": []})
    follow_up = context.get("follow_up") or call_tool(
        "plan_follow_up_mission",
        {"focus_locations": ["storage-c", "access-a", "gate-3"]},
    )

    vehicle_time = _minutes("01:22")
    drone_time = _minutes(drone_context["patrols"][0]["started_at"]) if drone_context["patrols"] else _minutes("02:03")
    coverage_gap = {
        "location_id": "storage-c",
        "start": "01:22",
        "end": drone_context["patrols"][0]["started_at"] if drone_context["patrols"] else "02:03",
        "minutes": max(0, drone_time - vehicle_time),
        "label": "Unverified window before drone arrival",
    }

    findings = [
        Finding(
            id="f-access-cluster",
            title="Storage Yard C access sequence needs escalation",
            classification="ESCALATE",
            severity="critical",
            confidence=84,
            review_status=review_map.get("f-access-cluster", {}).get("status", "pending"),
            summary="Vehicle movement near Storage Yard C deviated from the known contractor route and was followed within minutes by three failed badge swipes at Access Point A.",
            evidence_event_ids=["evt-003", "evt-004", "evt-005", "evt-006"],
            location_ids=["storage-c", "access-a", "block-c"],
            uncertainty="I cannot confirm whether this was a contractor shortcut or an unknown individual until vehicle authorization and badge identity are checked.",
            supports_escalation=["Restricted yard", "repeated access failures", "Raghav asked Maya to check Block C"],
            supports_false_alarm=["No person detected by later drone pass"],
        ),
        Finding(
            id="f-gate-3",
            title="Gate 3 fence alert is plausible noise but not fully cleared",
            classification="NEEDS_REVIEW",
            severity="warning",
            confidence=61,
            review_status=review_map.get("f-gate-3", {}).get("status", "pending"),
            summary="Adjacent Gate 3 sensors and 34 km/h wind make this consistent with wind noise, but it happened seven minutes before the Storage Yard C vehicle signal.",
            evidence_event_ids=["evt-001", "evt-002"],
            location_ids=["gate-3"],
            uncertainty="There was no visual confirmation exactly at 01:15.",
            supports_escalation=["Alert occurred seven minutes before vehicle detection"],
            supports_false_alarm=["Weather station recorded wind gusts near the same perimeter"],
        ),
        Finding(
            id="f-cleared-noise",
            title="Warehouse and South Yard signals look harmless",
            classification="CLEARED",
            severity="info",
            confidence=92,
            review_status=review_map.get("f-cleared-noise", {}).get("status", "pending"),
            summary="Warehouse 2 and South Yard activity match expected cleaning and contractor patterns.",
            evidence_event_ids=["evt-009", "evt-010"],
            location_ids=["warehouse-2", "south-yard"],
            uncertainty="Low. These do not spatially or temporally connect to Block C.",
            supports_escalation=[],
            supports_false_alarm=["Cleaning window matched", "contractor check-in matched expected staging"],
        ),
    ]

    base = {
        "important_events": important_events,
        "drone": drone_context,
        "badge": badge_context,
        "risk": risk,
        "coverage_gap": coverage_gap,
        "findings": [finding.model_dump() for finding in findings],
    }
    ai_synthesis = await _synthesize_with_gemini(base, review_note)

    headline = "Escalate Storage Yard C, clear routine noise"
    summary = (
        "The agent found a meaningful cluster around Storage Yard C: a vehicle path, repeated badge failures, "
        "and a later drone pass with medium coverage. I cannot confirm whether this was a contractor or intruder yet. "
        "Warehouse 2 and South Yard look like false alarms because they match expected activity."
    )
    harmless = [
        "Warehouse 2 service door activity matches the cleaning window.",
        "South Yard contractor check-in matches expected staging.",
        "Gate 3 has weather context, but remains tied to the open question.",
    ]
    needs_escalation = [
        "Confirm vehicle authorization for Storage Yard C.",
        "Identify the failed badge attempts at Access Point A.",
        "Run the follow-up drone sweep over the unverified window area.",
    ]
    open_questions = [
        "Was the vehicle authorized for Storage Yard C?",
        "Who attempted the three failed badge swipes?",
        f"What happened during the {coverage_gap['minutes']}-minute gap before the drone arrived?",
    ]

    if ai_synthesis:
        headline = ai_synthesis.get("headline") or headline
        summary = ai_synthesis.get("summary") or summary
        harmless = ai_synthesis.get("harmless") or harmless
        needs_escalation = ai_synthesis.get("needs_escalation") or needs_escalation
        open_questions = ai_synthesis.get("open_questions") or open_questions

    if review_note:
        summary = f"{summary} Maya context: {review_note}"

    final_step = "Synthesized escalation, false alarms, uncertainty, and a follow-up drone route."
    reasoning_steps.append(ReasoningStep(kind="summary", text=final_step))
    yield {"type": "summary", "data": {"text": final_step}}

    investigation = Investigation(
        status=status,  # type: ignore[arg-type]
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        headline=headline,
        confidence=risk["confidence"],
        escalation_level=risk["escalation_level"],
        summary=summary,
        harmless=harmless,
        needs_escalation=needs_escalation,
        drone_checked=["Block C corridor", "Storage Yard C perimeter", "Gate 3 fence line"],
        open_questions=open_questions,
        findings=findings,
        follow_up_mission=FollowUpMission(**follow_up),
        tool_calls=tool_calls,
        reasoning_steps=reasoning_steps,
        coverage_gap=coverage_gap,
        review_note=review_note,
    )
    yield {"type": "complete", "data": investigation.model_dump()}
