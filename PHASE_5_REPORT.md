# Phase 5 Report: Dashboard on Real Data

1. **Date:** 2026-10-06
2. **Branch:** `phase-5`
3. **Starting commit:** `193be5f` (Phase 4D). Phases 4A–4D (`0a35c91`, `3fa11e3`, `307ed88`, `193be5f`) are all in the history.
4. **Final implementation commit:** `2e0fe75` (the Playwright browser suite). This report was first committed in `1962fba` (when the implementation was complete at `ad8c73b`), extended by §19 and §20, and reconciled with the final state in §21; a file cannot contain the hash of the commit that contains it, so the reconciliation commit is not named here.
5. **Result:** `index.html` now renders **only data returned by the Phase 4 API**. No analytics number is a literal in the page. The AI Analyst is deferred to Phase 6.
6. **Not done:** pushing, merging, tagging, rewriting history, the AI analyst (Phase 6), and the plan's second-dataset sensitivity check (§15, §16). Done after the first version of this report: the Playwright suite (§20) and vendoring Chart.js (§19).

| Commit | Step |
|---|---|
| `34c63de` | Phase 5 plan |
| `e70367f` | Step 1: serve the dashboard through the API with a hash-based CSP |
| `d0a71d9` | Step 2: the dashboard data layer |
| `ad8c73b` | Step 3: wire every panel to the API |
| `1962fba` | Final validation report (the first version of this document) |
| `fcc4f43` | Vendor Chart.js 4.4.1 and serve it from the dashboard's own origin (§19) |
| `abee9e1` | Docs: README and `docs/architecture.md` describe the dashboard as it works now |
| `2e0fe75` | **Phase 5: add Playwright browser validation** (42 tests; §20) |
| *(reconciliation)* | This report reconciled with the final state (§21) |

---

## 1. Summary of what changed

| Area | Before | After |
|---|---|---|
| Data | Every KPI, series, table row and "AI answer" was a literal in `index.html` | Every displayed value comes from 13 of the API's 14 data endpoints (all but `/api/retention`), through one client |
| Serving | `index.html` had to be opened as a file | `GET /` on the API container serves it, same origin, with its own CSP |
| CSP | n/a (API-only policy) | Derived from the page at startup: script and style hashes, one exact CDN URL (since replaced by `'self'`, see §19), `style-src-attr 'none'`, no `unsafe-inline`, no `unsafe-hashes` |
| Failure handling | None (nothing could fail) | Per-panel loading, empty and error states, request id, Retry; stale responses dropped |
| API key | n/a | 401 dialog, `sessionStorage`, `X-API-Key` header only, one retry |
| AI Analyst | Canned answers with invented numbers | "AI Analyst coming in Phase 6" placeholder, nothing interactive |
| Phase 4 code | Frozen | Extended additively in 4 `api/` files (the serving hook); no behavior changed |

## 2. Files changed since `193be5f`

In the first version of this report (at `ad8c73b`): 29 files, +5,663 / −494, not counting the report. **Final, at `2e0fe75` (`git diff phase-4...phase-5 --stat`): 42 files, +7,301 / −519**, which adds the later work listed in the last rows of the table.

