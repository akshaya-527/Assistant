# Assistant

AI-first overnight intelligence platform for Ridgeway Site. The app helps Maya move from noisy night logs to a trusted morning briefing with evidence, map context, drone follow-up simulation, and human review.

## Submission Resources

- [SUBMISSION_GUIDE.md](SUBMISSION_GUIDE.md): detailed design answers, demo script, tradeoffs, and interviewer Q&A
- [WRITTEN_RESPONSES.md](WRITTEN_RESPONSES.md): concise submission-ready answers
- [FINAL_DEMO_SCRIPT.md](FINAL_DEMO_SCRIPT.md): 5-8 minute word-for-word video script
- [DEPLOYMENT.md](DEPLOYMENT.md): frontend/backend deployment, secrets, CORS, SQLite persistence, and cleanup
- [ARCHITECTURE.md](ARCHITECTURE.md): current system design
- [SCALING.md](SCALING.md): large-log and PostgreSQL roadmap
- [DEMO_SCRIPT.md](DEMO_SCRIPT.md): short demo outline

## What This MVP Demonstrates

- AI-first investigation before Maya manually reads logs
- A guarded Gemini Planner + ReAct agent that chooses MCP-style tools
- Explicit CLEARED, NEEDS_REVIEW, and ESCALATE signal classification
- Map/spatial view with events, zones, drone route, and bidirectional finding selection
- Drone patrol and follow-up mission simulation
- Per-finding review to clear noise, confirm action, dispute with context, refine, approve, or reject
- Visible uncertainty split into known evidence, unknowns, and what Maya must verify
- Morning briefing handoff for the 8:00 AM leadership review

## Stack

- Backend: FastAPI, Pydantic, SQLite, Gemini API
- Frontend: React, Vite, TypeScript, CSS
- Data: SQLite seeded from JSON files
- Tool interface: MCP-style tool registry exposed through `/api/tools`
- Agent: Gemini planner and evidence-driven finding generator with strict validation

Gemini proposes contextual tool use and generates structured findings from the current tool evidence. The backend derives candidate groups, risk, confidence, coverage gaps, evidence IDs, and follow-up locations from current data, then validates every model citation. When Gemini is unavailable, the API retries transient failures within a bounded cost budget and returns a visible retryable error; it does not silently present predetermined conclusions.

## Design Decisions and Tradeoffs

- **No conclusion fallback:** The system does not manufacture findings when Gemini is unavailable. It returns a clear retryable error after the retry budget is exhausted.
- **Dynamic findings:** Only simulated input data and safety policies are fixed. Candidate groups, classifications, evidence references, confidence, coverage gaps, briefing text, and follow-up routes emerge from current tool results and validated Gemini output.
- **SQLite:** Chosen for zero setup, local persistence, and easy inspection in a single-user prototype. Assumptions were low concurrency and one backend instance. PostgreSQL is the production choice for concurrent replicas, durable cloud storage, indexing, and backups.
- **Timeout and retries:** Gemini calls use bounded exponential backoff with jitter for timeouts, rate limits, and transient server errors, respect Retry-After, and avoid retrying non-retryable client failures. Attempt count, timeout, and backoff are configurable environment variables.
- **MCP-style tooling:** Tools have discoverable names, descriptions, input schemas, validation, and structured results. This meets the assignment's MCP-style requirement but is not a protocol-complete MCP server transport.
- **Simulated integrations:** Events, weather, badge history, contractor routes, and drone telemetry are seeded or mocked because real hardware and sensor integrations are out of scope. Production adapters can replace handlers without changing the agent/tool contracts.
- **Custom spatial plan:** A calibrated site plan was chosen instead of map tiles because Ridgeway is a private facility, the workflow must work without a map API key, and all zones, events, and drone routes share one coordinate system.
- **Human authority:** The system proposes classifications and missions but does not autonomously dispatch security or accuse a person. Maya can clear, confirm, dispute, refine, or approve every finding.

Add Gemini API key here

```bash
set GEMINI_API_KEY=your_key_here
```

Or set in `backend\.env`:

```env
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_TIMEOUT_SECONDS=30
GEMINI_FINDING_TIMEOUT_SECONDS=90
GEMINI_MAX_ATTEMPTS=2
GEMINI_RETRY_BASE_SECONDS=2
```

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Backend runs on `http://localhost:8000`.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Demo Flow

1. Open the app as Maya at 6:10 AM.
2. Click `Investigate Night`.
3. Watch the live reasoning stream and tool calls.
4. Click a map marker to open and scroll to its related finding; hover a finding to highlight its map locations.
5. Clear noise, confirm action, or dispute each finding and add Maya's context.
6. Refine, approve, or reject the briefing.
7. Use the morning briefing handoff for leadership.

## Future Improvements

- PostgreSQL event storage with time/site partitions and indexed filtering
- Streaming ingestion from security systems through a queue
- Hierarchical chunk summaries with durable evidence links
- Retrieval of related historical incidents using structured keys and optional embeddings
- Background job workers for long investigations
- Authentication, role-based review, audit exports, and multi-site tenancy
- Real weather, badge, camera, and drone adapters behind the same MCP tool contracts
- Evaluation datasets for classification accuracy, false-clear rate, latency, and reviewer overrides
