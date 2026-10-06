# Phase 4D Report: API Production Hardening

1. **Date:** 2026-10-06
2. **Branch:** `phase-4`
3. **Starting commit:** `307ed88` (Phase 4C). Phase 4B `3fa11e3` is in its history.
4. **Final commit:** the commit that contains this report, `Phase 4D: harden analytics API caching and contracts` (its hash is in `git log`; a file can't contain its own commit's hash).
5. **Scope:** the three items Phase 4C left open (`PHASE_4_PLAN.md` §8.2, §8.5, step 5) — the response cache, ETag and conditional requests, and the OpenAPI snapshot test — with their tests and documentation.
   - **Not touched:** the API skeleton, repositories, services, schemas, business logic, `index.html`, the AI analyst, dbt.
   - **Not done:** pushing, merging, tagging.

Step 0 found the repository as expected: on `phase-4`, a clean tree, `307ed88` at HEAD on top of `3fa11e3`, and all 13 business routes present.

---

## 6. Cache architecture

`api/cache.py` adds `CacheMiddleware`, the **innermost** middleware:

```
RequestContext (request ID, JSON log, security headers, 500s)
  -> CORS -> GZip -> CacheMiddleware -> exception handlers -> routes
```

**Why innermost:**
- Cache hits still get a fresh request ID, a log line, security headers, CORS headers and compression.
- The cache stores the **uncompressed canonical body**.

**On a cacheable request** (`GET`/`HEAD` of `/api/meta` or a business endpoint), the middleware:
1. Checks the API key itself, using the same `api_key_valid()` as the route dependency, now shared in `api/security.py`. An unauthenticated request skips the cache and the route answers 401, so **a cached entry is never served to a caller without a valid key**.
2. Reads the current `data_version` (see §9).
3. On a hit, returns the stored body without touching the routes or the database: **0 SQL queries**.
4. On a miss, runs the route. A **200 JSON** response is stored; anything else (400/401/404/422/500/503/504) passes through **untouched and uncached**.
5. Adds `ETag`, `Cache-Control: private, max-age=N` and `X-Cache: HIT|MISS|BYPASS` to cacheable 200s, and answers `304` when `If-None-Match` matches (§10).

**Never cached:** `/api/health` and `/api/health/ready` (`Cache-Control: no-store`, no ETag), docs, `openapi.json`, and every error.

**`/api/meta` is cached.** It describes the loaded data (window, snapshot, last validated run, dimensions), all of which is fixed for a given `data_version`. It does not evaluate readiness, which is `/api/health/ready`'s job, and readiness is never cached.

**Deployment model:** one in-process cache per uvicorn worker (2 in Docker), with no Redis or other new dependency. The cache is used only from the event loop, so it needs no locks. The data-version lookup runs in the threadpool.

## 7. Cache configuration

All in `api/settings.py` and documented in `docs/api.md` and `.env.example`:

| Setting | Default | Effect |
|---|---|---|
| `API_CACHE_ENABLED` | `true` | `false`: nothing cached; ETag and 304 still work |
| `API_CACHE_TTL_S` | 300 | Entry lifetime |
| `API_CACHE_MAX_ENTRIES` | 512 | LRU eviction beyond this |
| `API_CACHE_MAX_BYTES` | 64 MiB | LRU eviction beyond this many body bytes; a single larger body is not cached |
| `API_DATA_VERSION_TTL_S` | 30 | How often `data_version` is re-read (0 = every request) |
| `API_CACHE_MAX_AGE_S` | 60 | `Cache-Control: private, max-age=…` |

Memory per worker is bounded by both limits: at most 512 entries and 64 MiB of bodies. The largest response today is about 65 KB (feature-adoption).

## 8. Cache key design

`cache_key(path, query_string, data_version)` = `(path, sorted(parse_qsl(query, keep_blank_values=True)), data_version)`:
- **Path:** distinct endpoints and distinct experiment IDs never collide.
- **All query parameters:** order-insensitive (`?a&b` = `?b&a`); repeated values and blank values are kept. Two different queries can't share an entry. Unknown or invalid parameters produce errors, which are never stored.
- **`data_version`:** see §9.
- **No headers.** No header changes a response body: the API key only gates access and is checked before the cache.
- **No secrets.** Settings are fixed per process. Keys hold only the path, the caller's query values and a run ID, so no credentials, DSNs or keys (tested).

## 9. Data-version invalidation

