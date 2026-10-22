# KEY_METRICS.md

## Structure: one North Star, a few supporting metrics

Every metric below exists to do one of exactly two jobs: explain movement in the North Star, or
guard against the North Star being gamed. A metric that does neither is logged for context, not
tracked as a key metric — see the closing section for what got deliberately left out and why.

This matters more than it sounds for this specific project. The PRD's central concern is that a
confidently wrong clinical answer is a failure, not a demo quirk — so the metrics here are chosen
to make that concern measurable, not to produce a wall of numbers that looks thorough but doesn't
actually prove anything to a hospital CTO deciding whether to trust this in front of physicians.

---

## North Star: Verification pass rate

**Definition:** the percentage of agent responses in which every clinical claim survives the
source-attribution check (`ARCHITECTURE.md` §3.1) without being stripped or flagged as unverified.

**Formula:** `(responses with zero stripped/flagged claims) / (total responses) × 100`

**Why this is the North Star, not a generic AI-quality metric:** this project's stated premise
(see the PRD's "Why This Matters" section) is that the entire gap between a demo and something
deployable in a hospital is whether the system can be trusted not to hallucinate. Verification
pass rate is the most direct possible measurement of that specific claim — not "does the agent
sound confident," not "did the resident say it was helpful," but "did every factual statement it
made actually trace back to a real record." It is the one number that most directly reflects
whether the product is delivering on its core promise.

**Target:** 100% is the goal in principle (an unverifiable claim should never reach the resident),
but the metric is tracked as a rate specifically to surface *how often* the verification layer is
catching something, which is itself useful signal, not just a pass/fail gate.

**Honest caveat on sample size:** at n=55 (the current Golden Set size, expanded from the original
19), a 90% pass rate carries a 95% confidence interval of roughly ±8 points (using the standard
proportion margin-of-error formula, 1.96 × √(p(1−p)/n) = 1.96 × √(0.9×0.1/55) ≈ 7.9 points). A
single case flipping pass/fail now moves the number by about 1.8 points (1/55) — still real noise at
this sample size, but no longer the dominant source of it the way it was at n=8's ±20-point
interval. Meaningfully more trustworthy as a trend now, not yet large enough to treat single-point
movements as significant on their own.

---

## Supporting metric 1: Tool failure rate

**Job:** explains movement in the North Star.

**Definition:** the percentage of tool calls (per `ARCHITECTURE.md` §2's tool table) that fail,
time out, or return malformed data.

**Why it's needed:** if verification pass rate drops, the agent's own reasoning isn't necessarily
the cause. It could just as easily mean the FHIR API is degraded and the agent has less real data
to ground its claims in — a different problem requiring a different fix (infrastructure, not
prompt or model changes). Without this metric, a drop in the North Star is a mystery; with it,
it's diagnosable within minutes. This directly operationalizes `ARCHITECTURE.md` §4's failure-mode
table, which treats a failed tool call as a first-class event, not an edge case to shrug off.

---

## Supporting metric 2: p95 latency

**Job:** guards against a good North Star number hiding an unusable product.

**Definition:** the 95th-percentile end-to-end response time, from the resident's message to a
verified response being returned.

**Why it's needed:** `USERS.md` use case 3 (synthesis during a rapid response or acute event) is
explicitly time-critical — a resident asking about code status or allergies mid-emergency needs an
answer in seconds, not after a thorough thirty-second verification pass. A verification-obsessed
system that is airtight but slow would still fail this specific use case. Tracking p95 (not just
average) matters because the worst-case tail is exactly what a resident experiences in the highest
-stakes moment, which is precisely when the cost of slowness is highest.

---

## Supporting metric 3: Verified claims per response (anti-gaming guard)

**Job:** guards against the North Star being gamed.

**Definition:** the average number of distinct, source-attributed clinical claims contained in a
response that passes verification.

**Why this one is necessary, not optional:** verification pass rate alone has an obvious, boring
failure mode — an agent that hedges everything, gives vague non-answers, or refuses to commit to
specifics will show a *perfect* pass rate while being nearly useless to a resident at 2 a.m. deciding
whether to escalate to an attending. This metric exists specifically so that a high North Star
number can't be achieved by the agent simply saying less. If a hospital CTO asked "couldn't you
just get 100% verification by having it say nothing useful," this metric is the direct answer:
we track that it isn't.

---

## Summary table

| Metric | Job | What it catches |
|---|---|---|
| **Verification pass rate** (North Star) | Reflects the core promise | Whether claims are actually grounded in real data |
| Tool failure rate | Explains movement | Whether a North Star drop is a data problem, not a reasoning problem |
| p95 latency | Guards against a hidden tradeoff | Whether verification rigor is quietly making the agent too slow for its most time-critical use case |
| Verified claims per response | Guards against gaming | Whether a high pass rate is being achieved by saying less, not by being more correct |

---

## What we deliberately did not make a key metric

Tracked and logged for operational context, but not treated as key metrics, because none of them
explain North Star movement or guard against gaming it on their own:

- **Total request volume / token cost** — necessary for the cost-analysis deliverable and for
  capacity planning, but volume alone says nothing about whether the product is trustworthy or
  useful; a busy but unreliable agent is not a success.
- **Uptime / error rate at the infrastructure level** — a baseline operational requirement (and
  covered by the engineering requirements' dashboards and alerts), but it's a floor to maintain,
  not a signal that distinguishes a good version of this product from a mediocre one.
- **Raw user satisfaction / thumbs-up rate**, if collected — useful qualitative signal, but easy
  to game with a confident, likeable, wrong answer, which is exactly the failure mode this whole
  project exists to prevent. It's logged, not promoted to a key metric, for that reason. Now
  collected: a lightweight in-UI control (👍/👎 plus an optional free-text comment) under each
  agent response, stored in two independent places (an append-only `feedback.jsonl` log, and a
  `resident_feedback` Langfuse score on that turn's trace) precisely so it survives even if one
  storage path has an outage — but "survives" is about data durability, not about elevating its
  importance relative to verification. The reasoning above still applies unchanged, and this data
  must never be folded into or reported alongside verification pass rate.

## Traceability

Every metric above maps to a specific requirement already established in this project's other
documents, not invented independently:

- Verification pass rate operationalizes `ARCHITECTURE.md` §3.1 (source attribution).
- Tool failure rate operationalizes `ARCHITECTURE.md` §4 (failure modes) and §2 (per-tool error
  handling).
- p95 latency operationalizes the time-criticality explicit in `USERS.md` use case 3.
- Verified claims per response exists because of the specific gaming vector the North Star alone
  would otherwise permit.

**The bar, stated plainly:** not "we're measuring a lot," but "we know exactly what we're claiming,
and these four numbers, together, are how we'd prove it's true."

---

## Week 2 section

Added 2026-09-23 for Week 2's multimodal capabilities (document ingestion, RAG, the citation
contract). The North Star above (verification pass rate) is unchanged and still the single most
important number for the whole system, Week 1 and Week 2 alike — these are additional supporting
metrics specific to what Week 2 adds, following the same "job: explains movement, or guards
against gaming" discipline as above.

### Week 2 supporting metric: Extraction field accuracy

**Job:** reflects the core promise of Week 2's headline capability, the same way verification pass
rate reflects it for Week 1.

**Definition, resolved:** two numbers, not one, reported together —
- **Primary: per-field accuracy** — of the required fields per document type (lab_pdf: test name,
  value, unit, reference range, collection date, abnormal flag; intake_form: demographics, chief
  concern, medications, allergies, family history — PRD Core Requirement 2's verbatim minimum), the
  percentage extracted correctly against a known ground truth. The more diagnostic number: pinpoints
  which field type is actually failing, not just that something in a document did.
- **Secondary: whole-document accuracy** — the percentage of documents where *every* required field
  is correct. The more decision-relevant number for a resident: a document with one wrong field out
  of six is still a document they can't fully trust, and per-field accuracy alone can hide that (95%
  per-field can still mean most documents have at least one error, depending on error clustering).

**Formula:** primary = `(fields matching ground truth) / (total required fields checked) × 100`;
secondary = `(documents with zero field errors) / (total documents checked) × 100`.

**Why it's needed:** an OCR + LLM-structuring pipeline can look impressive in a demo while quietly
getting values wrong — the PRD's own named risk ("Vision extraction without invention"). This is the
direct measurement of whether the schema/verification strategy is actually catching that, not just
whether extraction *runs* without crashing. `evals/cases.py`'s 10 extraction-accuracy cases (real
Tesseract OCR, then a real Anthropic LLM structuring call — not hand-built fixtures) are the current
source for both numbers — see `EVAL_GATE.md`.

**Current value (pipeline #28900, 2026-09-25):** counted from the fields that the extraction cases
check against the text rendered into each synthetic image.

| Documents | Per-field accuracy | Whole-document accuracy |
|---|---|---|
| Clean images (7 documents: 4 lab, 3 intake) | **21/21 (100%)** | **7/7 (100%)** |
| Degraded scan (1 lab document) | **1/4 (25%)**. `Potassium` was read correctly; `44` (for 4.4), `meq` (for mEq/L) and `35-50` (for 3.5-5.0) were not. All 4 were marked `low` confidence. | 0/1 |
| All 8 | 22/25 (88%) | 7/8 (88%) |

The 21 clean-image fields: the 6 required lab fields; 3 distinct test names on a multi-result
report; 1 abnormal flag (`H (High)` kept, not normalized); name, DOB and sex; chief concern and 2
medications; 2 allergies and 2 family-history entries; and 1 blank field correctly left empty.

This is a small synthetic sample, and it isn't a gate metric (`evals/gate.py` doesn't compute it).
What it shows: on clean input, every checked field matched. On a degraded scan, the pipeline misread
3 of 4 fields and flagged all of them `low`, so no misread reached the resident at `high`
confidence. The case-level check, "never `high` on a wrong read", passed. A separate case fed a
correctly OCR'd but clinically implausible value (creatinine 141 against a 0.6-1.2 range), and the
plausibility check downgraded it to `unreadable`.

**Pipeline #29930 (2026-09-26), the final code MR:** the same result. Clean images 21/21 and 7/7.
On the degraded scan, 1 of 4 fields was read correctly again; this time the misread reference range
(`3550`) was marked `unreadable` and the other two misreads `low`.

### Week 2 supporting metrics: anti-gaming guards

**Job:** guard against the two Week 2 headline numbers each being gameable in an obvious way,
the same role "Verified claims per response" plays for the Week 1 North Star.

**(a) Unreadable/low-confidence field rate.** Reported alongside extraction field accuracy, not as
a separate headline number. Extraction accuracy alone has a cheap failure mode: a pipeline that
marks an ambiguous field `unreadable` rather than risk a wrong value scores as "not incorrect" by
the accuracy formula, even though it delivered nothing useful. Tracking the unreadable/low-
confidence rate alongside accuracy closes that gap — a high accuracy number paired with a high
unreadable rate means the pipeline is refusing hard cases, not actually solving them.
**Formula:** `(fields marked low or unreadable confidence) / (total fields extracted) × 100`.
**Current value (2026-09-23):** **0%** — 0 of 44 fields extracted across tonight's clean
`eval_golden_set` run were low/unreadable confidence; all 44 came back `high`. A real measured
number from tonight's run logs, not an estimate. Pipeline #28900 doesn't log a per-field confidence count,
so this wasn't recomputed. Its extraction cases did show the non-`high` tiers working as designed:
4 `low` fields on the degraded scan and 1 `unreadable` on the implausible value.

**(b) In-scope retrieval hit rate.** Guards the relevance floor (`W2_ARCHITECTURE.md` §5.1) against
the mirror-image gaming vector: a floor set aggressively enough to never ground an answer in an
irrelevant guideline could just as easily be set so aggressively it returns nothing for genuinely
in-scope questions too, "passing" `citation_present`-style checks by refusing to retrieve rather
than by retrieving well. §5.1 already documents this precisely as a real, accepted tradeoff, not a
hypothetical: *"the floor trades recall on vaguely-phrased relevant questions for the guarantee of
never grounding an answer in an irrelevant guideline"* — a deliberately vague but genuinely on-topic
question can score below the floor, while every in-scope calibration question phrased with real
clinical specificity cleared it comfortably. This metric is how that tradeoff gets watched in
production, not just accepted once at calibration time.
**Formula:** `(genuinely in-scope questions that retrieve at least one chunk above the floor) / (total genuinely in-scope questions) × 100`.
**Current value (pipeline #28900, eval turns only; still not tracked in production):** **10/10**
in-scope guideline questions retrieved at least one chunk above the floor. Across the two eval jobs,
`evidence_retriever` ran on 14 turns:

| Turns | `retrieval_hits` | What they were |
|---|---|---|
| 10 | 3-4 | in-scope guideline questions (5 in `eval_graph_cases`, 5 in `eval_golden_set`) |
| 2 | 0 | out-of-corpus questions (acute pancreatitis; a broad electrolyte question), where 0 is the correct result |
| 1 | 0 | `evidence_retriever_crash_discloses_gracefully`, which injects a crash on purpose |
| 1 | 0 | a follow-up about the patient's own uploaded potassium value. The router also called the retriever, and nothing cleared the floor. |

The last row is the recall-for-precision tradeoff this metric exists to watch. The one-time
calibration set (`evals/calibrate_relevance_floor.py`, `W2_ARCHITECTURE.md` §5.1) is still the only
source for the floor itself.

### Week 2 supporting metric: Citation presence rate

**Job:** reflects the core promise (every clinical claim must be sourced — PRD's "Evidence
grounding" hard problem, Requirement 5's citation contract) and guards against Week 2 silently
regressing to Week 1's weaker, source_resources-only citation shape.

**Definition:** of responses that cite an uploaded document or a retrieved guideline chunk, the
percentage whose `citations` array contains a real (non-empty, correctly-typed) `Citation` object
for that claim — not the old `source_resources: str` shape.

**Formula:** `(responses with a real Citation object backing a document/guideline claim) / (responses making a document/guideline claim) × 100`

**Why it's needed:** this project already found and fixed a real bug where retrieved-but-irrelevant
material could appear in `citations` even when the narrative correctly declined to use it (the
relevance-floor fix, `W2_ARCHITECTURE.md` §5.1) — proof this isn't a hypothetical risk. This metric
is the ongoing guard against a citation existing that doesn't actually back a real, used claim, in
either direction (missing when it should be present, or present when it shouldn't be).

**Current value (pipeline #29930, 2026-09-26):** **100%**, 29/29 applicable cases
(`citation_present`, `evals/gate.py`: floor 100%, baseline 100%, drop 0.0%).

**Known gap: citation precision isn't measured yet.** Presence (does a real Citation object exist
at all) and precision (does the citations list contain *only* claims the response actually makes)
are different properties, and only the first is measured today. Found live tonight: a real
production turn produced roughly 33 citation chips for a short answer — `chart_citations()`'s
`GetUploadedDocumentsOutput` branch adds one `Citation` per field for every document
`get_uploaded_documents` returns, regardless of whether the response actually mentions that field.
Citation presence being 100% says nothing about this. **Since then, a runtime filter has
landed:** `filter_citations_to_stated_facts` (`app/graph/citations.py`) drops an uploaded-document
citation whose extracted value doesn't appear in the final response text. FHIR and guideline
citations aren't filtered this way. One golden-set case
(`uploaded_document_citation_field_or_chunk_id_matches_real_extracted_field`) checks that a
citation points at a real extracted field.
**Precision is still not a gate metric, though.** `grade_citation_present` in `evals/gate.py`
checks only that a citation exists (`AUDIT.md` §6.11). Citations are attached per source, not per
sentence; sentence-level claim-to-citation mapping is a future enhancement (`AUDIT.md` §6.13).

### Week 2 supporting metric: Gate pass rate by category

**Job:** explains movement in the golden set's aggregate pass rate — the Week 2 analogue of "tool
failure rate" explaining North Star movement.

**Definition:** pass rate broken out per PRD category (extraction, evidence retrieval, citations,
refusals, missing-data) and per `evals/gate.py` rubric metric (`schema_valid`, `citation_present`,
`factually_consistent`, `safe_refusal`, `no_phi_in_logs`), not just one aggregate 55-case number.

**Why it's needed:** an aggregate pass rate can hide exactly where a regression lives — e.g. 53/55
passing tells you almost nothing about whether the 2 failures are both in extraction (an OCR/LLM-
structuring problem) or spread across categories (a more systemic issue). `EVAL_GATE.md` documents
the per-category breakdown and the 5%-regression rule this rolls up into.

**Current value (pipeline #29930, 2026-09-26; identical in #28900):** **55/55**, which is 54 passed and 1 skipped
(`oauth_scope_enforcement_denied`, test account not configured on the runner).

| Group | Cases | Passed |
|---|---|---|
| Week 1 chart agent | 20 | 19 + 1 skip |
| Document extraction accuracy | 10 | 10 |
| Uploaded-document citations and storage | 9 | 9 |
| Guideline retrieval (RAG) | 5 | 5 |
| Week 1 safety cases via the LangGraph path | 5 | 5 |
| Routing, worker crash, multi-turn, PHI | 6 | 6 |

| Rubric metric | Passed / applicable |
|---|---|
| `schema_valid` | 37/37 |
| `citation_present` | 29/29 |
| `factually_consistent` | 32/32 |
| `safe_refusal` | 9/9 |
| `no_phi_in_logs` | 53/53 |

Per-case detail: `EVAL_RESULTS.md`.

### Week 2 supporting metric: Document-turn latency (p50/p95)

**Job:** guards against a good extraction-accuracy number hiding an unusable product, the same
role p95 latency plays for Week 1.

**Definition:** p50/p95 end-to-end latency specifically for a turn that invokes `intake_extractor`
and/or `evidence_retriever` (real Tesseract OCR, then real LLM structuring, plus real embedding/
rerank inference, are all synchronous, real work — this is expected to be meaningfully slower than
a chart-only Week 1 turn, and should be measured separately, not blended into one number that hides
which path is slow).

**Why it's needed:** `USERS.md`'s time-criticality concern applies just as much to a document-heavy
turn as a chart-only one, arguably more so given a resident is likely waiting on this specifically
because the answer isn't in the structured chart yet.

**Current value (pipeline #28900, 2026-09-25):** each turn is timed from its `turn_started` log
line to its `turn_summary` line. A "document/guideline turn" is one the router sent to
`intake_extractor` and/or `evidence_retriever`. The runner is staging CI (1 vCPU, 2 GB), not the
production droplet.

| Turns | n | p50 | p95 | max |
|---|---|---|---|---|
| Document/guideline, `eval_graph_cases` (one turn at a time) | 5 | 54.8s | 80.6s | 85.9s |
| Document/guideline, `eval_golden_set` (4 concurrent cases) | 15 | 13.2s | 61.4s | 73.8s |
| **Document/guideline, both jobs** | **20** | **17.2s** | **74.4s** | 85.9s |
| Chart-only graph turns, both jobs | 10 | 13.9s | 21.9s | 25.5s |
| All graph turns (the Week 2 path) | 30 | 14.8s | 67.4s | 85.9s |
| Week 1 agent path (`ClinicalCopilotAgent`), `eval_golden_set` | 20 | 13.2s | 19.4s | 22.2s |

With n=5 to n=30, a p95 is close to the maximum and should be read that way. The two
document/guideline rows differ because the graph-case questions are mostly multi-worker
chart-plus-guideline syntheses. The golden set's document turns include short
`intake_extractor`-only turns (about 6s).

**Where the time goes** (per-node `latency_ms` from `turn_summary`). Where the two jobs differ, the
range spans both jobs' values:

| Node | n | p50 | p95 | max |
|---|---|---|---|---|
| `chart_data_worker` | 23 | 13.1-24.4s | 40.6-47.2s | 49.8s |
| `supervisor_synthesize` | 30 | 0.9-9.1s | 9.9-23.3s | 27.4s |
| `evidence_retriever` (embed + rerank + query-safety call) | 14 | 2.3-3.7s | 3.9-6.7s | 6.7s |
| `supervisor_route` | 30 | 2.4-2.5s | 3.1-3.2s | 3.9s |
| `intake_extractor` | 8 | 1.6s | 3.9s | 4.9s |

The slow tail comes from `chart_data_worker`'s multi-round tool loop plus synthesis, not from
retrieval or OCR. The slowest turn (85.9s) was route 3.2s + `chart_data_worker` 49.8s (22,425 input /
3,725 output tokens) + synthesis 27.0s. The document-structuring LLM call on upload is separate from
these turns: 13 calls, p50 3.2s, p95 5.6s.

### Week 2 — logged, not a key metric

Same discipline as Week 1's own "What we deliberately did not make a key metric" section above:
tracked and logged for operational context, not treated as a key metric, because it doesn't explain
North Star movement or guard against gaming it on its own.

- **Cost per turn** — mean and p95 dollar cost per turn, computed from real token counts at Claude
  Sonnet 5's published per-MTok price (`app/graph/metrics.py`'s `compute_cost_usd`, rolled up
  per-turn and logged via `TurnObserver.log_turn_summary`'s `cost_usd` field). Necessary for the
  cost-analysis deliverable and capacity planning, not a trustworthiness signal by itself — a
  document-ingestion turn (OCR + LLM structuring, possibly a RAG retrieval too) costs meaningfully
  more than a chart-only turn, which is useful operational context, not evidence the product is
  more or less trustworthy.
  **Current value (pipeline #28900, 2026-09-25):** from `turn_summary.cost_usd` (graph path) and
  from `llm_call` token counts at the same $2/$10 per MTok (Week 1 path, which doesn't log
  `cost_usd`):

  | Turns | n | mean | p50 | p95 |
  |---|---|---|---|---|
  | All graph turns | 30 | $0.039 | $0.028 | $0.100 |
  | Document/guideline graph turns | 20 | $0.045 | $0.033 | $0.108 |
  | Chart-only graph turns | 10 | $0.027 | $0.023 | $0.039 |
  | Week 1 agent path | 20 | $0.021 | $0.020 | $0.032 |
  | Document-structuring call (per upload) | 13 | $0.0074 | $0.0067 | $0.0107 |

  Earlier value, 2026-09-23 (job 94454, 19 graph turns): mean $0.069, p50 $0.024, p95 $0.220. Full
  breakdown: `COST_ANALYSIS.md`.
