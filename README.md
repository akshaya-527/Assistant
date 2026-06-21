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
- Agent: Gemini-first guarded Planner + ReAct loop with deterministic fallback

The app works without an LLM key using the deterministic fallback agent. With a Gemini API key, Gemini proposes the tool plan and synthesizes the briefing while the backend validates tool names, arguments, and outputs. Remote LLM calls use a short timeout so the demo falls back quickly when Gemini is unavailable.

## Current Data Capacity

This is an assignment MVP, not a benchmarked production ingestion service.

- SQLite stores events, locations, patrols, and human reviews across sessions.
- The current event search reads event rows and filters them in Python.
- The seeded demo has 10 events. Low thousands of rows should remain comfortable for a local demo, but no production throughput claim is made.
- Tens or hundreds of thousands of logs should move filtering into indexed SQL and should not be sent directly to the LLM.

See [SCALING.md](SCALING.md) for the proposed PostgreSQL, batching, correlation, and hierarchical summarization design.

## Run Locally

Add Gemini API key here

```bash
set GEMINI_API_KEY=your_key_here
```

Or set in `backend\.env`:

```env
GEMINI_API_KEY=your_key_here
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

Frontend runs on `http://localhost:5173`.

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