- **What it is:** `data_version` is the existing `meta.data_version`: the `run_id` of the last `ops.pipeline_runs` row with `step = 'validate_analytics'` and `status = 'success'`. A new repository function, `system.data_version()`, reads only that. **No new mechanism and no dbt changes.**
- **Lookup:** `DataVersion` re-reads it at most every `API_DATA_VERSION_TTL_S` seconds.
- **No version, no cache:** if it can't be read (warehouse not built, database down) or is null (never validated), the request skips the cache.
- **Storage:** responses are stored under the `data_version` reported **in their own body**, so a stored entry's key always matches its content even if the version changed mid-request.
- **Invalidation:** when a new run validates, the version changes, every key changes, and old entries become unreachable; they expire or are evicted. **Verified with a real pipeline run** (§12, `test_cache_follows_the_data_version`).
- **Limitation, documented and tested:** a rebuild **without** a validated run (a manual `dbt run`, or `pipeline step dbt` alone) doesn't change the version, so cached responses keep being served until `API_CACHE_TTL_S`. This matches the plan (§8.2). The full pipeline, or `pipeline step validate_analytics`, publishes new data.

## 10. ETag implementation

- **Format:** `ETag: W/"<first 32 hex of SHA-256>"` over the **canonical JSON** of the response body: parsed, `meta.generated_at` removed, then serialized with sorted keys and no whitespace.
  - Identical content gives identical ETags across requests, cache expiries and **workers** (tested with two app instances, and over HTTP with 2 uvicorn workers).
  - Any content change gives a different ETag; a new `data_version` changes the body and therefore the ETag.
  - No request ID, timestamp, secret or database detail is an input.
- **Weak validator:** GZip may re-encode the body and `generated_at` differs between computations; both encodings share one ETag (tested).
- **Matching:** `If-None-Match` uses RFC 9110 weak comparison and accepts a list or `*`. A match gives **`304 Not Modified` with an empty body**, plus `ETag`, `Cache-Control`, `X-Cache`, `X-Request-ID` and the security headers. A mismatch gives the normal 200.
- **Without the cache:** ETag and 304 also work with the cache disabled, and on a miss: a cold worker answers 304 for an ETag issued by another worker.
- **Contract:** `200` (with `ETag` and `Cache-Control` headers) and `304` are declared on every cacheable route in OpenAPI.

## 11. OpenAPI snapshot implementation

- **Snapshot:** `docs/openapi.json` (170 KB; 16 paths with all methods, parameters, responses and component schemas). It sits where the plan put it.
- **Generation:** `api/openapi.py` builds the schema from `create_app()` with fixed settings (no database, no environment influence) and serializes it deterministically (sorted keys, 2-space indent, LF).
  - `python -m api.openapi --check` compares it with the snapshot and prints a unified diff.
  - `--write` (`make openapi`) regenerates it.
- **Test** (`tests/api/test_api_openapi_snapshot.py`, 5 tests):
  - the full schema equals the snapshot (structural comparison, with a diff on failure)
  - generation is deterministic
  - the snapshot is stored canonically (CRLF-tolerant for Windows checkouts)
  - **the snapshot is not hollow**: 16 GET-only paths, the workspace parameters and the sort enum, the 200/304/4xx/5xx responses with the ETag header, and the key component schemas
  - **a change is detected**: an extra route and a removed parameter both fail the comparison
- **Unstable fields:** none found. The schema contains no timestamps or environment-dependent values.
- **Deliberate contract changes:** change the code, run `make openapi`, review the `docs/openapi.json` diff, and commit both together (documented in `docs/api.md`).

## 12. Tests added (24)

