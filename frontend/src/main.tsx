import React from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  Check,
  CircleHelp,
  Crosshair,
  MapPinned,
  Pause,
  Play,
  RefreshCw,
  Route,
  ShieldAlert,
  X,
} from "lucide-react";
import "./styles.css";

type Severity = "info" | "warning" | "critical";
type Status = "draft" | "approved" | "rejected";
type ReviewStatus = "pending" | "confirmed" | "disputed" | "cleared";

type Location = {
  id: string;
  name: string;
  x: number;
  y: number;
  type: string;
  risk_weight: number;
  notes: string;
};

type Zone = {
  id: string;
  name: string;
  points: [number, number][];
  risk: "low" | "medium" | "high";
};

type EventLog = {
  id: string;
  timestamp: string;
  type: string;
  location_id: string;
  severity: Severity;
  description: string;
  actor: string;
  confidence: number;
  is_false_alarm: boolean;
  false_alarm_reason: string;
};

type DronePatrol = {
  id: string;
  started_at: string;
  ended_at: string;
  route: string[];
  observations: string[];
  coverage_quality: string;
};

type ToolCall = {
  name: string;
  arguments: Record<string, unknown>;
  result: unknown;
  rationale: string;
};

type ReasoningStep = {
  kind: "thought" | "tool" | "observation" | "summary";
  text: string;
  tool_call?: ToolCall;
};

type Finding = {
  id: string;
  title: string;
  classification: "CLEARED" | "NEEDS_REVIEW" | "ESCALATE";
  severity: Severity;
  confidence: number;
  review_status: ReviewStatus;
  summary: string;
  evidence_event_ids: string[];
  location_ids: string[];
  uncertainty: string;
  recommended_action: string;
  supports_escalation: string[];
  supports_false_alarm: string[];
};

type Investigation = {
  status: Status;
  generated_at: string;
  headline: string;
  confidence: number;
  escalation_level: "monitor" | "review" | "escalate";
  summary: string;
  harmless: string[];
  needs_escalation: string[];
  drone_checked: string[];
  open_questions: string[];
  findings: Finding[];
  follow_up_mission: {
    title: string;
    reason: string;
    route: string[];
    eta_minutes: number;
    priority: "low" | "medium" | "high";
  };
  tool_calls: ToolCall[];
  reasoning_steps: ReasoningStep[];
  coverage_gap: {
    location_id: string;
    start: string;
    end: string;
    minutes: number;
    label: string;
  };
  review_note?: string;
};

type Site = {
  locations: Location[];
  zones: Zone[];
};

type FindingReview = {
  finding_id: string;
  status: ReviewStatus;
  note: string;
};

type StreamEvent =
  | { type: "thought"; data: { text: string } }
  | { type: "observation"; data: { text: string } }
  | { type: "summary"; data: { text: string } }
  | { type: "tool"; data: ToolCall }
  | { type: "error"; data: { message: string; retryable: boolean } }
  | { type: "complete"; data: Investigation };

const API = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`);
  if (!response.ok) throw new Error(`Request failed: ${path}`);
  return response.json();
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `Request failed: ${path}`);
  }
  return response.json();
}

async function readSse(url: string, onEvent: (event: StreamEvent) => void) {
  const response = await fetch(url);
  if (!response.ok || !response.body) {
    throw new Error(`SSE unavailable (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const eventLine = chunk
        .split("\n")
        .find((line) => line.startsWith("event:"));
      const dataLine = chunk
        .split("\n")
        .find((line) => line.startsWith("data:"));
      if (!eventLine || !dataLine) continue;
      onEvent({
        type: eventLine.replace("event:", "").trim() as StreamEvent["type"],
        data: JSON.parse(dataLine.replace("data:", "").trim()),
      } as StreamEvent);
    }
  }
}

