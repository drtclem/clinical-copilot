# PERFORMANCE_BASELINE.md

Load/stress test results per ARCHITECTURE.md §7.9, establishing the baseline for §7.8. Run
2026-09-16, both locally and against the droplet. **These numbers are the baseline going
forward** — future performance work (caching, model tiering, infrastructure changes) should be
measured as an improvement against this document, not against assumptions.

## Methodology

**Tool:** Locust 2.46 (`load_test/locustfile.py`), matching ARCHITECTURE.md §7.9's original plan.

**Scenario — a realistic conversation, not a single repeated request.** Each simulated resident
runs one bounded session against `/chat`, carrying its own `conversation_id` across turns like a
real multi-turn conversation:

1. A normal orientation request (patient randomly chosen between pid1/Alice and pid2/Bob).
2. One natural follow-up in the *same* conversation — "Given her allergy history, is amoxicillin
   safe to start empirically?" — chosen specifically to exercise the multi-turn grounding fix
   (ERROR_ANALYSIS.md Entry 5, which threads `tool_records` across turns) under real concurrent
   load, not just the single-user testing it was originally verified with.
3. For roughly 1 in 10 simulated users: a third message with a malformed patient ID, so the load
   includes real error-path traffic, not only the happy path.

Every user sends exactly 2 or 3 messages total, then stops (`StopUser`) — this is a load test
proving the system holds up under concurrency, not an open-ended soak test.

**Cost estimate (checked before running, per the task's own checkpoint):** ~10×2 + 50×2 +
10×2 ≈ 140 baseline calls, +~10% for the malformed-ID branch ≈ 150-160 real `/chat` calls total
across all stages, at COST_ANALYSIS.md's ~$0.03/interaction estimate ≈ **$4.50-$4.80**, well
within "a few dollars." Actual real calls sent, counted directly from Locust's own request
counters (not estimated after the fact): 21 (local, 10 users) + 109 (local, 50 users) + 20
(droplet, 10 users) = **150 real `/chat` calls total**, matching the estimate closely.

## Results

### Stage 1 — 10 concurrent users, local

| Metric | Value |
|---|---|
| Requests | 21 (10 orientation, 10 follow-up, 1 malformed-ID) |
| Error rate | 0 / 21 (0%) |
| Throughput | 0.75 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 10.0s / 16.0s / 17.0s |
| p50 / p95 / max — orientation | 15.0s / 17.0s / 16.71s* |
| p50 / p95 / max — follow-up | 8.2s / 10.0s / 10.18s |
| Peak container CPU | `development-easy-openemr-1` 116.0%, `langfuse-worker` 63.2%, `mysql` 17.7%, `clickhouse` 16.5% |
| Peak container memory | `clickhouse` 2.24 GiB, `openemr` 769 MiB, `langfuse-web` 748 MiB, `langfuse-worker` 501 MiB |

\* Locust's percentile bucketing occasionally rounds a bucket above the true max; the true
measured max is used here in place of the rounded bucket value.

*(Our own service's host-level CPU/memory was not separately sampled in this stage — see the
honest note below. Only Stage 2 and Stage 3 sample the `uvicorn` process directly.)*

### Stage 2 — 50 concurrent users, local

| Metric | Value |
|---|---|
| Requests | 109 (50 orientation, 50 follow-up, 9 malformed-ID) |
| Error rate | 0 / 109 (0%) |
| Throughput | 1.39 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 21.0s / 61.0s / 65.0s |
| p50 / p95 / max — orientation | 53.0s / 65.0s / 64.58s |
| p50 / p95 / max — follow-up | 16.0s / 22.0s / 24.02s |
| Peak container CPU | `openemr-1` 156.3%, `clickhouse` 45.0%, `mysql` 16.7%, `minio` 15.9%, `langfuse-worker` 12.3% |
| Peak container memory | `openemr` 2.29 GiB, `clickhouse` 2.16 GiB, `langfuse-web` 804 MiB, `langfuse-worker` 501 MiB |
| Our own service (`uvicorn`, host process) | Peak 9.1% CPU, peak 103 MB RSS |

**Reading this honestly:** latency roughly doubled from 10 to 50 concurrent users (p95: 16s →
61s), and `development-easy-openemr-1` — OpenEMR's own FHIR backend, not our agent service — is
consistently the single most CPU-loaded container at both concurrency levels (116% → 156% on a
multi-core host). Our own service's CPU/memory footprint is trivial by comparison (peak 9.1% CPU,
~103 MB) because it is I/O-bound, waiting on Anthropic and FHIR round-trips, not doing real
compute itself. This matches ARCHITECTURE.md §6's already-documented tradeoff: FHIR access is the
latency-relevant bottleneck under load, not our own agent code, and caching or bulk-export (both
already named there) is the correct lever, not scaling the agent service itself.

