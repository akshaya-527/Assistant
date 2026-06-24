from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
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

Your job is to investigate current tool evidence, not to reproduce a known scenario.
Classify evidence as CLEARED, NEEDS_REVIEW, or ESCALATE.

Rules:
- Cite only event IDs and locations present in the supplied evidence.
- Never claim certainty about an intruder. Use unknown individual, contractor, vehicle, or activity.
- Never say definitely harmless. Explain which evidence matches a normal pattern.
- Three or more badge failures at one point within ten minutes requires NEEDS_REVIEW at minimum.
- State whether later drone coverage closes or leaves an evidence gap.
- State what is known, unknown, and what Maya should verify.
- Every input event must be represented in at least one finding.
- Return JSON only.
"""


class AgentUnavailable(RuntimeError):
    """Raised when Gemini cannot produce a trustworthy investigation."""


class AgentOutputError(AgentUnavailable):
    """Raised when Gemini output fails structural or evidence validation."""


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _tool(name: str, arguments: dict[str, Any], rationale: str) -> ToolCall:
    result = call_tool(name, arguments)
    return ToolCall(name=name, arguments=arguments, result=result, rationale=rationale)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Gemini returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Gemini request timed out"
    if isinstance(exc, httpx.TransportError):
        return "Gemini network request failed"
    return "Gemini returned an invalid response"


async def _gemini_json(
    prompt: str,
    purpose: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise AgentUnavailable(
            "Gemini is not configured. Add GEMINI_API_KEY and retry the investigation."
        )

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    request_timeout = timeout or float(os.getenv("GEMINI_TIMEOUT_SECONDS", "30"))
    max_attempts = max(1, int(os.getenv("GEMINI_MAX_ATTEMPTS", "2")))
    base_delay = max(0.1, float(os.getenv("GEMINI_RETRY_BASE_SECONDS", "0.75")))
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    last_error = "unknown Gemini failure"
    attempts_made = 0
    for attempt in range(max_attempts):
        attempts_made = attempt + 1
        retryable = False
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("Gemini response is not an object")
            return data
        except httpx.HTTPStatusError as exc:
            last_error = _safe_error(exc)
            retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            retry_after = exc.response.headers.get("retry-after")
            if retry_after:
                try:
                    base_delay = max(base_delay, float(retry_after))
                except ValueError:
                    pass
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = _safe_error(exc)
            retryable = True
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = _safe_error(exc)
            retryable = attempt + 1 < max_attempts

        if not retryable or attempt + 1 >= max_attempts:
            break
        delay = (base_delay * (2**attempt)) + random.uniform(0, base_delay / 3)
        await asyncio.sleep(delay)

    raise AgentUnavailable(
        f"Gemini could not complete {purpose} after {attempts_made} attempt(s): "
        f"{last_error}. Retry when the model is available."
    )


async def _plan_with_gemini(
    events: list[dict[str, Any]],
    review_note: str | None,
) -> list[dict[str, Any]]:
    available = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for name, tool in TOOLS.items()
        if name not in {"search_events", "plan_follow_up_mission"}
    ]
    event_digest = [
        {
            "id": event["id"],
            "timestamp": event["timestamp"],
            "type": event["type"],
            "location_id": event["location_id"],
            "severity": event["severity"],
            "description": event["description"],
            "context": event.get("context", {}),
        }
        for event in events
    ]
    prompt = f"""
{INVESTIGATION_SYSTEM_PROMPT}

The mandatory event-search tool has returned:
{json.dumps(event_digest)}

Choose the contextual tools required before classification.
Available tools:
{json.dumps(available)}

Return:
{{"steps":[{{"thought":"auditable reason","tool":"tool_name","arguments":{{}},"rationale":"why this evidence is needed"}}]}}