function App() {
  const [site, setSite] = React.useState<Site | null>(null);
  const [events, setEvents] = React.useState<EventLog[]>([]);
  const [drones, setDrones] = React.useState<DronePatrol[]>([]);
  const [investigation, setInvestigation] =
    React.useState<Investigation | null>(null);
  const [streamSteps, setStreamSteps] = React.useState<ReasoningStep[]>([]);
  const [reviewNote, setReviewNote] = React.useState(
    "Raghav mentioned Block C. Check whether Storage Yard C needs escalation.",
  );
  const [findingReviews, setFindingReviews] = React.useState<
    Record<string, FindingReview>
  >({});
  const [findingSaveState, setFindingSaveState] = React.useState<
    Record<string, "idle" | "saving" | "saved" | "error">
  >({});
  const [selectedLocation, setSelectedLocation] = React.useState("storage-c");
  const [selectedEventId, setSelectedEventId] = React.useState<string | null>(
    "evt-003",
  );
  const [selectedFindingId, setSelectedFindingId] = React.useState<
    string | null
  >(null);
  const [zoneFilter, setZoneFilter] = React.useState<string | null>(null);
  const [dronePlaying, setDronePlaying] = React.useState(false);
  const [droneIndex, setDroneIndex] = React.useState(0);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    Promise.all([
      getJson<Site>("/api/site"),
      getJson<EventLog[]>("/api/events"),
      getJson<DronePatrol[]>("/api/drones"),
      getJson<FindingReview[]>("/api/reviews/findings"),
    ])
      .then(([siteData, eventData, droneData, savedReviews]) => {
        setSite(siteData);
        setEvents(eventData);
        setDrones(droneData);
        setFindingReviews(
          Object.fromEntries(savedReviews.map((review) => [review.finding_id, review])),
        );
        setFindingSaveState(
          Object.fromEntries(savedReviews.map((review) => [review.finding_id, "saved"])),
        );
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  React.useEffect(() => {
    if (!dronePlaying || !drones[0]?.route.length) return;
    const timer = window.setInterval(() => {
      setDroneIndex((current) => (current + 1) % drones[0].route.length);
    }, 800);
    return () => window.clearInterval(timer);
  }, [dronePlaying, drones]);

  React.useEffect(() => {
    if (!investigation) return;
    setFindingReviews((existing) => {
      const next = { ...existing };
      for (const finding of investigation.findings) {
        if (!next[finding.id]) {
          next[finding.id] = {
            finding_id: finding.id,
            status: finding.review_status,
            note: "",
          };
        }
      }
      return next;
    });
    setSelectedFindingId(investigation.findings[0]?.id ?? null);
  }, [investigation]);

  const locationsById = React.useMemo(
    () =>
      new Map(
        (site?.locations ?? []).map((location) => [location.id, location]),
      ),
    [site],
  );
  const eventById = React.useMemo(
    () => new Map(events.map((event) => [event.id, event])),
    [events],
  );
  const selected = locationsById.get(selectedLocation);
  const selectedEvent = selectedEventId
    ? eventById.get(selectedEventId)
    : undefined;

  const focusFinding = (findingId: string, scrollIntoView = false) => {
    const finding = investigation?.findings.find((item) => item.id === findingId);
    if (!finding) return;
    setSelectedFindingId(finding.id);
    const locationId = finding.location_ids[0];
    if (locationId) setSelectedLocation(locationId);
    const eventId = finding.evidence_event_ids[0];
    if (eventId) setSelectedEventId(eventId);
    if (scrollIntoView) {
      window.requestAnimationFrame(() => {
        document.getElementById(`finding-${finding.id}`)?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
        });
      });
    }
  };

  const focusFindingForLocation = (locationId: string, scrollIntoView = false) => {
    const finding = investigation?.findings.find((item) =>
      item.location_ids.includes(locationId),
    );
    if (finding) focusFinding(finding.id, scrollIntoView);
  };

  const runStream = async () => {
    setLoading(true);
    setError(null);
    setStreamSteps([]);
    try {
      await readSse(
        `${API}/api/investigate/stream?review_note=${encodeURIComponent(reviewNote)}`,
        (event) => {
          if (event.type === "tool") {
            setStreamSteps((steps) => [
              ...steps,
              {
                kind: "tool",
                text: `Called ${event.data.name}`,
                tool_call: event.data,
              },
            ]);
          } else if (event.type === "complete") {
            setInvestigation(event.data);
          } else if (event.type === "error") {
            setError(event.data.message);
          } else {
            setStreamSteps((steps) => [
              ...steps,
              { kind: event.type, text: event.data.text },
            ]);
          }
        },
      );
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "The AI investigation failed. Retry when Gemini is available.",
      );
    } finally {
      setLoading(false);
    }
  };

  const updateFindingReview = (
    findingId: string,
    status: ReviewStatus,
    note?: string,
    persist = false,
  ) => {
    const nextReview = {
      finding_id: findingId,
      status,
      note: note ?? findingReviews[findingId]?.note ?? "",
    };
    setFindingReviews((existing) => ({ ...existing, [findingId]: nextReview }));
    setFindingSaveState((existing) => ({
      ...existing,
      [findingId]: persist ? "saving" : "idle",
    }));
    if (persist) {
      void postJson("/api/reviews/finding", nextReview)
        .then(() =>
          setFindingSaveState((existing) => ({
            ...existing,
            [findingId]: "saved",
          })),
        )
        .catch(() =>
          setFindingSaveState((existing) => ({
            ...existing,
            [findingId]: "error",
          })),
        );
    }
  };

  const review = async (action: "refine" | "approve" | "reject") => {
    setLoading(true);
    setError(null);
    try {
      const result = await postJson<Investigation>("/api/review", {
        action,
        note: reviewNote,
        finding_reviews: Object.values(findingReviews),
      });
      setInvestigation(result);
      setStreamSteps(result.reasoning_steps);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Review failed");
    } finally {
      setLoading(false);
    }
  };

  const visibleEvents = React.useMemo(() => {
    if (!zoneFilter || !site) return events;
    const zone = site.zones.find((item) => item.id === zoneFilter);
    if (!zone) return events;
    const zoneLocationIds = site.locations
      .filter((location) =>
        pointInPolygon([location.x, location.y], zone.points),
      )
      .map((location) => location.id);
    return events.filter((event) =>
      zoneLocationIds.includes(event.location_id),
    );
  }, [events, site, zoneFilter]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Ridgeway Site | Morning Review</p>
          <h1>6.10 Assistant</h1>
        </div>
        <div className="status-strip">
          <span>
            {events.filter((event) => event.severity === "critical").length}{" "}
            critical
          </span>
          <span>
            {events.filter((event) => event.is_false_alarm).length} false alarms
          </span>
          <span>{events.length} signals</span>
        </div>
      </header>

      <section className="hero-band">
        <div className="hero-copy">
          <p className="eyebrow">AI-first overnight investigation</p>
          <h2>
            {investigation?.headline ??
              "Let the agent investigate before Maya stitches logs together"}
          </h2>
          <p>
            Streaming tool use, uncertainty, spatial evidence, drone coverage,
            and human review in one workflow.
          </p>
        </div>
        <button
          className="primary-action"
          onClick={runStream}
          disabled={loading}
        >
          {loading ? (
            <RefreshCw className="spin" size={18} />
          ) : (
            <ShieldAlert size={18} />
          )}
          {loading ? "Investigating" : "Investigate Night"}
        </button>
      </section>

      {error && <div className="error-banner">{error}</div>}

      <section className="workbench">
        <ReasoningPanel steps={streamSteps} investigation={investigation} />
        <MapPanel
          site={site}
          events={events}
          drones={drones}
          investigation={investigation}
          selectedLocation={selectedLocation}
          selectedEventId={selectedEventId}
          selectedFindingId={selectedFindingId}
          zoneFilter={zoneFilter}
          dronePlaying={dronePlaying}
          droneIndex={droneIndex}
          setSelectedLocation={setSelectedLocation}
          setSelectedEventId={setSelectedEventId}
          focusFindingForLocation={focusFindingForLocation}
          setZoneFilter={setZoneFilter}
          setDronePlaying={setDronePlaying}
        />
      </section>

      <section className="lower-grid">
        <Timeline
          events={visibleEvents}
          locationsById={locationsById}
          selectedEventId={selectedEventId}
          setSelectedEventId={(id, locationId) => {
            setSelectedEventId(id);
            setSelectedLocation(locationId);
            focusFindingForLocation(locationId);
          }}
          zoneFilter={zoneFilter}
          clearZone={() => setZoneFilter(null)}
        />
        <ReviewPanel
          investigation={investigation}
          events={events}
          locationsById={locationsById}
          selectedEvent={selectedEvent}
          selectedFindingId={selectedFindingId}
          focusFinding={focusFinding}
          findingReviews={findingReviews}
          findingSaveState={findingSaveState}
          updateFindingReview={updateFindingReview}
          reviewNote={reviewNote}
          setReviewNote={setReviewNote}
          review={review}
          loading={loading}
        />
        <Handoff
          investigation={investigation}
          findingReviews={findingReviews}
          selected={selected}
        />
      </section>
    </main>
  );
}

