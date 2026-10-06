# Phase 5 Plan: Dashboard on Real Data

**Status:** draft for review. Nothing is implemented. Branch to create: `phase-5`, from `193be5f` (Phase 4D).

## 0. Goal and scope

Make `index.html` render **only data returned by the Phase 4 API**. After this phase no number on the page is a JS literal.

**In scope**
- A data layer in `index.html` (fetch, ETag revalidation, error handling).
- Wiring every panel to an endpoint (§4), with loading, empty and error states.
- Removing or relabelling everything the API cannot back (§5).
- Serving the page from the API container for same-origin use (§6).
- Browser-level tests of the dashboard (§8).
- Docs and a Phase 5 report.

**Out of scope**
- **The AI analyst.** The tab shows "not available yet" (as Phase 4 §12.2 decided). A grounded server-side analyst is **Phase 6** (§10).
- New API endpoints or dbt models. The API is treated as frozen; any change to it is a reported deviation and must regenerate `docs/openapi.json`.
- Optional model 10 (`by_week1_features`). Its panel is hidden.
- Public hosting (GitHub Pages) beyond making it *possible* via `?api=` and CORS.
- A framework or build step. Single file, Chart.js, as today.

## 1. What exists today (inspection)

**`index.html` (about 1,000 lines)**
- Six tabs: Overview, Retention, Activation, Experiments, Customer Health, AI Analyst.
- Chart.js 4.4.1 from cdnjs and Google Fonts. Inline `<script>`, inline `onclick` handlers.
- Hard-coded: 6 KPIs, 3 "Key Insight" boxes, 8 charts, the heatmap, funnel, the "Onboarding V2" experiment card, 4 health tiers, the at-risk table (invented company names), "Live" pill, "Oct 2025 — Sep 2026", `PROJECT_CONTEXT`, `getFallbackResponse()`.
- Known defect (audit §3.1): `chart-health-dist` uses a scriptable `backgroundColor` that returns an array, so the bar colors don't apply.
- Rendering uses `innerHTML` in 3 places (heatmap, funnel, health table).

**API (frozen, 16 paths, all GET)**
`/api/{meta, overview, engagement, activation, retention, cohorts, revenue, feature-adoption, experiments, experiments/{id}, nps, support, customer-health, customer-health/workspaces}` plus `/api/health` and `/api/health/ready`.
- Envelope `{meta, data}`, errors as RFC 9457 `application/problem+json`.
- ETag and `304` on every cacheable route; `Cache-Control: private, max-age=60`; `X-Cache` exposed.
- Optional `X-API-Key` (`API_AUTH_MODE=api_key`); CORS from `API_CORS_ORIGINS`.

### 1.1 Gaps found during inspection

1. **`API_DASHBOARD_PATH` does not exist in the code.** Phase 4B/4C only mention it (docstring and plan §7). The "same-origin dev" path in Phase 4 §12.1 is therefore unimplemented.
2. **The API's headers would break a served page.** Every non-docs response gets `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` plus `X-Frame-Options: DENY` (`api/middleware.py:25-31`). A dashboard served from `/` needs its own CSP allowing its inline script, the Chart.js CDN, Google Fonts and `connect-src 'self'`.
3. **The page uses inline script and inline event handlers**, which a useful CSP forbids unless `'unsafe-inline'` is allowed. Decision in §6.2.
4. **Several panels have no endpoint field to back them** (§5), and the API has NPS and support data the page doesn't show yet.
5. **The API key problem.** With `API_AUTH_MODE=api_key`, a browser page needs the key, and any key embedded in a static page is public. Decision in §7.
6. **No browser test tooling in the repo.** Node is installed on this machine (audit §1), but nothing in `requirements*` or the Makefile uses it. Decision in §8.

## 2. Architecture

```
Browser ── GET / ──────────────▶ API container (serves index.html, own CSP)
        ── GET /api/* (same origin; If-None-Match) ──▶ FastAPI ──▶ PostgreSQL (read-only role)
```

- **One origin in dev and in Docker.** No CORS involved.
- **Alternative host** (`?api=https://…`) works only if that origin is listed in `API_CORS_ORIGINS`. Supported, documented, not the default.
- `index.html` stays a single file. The data layer is a clearly delimited section at the top of the `<script>`.

## 3. Data layer design

```
const API_BASE = window.CONNECTHUB_API_BASE ?? new URLSearchParams(location.search).get('api') ?? location.origin;
async function api(path, params)   // GET, If-None-Match from an in-memory ETag map, returns {data, meta, cache}
```

