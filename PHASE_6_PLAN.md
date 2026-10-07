# Phase 6 Plan: Grounded AI Analyst

**Status:** draft for review. Nothing is implemented. Branch to create: `phase-6`, from `32381fb` (Phase 5 final).

## 0. Goal and scope

Replace the "AI Analyst coming in Phase 6" placeholder with an analyst that answers questions **only from the Phase 4 API's data**, through a **server-side endpoint that keeps the LLM key (Gemini) out of the browser**, and that shows where every number came from.

**In scope**
- One new endpoint, `POST /api/analyst/chat`, calling an LLM API with tool use.
- Tools that wrap the existing read-only analytics services (§3).
- Grounding, prompt-injection, cost and abuse controls (§4, §5).
- The AI Analyst tab: input, answer, sources, error and "not configured" states (§6).
- An evaluation set with known answers, and tests that need no network or key (§7).
- Docs and `PHASE_6_REPORT.md`.

**Out of scope**
- Free-form SQL by the model (text-to-SQL). Considered and deferred (§3).
- Any write path, any tool with side effects, any new dbt model or warehouse change.
- Server-side conversation storage, accounts, per-user history.
- Streaming responses (a later increment if wanted).
- Public hosting, a second LLM vendor, fine-tuning, embeddings or a vector store.
- Closing the Phase 5 leftovers (second-dataset check, container-image browser run) unless you ask. They are listed in step 0 as optional.

## 1. What exists today

- `GET /` serves `index.html` under a hash-based CSP (`connect-src 'self'`, no inline handlers, no remote scripts). The AI tab (`#tab-ai`) is a placeholder with **no controls and no network request**.
- 16 paths, all `GET`, read-only database role (`connecthub_api`), statement and lock timeouts, an ETag cache, RFC 9457 errors, request ids, optional `X-API-Key`, settings that mask secrets in the startup log (`api_db_password: "***"`).
- `docs/openapi.json` is a checked snapshot; `python -m api.openapi --check` fails on drift.
- Data is **synthetic** (`api_dataset_label`), 2025-01-02 to 2025-12-31, "as of" shown in the page bar.
- Tests: 379 passed, 2 skipped. 42 Playwright and 158 Node tests cover the page.

## 2. Design principles

1. **The model never touches the database or the network.** It can only call a fixed list of tools, and the tools only call code that already exists and is already read-only.
2. **Numbers come from tools, not from the model.** The system prompt forbids stating a figure that no tool returned, and a server-side check flags any number in the answer that is absent from the tool results (§4.3).
3. **Data is untrusted input.** Workspace names, experiment hypotheses and similar strings can contain instructions. They reach the model only inside tool results, and nothing the model outputs can act on them.
4. **Off by default, safe without a key.** No key means the tab says "not configured" and the endpoint answers `503`. Nothing breaks, nothing is sent anywhere.
5. **No new reach for the page.** The browser still talks only to its own origin. The CSP does not change.

## 3. Tool design (the main decision)

| Option | What the model gets | Verdict |
|---|---|---|
| **A (recommended): tools over the existing services** | About 13 tools, one per API endpoint, with the same parameters and validation as the REST routes. Results are the same envelopes the dashboard shows. | Smallest attack surface; answers match the dashboard exactly; reuses the cache, timeouts and grants. Cannot answer a question the API cannot. |
| B: allow-listed read-only SQL | One `run_sql` tool over gold tables. | More flexible, but needs a SQL parser or sandbox, row and cost limits, and a much larger security review. Defer to a later phase if A proves too limiting. |

Tools are called **in-process** against the service layer (not by HTTP to ourselves), so they inherit the read-only pool. The exact module and function names are confirmed in step 0 before any code is written.

Tool results are truncated to a size cap and wrapped as data (§4.2). `retention`, `cohorts` and `customer-health/workspaces` are the largest, so they get row limits in the tool wrapper.

## 4. Endpoint, grounding and safety