function ReasoningPanel({
  steps,
  investigation,
}: {
  steps: ReasoningStep[];
  investigation: Investigation | null;
}) {
  return (
    <section className="panel reasoning-panel">
      <div className="panel-heading between">
        <span className="with-icon">
          <Crosshair size={18} /> Live Agent Reasoning
        </span>
        <span
          className={`pill ${investigation?.escalation_level ?? "neutral"}`}
        >
          {investigation?.escalation_level ?? "waiting"}
        </span>
      </div>
      <div className="confidence-row compact">
        <div>
          <span className="metric">{investigation?.confidence ?? "--"}%</span>
          <span className="metric-label">overall confidence</span>
        </div>
        <div className={`review-status ${investigation?.status ?? "draft"}`}>
          {investigation?.status ?? "draft"}
        </div>
      </div>
      <div className="reasoning-stream">
        {steps.length === 0 && (
          <p className="muted">
            Click Investigate Night to watch the agent plan, call tools, observe
            evidence, and summarize.
          </p>
        )}
        {steps.map((step, index) => (
          <article
            className={`reasoning-step ${step.kind}`}
            key={`${step.kind}-${index}`}
          >
            <span>{step.kind}</span>
            <p>{step.text}</p>
            {step.tool_call && (
              <div className="tool-inline">
                <strong>{step.tool_call.name}</strong>
                <code>{JSON.stringify(step.tool_call.arguments)}</code>
                <small>{summarizeToolResult(step.tool_call)}</small>
              </div>
            )}
          </article>
        ))}
      </div>
      {investigation && <p className="summary">{investigation.summary}</p>}
    </section>
  );
}

