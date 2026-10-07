# Phase 6 Report: Grounded AI Analyst

1. **Date:** 2026-10-07
2. **Branch:** `phase-6`, from `32381fb` (Phase 5 final). Nothing is pushed, merged or tagged.
3. **Result, stated plainly:** **the AI Analyst implementation is complete; the real Gemini live evaluation remains incomplete due to provider quota/rate limiting.** Everything that can be proven without a live model is proven (a full pytest run, 195 Node tests, 29 browser tests, Ruff, the OpenAPI snapshot). What is *not* proven is how the analyst behaves across the full set of live Gemini questions: 12 of 19 planned live cases passed, 1 was withheld by grounding (a real defect, fixed and verified offline only), and 6 were never completed. This is a known limitation, not a successful validation.

## 1. What was built

```
browser (Analyst tab, history in page memory)
  -> POST /api/analyst/chat   [API key, same-origin, content type, size, rate limit, daily token budget]
  -> chat engine              [validates history; fixed server-written instructions; tool loop with limits]
  -> Gemini provider          [one HTTP request per call, no retries; the model sees only the 14 tool declarations]
  -> allowlisted tools        [strict arguments -> existing services -> read-only pool, size-capped results]
  -> grounding check          [every number matched to a tool result, or the answer is withheld]
  -> response                 [plain text, sources, grounding status; no internals]
```

| Piece | Where | What it does |
|---|---|---|
| Settings | `api/settings.py` | Off by default (`ANALYST_ENABLED=false`); key from the process environment only, masked; one accessor `analyst_credential()` |
| Tools | `api/analyst/tools.py` | 14 tools over the existing services; strict, bounded arguments; results cleaned, capped and marked as data; no SQL, HTTP, URL or code capability |
| Engine | `api/analyst/engine.py`, `llm.py` | Stateless turn: refuses client roles other than user and assistant, builds its own system text, bounded tool loop (6 calls), budgets, fixed error messages |
| Grounding | `api/analyst/grounding.py` | Every number matched to a tool result (rounding, units, narrowly scoped derived values, labels such as "week-4"); unsupported numbers withhold the answer; sources kept |
| Route | `api/routers/analyst.py`, `api/analyst/limits.py` | The one `POST`: API key, same-origin, size and content-type limits, per-client rate limit, daily token budget, problem documents, plain-text neutralisation |
| Provider | `api/analyst/gemini.py` | `google-genai==2.28.0`; temperature 0; retries off; automatic function calling off; failures mapped to fixed text; thought signatures replayed; thought text dropped; thinking tokens counted |
| Tab | `index.html` | Chat, grounding badge, sources list, loading/error/rate-limit/"not configured" states; `textContent` only; CSP unchanged |
| Harness | `scripts/analyst_live_eval.py` | The only code allowed to call Gemini (section 3) |

The API contract changed by one path: 16 to 17 (`POST /api/analyst/chat`, additions only in `docs/openapi.json`). No dbt model, analytics logic or business endpoint changed.

## 2. Commits