- **Validation:** `API_BASE` from `?api=` must parse as an `http(s)` URL; anything else is ignored.
- **ETag:** keep `url → {etag, body}` in memory. On `304`, reuse the stored body. (The browser's own HTTP cache may already handle `max-age=60`; the map makes revalidation explicit and testable.)
- **Errors:** parse `problem+json` into `{status, type, title, detail, request_id}`. Map the types the API actually emits (503 data-not-ready, 503 pool/warehouse busy, 504 timeout, 401, 422, network failure) to panel messages. Show `request_id` in a small "details" line to make support easier.
- **Lazy loading:** `/api/meta` and `/api/overview` plus the Overview charts on load; each other tab on first activation; failures don't block other panels (each panel owns its state).
- **Race and staleness:** a per-panel request token so a slow response from a previous selection (for example another experiment) can't overwrite a newer one. `AbortController` on tab change and selector change.
- **Rendering safety:** every API string goes through `textContent` or an `esc()` helper. No `innerHTML` with data. Build DOM nodes for the heatmap, funnel and table rather than concatenating HTML.
- **Number formatting:** one `fmt` module (percent, currency, integer, pp deltas, null → "—"). The API's units (fractions vs percents) are confirmed against the schemas in step 1, then fixed in one place. The audit already found `percent_*` scale bugs once; a unit test per formatter prevents a repeat.
- **Panel states:** every panel renders one of `loading | ready | empty | error`, via a single `setPanelState(el, state, info)`.

## 4. Panel → endpoint mapping (confirmed against `docs/openapi.json`)

| Panel | Endpoint | Notes |
|---|---|---|
| Top bar pill and date range | `/api/meta` | "Data as of {last validated run}" and `{data_start} — {data_end}`; dataset label (`synthetic`) shown. "Live" removed. |
| Overview KPIs (6) | `/api/overview` → `kpis` | value, change and period tooltip. A null KPI shows "—", never 0. |
| DAU chart | `/api/engagement?granularity=month` | `avg_dau`; DAU/MAU as second series if present. |
| Feature adoption | `/api/feature-adoption` | Curves by day since signup. **Carry the right-censoring caveat** (Phase 4 §15) into the subtitle. |
| Revenue by plan | `/api/revenue?group_by=plan_tier` | |
| AI resolution rate | `/api/support?granularity=month` | |
| Retention heatmap | `/api/cohorts` | null cell → "—"; the weeks parameter bounds the width. |
| Activation funnel | `/api/activation` | Use the API's stage order and `include_incomplete` default; show the observation-window note. |
| Time to milestone | `/api/activation` → milestone timings | |
| Experiments | `/api/experiments` → `/api/experiments/{id}` | **Selector** replaces the fixed "Onboarding V2" card. Verdict class from `decision_code`. SRM, guardrails and Bayesian values from `evaluation`. The A/A experiment is labelled as such (`kind`). |
| Experiment curve | `/api/experiments/{id}` → curves | |
| Health tiers + histogram | `/api/customer-health` | Per-bar color array (fixes the scriptable-option bug). |
| At-risk table | `/api/customer-health/workspaces?tier=…&sort=score&order=asc&limit=10` | Exact `sort` enum value taken from the OpenAPI snapshot. Rendered via DOM nodes. |
| **New:** NPS panel | `/api/nps` | Added to Overview or a new "Voice of customer" row; decision in §9. |
| **New:** Support panel | `/api/support` | Same. |

## 5. What gets removed or relabelled

| Item | Action |
|---|---|
| "Live" pill | Replace with "Data as of …" from `/api/meta`. |
| 3 "Key Insight" boxes (hard-coded narratives that contradict the data) | **Removed.** Data-driven insights belong to the analyst phase. |
| "Retention by Features Adopted (Week 1)" panel | **Hidden** (model 10 not built). |
| "Onboarding V2" fixed card and hypothesis text | Replaced by the selector; hypothesis only if the API returns one. |
| AI Analyst tab content | Replaced with a short "Not available yet" state. `PROJECT_CONTEXT`, `getFallbackResponse()`, `claude.use('sample')`, suggestion chips, `sendMessage` removed. |
| "Built by … yourusername" footer link | Fixed to the real repo URL or removed (needs your input, §11). |
| "22,000+ businesses / 500K users / 50M+ events" claims | Removed with `PROJECT_CONTEXT`. Any scale text comes from `/api/meta`. |
| Hard-coded subtitles that make claims ("More features in week 1 = higher retention") | Neutral descriptions only. |

## 6. Serving the dashboard

### 6.1 `API_DASHBOARD_PATH`
- New setting (default unset). When set, `GET /` returns the file; any other non-`/api` path returns 404.
- The file path is resolved once at startup and **must exist**, or startup fails with a clear message (no secrets in it).
- `HEAD` supported; `ETag`/`304` using a hash of the file; `Cache-Control: no-cache`.
- Dockerfile copies `index.html` into the image; compose sets the variable.
- Not mounted in `API_ENV=production` unless explicitly enabled.

### 6.2 CSP for the dashboard response
The API CSP stays as is for all `/api/*` responses. The `/` response gets its own policy and no `X-Frame-Options: DENY` conflict (`frame-ancestors 'none'` stays).

Proposed:
```
default-src 'none'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;
connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'
```
- **Decision:** `'unsafe-inline'` for script is the pragmatic choice for a single file. The stricter alternative is a nonce generated per response or a hash of the inline script, which needs the server to template the file. Recommendation: **script hash computed at startup** (no templating per request, no `'unsafe-inline'` for scripts) and inline `onclick` handlers replaced with `addEventListener`. This is a small change and a real security gain; I'd include it unless you want to keep scope smaller.
- Add **Subresource Integrity** for the Chart.js tag (audit §3.17, P2). Alternatively vendor Chart.js into the repo and serve it from `'self'`, which removes the CDN dependency and makes the CSP simpler. **Recommendation: vendor it** (about 200 KB, version-pinned), at `static/vendor/chart.umd.min.js`. Needs a route; decision in §11.

## 7. Authentication from the browser

- **Default (dev, Docker on localhost): `API_AUTH_MODE=none`**, same origin. Works with no key in the page.
- **With `api_key` mode:** a key in a static page is public, so it is **not** a credential. Options:
  1. Don't support it for the browser. Document that `api_key` is for programmatic clients.
  2. A user-entered key: the page shows a small "Enter API key" dialog on 401, keeps it in `sessionStorage`, and sends `X-API-Key`.
- **Recommendation: option 2, minimal.** It exercises the 401 path and is honest about the security model. A real login or OAuth stays out of scope (Phase 4 §15).
- The key is never put in a URL or logged by the page.

## 8. Testing

The page is the new surface, so it needs real browser tests, not string checks.

- **Tooling decision:** Playwright for Python (`pytest-playwright`) keeps the toolchain in pytest and `ruff`. It adds a dev dependency and a browser download (~150 MB; disk is tight at about 2.5 GB free, noted). Alternative: jsdom via Node, lighter but doesn't run Chart.js canvas rendering faithfully. **Recommendation: Playwright, Chromium only, in `requirements-dev.txt`, tests marked `browser` and skipped if the browser isn't installed** (same pattern as the Airflow and Spark skips).
- **Contract tests (no browser):** a test loads `docs/openapi.json` and checks that every endpoint and every field path the page reads exists. The field paths are listed in one `const FIELD_MAP` in the page (or a sidecar JSON) so the test can parse it. This is the main protection against API/dashboard drift.
- **Browser tests** against a real API on the 10K warehouse (the existing fixtures):
  - No literal numbers: every KPI, chart dataset and table cell equals the API response (fetched independently in the test).
  - Each tab loads lazily; non-active tabs make no requests until opened.
  - A second visit sends `If-None-Match` and handles `304`.
  - Error states: stub 503 (data not ready), 504, 401, 422 and a network failure; the other panels stay usable; `request_id` is shown.
  - Empty states: null KPI shows "—"; empty experiment list; zero workspaces.
  - Experiment selector: switching experiments, and the stale-response race (delay the first reply).
  - XSS: a workspace name like `<img src=x onerror=…>` is rendered as text.
  - Health chart: bar colors are applied (reads the Chart.js dataset).
  - AI tab shows the unavailable state and makes no network call.
  - CSP: no console CSP violations on any tab.
- **Unit-level JS** (formatters, problem+json parsing) tested through the browser harness, to avoid adding a JS test runner.
- **Existing suite** must stay green: 264 passed, 2 skipped at 4D, plus ruff. The OpenAPI snapshot must not change, unless `API_DASHBOARD_PATH` adds a route. Decision: `GET /` is excluded from the schema (`include_in_schema=False`), so the snapshot is unchanged.
- **Data-sensitivity check:** run the pipeline with a second dataset (as the Phase 4 sensitivity test does) and confirm the rendered page changes.

## 9. Layout decisions needed for the new panels

NPS and support data exist but have no home on the page. Options:
- **A (recommended):** add a **"Voice of customer"** section on the Overview tab (NPS trend plus support summary). No new tab, so the six-tab nav is preserved and the AI tab stays last.
- **B:** a seventh "Support & NPS" tab.
- **C:** don't show them this phase.

## 10. Phase 6 preview (not part of this plan)

The grounded AI analyst: a server-side endpoint calling an LLM API (key stays on the server), tool use over the **existing read-only gold/analytics grants** or over the existing API endpoints as tools, conversation history, rate limiting, prompt-injection-aware tool design, and an evaluation set of questions with known answers. Planned separately because it adds a secret, a vendor dependency, cost and its own security review.

## 11. Open decisions (need your input)

| # | Decision | Recommendation |
|---|---|---|
| 1 | CSP: script hash + `addEventListener` refactor, or `'unsafe-inline'` | Hash + refactor |
| 2 | Chart.js: vendor it, or CDN with SRI | Vendor it |
| 3 | Browser key entry on 401 (option 2 in §7) | Yes, minimal |
| 4 | NPS and support layout (§9) | Option A |
| 5 | Playwright dev dependency and browser download, given about 2.5 GB free disk | Yes, Chromium only |
| 6 | Footer: real repo URL (you've no remote yet) or remove | Remove until a remote exists |
| 7 | Should `README.md` drop the "Live Dashboard" placeholder links now? | Yes |
| 8 | Merge `phase-4` before starting, or branch `phase-5` from it | Branch from `phase-4`; merging is your call |

## 12. Work order and validation

Each step ends green (`pytest`, `ruff`) and is a commit on `phase-5`.

0. **Baseline.** Branch from `193be5f`. Start Docker, run the pipeline at 10K, confirm `/api/health/ready` is `ready`. Capture every endpoint's response as a fixture for tests. Confirm units (fractions vs percents) and the exact field paths from the schemas.
1. **Serving.** `API_DASHBOARD_PATH`, the dashboard CSP, the file ETag, Docker wiring, and tests for `GET /`, `HEAD /`, headers, 404 elsewhere, and an unchanged OpenAPI snapshot.
2. **Data layer.** `api()`, problem+json parsing, ETag map, formatters, panel state helper, `FIELD_MAP`. No visual change yet.
3. **Overview tab.** Meta bar, KPIs, DAU, adoption, revenue, resolution rate; remove the Overview insight.
4. **Retention and Activation tabs.** Heatmap and funnel as DOM nodes; hide the week-1 features panel.
5. **Experiments tab.** Selector, evaluation cards, verdict, curve, A/A label, stale-response guard.
6. **Customer Health tab.** Tiers, histogram with correct colors, safe table.
7. **NPS and support section** (per decision 4).
8. **Removals.** AI tab placeholder, remaining literals, footer and README links, `PROJECT_CONTEXT`, fallback.
9. **Hardening.** CSP hash and listener refactor, Chart.js vendoring, optional key dialog.
10. **Browser, contract and sensitivity tests**, then full `pytest`, `ruff`, `python -m api.openapi --check`.
11. **Docker validation.** Rebuild, open the page against the container, run the browser suite against it, warehouse fingerprint before and after (must be identical).
12. **`PHASE_5_REPORT.md`**, and `docs/architecture.md`, `docs/api.md` and README updates.

## 13. Acceptance criteria

- A scripted search finds **no numeric literals** that render as data in `index.html` (an allow-list covers axis ticks and layout constants), and the browser tests show every displayed value equals the API.
- Every panel shows loading, empty and error states; one failing endpoint never blanks another panel.
- The page never calls `innerHTML` with API data; the XSS test passes.
- `GET /` serves the page with its CSP; no CSP violations in the console; `/api/*` responses keep their strict CSP.
- Conditional requests work (`304`) from the page; the stale-response test passes.
- The AI tab makes no network request and no longer contains canned answers or fabricated context.
- `docs/openapi.json` is unchanged; the full suite and ruff are green; the warehouse fingerprint is identical before and after.
- The secret scan is clean over all new files; no key or password appears in the page, image or logs.
- `PHASE_5_REPORT.md` lists deviations, skipped tests and limitations, including anything not verified.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Real data looks worse than the mock (flat retention, A/A experiment "ships", small tiers) | Show it as it is, with the API's caveats. Don't smooth or hide it. Reported in the Phase 5 report. |
| Unit mismatches (fraction vs percent) | Confirm in step 0, single formatter module, one test per formatter. |
| A CSP that's too strict silently blanks charts | CSP violation check in every browser test. |
| Disk (about 2.5 GB free) vs Playwright browser and Docker image rebuilds | Chromium only; prune old images first; report disk before and after. |
| Dashboard drifts from the API later | The `FIELD_MAP` contract test against `docs/openapi.json`. |
| Scope creep into the AI analyst | Held to the placeholder; Phase 6 is separate. |
| Cold-start latency (first overview miss was about 494 ms in 4D) | Parallel panel loading with skeletons; no blocking spinner on the whole page. |