function MapPanel({
  site,
  events,
  drones,
  investigation,
  selectedLocation,
  selectedEventId,
  selectedFindingId,
  zoneFilter,
  dronePlaying,
  droneIndex,
  setSelectedLocation,
  setSelectedEventId,
  focusFindingForLocation,
  setZoneFilter,
  setDronePlaying,
}: {
  site: Site | null;
  events: EventLog[];
  drones: DronePatrol[];
  investigation: Investigation | null;
  selectedLocation: string;
  selectedEventId: string | null;
  selectedFindingId: string | null;
  zoneFilter: string | null;
  dronePlaying: boolean;
  droneIndex: number;
  setSelectedLocation: (id: string) => void;
  setSelectedEventId: (id: string | null) => void;
  focusFindingForLocation: (locationId: string, scrollIntoView?: boolean) => void;
  setZoneFilter: (id: string | null) => void;
  setDronePlaying: (value: boolean) => void;
}) {
  if (!site)
    return <section className="panel">Loading spatial workflow...</section>;
  const locationMap = new Map(
    site.locations.map((location) => [location.id, location]),
  );
  const patrolRoute = drones[0]?.route ?? [];
  const followRoute = investigation?.follow_up_mission.route ?? [];
  const droneLocation = locationMap.get(patrolRoute[droneIndex] ?? "");
  const selectedZone = site.zones.find((zone) => zone.id === zoneFilter);
  const selectedFinding = investigation?.findings.find(
    (finding) => finding.id === selectedFindingId,
  );

  return (
    <section className="panel map-panel">
      <div className="panel-heading between">
        <span className="with-icon">
          <MapPinned size={18} /> Interactive Spatial Review
        </span>
        <button
          className="icon-button"
          onClick={() => setDronePlaying(!dronePlaying)}
          title="Play drone patrol"
        >
          {dronePlaying ? <Pause size={16} /> : <Play size={16} />}
        </button>
      </div>
      <div className="site-map">
        <svg
          className="zone-layer"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {site.zones.map((zone) => (
            <polygon
              key={zone.id}
              points={zone.points.map(([x, y]) => `${x},${y}`).join(" ")}
              className={`zone ${zone.risk} ${zoneFilter === zone.id ? "active" : ""}`}
              onClick={() =>
                setZoneFilter(zoneFilter === zone.id ? null : zone.id)
              }
            />
          ))}
        </svg>
        <RouteLine
          route={patrolRoute}
          locationMap={locationMap}
          className="drone-route"
        />
        <RouteLine
          route={followRoute}
          locationMap={locationMap}
          className="follow-route"
        />
        {investigation?.coverage_gap && (
          <CoverageGap
            location={locationMap.get(investigation.coverage_gap.location_id)}
            gap={investigation.coverage_gap}
          />
        )}
        {droneLocation && (
          <div
            className="drone-dot"
            style={{ left: `${droneLocation.x}%`, top: `${droneLocation.y}%` }}
          >
            <Route size={14} />
          </div>
        )}
        {site.locations.map((location) => {
          const markerEvents = events.filter(
            (event) => event.location_id === location.id,
          );
          const highest = markerEvents.some(
            (event) => event.severity === "critical",
          )
            ? "critical"
            : markerEvents.some((event) => event.severity === "warning")
              ? "warning"
              : "info";
          const firstEvent = markerEvents[0];
          const belongsToSelectedFinding =
            selectedFinding?.location_ids.includes(location.id) ?? false;
          return (
            <button
              key={location.id}
              className={`map-marker ${highest} ${selectedLocation === location.id ? "selected" : ""} ${belongsToSelectedFinding ? "finding-highlight" : ""}`}
              style={{ left: `${location.x}%`, top: `${location.y}%` }}
              onClick={() => {
                setSelectedLocation(location.id);
                setSelectedEventId(firstEvent?.id ?? null);
                focusFindingForLocation(location.id, true);
              }}
              title={location.notes}
            >
              <span>{location.name}</span>
            </button>
          );
        })}
      </div>
      <div className="map-context">
        <strong>
          {selectedZone
            ? selectedZone.name
            : locationMap.get(selectedLocation)?.name}
        </strong>
        <span>
          {selectedZone
            ? `${selectedZone.risk} risk zone filter active`
            : locationMap.get(selectedLocation)?.notes}
        </span>
      </div>
      <div className="legend">
        <span>
          <i className="legend-line drone" /> Drone patrol
        </span>
        <span>
          <i className="legend-line follow" /> Follow-up mission
        </span>
        <span>
          <i className="coverage-swatch" /> Coverage gap
        </span>
      </div>
    </section>
  );
}