### Stage 3 — droplet confirmatory pass

**Pre-flight check:** droplet load average was 3.46/2.62/2.50 (healthy — nowhere near the ~175
load average seen during the earlier resource-contention incident in COVERAGE.md), and Selenium /
CouchDB / Mailpit were **not** running, so nothing needed stopping this time (unlike before).

**10 concurrent users — run:**

| Metric | Value |
|---|---|
| Requests | 20 (10 orientation, 10 follow-up, 0 malformed-ID — 1-in-10 odds over only 10 users legitimately landed on zero) |
| Error rate | 0 / 20 (0%) |
| Throughput | 0.12 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 81.0s / 154.0s / 154.0s |
| p50 / max — orientation | 82.0s / 154.19s |
| p50 / max — follow-up | 73.0s / 72.61s |
| Peak container CPU | `clickhouse` **165.8%**, `langfuse-web` 93.9%, `openemr` 68.0%, `langfuse-worker` 31.4% |
| Peak container memory | `langfuse-web` 981 MiB, `openemr` 824 MiB, `clickhouse` 801 MiB, `langfuse-worker` 626 MiB |
| Our own service (`uvicorn`, host process) | Peak 0.8% CPU, ~79 MB RSS |

**50 concurrent users — deliberately skipped at the time, stated honestly.** The 10-user droplet
result above already showed severe degradation relative to both the local result and the droplet's
own single-user baseline (ARCHITECTURE.md §7.7's measured p50≈12.8s/p95≈19.7s/max≈25.8s from 218
real traces) — median latency alone (81s) was already 4-6x the droplet's normal single-user
experience, and `clickhouse` alone was consuming more CPU than one full core on this 2-vCPU box.
Scaling to 50 concurrent users on top of that would almost certainly have produced multi-minute
waits or outright timeouts without teaching us anything the 10-user result hadn't already shown,
while risking real disruption to the shared droplet's other workloads (it also runs the actual
OpenEMR dev stack) for no added diagnostic value. That was a judgment call, stated plainly rather
than silently substituting a smaller number as if it were the full test — and it was revisited once
the underlying constraint changed (see below), not left as a permanent gap.

### Stage 3, continued — 50 concurrent users, after the droplet resize (2026-09-16, later same evening)

Once the droplet was resized 2 vCPU/4GB → 4 vCPU/8GB (ERROR_ANALYSIS.md Entry 6) and the 10-user
stage was re-verified there, the originally-skipped 50-user droplet stage was run for real:

| Metric | Value |
|---|---|
| Requests | 105 (50 orientation, 50 follow-up, 5 malformed-ID) |
| Error rate | 0 / 105 (0%) |
| Throughput | 0.70 req/s sustained |
| p50 / p95 / p99 latency (aggregate) | 59.0s / 83.0s / 107.0s |
| p50 / max — orientation | 75.0s / 107.68s |
| p50 / max — follow-up | 54.0s / 78.91s |
| Peak container CPU | `openemr-1` **338.6%** (near-saturating all 4 cores by itself), `clickhouse` 120.4%, `langfuse-web` 92.3%, `langfuse-worker` 67.6% |
| Peak container memory | `openemr` 2.68 GiB, `clickhouse` 1.10 GiB, `langfuse-web` 1009 MiB, `langfuse-worker` 788 MiB |
| Our own service (`uvicorn`, host process) | Peak 1.8% CPU, ~104 MB RSS |

