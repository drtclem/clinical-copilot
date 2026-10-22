# W2_ARCHITECTURE.md — Week 2: Multimodal Evidence Agent

Cross-references, does not replace, `ARCHITECTURE.md` (Week 1). This document
covers what Week 2 actually added: document ingestion, multi-agent routing
via LangGraph, an enforced eval-CI gate (already documented as built —
`evals/gate.py`, `.gitlab-ci.yml`), and — per the build's own working
convention — every real deviation from the PRD found while building it, not
silently absorbed.

Status (2026-09-26): everything this document describes is built, merged to `main`, and running
on production (through MR !16, commit `8d65b3b`), verified against a real OpenEMR + Anthropic
deployment, not simulated. The eval gate is enforced in CI on every code MR (55-case golden set,
8 graph cases, 288 LLM-free unit tests; `EVAL_RESULTS.md`). What is still open is listed in §4,
§7 and the "PRD extensions" subsection in §3.1, not silently worked around.

---

## 1. Topology

```
Resident message
      |
      v
 [Supervisor: routes]  --dashed/conditional--> [chart_data_worker]
      |                --dashed/conditional--> [intake_extractor]
      |                --dashed/conditional--> [evidence_retriever]
      |                                              |
      +----------------------<-----------------------+
      v
 [Supervisor: synthesizes + verifies]
      |
      v
Response to resident (text + citations + review flags)
```

Implemented in `app/graph/build.py` (LangGraph `StateGraph`), `app/graph/
supervisor.py` (both supervisor entries), `app/graph/workers.py` (the three
workers). Confirmed live via direct ClickHouse inspection of the self-hosted
Langfuse backend (this deployment's REST query API is disabled, same
constraint Week 1 already documented) that the actual span hierarchy matches
this diagram exactly: `chat_turn` (AGENT, the parent trace) → `worker:
chart_data_worker` / `worker:evidence_retriever` (SPAN) → each worker's own
`llm_call_N` / `tool:*` children, with the supervisor's own routing and
synthesis LLM calls nesting directly under `chat_turn`.