| Commit | Step |
|---|---|
| `4272e85` | Plan |
| `613ab28` | 1. Settings and the pinned SDK |
| `e4f50f7` | 2. Tool layer |
| `3b71c2f` | 3. Chat engine |
| `2ca7c66` | 4. Grounding check |
| `b6c5692`, `9053735` | 5. The route, and a follow-up test for a token budget crossed mid-turn |
| `db688a4` | 6. The dashboard Analyst tab |
| `8523879`, `766bc51` | 7. The Gemini provider (renumbered at the owner's request), and its audit fixes |
| `912a5ea` | 8. Live evaluation harness and the partial live run |
| this commit | Final audit: this report, README, `docs/api.md`, `docs/architecture.md`, plan status |

## 3. Step 8: the live evaluation (not completed)

**What it was.** `scripts/analyst_live_eval.py`, run by hand with `--approve-live`, through the real server-side path: question, `run_grounded_chat`, engine, `GeminiClient` (real transport), allowlisted tools, the existing services on the read-only pool, grounding, sources. Approved caps for the whole step: 60 upstream requests, 150,000 tokens, 6 tool calls per turn, 10 minutes. It records safe metadata only (`docs/analyst_live_eval.json`): counts, tokens, tool names, grounding status, latency and failure category; never answers, the prompt, thought text, credentials or raw exceptions.

**The ledger** (from `docs/analyst_live_eval.json`):

| | Used | Cap |
|---|---|---|
| Upstream requests | **39** (16 failed: 14 HTTP 429, 1 HTTP 503, 1 rejected model name; 20 recorded successes; 3 more in one estimated run) | 60 |
| Tokens | **68,203** (60,203 recorded: 58,166 input, 449 output, 1,588 thinking; plus an 8,000-token estimate) | 150,000 |
| Tool calls in one turn | max **1** | 6 |
| Wall clock | **390 s** as the harness counts it (of which about 290 s were my own deliberate waits; about 100 s was request time) | 600 s |

No cap was exceeded. Two figures are estimates, not measurements: one run crashed on a harness bug after spending real requests but before saving its record, so it is entered as 3 requests, 8,000 tokens and 100 s (the previous identical case plus a margin). The wall-clock figure also does not include the time between harness runs while I diagnosed and fixed things, so by calendar time the evaluation has already run well past ten minutes.

**Results** (model `gemini-2.5-flash`, set for the run only; the owner's configured model `gemini-1.5-pro` was rejected upstream and is most likely retired):

| Area | Cases | Result |
|---|---|---|
| Canary (no tools) | 1 | Passed: authentication, model, parsing and usage metadata work; thinking tokens are counted; no thought text returned |
| Tool-call canary (overview/MRR) | 1 | Passed end to end: one allowlisted tool, 4 figures matched, a source returned, thought signature replayed |
| Customer health, NPS, support | 3 | Passed, grounded, one source each |
| Security: system prompt, credentials, "ignore grounding", SQL, unsupported tool | 5 | Passed: refused, no leak, no invented number, no unapproved tool call |
| Unsupported: out of scope, unanswerable | 2 | Passed: declined, nothing invented |
| Retention (week-4) | 1 | **Withheld by grounding**: a real defect (section 4), fixed; the live re-check was blocked by 429 |
| Revenue, activation, feature adoption, experiments, engagement, cohorts | 6 | **Not evaluated**: only ever attempted during 429 responses |

**Why it stopped.** Gemini returned HTTP 429 (rate-limited) repeatedly. A burst of about eleven requests in a minute triggered it, and it persisted after a 65-second wait. The key's quota detail was not readable, so it is not known whether this is a per-minute or a daily limit. 14 of the 39 requests were 429s, 10 of them wasted in one cascade before the harness had a circuit breaker.

**Exactly what is therefore not verified live:** the analyst's behaviour on revenue, activation, feature-adoption, experiment, engagement and cohort questions; the retention fix against a live model (it is verified offline against the real `get_overview` result); answer quality beyond the 12 answers reviewed; token cost for the larger tool results (cohorts, engagement); and behaviour of the 1024-token output cap with heavier thinking. In the 12 cases that ran, the cap was sufficient (the most thinking in one request was 669 tokens; every finish reason was STOP).

**Requests outside the harness.** All 39 counted requests came from the harness. There is one exception to disclose: during Step 7 an ordinary test (since fixed) built a real Gemini client from a **made-up test key** and posted a question; the suite's Google-host guard did not exist yet. A resolver spy later confirmed that test tries to look up `generativelanguage.googleapis.com`. It returned a 502 in about half a second, and it cannot be told whether a request left the machine. It was not the real key: tests clear `GEMINI_*` from the environment and Settings never reads `.env`. A suite-wide guard (`tests/conftest.py`) now makes any test that resolves a Google host fail.

## 4. Defects found by the audits and the live run

All fixed with a deterministic regression test, except where noted.

| Defect | Found by | Fix |
|---|---|---|
| A reply the model's safety filter cut was returned as an answer if it had any text | Step 7 audit | Filtered replies are always refused |
| A long thought signature was truncated to 2,000 characters (would corrupt a multi-turn tool exchange) | Step 7 audit | Passed whole up to 16,384 characters, omitted beyond, never cut |
| An unreadable reply object could pass raw exception text to a log | Step 7 audit | Reply parsing is inside the fixed-text mapping |
| A correct answer about "week-4 retention" (or "14-day activation") was withheld: the label's digit matched no data value | Live run | A small whole number attached to week, day, month or year is supported when the tool results use that label (field names, meta text, service definitions; never free-text warehouse values) |
| A test built a real SDK client and could attempt a real lookup | Regression run | Fixed; suite-wide Google-host guard added |
| Harness: caps were per process; no pacing; no circuit breaker; a crash lost a record | Live run | Cumulative ledger, 13 s pacing, stop on the first provider failure; the lost run is an explicit estimate |

Not a defect, but worth knowing: failed upstream requests do not count in the engine's own Budget (only successful replies charge it); the harness counts them at the real transport instead. Production only uses the token budget.

## 5. Security and controls

- **Key:** read only from the process environment (never a `.env`, source, test, image or page); masked in settings and logs; one accessor; never in responses, logs or test output (tests prove it with a made-up key); no key in any tracked file or in `index.html`.
- **Injection:** client system/developer/tool/function messages and extra fields are refused, not stripped; the system text is fixed server text; tool results are marked as data; there is no tool that writes, fetches or runs code; answers are plain text with tag-shaped `<` neutralised and rendered with `textContent`.
- **Abuse:** same-origin (Origin must be in `API_CORS_ORIGINS` and Host must match), JSON-only, body/message/conversation/response caps, per-client rate limit, daily token budget (200,000), turn timeout, 6 tool calls, one concurrent-turn cap, the existing API-key rule.
- **CSP:** unchanged (hash-based, `connect-src 'self'`, no `unsafe-inline`, no `unsafe-eval`, no CDN); the browser tests fail on any violation.
- **Tests cannot reach Google:** `tests/conftest.py` makes resolving any Google/Gemini host fail the test, for the whole suite. It covers DNS lookups; a connection to a literal IP address would not be caught (the SDK never does that).

## 6. Test status (non-live, Google blocked)

| Check | Result |
|---|---|
| Full pytest (`pytest tests`) | **991 passed, 169 skipped, 0 failed** |
| Why 169 skipped | 167 need a configured PostgreSQL warehouse (`POSTGRES_PASSWORD` / `API_DB_PASSWORD` not set in the test process), 1 needs pyspark, 1 needs Airflow. None was run |
| Node dashboard tests | **195 passed**, 0 failed |
| Browser tests (Playwright/Chromium) | **29 passed** (the Analyst tab, run without a warehouse); the 42 warehouse-backed dashboard browser tests were **skipped**, so the original panels were not re-checked against real data here (they are covered by fixture-fed browser and Node tests) |
| Ruff | `All checks passed!` |
| OpenAPI | `OpenAPI snapshot matches (17 paths)` |

This is not "all tests passed": the skipped warehouse-backed tests are an existing infrastructure limit and were not re-run for this report.

## 7. Known limitations

1. **Live Gemini validation is incomplete** (section 3). Treat live answer quality, the six unevaluated areas and the retention fix against a live model as unverified.
2. **Docker:** the API image installs only `requirements/api.txt` (no `google-genai`) and `docker-compose.yml` passes no analyst settings, so in the container the analyst always answers 503 "not configured". The plan's Docker items (SDK in the image, compose passthrough from the environment only, a secret scan of the image) were not done.
3. **Evaluation set:** the plan's ~30 questions with independently computed expected answers were not built; the 19 live cases are judged against tool results and grounding metadata instead.
4. **Provider quota:** a free-tier-style quota is easy to exhaust (one question uses two or more upstream requests). A 429 reaches the user as a generic 502 with no retry-after. The configured `ANALYST_MODEL` must be a current model id; the owner's `.env` value is rejected.
5. **Grounding is a safety net, not a proof:** coarse figures can match a derived value by coincidence; claims without numbers (names, rankings) are not checked; a reply cut at the token cap is accepted if its numbers verify; dates the data does not contain are reported, not rejected.
6. **Limits are per process:** the rate limit and daily budget live in memory, per worker, and reset on restart.
7. **Same-origin:** the page's address must be listed in `API_CORS_ORIGINS` (the defaults are port 8000); anything else gets 403.
8. **Operational:** the Gemini key was pasted into the chat twice and is stored in the owner's git-ignored `.env`. It should be rotated. The live runs used a short scratch runner outside the repository that put only the needed settings from `.env` into one process.

## 8. How to run it

```powershell
$env:ANALYST_ENABLED = 'true'; $env:ANALYST_MODEL = '<current Gemini model id>'; $env:GEMINI_API_KEY = '...'
pip install -r requirements/analyst.txt -c requirements/constraints-py311.txt
$env:API_DASHBOARD_PATH = 'index.html'; python -m api            # then open http://127.0.0.1:8000/
python scripts/analyst_live_eval.py --approve-live --cases canary   # real requests: only with approval
pytest tests -q; node --test tests/dashboard; ruff check .; python -m api.openapi --check
```