| File | Change | Purpose |
|---|---|---|
| `api/dashboard.py` | new (185 lines at Step 1; 263 after Chart.js vendoring) | Reads the page once, derives the CSP, serves `GET /` (ETag/304, `no-cache`) and the vendored script |
| `api/main.py`, `api/settings.py`, `api/__main__.py` | +7 lines each | `API_DASHBOARD_PATH` setting; mount the route; fail fast at startup with a clear message |
| `index.html` | rewritten (1,818 lines) | The data layer, panel models, panels, new styles; all mock data and the canned analyst removed |
| `docker/api/Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example` | small | Put `index.html` in the image and set `API_DASHBOARD_PATH` for the compose service |
| `docs/api.md` | +44 lines at Step 3 (+45 final) | Dashboard section: serving, CSP table, data layer, panel table, key handling |
| `tests/api/test_api_dashboard.py` | new (28 tests at Step 1; 72 after vendoring) | Serving, headers, ETag/304, CSP content, startup refusals, vendored-script checks |
| `tests/dashboard/data_layer.test.mjs` | new (104 tests) | The data layer, extracted from the page and run in Node against a fake `fetch` |
| `tests/dashboard/panel_models.test.mjs` | new (53 tests) | Panel models and page-level checks (no mock data, no unsafe sinks, every element id exists) |
| `tests/dashboard/fixtures/*.json` (14) | new | Trimmed real API responses |
| `tests/test_dashboard_data_layer.py` | new (1 test) | Runs the Node tests under `pytest` (skips if Node is missing) |
| `PHASE_5_PLAN.md`, `PHASE_5_REPORT.md` | new | Plan and this report |
| `vendor/chart.umd.js`, `vendor/LICENSE-chartjs.md`, `vendor/README.md` | new (`fcc4f43`) | Vendored Chart.js 4.4.1, its MIT license and provenance (§19) |
| `.gitattributes` | +3 (`fcc4f43`) | `vendor/** -text`: Git never converts the vendored bytes |
| `tests/browser/browserlib.py`, `conftest.py`, `test_dashboard_browser.py` | new (`2e0fe75`) | The Playwright browser suite, 42 tests (§20) |
| `requirements/browser.txt`, `requirements/constraints-py311.txt`, `pyproject.toml` | new / +2 / +1 (`2e0fe75`) | Optional Playwright dependency, its pins, and a `browser` marker |
| `README.md`, `docs/architecture.md` | updated (`abee9e1`, `2e0fe75`) | Describe the dashboard, the vendored Chart.js and the browser suite |

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
- **The policy block above is as of Step 3.** Chart.js vendoring (`fcc4f43`) replaced the CDN script source with `'self'` (the final policy is in §19), and the loader now also serves a page-relative, integrity-checked script and refuses any remote script.

## 5. Data layer architecture

One client (`createApiClient`) between `DATA LAYER: BEGIN/END` markers; it touches no `window`, `document` or `location` (they are injected), so Node runs the shipped code.

- **Requests:** credential-less `GET`s to the page's own origin; undeclared parameters are refused client-side; repeatable parameters (`features`) are sent repeated; identical concurrent requests share one fetch. The endpoint table is checked against `docs/openapi.json` by a test.
- **ETag/304:** per-URL `If-None-Match` from a bounded in-memory cache; a `304` reuses the stored response, returned as a private copy; a `304` with nothing stored is retried once and a second one is a protocol error.
- **Errors:** `ApiError` with a kind (`network`, `timeout`, `aborted`, `auth`, `validation`, `not-found`, `unavailable`, `server-timeout`, `server`, `parse`, `protocol`, `config`), the problem type, request id and `Retry-After`. A non-JSON error body is never echoed. The 10 s budget covers reading the body; an already-aborted request makes no request.
- **Empty data:** a successful response with nothing to draw is flagged `empty` by a structural per-endpoint check.
- **Panels:** `createPanelLoader` gives loading/ready/empty/error with a stale-response guard; `definePanel` adds the state box, Retry button, request id and the API's caveats. **12 panels** (12 `definePanel` calls) plus the top-bar loader (an earlier version of this report said 13, which counted the function's own definition line); the Overview loads at start and every other tab on first open, so one failing endpoint never blanks another panel.

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

Suite size by step: 264 (Phase 4D) → 292 (Step 1) → 293 (Step 2) → 293 (Step 3; the new Node tests run inside the one wrapper). Later runs: 337 + 2 skipped after Chart.js vendoring (§19) and **379 + 2 skipped with the browser suite (§20, final)**.

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

