from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from db import rows
from models import ToolDefinition


def _time_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass
class McpStyleTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    def call(self, arguments: dict[str, Any]) -> Any:
        return self.handler(arguments)


EVENT_CONTEXT: dict[str, dict[str, Any]] = {
    "evt-002": {
        "adjacent_sensors_triggered": ["FENCE-G3-03", "FENCE-G3-05"],
        "wind_speed_kmh": 34,
        "vehicle_detected_within_15min": True,
        "historical_note": "Gate 3 fence vibration has matched wind on 6 previous windy nights, but this alert sits near the Block C sequence.",
        "noise_signal": "partial_noise",
    },
    "evt-003": {
        "matches_contractor_schedule": True,
        "contractor_id": "NLC-LOGISTICS-TUE",
        "registered_route": "gate2_to_yard_c_to_gate4",
        "deviation_from_route_meters": 180,
        "day_of_week": "Tuesday",
        "historical_note": "Tuesday logistics contractors pass Storage Yard C, but deviations over 100m are treated as abnormal.",
        "noise_signal": "needs_review",
    },
    "evt-004": {
        "badge_id": "EMP-2847",
        "failures_at_this_point_within_10min": 3,
        "historical_note": "Three failures at one reader within 10 minutes is the threshold for forced-entry or tailgating review.",
        "noise_signal": "escalate",
    },
}


BADGE_HISTORY: dict[str, dict[str, Any]] = {
    "EMP-2847": {
        "badge_id": "EMP-2847",
        "employee_name": "R. Sharma",
        "employee_shift": "night",
        "last_successful_swipe": "gate-2",
        "last_successful_swipe_at": "01:02",
        "expected_zone": "loading-bay",
        "failures_at_access_a_within_10min": 3,
        "classification_hint": "NEEDS_REVIEW",
        "note": "Employee should be on site, but the last successful swipe was on the wrong side of the facility.",
    }
}


def search_events(arguments: dict[str, Any]) -> dict[str, Any]:
    events = rows("select * from events order by timestamp")
    severities = set(arguments.get("severities") or ["warning", "critical"])
    location_ids = set(arguments.get("location_ids") or [])
    since = arguments.get("since")
    until = arguments.get("until")

    filtered = []
    for event in events:
        if event["severity"] not in severities:
            continue
        if location_ids and event["location_id"] not in location_ids:
            continue
        if since and _time_to_minutes(event["timestamp"]) < _time_to_minutes(since):
            continue
        if until and _time_to_minutes(event["timestamp"]) > _time_to_minutes(until):
            continue
        event["is_false_alarm"] = bool(event["is_false_alarm"])
        event["context"] = EVENT_CONTEXT.get(event["id"], {})
        filtered.append(event)

    return {"events": filtered, "count": len(filtered)}


def get_site_context(arguments: dict[str, Any]) -> dict[str, Any]:
    site_rows = rows("select * from locations")
    zone_rows = rows("select * from zones")
    requested = set(arguments.get("location_ids") or [])
    locations = site_rows
    if requested:
        locations = [location for location in locations if location["id"] in requested]
    zones = [{**zone, "points": json.loads(zone["points"])} for zone in zone_rows]
    return {"locations": locations, "zones": zones}


def get_drone_patrols(arguments: dict[str, Any]) -> dict[str, Any]:
    patrols = rows("select * from drone_patrols")
    patrols = [
        {**patrol, "route": json.loads(patrol["route"]), "observations": json.loads(patrol["observations"])}
        for patrol in patrols
    ]
    location_id = arguments.get("location_id")
    if location_id:
        patrols = [patrol for patrol in patrols if location_id in patrol["route"]]
    return {"patrols": patrols, "count": len(patrols)}


def get_badge_history(arguments: dict[str, Any]) -> dict[str, Any]:
    badge_ids = set(arguments.get("badge_ids") or [])
    event_ids = set(arguments.get("event_ids") or [])
    if event_ids:
        for event_id in event_ids:
            badge_id = EVENT_CONTEXT.get(event_id, {}).get("badge_id")
            if badge_id:
                badge_ids.add(badge_id)

    histories = [history for badge_id, history in BADGE_HISTORY.items() if not badge_ids or badge_id in badge_ids]
    return {"badge_histories": histories, "count": len(histories)}