Supervisor routing is inspectable per turn: `routing_decision` is its own log event naming which
workers the supervisor chose. A real production example is in
[`prod_turn_log_sample.jsonl`](clinical-copilot/docs/evidence/prod_turn_log_sample.jsonl):
`"workers": ["chart_data_worker", "intake_extractor"]`, followed by that turn's per-node latency,
tokens and cost in `turn_summary.node_metrics`. The event carries the rationale's length, not its
text (the text can restate clinical terms, so it isn't written to the log); the full rationale is
in that turn's Langfuse trace. See §4.3 and `clinical-copilot/docs/evidence/README.md`.

Every node function calls `app/llm_client.py`'s `LLMClient` Protocol
directly — no LangChain/LangGraph model wrapper class, preserving Week 1's
provider-agnostic seam (PRD Section 2, `ARCHITECTURE.md` 1.2).

`app/main.py`'s live `/chat` endpoint now calls `app/graph/build.py`'s
`run_turn()`, not `ClinicalCopilotAgent.run_turn()` directly — a real
switch, not just new code sitting unused alongside the old path.
`ClinicalCopilotAgent.run_turn()` itself is unchanged in observable
behavior (confirmed at the time via the full 44/44 LLM-free suite before *and* after
the internal `gather_chart_facts()` extraction; the suite is now 288/288) and is still what
`evals/run_evals.py`'s Golden Set drives directly — Week 1's regression
gate is untouched.

## 2. Component status

| Component | Status | Notes |
|---|---|---|
| Graph skeleton (routing, state, workers) | Built, live-verified | |
| Guideline corpus | Built | 4 docs, `app/retrieval/corpus/`. Authored reference summaries, not published guideline text; see §4.30 |
| Hybrid RAG + rerank | Built, live-verified | MiniLM pair, ~612MB measured RSS (PRD's own ~565MB estimate); pipeline in §2.1 |
| Document ingestion (OCR → structure → plausibility → store) | Built, live-verified end-to-end including a real write | |
| Document read-back (`get_uploaded_documents`, `intake_extractor`) | Built, live-verified | Unblocked by §4.1's CSRF fix (`cc99f2a`). Still never silently swallows a failed read (see §4) |
| Citation-translation layer | Built, live-verified | |
| Per-node instrumentation (nested spans, Core Requirement 7 fields) | Built, live-verified via ClickHouse | |
| Click-to-source UI | Built, live-verified on prod | Guideline and document citations both work, including the bounding-box highlight, for non-admin clinicians (MR !12, previews via the Standard API under the user's own token). Verified on prod as `test_clinician` |
| Eval cases | Built, blocking | 55-case golden set, 8 graph cases, 288 LLM-free unit tests, all blocking merges in CI (pipeline #30322, MR !16); see §5 and `EVAL_RESULTS.md` |
| This document | Built | |

### 2.1 Hybrid RAG + rerank: how it works

`evidence_retriever_node` (`app/graph/workers.py`) is the only caller of `app/retrieval/`. The
supervisor routes a turn to it only when the turn needs guideline evidence. One guideline question
goes through these steps:

1. **PHI-safe queries** (`app/retrieval/query.py`, `build_safe_query`). A forced `extract_query`
   tool call (`max_tokens=256`) turns the resident's message into **up to three** short
   clinical-concept phrases, one per distinct topic. For example, a question about fever and
   antibiotic choice with a sulfa allergy becomes two phrases. A regex then strips anything
   UUID-shaped (patient ids) or any run of six or more digits (MRN-shaped) from every phrase, and
   empty phrases are dropped. **What protects what:** the regex is a hard guarantee only for those
   two identifier *shapes*. A patient's name typed into the message is removed by the extraction
   prompt, not by the regex. The golden-set case `rag_query_contains_no_patient_identifiers` checks
   that, but it isn't guaranteed. The worker reads only the resident's message, never chart data,
   so no FHIR content can reach a query.
2. **Chunking** (`chunking.py`). Each corpus file is split into 800-character chunks with
   150-character overlap. This happens once per process, the first time the retriever is used.
   `get_retriever()` builds it lazily, behind a lock plus `lru_cache`, so concurrent first callers
   don't each load the models. Chunk boundaries ignore headings. The nearest `##` heading is kept
   only as the citation's `page_or_section`.
3. **Sparse retrieval, per phrase** (`sparse.py`). BM25 (`BM25Okapi`, `rank-bm25==0.2.2`) over the
   chunks, with a lowercase alphanumeric tokenizer. This catches exact clinical terms: drug names,
   lab names, abbreviations. Top 10.
4. **Dense retrieval, per phrase** (`embeddings.py`). `sentence-transformers/all-MiniLM-L6-v2`
   embeddings, normalized, scored by dot product (cosine), held in an in-memory numpy array. At
   this corpus size there is no vector database. This catches paraphrase ("high potassium" →
   "hyperkalemia"). Top 10.
5. **Fusion, per phrase** (`fusion.py`). Reciprocal Rank Fusion with k = 60 merges the two top-10
   lists by rank, not raw score, because BM25 scores and cosine similarities are on different
   scales. The fused top 10 from every phrase are then **merged into one candidate pool**,
   deduplicated by chunk id, so each topic's own search contributes candidates.
6. **Rerank, per phrase** (`rerank.py`). `cross-encoder/ms-marco-MiniLM-L-6-v2` scores the whole
   merged pool against **each phrase separately**. Each chunk keeps its best score, and the top 4
   are kept. An earlier version reranked once against the phrases joined together. That diluted
   the cross-encoder enough to push a real two-topic question under the floor (§4.18).
7. **Relevance floor** (`retriever.py`). Chunks scoring below −3.4 are dropped, one chunk at a
   time. See §5.1 for the calibration.
8. **Outcome handling** (`workers.py`).
   - **No chunk survives:** retrieval worked and found nothing relevant. The turn gets an
     info-level `no_guideline_match` notice. It is not a failure and doesn't cost the answer its
     verified check (§4.19).
   - **Retriever raises:** the call is retried once. If it fails again, a `partial_failures` entry
     says the answer was given without guideline evidence.
   - **Query extraction is cut off at `max_tokens`:** nothing is retrieved, and the same
     `partial_failures` disclosure is added.
9. **Citations** (`app/graph/citations.py`). Each surviving chunk becomes a canonical `Citation`:

   | Field | Value |
   |---|---|
   | `source_type` | `"guideline"` |
   | `source_id` | the file stem |
   | `page_or_section` | the nearest heading |
   | `field_or_chunk_id` | `<stem>:<index>` |
   | `quote_or_value` | the chunk text |

**Why these choices.**
- *Hybrid.* Clinical questions mix exact tokens, which keyword search handles, with paraphrase,
  which embeddings handle.
- *Per-phrase search and rerank.* One embedding or BM25 query can't represent two unrelated
  concepts at once. Without it, a two-topic question depends on one phrase happening to cover
  both topics.
- *Self-hosted models instead of Cohere Rerank.* The PRD allows "or equivalent". This keeps the
  RAG path free of new vendors, and anyone can deploy it without our API accounts.
- *MiniLM instead of BGE.* This was measured, not assumed: `bge-reranker-base` alone loaded at
  1.49 GB, while the MiniLM pair measured about 565–612 MB in one process (`rerank.py`, table
  above). The trade-off is some retrieval quality for resource fit on a small droplet.
- *Fixed-size chunking.* The PRD lists better chunking as an extension, not core.

**Known limits.**
- Raw chunks can start or end mid-sentence. The UI trims the displayed text, but retrieval uses
  the raw chunk.
- The relevance floor trades recall for safety on vaguely worded questions (§5.1). Multi-topic
  query decomposition is built (step 1 above); specificity-sharpening rewrite is the remaining
  planned fix.
- RRF k, the candidate count and the chunk size are standard defaults, not tuned for this corpus.
- Name removal from queries depends on the extraction prompt (step 1).

## 3. The §4.7 critic-agent substitution

The PDF: *"A critic agent is extension work, not core."* Not built. Its
stated purpose — reject uncited claims, reject unsafe action suggestions —
is satisfied by the same mechanism Week 1 already built and this phase
extended, not a new component:

- **Uncited claims**: `app/verification.py`'s `_check_source_attribution`
  (Week 1, unchanged) plus, new this phase, an `extra_grounded_text`
  parameter on `verify_response()` so a synthesized answer's own guideline
  citations count as grounding too (§6.5 below — this was a real, found-live
  bug, not a planned feature).
- **Unsafe action suggestions**: `_check_domain_constraint`'s hard,
  code-level allergy-conflict re-check (Week 1, unchanged) — still runs
  regardless of which worker(s) produced the draft, since `synthesize_node`
  routes every turn's draft through the exact same `verify_response()` call.
- **Unreconciled document data**: new this phase —
  `_enforce_uploaded_document_disclosure` (tool path) and
  `synthesize_node`'s equivalent check for the same-turn `intake_extractor`
  path both force an explicit "unreconciled/pending review" disclosure onto
  any response citing AI-extracted document facts, per PRD Section 4.2's
  hard rule. Neither is a critic agent second-guessing a drafted answer —
  both are deterministic, pre-existing-pattern code paths.

### 3.1 Other PRD extensions

The PRD's Core Deliverables list includes four more items that its own text treats as extensions.
Status of each:

- **Critic agent:** not built as a separate agent. See above for why `verify_response()`, the
  domain-constraint hard stop and the forced pending-review disclosure cover its stated purpose.
- **Third document type (referral fax / medication list):** not built. Deliberate: the PRD's Final
  Note and its "two document types" scope say to make two reliable first, and
  `DocType = Literal["lab_pdf", "intake_form"]` (`app/ingestion/schemas.py`) is that scope. Adding
  a third would need a new schema, a structuring prompt and extraction-accuracy golden cases.
- **Lab trend chart widget:** not built. It depends on extracted values being queryable as
  Observations, and §4.22 explains why extracted facts are stored as a pending-review OpenEMR
  document, not as FHIR Observations. A review-and-promote workflow would come first, then the
  chart.
- **Contextual retrieval improvements:** partial. Built: multi-topic query decomposition
  (`build_safe_query`, up to 3 phrases), per-phrase fusion and rerank (§4.18), a calibrated
  relevance floor (§5.1) and PHI stripping of queries. Not built: heading-aware chunking,
  specificity-sharpening query rewrite, domain metadata filters.

## 4. Known gaps, precisely diagnosed, tracked

### 4.0 Stale duplicate checkout on production

Production (`root@<deployed-host>`) has two repo checkouts: `/opt/openemr-base-clean_clem` (the
real one — `clinical-copilot.service`'s systemd unit runs `uvicorn` directly out of this
checkout's venv, confirmed live 2026-09-24) and `/root/openemr-base-clean_clem`, several commits
behind and not referenced by anything running. Left alone for now — not deleted, since it isn't
causing any active problem and removing it is a distraction from the actual deployment work
tonight — but flagged here for a later cleanup pass so it doesn't get mistaken for the live
checkout by someone deploying later.

### 4.1 OpenEMR document-read-back route: CSRF key never populated for API requests

`GET /api/patient/:pid/document/:did` (single-document download) 500s with
`"OpenEMR is potentially not secure because CSRF key is empty."` Root
cause, confirmed by reading the actual call path
(`DocumentRestController::downloadFile` → the legacy, session-based
`C_Document` controller → `CsrfUtils::collectCsrfToken()`, which reads a
`csrf_private_key` normally populated at browser-login time by
`CsrfUtils::setupCsrfKey()`): a stateless OAuth Bearer-token REST request
never goes through that bootstrap, so the key is genuinely never set for
this specific route. This looks like an architectural gap in this route
itself (reusing session-only legacy code from what should be a stateless
REST controller), not a local misconfiguration.

**Effect:** `attach_and_extract` (the upload/write path — OCR, structuring,
the numeric-plausibility check, storing both documents) is fully confirmed
working end-to-end, including a real live upload. Reading a previously-
stored extraction back — `get_uploaded_documents` (the 7th chart-data-worker
tool), `intake_extractor_node`'s same-turn read-back, and the click-to-
source preview image endpoint — all correctly degrade rather than crash or
fabricate: found live during this phase that an earlier version of
`list_extracted_documents` (`app/ingestion/storage.py`) silently `continue`d
past a failed per-document read, making "a document exists but couldn't be
read" indistinguishable from "no documents at all" — fixed to return
`(results, partial_failures)`, wired through `GetUploadedDocumentsOutput`'s
own `partial_failures` field and `app/verification.py`'s existing
`_enforce_partial_failures` convention (now including this output type in
`_PARTIAL_FAILURE_TYPES`). Confirmed live: the resident-facing answer now
correctly says *"unable to verify uploaded labs... rather than no labs
exist"* instead of falsely reporting nothing on file.

**Fix:** landed 2026-09-23 (commit `cc99f2a`) — a narrow, additive CSRF skip
for requests already authenticated via a verified OAuth bearer token
(gated on `userRole`, confirmed exclusive to
`BearerTokenAuthorizationStrategy::setupSessionForUserRole()`, never set by
a genuine interactive session-cookie login). Verified both directions
locally (a real bearer-token request and a real browser login) before
deploying, then verified end-to-end on staging. This closes the read-back
path the rest of this section describes as degrading rather than
fabricating — extraction-accuracy eval coverage, previously blocked by
this exact gap (§5), was added the following day once the fix was
confirmed live.

### 4.2 OAuth scopes and a MIME whitelist gap, corrected during this phase

The PRD's own "confirmed live" scopes for document ingestion were wrong on
two of three counts, re-verified directly against `src/Common/Http/
HttpRestRouteHandler.php`'s `getGetRequestPermission()` (a collection route
needs `.s`, an instance route needs `.r`, POST is always `.c`) rather than
trusting an earlier, stale log-correlation reading:

- `GET /api/patient/:pid/document` (list) → `user/document.s`, not `.c`.
- `GET /api/patient/:pid/document/:did` (read one) → `user/document.r`.
- `GET /fhir/DocumentReference?patient=...` (search) → `user/DocumentReference.s`, not `.read`.

Also found: the registered OAuth client's own `scope` DB column does **not**
gate what a password-grant token actually receives on this deployment —
`user/DocumentReference.s` was granted and worked live before that column
listed it. `config.py`/`.env.example`/`README.md` all corrected; see
`config.py`'s own comment for the full, precise story.

Separately: `secure_upload`'s MIME allowlist (`list_options`,
`list_id='files_white_list'`) didn't include `application/json`, so the
paired structured-extraction-JSON upload silently failed even after the
scope fix (the original scanned-document upload, image/PDF, already
succeeded). Fixed with one `INSERT`, documented in `README.md`'s setup
section as a required step before ingestion is tested on staging.

### 4.3 Tracing: what production captures, and where

**Production records every turn twice**, and both are checked into the repo as evidence
(`clinical-copilot/docs/evidence/`, indexed in its `README.md`):

- **Langfuse (self-hosted, prod).** Each turn's nested span tree: `chat_turn` → `worker:*` →
  `llm_call_N` / `tool:*`, with latency, token counts and cost per span, plus `eval_outcome` and
  `turn_cost_usd` scores on the trace. Prod is receiving these: a read-only count in its
  ClickHouse on 2026-09-26 found 8, 15 and 22 traces on Sep 24, 25 and 26 (per-day table in the
  evidence README). The screenshot,
  [`langfuse_trace_local.png`](clinical-copilot/docs/evidence/langfuse_trace_local.png), is
  **from a local run on 2026-09-25, not from production**: it shows the same code path's span tree
  (a chart-only turn), because prod's Langfuse UI isn't reachable for a screenshot. The patient id
  is covered.
- **Redacted structured logs (journald).** `turn_started`, `routing_decision`, `llm_call`,
  `tool_call`, `citation_counts`, `verification`, `turn_summary` and `turn_finished`, keyed by one
  correlation id, carrying every Core Requirement 7 field (tool sequence, latency by step, token
  usage, cost estimate, retrieval hits, extraction confidence, eval outcome). See
  [`prod_turn_log_sample.jsonl`](clinical-copilot/docs/evidence/prod_turn_log_sample.jsonl), the
  complete lines for one production turn. It contains only salted hashes and counts, and it passes
  the repo's own PHI grader.

Two limits, both confirmed directly, not assumed:

- **Staging has no Langfuse at all.** `docker ps` on the staging droplet shows only
  `development-easy-openemr-1`/`development-easy-mysql-1`. `/etc/clinical-copilot-ci.env` (the
  file every CI job and manual staging run sources) has no `LANGFUSE_PUBLIC_KEY`/
  `LANGFUSE_SECRET_KEY`, so `settings.observability_enabled` is `False` on staging.
  `no_phi_in_logs` (§5) has therefore been passing there on the strength of the local structured
  log alone (see `EVAL_GATE.md` §3 for the precise scope of that check). Production, which is
  what a grader uses, is traced.
- **Production's Langfuse runs in Langfuse v4 "events_only" mode.** The web UI
  (`http://localhost:3300` on the droplet, loopback-only) responds normally, but the entire public
  query API is disabled: `/api/public/observations`, `/traces`, `/scores` and `/metrics` all
  return "not available on deployments running in Langfuse v4 events_only mode". Trace data
  can't be pulled programmatically for a cost/latency report on this deployment, which is why the
  numbers in `COST_ANALYSIS.md`'s Week 2 section come from journald.

### 4.4 Tesseract OCR: missing on CI, version mismatch between CI and production

The staging CI runner (`shell` executor — jobs run directly on the host OS, no container
isolation) never had `tesseract-ocr` installed at all, discovered when every real-pipeline
extraction case in the 51-case suite failed with `TesseractNotFoundError`, swallowed into
`attach_and_extract`'s generic "OCR failed on this document" disclosure. Confirmed directly, not
assumed: `which tesseract` found nothing, and a direct `run_ocr()` call against a real fixture
document raised the exception explicitly. Fixed by installing `tesseract-ocr` on the runner host
2026-09-24 — persistent, not a one-off `before_script` step, since a `shell`-executor job shares
that host's OS state across every future run.

**Real, known version difference:** the runner now has **tesseract 5.3.4**; production runs
**tesseract 4.1.1**. Both confirmed working via a direct `run_ocr()` test against the same real
fixture document (93.88% average confidence on staging; not yet re-verified at the exact same
confidence on production, since production wasn't the one broken). Not yet observed to cause any
actual behavioral difference, but a real gap between what CI validates and what production
actually runs — worth checking first if OCR-dependent behavior (confidence scoring, exact misread
characters on a degraded scan) ever diverges between the two in a way that's hard to explain
otherwise. `tesseract-ocr` is now documented as a required system dependency in both
`EVAL_GATE.md` §5 and `README.md`.

### 4.5 Extracted-fact grounding doesn't yet weight confidence tier

Found live 2026-09-24, while diagnosing why CI pipeline 26655 blocked on two real cases
(`uploaded_document_citation_present_for_lab_result`,
`uploaded_document_citation_field_or_chunk_id_matches_real_extracted_field`): fixing that gap —
`app/graph/supervisor.py`'s `_extraction_field_values()`, which grounds `state["extracted_facts"]`
(the same-turn `intake_extractor` path) into `verify_response()`'s `extra_grounded_text` — surfaced
a second, pre-existing property worth stating precisely rather than assuming away.

**Extracted values count as grounded regardless of confidence tier, on both paths.**
`_extraction_field_values()` (same-turn) and `_grounded_vocabulary`'s `GetUploadedDocumentsOutput`
branch (`app/verification.py`, the later-turn `get_uploaded_documents` TOOL path) both flatten every
`ExtractedField.value` into the grounding vocabulary unconditionally — `high`, `low`, and
`unreadable` confidence are treated identically. Neither path was ever confidence-gated; the
same-turn fix made it consistent with the tool-call path's existing behavior, not a new
inconsistency.

**This does not mean an unverified value reaches the resident silently.** Every answer that draws
on `extracted_facts` (same-turn) or a `get_uploaded_documents` result (tool-call) unconditionally
carries the "AI-extracted, pending review — treat as unreconciled" disclosure
(`synthesize_node`'s `mentions_uploaded_document_disclosure` check; `app/verification.py`'s
`_enforce_uploaded_document_disclosure`) — that check fires on ANY cited extracted fact, independent
of confidence tier or grounding outcome. So a low/unreadable-confidence value is never presented as
verified *without* the pending-review disclosure — that PRD Section 4.2 hard rule holds. What's
missing is the finer-grained half of PRD Section 4.6 ("extraction confidence... surfaced per
extracted field"): the disclosure is turn-level and identical regardless of which specific tier a
cited value actually has, not a per-value uncertainty flag.

**Deliberately not fixed by filtering low-confidence values out of the grounding vocabulary.**
Removing them from `grounded` would make `_check_source_attribution` strip them as unverified,
which would silently delete the information from the resident's answer entirely — worse than
surfacing an uncertain value with a warning, for a user (USERS.md's cross-covering resident, 2 a.m.,
no time to re-derive what was dropped and why) who needs to know a value exists and is uncertain,
not have it vanish.

**Fixed 2026-09-24 (fix/tomorrow-prep): per-value confidence flagging, not filtering.** A response
citing a `low` or `unreadable`-confidence extracted value keeps that value in the answer — the
grounding vocabulary itself is untouched, per the "deliberately not fixed by filtering" reasoning
above, which still holds — but now gets an explicit, per-value uncertainty note distinct from the
blanket pending-review disclosure, on both paths:
`app/verification.py`'s `_enforce_low_confidence_value_warnings`/`_cited_low_confidence_facts` (the
tool-call path, wired into `verify_response()`'s own pipeline) and `app/graph/supervisor.py`'s
`_extraction_field_label_value_confidence` (the same-turn path, wired into `synthesize_node`
directly after the existing disclosure append). Both use a shared label vocabulary
(`LAB_SUBFIELD_LABELS`/`INTAKE_FIELD_LABELS`, `app/verification.py`) so the note names the real field
— "Potassium value (5.9) was read with low confidence — check the scan before relying on this." —
not a positional or raw-attribute label, on either path.

### 4.6 `verify_response()` only recognizes drug/condition/lab-numeric claim shapes — free-text intake facts are cited and disclosed, not independently verified

Documented 2026-09-25, during the citation-precision work (§4.5 above's sibling fix). Not a new
gap — `app/verification.py`'s own module docstring already says this plainly ("deliberately NOT full
per-claim source tagging... will miss a clinical claim phrased in words outside CLINICAL_VOCAB") —
but not previously spelled out here in terms of what it specifically means for Week 2's intake-form
facts.

**What `_check_source_attribution` actually scans for.** `_find_candidate_terms()` extracts only three
shapes from a drafted response: a curated vocabulary of medication/allergy/condition names
(`_MEDICATION_TERMS`/`_ALLERGY_TERMS`/`_CONDITION_TERMS`, `app/verification.py`), a dose pattern
(`_DOSE_RE`, e.g. "10 mg Lisinopril"), and a bare numeric lab/vital value (`_LAB_VALUE_RE`, e.g.
"5.9" or "38.9°C"). Only text matching one of these three shapes is ever checked against the grounded
vocabulary (`_is_grounded`) and is therefore at risk of being stripped as unverified if it doesn't
match.

**What this means concretely for intake-form facts.** A demographics name ("Jordan Rivera"), a
chief-concern sentence ("shortness of breath for two days"), and free-text allergy/family-history
content ("Penicillin (hives)", "father had an MI at 52") are none of these three shapes — the allergen
or condition name inside them might coincidentally match the curated vocabulary (e.g. "penicillin",
"MI" via `_CLINICAL_ALIASES`), but the surrounding free text never gets scanned as a candidate at
all. `_check_source_attribution` never attempts to verify these claims one way or the other — they
are not flagged as unverified, but they are also never confirmed grounded.

**What still applies to these facts, and what doesn't.** They still carry the turn-level
"AI-extracted, pending review" disclosure (§4.5 above — that check fires on any cited extracted fact,
independent of claim shape), and, per the citation-precision fix (`app/graph/citations.py`'s
`filter_citations_to_stated_facts`/`_document_fact_stated_in_response`), they only keep a citation if
the exact value is actually present in the final response text. **Citation precision is not the same
guarantee as verification** — the precision filter confirms the response *states* the stored value
(a text-presence check), not that `verify_response()` independently *confirmed* the claim the way it
does for a recognized drug/condition/lab-numeric shape. A resident reading a cited allergy or chief-
concern fact is relying on the pending-review disclosure and the OCR/extraction confidence tier
(§4.5), not on `_check_source_attribution` having verified it.

**Not fixed here.** Widening `_find_candidate_terms` to recognize free-text clinical claims generally
is a real extension (closer to full per-claim source tagging, already named out of scope in
`app/verification.py`'s own docstring and `early-submission-build-prompt.md`), not a small addition —
flagged for a future pass, not attempted under tonight's scope.

### 4.7 Multi-document citation filtering is still text-matching, not a real per-claim citation id

Found live via `uploaded_document_multiple_documents_each_cited_distinctly`
(`evals/cases.py`): two uploaded documents of the same type routinely share every field except the
one that actually differs (e.g. two lab reports share `test_name`/`unit`/`reference_range`/
`collection_date`, differing only in `value`). `filter_citations_to_stated_facts`
(`app/graph/citations.py`) originally decided keep/drop per citation in isolation — a plain text
match against the response — with no notion of which document a value belonged to, so the
unreferenced document's shared-field citations rode along on the referenced document's answer.

**Fixed** (this branch) by deciding at the document level first: a document counts as "referenced"
only if the response states at least one of its values that isn't shared by any other cited
document (its distinctive value), and only a referenced document's citations are kept. Two
documents with no distinctive value at all relative to each other (a genuine duplicate upload) fall
back to the original per-value check and survive or drop together, since there is no way to
distinguish them by text.

**Still a heuristic, not a real fix.** This closes the specific shared-boilerplate-field collapse
found live, but it is still fundamentally text-matching against the response, the same category of
approach `_document_fact_stated_in_response` already was. A coincidental cross-document value match
outside the boilerplate-field pattern this fix targets (e.g. two genuinely different documents that
happen to share a distinctive-looking value by chance) could still fool it. **The longer-term fix is
a model-emitted structured citation id per claim** (the model states which `source_id`/`field_name`
each part of its answer comes from, rather than the answer's prose being reverse-matched against
stored values after the fact) — out of scope for this pass, flagged for whoever picks up real
per-claim source tagging (§4.6 names the same underlying limitation for verification's own
source-attribution check; this is the citation-rendering side of the same "text-matching instead of
the model tracking its own sources" gap).

**Not yet confirmed fixed end-to-end.** A staging retest of this exact case after this fix landed
still failed the same way (`the OTHER document was also cited this turn despite not being
referenced`). Traced the whole `synthesize_node` path (`app/graph/supervisor.py`) and confirmed
there is exactly one write to `state["citations"]`, always downstream of this fix's filter — no
code path bypasses it. That leaves two live possibilities, not yet distinguished: (a) the model's
own answer genuinely states both documents' distinctive values (e.g. mentioning the other stored
document's value for context), which this fix's "referenced" rule would then correctly, not
accidentally, keep — a model-behavior question, not a filter bug; or (b) a real bug this fix's
design didn't anticipate. Diagnosis requires the actual response text, which the app's own
PHI-safe structured logs deliberately never record (`response_length` only). **Open item:** the
case is not confirmed fixed, and no direct capture of the response has been run, so (a) and (b)
remain undistinguished.

### 4.8 Verification's literal-text grounding strips valid clinical paraphrases as false positives

Found live via `rag_ambiguous_topic_question_retrieves_most_relevant_not_wrong_doc`: a run that
retrieved `app/retrieval/corpus/sepsis_screening.md` (containing the qSOFA criterion "Systolic
blood pressure ≤ 100 mmHg") had its response's use of the word "hypotension" stripped by
verification as an unverified claim (`passed_source_attribution: false`, `flagged_claims:
["hypotension"]`) — a false positive: "hypotension" is a correct, standard clinical name for
exactly that criterion, not an invented condition.

**Why:** `extra_grounded_text` (`app/graph/supervisor.py`'s `verify_response(...,
extra_grounded_text=[ev.chunk.text for ev in retrieved_evidence] + ...)`) does correctly pass the
retrieved chunk's full text into grounding — that mechanism works. But `_is_grounded`
(`app/verification.py`) checks it with a literal substring match (`t in g`) plus a small curated
alias table (`_CLINICAL_ALIASES` — abbreviation pairs like "htn"/"hypertension" only). "Hypotension"
never appears as a literal substring of "Systolic blood pressure ≤ 100 mmHg," and there is no alias
bridging a named condition to the numeric criterion that defines it. Verification has no
meaning-based/clinical-equivalence reasoning at all — only exact and near-exact text matching.

**This is deliberately strict for safety** (§4.6's `verify_response()` docstring already says this
plainly for the broader claim-shape gap) — a stricter-than-necessary grounding check that
occasionally strips a valid paraphrase is a far safer failure mode than a looser one that misses a
genuinely invented claim. **Not fixed here** — mitigated instead by a prompt-level rule
(`_SYNTHESIS_SYSTEM_PROMPT`) asking the model to restate a guideline's own terms/values rather than
naming a different clinical term for the same concept, so this specific pattern (a named condition
substituting for a numeric criterion) is less likely to occur in the first place. **The real
long-term fix is meaning-based grounding** (e.g. embedding-similarity or an LLM-judged equivalence
check between a candidate term and the grounded text, not string containment) — a substantially
larger change than this pass's scope, and one that trades some of today's conservative-by-construction
safety margin for recall, so it needs its own deliberate design pass, not a quick swap.

### 4.9 Retrieved guideline evidence isn't carried into later turns, unlike chart facts

Investigated live via `graph_follow_up_turn_keeps_uploaded_document_fact_grounded`'s "5.1 mEq/L"
flag (turn 2) before that specific case turned out to have a different root cause (§4.8-adjacent
literal-match issue, fixed — see the range-endpoint grounding fix). The investigation surfaced a
real, separate, confirmed asymmetry worth documenting even though it wasn't the cause that time.

**Chart facts persist across turns.** `app/agent.py`'s `gather_chart_facts` re-seeds a turn's
`turn_records` from the caller's `prior_tool_records` at the very start (re-stamped
`from_prior_turn=True`), and `app/graph/build.py`'s `run_turn()` accepts and threads through a
`prior_tool_records` parameter for exactly this. So `_grounded_vocabulary()` on turn 2 already
includes turn 1's `get_uploaded_documents`/`get_patient_snapshot`/etc. facts, even if
`chart_data_worker` makes zero new tool calls that turn.

**Retrieved guideline evidence does not.** `app/graph/build.py`'s `initial_state` resets
`retrieved_evidence` to `[]` on every turn (line with `"retrieved_evidence": []`), and `run_turn()`'s
signature has no equivalent `prior_retrieved_evidence` (or similar) parameter at all. If
`evidence_retriever` retrieves and correctly grounds a guideline fact on turn N, and the model
(quite reasonably) restates that same fact on turn N+1 — history-recall, not a new claim — that
restatement can only ground if `evidence_retriever` happens to run again on turn N+1 and retrieve
the same chunk. If it doesn't run again (e.g. routing decides no new guideline lookup is needed for
a pure follow-up question), the restated fact fails grounding even though it was genuinely,
correctly sourced one turn earlier.

**Not fixed here**, per instruction — flagged for whoever picks this up. The shape of a fix would
mirror `prior_tool_records`: a `prior_retrieved_evidence` (or accumulated-evidence) parameter
threaded the same way, with the same `from_prior_turn`-style provenance chart facts already carry,
so a later turn can distinguish "grounded this turn" from "grounded on an earlier turn of this same
thread."

**Correction, for the record:** this section documents a real, separate, still-live gap found while
investigating the "5.1 mEq/L" flag above — it is not that incident's actual explanation. That flag
was independently confirmed to be caused by the range-endpoint literal-match gap instead
(`evidence_retriever` genuinely ran on the flagged turn itself, `retrieval_hits=1`).

### 4.10 Unpinned LLM SDK drifted in CI and removed `temperature` (fixed)

`requirements.txt` had `anthropic>=0.40`. On 2026-09-24 the CI runner's fresh venv resolved
`anthropic==1.8.0`, whose `Messages.create()` no longer accepts `temperature` (confirmed by reading
the installed SDK's signature). Every `build_safe_query` call passed `temperature`, so
`evidence_retriever` crashed on every RAG turn in CI while working in the older local/production
environments. Fix (`710fe1c`): drop the parameter and pin `anthropic==1.8.0` exactly.

Consequence to be aware of: query extraction now runs at the SDK's default temperature, which the
original code had set explicitly for determinism; the prompt and the deterministic extraction fixes
carry that burden instead. **Lesson: pin LLM SDKs exactly.** A range on a provider SDK makes CI the
first place a breaking release is discovered. Follow-up (merged to `main`): every
direct and transitive dependency in `requirements.txt` is now pinned exactly, using `pip freeze` from
the CI runner's venv as of pipeline #28200. Linux-only `nvidia-*`/`cuda-*`/`triton` pins carry a
`sys_platform == "linux"` marker so the file still installs on macOS. The pins are a snapshot, not
a lockfile with hashes, and will need deliberate re-freezing to update.

### 4.11 Worker crashes are now contained and disclosed (fixed)

Before `9e50b7e`, an exception a worker's own error handling didn't anticipate (the case above)
failed the entire turn. Now every worker node is wrapped by `_crash_contained` in
`app/graph/build.py`, which records a `partial_failures` entry and lets the other workers' results
through. The synthesis step's "chart facts only" branch previously ignored `partial_failures`
entirely, so a crashed retriever would have been dropped silently there; it now discloses it too.
Regression case: `evidence_retriever_crash_discloses_gracefully` (a retriever that always raises,
on a message that routes to both workers).

Limits: this contains and discloses; it does not retry, and the resident sees a degraded answer
rather than the missing evidence.

### 4.12 Answers were silently truncated at `max_tokens=1024` (fixed, with a tradeoff)

Several `llm_call` events landed on `output_tokens` exactly equal to `max_tokens` (1024), which
`stop_reason` (now captured) confirmed as real truncation: the resident saw a cut-off clinical
answer with no signal. Fix (`a156afd`): the chart worker and synthesis caps are now 4096, and
`disclose_if_truncated()` appends a note whenever `stop_reason == "max_tokens"`, at both sites that
turn an LLM response into resident-facing prose.

Tradeoff: a higher cap allows longer answers, which cost more latency and spend. Brevity should be
enforced by the prompt, not by the cap; the cap should only be a backstop, and the disclosure
covers the case where it is still hit. Other, smaller caps (structuring 2048, routing 512, query
extraction 128) were not changed.

### 4.13 `/documents/preview`: subject and deleted-document checks (fixed)

Before the fix, `/documents/preview` checked authentication (a valid session) and searched
DocumentReferences under the resident's own token, but did not verify that the matched document's
subject was the requested patient. Exposure was bounded by what that resident's token could already
read in OpenEMR; this was a missing consistency check, not a demonstrated cross-patient leak, and it
was found by review, not exploited. It also refused only `status: entered-in-error`, not other
non-current statuses (OpenEMR's FHIR search returns soft-deleted `documents.deleted = 1` rows,
AUDIT.md §6.9).

Fixed in `d157078` (`fix/preview-endpoint-authz`, merged):

- The document's `subject` must positively name the requested patient; a missing subject is refused
  (fail closed).
- The document's `status` must be `current`; anything else is refused.
- `patient_id` must be a UUID and a Patient read under the resident's token must succeed (the same
  gate `/patient` uses).
- Missing, deleted, other-patient's and not-visible documents all return the same 404. Each denial is
  logged with a correlation ID, the resident and a reason code (no patient or document identifiers).

The earlier entered-in-error check (`7b32c29`) and this fix (`d157078`) are both merged to `main`.

Live check (staging, read-only, 2026-09-25, counts only): every DocumentReference for the two
fixture patients that hold uploads carried a `Patient/<uuid>` subject naming that same patient, with
none missing, mismatched or in an unexpected format. Alice: 17 documents (1 current Source Document, 16
`entered-in-error`). Bob: 16 documents (8 current Source Document, 8 current AI-Extracted Pending
Review). No search returned a next page. So the fail-closed subject check would not have refused any
of these. Scope of that evidence: two fixture patients on one staging instance at one point in time;
it does not cover documents uploaded through other paths. There is still no direct
`documents.deleted` check; deletion is inferred from FHIR `status` and from OpenEMR's own 404 on the
Binary route.

### 4.14 Parallel eval execution: shared-state bugs and remaining limits (merged)

The Golden Set can now run cases concurrently (`EVAL_CONCURRENCY`, default 4; 1 = serial in case
order). Results are returned in case order and gate/regression math is unchanged (checked offline
against a serial run). Three pieces of process-wide state would have corrupted concurrent cases and
were fixed:

1. **Per-case log capture.** `no_phi_in_logs` and the routing-log assertions read one shared logger;
   concurrent cases would see each other's lines. Capture is now scoped per case. An untagged record
   (from a thread that lost the case context) is attributed to every active capture, so this can
   cause a false fail but not hide a PHI leak.
2. **Document-cleanup registry.** It was process-wide, so one case's `finally` flush could delete a
   document another case was still using. It is now per-thread.
3. **`get_retriever()`** could load both models once per concurrent first caller; now lock-guarded.

Serial lane: the 23 cases on the shared upload fixture patient (`PID7_UPLOAD_EVAL`) run one at a
time, alongside the pool, because they assert on that patient's document set. A different
fixture patient per case group would shrink the lane; that needs staging-side seeding and is not done.
Expect the lane, not the pool, to bound wall-clock time. Measured (`EVAL_RESULTS.md` §7): the last
serial run (#28288, 54 cases) took **859s**; the first parallel runs (#28346, concurrency 4) took
**419.0s / 414.6s**, about half. Pipeline #29930 (55 cases) took 399.9s wall-clock against 902.11s
of summed per-case time (2.26×).

Known flake risk: `eval_unit_tests` uploads a document to PID1 while `eval_golden_set` runs in the
same CI stage. PID1 cases that assume no uploaded documents could see it. This overlap pre-dates the
parallel work, is not addressed by it, and has not been observed to fail. Also not parallelized:
`eval_unit_tests` and `eval_graph_cases`.

### 4.15 Turn latency is high (observed, not yet fixed)

Turns are taking roughly 30-50 s. That figure is an observation, not a profiled breakdown. The
working diagnosis is that most of it is sequential chart-worker rounds (each an LLM call of a few
seconds, plus a tool round trip). Candidate next steps, none implemented: batch the allergy
conflict checks into fewer calls, and reduce the number of LLM rounds per turn. Raising the token
caps (§4.12) can add to it.

### 4.16 Allergy checking: curated antibiotic table, policy decisions, and what replaces it (2026-09-25)

**Found in a prod browser check.** `check_allergy_conflict("Bactrim")` returned no conflict for a recorded
"Sulfa drugs" allergy: `app/clinical_reference.py` held only a penicillin class, so a sulfa allergy matched no class
and neither name was a substring of the other. The table now covers penicillins, cephalosporins, sulfonamide
antibiotics, fluoroquinolones, macrolides, tetracyclines, carbapenems, aminoglycosides and single-drug brand groups
(clindamycin, metronidazole, vancomycin, nitrofurantoin, linezolid), with generic **and** brand names. Aliases match as
whole words (`sulfa` is a substring of `sulfate`, so substring matching would flag morphine/magnesium sulfate) and
sulfate/sulfite/sulfonate forms are ignored.

**Clinical policy decisions -- pharmacist review needed before any real use.** These were chosen, not derived:
- **Non-antibiotic sulfonamides (furosemide, hydrochlorothiazide, celecoxib, ...) are NOT flagged by a "sulfa"
  allergy.** Cross-reactivity between sulfonamide antibiotics and non-antibiotic sulfonamides is not established, and
  flagging every one would bury real conflicts. Accepted by the project owner 2026-09-25; a pharmacist should confirm
  or overrule it. The class is "sulfonamide antibiotics" (plus sulfasalazine).
- **Penicillins and cephalosporins are separate classes.** Cross-reactivity depends on shared side chains rather than
  the shared beta-lactam ring (`app/retrieval/corpus/antibiotic_cross_reactivity.md`), so a penicillin allergy does not
  auto-flag a cephalosporin. A test pins this so it cannot change silently.

**Enforcement now follows the table.** Verification's independent re-check (`_check_domain_constraint`) used to cover
only 27 hard-coded drug names (no Bactrim, no cephalosporins), so a fixed tool was consulted only if the model called
it. It now also checks every curated drug name (generic and brand, never a bare class name like "sulfa") the response
mentions, from the same table. The 27-term list is kept for grounding and for non-antibiotic drugs the table does not
cover; nothing lost coverage.

**"No conflict" no longer means "unchecked."** A drug that is not in the table **and** does not name-match any recorded
allergy used to return `conflict_found: false`, indistinguishable from a real all-clear -- cross-reactivity (a brand
name, a drug class) is exactly what name matching cannot see. The tool now returns `assessment: "not_assessed"` (with a
reason), and the agent prompt, the synthesis prompt and `_enforce_unassessed_allergy_checks` require the answer to say
"I couldn't check X against the allergy list -- verify with pharmacy", never that it is safe. Scope: only medications
the model actually checked and the answer names (re-checking every drug mention would flag a patient's own current
medications on every turn). It does not fail the domain constraint (no wrong claim was made) but it does show as an
enforced warning, so the answer loses its verified check.

**Known remaining gaps.** (1) The table is antibiotic-focused; an allergen the table does not recognize (e.g. "beta-lactam
antibiotics", "ACE inhibitors") is compared to a drug by name only, even when the drug is recognized. (2) Only
generic/brand names are known, not dose forms, combinations or misspellings. (3) No severity grading.

**Production replacement: RxNorm / RxClass.** NLM's RxNorm normalizes generic and brand names to a single concept (RxCUI)
and RxClass exposes drug-class membership (ATC, VA, EPC/mechanism classes) for any RxCUI -- replacing this hand-curated
alias list and giving the brand/generic mapping for every drug, not just antibiotics. Allergy substances are coded in
RxNorm/SNOMED in a properly coded chart, so the allergy side should be resolved to a concept, not matched as free text.
RxClass gives class *membership*, not cross-reactivity *rules*: real cross-reactivity and severity still need a licensed,
maintained reference (First Databank, Medi-Span, Multum) integrated at the EHR/pharmacy layer. Until then this table is a
scoped stand-in and must not be presented as a drug-interaction database.

### 4.17 Verification treats every ungrounded mention as a patient-fact claim (known limitation; planned design)

**Limitation.** `_check_source_attribution` finds curated condition/drug terms by substring and treats any that is not
grounded in this turn's tool output or retrieved guideline text as a claim about the patient, then removes the sentences
containing it. It has no notion of the *role* of a mention, so "screen for sepsis", "no evidence of sepsis", "is this
pneumonia?" and "consider azithromycin" are removed exactly like "the patient has sepsis". Two effects seen in production:
a differential/screening answer loses its clinically useful content, and the recurring
`domain_constraint_cross_reactive_allergy` first-attempt failure (the model names an alternative such as azithromycin,
it is ungrounded, it is stripped, `verification_passed=False`). It is compounded when retrieval misses (nothing can ground
"sepsis") and by the corpus having no pneumonia document (that mention is always ungrounded).

**Decision (2026-09-25): keep this behavior for now.** The synthesis prompt's rule against naming a condition "even as
general reasoning" and the ungrounded-term stripping stay as they are. Only the *removal formatting* was fixed (below).

**Planned design (not implemented).**
1. Classify the role of each ungrounded mention before acting. *Patient fact* ("she has / is on / allergic to / labs
   show") keeps today's behavior. *Screening, differential, rule-out, recommendation, option list, negation, question*
   ("screen for", "rule out", "consider", "options such as", "no evidence of", a trailing "?") is not a claim about the
   chart: keep it and do not count it as a verification failure.
2. Deterministic cue rules first; optionally a small classifier for the residual, run only when something is ungrounded.
3. **Fail closed.** Any mention the classifier is unsure about is treated as a patient-fact claim (today's behavior). The
   asymmetry is deliberate: wrongly *keeping* an invented patient fact can mislead a clinician about the chart, while
   wrongly *removing* a general-knowledge mention costs some helpfulness. So new leniency must be earned per cue, with
   precision/recall measured on a labelled sentence set before it is enabled, and patient-fact sentences must still be
   removed in that set.
4. A recommended medication is not a grounding question at all -- it must go through the allergy check (4.16).
5. Removing the synthesis rule is a separate policy decision: the plan narrows it to claims about *the patient*.

**Fixed now: removal no longer mangles the answer.** `_remove_unverified_sentences` removes only the sentence or bullet
that carries the term, leaves every other line exactly as written, never glues lines together (the old code rejoined
everything with `" ".join`, flattening newlines and deleting a whole unpunctuated bullet block with its heading), keeps
`e.g.`/`i.e.` inside their sentence, and drops a lead-in ("Screen for:", a bold heading) whose entire body was removed.
Which text is removed is unchanged.

### 4.18 Two-topic guideline questions were reported as "no relevant guideline" (fixed)

Measured with the real models: the rerank ran against the phrases joined with `" ; "`, which dilutes the cross-encoder
(`sepsis screening fever productive cough` +0.21 alone, -2.08 joined; `sulfonamide allergy antibiotic cross-reactivity`
+2.4 alone, -0.18 joined), pushing genuine two-topic questions under the -3.4 floor. Retrieval now reranks each phrase on
its own over the merged candidate pool and keeps each chunk's best score. Single-phrase results are identical and all 13
out-of-scope calibration questions still return nothing (best -7.2). The floor is unchanged. The corpus still has no
sulfonamide-allergy guidance and no pneumonia document; adding content needs clinical review.

### 4.19 "No relevant guideline" is a notice, not a failure (fixed)

A guideline search that ran fine and found nothing relevant is an expected outcome. It used to travel through
`partial_failures`, so the resident saw a `[Verification note: ...]` bracket, lost the verified check, and had the raw code
`worker_partial_failure` listed as a warning -- the same treatment as a crash. It is now a structured, severity-tagged
notice (`app/graph/notices.py`, `ChatResponse.notices`): `no_guideline_match`, severity **info**, rendered as a neutral chip
with plain wording ("I didn't find a guideline in the reference set that covers this, so this answer isn't based on
one."), never an enforced warning, and the verified check is kept. Real failures (retrieval service unavailable, a crashed
worker) still go through `partial_failures`, remain warnings, and still add `worker_partial_failure`.

### 4.20 Security hardening (2026-09-25)

Changes from a review of the Week 2 PRD against the running system.

- **Logs carry no raw PHI** (`app/log_redaction.py`). `patient_id`, `other_patient_id` and `resident_username` are replaced by
  a salted HMAC (`h_<12 hex>`): the same patient always maps to the same token, so a patient's turns can still be followed and
  counted, but the token identifies no one without the salt (`LOG_HASH_SALT`; unset -> random per-process salt). `tool_call`
  keeps the tool name, argument *names*, latency and failed/retry but not the argument values (patient id, drug name, search
  phrase). The model's routing rationale, the flagged claims and any drug names in `enforced_warnings` are reduced to a
  length, a count and neutral kinds; a crashed worker logs its exception *type* only. Any UUID left in any other field is
  hashed (safety net). Uvicorn's access log has its query string stripped (it carried `?patient=<uuid>`). `feedback.jsonl`
  stores hashed identifiers (the resident's free-text comment is kept -- that is the feedback). Not covered: Langfuse trace
  payloads (self-hosted; they still carry the patient id and chart content the model saw) and exception tracebacks printed
  by the server, whose exception messages could carry data and are not filtered (worker crashes and provider errors are
  reduced to a type; anything else that escapes is not).
- **The `no_phi_in_logs` gate now actually gates.** It used to search five fixed strings and explicitly allowlisted
  `patient_id`. It now fails on any raw UUID (except `correlation_id`), any fixture patient's name/DOB/SSN/street/phone
  (derived from `evals/seed_fixtures.py`), any resident username, and any drug/allergy/condition term from the clinical
  reference tables. Failure reports name the category and event, never the matched text. Twelve mutations of the redaction,
  the grader, and the surrounding protections are each caught by the offline tests.
- **`/docs`, `/redoc` and `/openapi.json` are off** unless `ENABLE_API_DOCS` is set (local development only).
- **PDFs are capped at 5 pages** (`MAX_DOCUMENT_PAGES`, on top of the 20 MB byte cap). Ingestion renders at most one page past
  the cap, so an oversized PDF is refused cheaply with "This PDF has more than 5 pages. Please upload just the pages that
  matter" (`detail_code: too_many_pages`, HTTP 400) instead of OCR-ing hundreds of pages on a 2-vCPU host. Previews of
  documents stored earlier are not capped.
- **Model-provider outages no longer 500.** An Anthropic error (rejected key, rate limit, overload, connection failure) raised
  in the routing step or any later LLM call is caught around the whole graph run: the resident gets "The assistant is
  temporarily unavailable ... quote this reference: <correlation id>" (HTTP 200, a warning-level `assistant_unavailable`
  notice, no verified check), the failed turn is not added to the conversation, and only the error type and status are logged.
  Found in production: a single 401 at 18:26 on 2026-09-25 surfaced as a 500.
- **`eval_graph_cases` now blocks merges.** It succeeded on the last 3 completed pipelines (28566, 28733, 28900) and on the
  last 7 completed runs, each on its first attempt. `retry: 1` stays.
- **Removed** `evals/trace_review_raw.md`: a raw export of 126 real Langfuse traces (full prompts, responses, patient UUIDs,
  names and DOBs). Its three references keep only the aggregate facts.

Deliberately unchanged: repository visibility (Internal) and the published `test_clinician` grading credential.

### 4.21 Final fixes and remaining gaps (2026-09-26)

**Fixed:**

- **Citation previews work for non-admin clinicians** (MR !12). The preview used to read the source
  document through OpenEMR's FHIR `DocumentReference` search and `Binary` routes. OpenEMR gates those
  behind `admin`/`super` and `admin`/`users`, which Clinicians and Physicians don't hold, so every
  non-admin resident got "This document preview is not available right now". Testing missed it
  because the development account is an Administrator. The preview now reads through the Standard
  API document routes under the resident's **own** token (ACL `patients`/`docs`, which Clinicians
  hold):
  1. resolve the pid;
  2. list the patient's Source Document category;
  3. fetch the document id taken from that listing.

  No privileges are borrowed. The listing's own SQL (category, `foreign_id` = this patient,
  `deleted = 0`) replaces the old FHIR status and subject checks, so another patient's, soft-deleted
  and ownerless documents all get the same 404. Full finding: `AUDIT.md` §6.10.
- **Documents can be uploaded from `/ui`** (MR !13). This adds an **Attach document** control (PDF or
  PNG, 20 MB, 5 pages) with progress, then an extraction card per document. The card has a "Pending
  review" badge, low and unreadable confidence marked per row, and click-to-source per field. The
  document is sent as `uploaded_document_ref` with the next question only. Every value from the
  document, and the file name, is rendered with `textContent`, never `innerHTML`. There are no
  server changes.
- **Empty extractions are retried once, then refused** (MR !14). An intake form with clean OCR once
  came back with no fields. The pipeline stored that empty result, and because uploads are
  de-duplicated by content, the file couldn't be uploaded again. Now, when a page has 25 or more OCR
  words and the extraction has zero fields, structuring is retried once. If it's still empty, the
  upload is refused as `structuring_failed` **before anything is stored** (fail closed). A near-blank
  page with no fields is still stored. A `structuring_result` event logs counts only: attempt, field
  count, OCR word count, OCR confidence. The 25-word threshold is a judgment call, and the cause of
  the original empty result is unknown, because the model's tool call isn't logged.
- **The allergy enforcer no longer flags a drug that is named only to be avoided** (MR !14). In a
  final golden-set run, the model *listed* six penicillin-class drugs as members of the class to
  avoid. No allergy wording fell within the 200-character window, so six HARD STOPs were appended and
  the turn failed. A mention is now also accepted when **every** sentence naming the drug carries an
  explicit avoidance marker (`avoid`, `do not give`, `contraindicated`, `hard-stop`, ...). The
  sentence must contain no contrast or permission wording ("but", "instead", "safe", "consider",
  ...) and no negated marker ("not contraindicated"). Anything unclear still gets the HARD STOP. This
  is a heuristic on top of the unchanged deterministic check, not a replacement for it.
- **The confidence note is no longer repeated** (MR !14). When `get_uploaded_documents` and the
  intake extractor both covered the same document, `verify_response` and the supervisor each
  added a note, with overlapping clauses. The supervisor now adds only clauses the response doesn't
  already contain.

**Still open (stated in `AUDIT.md` §§6.10-6.11):**

- **A guideline's drug name can be stripped as an unverified patient claim.** In pipeline #29194's
  first attempt, `graph_answer_separates_patient_facts_from_guideline_evidence` gave the guideline's
  general hyperkalemia steps, which include albuterol. Source attribution couldn't find albuterol on
  the patient's chart, so it removed that sentence, and the numbered list skipped a step.
  `verification_passed` went false and the gate blocked. This is §4.17's limitation from the other
  side: the verifier doesn't know whether a drug mention came from the chart or from a guideline. A
  real fix has to carry that source into source attribution.
- **Langfuse traces still contain PHI.** Log redaction (§4.20) covers the local structured logs,
  the access log, and `feedback.jsonl`. It doesn't cover Langfuse. Trace payloads still carry the
  patient id and the chart content the model saw. Langfuse is self-hosted on the same droplet, so
  nothing leaves the host, but anyone with Langfuse access can read patient data there.
  `no_phi_in_logs` doesn't check Langfuse.

### 4.22 Derived-fact persistence and the citation contract (how the PRD requirements are met)

**Persistence.** The PRD asks for derived facts to be stored as "FHIR resources or OpenEMR
records". Extracted facts are persisted as an OpenEMR document record in the AI-Extracted Pending
Review category. The record carries the id of its Source Document (`ExtractionResult.doc_id`) and
is deduplicated by the upload's content hash. FHIR Observations weren't used for two reasons:

1. This OpenEMR version exposes no FHIR create route for `Observation`.
2. Writing unreviewed AI extractions into the structured chart would bypass clinician
   reconciliation. The values would read as authoritative results to every FHIR client and rule.

The tradeoff is that the values aren't queryable as Observations, and there is no
review-and-promote workflow yet.

**Citations.** Every citation in a final response uses the PRD's machine-readable shape
(`source_type`, `source_id`, `page_or_section`, `field_or_chunk_id`, `quote_or_value`). The
verifier checks each term it detects (a curated drug, allergy and condition vocabulary, plus doses
and lab values) against the data retrieved this turn, which is the same data the citations are
built from. It strips any term it can't ground. Uploaded-document citations are then filtered to
the values the final text actually states.

**The contract is enforced by the schema, not by convention.** `Citation`
(`app/graph/citations.py`) requires all five fields (`source_type`, `source_id`, `page_or_section`,
`field_or_chunk_id`, `quote_or_value`) and rejects an empty string in any of them; Pydantic raises
before a malformed citation can reach a response. Every ingestion schema (`DocumentCitation`,
`ExtractedField`, `LabResultFact`, `LabPdfExtraction`, `DemographicsFact`, `IntakeFormExtraction`,
`ExtractionResult`) and `Citation` set `extra="forbid"`, so an unknown key raises instead of being
silently dropped. The schemas are not `strict=True`, because a stored extraction's bbox tuple comes
back from JSON as a list. Where a value genuinely doesn't exist the citation says so rather than
guessing:

- `page_or_section` is `"page unknown"` for a stored document fact that has no page number. The UI
  treats that as no page: it shows no page label, requests no preview and shows no thumbnail.
- `field_or_chunk_id` is the resource type for a FHIR reference like `Condition/123`, and the full
  reference when it has no `/`. For uploaded documents it is the field path
  (`results[0].value`, `demographics.name`, `allergies[1]`), the same id on the same-turn and
  later-turn paths. Guideline chunks use the chunk id.
- A field whose extracted value is blank gets no citation, because there is nothing to quote.
- `DocumentCitation.bbox` must be four values in [0, 1] with `x0 <= x1` and `y0 <= y1`. The OCR
  box producer (`app/ingestion/bbox.py`) clamps rounding at the source, so the validator is not
  loosened.

One deliberate boundary filter: the structuring model's lab-result rows are reduced to the six
known keys before the schema sees them. An extra key from the model (e.g. `notes`) is dropped and
counted in a redacted `extraction_extra_keys_dropped` log event (a count only, never the key names
or values), instead of failing the whole extraction. Unit tests for all of this:
`evals/schema_contract_tests.py`.

Scope limits:

- Claims outside the verifier's vocabulary and patterns aren't checked.
- Guideline text counts as grounding (§4.17, §4.21).
- FHIR and guideline citations aren't filtered to stated facts.

Sentence-level claim-to-citation mapping, meaning a citation id attached to each claim (§4.7), is
a future enhancement beyond this contract, not a missing requirement. Full statement:
`AUDIT.md` §6.13.

### 4.30 Guideline corpus provenance: authored, not sourced

The four corpus documents were **written for this project** as general reference summaries:
`hyperkalemia_management.md`, `sepsis_screening.md`, `antibiotic_cross_reactivity.md` and
`anticoagulation_reversal.md`. Each maps to a `USERS.md` use case. They reflect widely taught
guidance of the kind found in the Surviving Sepsis Campaign, ACC/AHA/ASH guidance and standard
references.

They are **not excerpts of published guidelines**, they carry no edition or date, they were **not
verified line by line against primary sources**, and they **have not been reviewed by a
clinician**. Each file opens by saying it is general reference only, not a protocol, and
`app/retrieval/corpus/README.md` says the same.

**Why this meets the PRD.** Stage 2 asks for "a small clinical-guideline corpus relevant to your
user profile". It doesn't require published source text. The graded core is the retriever:
sparse+dense search, rerank, and evidence returned with source metadata (§2.1).

**Trade-off.** Authoring gave controlled scope and clean topical separation between documents.
That separation is what made the relevance floor calibratable (§5.1: a clean gap between in-scope
and out-of-scope scores). The cost is clinical authority. A citation proves *which chunk* grounded
a claim, not that the chunk is clinically correct.

**Production path.**
1. Replace the authored files with versioned, dated published sources: society guidelines, or
   DailyMed/openFDA labels for drug content, which are public domain.
2. Carry `source_title`, `edition` or `published_date`, and a URL on every chunk and into the
   citation.
3. Require clinician or pharmacist sign-off before any corpus change, gated by the same eval
   suite.
4. Recalibrate the relevance floor after any corpus change, because the −3.4 threshold is specific
   to this corpus.

## 5. Eval coverage — honest scope

**The 50-case golden set (Stage 4 / Core Requirement 6, quoted verbatim
from the PRD PDF):** `evals/cases.py`'s `ALL_CASES` — the only suite scored
against the five named boolean-rubric metrics (`schema_valid`,
`citation_present`, `factually_consistent`, `safe_refusal`,
`no_phi_in_logs`, all in `evals/gate.py`) and the only one that's genuinely
PR-blocking (`eval_golden_set`, `allow_failure` removed once its pass rate
was trusted) — now has **55 cases**: the original 19 Week 1 cases, 32
added 2026-09-23, once §4.1's CSRF fix unblocked document read-back, and 4 more added
2026-09-24/25 as findings surfaced (`upload_cap_discloses_omitted`, `conflicting_same_date_values`,
`evidence_retriever_crash_disclosed`, `sulfa_brand_name_allergy`), each pinning a bug found live.
(Update 2026-09-26: the count in this section's original text was 51.)
`evals/graph_cases.py`'s 8 cases (7 scenario cases plus one `/chat` endpoint smoke test) stay
deliberately separate (below) — the PRD asks for *one* golden set, and folding a suite with no
rubric wiring into that count on a technicality would look fine on paper and fail the moment
anyone checked it against the letter of the requirement. They are no longer informational:
`eval_graph_cases` has blocked merges since MR !10 (its `allow_failure: true` was removed).

The 32 new cases close the four gaps a prior audit of the original 19
found: **extraction accuracy** (10 cases — real OCR + real Anthropic
structuring calls against synthetic lab_pdf/intake_form images, not
hand-constructed fixtures; includes an empirically-calibrated degraded-scan
fixture and the PRD's own digit-transposition plausibility scenario run
through the full pipeline, not just the isolated function), **uploaded-
document citation presence** (4 cases — the domain the original 19 had
zero present-citation coverage for; each asserts against the exact `doc_id`
its own setup stored, not just "some uploaded_document citation exists",
so a stale document from a prior CI run can't produce a false pass),
**RAG honest-failure and breadth** (4 cases — a question outside the
corpus's scope, a question broader than any one doc's actual coverage, the
previously-untested 4th corpus doc, and an ambiguous-topic query), and
**refusal/missing-data behavior re-run through the actual graph path**
(5 cases — confirming the same Week 1 safety invariants hold on
`app/graph/build.py`'s `run_turn()`, the code path `/chat` actually calls
today, not just the original `ClinicalCopilotAgent.run_turn()` path).
Plus 3 cases closing the PRD's own named FHIR/OpenEMR-integrity
requirement ("round-trip... without creating duplicate or untraceable
records") and a citation-contract precision check, and 6 cases added for
routing correctness, PHI-in-extraction-trace safety, cross-turn document
grounding, and the retrieval query's own PHI-safety (`build_safe_query`,
previously zero test coverage).

`evals/run_evals.py`'s `run_golden_set()` gained the graph-based case type
this section previously flagged as "real future work, not done here":
`EvalCase.use_graph_path` routes a case through `graph_run_turn()` instead
of `ClinicalCopilotAgent.run_turn()`, and `gate.grade_citation_present` is
given the turn's real `Citation` list rather than falling back to the old
`source_resources` shape — the upgrade this section previously deferred.

**Known limitations in this new coverage, stated plainly:**

- `graph_answer_separates_patient_facts_from_guideline_evidence` checks
  citation *domains* per turn (a patient-fact citation and a guideline
  citation both present, each correctly corresponding to the worker that
  produced it) — not true per-sentence attribution ("this specific
  sentence cites X, that one cites Y, never crossed"). No mechanism
  anywhere in this codebase attributes a citation to a specific sentence;
  only a flat per-turn citations list exists. This is a real, unclosed
  gap, not a weaker check disguised as the full requirement.
- The reranking investigation (motivated by a planned `rag_reranking_
  prefers_more_specific_chunk_over_generic_match` case) found no query,
  phrased the way a real eval case's message needs to be, where reranking
  changed the top retrieved result against the actual 4-document corpus.
  17 real queries were tested directly against `GuidelineRetriever`; the
  only two that flipped (fusion-only top doc vs. reranked top doc) were
  artificially terse 2-3 word fragments ("blood too thin") — the flip
  disappeared the moment either was phrased as a realistic clinical
  question. The case was dropped rather than forced in with an unrealistic
  message. This corpus is small (4 docs) and topically well-separated
  enough that fusion alone already picks the right document for any
  realistically-phrased query tested; reranking's contribution on this
  specific corpus size, empirically, is not yet demonstrated to change an
  outcome, though the mechanism itself runs on every real turn regardless.

### 5.1 Retrieval relevance floor, added 2026-09-24

Found while writing `rag_no_matching_guideline_honest_no_evidence_and_no_
fabricated_citation`: `GuidelineRetriever.retrieve()` had no relevance
floor anywhere — RRF fusion and the reranker both rank a fixed candidate
pool and always return the top-K, with no concept of "none of these are
actually relevant." A pancreatitis question (nothing in this corpus covers
it) retrieved `hyperkalemia_management` and `sepsis_screening` chunks into
`retrieved_evidence` and from there into the response's own `citations`
list. The narrative text that run happened to be honest — the model
noticed the retrieved material didn't apply and said so — but the
structured `citations` object still listed two irrelevant guidelines as
sources, which a caller trusting that field (exactly what it's for) would
have no way to catch, and nothing guaranteed the model would notice and
decline on a different run or phrasing.

**Fix:** `app/retrieval/retriever.py` gained `_MIN_RELEVANCE_SCORE = -3.4`,
a floor on the reranker's own score (the cross-encoder's raw logit,
unbounded — not a 0–1 probability), applied per-chunk after reranking. A
chunk that doesn't clear it is dropped; if none clear it, `retrieve()`
returns empty. `app/graph/workers.py`'s `evidence_retriever_node` now
distinguishes this from a genuine service failure with its own honest
disclosure (`"no guideline in the corpus was relevant enough to this
question"`), surfaced via `partial_failures` the same way a down reranker
already was. `app/graph/supervisor.py`'s `synthesize_node` had a second,
related bug: its "nothing gathered" fallback branch was written when
`evidence_retriever`/`intake_extractor` were still stubs that could never
produce a `partial_failure` of their own, so it always meant "no worker
was routed at all." A legitimately-routed `evidence_retriever` finding
nothing relevant now reaches that exact same state
(`chart_facts=None, retrieved_evidence=[]`) for a completely different,
expected reason — fixed to check `partial_failures` first and give an
honest, specific disclosure instead of a generic "please rephrase."

**Threshold calibration** (a separate set, 8 realistic in-scope questions
— 2 per corpus doc — and 8 realistic out-of-scope ones, pancreatitis-style
topics genuinely absent from the corpus; explicitly NOT tuned against
`rag_no_matching_guideline_honest_no_evidence_and_no_fabricated_citation`
or any other eval case):

| | score |
|---|---|
| In-scope: hyperkalemia (treatment sequence, ECG changes) | 3.544 |
| In-scope: hyperkalemia (renal function affecting management) | **-2.727** (in-scope minimum) |
| In-scope: sepsis (bedside score) | -1.425 |
| In-scope: sepsis (source control) | -0.153 |
| In-scope: anticoagulation (DOAC reversal, bleeding) | 1.652 |
| In-scope: anticoagulation (warfarin reversal, surgery) | 4.435 |
| In-scope: antibiotic (penicillin allergy, cross-reactivity classes) | 4.909 (in-scope maximum) |
| In-scope: antibiotic (allergy history reliability, cephalosporins) | 1.492 |
| Out-of-scope: pancreatitis | -8.315 |
| Out-of-scope: new-onset atrial fibrillation | **-4.117** (out-of-scope maximum) |
| Out-of-scope: pulmonary embolism workup | -4.245 |
| Out-of-scope: diabetic ketoacidosis | -7.866 |
| Out-of-scope: acute asthma exacerbation | -9.703 |
| Out-of-scope: acute stroke, treatment window | -7.237 |
| Out-of-scope: upper GI bleed | -4.798 |
| Out-of-scope: community-acquired pneumonia | -10.528 (out-of-scope minimum) |

In-scope: min -2.727, max 4.909, mean 1.466. Out-of-scope: min -10.528, max
-4.117, mean -7.101. Clean separation, gap of 1.389 between the in-scope
minimum and the out-of-scope maximum. Chosen threshold **-3.4** sits
roughly at the midpoint: **0.673** margin above the out-of-scope maximum,
**0.717** margin below the in-scope minimum. Stated plainly since it's
being asked directly: the in-scope minimum is not razor-close to the
threshold, but 0.673 is a moderate, not large, margin on a score scale
whose full observed range spans roughly -10.5 to +4.9 — a shift in either
group's distribution on a different question set could plausibly close it.

**Real tradeoff, not free:** the floor trades recall on vaguely-phrased
relevant questions for the guarantee of never grounding an answer in an
irrelevant guideline. Found live, immediately, calibrating this: the
cross-encoder reranker is highly sensitive to lexical specificity, not
just topical relevance. `rag_vague_broader_than_corpus_question_honest_no_
match`'s own query ("What are electrolyte abnormality management
considerations broadly?") scores **-8.202** against
`hyperkalemia_management` — worse than the out-of-scope group's atrial
fibrillation question at -4.117, despite being genuinely, if broadly,
on-topic. A specific, well-targeted phrasing of the same underlying
question would very likely score well above the floor (every in-scope
calibration question, phrased with real clinical specificity, cleared it
comfortably); a deliberately vague one does not, regardless of true
relevance. This is a real, known limitation of scoring on lexical/semantic
similarity to a specific passage, not a bug to silently patch around.

**Partly built; the rest is not.** Multi-topic query decomposition **is** built:
`app/retrieval/query.py`'s `build_safe_query` already rewrites each question into up to 3 topic
phrases, with per-phrase retrieval and rerank (§2.1 step 1, §4.18). What is *not* built is
specificity sharpening: reformulating a vague resident question into more specific candidate
queries before it hits the reranker, rather than passing the raw phrasing straight through. That
remains deliberate future work.

`evals/graph_cases.py`'s 8 cases (routing correctness, citations across
`fhir_resource`/`guideline`, RAG retrieval accuracy, multi-worker citation
combination, honest missing-data disclosure for the document domain, and one `/chat` endpoint
smoke test) stay as a separate, additive suite — real coverage of the graph/routing layer, just
not part of the 55-case golden set proper. Update 2026-09-26: `eval_graph_cases` has been
blocking since MR !10 (`allow_failure` removed). Wiring these cases into `gate.py`'s five-metric
rubric is still future work: the job gates on pass/fail per case, not on the rubric metrics.

### 5.2 CI runtime, found live: a full 51-case run exceeded the default
60-minute job timeout

The old 19-case `eval_golden_set` job ran in 490s (~8.2 min). The first
real run of the 51-case set (this phase's own MR) was still running past
the 60-minute mark — GitLab's project-level `build_timeout` was
unconfigured (returns `None` via the API, i.e. the platform default of 60
minutes), and no job-level `timeout:` override existed in `.gitlab-ci.yml`
for any job. Raised to 3 hours in the GitLab UI once this was found. What
this means going forward: **the 51-case set is genuinely CPU/API-latency
bound, not stuck** — 10 real extraction cases each do real OCR + a real
Anthropic structuring call, and several graph-path cases make 2+ real LLM
rounds; a single shared self-hosted runner (staging, `tags: [staging]`)
processes every MR's jobs sequentially, so queue time on top of run time
compounds this further under any concurrent CI activity. Anyone adding
meaningfully more real-call cases to this suite should expect runtime to
keep growing roughly linearly and should watch this number, not assume the
60-minute default still has headroom.

(Update 2026-09-26: parallel execution brought a full run down to about 400–420s wall-clock at
concurrency 4 — 419.0s / 414.6s for #28346, 399.9s for the 55-case #29930. See §4.14 and
`EVAL_RESULTS.md` §7.)

## 6. Other real deviations found while building

1. **`agent.py` refactor**: `ClinicalCopilotAgent.gather_chart_facts()` was
   extracted from `run_turn()` so `chart_data_worker` could reuse the exact
   tool-calling loop rather than a second copy. Behavior-preserving,
   confirmed via the full unit suite before and after.
2. **`LLMClient` extended** with an optional `tool_choice` parameter
   (`app/llm_client.py`) — needed for a reliable forced routing decision;
   backward-compatible, every existing call site omits it.
3. **`FhirClient` extended** with `post_standard_api_document`,
   `get_standard_api_binary` — the write/download primitives ingestion
   needs; `CachingFhirClient` passes both through uncached (a write and a
   raw-file download have no cache semantics to begin with).
4. **Routing prompt bug, found and fixed live**: the original routing
   prompt described `intake_extractor` as also handling "a previously-
   extracted document's facts," but the real implementation only handles
   *this turn's* fresh attachment — a "do we have any prior uploads"
   question was being mis-routed to a worker that structurally couldn't
   answer it. Fixed by clarifying the routing prompt's division of labor
   (prior uploads → `chart_data_worker`'s `get_uploaded_documents` tool).
5. **Verification grounding gap, found and fixed live**: `verify_response()`
   only ever grounded claims against `turn_records` (FHIR facts) — a
   synthesized answer correctly restating a *retrieved guideline's own*
   general advice (e.g., "insulin, then albuterol" as standard hyperkalemia
   management, not a claim this patient is on either) was being stripped as
   unverified. Fixed with the additive `extra_grounded_text` parameter (§3).

## 7. Week 1 technical debt — resolved or still open

**The headline question, answered plainly with evidence, not assumed:** yes,
per-user OAuth sessions genuinely replaced the shared credential for all
live traffic. `app/main.py`'s `/chat` requires a real `copilot_session`
cookie and 401s with no fallback if one isn't present ("No more anonymous
access and no shared fallback credential — Phase 1's whole point," the
code's own comment). That cookie only exists after `app/oauth_session.py`'s
`SessionStore` completes a real `grant_type: "authorization_code"` exchange
at `/callback`, producing a `ResidentSession` whose `FhirClient` is seeded
from that specific resident's own tokens
(`OAuthTokenProvider.from_authorization_code_tokens()`) — not a shared
service account. `session.fhir` is "one instance per logged-in resident
session," by the code's own docstring. The **only** remaining use of the
shared password-grant credential is `evals/run_evals.py`'s automated suite
(no browser available to complete a real login) — a deliberate, scoped,
documented exception for offline regression runs, not a live-traffic gap.

Everything else carried over from Week 1, status checked against the code
and `README.md`'s "Known gaps," not just re-asserted:

| Item | Status | Evidence |
|---|---|---|
| Shared/anonymous credential on `/chat` | **RESOLVED** | See above. |
| Cross-provider patient access (OpenEMR ACL) | **RESOLVED** for the specific finding tested (`encounters:auth_a` granted to the `clin` group), architecturally preferred fix (`patient/Encounter.read` scoping) still **OPEN** | `README.md` Known Gaps; `EVAL_RESULTS.md` §4 |
| FHIR `Encounter` has no sensitivity field | **OPEN** (platform limitation, can't be fixed here) — compensating control **RESOLVED** | `app/sensitivity.py`, `graph_encounter_sensitivity_filter_blocks_high` passing |
| Source-attribution is curated-vocabulary substring match, not full per-claim tagging | **OPEN**, deliberately deferred | `app/verification.py` |
| Multi-patient-per-turn not handled | **OPEN**, deliberately out of scope | `README.md` Known Gaps |
| `check_allergy_conflict` cross-reactivity table is curated, not a real drug-interaction DB | **OPEN**, deliberately scoped | `app/clinical_reference.py` |
| Conversation state in-memory, single-process | **OPEN** | `app/main.py`'s `_conversations`/`_sessions` |
| `/health`/`/ready`, Bruno collection, load tests, alerts | **RESOLVED** | `ARCHITECTURE.md` §§7.5–7.7, `PERFORMANCE_BASELINE.md` |
| No application-level rate limiting | **OPEN** | confirmed by code read — no throttling middleware anywhere in `app/` |
| No secrets-rotation policy | **OPEN** | every credential is a long-lived `.env` value |
| Langfuse is a single self-hosted container, not a managed DB | **OPEN**, named future work | already caused one real incident, `ERROR_ANALYSIS.md` Entry 6 |
| `origin` GitHub remote behind real work | **OPEN**, by design — `gitlab` is authoritative | `README.md` Known Gaps |
| No OpenEMR module / chart UI entry point | **OPEN**, deliberately deferred | `ARCHITECTURE.md` §1.1 |