function CoverageGap({
  location,
  gap,
}: {
  location?: Location;
  gap: Investigation["coverage_gap"];
}) {
  if (!location) return null;
  return (
    <div
      className="coverage-gap"
      style={{ left: `${location.x}%`, top: `${location.y}%` }}
    >
      <strong>{gap.minutes} min</strong>
      <span>{gap.label}</span>
    </div>
  );
}

function RouteLine({
  route,
  locationMap,
  className,
}: {
  route: string[];
  locationMap: Map<string, Location>;
  className: string;
}) {
  const points = route
    .map((id) => locationMap.get(id))
    .filter(Boolean) as Location[];
  if (points.length < 2) return null;
  return (
    <svg
      className="route-layer"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
    >
      <polyline
        points={points.map((point) => `${point.x},${point.y}`).join(" ")}
        className={className}
      />
    </svg>
  );
}

function Timeline({
  events,
  locationsById,
  selectedEventId,
  setSelectedEventId,
  zoneFilter,
  clearZone,
}: {
  events: EventLog[];
  locationsById: Map<string, Location>;
  selectedEventId: string | null;
  setSelectedEventId: (id: string, locationId: string) => void;
  zoneFilter: string | null;
  clearZone: () => void;
}) {
  return (
    <section className="panel">
      <div className="panel-heading between">
        <span className="with-icon">
          <AlertTriangle size={18} /> Evidence Timeline
        </span>
        {zoneFilter && (
          <button className="text-button" onClick={clearZone}>
            Clear zone
          </button>
        )}
      </div>
      <div className="timeline">
        {events.map((event) => (
          <button
            className={`timeline-item ${selectedEventId === event.id ? "active" : ""}`}
            key={event.id}
            onClick={() => setSelectedEventId(event.id, event.location_id)}
          >
            <time>{event.timestamp}</time>
            <div>
              <strong>{event.description}</strong>
              <span>
                {locationsById.get(event.location_id)?.name ??
                  event.location_id}{" "}
                - {severityLabel(event.severity)}
                {event.is_false_alarm ? " - false-alarm context" : ""}
              </span>
            </div>
            <ConfidenceBadge value={Math.round(event.confidence * 100)} />
          </button>
        ))}
      </div>
    </section>
  );
}