def calculate_risk(arguments: dict[str, Any]) -> dict[str, Any]:
    events = arguments.get("events", [])
    location_weights = arguments.get("location_weights", {})
    drone_coverage = arguments.get("drone_coverage", "low")
    badge_history = arguments.get("badge_history", [])

    severity_score = {"info": 1, "warning": 4, "critical": 8}
    score = 0
    reasons = []

    for event in events:
        if event.get("is_false_alarm"):
            score -= 2
            reasons.append(f"{event['timestamp']} {event['type']} has false-alarm context")
            continue
        weight = location_weights.get(event["location_id"], 1)
        event_score = severity_score.get(event["severity"], 1) * weight
        score += event_score
        reasons.append(f"{event['timestamp']} {event['type']} at {event['location_id']} added {event_score} risk points")

    badge_failures = [event for event in events if event["type"] == "badge_failure"]
    if len(badge_failures) >= 3:
        score += 12
        reasons.append("Three failed badge swipes within five minutes increased risk")

    if badge_history:
        for record in badge_history:
            if record.get("failures_at_access_a_within_10min", 0) >= 3:
                score += 10
                reasons.append(f"{record['badge_id']} has three Access Point A failures and a last successful swipe at {record['last_successful_swipe']}")

    has_vehicle = any(event["type"] == "vehicle_detection" for event in events)
    has_access = bool(badge_failures)
    if has_vehicle and has_access:
        score += 16
        reasons.append("Vehicle detection and repeated access failures are temporally correlated")

    if drone_coverage == "medium":
        score -= 8
        reasons.append("Drone patrol partially reduced risk but did not close uncertainty")
    elif drone_coverage == "high":
        score -= 14
        reasons.append("High-quality drone coverage reduced risk")

    score = max(score, 0)
    confidence = min(92, 55 + score // 2)
    escalation = "monitor"
    if score >= 65:
        escalation = "escalate"
    elif score >= 35:
        escalation = "review"

    return {
        "score": score,
        "confidence": confidence,
        "escalation_level": escalation,
        "reasons": reasons,
    }


def plan_follow_up_mission(arguments: dict[str, Any]) -> dict[str, Any]:
    requested = arguments.get("focus_locations") or []
    focus_locations = list(dict.fromkeys(location for location in requested if location != "control-room"))
    location_rows = rows("select id, name, risk_weight from locations")
    locations = {location["id"]: location for location in location_rows}
    focus_locations = [location for location in focus_locations if location in locations]
    if not focus_locations:
        return {
            "title": "No follow-up mission required",
            "reason": "The current findings do not contain unresolved locations.",
            "route": ["control-room"],
            "eta_minutes": 0,
            "priority": "low",
        }

    names = [locations[location]["name"] for location in focus_locations]
    highest_weight = max(locations[location]["risk_weight"] for location in focus_locations)
    priority = "high" if highest_weight >= 4 else "medium"
    route = ["control-room", *focus_locations, "control-room"]
    return {
        "title": "Follow-up verification sweep",
        "reason": f"Close remaining uncertainty around {', '.join(names)}.",
        "route": route,
        "eta_minutes": 6 + (4 * len(focus_locations)),
        "priority": priority,
    }


TOOLS: dict[str, McpStyleTool] = {
    "search_events": McpStyleTool(
        name="search_events",
        description="Search overnight site events by severity, location, and time window.",
        input_schema={
            "type": "object",
            "properties": {
                "severities": {"type": "array", "items": {"type": "string"}},
                "location_ids": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string"},
                "until": {"type": "string"},
            },
        },
        handler=search_events,
    ),
    "get_site_context": McpStyleTool(
        name="get_site_context",
        description="Fetch site locations, risk notes, and zone geometry for spatial reasoning.",
        input_schema={
            "type": "object",
            "properties": {"location_ids": {"type": "array", "items": {"type": "string"}}},
        },
        handler=get_site_context,
    ),
    "get_drone_patrols": McpStyleTool(
        name="get_drone_patrols",
        description="Find drone patrols and observations for a site location.",
        input_schema={
            "type": "object",
            "properties": {"location_id": {"type": "string"}},
        },
        handler=get_drone_patrols,
    ),
    "get_badge_history": McpStyleTool(
        name="get_badge_history",
        description="Fetch personnel and badge context for access-control failures.",
        input_schema={
            "type": "object",
            "properties": {
                "badge_ids": {"type": "array", "items": {"type": "string"}},
                "event_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        handler=get_badge_history,
    ),
    "calculate_risk": McpStyleTool(
        name="calculate_risk",
        description="Calculate risk, confidence, and escalation level from correlated evidence.",
        input_schema={
            "type": "object",
            "properties": {
                "events": {"type": "array"},
                "location_weights": {"type": "object"},
                "drone_coverage": {"type": "string"},
                "badge_history": {"type": "array"},
            },
            "required": ["events"],
        },
        handler=calculate_risk,
    ),
    "plan_follow_up_mission": McpStyleTool(
        name="plan_follow_up_mission",
        description="Create a lightweight drone follow-up route for unresolved locations.",
        input_schema={
            "type": "object",
            "properties": {"focus_locations": {"type": "array", "items": {"type": "string"}}},
        },
        handler=plan_follow_up_mission,
    ),
}


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name not in TOOLS:
        raise KeyError(f"Unknown tool: {name}")
    return TOOLS[name].call(arguments)


def list_tools() -> list[ToolDefinition]:
    return [tool.definition() for tool in TOOLS.values()]