**Cross-checked against the new `request_timing.log` and Langfuse's own `AGENT`-span data** (same
method as the Entry 6 verification): all three sources agree closely for these same 105 real
requests --

| Source | p50 | p95 | max | count vs. real requests |
|---|---|---|---|---|
| Locust (ground truth) | 59.0s | 83.0s | 107.68s | 105/105 |
| Langfuse `AGENT`-span | 51.2s | 80.5s | 82.2s | **105/105** |
| `request_timing.log` | 58.6s | 83.1s | 107.66s | 105/105, mean 58.96s vs. Locust's own mean 58.98s |

The observability fix holds at 50 concurrent users too, not just the 10-user case it was verified
against -- both independent sources continue to track the real client-observed latency closely.

**Reading the result honestly:** the resize resolved the *observability* gap completely, but 50
concurrent users is still genuinely heavy load for this box -- `development-easy-openemr-1` alone
now peaks at 338.6% CPU, close to saturating all four cores by itself, and p95 latency (83s) is
roughly 4x the droplet's single-user baseline (19.7s). This is worse than local's 50-user result
(p95 61.0s, Stage 2 above) on the same relative concurrency, consistent with the droplet remaining
a smaller, shared box even after the resize. All three configured alerts correctly fired on this
run (`p95 latency`, `error rate`, `tool failure rate` all `ALERT`) -- the latter two legitimately,
since the 5 deliberate malformed-ID requests in this run are real tool failures by design, not a
bug, and exceeded the `>3` threshold as expected. Nothing here suggests scaling to 50 concurrent
droplet users is currently viable without the same infrastructure changes (caching, bulk-export,
eventually horizontal scaling) already named in ARCHITECTURE.md §6 -- consistent with, not
contradicting, the local Stage 2 finding.

## A significant finding this load test surfaced, not smoothed over

On the droplet specifically (not reproduced locally — see below), the load test surfaced a real
gap in this project's own observability, not just a capacity limit:

- **Locust measured** (client-side, real wall-clock round trip): p95 = 154.0s, max = 154.19s across
  the 20 completed requests.
- **Langfuse's own recorded `AGENT`-span duration** for the same time window, queried directly from
  the droplet's ClickHouse: only **5** `AGENT` spans were recorded for those 20 real completed
  requests (also only 4 `TOOL` spans, where closer to 15-20 would be expected), with p95 = 17.2s
  and max = 17.7s — nowhere close to what the client actually experienced.
- **Locally, this discrepancy does not occur.** The same ClickHouse query against local's own
  instance for its 130 real requests (stage 1 + stage 2 combined) shows exactly 130 `AGENT` spans
  and 130 `TOOL` spans — a clean 1:1 match — with span-level latency tracking the client-observed
  latency reasonably closely at both concurrency levels.

**Most likely explanation:** on the resource-constrained droplet, severe CPU contention
(`clickhouse` alone peaked at 165.8% of a 2-vCPU box) delays the request well before our own
instrumented code path even begins — time the client genuinely waits, but that our own `AGENT`
span never measures, since the span timer only starts once `run_turn()` begins executing. Under
that same contention, a real fraction of this project's own span-create calls to the self-hosted
Langfuse ingestion endpoint likely fail or are dropped outright — consistent with
`app/observability.py`'s deliberate design of treating the observability pipeline as best-effort
(a Langfuse outage should never break the actual resident-facing response), which is the right
tradeoff for availability, but means telemetry itself becomes unreliable exactly when the system
is under the load conditions where accurate telemetry matters most.

