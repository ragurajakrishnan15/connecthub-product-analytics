# Phase 5 Report: Dashboard on Real Data

1. **Date:** 2026-10-06
2. **Branch:** `phase-5`
3. **Starting commit:** `193be5f` (Phase 4D). Phases 4A–4D (`0a35c91`, `3fa11e3`, `307ed88`, `193be5f`) are all in the history.
4. **Final commit:** the commit that contains this report (`git log -1`; a file cannot contain its own commit's hash). The implementation is complete at `ad8c73b`; this report is the only change after it.
5. **Result:** `index.html` now renders **only data returned by the Phase 4 API**. No analytics number is a literal in the page. The AI Analyst is deferred to Phase 6.
6. **Not done:** pushing, merging, tagging, rewriting history, installing Playwright/Chromium (see §11), vendoring Chart.js (done afterwards, see §19), the AI analyst.

| Commit | Step |
|---|---|
| `34c63de` | Phase 5 plan |
| `e70367f` | Step 1: serve the dashboard through the API with a hash-based CSP |
| `d0a71d9` | Step 2: the dashboard data layer |
| `ad8c73b` | Step 3: wire every panel to the API |
| *(this commit)* | Final validation and this report |

---

## 1. Summary of what changed

| Area | Before | After |
|---|---|---|
| Data | Every KPI, series, table row and "AI answer" was a literal in `index.html` | Every displayed value comes from one of 13 API endpoints, through one client |
| Serving | `index.html` had to be opened as a file | `GET /` on the API container serves it, same origin, with its own CSP |
| CSP | n/a (API-only policy) | Derived from the page at startup: script and style hashes, one exact CDN URL (since replaced by `'self'`, see §19), `style-src-attr 'none'`, no `unsafe-inline`, no `unsafe-hashes` |
| Failure handling | None (nothing could fail) | Per-panel loading, empty and error states, request id, Retry; stale responses dropped |
| API key | n/a | 401 dialog, `sessionStorage`, `X-API-Key` header only, one retry |
| AI Analyst | Canned answers with invented numbers | "AI Analyst coming in Phase 6" placeholder, nothing interactive |
| Phase 4 code | Frozen | Extended additively in 4 `api/` files (the serving hook); no behavior changed |

## 2. Files changed since `193be5f` (29 files, +5,663 / −494, not counting this report)

| File | Change | Purpose |
|---|---|---|
| `api/dashboard.py` | new (185 lines) | Reads the page once, derives the CSP, serves `GET /` (ETag/304, `no-cache`) |
| `api/main.py`, `api/settings.py`, `api/__main__.py` | +7 lines each | `API_DASHBOARD_PATH` setting; mount the route; fail fast at startup with a clear message |
| `index.html` | rewritten (1,818 lines) | The data layer, panel models, panels, new styles; all mock data and the canned analyst removed |
| `docker/api/Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example` | small | Put `index.html` in the image and set `API_DASHBOARD_PATH` for the compose service |
| `docs/api.md` | +44 lines | Dashboard section: serving, CSP table, data layer, panel table, key handling |
| `tests/api/test_api_dashboard.py` | new (28 tests) | Serving, headers, ETag/304, CSP content, startup refusals |
| `tests/dashboard/data_layer.test.mjs` | new (104 tests) | The data layer, extracted from the page and run in Node against a fake `fetch` |
| `tests/dashboard/panel_models.test.mjs` | new (53 tests) | Panel models and page-level checks (no mock data, no unsafe sinks, every element id exists) |
| `tests/dashboard/fixtures/*.json` (14) | new | Trimmed real API responses |
| `tests/test_dashboard_data_layer.py` | new (1 test) | Runs the Node tests under `pytest` (skips if Node is missing) |
| `PHASE_5_PLAN.md`, `PHASE_5_REPORT.md` | new | Plan and this report |

**Not changed:** `pipeline/`, `dbt_project/`, `analytics/`, `experimentation/`, `docs/openapi.json`, the warehouse, and the cache, ETag, authentication and endpoint code.

## 3. Dashboard-to-endpoint mapping

| Panel | Element | Endpoint | Notes |
|---|---|---|---|
| Top bar | `topbar-pill`, `topbar-date` | `/api/meta` | "Data as of" uses the last validated run, not the clock; shows the data window and the dataset label |
| KPI cards (6) | `panel-kpis` | `/api/overview` | Value by the API `unit`; change in pp for rates, % for money and counts, points for NPS; tooltip carries the API definition and both periods |
| Daily Active Users | `chart-dau` | `/api/engagement?granularity=month` | Partial first month starred |
| Feature Adoption | `chart-adoption` | `/api/feature-adoption` | All 7 features; plots `observed_rate` (the cumulative rate understates later days) |
| Revenue by Plan | `chart-revenue` | `/api/revenue?group_by=plan_tier` | Stacked, $K; plan order from `/api/meta` |
| AI Agent Resolution | `chart-agent` | `/api/support` (12-month window, monthly) | |
| Voice of Customer (new) | `voc-*`, `chart-nps-*` | `/api/nps`, `/api/support` | NPS, tickets, AI resolution, CSAT, NPS trend and by plan; trailing 12 months from `/api/meta`'s data window |
| Cohort matrix | `heatmap-container` | `/api/cohorts` | 26 cohorts; unobserved weeks shown as "—" |
| Activation funnel | `funnel-container` | `/api/activation` | Subtitle states the last day with a complete 14-day window |
| Time to milestone | `chart-time-activation` | `/api/activation` | 75th percentile and reach in the tooltip |
| Experiments | `exp-select`, `exp-body` | `/api/experiments` → `/api/experiments/{id}` | Selector from the real list; default = first A/B experiment |
| Experiment curve | `chart-exp-time` | `/api/experiments/{id}` | Control dashed, treatment solid |
| Health tiers, distribution | `panel-tiers`, `chart-health-dist` | `/api/customer-health` | One color per bin from the bin's tier |
| At-risk table | `health-table-body` | `/api/customer-health/workspaces?sort=health_score&order=asc&limit=10` | |
| AI Analyst | `tab-ai` | none | Phase 6 placeholder |

`/api/retention` is available in the client but no panel uses it: the heatmap uses `/api/cohorts` and week-4 retention comes from the overview KPI (as the plan mapped it).

**Removed:** the three fabricated "Key Insight" boxes, "Retention by Features Adopted" (its model was never built), the canned analyst and its `PROJECT_CONTEXT`, the "Live" pill, the placeholder GitHub footer link, and every literal series and table.

## 4. Serving and CSP implementation

`API_DASHBOARD_PATH` (unset by default) makes the app serve that one file at `GET /`. `api/dashboard.py` reads it once at startup and **derives the policy from the page**, so the policy always matches the file:

```
default-src 'none'
script-src  'sha256-<the one inline script>' https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js
style-src   'sha256-<the one inline style>' https://fonts.googleapis.com
style-src-attr 'none'
font-src    https://fonts.gstatic.com
connect-src 'self'
base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

- No `unsafe-inline`, no `unsafe-eval`, no `unsafe-hashes`. The CDN script is allowed by its **exact URL**, not by host.
- Startup is refused (exit 2, message names `API_DASHBOARD_PATH`) if the page has inline event handlers, `javascript:` URLs, an external script or stylesheet outside the allow-list, is over 2 MiB, or is not UTF-8. Error messages never echo file content.
- Line endings are normalized to LF before hashing and serving, so Windows and Linux checkouts give the same policy.
- The route is public and outside the OpenAPI contract; `/api/*` keeps `default-src 'none'; frame-ancestors 'none'`. `X-Frame-Options`, `nosniff` and `no-referrer` apply to the page too.
- **Evolution:** Step 1 shipped `style-src-attr 'unsafe-hashes'` for 16 static style attributes. Step 3 rebuilt the markup with CSS classes, so the exception is gone.

## 5. Data layer architecture

One client (`createApiClient`) between `DATA LAYER: BEGIN/END` markers; it touches no `window`, `document` or `location` (they are injected), so Node runs the shipped code.

- **Requests:** credential-less `GET`s to the page's own origin; undeclared parameters are refused client-side; repeatable parameters (`features`) are sent repeated; identical concurrent requests share one fetch. The endpoint table is checked against `docs/openapi.json` by a test.
- **ETag/304:** per-URL `If-None-Match` from a bounded in-memory cache; a `304` reuses the stored response, returned as a private copy; a `304` with nothing stored is retried once and a second one is a protocol error.
- **Errors:** `ApiError` with a kind (`network`, `timeout`, `aborted`, `auth`, `validation`, `not-found`, `unavailable`, `server-timeout`, `server`, `parse`, `protocol`, `config`), the problem type, request id and `Retry-After`. A non-JSON error body is never echoed. The 10 s budget covers reading the body; an already-aborted request makes no request.
- **Empty data:** a successful response with nothing to draw is flagged `empty` by a structural per-endpoint check.
- **Panels:** `createPanelLoader` gives loading/ready/empty/error with a stale-response guard; `definePanel` adds the state box, Retry button, request id and the API's caveats. 13 panels plus the top bar; the Overview loads at start and every other tab on first open, so one failing endpoint never blanks another panel.

## 6. Authentication behavior

Unchanged server-side (Phase 4 `API_AUTH_MODE`). In the browser:
- With auth off (the default), no key is involved.
- On a 401 the page opens one key dialog (however many panels failed), keeps the key in `sessionStorage` (this tab only; memory if storage is blocked), sends it only as `X-API-Key`, and retries once. A rejected key is forgotten and the dialog says "invalid". The key is never in a URL, the source, a log or the served page. Same-origin only: there is no alternative-API-host override, so a key can only reach the origin that served the page.
- `GET /` stays public; the data endpoints enforce the key.

## 7. Tests and exact results

Run on the committed tree at `ad8c73b` (clean working tree):

| Check | Result |
|---|---|
| Full `pytest` | **295 tests: 293 passed, 2 skipped, 0 failed, 0 errors** in 423.3 s |
| of which `tests/api` | **170 passed** (Phase 4D: 142; +28 dashboard serving tests) |
| of which the Node wrapper | 1 passed, running **157 Node tests** (data layer 104 + panel models and page 53), 0 failed |
| Skipped | `tests/test_dag.py`, `tests/test_spark_jobs.py` (Airflow and Spark are not in the dev venv; unchanged since Phase 4) |
| `ruff check .` | All checks passed |
| `python -m api.openapi --check` | Snapshot matches (16 paths); `docs/openapi.json` unchanged |

Suite size by step: 264 (Phase 4D) → 292 (Step 1) → 293 (Step 2) → 293 (Step 3; the new Node tests run inside the one wrapper).

## 8. Live API validation (committed page, running container)

- **Endpoint comparison:** the shipped data layer was run with real HTTP against the container. All 15 endpoint calls (14 endpoints, both experiments) returned data **identical to the Step 0 baseline** (ignoring `generated_at`), with the same ETags.
- **ETag/304:** each of the 15 went 200 → 304 on the repeat, reusing the stored copy.
- **Errors:** unknown experiment → `not-found`/`experiment-not-found` with request id; bad enum → 422 `validation` with field errors; reversed range → 400 `invalid-range`; undeclared parameter → refused before any request; 1 ms budget → `timeout`; closed port → `network`.
- **Key flow** (throwaway container with a random 40-character key, since removed): page without a key 200; data without a key 401; wrong key 401; right key 200; 401 → prompt once → retry → 200 and revalidation 304 with the key; a wrong stored key → reason "invalid", no retry loop; `/api/health` open. The key appears in neither the container logs nor the served page.
- **Served page:** byte-identical to the committed `index.html` (SHA-256 match, LF-normalized).

## 9. Browser validation (real Chromium-based browser, installed Edge, headless)

A scratch copy of the page plus a driver script was served by a throwaway container **under the real CSP** and executed in headless Edge. The driver opens every tab, fetches the API independently, and compares what the DOM and the Chart.js datasets show:

- **54 of 54 checks passed; 0 CSP violations; 0 uncaught JS errors.**
- Values checked: top bar; all 6 KPI values and the tooltip definition; DAU, adoption (7 curves), revenue (stacks to the API MRR, Enterprise first), AI-resolution, NPS-by-plan charts; the 4 Voice-of-Customer cards; heatmap row count, 78 of 78 gaps shown as "—", a cell value, colors applied through the DOM; funnel counts and bar widths; milestone medians; the selector options, the default A/B experiment, verdict text and class, guardrail cards, the curve, the A/A banner, and quick-switch ordering; the 4 tier counts; 20 histogram bins with 4 distinct colors; the table rows in API order and sorted ascending; the AI tab as a placeholder with no input or button; no week-1-features panel.
- **States, on the page's real panels with a stubbed client:** loading, error (title, message, request id, Retry; stale content hidden; Retry reloads), empty, and `<img onerror>` text in an API string stays text with no element created.
- Only the Overview's 7 data requests were made at load; other tabs stayed lazy.
- Screenshots of all five data tabs were reviewed by eye.
- **The key dialog** was verified separately in a real DOM: modal, focused, password field, invalid key refused, submit, Cancel and Escape, and the full 401 → dialog → retry flow.

**Important:** this is **not** the planned Playwright suite. The driver is a scratch script and is **not committed**, and nothing in CI runs it. See §11.

## 10. Data-integrity and security validation

**Data integrity:** no analytics literal remains in the page.
- Tests (committed, in the Node suite) assert the page has none of the known mock tokens (`42,891`, `$2.4M`, `Acme Corp`, `Onboarding V2`, `PROJECT_CONTEXT`, `Key Insight`, `yourusername`, …), no literal numeric data array, no `Math.random`, no hard-coded date, and that every element id the script uses exists. The data layer's only numeric literals are timeouts, limits, HTTP statuses and indexes.
- Presentation constants that remain: colors, the tier-to-color map, the heatmap's color thresholds, and display order.
- Every value shown was compared to the API in §9; everything else (labels, wording) is static text.

**Security:**
- API strings reach the page only as text: no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval` anywhere in the page (tested), and an HTML payload in an API string stays text (browser test).
- The markup has no style attributes and no inline handlers.
- No secret in the whole Phase 5 diff: none of the 5 secret values from `.env` appears in it, no `.env` file is tracked, and the only key-like literal added is the test sentinel `'SECRET-TOKEN-1234'`. No secret in the API container logs or the served page. The browser receives no database credentials or server secrets.
- Same-origin only; the key goes only to the page's own origin.

## 11. Acceptance criteria (PHASE_5_PLAN.md §13)

| Criterion | Status |
|---|---|
| No numeric literals render as data; displayed values equal the API | **Met** (§9, §10) |
| Every panel has loading, empty and error states; one failure never blanks another | **Met** (tested in a real browser) |
| No `innerHTML` with API data; the XSS test passes | **Met** |
| `GET /` serves the page with its CSP; no console violations; `/api/*` keeps its strict policy | **Met** |
| Conditional requests work from the page; the stale-response test passes | **Met** |
| The AI tab makes no request and holds no canned answers | **Met** |
| `openapi.json` unchanged; suite and Ruff green; fingerprint identical | **Met** |
| Secret scan clean | **Met** |
| **Browser tests pass (Playwright)** | **Not met as specified.** Substituted by the uncommitted Edge driver above. Playwright/Chromium was not installed because Docker storage is still on C: (§14) |
| This report lists deviations, skipped tests and limitations | Met |

## 12. Warehouse fingerprint

| | Result |
|---|---|
| Step 0 baseline (before any Phase 5 change) | 35 relations, 2,112,759 rows |
| Now (after the full suite, all HTTP and browser validation) | **identical**: `{"relations": 35, "identical": true, "differing": []}` |

## 13. Phase 4 regression check

- API suite 170/170; OpenAPI snapshot unchanged; all 15 endpoint responses and ETags identical to the baseline; cache and ETag behavior unchanged (a conditional request still gets a 304 with an `X-Cache` hit); authentication unchanged (401 without or with a wrong key).
- The only Phase 4 code touched is the additive serving hook: `api/dashboard.py` (new) and +7 lines each in `api/main.py`, `api/settings.py`, `api/__main__.py`. `git diff 193be5f HEAD -- pipeline dbt_project analytics experimentation docs/openapi.json` is empty.

## 14. Environment

The API and Postgres containers are healthy and `/api/health/ready` is `ready`. C: has about 5.6 GB free and D: about 37 GB. **Docker storage is still on C:**: Docker has no custom data folder, and the C: `docker_data.vhdx` (49.1 GB) is the one in use. Two more 49.1 GB `docker_data.vhdx` files sit on D: (`D:\Docker\DockerDesktopWSL` and `D:\DockerDesktopWSL`, last modified 2026-10-05 evening); they look like earlier move attempts. I did not touch them. This is why the Playwright gate stays closed.

## 15. Known limitations

1. **No committed browser test.** The 54-check browser run and the dialog check are scratch scripts. The Node tests prove the logic and the page's static properties, not rendering. A Playwright suite is the follow-up once storage is sorted.
2. **Only one browser engine was exercised** (Chromium-based Edge, headless). Firefox and Safari are untested. No mobile-layout or accessibility audit was done.
3. **Chart.js loaded from cdnjs at the time of this report.** *Resolved afterwards: Chart.js is now vendored and served from the same origin (§19).* The guard for a missing `Chart` global (panels show an error state rather than the script halting) is still not exercised in a browser.
4. **Fonts still load from Google Fonts** (`fonts.googleapis.com`, `fonts.gstatic.com`).
5. **The Node tests are skipped if Node is missing**, silently, as one skipped-by-condition test.
6. **Small dataset, and the page shows it as it is.** 10K users: DAU 671, MRR $61,870, 30% of workspaces Critical. Not exercised at larger scale.
7. **Two NPS values on one page:** the KPI card is the last 90 days, the Voice-of-Customer card is trailing 12 months. Both are labelled.
8. **The A/A experiment currently reads SHIP** (+13.7%, p=0.019): a designed false positive. The page labels it as an A/A check and shows the API's no-correction caveat.
9. **Heatmap is 26 rows** (the API default) and scrolls; the DAU chart's first month is partial and starred.
10. **`/api/retention` has a client method but no panel.**
11. **Phase 4 limitations carry over:** a rebuild without a validated run can serve cached responses for up to 5 minutes; per-worker caches; benchmarks at 10K users only.
12. **Documentation not yet updated:** `README.md` and `docs/architecture.md` still describe the dashboard as a static prototype (`docs/api.md` is current).
13. **Pre-existing, not created by this phase:** a `v3.0` tag and an `origin` remote exist; `phase-5` has no upstream. `FALL 2026 FEE PAYMENT.pdf` is untracked in the repo folder and should be moved out.

## 16. Deviations from the plan

1. **Plan steps 3–8 were done as one step** (one commit), as the Step 3 brief asked.
2. **No `?api=` alternative-host override** (plan §3). You asked for same-origin, and an overridable base would let a crafted link capture a typed key.
3. **The key dialog came in Step 2**, not the plan's hardening step.
4. **CSP is stricter than planned:** no `unsafe-hashes` at all (§4).
5. **Number formatters live in the panel models** (Step 3), not the data layer; they follow the API `unit`.
6. **`PHASE_5_PLAN.md` panel mapping changes:** `/api/retention` is not used; the Voice-of-Customer cards are individual panels so an NPS failure and a support failure show separately; all 7 adoption curves are shown (the mock showed 4); `observed_rate` is plotted; the page adds the API caveats under each panel.
7. **Footer link removed**, not set to the real repository URL (the repo does have a remote; I did not publish your URL without your say-so).
8. **The browser validation used installed Edge with scratch drivers**, not Playwright (§9, §11).
9. **Four implementation commits plus this report**, rather than the single commit you first described; each followed a step you approved.

## 17. Reproduce

```powershell
# environment: load .env into the process (PowerShell), then
docker compose up -d postgres api-init api         # API + dashboard on http://127.0.0.1:8002/ (port from API_PORT)
.\.venv\Scripts\python.exe -m pytest -q            # 293 passed, 2 skipped
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m api.openapi --check
.\.venv\Scripts\python.exe -m pipeline fingerprint --compare <baseline.json>   # warehouse unchanged
node --test tests/dashboard                        # 157 Node tests (also run by pytest)
```

## 18. Next steps (not started)

1. Move Docker storage off C: (your decision on the three `docker_data.vhdx` files), then install Playwright (Chromium only) and turn the §9 driver into a committed suite.
2. ~~Vendor Chart.js~~ (done, §19). Optionally vendor the fonts too and drop the Google hosts from the CSP.
3. Update `README.md` and `docs/architecture.md`.
4. Phase 6: the grounded AI analyst.
5. Your call: merging `phase-4`/`phase-5`, and what to do about the `origin` remote.

---

## 19. Addendum: Chart.js vendored (after commit `1962fba`)

Chart.js no longer loads from a CDN. It is served by the API from the same origin, so the dashboard needs no external script host.

- **File:** `vendor/chart.umd.js`, 205,087 bytes, SHA-256 `0ee28337f25838a7a5d6b1e8b2b02279ab10e80e17c217a99893a7a717f2ba05`. It is npm `chart.js@4.4.1` `dist/chart.umd.js` (tarball integrity checked against the registry) with exactly one change: its final `//# sourceMappingURL=chart.umd.js.map` line is removed, so browsers do not request a map the server does not ship. Appending that line reproduces the npm file byte for byte (SHA-256 `74401d73…`, tested). The MIT banner is kept; `vendor/LICENSE-chartjs.md` is the license from the same package. `vendor/README.md` records the provenance and the update steps.
- **Why npm's build and not cdnjs's `chart.umd.min.js`:** the cdnjs `.min.js` is a separately re-minified build with no license banner (same version and the same exported API, but not the registry artifact). The npm file equals cdnjs's own `chart.umd.js`.
- **Serving:** `index.html` has `<script src="vendor/chart.umd.js" integrity="sha384-…">`. `api/dashboard.py` reads the file at startup and serves it at `GET /vendor/chart.umd.js` (`ETag`/`304`, public, outside OpenAPI). Startup is refused unless the `src` is a plain relative `.js` path inside the page's directory and its `integrity` matches the file; any remote script is refused; only the named file is served (README, license and `.map` are 404). The Dockerfile copies `vendor/` into the image. `.gitattributes` marks `vendor/**` as `-text` so Git never converts its line endings.
- **CSP:** only `script-src` changed, from `'sha256-<inline>' https://cdnjs.cloudflare.com/…/chart.umd.min.js` to `'sha256-<inline>' 'self'`. The inline-script hash is identical; there is no `unsafe-inline`, `unsafe-eval`, wildcard or remote script host. Everything else, including `style-src-attr 'none'` and `connect-src 'self'`, is unchanged.
- **Tests:** full `pytest` **337 passed, 2 skipped, 0 failed** (was 293 + 2), of which `tests/api` is 214 (the dashboard serving and vendoring tests are 72, up from 28) and the Node tests are 158 (was 157). Ruff passes and the OpenAPI snapshot is unchanged.
- **Browser (headless Edge, real CSP):** 54/54 end-to-end checks, 0 CSP violations, 0 JS errors, 0 integrity or source-map warnings; all overview charts drawn from the vendored file; `Chart.version` is `4.4.1`. The browser's own Resource Timing list shows only Google Fonts, `/vendor/chart.umd.js` and API calls: no CDN, no `.map`.
- **Phase 4 and warehouse:** untouched. The warehouse fingerprint is identical to Step 0 after the full suite (35 relations, 2,112,759 rows); 15 live endpoint calls are identical to the baseline with the same ETags. The only Phase 4 code touched is the dashboard loader added in Step 1 (`api/dashboard.py`).
- **Still external:** Google Fonts (`fonts.googleapis.com`, `fonts.gstatic.com`). Vendoring the fonts is optional follow-up work.