Evidence policy:
- Include get_site_context for all observed location IDs.
- If badge failures exist, include get_badge_history with their event IDs.
- If warning or critical events exist, include get_drone_patrols.
- Include calculate_risk after the contextual tools.
- Use each tool at most once.
- Do not include search_events or plan_follow_up_mission.
- Maximum five steps.
- Maya's note: {review_note or "none"}
"""
    data = await _gemini_json(prompt, "tool planning")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise AgentOutputError("Gemini tool plan did not contain a steps array.")

    validated: list[dict[str, Any]] = []
    used: set[str] = set()
    for step in steps[:5]:
        if not isinstance(step, dict):
            raise AgentOutputError("Gemini tool plan contained a malformed step.")
        name = step.get("tool")
        arguments = step.get("arguments") or {}
        if name not in TOOLS or name in {"search_events", "plan_follow_up_mission"}:
            raise AgentOutputError(f"Gemini selected an unavailable tool: {name}.")
        if name in used or not isinstance(arguments, dict):
            raise AgentOutputError("Gemini tool plan contained duplicate or invalid arguments.")
        used.add(name)
        validated.append(
            {
                "thought": str(step.get("thought") or f"Gathering evidence with {name}."),
                "tool": name,
                "arguments": arguments,
                "rationale": str(step.get("rationale") or TOOLS[name].description),
            }
        )

    required = {"get_site_context", "calculate_risk"}
    if any(event["type"] == "badge_failure" for event in events):
        required.add("get_badge_history")
    if any(event["severity"] in {"warning", "critical"} for event in events):
        required.add("get_drone_patrols")
    missing = required - used
    if missing:
        raise AgentOutputError(
            f"Gemini tool plan omitted required evidence tools: {', '.join(sorted(missing))}."
        )

    positions = {step["tool"]: index for index, step in enumerate(validated)}
    risk_position = positions["calculate_risk"]
    if any(positions[name] > risk_position for name in required - {"calculate_risk"}):
        raise AgentOutputError("Gemini attempted risk calculation before gathering context.")
    return validated


def _prepare_calculate_args(
    arguments: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    events = context.get("events", [])
    locations = context.get("site_context", {}).get("locations", [])
    patrols = context.get("drone_context", {}).get("patrols", [])
    badge_history = context.get("badge_context", {}).get("badge_histories", [])
    return {
        **arguments,
        "events": events,
        "location_weights": {
            location["id"]: location["risk_weight"] for location in locations
        },
        "drone_coverage": patrols[0]["coverage_quality"] if patrols else "low",
        "badge_history": badge_history,
    }


def _summarize_observation(tool: ToolCall) -> str:
    if tool.name == "search_events":
        return f"Found {tool.result['count']} overnight signals across all severities."
    if tool.name == "get_site_context":
        return f"Loaded spatial context for {len(tool.result['locations'])} locations."
    if tool.name == "get_drone_patrols":
        return f"Found {tool.result['count']} relevant drone patrol record(s)."
    if tool.name == "get_badge_history":
        return f"Found {tool.result['count']} badge-history record(s)."
    if tool.name == "calculate_risk":
        return (
            f"Evidence risk score is {tool.result['score']} with "
            f"{tool.result['confidence']}% evidence confidence."
        )
    return "Structured evidence captured."


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _events_related(
    left: dict[str, Any],
    right: dict[str, Any],
    locations: dict[str, dict[str, Any]],
) -> bool:
    delta = abs(_minutes(left["timestamp"]) - _minutes(right["timestamp"]))
    if left["location_id"] == right["location_id"] and delta <= 75:
        return True
    left_location = locations.get(left["location_id"])
    right_location = locations.get(right["location_id"])
    spatially_close = bool(
        left_location
        and right_location
        and _distance(left_location, right_location) <= 35
    )
    meaningful_signal = (
        left["severity"] in {"warning", "critical"}
        or right["severity"] in {"warning", "critical"}
    )
    return delta <= 20 and spatially_close and meaningful_signal


def _coverage_gap_for_events(
    events: list[dict[str, Any]],
    patrols: list[dict[str, Any]],
    locations: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        event
        for event in events
        if event["severity"] in {"warning", "critical"}
        and not event.get("is_false_alarm")
        and event["type"] not in {"drone_patrol", "thermal_clear"}
    ]
    if not candidates:
        return None
    severity_rank = {"warning": 1, "critical": 2}
    event = max(
        candidates,
        key=lambda item: (
            severity_rank[item["severity"]],
            locations.get(item["location_id"], {}).get("risk_weight", 1),
            -_minutes(item["timestamp"]),
        ),
    )
    event_time = _minutes(event["timestamp"])
    later_patrols = [
        patrol
        for patrol in patrols
        if event["location_id"] in patrol["route"]
        and _minutes(patrol["started_at"]) >= event_time
    ]
    if later_patrols:
        patrol = min(later_patrols, key=lambda item: _minutes(item["started_at"]))
        end = patrol["started_at"]
        label = "Unverified window before drone arrival"
    else:
        end = "06:10"
        label = "No later drone coverage before morning review"
    return {
        "location_id": event["location_id"],
        "start": event["timestamp"],
        "end": end,
        "minutes": max(0, _minutes(end) - event_time),
        "label": label,
    }


def _evidence_confidence(
    events: list[dict[str, Any]],
    coverage_gap: dict[str, Any] | None,
) -> int:
    if not events:
        return 0
    base = sum(float(event.get("confidence", 0.5)) for event in events) / len(events)
    sources = len({event.get("actor") for event in events if event.get("actor")})
    corroboration_bonus = min(0.1, max(0, sources - 1) * 0.025)
    gap_penalty = 0.08 if coverage_gap and coverage_gap["minutes"] > 30 else 0
    return round(max(0.25, min(0.95, base + corroboration_bonus - gap_penalty)) * 100)


def build_incident_candidates(
    events: list[dict[str, Any]],
    site_context: dict[str, Any],
    drone_context: dict[str, Any],
    badge_context: dict[str, Any],
) -> list[dict[str, Any]]:
    locations = {
        location["id"]: location for location in site_context.get("locations", [])
    }
    patrols = drone_context.get("patrols", [])
    parents = list(range(len(events)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(events)):
        for right in range(left + 1, len(events)):
            if _events_related(events[left], events[right], locations):
                union(left, right)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, event in enumerate(events):
        groups.setdefault(find(index), []).append(event)

    candidates: list[dict[str, Any]] = []
    all_badge_history = badge_context.get("badge_histories", [])
    location_weights = {
        location_id: location.get("risk_weight", 1)
        for location_id, location in locations.items()
    }
    for grouped_events in groups.values():
        grouped_events = sorted(grouped_events, key=lambda event: event["timestamp"])
        location_ids = list(
            dict.fromkeys(event["location_id"] for event in grouped_events)
        )
        relevant_patrols = [
            patrol
            for patrol in patrols
            if any(location_id in patrol["route"] for location_id in location_ids)
        ]
        badge_failures = [
            event for event in grouped_events if event["type"] == "badge_failure"
        ]
        relevant_badges = all_badge_history if len(badge_failures) >= 3 else []
        gap = _coverage_gap_for_events(grouped_events, relevant_patrols, locations)
        risk = call_tool(
            "calculate_risk",
            {
                "events": grouped_events,
                "location_weights": location_weights,
                "drone_coverage": (
                    relevant_patrols[0]["coverage_quality"]
                    if relevant_patrols
                    else "low"
                ),
                "badge_history": relevant_badges,
            },
        )
        event_ids = [event["id"] for event in grouped_events]
        candidate_id = "candidate-" + hashlib.sha1(
            "|".join(sorted(event_ids)).encode("utf-8")
        ).hexdigest()[:10]
        candidates.append(
            {
                "id": candidate_id,
                "events": grouped_events,
                "location_ids": location_ids,
                "locations": [
                    locations[location_id]
                    for location_id in location_ids
                    if location_id in locations
                ],
                "badge_history": relevant_badges,
                "drone_patrols": relevant_patrols,
                "coverage_gap": gap,
                "risk": risk,
                "evidence_confidence": _evidence_confidence(grouped_events, gap),
            }
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate["risk"]["score"],
            candidate["events"][0]["timestamp"],
        ),
    )


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AgentOutputError(f"Gemini finding field {field} must be a string array.")
    return [item.strip() for item in value]


def _replace_unsafe_terms(value: str) -> str:
    return value.replace("intruder", "unknown individual").replace(
        "Intruder", "Unknown individual"
    )


def _confidence_for_finding(
    evidence_events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> int:
    evidence_ids = {event["id"] for event in evidence_events}
    related = max(
        candidates,
        key=lambda candidate: len(
            evidence_ids & {event["id"] for event in candidate["events"]}
        ),
    )
    gap = related.get("coverage_gap")
    return _evidence_confidence(evidence_events, gap)


def validate_generated_investigation(
    raw: dict[str, Any],
    events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    review_map: dict[str, dict[str, Any]],
) -> tuple[list[Finding], str, str, list[str]]:
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list):
        raise AgentOutputError("Gemini investigation did not contain findings.")
    if events and not raw_findings:
        raise AgentOutputError("Gemini returned no findings for non-empty evidence.")

    events_by_id = {event["id"]: event for event in events}
    allowed_ids = set(events_by_id)
    covered_ids: set[str] = set()
    findings: list[Finding] = []
    severity_for = {
        "CLEARED": "info",
        "NEEDS_REVIEW": "warning",
        "ESCALATE": "critical",
    }

    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise AgentOutputError("Gemini returned a malformed finding.")
        classification = item.get("classification")
        if classification not in severity_for:
            raise AgentOutputError("Gemini returned an invalid classification.")
        evidence_ids = _string_list(item.get("evidence_event_ids"), "evidence_event_ids")
        if not evidence_ids or not set(evidence_ids) <= allowed_ids:
            raise AgentOutputError("Gemini cited missing or invented event evidence.")
        covered_ids.update(evidence_ids)
        evidence_events = [events_by_id[event_id] for event_id in evidence_ids]
        location_ids = list(
            dict.fromkeys(event["location_id"] for event in evidence_events)
        )
        finding_id = "f-" + hashlib.sha1(
            ("|".join(sorted(evidence_ids)) + f"|{index}").encode("utf-8")
        ).hexdigest()[:10]
        title = _replace_unsafe_terms(str(item.get("title") or "").strip())
        summary = _replace_unsafe_terms(str(item.get("summary") or "").strip())
        uncertainty = _replace_unsafe_terms(
            str(item.get("uncertainty") or "").strip()
        )
        action = _replace_unsafe_terms(
            str(item.get("recommended_action") or "").strip()
        )
        if not all([title, summary, uncertainty, action]):
            raise AgentOutputError("Gemini omitted required finding text.")
        findings.append(
            Finding(
                id=finding_id,
                title=title,
                classification=classification,
                severity=severity_for[classification],
                confidence=_confidence_for_finding(
                    evidence_events,
                    candidates,
                ),
                review_status=review_map.get(finding_id, {}).get("status", "pending"),
                summary=summary,
                evidence_event_ids=evidence_ids,
                location_ids=location_ids,
                uncertainty=uncertainty,
                recommended_action=action,
                supports_escalation=_string_list(
                    item.get("supports_escalation", []),
                    "supports_escalation",
                ),
                supports_false_alarm=_string_list(
                    item.get("supports_false_alarm", []),
                    "supports_false_alarm",
                ),
            )
        )

    missing = allowed_ids - covered_ids
    if missing:
        raise AgentOutputError(
            f"Gemini omitted event evidence: {', '.join(sorted(missing))}."
        )
    headline = _replace_unsafe_terms(str(raw.get("headline") or "").strip())
    summary = _replace_unsafe_terms(str(raw.get("summary") or "").strip())
    if not headline or not summary:
        raise AgentOutputError("Gemini omitted the briefing headline or summary.")
    open_questions = _string_list(raw.get("open_questions", []), "open_questions")
    return findings, headline, summary, open_questions


async def _generate_investigation_with_gemini(
    candidates: list[dict[str, Any]],
    events: list[dict[str, Any]],
    review_note: str | None,
) -> dict[str, Any]:
    prompt = f"""
{INVESTIGATION_SYSTEM_PROMPT}

