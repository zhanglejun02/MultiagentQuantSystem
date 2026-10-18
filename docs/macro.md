# Independent Macro Domain

## Scope

`quant_system.macro` collects official FRED and SAFE observations, freezes responses,
and computes deterministic descriptive indicators. It imports no gold, factor,
agent or research-runtime module. It calls no LLM and has no trading capability.

Public imports:

```python
from quant_system.macro import (
    MacroAnalyzeRequest,
    MacroCollectRequest,
    MacroCollection,
    MacroService,
)

service = MacroService(store, registry, clock=utc_now, transport=transport)
collection: MacroCollection = service.collect(MacroCollectRequest())
analysis: dict = service.analyze(collection.snapshot_id, knowledge_cutoff=None)
```

`MacroCollectRequest` defaults to `DGS10`, `DFII10`, `DTWEXBGS`, `DEXCHUS`,
`CN_GOLD_RESERVES`, starting 2023-01-01, with `end_date=None` resolved to the
actual UTC collection date. It rejects extra fields, unknown/duplicate series,
inverted dates and arbitrary URLs. `MacroAnalyzeRequest(snapshot_id: str)` is
the separate analysis request. Future collection windows are rejected.

`MacroCollection` contains `snapshot_id`, `raw_snapshot_id`, `as_of`, actual
`available_at`, per-series `coverage`, `missing`, `summary`, `quality_errors`.
Coverage reports actual counts and first/last dates, not certified completeness.
Collecting quantity also includes the separately typed USD valuation series.

## Sources And Units

| ID | Meaning | Unit | Observation / Release | Stale After |
| --- | --- | --- | --- | --- |
| DGS10 | US 10-year nominal constant maturity yield | percent | daily / business daily | 7 days |
| DFII10 | US 10-year inflation-indexed real yield, may be negative | percent | daily / business daily | 7 days |
| DTWEXBGS | Broad trade-weighted nominal USD index, **not ICE DXY** | January 2006 = 100 | daily / weekly | 14 days |
| DEXCHUS | CNY per USD, New York noon buying rate, **not PBOC parity** | CNY per 1 USD | daily / weekly | 14 days |
| CN_GOLD_RESERVES | China's official gold reserve stock | 10,000 troy ounces | monthly / monthly | 75 days |
| CN_GOLD_RESERVES_USD | Official gold reserve valuation, not purchases | USD 100 million | monthly / monthly | 75 days |

FRED source pages are `https://fred.stlouisfed.org/series/<ID>`; data uses the
official `https://api.stlouisfed.org/fred/series/observations` JSON endpoint.
`FRED_API_KEY` is read from the process environment, never from a request model
or CLI argument. No key or URL containing a key is persisted or logged.
Errors become bounded error codes; response bodies echoing credentials are
rejected rather than archived. A missing key affects only FRED series.

SAFE discovery starts at <https://www.safe.gov.cn/safe/whcb/index.html>, follows
the official reserve-assets year page and its HTML link. Publication years come
from link titles, **not** URL path years. Internal `safe_published_pages` in
[configs/data/macro.yaml](../configs/data/macro.yaml) may map years to approved
official HTML pages if discovery fails. Only HTTPS SAFE `/safe/*.html` URLs with
no credentials, query or fragment are allowed. Request models cannot supply URLs.
There is no hardcoded current-year article or silent historical-year substitution.
Missing years are recorded explicitly, including years outside the bounded sync.

The standard-library HTML parser expands rowspans and colspans, reads month
headers, selects USD rather than SDR valuations, and requires explicit quantity
units. Empty/future months and FRED `.` observations are omitted, never zero
filled. Unknown units, conflicting values and changed structures fail that source.
A full actual SAFE HTML response, source URL and SHA256 are frozen in
[tests/unit/test_macro_safe_official.py](../tests/unit/test_macro_safe_official.py)
for entirely offline regression tests.

## Time And Evidence

An observation records `observation_date`, `period`, `value`, `unit`, `source`,
`source_url`, optional `published_at`, timezone-aware `ingested_at` and
`available_at`, `vintage` metadata and the raw response SHA256. Monthly dates
are period-end labels, not release dates. Page update dates and FRED realtime
dates are **not** original per-observation publication evidence.