**Superseded:** when this report was written, this run was a scratch script (installed Edge), not the planned Playwright suite, and was not committed. Its checks are now a committed Playwright/Chromium suite (`tests/browser`, §20).

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
| **Browser tests pass (Playwright)** | **Met** (commit `2e0fe75`, §20): 42 Playwright/Chromium tests in `tests/browser` pass, and the full suite is 379 passed, 2 skipped. At the time of the first version of this report it was **not** met: it was substituted by an uncommitted Edge driver because Docker storage was still on C:. That history is kept in §9 |
| This report lists deviations, skipped tests and limitations | Met (§15, §16) |

Status is against `PHASE_5_PLAN.md` §13. Plan items outside §13 that were not delivered, such as the second-dataset sensitivity check in plan §8, are listed in §16.

## 12. Warehouse fingerprint

| | Result |
|---|---|
| Step 0 baseline (before any Phase 5 change) | 35 relations, 2,112,759 rows |
| Now (after the full suite, all HTTP and browser validation) | **identical**: `{"relations": 35, "identical": true, "differing": []}` |

## 13. Phase 4 regression check

- API suite 170/170; OpenAPI snapshot unchanged; all 15 endpoint responses and ETags identical to the baseline; cache and ETag behavior unchanged (a conditional request still gets a 304 with an `X-Cache` hit); authentication unchanged (401 without or with a wrong key).
- The only Phase 4 code touched is the additive serving hook: `api/dashboard.py` (new) and +7 lines each in `api/main.py`, `api/settings.py`, `api/__main__.py`. `git diff 193be5f HEAD -- pipeline dbt_project analytics experimentation docs/openapi.json` is empty.

## 14. Environment

The API and Postgres containers are healthy and `/api/health/ready` is `ready`. C: has about 5.6 GB free and D: about 37 GB. **Docker storage is still on C:**: Docker has no custom data folder, and the C: `docker_data.vhdx` (49.1 GB) is the one in use. Two more 49.1 GB `docker_data.vhdx` files sit on D: (`D:\Docker\DockerDesktopWSL` and `D:\DockerDesktopWSL`, last modified 2026-10-05 evening); they look like earlier move attempts. I did not touch them. This is why the Playwright gate was closed; it was later resolved without moving Docker, by using a Chromium that was already on disk (§20). At the final commit C: had about 5 GB free (4.99 GB after the browser work) and Docker storage was unchanged.

## 15. Remaining limitations (at the final commit `2e0fe75`)

1. **Browser coverage.** The browser suite covers Chromium only (not Firefox or Safari); it runs locally, not in CI; and it needs the local PostgreSQL warehouse, like the other integration tests. No mobile-layout or accessibility audit was done.
2. **Not exercised in a browser:** the guard for a missing `Chart` global (panels show an error state rather than the script halting, but no test removes Chart.js), and `unsafe-eval`, which is asserted on the CSP header only because Playwright's `page.evaluate` is exempt from CSP's `eval` rule.
3. **External hosts that remain.** The dashboard still loads its fonts from Google Fonts (`fonts.googleapis.com`, `fonts.gstatic.com`). Separately, the Swagger/ReDoc pages (`/api/docs`, `/api/redoc`) allow `cdn.jsdelivr.net` in their own CSP (`api/middleware.py`, Phase 4B); that is unrelated to the dashboard.
4. **Tests skip rather than fail** when a prerequisite is missing: the Node tests when Node is missing, the browser tests when Playwright, Chromium or PostgreSQL is missing (verified). The two Airflow/Spark tests are skipped as before.
5. **Small dataset, shown as it is.** 10K users: DAU 671, MRR $61,870, 30% of workspaces Critical. Not exercised at larger scale.
6. **Two NPS values on one page:** the KPI card is the last 90 days, the Voice-of-Customer card is trailing 12 months. Both are labelled.
7. **The A/A experiment currently reads SHIP** (+13.7%, p=0.019): a designed false positive. The page labels it as an A/A check and shows the API's no-correction caveat.
8. **Presentation.** The heatmap is 26 rows (the API default) and scrolls; the DAU chart's first month is partial and starred; `/api/retention` has a client method but no panel.
9. **Phase 4 limitations carry over:** a rebuild without a validated run can serve cached responses for up to 5 minutes; per-worker caches; benchmarks at 10K users only.
10. **Docker storage is still on C:** (about 5 GB free), with three 49 GB `docker_data.vhdx` files (one on C:, two older ones on D:). They were never touched.
11. **Repository housekeeping, not created by this phase:** a `v3.0` tag and an `origin` remote exist; `phase-5` has no upstream and GitHub was never contacted; `FALL 2026 FEE PAYMENT.pdf` is untracked in the repository folder and should be moved out; the subject line of `1962fba` carries an invisible byte-order mark (history was not rewritten); `PROJECT_AUDIT.md` still describes the pre-Phase-4 state.