| File | Tests | Covers |
|---|---|---|
| `tests/api/test_api_cache.py` | 18 | Unit: key covers path, all parameters (order-free, repeats, blanks) and version; ETag deterministic, content-based, ignores `generated_at`, canonical; weak If-None-Match comparison; TTL expiry, LRU and byte budget; `DataVersion` re-read only after its TTL, and a failure means no cache; the cacheable set equals meta plus the business routes; errors and an unavailable database never cached; health and ready have no ETag. Integration (10K warehouse): first GET populates and the repeat hits (0 SQL on a hit); parameters and endpoints never share; error responses not cached; TTL expiry; LRU bound; entries keyed by `data_version` (another version's entry never served); ETag and 304 on **every** cacheable endpoint; per-query ETags; ETags agree across workers; HEAD, GZip, cache disabled; cached entries still require the API key; no secrets or internals in keys or ETags |
| `tests/api/test_api_openapi_snapshot.py` | 5 | §11 |
| `tests/api/test_api_sensitivity.py` | +1 | `test_cache_follows_the_data_version` (pipeline-built DB): cached response; data changed and `fct_nps_daily` rebuilt without validation, still a HIT (documented); real `pipeline step validate_analytics` gives a new `data_version`, a MISS, +7 responses, a new ETag, and the old ETag no longer gets a 304 |

Your 17 required cases map to these tests:

| Required case | Test |
|---|---|
| 1, 2 | `test_first_get_populates_and_the_repeat_is_served_from_cache` |
| 3, 4 | `test_parameters_and_endpoints_never_share_entries` |
| 5 | `test_error_responses_are_not_cached` |
| 6 (503 not cached) | `test_errors_and_unavailable_database_are_never_cached`; data-not-ready skips the cache because no version can be read |
| 7 | `test_entries_expire_after_the_ttl` and the unit TTL test |
| 8 | `test_the_cache_is_bounded_lru` and the unit LRU and byte tests |
| 9 | `test_entries_are_keyed_by_data_version` and `test_cache_follows_the_data_version` |
| 10–12, 15, 16 | `test_every_cacheable_endpoint_gets_an_etag_and_honours_if_none_match`, `test_etags_are_per_query_and_agree_across_workers`, the unit ETag test |
| 13, 14 | Same, plus over real HTTP |
| 17 | `test_no_secrets_or_internals_in_cache_keys_or_etags` |

**No existing test was deleted or changed.** `test_api_sensitivity.py` only gains a function. All 118 Phase 4B/4C API tests passed with caching on by default **before** any new test was written.

## 13. Full pytest result

**264 passed, 2 skipped** in 509 s (was 240 + 2 at Phase 4C). This includes the end-to-end pipeline test, the Phase 4A serving tests and all API tests.

## 14. Ruff result

`ruff check .`: **All checks passed.**

**API suite on its own:** **142 passed** (cache 18, endpoint states 5, endpoints 32, http 29, OpenAPI snapshot 5, params and metrics 16, sensitivity 3, settings 22, warehouse 12).

**OpenAPI:** `python -m api.openapi --check` reports a match (16 paths).

## 15. HTTP smoke-test result

All against the rebuilt container on `127.0.0.1:8002`, with 2 uvicorn workers:

| Suite | Result |
|---|---|
| Phase 4C endpoint smoke (19 endpoint variants, 7 error cases, CORS) | **27/27** |
| Phase 4D cache / ETag / 304 (auth off) | **13/13**: HITs served; both workers give the same ETag; matching `If-None-Match` gives 304 with an empty body and all headers; a non-match gives 200; a different query gives a different ETag; HEAD; GZip and identity share the ETag; health and ready have no ETag and `no-store`; 3 error cases neither cached nor tagged; CORS exposes `ETag` |
| Phase 4D with `API_AUTH_MODE=api_key` (a throwaway container on port 8003, random test key) | **16/16**: no key and a wrong key give 401; health is open; keyed requests are cached and validated. Neither the key nor the database password is in its logs; the container was removed afterwards. |
| Phase 4B smoke script (unchanged from 4B) | 10/12. The two "failures" are 4B-era expectations that are now obsolete: `/api/overview` returning "404, not built yet" (it now returns 200 with real KPIs) and the OpenAPI document having exactly 3 paths (now 16). This scratch script isn't a repo test; the 27-check 4C suite supersedes it. |

## 16. Docker validation

- **`docker compose config`:** valid.
- **Image:** rebuilt (`connecthub-api:local`). `api-init` exited 0 and the `api` container is healthy.
- **No compose or Dockerfile changes were needed:** `api/cache.py` and `api/openapi.py` are inside the copied `api/` package, and the cache needs no new dependency.

## 17. Warehouse fingerprint

| | Result |
|---|---|
| **Before** (taken before the test suite and HTTP validation) | 35 relations, 2,112,759 rows |
| **After** (taken after the full suite, the API suite and all HTTP validation) | **identical**: `{"relations": 35, "identical": true, "differing": []}` |

The API stayed strictly read-only. One note: the Phase 4B grants test rebuilds two gold tables in place with dbt (same content), which the identical fingerprint confirms.

## 18. Security validation

| Check | Result |
|---|---|
| SQL injection | The Phase 4C tests (6 payloads × 10 parameters + the path) still pass |
| Parameter binding | User values are only ever bound parameters. The repositories' f-strings interpolate only module constants (filter and select fragments, plus the allow-listed `ORDER BY` built from dict lookups). |
| Write SQL | No INSERT/UPDATE/DELETE/TRUNCATE/DDL in repositories, services, routers or the cache (scanned) |
| Workspace sort | Still an allow-list (enum → fixed SQL); pinned by the OpenAPI snapshot |
| Secrets in cache keys and ETags | None (tested). ETags are opaque hashes of response content. |
| Secrets in errors and logs | Phase 4B tests still pass. No `.env` secret value in the 20 changed or new files (this report included) or in the container logs, nor the auth container's key or password. |
| Cache vs auth | Cached entries are only served to requests with a valid key (pytest and HTTP) |
| Surface | GET-only (OpenAPI snapshot plus the Phase 4C test) |

## 19. Performance impact

From the container's request log (server-side, 2 workers):

| Cache status | Requests | p50 | p95 | SQL queries (p50) |
|---|---|---|---|---|
| **hit** | 510 | **0.44 ms** | 1.18 ms | **0** |
| miss | 41 | 8.3 ms | 471 ms | 4 |

- **Per endpoint, miss → hit (p50):** overview 494 ms → 0.45 ms; retention 74 ms → 0.40 ms; activation 23 ms → 0.41 ms; support 13 ms → 0.42 ms.
- **About the misses:** they include the **first requests after the container started** (connection setup, cold imports), so the 471 ms p95 and the overview miss reflect cold start rather than steady state. Phase 4C measured overview at about 22 ms warm. This was not re-measured separately here.
- **The hit path's cost:** a dict lookup plus, at most every 30 s, one version query. The miss path adds one JSON parse and hash per 200 response (about 65 KB at most).
- **Client-side latency** over Docker Desktop is still about 50 ms. That is the Windows port-forwarding artifact documented in Phase 4C, not API cost.
- **Scale:** measured at 10K users only.

## 20. Skipped tests

| Test | Why |
|---|---|
| `tests/test_dag.py` | Airflow isn't in the dev venv; it runs in the Airflow container |
| `tests/test_spark_jobs.py` | pyspark isn't in the dev venv; it runs in the Spark container |

Nothing else was skipped.

## 21. Deviations

1. **ETag input:** the plan (§8.2) proposed `ETag = sha256(data_version + route + params)`. You asked for an ETag derived from the actual response representation, so it is a hash of the canonical body with only `meta.generated_at` excluded. That makes it agree across workers and cache expiries; it is a weak validator for that reason.
2. **`X-Cache` header:** added (`HIT`/`MISS`/`BYPASS`) for observability and testing; the plan only mentioned a `cache` log field, which is also added.
3. **`/api/meta` is cached** like the business endpoints, as the plan specified (§5.2), with the rationale in §6.
4. **`API_CACHE_MAX_BYTES`:** an extra bound beyond the plan's entry count, so memory can't grow with large responses.

## 22. Known limitations

1. **Rebuilds without validation:** a rebuild that doesn't end in a validated run keeps serving cached responses for up to `API_CACHE_TTL_S` (300 s), as documented and tested.
2. **`data_version` lookup lag:** a new version is noticed up to `API_DATA_VERSION_TTL_S` (30 s) after validation.
3. **Per-worker caches:** each worker warms its own cache; there is no shared cache. ETags still agree across workers.
4. **`meta.generated_at`** on a cached response is when it was computed, not when it was served.
5. **Possible stale responses while the database is down:** a cached entry can still be served for up to 30 s if the database goes down right after a version lookup. Readiness reports the outage regardless. This window isn't explicitly tested.
6. **Benchmarks:** 10K users only; the miss timings include cold start.
7. **Carried over:**
   - Starlette 1.x TestClient deprecation (filtered)
   - about 2.5 GB free disk on C:
   - stale Airflow image
   - ports 8000/8001 occupied locally (8002 used)

## 23. Files changed

**Added (5):**
- `api/cache.py`
- `api/openapi.py`
- `docs/openapi.json`
- `tests/api/test_api_cache.py`
- `tests/api/test_api_openapi_snapshot.py`

Plus this report.

**Modified (14):**

| File | Change |
|---|---|
| `api/main.py` | Cache objects and middleware; description |
| `api/middleware.py` | Request state; `cache` log field |
| `api/context.py` | `request_state_var` |
| `api/security.py` | `api_key_valid()` shared by the route and the cache |
| `api/settings.py` | 6 cache settings |
| `api/repositories/system.py` | `data_version()` |
| `api/routers/analytics.py`, `api/routers/meta.py` | 200/304 validators documented |
| `api/schemas/common.py` | `cacheable_responses` |
| `tests/api/test_api_sensitivity.py` | +1 test |
| `docs/api.md` | Caching, ETag and snapshot sections |
| `docs/architecture.md` | One paragraph |
| `Makefile` | `openapi` target |
| `.env.example` | Cache settings (comments) |

## 24. Commit hash

See the final response or `git log -1` (`Phase 4D: harden analytics API caching and contracts`).