Current incident candidates generated from tool evidence:
{json.dumps(candidates)}

Maya's context:
{review_note or "none"}

Produce the morning investigation. You may merge or split candidates, but every input event ID
must appear in at least one finding. Do not copy risk score into confidence; the backend derives
confidence from evidence quality.

Return:
{{
  "headline": "short decision headline",
  "summary": "what happened, what is harmless, and what remains uncertain",
  "open_questions": ["question requiring verification"],
  "findings": [
    {{
      "title": "finding title",
      "classification": "CLEARED|NEEDS_REVIEW|ESCALATE",
      "summary": "evidence-based finding",
      "uncertainty": "what cannot be verified",
      "recommended_action": "specific next action or no action required",
      "evidence_event_ids": ["existing event id"],
      "supports_escalation": ["specific evidence"],
      "supports_false_alarm": ["specific evidence"]
    }}
  ]
}}
"""
    finding_timeout = float(os.getenv("GEMINI_FINDING_TIMEOUT_SECONDS", "90"))
    return await _gemini_json(
        prompt,
        "finding generation",
        timeout=finding_timeout,
    )


def _dynamic_coverage_gap(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    candidates_with_gap = [
        candidate for candidate in candidates if candidate.get("coverage_gap")
    ]
    if not candidates_with_gap:
        return {
            "location_id": "",
            "start": "",
            "end": "",
            "minutes": 0,
            "label": "No unresolved drone coverage gap",
        }
    candidate = max(
        candidates_with_gap,
        key=lambda item: (item["risk"]["score"], item["coverage_gap"]["minutes"]),
    )
    return candidate["coverage_gap"]


def _drone_checked_locations(
    drone_context: dict[str, Any],
    site_context: dict[str, Any],
) -> list[str]:
    names = {
        location["id"]: location["name"]
        for location in site_context.get("locations", [])
    }
    checked: list[str] = []
    for patrol in drone_context.get("patrols", []):
        for location_id in patrol["route"]:
            if location_id != "control-room" and location_id in names:
                checked.append(names[location_id])
    return list(dict.fromkeys(checked))


async def build_investigation(
    review_note: str | None = None,
    status: str = "draft",
    finding_reviews: list[dict[str, Any]] | None = None,
) -> Investigation:
    investigation: Investigation | None = None
    async for event in run_investigation_stream(review_note, status, finding_reviews):
        if event["type"] == "complete":
            investigation = Investigation(**event["data"])
    if investigation is None:
        raise AgentUnavailable("The investigation did not produce a result.")
    return investigation


async def run_investigation_stream(
    review_note: str | None = None,
    status: str = "draft",
    finding_reviews: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    reasoning_steps: list[ReasoningStep] = []
    tool_calls: list[ToolCall] = []
    review_map = {
        item.get("finding_id"): item for item in (finding_reviews or [])
    }

    search_thought = "Searching every overnight signal before deciding which context tools are needed."
    reasoning_steps.append(ReasoningStep(kind="thought", text=search_thought))
    yield {"type": "thought", "data": {"text": search_thought}}
    search_call = _tool(
        "search_events",
        {
            "severities": ["info", "warning", "critical"],
            "since": "00:00",
            "until": "06:10",
        },
        "Discover the complete overnight evidence set.",
    )
    tool_calls.append(search_call)
    reasoning_steps.append(
        ReasoningStep(kind="tool", text="Called search_events.", tool_call=search_call)
    )
    yield {"type": "tool", "data": search_call.model_dump()}
    search_observation = _summarize_observation(search_call)
    reasoning_steps.append(
        ReasoningStep(kind="observation", text=search_observation)
    )
    yield {"type": "observation", "data": {"text": search_observation}}

    events = search_call.result["events"]
    plan = await _plan_with_gemini(events, review_note)
    context: dict[str, Any] = {"events": events}

    for step in plan:
        thought = step["thought"]
        reasoning_steps.append(ReasoningStep(kind="thought", text=thought))
        yield {"type": "thought", "data": {"text": thought}}
        name = step["tool"]
        arguments = step["arguments"]
        if name == "calculate_risk":
            arguments = _prepare_calculate_args(arguments, context)
        tool_call = _tool(name, arguments, step["rationale"])
        tool_calls.append(tool_call)
        reasoning_steps.append(
            ReasoningStep(kind="tool", text=f"Called {name}.", tool_call=tool_call)
        )
        yield {"type": "tool", "data": tool_call.model_dump()}
        if name == "get_site_context":
            context["site_context"] = tool_call.result
        elif name == "get_badge_history":
            context["badge_context"] = tool_call.result
        elif name == "get_drone_patrols":
            context["drone_context"] = tool_call.result
        elif name == "calculate_risk":
            context["risk"] = tool_call.result
        observation = _summarize_observation(tool_call)
        reasoning_steps.append(ReasoningStep(kind="observation", text=observation))
        yield {"type": "observation", "data": {"text": observation}}

    site_context = context.get("site_context", {"locations": [], "zones": []})
    observed_locations = {event["location_id"] for event in events}
    loaded_locations = {
        location["id"] for location in site_context.get("locations", [])
    }
    if not observed_locations <= loaded_locations:
        raise AgentOutputError(
            "Gemini did not request spatial context for every observed location."
        )
    drone_context = context.get("drone_context", {"patrols": []})
    badge_context = context.get("badge_context", {"badge_histories": []})
    candidates = build_incident_candidates(
        events,
        site_context,
        drone_context,
        badge_context,
    )

    synthesis_thought = (
        f"Correlated {len(events)} events into {len(candidates)} evidence-driven "
        "candidate(s); asking Gemini to classify only cited evidence."
    )
    reasoning_steps.append(ReasoningStep(kind="thought", text=synthesis_thought))
    yield {"type": "thought", "data": {"text": synthesis_thought}}
    raw = await _generate_investigation_with_gemini(candidates, events, review_note)
    findings, headline, summary, open_questions = validate_generated_investigation(
        raw,
        events,
        candidates,
        review_map,
    )

    unresolved_locations = list(
        dict.fromkeys(
            location_id
            for finding in findings
            if finding.classification != "CLEARED"
            for location_id in finding.location_ids
        )
    )
    mission_call = _tool(
        "plan_follow_up_mission",
        {"focus_locations": unresolved_locations},
        "Build a route only from locations in unresolved generated findings.",
    )
    tool_calls.append(mission_call)
    reasoning_steps.append(
        ReasoningStep(
            kind="tool",
            text="Called plan_follow_up_mission.",
            tool_call=mission_call,
        )
    )
    yield {"type": "tool", "data": mission_call.model_dump()}
    mission_observation = (
        f"Generated a follow-up route for {len(unresolved_locations)} unresolved "
        "location(s)."
    )
    reasoning_steps.append(
        ReasoningStep(kind="observation", text=mission_observation)
    )
    yield {"type": "observation", "data": {"text": mission_observation}}

    harmless = [
        finding.summary
        for finding in findings
        if finding.classification == "CLEARED"
    ]
    needs_escalation = [
        finding.recommended_action
        for finding in findings
        if finding.classification in {"NEEDS_REVIEW", "ESCALATE"}
    ]
    if any(finding.classification == "ESCALATE" for finding in findings):
        escalation_level = "escalate"
    elif any(finding.classification == "NEEDS_REVIEW" for finding in findings):
        escalation_level = "review"
    else:
        escalation_level = "monitor"
    confidence = (
        round(sum(finding.confidence for finding in findings) / len(findings))
        if findings
        else 0
    )
    final_step = (
        "Validated every generated finding against current event IDs and built "
        "the briefing from those findings."
    )
    reasoning_steps.append(ReasoningStep(kind="summary", text=final_step))
    yield {"type": "summary", "data": {"text": final_step}}

    investigation = Investigation(
        status=status,  # type: ignore[arg-type]
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        headline=headline,
        confidence=confidence,
        escalation_level=escalation_level,
        summary=summary,
        harmless=harmless,
        needs_escalation=needs_escalation,
        drone_checked=_drone_checked_locations(drone_context, site_context),
        open_questions=open_questions,
        findings=findings,
        follow_up_mission=FollowUpMission(**mission_call.result),
        tool_calls=tool_calls,
        reasoning_steps=reasoning_steps,
        coverage_gap=_dynamic_coverage_gap(candidates),
        review_note=review_note,
    )
    yield {"type": "complete", "data": investigation.model_dump()}