function ReviewPanel({
  investigation,
  events,
  locationsById,
  selectedEvent,
  selectedFindingId,
  focusFinding,
  findingReviews,
  findingSaveState,
  updateFindingReview,
  reviewNote,
  setReviewNote,
  review,
  loading,
}: {
  investigation: Investigation | null;
  events: EventLog[];
  locationsById: Map<string, Location>;
  selectedEvent?: EventLog;
  selectedFindingId: string | null;
  focusFinding: (findingId: string, scrollIntoView?: boolean) => void;
  findingReviews: Record<string, FindingReview>;
  findingSaveState: Record<string, "idle" | "saving" | "saved" | "error">;
  updateFindingReview: (
    findingId: string,
    status: ReviewStatus,
    note?: string,
    persist?: boolean,
  ) => void;
  reviewNote: string;
  setReviewNote: (value: string) => void;
  review: (action: "refine" | "approve" | "reject") => Promise<void>;
  loading: boolean;
}) {
  const findings = investigation?.findings ?? [];
  return (
    <section className="panel review-panel">
      <div className="panel-heading">
        <CircleHelp size={18} />
        <h3>Human Review</h3>
      </div>
      {selectedEvent && (
        <div className="selected-context">
          <strong>
            {selectedEvent.timestamp} -{" "}
            {locationsById.get(selectedEvent.location_id)?.name}
          </strong>
          <span>{selectedEvent.description}</span>
          {selectedEvent.false_alarm_reason && (
            <small>{selectedEvent.false_alarm_reason}</small>
          )}
        </div>
      )}
      <div className="findings-list review-findings">
        {findings.length === 0 && (
          <p className="muted">Findings appear after the agent investigates.</p>
        )}
        {findings.map((finding) => {
          const reviewState = findingReviews[finding.id] ?? {
            finding_id: finding.id,
            status: "pending",
            note: "",
          };
          const evidence = finding.evidence_event_ids
            .map((id) => events.find((event) => event.id === id))
            .filter(Boolean) as EventLog[];
          return (
            <article
              id={`finding-${finding.id}`}
              className={`finding review-card ${selectedFindingId === finding.id ? "selected" : ""}`}
              key={finding.id}
              onMouseEnter={() => focusFinding(finding.id)}
              onFocus={() => focusFinding(finding.id)}
            >
              <button
                className="finding-title"
                onClick={() => focusFinding(finding.id)}
              >
                <span className={`dot ${finding.severity}`} />
                <strong>{finding.title}</strong>
                <span
                  className={`classification ${finding.classification.toLowerCase().replace("_", "-")}`}
                >
                  {finding.classification.replace("_", " ")}
                </span>
                <ConfidenceBadge value={finding.confidence} />
              </button>
              <div className="uncertainty-grid">
                <div>
                  <strong>Known</strong>
                  <span>{finding.summary}</span>
                </div>
                <div>
                  <strong>Unknown</strong>
                  <span>{finding.uncertainty}</span>
                </div>
                <div>
                  <strong>Maya verifies</strong>
                  <span>
                    {finding.classification === "CLEARED"
                      ? "No physical check required unless new evidence appears."
                      : finding.recommended_action}
                  </span>
                </div>
              </div>
              <div className="argument-grid">
                <div>
                  <strong>Escalation evidence</strong>
                  {finding.supports_escalation.length ? (
                    finding.supports_escalation.map((item) => (
                      <span key={item}>{item}</span>
                    ))
                  ) : (
                    <span>None</span>
                  )}
                </div>
                <div>
                  <strong>False-alarm evidence</strong>
                  {finding.supports_false_alarm.length ? (
                    finding.supports_false_alarm.map((item) => (
                      <span key={item}>{item}</span>
                    ))
                  ) : (
                    <span>None</span>
                  )}
                </div>
              </div>
              <div className="evidence-chips">
                {evidence.map((event) => (
                  <span key={event.id}>
                    {event.timestamp} {event.type}
                  </span>
                ))}
              </div>
              <textarea
                className="finding-note"
                value={reviewState.note}
                onChange={(event) =>
                  updateFindingReview(
                    finding.id,
                    reviewState.status,
                    event.target.value,
                  )
                }
                onBlur={() =>
                  updateFindingReview(
                    finding.id,
                    reviewState.status,
                    reviewState.note,
                    true,
                  )
                }
                placeholder="Add Maya's context for this finding..."
              />
              <small className={`save-state ${findingSaveState[finding.id] ?? "idle"}`}>
                {findingSaveState[finding.id] === "saving"
                  ? "Saving decision..."
                  : findingSaveState[finding.id] === "saved"
                    ? "Decision saved to review history"
                    : findingSaveState[finding.id] === "error"
                      ? "Save failed - try again"
                      : "Unsaved note"}
              </small>
              <div className="button-row">
                <button
                  className={reviewState.status === "cleared" ? "approve" : ""}
                  onClick={() => updateFindingReview(finding.id, "cleared", undefined, true)}
                >
                  <Check size={16} /> Clear as noise
                </button>
                <button
                  className={
                    reviewState.status === "confirmed" ? "approve" : ""
                  }
                  onClick={() => updateFindingReview(finding.id, "confirmed", undefined, true)}
                >
                  <Check size={16} /> Confirm action
                </button>
                <button
                  className={reviewState.status === "disputed" ? "reject" : ""}
                  onClick={() => updateFindingReview(finding.id, "disputed", undefined, true)}
                >
                  <X size={16} /> Dispute
                </button>
              </div>
            </article>
          );
        })}
      </div>
      <textarea
        value={reviewNote}
        onChange={(event) => setReviewNote(event.target.value)}
        placeholder="Overall review note..."
      />
      <div className="button-row">
        <button
          onClick={() => review("refine")}
          disabled={!investigation || loading}
        >
          Refine With Reviews
        </button>
        <button
          onClick={() => review("approve")}
          disabled={!investigation || loading}
          className="approve"
        >
          <Check size={16} /> Approve Briefing
        </button>
        <button
          onClick={() => review("reject")}
          disabled={!investigation || loading}
          className="reject"
        >
          Reject Draft
        </button>
      </div>
    </section>
  );
}