*Resolved since the first version of this report (no longer limitations):* no committed browser test (§20); Chart.js loaded from a CDN (§19); `README.md` and `docs/architecture.md` not updated (`abee9e1`, `2e0fe75`).

## 16. Deviations from the plan (at the final commit `2e0fe75`)

**Plan items not delivered**
1. **The second-dataset sensitivity check (`PHASE_5_PLAN.md` §8) was not done.** The plan said to run the pipeline with a second dataset and confirm the rendered page changes. No Phase 5 test does this (the Phase 4 API-level sensitivity test, `tests/api/test_api_sensitivity.py`, is unchanged). The browser suite proves each displayed value equals the API's response for the same request, but not that the page changes when the underlying data changes.
2. **The browser suite does not run against the built container image** (plan §12 step 11). The committed tests start the app in-process on the local warehouse. The container and its served page were verified separately, by scripts in Steps 1–3 and in §19, which were not committed.

**Done differently from the plan**
3. **No `?api=` alternative-host override** (plan §3). You asked for same-origin, and an overridable base would let a crafted link capture a typed key.
4. **No `FIELD_MAP` contract test** (plan §8, step 2). It was replaced by a test that cross-checks every endpoint path and query parameter against `docs/openapi.json`, plus trimmed real-response fixtures and the browser tests against the live API. There is no field-level map of the paths the page reads.
5. **JavaScript unit tests use Node's built-in test runner**, not the browser harness the plan described (an optional dependency on Node, not a new JS test library).
6. **Playwright specifics** (plan §8): the plain `playwright` package rather than `pytest-playwright`, listed in the optional `requirements/browser.txt` (with pins in the constraints file), not the dev requirements; it reuses a Chromium already on the machine.
7. **Chart.js location and build** (plan §6.2): `vendor/chart.umd.js`, the registry-verified npm build with its final `sourceMappingURL` line removed, instead of the plan's `static/vendor/chart.umd.min.js`; the page carries an integrity hash that the server also verifies at startup.
8. **`API_DASHBOARD_PATH` production gating** (plan §6.1: "not mounted in `API_ENV=production` unless explicitly enabled"): there is no separate `API_ENV` check; setting the variable is the only opt-in.
9. **Plan steps 3–8 were done as one step** (one commit), as the Step 3 brief asked, and the key dialog came in Step 2 rather than the plan's hardening step.
10. **CSP is stricter than planned:** no `unsafe-hashes` at all (§4), and no remote script host.
11. **Number formatters live in the panel models** (Step 3), not the data layer; they follow the API `unit`.
12. **Panel mapping changes:** `/api/retention` is not used; the Voice-of-Customer cards are individual panels so an NPS failure and a support failure show separately; all 7 adoption curves are shown (the mock showed 4); `observed_rate` is plotted; the page adds the API caveats under each panel.
13. **Footer link removed**, not set to the real repository URL (the repo does have a remote; I did not publish your URL without your say-so).
14. **Eight commits** (the plan, three steps, the first report, Chart.js vendoring, the docs, the Playwright suite), rather than the single commit first described; each followed a step you approved.