Current downloads of old values are **not historical PIT data**. These providers
set `published_at=None` and conservatively use the actual response fetch time
for `available_at`. The module does not request ALFRED historical vintages or
retroactively infer release times. A historical `knowledge_cutoff` filters facts
using `available_at <= cutoff`; a cutoff before ingestion yields no indicators,
observations or evidence entries, even if the observations are dated years ago.

Analysis returns `indicators`, `coverage`, `missing`, `limitations`,
`evidence_entries`, `observations`, `observations_metadata`, `snapshot_id` and
`raw_snapshot_id`. Evidence entries expose `artifact_id`, snapshot content hash,
schema version, stable record key, raw response hash, observation period,
source and actual `available_at` for external `EvidenceRef` binding. No runtime
class is imported here. Derived indicators cite their input fact IDs.

Yield changes are percentage points and basis points. Other series report
absolute and relative changes; zero denominators yield `None`. Twenty-observation
changes are not called calendar-month returns. The nominal-minus-real spread
uses the latest actual common date, without forward filling, and is not presented
as a pure inflation forecast. Reserve stock or valuation changes are not global
central bank demand. No causal, predictive or allocation conclusions are generated.

## Storage And Failure

Successful collection appends a raw `DATA_SNAPSHOT` followed by its child
`MACRO_SNAPSHOT`; the latter's `data_snapshot_id` is the raw artifact ID.
Bodies are frozen as base64 with response hashes in a content-addressed raw JSON
artifact; normalized observations live in a separate content-addressed JSON.
Repeated/revised downloads append new registered envelopes, never mutate old
artifacts. Identical raw content may share the same immutable physical object.

Every analysis verifies normalized and raw artifact hashes, parent binding,
raw response hashes, record provenance, uniqueness and observation windows.
It performs no network requests and creates no analysis-time artifacts.
Source failures are explicit `missing` entries; usable remaining data produces
`PARTIAL` coverage. Zero usable observations registers a `FAILED` raw artifact
and raises `MacroQualityError` with `quality_errors` and `raw_snapshot_id`;
it never publishes a successful empty macro snapshot or falls back to other providers.
File writes and registry operations are not one transaction; a registration
failure may leave an unregistered content-addressed object, not a published snapshot.

## Running

Use the configured project `.venv`. Export `FRED_API_KEY` directly in the shell
when available; the script does not load or change `.env` files.

```sh
.venv/bin/python scripts/sync_macro.py --canary --analyze
.venv/bin/python scripts/sync_macro.py --series CN_GOLD_RESERVES --start-date 2026-01-01
.venv/bin/python -m pytest tests/unit/test_macro*.py
.venv/bin/python -m mypy src/quant_system/macro scripts/sync_macro.py
.venv/bin/python -m ruff check src/quant_system/macro scripts/sync_macro.py tests/unit/test_macro*.py
```

`--canary` restricts the window to the most recent 45 days within the current
SAFE year, at most one FRED page per series, one SAFE year and eight network
requests. It does not fetch all history. `--analyze` only reads the frozen result.
Defaults use local SQLite under `var/quant.db` and `var/artifacts`; explicit
`--db-url`, `--artifacts-root` and the existing environment settings may override.
Production schema management remains external. Exit code 2 means zero-data
quality failure with a frozen raw failure record.

Network transport is a small `MacroTransport.get(url, *, params, timeout) -> bytes`
protocol. Inject a fake transport and clock for tests. The default uses `urllib`
and existing `certifi`, preserves TLS verification, denies redirects, caps response
size, requests, pages, years and per-request timeout, and performs no retries.
Bounds are internal YAML configuration, not user-supplied URLs. There is no SLA
or full calendar completeness certification, automatic ALFRED reconstruction,
additional macro series, news, ETF flows or CFTC positioning in this slice.

## Integration Prerequisite

The owning integration agent must supply `ArtifactType.MACRO_SNAPSHOT =
"MACRO_SNAPSHOT"` in the shared enum. This module deliberately does not edit it
or substitute another artifact type. No dependency additions are needed:
Pydantic, SQLAlchemy, PyYAML and certifi already exist; HTML parsing uses the
standard library. API, runtime, agent configuration and other domains are untouched.