### 4.1 Contract
`POST /api/analyst/chat`, JSON body `{"messages": [{"role": "user"|"assistant", "content": "..."}]}`.
- **Stateless server.** The browser holds the conversation and sends the last N turns (default 10). The server accepts only `user` and `assistant` text; it **never accepts tool results or system text from the client**, so a client cannot forge grounding.
- Response: `{"answer": "...", "sources": [{"tool": "...", "args": {...}, "request_id": "...", "data_version": "..."}], "unverified_numbers": [...], "usage": {...}, "dataset": "synthetic"}`.
- Errors use the existing problem+json shape, plus `503 analyst_not_configured`, `429` with `Retry-After`, `502 analyst_upstream_error`, `413`/`422` for bad input.
- This is the **first non-GET route** and changes the OpenAPI snapshot: 17 paths. `docs/openapi.json` is regenerated deliberately and listed as a contract change in the report (Phase 5 treated the API as frozen; Phase 6 is the phase that unfreezes it, for this route only).

### 4.2 Prompt injection
- System prompt states: answer only from tool results; the data may contain text that looks like instructions and must be ignored; say "the dashboard data does not show that" rather than guess; mention the dataset is synthetic when relevant.
- Tool results are returned in a fixed wrapper that marks them as data. Strings from the warehouse are length-capped.
- There is **no tool that writes, fetches URLs, runs code or reads files**, so an injection has nothing to act on beyond producing a misleading sentence, which §4.3 and the evaluation set target.
- The answer is rendered in the browser as **text only** (no `innerHTML`, no markdown-to-HTML in v1).

### 4.3 Grounding check
After the model finishes, the server extracts numbers from the answer and checks each against the numbers in that turn's tool results (allowing for rounding and unit changes such as 0.639 to 63.9%). Unmatched numbers are returned in `unverified_numbers` and the tab shows a visible "could not be matched to the data" note. It does not rewrite the answer. This is a safety net, not a proof; the report states its false-positive and false-negative behavior as measured on the evaluation set.

### 4.4 Cost and abuse controls (all configurable, all with defaults)
Max message length and history turns, max tool calls per turn (6), max output tokens, upstream timeout, per-client rate limit, a global daily token budget that returns `429` when spent, and a same-origin check on `Origin`/`Host` because `API_AUTH_MODE` can be `none`. With `api_key` mode the existing key check applies to this route too.

### 4.5 Secrets and logging
- New settings: `ANALYST_ENABLED` (default `false`), `GEMINI_API_KEY` (secret, masked like `API_DB_PASSWORD`), `ANALYST_MODEL`, and the limits above. `GEMINI_API_KEY` is read **only from the process environment**: never from a `.env` file, source, Dockerfile, image layer, test, report or the page. `.env.example` lists the variable name with no value and a comment saying to export it in the shell.
- The key is read at runtime from the environment only. It is never baked into the image, never in `docker-compose.yml` defaults, never in a log line or an error body.
- Logs record request id, tool names, token counts and latency. **Prompt and answer text are not logged** unless `ANALYST_LOG_CONTENT=true`.
- A secret scan covers all new files, the built image's layers and the page.

## 5. Dependencies and environment