*Resolved since the first version of this report:* the browser validation used installed Edge with scratch drivers rather than Playwright (§9, §11); it is now a committed Playwright suite (§20).

## 17. Reproduce

```powershell
# environment: load .env into the process (PowerShell), then
docker compose up -d postgres api-init api         # API + dashboard on http://127.0.0.1:${API_PORT} (8002 on the development machine)
.\.venv\Scripts\python.exe -m pytest -q            # 379 passed, 2 skipped (this includes the browser tests when Playwright is installed)
.\.venv\Scripts\python.exe -m pytest tests/browser -q   # 42 Playwright tests; needs: pip install -r requirements/browser.txt -c requirements/constraints-py311.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m api.openapi --check
.\.venv\Scripts\python.exe -m pipeline fingerprint --compare <baseline.json>   # warehouse unchanged
node --test tests/dashboard                        # 158 Node tests (also run by pytest)
```

(The first version of this section said 293 passed and 157 Node tests: the figures at `ad8c73b`, kept in §7.)

## 18. Next steps (none started)

1. **Phase 6:** the grounded AI analyst (a server-side endpoint that keeps its key out of the browser). Not started.
2. **Your decisions:** merging `phase-4` and `phase-5`; what to do about the `origin` remote; moving `FALL 2026 FEE PAYMENT.pdf` out of the repository folder; moving Docker storage off C:.
3. **Optional hardening:** the second-dataset sensitivity check (§16); running the browser suite against the built image and in CI; vendoring the fonts and dropping the Google hosts from the CSP; dropping `cdn.jsdelivr.net` from the API docs pages' CSP.

---

## 19. Addendum: Chart.js vendored (commit `fcc4f43`, after `1962fba`)

Chart.js no longer loads from a CDN. It is served by the API from the same origin, so the dashboard needs no external script host.

- **File:** `vendor/chart.umd.js`, 205,087 bytes, SHA-256 `0ee28337f25838a7a5d6b1e8b2b02279ab10e80e17c217a99893a7a717f2ba05`. It is npm `chart.js@4.4.1` `dist/chart.umd.js` (tarball integrity checked against the registry) with exactly one change: its final `//# sourceMappingURL=chart.umd.js.map` line is removed, so browsers do not request a map the server does not ship. Appending that line reproduces the npm file byte for byte (SHA-256 `74401d73…`, tested). The MIT banner is kept; `vendor/LICENSE-chartjs.md` is the license from the same package. `vendor/README.md` records the provenance and the update steps.
- **Why npm's build and not cdnjs's `chart.umd.min.js`:** the cdnjs `.min.js` is a separately re-minified build with no license banner (same version and the same exported API, but not the registry artifact). The npm file equals cdnjs's own `chart.umd.js`.
- **Serving:** `index.html` has `<script src="vendor/chart.umd.js" integrity="sha384-…">`. `api/dashboard.py` reads the file at startup and serves it at `GET /vendor/chart.umd.js` (`ETag`/`304`, public, outside OpenAPI). Startup is refused unless the `src` is a plain relative `.js` path inside the page's directory and its `integrity` matches the file; any remote script is refused; only the named file is served (README, license and `.map` are 404). The Dockerfile copies `vendor/` into the image. `.gitattributes` marks `vendor/**` as `-text` so Git never converts its line endings.
- **CSP:** only `script-src` changed, from `'sha256-<inline>' https://cdnjs.cloudflare.com/…/chart.umd.min.js` to `'sha256-<inline>' 'self'`. The inline-script hash is identical; there is no `unsafe-inline`, `unsafe-eval`, wildcard or remote script host. Everything else, including `style-src-attr 'none'` and `connect-src 'self'`, is unchanged.
- **Tests:** full `pytest` **337 passed, 2 skipped, 0 failed** (was 293 + 2), of which `tests/api` is 214 (the dashboard serving and vendoring tests are 72, up from 28) and the Node tests are 158 (was 157). Ruff passes and the OpenAPI snapshot is unchanged.
- **Browser (headless Edge, real CSP):** 54/54 end-to-end checks, 0 CSP violations, 0 JS errors, 0 integrity or source-map warnings; all overview charts drawn from the vendored file; `Chart.version` is `4.4.1`. The browser's own Resource Timing list shows only Google Fonts, `/vendor/chart.umd.js` and API calls: no CDN, no `.map`.
- **Phase 4 and warehouse:** untouched. The warehouse fingerprint is identical to Step 0 after the full suite (35 relations, 2,112,759 rows); 15 live endpoint calls are identical to the baseline with the same ETags. The only Phase 4 code touched is the dashboard loader added in Step 1 (`api/dashboard.py`).
- **Still external:** Google Fonts (`fonts.googleapis.com`, `fonts.gstatic.com`). Vendoring the fonts is optional follow-up work.

