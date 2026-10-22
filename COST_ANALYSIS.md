# AI Cost Analysis

## Actual development spend to date

| Item | Cost | Notes |
|---|---|---|
| DigitalOcean droplet (4 vCPU / 8GB, "Basic") | $48/month (confirmed on DigitalOcean's current pricing page) | Resized up from 2 vCPU/4GB ($24/month) specifically to resolve the CPU-contention issue that was making Langfuse silently lose visibility into real request latency under load — see `evals/ERROR_ANALYSIS.md` Entry 6. Prorated actual spend so far is a fraction of this given the project's short runtime |
| Claude Code (development assistant) | Flat subscription (Claude Pro/Max), not metered per-token | This is the cost of *building* the agent, separate from what the deployed agent itself will cost to *run* — see below |
| GitHub + GitLab (labs.gauntletai.com) | $0 | Both used at their free tier |
| Self-hosted Langfuse | $0 direct cost | Runs as a container on the already-paid-for droplet; no separate vendor spend |
| ElevenLabs, Midjourney, OpenRouter | $0 | Not used in this project |

**Actual measured spend, pulled directly from the Anthropic Console (Spend this month) on the day
of Early Submission: $0.32.** This covers all development testing, manual interactions, and the
full eval suite run twice (once locally, once against the deployed droplet). Notably, this closely
matches the per-interaction estimate below ($0.03/interaction) — roughly 10 interactions worth of
usage, consistent with the number of eval cases and manual tests actually run.

**Total known cost so far: ~$48/month (the droplet, after the resize) + $0.32 (measured API
usage) — genuinely minimal for a working, verified, deployed agent.**

---

## Cost model methodology

This is not a simple cost-per-token × user-count calculation. The PRD explicitly asks for
projected cost **and** the architectural changes needed at each scale — because those two things
are linked: the naive calculation undercounts real cost at scale (it ignores infrastructure that
must change) and overstates it at small scale (it assumes per-user costs that don't actually apply
until real concurrency exists).

**Per-interaction token estimate**, based on the two tools built for Early Submission
(`get_patient_snapshot`, `check_allergy_conflict`) and Anthropic's current published pricing
(Claude Sonnet 5: $2/MTok input, $10/MTok output, confirmed current as of this writing, not
introductory/expiring pricing):

| Component | Estimated tokens |
|---|---|
| System prompt + tool definitions (tool-use overhead) | ~500 |
| Retrieved FHIR data injected into context (Patient + Condition + AllergyIntolerance + MedicationRequest bundles for one patient) | ~1,500-2,500 |
| Conversation history (grows per turn) | ~300-800, turn-dependent |
| Model's response (output tokens) | ~300-600 |

**Estimated cost per conversational turn:** roughly 2,500 input tokens × $2/M + 450 output tokens ×
$10/M ≈ **$0.0095, call it $0.01 per turn**.

**Estimated cost per full resident interaction** (one page → resolution, averaging ~3 turns
including follow-up questions, per `USERS.md` use case 1): **roughly $0.03**.

This is a planning estimate, not a measured figure — it should be replaced with real numbers
pulled from Langfuse's token-usage logging once enough real usage exists to average over.

**Usage assumption, stated explicitly:** because this agent serves overnight cross-covering
residents (not all patients or all staff, see `USERS.md`), "users" in the tiers below means
registered resident accounts, and a blended estimate of **5 interactions per user per day** is
used, accounting for the fact that any individual resident is only on overnight cross-cover
rotation some nights, not every night.

---

## Projected cost by scale, with required architectural changes

### 100 users
**LLM cost:** 100 users × 5 interactions/day × $0.03 × 30 days ≈ **$450/month**

**Architecture:** no changes needed *for this specific usage assumption* — but that claim needs to
be scoped precisely, not left as a blanket "comfortably handles it." "100 users" here means 100
*registered* users at 5 interactions/day each, not 100 users hitting the service at once, and that
distinction matters: real load testing (`clinical-copilot/PERFORMANCE_BASELINE.md`) found p95
latency around 37 seconds at just **10 concurrent** users, even on the droplet after its resize to
4 vCPU/8GB. At registered-user scale (5 interactions/day, spread across the day), the odds of 10+
truly simultaneous requests are low, so the "no changes needed" conclusion for *this* tier's stated
usage pattern still holds. But the FHIR/OpenEMR bottleneck this document already predicts for later
tiers (see the 10,000-user tier below) is not only a future concern — it is already measurably
present at small scale, confirmed directly by that load test, not projected. See
`PERFORMANCE_BASELINE.md` for the actual numbers rather than restating them here.

### 1,000 users
**LLM cost:** 1,000 users × 5 × $0.03 × 30 ≈ **$4,500/month**

**Architecture changes required:**
- Separate the Co-Pilot service onto its own droplet, away from OpenEMR. The audit already found
  the current droplet running at ~92% memory with just the base OpenEMR stack; adding real
  concurrent agent traffic on the same box risks resource contention affecting OpenEMR itself.
- **Enable prompt caching** on the system prompt and tool definitions (these repeat on every call
  and don't change per-request) — at this volume, caching starts meaningfully reducing input token
  cost, since a cache read is priced at 10% of the base input rate.

### 10,000 users
**LLM cost, naive:** 10,000 × 5 × $0.03 × 30 ≈ **$45,000/month** — but this figure is misleading
without the architecture changes below, which directly reduce it.

**Architecture changes required:**
- **Horizontal scaling of the Co-Pilot service** — multiple instances behind a load balancer, not
  a single process on a single droplet.
- **A dedicated, managed database for Langfuse**, not a single self-hosted container — observation
  volume at this scale needs real database performance, not a Docker volume on a shared droplet.
- **Prompt caching becomes a real cost lever, not just an optimization** — with caching properly
  applied to the repeated system/tool-definition tokens, realistic effective cost is meaningfully
  below the naive figure above; the exact savings depend on cache hit rate, which should be
  measured, not assumed.
- **OpenEMR's own database becomes a real bottleneck**, independent of the LLM cost. The audit
  found zero foreign-key constraints and 1,938 direct SQL calls scattered through the legacy UI —
  at this concurrency, a caching layer or a FHIR bulk-export pattern (already identified in
  `ARCHITECTURE.md` §6 as an available OpenEMR operation) becomes necessary to avoid the FHIR API
  itself becoming the bottleneck, not just the LLM calls.
- **Real alerting and on-call rotation**, not the single-person "check the dashboard yourself"
  posture that's adequate at 100-1,000 users.

### 100,000 users
**LLM cost, naive:** 10x the above, order of **$450,000/month** before any mitigation — this is the
tier where the naive calculation is most misleading, and where architecture, not just token
pricing, determines whether this is viable at all.

**Architecture changes required:**
- This tier is no longer "one project's infrastructure" — it's the infrastructure of a real
  multi-hospital-system healthcare product. A single droplet-based deployment model doesn't apply
  at all; this requires a proper cloud architecture (managed Kubernetes or equivalent), likely
  across multiple regions if serving hospital systems in different geographies (relevant given
  HIPAA's data-residency-adjacent concerns already discussed in `AUDIT.md`).
- **Model tiering becomes necessary, not optional.** Not every interaction needs Sonnet-level
  reasoning — Anthropic's own pricing guidance recommends Haiku for simpler tasks. A lookup-only
  interaction (e.g., "what's this patient's code status") could route to a cheaper model tier;
  reserving Sonnet for interactions that genuinely require the verification/reasoning layer in
  `ARCHITECTURE.md` §3 would meaningfully reduce blended cost at this volume.
- **Volume-based pricing negotiation with Anthropic directly** — Anthropic's published pricing page
  explicitly notes that volume discounts are available and negotiated case-by-case at enterprise
  scale; a project at this size would not be paying list-rate per-token pricing.
- **A dedicated compliance function**, not a project-level BAA checklist. At 100,000 users across
  presumably multiple hospital systems, this needs a formal, ongoing compliance program — SOC 2,
  regular audits, dedicated security staff — not the one-time audit this project performed.

---

## Summary

| Tier | Naive LLM cost | Realistic cost after architecture changes | Key structural change |
|---|---|---|---|
| 100 users | ~$450/mo | ~$450/mo (no change needed) | None — current single-droplet design holds |
| 1,000 users | ~$4,500/mo | Lower, with caching | Separate Co-Pilot from OpenEMR; enable prompt caching |
| 10,000 users | ~$45,000/mo | Meaningfully lower with caching + tiering | Horizontal scaling, managed Langfuse DB, FHIR bulk-export/caching layer |
| 100,000 users | ~$450,000/mo | Substantially lower with model tiering + negotiated pricing | Full cloud architecture, model tiering, enterprise Anthropic pricing, formal compliance program |

The point of this table is the pattern, not the exact dollar figures: **cost does not scale
linearly with users, because the architecture isn't allowed to stay fixed while users grow.** Each
tier above represents a point where the current design would break or become uneconomical without
a specific, named change — which is the actual question a hospital CTO evaluating this product
would ask, not "what does it cost today."

---

## Week 2 update (2026-09-24)

### Actual dev spend

**Anthropic Console spend, month to date as of 2026-09-26: $83.36.** This is read from the
Anthropic Console and covers Week 1 and Week 2 development, manual testing, and every CI eval run
this month. It is $17.82 more than the 2026-09-23 snapshot below.

Earlier snapshot, kept for the record: **Anthropic Console spend month-to-date, as of 2026-09-23: $65.54** — covers Week 1 and Week 2
development and eval runs combined (the Week 1 $0.32 figure above is a snapshot from that earlier
submission, not this month's running total; not restated/combined here since the two numbers were
measured at different points and covering different scope). A single MR pipeline's real
`eval_golden_set` run alone costs roughly $1.42 (measured directly, 113 LLM calls, 528K input /
36K output tokens, this same commit's pipeline) — the volume of real pipeline runs, local
smoke-testing, and manual verification across tonight's work is the dominant driver of the gap
between the two figures.

**Live constraint, worth stating plainly: this project has a $100/month Anthropic API spend cap,
with approximately $34 remaining as of this writing.** Real-call testing from here forward should
be deliberate, not exploratory — CI's own `eval_golden_set` run is the trusted source of truth for
full-suite verification; no further local full 51-case runs tonight (each one is a real,
non-trivial fraction of what's left of the monthly cap on top of what CI itself already spends).

### Current cost per turn, from real `cost_usd` values

`app/graph/metrics.py`'s `compute_cost_usd` (Claude Sonnet 5, $2/$10 per MTok input/output,
re-confirmed live against claude.com/pricing 2026-09-24) has been computing real per-node cost all
along; as of tonight it's rolled up into one per-turn total and logged
(`TurnObserver.log_turn_summary`'s `cost_usd` field). Two real, observed data points, not
projections:

- **A simple chart-only turn** ("give me a quick orientation on this patient," `chart_data_worker`
  only): **$0.023** — `supervisor_route` ($0.0044, 1 LLM call) + `chart_data_worker` ($0.0185, 2
  LLM calls internally).
- **A document-heavy turn** (a real case from tonight's 51-case CI run, `get_uploaded_documents`
  returning several previously-uploaded documents' full extracted JSON into context): individual
  LLM calls in that turn ran as high as 23,704–26,088 input tokens (versus ~1,500–3,000 for a
  chart-only call) — a single one of those calls alone costs **~$0.057**, before the turn's other
  calls are added. **Honest caveat:** this specific turn's high token count is partly an artifact
  of tonight's own test data accumulating many uploads on one fixture patient over the session
  (`PID7_UPLOAD_EVAL`) — a real deployment's per-patient document count would typically be much
  smaller, but the underlying mechanism (every stored document's full extracted JSON goes into
  context on every `get_uploaded_documents` call, unbounded by count) is real and worth watching
  as a genuine cost driver that scales with how many documents accumulate per patient over time,
  not just with call volume.

### Measured cost, pipeline #28900 (2026-09-25)

Computed from the real token counts in the job logs at Claude Sonnet 5's $2/$10 per MTok
(`app/graph/metrics.py`). Graph-path turns log `cost_usd` directly. Week 1-path turns are priced
from their `llm_call` token counts with the same constants.

**Per turn:**

| Turns | n | mean | p50 | p95 |
|---|---|---|---|---|
| Week 1 agent path (chart only) | 20 | $0.021 | $0.020 | $0.032 |
| Graph path, chart only | 10 | $0.027 | $0.023 | $0.039 |
| Graph path, document and/or guideline | 20 | $0.045 | $0.033 | $0.108 |
| Graph path, all | 30 | $0.039 | $0.028 | $0.100 |
| Document-structuring call (once per upload) | 13 | $0.0074 | $0.0067 | $0.0107 |

**Per CI run:**

| Job | LLM calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|
| `eval_golden_set` (55 cases, including 2 judge calls) | 135 | 409,383 | 42,000 | $1.24 |
| `eval_graph_cases` (8 cases) | 36 | 117,457 | 20,910 | $0.44 |
| `eval_unit_tests` (225 tests, mock LLM only) | 0 real | — | — | $0.00 |
| **One full MR eval pipeline** | **171** | **526,840** | **62,910** | **$1.68** |

**Measured vs. the original planning estimate.** The Week 1 methodology below assumed about $0.01
per turn. The measured Week 1-path turn is $0.021 (mean), roughly twice that. A Week 2 graph turn
that touches documents or guidelines averages $0.045. The scale tiers below still use the original
$0.03-per-interaction assumption. Read them as low by about 2× for chart-only use, and more for
document-heavy use. Their structure, meaning which architecture change is needed at which tier, is
unaffected.

### Future optimization: prompt caching (not implemented — named, not built)

**Prompt caching is not enabled anywhere in this codebase today.** Every real call re-sends the
full system prompt and tool definitions from scratch, even though those are identical (or nearly
identical) call to call within a turn and across turns. The real ratio measured directly from a
full `eval_golden_set` CI run (`a565ec4`, 113 LLM calls): **528,106 input tokens vs. 36,080 output
tokens — roughly 15:1.** Pipeline #28900's golden set: 409,383 input vs. 42,000 output, about
9.7:1. With input this dominant, and a cache read priced at a fraction of the
base input rate, caching the repeated system prompts (`SYSTEM_PROMPT`, `_ROUTING_SYSTEM_PROMPT`,
`_SYNTHESIS_SYSTEM_PROMPT`, `_STRUCTURING_SYSTEM_PROMPT`, `_QUERY_SYSTEM_PROMPT` — all five,
`EVAL_GATE.md` §1) and the tool-definition schemas is the single largest cost lever available in
this project, larger than any other optimization named in the scale table below. Not implemented
in this pass — named here as the clear next step, not attempted under tonight's time/budget
constraints (see the live spend-cap note above).

### End-to-end turn latency, before and after the relevance-floor deploy

**Before** (production, commit `a565ec4`, structured JSON logs via `journalctl -u
clinical-copilot`, 28 paired `turn_started`/`turn_finished` events out of 32 total — 4 never
completed, likely killed mid-turn by a service restart, excluded):

| n | p50 | p95 | max | min |
|---|---|---|---|---|
| 28 | 9.00s | 17.65s | 23.00s | 0.00s |

(Two of the 28 durations are exactly 0.00s — almost certainly an immediate error, not a genuinely
instant real response; worth a closer look before this number is presented without qualification.)

**After (pipeline #28900, 2026-09-25):** measured on the staging CI runner (1 vCPU / 2 GB), not
on production. The "before" row is production, so the two are not a like-for-like comparison. Each
turn is timed from `turn_started` to `turn_summary`.

| Turns | n | p50 | p95 | max |
|---|---|---|---|---|
| Week 1 agent path | 20 | 13.2s | 19.4s | 22.2s |
| Graph path, chart only | 10 | 13.9s | 21.9s | 25.5s |
| Graph path, document and/or guideline | 20 | 17.2s | 74.4s | 85.9s |
| Graph path, all | 30 | 14.8s | 67.4s | 85.9s |

No production Week 2 latency sample has been pulled yet. That still needs 5-10 real turns on the
droplet using `demo_documents/`.

### Bottleneck analysis

**Measured (pipeline #28900, per-node `latency_ms`):** `chart_data_worker` dominates, with p50
13.1-24.4s, p95 40.6-47.2s and max 49.8s. After it comes `supervisor_synthesize` (p95 9.9-23.3s,
max 27.4s). `evidence_retriever` (p50 2.3-3.7s), `supervisor_route` (p50 about 2.5s) and
`intake_extractor` (p50 1.6s) are small by comparison. The slowest turn, 85.9s, was a
chart-plus-guideline question: route 3.2s, then `chart_data_worker` 49.8s on 22,425 input tokens,
then synthesis 27.0s. Retrieval and OCR are not the bottleneck. The chart worker's multi-round tool
loop and a long synthesis step are. Details: `KEY_METRICS.md`, "Document-turn latency".

The earlier, qualitative notes:

- **Real OCR + real Anthropic structuring calls dominate extraction-case latency** — Tesseract OCR
  itself is fast (self-hosted, CPU-bound, sub-second on the synthetic images tested), but the
  structuring LLM call is a real Anthropic round-trip like any other, and every extraction turn
  needs at least one.
- **`get_uploaded_documents` scales with accumulated document count, not just this turn's work** —
  see the cost finding above; the same mechanism that inflates cost also inflates latency, since
  more context means more input tokens to process before the model can respond.
- **The embedding + reranker models are self-hosted, not an API cost**, but they are real inference
  work on the droplet's own CPU — combined ~565MB resident memory once loaded
  (`app/retrieval/rerank.py`), and every `evidence_retriever` call pays their inference latency
  even though it costs $0 in Anthropic spend.
- **A single shared CI runner queues jobs sequentially** — not a production-latency concern, but a
  real development-velocity one: see the CI runtime note below.

### Projected cost by scale, Week 2's extra calls factored in

Week 1's per-interaction estimate ($0.03) assumed a chart-only turn. Week 2 adds, per turn that
actually needs them (not every turn does — a pure chart question still costs roughly the Week 1
figure):

- **The supervisor now runs twice** where Week 1 ran the chart agent once — a routing call, then a
  synthesis call — measured at ~$0.004–0.008 combined for the routing+synthesis overhead alone,
  before whichever worker(s) it routes to.
- **Real OCR + a real structuring LLM call** for any turn that extracts a document (not just reads
  one back) — the structuring call alone is a genuine Anthropic API cost each time a new document
  is attached.
- **A RAG query-safety LLM call** (`build_safe_query`) plus self-hosted (non-Anthropic-billed)
  embedding/rerank inference for any turn that needs guideline evidence.

Net effect: a blended per-user cost estimate needs a realistic mix of chart-only vs.
document-heavy vs. guideline-question turns, not a single flat per-interaction number the way Week
1's table used. At the same tier boundaries as the Week 1 table above, holding the same 5
interactions/day/user assumption but now assuming roughly 1-in-5 of those interactions is
document- or guideline-heavy (a judgment call, not measured — real usage data should replace this
once available):

| Tier | Week 1 naive estimate | Week 2 blended estimate (rough) | What changes |
|---|---|---|---|
| 100 users | ~$450/mo | ~$550–650/mo | Same infra tier holds; cost delta is purely the heavier per-turn mix |
| 1,000 users | ~$4,500/mo | ~$5,500–6,500/mo | Same architecture changes as Week 1 (separate droplet, prompt caching) still apply, now also caching the structuring/routing/synthesis prompts specifically |
| 10,000 users | ~$45,000/mo (naive) | Meaningfully higher naive, same caching/tiering levers apply | Document-count-driven context growth (see bottleneck analysis) becomes a real reason to cap or paginate how much prior-document context `get_uploaded_documents` includes, independent of the LLM-cost levers already named for Week 1 |
| 100,000 users | ~$450,000/mo (naive) | Same qualitative story, higher absolute numbers | Same enterprise-scale changes as Week 1, plus: the unbounded-context-growth mechanism above needs an actual fix (pagination/summarization of older documents) before this tier is viable at all, not just an infrastructure scale-up |

### CI runtime, and what it means going forward

**Measured, pipeline #28900:** the four jobs run one after another on one runner. The whole
pipeline took 2,162s (36 minutes).

| Job | Venv + `pip install` | Eval run | Job total |
|---|---|---|---|
| `prune_upload_fixture` | — | dry run | 297s |
| `eval_unit_tests` | 281s | 155.3s | 474s |
| `eval_golden_set` | 304s | 428.7s (55 cases, concurrency 4; per-case sum 924.94s) | 770s |
| `eval_graph_cases` | 294s | 289.0s (8 cases, serial) | 621s |

Two things changed since the paragraph below was written. Golden-set cases now run 4 at a time
(`EVAL_CONCURRENCY`). That took the case run from 859s (#28288, the last serial run) to 419.0s and
414.6s (#28346, the first parallel run), for the same 54 cases. And the largest remaining cost
is now dependency installation: each job deletes and rebuilds `.venv`, spending 274-296s in
`pip install` alone, about 14 minutes per pipeline. Caching the venv between jobs is the next CI-time
saving. It isn't done.

Earlier history:

A full 51-case `eval_golden_set` run exceeded GitLab's default 60-minute job timeout (unconfigured
project setting, confirmed via the API) — raised to 3 hours in the GitLab UI once this was found.
This isn't a fluke: 10 real extraction cases each do real OCR + a real structuring call, several
graph-path cases make 2+ real LLM rounds, and a single shared self-hosted runner processes every
job sequentially. **Practical implication for anyone extending this suite:** budget real wall-clock
time for CI, not just API cost, and treat runtime growth as a first-class cost of adding more
real-call cases — the same "architecture doesn't stay fixed while the thing being measured grows"
principle the scale table above already applies to user growth applies here too, just to
development velocity instead of production traffic. Full detail: `W2_ARCHITECTURE.md` §5.1.