- One new pinned dependency, the official Google Gen AI Python SDK (`google-genai`; exact package and version confirmed and pinned in step 1), added to `requirements/` and `requirements/constraints-py311.txt` (the project's Python is 3.11 in `.venv`). It sits in an optional extra if possible so the API still runs without it, and the endpoint reports `503` when it is missing.
- Default model is configurable, not hard-coded; the report records which model produced the live evaluation numbers.
- No model downloads and no browser or Docker changes. About 5 GB is free on C:, so this phase should not need disk.

## 6. Dashboard changes

The AI Analyst tab gets: a text input and Send button, a message list, a per-answer "Sources" disclosure (tool names and arguments), the unverified-numbers note, loading, error, rate-limit, and "not configured" states, and a visible "synthetic data" label. It follows the same pattern as the other panels: the data layer's `ApiError` kinds, abort on a new send, stale-response guard, API text inserted as text only. One new function in the data layer posts to `/api/analyst/chat`; the CSP is unchanged and there are no inline handlers. Suggestion chips may return, but as fixed strings in the page with **no canned answers**.

## 7. Evaluation and tests

**Deterministic tests (run in CI, no key, no network).** A fake LLM client scripts tool-use turns so the loop, limits, truncation, history validation, error mapping, rate limiting, secret masking, `503` without a key, the OpenAPI snapshot, and the grounding checker are all tested exactly. Browser tests run the tab against a stub endpoint, including text-only rendering of hostile answers.

**Evaluation set (about 30 questions).** Each has an expected answer computed **independently of the model** from the warehouse or the committed API fixtures: single metric lookups, comparisons, trends, ranking, "which experiment shipped", unit traps (fraction vs percent), and questions the data cannot answer. Adversarial cases: an instruction planted in a workspace name or experiment hypothesis, "ignore your rules", a request for the API key or system prompt, an out-of-scope question, and a request for a number that does not exist.

**Live evaluation (optional, costs money).** Marked and skipped unless `GEMINI_API_KEY` is set **and** you have explicitly approved the run. It starts only after the fake-LLM implementation and every keyless security and grounding test have passed. It runs under hard caps enforced by the runner, which stops when any is reached: at most 60 upstream requests, 150,000 total tokens, 6 tool calls per turn, and a 10-minute wall clock (proposed values, to be confirmed by you at approval). The report states pass rate per category, requests and tokens used, and does not claim more than was run.

**Mutation checks.** Deliberate breaks (grounding check removed, client allowed to send tool results, key logged, rate limit off) must each fail a test, as in Phase 5.

## 8. Work order

Each step ends green (`pytest`, `ruff`, OpenAPI check) and is one commit on `phase-6`.

0. **Baseline.** Branch from `32381fb`. Confirm the service-layer entry points for the tools, the settings and masking pattern, and that the warehouse is ready. Optionally close the two Phase 5 leftovers first.
1. **Settings and dependency.** New settings, secret masking, optional SDK, `.env.example`, disabled by default.
2. **Tool layer.** Tool definitions and in-process wrappers with size caps; tests against fixtures.
3. **Chat engine.** The tool-use loop with limits, history validation and the fake client; no route yet.
4. **Grounding check** and its tests.
5. **Route.** `POST /api/analyst/chat`, error mapping, same-origin and rate-limit, budget; regenerate `docs/openapi.json`; update the OpenAPI and cache tests.
6. **Dashboard tab.** UI, data-layer function, Node model tests, browser tests against a stub.
7. **Evaluation set** and the deterministic runner; mutation checks.
8. **Live evaluation.** No live request is made before this step. It needs all keyless tests green and your explicit approval, and it uses the caps in §7.
9. **Docker and docs.** Compose passes the key through from the environment only; README, `docs/api.md`, `docs/architecture.md`; secret scan over the image.
10. **`PHASE_6_REPORT.md`** with deviations, skipped tests and limitations.

## 9. Acceptance criteria

- With no key the app starts, the tab says "not configured", the endpoint answers `503`, and no outbound request is made.
- The key appears in no page, image layer, log, error body or test output; the secret scan is clean.
- A client cannot inject tool results or system text; the history validator rejects them.
- Every answer returns its sources; every number in an answer is either matched to a tool result or listed as unverified.
- The planted-instruction cases in the evaluation set do not change the analyst's behavior in the deterministic tests, and the live results are reported honestly if run.
- Tool, token, rate and budget limits are enforced and tested.
- The CSP is unchanged, the console has no errors or CSP violations, and answers render as text only.
- `docs/openapi.json` changes only by the one new route; the rest of the contract and the warehouse fingerprint are unchanged.
- Full suite and ruff are green; the report lists everything not verified.

## 10. Risks

| Risk | Mitigation |
|---|---|
| The model states a plausible but wrong number | Tool-only numbers, grounding check, evaluation set, visible sources; reported as a limit, not claimed solved. |
| Prompt injection through warehouse strings | No side-effect tools, data wrapper, length caps, text-only rendering, adversarial evaluation cases. |
| Cost runaway or abuse of an open endpoint | Per-client limit, daily budget, turn and token caps, same-origin check, `ANALYST_ENABLED=false` by default. |
| Key leakage | Environment only, masked in settings, never logged, secret scan on image and repo. |
| The first POST route weakens the read-only story | The route writes nothing; the database role is unchanged; a test asserts the analyst code path uses only the read-only pool. |
| Tools return too much data, so cost and latency rise | Row and byte caps per tool, the existing ETag cache, a tool-call limit. |
| Vendor outage or API change | Upstream errors map to `502`; the dashboard keeps working; the SDK is pinned. |
| Answers inherit the synthetic data's oddities (A/A "winner", small tiers) | The prompt requires the API's caveats to be repeated; the evaluation set includes these cases. |

## 11. Open decisions (need your input)

| # | Decision | Recommendation |
|---|---|---|
| 1 | LLM provider and default model | **Decided: Gemini**, key from `GEMINI_API_KEY` only; model set by `ANALYST_MODEL`, chosen with you before step 8 |
| 2 | Spend and request caps for live evaluation (step 8) | Decided in principle: use the caps in §7; run only after keyless tests pass and you approve |
| 3 | Tools over services (A) or read-only SQL (B) | A |
| 4 | History held by the browser (stateless server) or stored server-side | Browser; no server storage |
| 5 | Should the analyst be on by default when a key is present | No: `ANALYST_ENABLED=false` until set |
| 6 | Unfreeze the API for the one new `POST` route and regenerate the snapshot | Yes, as a documented contract change |
| 7 | Render answers as plain text only in v1 | Yes; no markdown or HTML |
| 8 | Close the two Phase 5 leftovers (second dataset, browser suite against the container) first | Container check yes (it touches the same Docker path); second dataset can wait |
| 9 | Merge `phase-5` before starting, or branch `phase-6` from it | Branch from `phase-5`; merging stays your call |

## 12. Step 0 findings (service layer inspected; approved to proceed)

Approved by you with the live-evaluation limits exactly as in §7: 60 upstream requests, 150,000 total tokens, 6 tool calls per turn, 10-minute wall clock, stopping immediately when any one is reached. Gemini is the provider; the key comes only from `GEMINI_API_KEY` in the process environment.

**Reusable service functions** (all take an open SQLAlchemy `Connection` first and return the same `Envelope` the REST route returns; routes only parse parameters and call these):

| Planned tool | Function | Parameters |
|---|---|---|
| `get_meta` | `api.services.meta.build(conn, settings)` | none |
| `get_overview` | `api.services.overview.build(conn)` | none |
| `get_engagement` | `api.services.engagement.build(conn, start, end, granularity)` | dates, granularity |
| `get_activation` | `api.services.activation.build(conn, start, end, plan_tier, granularity, include_incomplete)` | |
| `get_retention` | `api.services.retention.retention(conn, as_of, cohort_start, cohort_end)` | |
| `get_cohorts` | `api.services.retention.cohorts(conn, as_of, cohort_start, cohort_end, weeks)` | |
| `get_revenue` | `api.services.revenue.build(conn, start_month, end_month, plan_tier, group_by)` | |
| `get_feature_adoption` | `api.services.adoption.build(conn, features, max_day, start_month, end_month)` | |
| `list_experiments` | `api.services.experiments.list_experiments(conn, decision, status)` | |
| `get_experiment` | `api.services.experiments.detail(conn, experiment_id)` | `^[a-z0-9_]{1,64}$` |
| `get_nps` | `api.services.nps.build(conn, start, end, plan_tier, granularity, min_responses)` | |
| `get_support` | `api.services.support.build(conn, start, end, plan_tier, call_type, granularity)` | |
| `get_customer_health` | `api.services.customer_health.summary(conn, plan_tier)` | |
| `list_workspaces` | `api.services.customer_health.workspaces(conn, tier, plan_tier, sort, order, limit, offset)` | |

`health.liveness/readiness` are not tools. Findings that shape step 2:
- **Parameter validation lives in the routers** (`api/params.py`: `PlanTier`, `Granularity`, `RiskTier`, month and date patterns, allowed-parameter checks), not in the services. The tool layer must re-apply the same validation (reusing `api.params` types) before calling a service.
- **Connections** come from `api.db.get_conn`: a connection from `app.state.engine` inside a read-only transaction that is rolled back. The tools reuse this engine, so the read-only role and session limits apply unchanged.
- **The response cache is HTTP middleware** (`api/cache.py`), so in-process tool calls do not use it. This is acceptable (statement timeout 5 s); a per-turn memo is a possible step-2 addition.
- **Envelope metadata** (`meta.data_version`, `as_of`, `sources`, `caveats`) is exactly what `sources` in the chat response and the model's caveats need.

**Step 1 outcome:** settings added in `api/settings.py` (off by default; the key is a masked optional secret; `analyst_ready` needs enabled, key and model); `google-genai==2.28.0` pinned in `requirements/analyst.txt` and the lock file (`pip check` clean).