---

## 20. Addendum: the Playwright browser suite (commit `2e0fe75`; acceptance criterion met)

The criterion "browser tests pass (Playwright)" in §11 was substituted at the time of the first version of this report by an uncommitted Edge script. It is now a committed Playwright/Chromium suite of **42 tests** (`tests/browser`), and it passes.

**What was added** (no application file changed: `index.html`, `vendor/`, `api/`, Docker, dbt, `pipeline/` and `analytics/` are untouched)

| File | Purpose |
|---|---|
| `tests/browser/test_dashboard_browser.py` | 42 browser tests |
| `tests/browser/browserlib.py`, `conftest.py` | An in-process app server on the local warehouse, the Playwright fixtures, and expected-value formatting that does not reuse the page's code |
| `requirements/browser.txt` | Optional dependency (`playwright==1.58.0`) with install notes; `requirements/constraints-py311.txt` pins `playwright` and `pyee` |
| `pyproject.toml` | A `browser` marker |
| `README.md`, `docs/architecture.md`, `docs/api.md` | Describe the suite and drop the "no automated browser tests" statements |

**How it avoids the storage problem.** Docker storage is still on C: and was not touched (the three `docker_data.vhdx` files are unchanged). No browser was downloaded: this machine already had Chromium revision 1208 in `%LOCALAPPDATA%\ms-playwright` (from another project's Playwright 1.58.0), and the pinned Playwright 1.58.0 uses exactly that revision. Only the Playwright Python package was installed, into the project venv, about 100 MB on C:. The browsers folder is still 655 MB. If the browser is missing, `requirements/browser.txt` explains how to download only Chromium and keep it on another drive (`PLAYWRIGHT_BROWSERS_PATH`).

**What the 42 tests check** (every value is compared with the API's response for the same request, read independently of the page)
- The 54 checks of the earlier Edge driver, now named tests: all overview panels ready; the top bar; 6 KPI values and tooltip; the DAU, adoption, revenue, AI-resolution and NPS charts; the Voice of Customer cards; the API caveats; lazy tab loading; the heatmap (rows, gaps, values, DOM-applied colors, no week-1 panel); the funnel and milestones; the experiment selector, default, verdict and class, cards, guardrails, curve, A/A banner and switching; tier cards, histogram colors and the table; the AI tab placeholder.
- The states, with routed stubs: loading, error (title, request id, Retry that recovers), empty, an unreachable API, a malformed response, one failing panel not blanking the others, and API strings staying text.
- New beyond the old driver: the API-key dialog end to end against a second app with `API_AUTH_MODE=api_key` (one dialog for many failing panels, a wrong key forgotten and not retried in a loop, `sessionStorage` only, the key never in a URL or the page, a rejected stored key, Cancel and Escape); the page opened as a file; that Chromium actually refuses an injected inline script, inline handler and style attribute; Chart.js 4.4.1 served from this origin; and that the page asks only its own origin and Google Fonts.
- Every test also fails on an uncaught page error, a CSP violation, or a request to any other host.

**Does it catch bugs?** Eight deliberate breaks of the page were each caught: percentages ten times too small, API text through `innerHTML`, no request id on an error, the key put in the URL, a single-color histogram, tabs loading eagerly (all by the browser suite), the superseded request not cancelled (the browser suite, after the stale-response test was strengthened because the first version did not detect a removed stale-result check), and that stale-result check removed (the Node suite; it is masked in the browser because the request is aborted first).

**Results (final tree)**

| Check | Result |
|---|---|
| Browser suite alone | **42 passed** in about 25 s |
| Full `pytest` (browser tests included, one session) | **379 passed, 2 skipped, 0 failed** in 471 s (was 337 + 2; the skips are the Airflow and Spark tests, as before) |
| Skips when a prerequisite is missing | Playwright not installed: skipped; Chromium not found: skipped; no database: skipped (verified for each; never a failure) |
| Node dashboard tests | 158 passed |
| `tests/api` | 214 passed |
| Ruff | All checks passed |
| OpenAPI snapshot | Unchanged (16 paths) |
| Warehouse fingerprint | Identical to Step 0 (35 relations, 2,112,759 rows) |
| Live API through the data layer | 15 endpoint calls (200 then 304), identical to the Step 0 baseline |
| Secrets in the new and changed files | None |

**Limits.** Only Chromium is covered (not Firefox or Safari), and the browser tests run locally, not in CI. They need the local PostgreSQL warehouse, like the other integration tests. Playwright's `page.evaluate` is exempt from CSP's `eval` rule, so `unsafe-eval` is asserted on the header, not by running `eval`. The unrelated `FALL 2026 FEE PAYMENT.pdf` is still untracked in the repository folder.

---

## 21. Reconciliation with the final committed state (`2e0fe75`)

This report was first committed in `1962fba`, extended by §19 and §20, and reconciled here with the final state. The historical results above are preserved as they were recorded; only statements that had become inaccurate were corrected or marked as superseded.

**Final state**

| | |
|---|---|
| Branch and HEAD | `phase-5`, `2e0fe75` "Phase 5: add Playwright browser validation" |
| Phase 5 commits | 8 (table at the top), no merge commits; `phase-4` is still at `193be5f` |
| Diff against `phase-4` | 42 files, +7,301 / −519 (at `2e0fe75`) |
| Full `pytest` (final run) | 379 passed, 2 skipped, 0 failed; no test-relevant file changed after that run |
| Playwright browser suite | 42 passed |
| Dashboard-serving validation | **73 tests in total: 72 serving tests** (`tests/api/test_api_dashboard.py`) **plus 1 Node wrapper test** (`tests/test_dashboard_data_layer.py`, which runs the Node tests below); all passed |
| Node dashboard tests | 158 passed |
| Ruff, OpenAPI snapshot, warehouse | all checks passed; snapshot unchanged (16 paths); 35 relations, 2,112,759 rows |
| Documentation | `README.md`, `docs/architecture.md` and `docs/api.md` describe the final state |
| Pushed, merged or tagged | No. The only tag is the pre-existing `v3.0`; `phase-5` has no upstream; GitHub itself was not contacted |

**Corrected in this reconciliation**
- The page has **12** panels, not 13: the earlier count included the function's own definition line (§5).
- The final implementation commit, the commit table and the file totals were updated (header, §2).
- The acceptance criterion "browser tests pass (Playwright)" is marked **met**, with the earlier "not met" history kept (§9, §11).
- The limitations and deviations (§15, §16) now list only what remains, including that the plan's second-dataset sensitivity check was **not done**; items that were resolved are named once and removed from the lists.
- The reproduce commands and next steps (§17, §18) show the final figures and what is left.