function Handoff({
  investigation,
  findingReviews,
  selected,
}: {
  investigation: Investigation | null;
  findingReviews: Record<string, FindingReview>;
  selected?: Location;
}) {
  const confirmed =
    investigation?.findings.filter(
      (finding) => findingReviews[finding.id]?.status === "confirmed",
    ) ?? [];
  const cleared =
    investigation?.findings.filter(
      (finding) => findingReviews[finding.id]?.status === "cleared",
    ) ?? [];
  const disputed =
    investigation?.findings.filter(
      (finding) => findingReviews[finding.id]?.status === "disputed",
    ) ?? [];
  return (
    <section className="panel">
      <div className="panel-heading">
        <Check size={18} />
        <h3>Morning Briefing</h3>
      </div>
      {investigation ? (
        <div className="handoff">
          <p>
            <strong>Draft:</strong> {investigation.summary}
          </p>
          <p>
            <strong>Confirmed action:</strong>{" "}
            {confirmed.length
              ? confirmed.map((finding) => finding.title).join("; ")
              : "None yet."}
          </p>
          <p>
            <strong>Cleared as noise:</strong>{" "}
            {cleared.length
              ? cleared.map((finding) => finding.title).join("; ")
              : investigation.harmless.join(" ")}
          </p>
          <p>
            <strong>Disputed / follow-up:</strong>{" "}
            {disputed.length
              ? disputed
                  .map(
                    (finding) =>
                      `${finding.title}${findingReviews[finding.id]?.note ? ` - ${findingReviews[finding.id].note}` : ""}`,
                  )
                  .join("; ")
              : investigation.open_questions.join(" ")}
          </p>
          <p>
            <strong>Drone:</strong> {investigation.follow_up_mission.reason}
          </p>
          <p>
            <strong>Selected map point:</strong> {selected?.name ?? "None"} -{" "}
            {selected?.notes ?? "Select a site point."}
          </p>
        </div>
      ) : (
        <p className="muted">
          The briefing updates after investigation and per-finding review.
        </p>
      )}
    </section>
  );
}

function ConfidenceBadge({ value }: { value: number }) {
  const level = value >= 80 ? "high" : value >= 60 ? "medium" : "low";
  return <span className={`confidence-badge ${level}`}>{value}%</span>;
}

function severityLabel(severity: Severity) {
  return severity === "critical"
    ? "Critical"
    : severity === "warning"
      ? "Warning"
      : "Info";
}

function summarizeToolResult(tool: ToolCall) {
  const result = tool.result as Record<string, unknown>;
  if (typeof result.count === "number") return `${result.count} records`;
  if (typeof result.score === "number") return `risk score ${result.score}`;
  if (typeof result.title === "string") return result.title;
  return "structured result captured";
}

function pointInPolygon(point: [number, number], polygon: [number, number][]) {
  const [x, y] = point;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    const intersects =
      yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

createRoot(document.getElementById("root")!).render(<App />);
