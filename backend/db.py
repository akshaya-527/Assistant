from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "assistant.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_json(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists locations (
                id text primary key,
                name text not null,
                x real not null,
                y real not null,
                type text not null,
                risk_weight integer not null,
                notes text not null
            );
            create table if not exists zones (
                id text primary key,
                name text not null,
                points text not null,
                risk text not null
            );
            create table if not exists events (
                id text primary key,
                timestamp text not null,
                type text not null,
                location_id text not null,
                severity text not null,
                description text not null,
                actor text,
                confidence real not null,
                is_false_alarm integer not null default 0,
                false_alarm_reason text not null default ''
            );
            create table if not exists drone_patrols (
                id text primary key,
                started_at text not null,
                ended_at text not null,
                route text not null,
                observations text not null,
                coverage_quality text not null
            );
            create table if not exists reviews (
                id integer primary key autoincrement,
                created_at text not null default current_timestamp,
                action text not null,
                note text not null,
                finding_reviews text not null,
                result text not null
            );
            create table if not exists finding_review_drafts (
                finding_id text primary key,
                status text not null,
                note text not null default '',
                updated_at text not null default current_timestamp
            );
            """
        )

        if conn.execute("select count(*) from locations").fetchone()[0] == 0:
            site = _load_json("site.json")
            conn.executemany(
                """
                insert into locations (id, name, x, y, type, risk_weight, notes)
                values (:id, :name, :x, :y, :type, :risk_weight, :notes)
                """,
                site["locations"],
            )
            conn.executemany(
                """
                insert into zones (id, name, points, risk)
                values (:id, :name, :points, :risk)
                """,
                [
                    {**zone, "points": json.dumps(zone["points"])}
                    for zone in site["zones"]
                ],
            )

        if conn.execute("select count(*) from events").fetchone()[0] == 0:
            events = _load_json("events.json")
            enriched_events = []
            for event in events:
                enriched_events.append(
                    {
                        **event,
                        "is_false_alarm": 1 if event["id"] in {"evt-001", "evt-009", "evt-010"} else 0,
                        "false_alarm_reason": {
                            "evt-001": "Weather context explains part of the Gate 3 vibration.",
                            "evt-009": "Warehouse 2 activity matches the cleaning window.",
                            "evt-010": "Contractor check-in matches expected South Yard staging.",
                        }.get(event["id"], ""),
                    }
                )
            conn.executemany(
                """
                insert into events (
                    id, timestamp, type, location_id, severity, description,
                    actor, confidence, is_false_alarm, false_alarm_reason
                )
                values (
                    :id, :timestamp, :type, :location_id, :severity, :description,
                    :actor, :confidence, :is_false_alarm, :false_alarm_reason
                )
                """,
                enriched_events,
            )

        if conn.execute("select count(*) from drone_patrols").fetchone()[0] == 0:
            patrols = _load_json("drones.json")
            conn.executemany(
                """
                insert into drone_patrols (id, started_at, ended_at, route, observations, coverage_quality)
                values (:id, :started_at, :ended_at, :route, :observations, :coverage_quality)
                """,
                [
                    {
                        **patrol,
                        "route": json.dumps(patrol["route"]),
                        "observations": json.dumps(patrol["observations"]),
                    }
                    for patrol in patrols
                ],
            )


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def save_review(action: str, note: str, finding_reviews: Any, result: Any) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "insert into reviews (action, note, finding_reviews, result) values (?, ?, ?, ?)",
            (action, note, json.dumps(finding_reviews), json.dumps(result)),
        )


def save_finding_review(finding_id: str, status: str, note: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            insert into finding_review_drafts (finding_id, status, note, updated_at)
            values (?, ?, ?, current_timestamp)
            on conflict(finding_id) do update set
                status = excluded.status,
                note = excluded.note,
                updated_at = current_timestamp
            """,
            (finding_id, status, note),
        )


def finding_review_drafts() -> list[dict[str, Any]]:
    return rows(
        "select finding_id, status, note, updated_at from finding_review_drafts order by updated_at"
    )