**Why this matters for the alerts just configured:** the droplet's p95-latency Monitor
(ARCHITECTURE.md §7.7, threshold 30,000 ms) read `severity: OK` throughout this entire load test —
it never fired, because it evaluates Langfuse's own `AGENT`-span data, which this test proves can
itself go quiet under exactly the kind of real contention the alert exists to catch. A 154-second
real resident wait produced no alert. This is a genuine blind spot, not a rounding error, and it is
the single most important finding of this exercise — more important than any individual latency
number above. It is noted here as a concrete next step (a client-observable or reverse-proxy-level
latency signal, independent of the agent's own internal instrumentation, would close this gap),
not fixed in this session, which was scoped to running and honestly reporting the load test.

### Addendum (2026-09-16, same evening): root cause confirmed and resolved -- see ERROR_ANALYSIS.md Entry 6

Two things happened after the finding above, both real, both verified, not just asserted:

1. **A scoped, Langfuse-independent fix** — a small request-timing middleware in `app/main.py`
   (`request_timing.log`, plain file I/O, zero dependency on Langfuse) was added specifically so
   real request latency is captured even if Langfuse's own telemetry degrades again.
2. **The droplet was resized 2 vCPU/4GB → 4 vCPU/8GB**, testing the theory directly: that the gap
   was CPU contention starving Langfuse's own ingestion pipeline, not a Langfuse limitation.

Re-running the *exact same* 10-concurrent-user droplet stage after both changes, and comparing all
three latency sources for the same 22 real completed requests:

| Source | p50 | p95 | max | span/record count |
|---|---|---|---|---|
| Locust (client-side ground truth) | 14.0s | 37.0s | 36.64s | 22/22 requests |
| Langfuse `AGENT`-span (ClickHouse) | 11.68s | 34.50s | 34.90s | **22/22** (was 5/20) |
| New `request_timing.log` | 12.72s | 36.62s | 36.62s | 22/22 (mean 19.03s vs. Locust's own mean 19.04s) |

**Conclusion, stated honestly:** the resize alone resolved the observability gap at its root. All
three independent sources now agree within a few seconds of each other, and Langfuse's own span
count exactly matches the real request count (22/22) instead of badly undercounting (5/20). This
confirms the original theory — CPU contention on the old 2-vCPU box was starving Langfuse's own
ingestion pipeline, not a fundamental Langfuse limitation — and no separate monitoring pipeline is
needed to close this specific gap. Container CPU also confirms the contention itself eased: peak
CPU on the 4-vCPU box (`openemr` 212.9%, `langfuse-web` 139.8%, `clickhouse` 86.1%) is well under
the new ~400% ceiling, where `clickhouse` alone previously consumed 165.8% of the old ~200% ceiling.

**What did *not* fully resolve, stated honestly rather than declaring total victory:** p95 latency
at 10 concurrent droplet users (34.5-37s across all three sources) is still noticeably above the
droplet's own single-user historical baseline (p50≈12.8s/p95≈19.7s, ARCHITECTURE.md §7.7) — roughly
a 2x concurrency cost remains, just nowhere near the previous 5-10x blowup. And because the
telemetry is trustworthy again, the droplet's p95-latency Monitor correctly fired
(`severity: ALERT` at `23:37:10Z`) on this very run — the alerting system working exactly as
designed once its underlying data can be trusted. The `request_timing.log` middleware stays in
place regardless, as a permanent, low-cost, Langfuse-agnostic safety net.

## Connecting back to KEY_METRICS.md and the configured alerts

KEY_METRICS.md names p95 latency as a supporting metric specifically to guard against
"verification rigor quietly making the agent too slow for its most time-critical use case." This
load test is the first real evidence bearing on that question under concurrency, and the honest
answer is: **yes, these numbers exceed the alert thresholds already configured**, substantially, at
both stages:

- Local: Stage 2's p95 (61.0s) is already ~3x the local alert threshold (20,000 ms / 20s).
- Droplet: Stage 3's p95 (154.0s) is over **5x** the droplet's alert threshold (30,000 ms / 30s) —
  and yet, per the finding above, the alert did not fire, because the underlying telemetry itself
  degraded under the same contention.

Both alert thresholds were deliberately set from *single-user, uncontended* baseline traffic
(ARCHITECTURE.md §7.7) specifically to catch *regressions from that normal case* — they were never
claimed to represent a validated concurrency ceiling, and this test shows they aren't one. The
system remains correct under concurrency (0 errors across all 150 real requests, every one of them
eventually returned a real, complete response) but is **not currently fast enough at 50 concurrent
local users or even 10 concurrent droplet users** to meet the sub-30-second bound the architecture
document aspires to for its most time-critical use case. That gap is FHIR/infrastructure-bound
(Stage 1 vs 2's dominant CPU consumer was consistently OpenEMR itself, not our agent), matching
ARCHITECTURE.md §6's and COST_ANALYSIS.md's existing guidance that caching, bulk-export, and
eventually horizontal scaling — not agent-code changes — are the correct next levers, not something
newly discovered here, but now backed by a real number instead of a guess.

## Two follow-on fixes for the first-fetch case caching alone doesn't cover (2026-09-19)

Per-conversation FHIR caching (implemented 2026-09-19, commit `343efc3`) eliminates *redundant*
fetches within a conversation, but does nothing for the very first time a resident asks about a
patient — all of `get_patient_snapshot`'s FHIR calls still had to happen before that first response.
Two things followed from that gap.

**Where the latency actually concentrates, from real per-tool span data (Langfuse ClickHouse):**

| Tool | FHIR calls | n | avg | p95 | max |
|---|---|---|---|---|---|
| `get_patient_snapshot` | 5 | 932 | 2.66s | 20.87s | 33.41s |
| `check_allergy_conflict` | 1 | 160 | 1.22s | 4.63s | 5.7s |
| `get_recent_encounters` | 2 | 5 | 1.27s | 1.52s | — |
| `get_recent_observations` | 1 | 0 | — | — | — |
| `compare_signout_to_chart` | 8 (delegates to the three above) | 0 | — | — | — |

`get_recent_observations` and `compare_signout_to_chart` have zero real trace data — neither this
load test nor any traced run has exercised them — so nothing below claims to speed those up with
evidence behind it.

**Parallelizing `get_patient_snapshot`'s FHIR calls (implemented 2026-09-19).** Reading the actual
code path (not assuming from the tool's shape) found 4 of the 5 calls genuinely independent (Patient
read, Condition, MedicationRequest, AllergyIntolerance searches — each needs only the input
`patient_id`), and one (the duplicate-detection Patient search) genuinely dependent on the Patient
read's own result. Implemented via `concurrent.futures.ThreadPoolExecutor`, not asyncio — the whole
app is synchronous (`/chat` is `def`, not `async def`; `FhirClient` uses sync `httpx.Client`), and
`OAuthTokenProvider` already had a `threading.Lock()` around its state, so no new async plumbing was
needed anywhere else.

Measured empirically against the real, running **local** OpenEMR instance (this project's actual
`FhirClient`/`OAuthTokenProvider`, not a mock), 5 trials each:

| | mean | median | min | max |
|---|---|---|---|---|
| Sequential, uncontended | 1.049s | 1.017s | 0.995s | 1.188s |
| Concurrent, uncontended | 0.723s | 0.708s | 0.695s | 0.786s |
| Sequential, 8 background noise threads (simulated concurrent-resident load) | 1.610s | 1.449s | 1.419s | 2.286s |
| Concurrent, 8 background noise threads | 1.033s | 1.032s | 1.018s | 1.049s |

**1.45x uncontended, 1.56x under simulated contention** — the relative benefit held up (if anything
slightly improved) when the backend was busy, not just when idle. 50 total concurrent request-
issuances across both runs through one shared `httpx.Client`, zero errors, zero corrupted results —
real evidence the client tolerates concurrent use under this codebase's real objects, not assumed
from documentation.

**Open validation gap, stated plainly, not silently assumed away:** this was measured on local
Docker Desktop only. The droplet — the actual resource-constrained environment (2→4 vCPU after the
Stage 3 resize above) this whole latency investigation started from — was not re-tested. An SSH
session to the droplet was available and connected successfully, but surfaced an unrelated, more
urgent finding first: an unauthorized process (`bitpingd`, plus a companion `cli start accept
--token=...` process) running inside `development-easy-selenium-1`, not part of the official
Selenium image, with its binary deleted from disk post-execution and numerous outbound connections
to external IPs — see the project's own incident-documentation convention (`THREAT_MODEL.md`,
recent commits documenting droplet auth incidents) for how this gets tracked and resolved. Running
additional synthetic load against that host right now would both be irresponsible given the
finding and scientifically confounded (measuring against `bitpingd`'s resource use, not clean
concurrent-resident contention). **The droplet re-run remains an explicitly open validation step,
not confirmed, until that's resolved and the test can be run cleanly.**
