# Executive Summary

The Clinical Co-Pilot will be built as an external service, not as code inside OpenEMR, registered
via OAuth2/SMART on FHIR the way OpenEMR's own platform already anticipates for AI decision-support
tools (`dsi_type='predictive'`, found in the architecture audit). It consumes patient data through
the REST/FHIR API, never the database directly, and presents itself inside the chart only through a
thin module that renders UI cards, not clinical logic.

This design is a direct consequence of the audit, not a default choice. The audit found OpenEMR's
legacy interface enforces authorization in a decentralized way, one check per page across 1,048
files, while the FHIR API has a single centralized authorization checkpoint every request passes
through. Building against anything else would inherit the same inconsistent enforcement that
produced two of the audit's highest-severity findings.

The target user is the overnight cross-covering resident (see `USERS.md`), and every capability
below traces to one of that document's four use cases: rapid orientation on a page, verifying a
sign-out instruction against the current chart, time-critical synthesis during an acute event, and
an end-of-shift summary. The agent is a single-agent, tool-calling system, not a multi-agent
framework, because every one of these use cases is one resident having one sequential conversation
with tools, not a task that benefits from multiple coordinating agents.

Data access uses tokens scoped to the logged-in resident, not a backend service credential with
standing access to every patient. This matters directly because of the audit's most consequential
correction: a system-scoped FHIR token does not apply the same sensitivity filtering the UI
enforces, meaning a backend-service agent could see restricted visits the resident themselves
cannot. Binding the agent's access to the resident's own session is the first line of defense
against this; because whether user-scoped tokens actually enforce sensitivity was never verified,
a second, explicit filter is built into the agent's own data-retrieval layer as a compensating
control, regardless of what the API does.

Verification is designed as two distinct layers, per the PRD's requirement. Source attribution
means every clinical claim in a response must be traceable to a specific retrieved record, checked
programmatically before the response reaches the resident, not assumed from the model's output.
Domain constraint enforcement means a small set of hard, rules-based checks, not model judgment,
for things like allergy conflicts, run independently of what the model concludes.

The riskiest known gap, carried over explicitly from the audit, is data fidelity: free-text
clinical entries can lose their content in structured FHIR fields, meaning the agent must read
narrative fallback text for uncoded entries and treat them as lower-confidence. This is documented
here as an open design constraint, not resolved, because measuring how common it is on real data
was never done and shouldn't be assumed away.

Failure handling follows one rule throughout: when a tool call fails or data is missing, the agent
states that plainly rather than answering around the gap. Given this project's stated concern that
a confident wrong answer is worse than an admitted unknown, silent degradation is treated as a
verification failure, not an acceptable fallback.

---

# 1. System shape

## 1.1 Where the agent lives

An external service, not in-process OpenEMR code. Two reasons, both from the audit:

- **Blast radius.** A module running inside OpenEMR inherits ambient database access and sits
  inside a codebase where per-file authorization is demonstrably unreliable (audit-notes.md,
  Findings 1 and 2). An external client is constrained by whatever OAuth2 scopes it's granted,
  centrally enforced, the one part of this codebase where that guarantee actually holds.
- **It's the intended pattern, not a workaround.** OpenEMR's `dsi_type='predictive'` client
  registration and `dsi_source_attributes` table (architecture-audit.md §5.2) exist specifically
  for external predictive decision-support tools to register themselves with declared source
  attributes. Building the Co-Pilot this way uses a mechanism the platform already has, rather
  than inventing a new integration pattern.

A thin OpenEMR module, subscribing to `CardRenderEvent`/`MenuEvent` (the same mechanism OpenEMR's
own demographics screen uses for its dashboard cards), is used only to surface a chat entry point
inside the chart UI. It contains no clinical logic and makes no direct data queries, it exists
purely so the resident doesn't have to leave OpenEMR to reach the Co-Pilot.

## 1.2 Framework

A single Python service calling the Anthropic API directly with tool-calling (function calling),
not a multi-agent framework (LangGraph, CrewAI, or similar).

**Why not a multi-agent framework:** every use case in `USERS.md` is structurally the same shape,
one resident, one conversation, a sequence of tool calls against FHIR endpoints, one response.
Nothing in the four use cases requires multiple agents coordinating, delegating subtasks to each
other, or running in parallel. Adding that machinery here would add real complexity (more moving
parts to secure, log, and verify) with no corresponding requirement to justify it. If a future use
case genuinely needs parallel specialized agents, that's a reason to add the complexity then, not
now.

**Why direct tool-calling over a lighter agent framework:** a thin, directly-controlled tool-calling
loop makes the verification layer (Section 3) straightforward to insert as an explicit step between
"model drafts a response" and "response reaches the resident." A heavier abstraction layer would
make it harder to guarantee that insertion point is never skipped.

**Correcting the record, 2026-09-18 (documentation update reflecting an existing decision, not a
new design choice):** the two reasons above are the entire justification. Single-agent was never
chosen because multiple patients in context would "confuse" the model — that would be a claim
about model capability, not a system-design decision, and this project doesn't need to make it.
The cross-patient guard is direct, code-level evidence to the contrary: `run_turn` in `app/agent.py`
already tracks per-patient state correctly within a single agent's own turn, filtering
`turn_records` down to `records_for_active_patient` before grounding runs, and separately rejecting
any tool call whose target patient doesn't match the declared active one
(`THREAT_MODEL.md` §4.1/§4.2, `evals/unit_tests.py::test_cross_patient_tool_call_blocked`). A
single agent already handles a conversation touching multiple patients correctly; if it didn't,
that mechanism wouldn't be sufficient, and no amount of adding more agents would fix a model
confusing patients within one of them anyway. The real reasons are enforcement (Section 1.2's
verification-checkpoint argument) and shape (every USERS.md use case is one resident, one
conversation, sequential tool calls — nothing here decomposes into distinct specialized roles like
a retrieval agent and a reasoning agent), not a workaround for a model limitation.

## 1.3 Data access and authorization

Data is retrieved exclusively through OpenEMR's REST/FHIR API (never the database directly), using
an OAuth2 **authorization_code** flow bound to the logged-in resident's own OpenEMR session,
`user/`-scoped tokens, not `system/`-scoped backend credentials.

**Why this specific choice:** the audit's live FHIR verification found that a `system/`-scoped
token bypasses the sensitivity filtering the UI enforces (Finding 12), a backend-service agent
would see restricted visits the resident themselves cannot open. Binding the agent's token to the
resident's own authenticated session means the agent's access ceiling is, at minimum, no higher
than what that resident could already reach by clicking around OpenEMR directly.

**Resolved (Phase 1, 2026-09-17):** architecture-audit.md §6.6 flagged whether a `user/`-scoped
token enforces sensitivity filtering as untested. It's now tested, live, against pid4's known
`sensitivity='high'` test encounter (audit-notes.md's "AUDIT TEST: encounter created by dr_1,
attributed to admin, sensitivity high", form_encounter id 1 / encounter 7): a real
`authorization_code`-obtained `user/`-scoped token's `GET /Encounter?patient=...` returned it in
full, identical to Finding 12's `system/`-scope result. **`user/` scope does not enforce
sensitivity filtering either** -- this was never a UI-only gap that a better grant type would
close on its own. Section 3.3's compensating control is not a defensive no-op; it is the only
thing standing between a restricted visit and the model's context, confirmed necessary rather than
assumed necessary.

**Document reads stay under the resident's own token, 2026-09-25 (no privilege borrowing):** the
click-to-source preview briefly depended on OpenEMR's FHIR `DocumentReference`/`Binary` routes, which
OpenEMR gates behind `admin`/`super` and `admin`/`users`, so it only worked for Administrators
(AUDIT.md §6.10). The fix keeps the ceiling at what the resident could reach directly: it reads through
the Standard API document routes (ACL `patients`/`docs`) with the resident's own token, rather than
falling back to the service-account credential for the document bytes. Widening a resident's effective
access to make the feature work was considered and rejected; a deployment whose Standard API document
routes were also admin-only would have to make that trade explicitly.

**Privacy and agent count are independent decisions, 2026-09-18 (documentation update, not a new
design choice):** everything above -- the `user/`-scoped token binding, the sensitivity
compensating control -- is enforced at the auth/token and data-retrieval layer, and holds
identically regardless of how many agents this system runs. Access control does not get weaker if
a future version adds a second agent, and it is not the reason there's only one today. Single-agent
(Section 1.2) is a verification-integrity and complexity decision -- keeping the checkpoint between
model draft and resident-facing response un-skippable, and not adding coordination machinery
nothing here requires. Privacy is a separate concern, already fully addressed by Section 1.3 and
3.3 on their own terms, and would need to be addressed the same way even if this were a
multi-agent system.

## 1.4 Team & Skill Constraints

This is a solo-operator project -- one person building, deploying, and operating the full stack, not
a team with specialized roles. That constraint directly shaped decisions already made elsewhere in
this document, not a footnote added after the fact:

- **Single-agent over multi-agent (Section 1.2).** Beyond the verification-integrity argument
  already given there, a multi-agent architecture adds coordination machinery -- more moving parts
  to secure, log, and verify -- that one person has to build, debug, and keep working alone. Nothing
  in `USERS.md`'s four use cases requires it, so that added maintenance burden has no corresponding
  requirement to justify it.
- **Tooling choices sized to what one person can actually run.** Self-hosted Langfuse (Section 7.4),
  Bruno, and Locust were each picked specifically because they need no new vendor relationship and no
  team to administer: Langfuse's open-source deployment runs as one more container on infrastructure
  already paid for, rather than a managed SaaS contract someone would have to negotiate and someone
  else would have to own; Bruno's collection is plain files committed to this git repo, not a hosted
  workspace requiring account/seat management; Locust is a Python script run from the command line,
  not a load-testing platform with its own account and onboarding. Every one of these is a tool a
  single operator can install, configure, and turn off again without involving anyone else.

# 2. Tool design

Each tool maps directly to a `USERS.md` use case. No tool exists that doesn't trace back to one.

| Tool | FHIR resource(s) | Use case(s) |
|---|---|---|
| `get_patient_snapshot` | Patient, Condition, AllergyIntolerance, MedicationRequest | 1, 3 |
| `get_recent_encounters` | Encounter (filtered per 3.3) | 1, 2, 4 |
| `get_recent_observations` | Observation, DiagnosticReport | 1, 2, 3 |
| `compare_signout_to_chart` | Patient, Condition, MedicationRequest, AllergyIntolerance, Encounter, Observation (fresh fetch, structurally diffed against `turn_records` -- deliberately does NOT take the sign-out's own text as input; see 3.1's UC2 addition note for why) | 2 |
| `check_allergy_conflict` | AllergyIntolerance, MedicationRequest | 2, 3 (backs the domain-constraint layer, Section 3.2) |
| `summarize_shift_events` | aggregates tool calls/results already made this session | 4 |

**Mock vs. real data during development:** early development should run against the audit's own
existing fabricated test patients (pid 1-6, per `audit-notes.md`) rather than inventing new fixtures,
they already include the specific edge cases that matter (a duplicate patient, an empty chart, an
uncoded allergy, conflicting medication doses across duplicate records), which makes them
genuinely useful eval material, not just placeholder data.

**Error handling per tool:** every tool call that fails (timeout, 401, 404, malformed response)
returns a structured failure the agent must surface to the resident directly, "I couldn't retrieve
recent labs for this patient", rather than the agent continuing to answer as if the call had
succeeded with no data. This is a hard rule, not a suggestion, because of Section 4.

# 3. Verification system

Two distinct layers, run after the model drafts a response and before the resident sees it.

**Tool vs. verification step, 2026-09-18 (documentation update clarifying an existing distinction,
not a new design choice):** these are different categories, not synonyms. `get_patient_snapshot`
(and every tool in Section 2's table) is a **retrieval tool** -- it fetches data and feeds it into
the model's context; whether that data gets used correctly is still the model's own reasoning to
get right or wrong. `check_allergy_conflict` is a **verification step** -- a forced, deterministic
domain-constraint check (Section 3.2) that runs independent of what the model concluded, not
something the model can choose to skip. Source attribution (3.1) is neither a tool nor something
the model invokes; it's a code check that runs automatically against every drafted response's tool
output, with no model participation in whether it happens.

This is why `check_allergy_conflict` is not redundant with the allergy list already present in
`get_patient_snapshot`'s output. The snapshot's allergy data is passive: the model might read it
and reason correctly about a proposed medication, or might not, the same way it might correctly
read anything else in its context. `check_allergy_conflict` is forced and deterministic regardless
of the model's own judgment -- it makes an allergy-conflicting response structurally impossible to
complete, rather than trusting the model to have reasoned well over data it already had. The
distinction is load-bearing, not academic, evidenced by a real bug earlier in this project (commit
`822dd92`, 2026-09-16, "trajectory-verified domain-constraint check + gate/attempt-rate reporting"):
`evals/cases.py`'s own `_check_domain_constraint_hard_block` originally scored
`domain_constraint_allergy_hard_block` as a pass purely by scanning `result.response_text` for
conflict-sounding words ("allerg", "conflict", "contraindicat", etc.) -- it never checked whether
`check_allergy_conflict` had actually run. A response could pass this invariant on wording alone,
from the model getting lucky off general knowledge, with the hard-coded check never having fired at
all. The fix added a trajectory check -- confirming `check_allergy_conflict` appears in the turn's
real tool calls, or that `verify_response()`'s own independent domain-constraint re-check enforced
it server-side -- before crediting the case as protected. This is the exact failure mode the
tool-vs-verification-step distinction above exists to prevent: text that merely sounds correct is
not the same guarantee as a forced, deterministic check having actually run, and this incident is
concrete proof an eval suite can be fooled by the former while missing the latter.

## 3.1 Source attribution

Every clinical claim in a drafted response is checked against the actual tool call results from
that same conversation turn before release. Concretely: the drafting step must tag each factual
claim with which tool result it came from; a separate, non-generative check confirms that tagged
source data actually contains what's being claimed. A claim that can't be matched to a real tool
result is either stripped from the response or explicitly flagged as unverified rather than shown
as fact.

**Known limitation, inherited directly from the audit's Finding 11:** an uncoded allergy or problem
can lose its actual content in FHIR's structured fields, surviving only in a narrative `text.div`
field. The agent must read that narrative fallback whenever a structured field returns
`data-absent-reason`, and must mark anything sourced from narrative text as lower-confidence than a
properly coded field. How often real OpenEMR data is uncoded, and therefore how often this
fallback path is actually exercised, is unmeasured, this is called out as an open risk, not
assumed to be rare.

**UC4 addition (`summarize_shift_events`), 2026-09-18 — an honest, explicitly non-load-bearing
exception, not a correctness mechanism:** `SYSTEM_PROMPT` instructs the model to call
`summarize_shift_events` before drafting a shift-handoff summary, rather than synthesizing one
directly from what it already has in context. Examined directly, not assumed, before writing this
paragraph: **this rule does not change what `verify_response()` catches.** `_grounded_vocabulary()`
builds its vocabulary from the turn's full accumulated `ToolCallRecord` list regardless of which
tools ran, so a medication/condition/allergy/encounter/observation-shaped fact is grounded whether
the model routes through `summarize_shift_events` or drafts directly from the same underlying
records. A duplicate-record warning isn't grounded via this mechanism in either path (it's
enforced separately, by `_enforce_duplicate_and_empty_chart`). And an event-shaped claim outside
`_ALL_TERMS`'s medication/allergy/condition vocabulary (e.g. "transferred to the ICU") is never
even considered a candidate needing grounding in the first place, by either path — the same
scannable-vocabulary gap documented for `get_recent_observations` above and in
`clinical-copilot/evals/behavioral_coverage.py`'s module docstring.

So what the rule actually is: a prompt-level nudge toward a deterministic aggregation checkpoint,
kept for auditability and future extensibility (a stable, inspectable `ToolCallRecord` for "what
was staged for this summary," and a concrete point a future, stricter rule could hook into — e.g.
"the shift-summary section may only restate `events` from this record" — which nothing enforces
today), not a §3.2-style wall. Unlike `check_allergy_conflict`, this is not "structurally impossible
to route around" — it's a request the model can still reason past, and empirically did on the first
live test of the weaker initial wording (it synthesized an accurate summary directly from context
without ever calling the tool). Strengthened to an explicit imperative afterward, but strengthened
wording is still wording: nothing here is a code-level guarantee. Documented explicitly so this
isn't later mistaken for the same category of protection §3.2 describes.

**UC2 addition (`compare_signout_to_chart`), 2026-09-18 -- same "no nested LLM call" principle,
investigated before building, not after:** verifying a sign-out claim against the chart genuinely
requires interpreting free text, which is a natural-language judgment call this tool deliberately
does not attempt. It does not take the sign-out's own text as input at all, and does not parse or
semantically compare it -- putting that interpretation inside the tool would mean a second, nested
LLM call whose output nothing in this architecture checks, the same risk named above for
`summarize_shift_events`. Instead it does a fresh, unconditional fetch and structurally diffs it
(by resource id, and by status for medications) against whatever `turn_records` already knew for
this patient -- a data-shape comparison, not a semantic one. The sign-out's prose stays in the
resident's own message, already in the outer model's context; the model connects the claim to the
tool's structured diff in its final drafted response, which still passes through
`verify_response()`'s existing grounding check. Named explicitly, matching the same discipline as
above: this tool's freshness guarantee and diff logic are real, code-level properties; whether the
model correctly *connects* a sign-out claim to that diff in its response is still the model's own
reasoning, not something this tool enforces.

## 3.2 Domain constraint enforcement

A small set of rules-based checks run independently of the model's own reasoning, not as a prompt
instruction the model might or might not follow:

- **Allergy conflict check:** before the agent surfaces or suggests anything involving a
  medication, `check_allergy_conflict` runs as a hard, code-level check against the patient's
  allergy list, not a judgment call left to the model.
- **Sensitivity exclusion (see 3.3):** enforced the same way, a code-level filter, not a prompt
  instruction asking the model to "please not mention high-sensitivity visits."

The principle throughout: anything where a wrong answer is dangerous is enforced in code the model
cannot reason its way around, not requested of the model as an instruction.

This is a direct application of a well-established pattern in agent evaluation: fixes that live
in the environment or tool layer hold; fixes that only live in prompt wording don't, because the
model can still *want* to do the wrong thing even when a guardrail stops it from succeeding.
`check_allergy_conflict` is a wall, not a request — it doesn't ask the model to be careful about
allergies, it makes an allergy-conflicting response structurally impossible to complete. The
distinction matters: a prompt instruction can be forgotten or reasoned around; a code-level check
cannot.

**Phase 5 addition (cross-reactivity), 2026-09-17:** `check_allergy_conflict`'s allergy match was,
until now, a direct/substring comparison only -- it could not catch a proposed medication that is a
*different* drug in the same class as a recorded allergy (e.g. amoxicillin against a documented
penicillin allergy), because that relationship is general pharmacology, not something any FHIR
resource for the patient states. `clinical-copilot/app/clinical_reference.py` adds a small, curated
drug-class table (`DRUG_CLASSES`) that `check_allergy_conflict` also checks, scoped to just the
classes the demo patients need, starting with penicillins. **This table is explicitly a stand-in
for a production drug-interaction database (First Databank/Medi-Span/Multum-style), not a claim of
full clinical decision-support integration** -- it is sized to this project's fixture data, not
general pharmacology, and a real deployment must replace it with a licensed, maintained reference
before this touches real patients. The tool's output now reports `cross_reactive_class` explicitly
(non-`None` only when the match came from this table rather than a direct name match), so a
resident-facing response and this system's own logs can distinguish the two rather than presenting
an inferred class relationship as if it were an exact match on the chart.

**Scope decision: dosage thresholds and interaction flags, 2026-09-18 (documentation update
recording an existing decision, not a new design choice):** the PRD names these alongside allergy
conflicts as domain constraints. They are **explicit future work, not in scope for this build** --
the same treatment already given to the drug-class table above, which is documented as a stand-in
sized to this project's fixture data rather than a claim of full clinical decision-support
coverage. No dosage-threshold or interaction-flag check exists anywhere in `app/` today; this is a
deliberate deferral, not an oversight discovered while writing this note. A production deployment
implementing these would need the same treatment §3.2's cross-reactivity table already got: a
licensed, maintained reference (First Databank/Medi-Span/Multum-style) behind a hard, code-level
check, not model judgment or a curated stand-in table asked to bear more weight than it was sized
for.

## 3.3 Sensitivity compensating control

Confirmed necessary, not just deliberately redundant (Section 1.3): a resident's own `user/`-scoped
token does not enforce sensitivity filtering either, so `get_recent_encounters` and any tool
touching encounter data must apply its own filter -- any encounter this resident's role would not
be permitted to view under OpenEMR's own ACL (the audit's role matrix shows the `clin`/
physician-covering roles this persona maps to do not hold a High sensitivity grant) is excluded
from what reaches the model's context entirely, not just hidden from the final response.

**Implementation note:** the FHIR `Encounter` resource carries no sensitivity marker at all --
`src/Services/FHIR/FhirEncounterService.php` never maps `form_encounter.sensitivity` into any FHIR
field or extension, so this isn't just unfiltered, the FHIR representation doesn't expose the value
to filter on. The compensating control (`clinical-copilot/app/sensitivity.py`) therefore sources
`sensitivity` from OpenEMR's own standard REST API (`GET /apis/default/api/patient/{puuid}/
encounter`, `api:oemr` scope) instead of FHIR -- still "OpenEMR's REST/FHIR API" per Section 1.3,
not a direct-database read.

## 3.4 Medical reference layer

A separate, deliberately additive-only mechanism (`clinical-copilot/app/reference_layer.py`)
attaches informational footnotes to a response after verification -- what a drug's real FDA label
warns about (DailyMed), or what a documented condition actually is (MedlinePlus) -- so a resident
isn't left to look those up themselves mid-shift. This is not part of `verify_response()` and never
blocks, flags, or contradicts the model's own claims; it's the same spirit as a footnote, not a
second gate.

**What triggers it -- two independent, structural checks, not a model judgment call:**

- **Medication path:** fires only when a medication-shaped term (reusing `verification.py`'s own
  candidate detection) co-occurs with a prospective action verb (start, increase, discontinue, ...)
  within 100 characters on either side of the match in the response text
  (`_medications_recommended`). A drug merely read off an existing med list, or narrated in the past
  tense ("she was started on... then discontinued"), does not fire -- deliberately restricted to
  base/gerund verb forms so "start" cannot match inside "started."
- **Condition path:** fires only for a real, chart-sourced `ConditionFact` already present in this
  turn's `turn_records` AND actually mentioned in the verified final response
  (`_conditions_in_turn_records`) -- never a hypothesized or speculative diagnosis, since a
  speculative one was never fetched and therefore can't appear in `turn_records` to begin with.

**Fully deterministic, no model call anywhere in this module.** Both paths make one real HTTP call
(DailyMed's SPL label API; MedlinePlus's Health Topics Search) and extract a fixed, structural slice
of the response -- DailyMed's FDA-authored `<excerpt><highlight>` sub-element for the "Warnings and
Precautions" section, MedlinePlus's `FullSummary` up to the first paragraph break. If that fixed
shape isn't present, the result is an honest "no reference available"
(`ReferenceNote.note_text=None`), never a model call invoked to summarize, paraphrase, or otherwise
fabricate a substitute.

**Kept separate from patient-chart-sourced claims -- isolation enforced structurally, not by
convention:** every function that touches the network takes only a bare drug/condition name string
-- no `FhirClient`, no `patient_id`, no `ToolCallRecord`, nothing else from the chart (verified
directly: `evals/unit_tests.py`'s `test_reference_layer_query_isolation` asserts the outbound query
dict contains only the name plus fixed constants). The result is carried on a separate
`ChatTurnResult.reference_notes` field, never folded into `response` text, so a client renders it
distinctly (`/ui` labels it "Reference material -- not from this chart, not the co-pilot's own
words"). `build_reference_notes` runs once per turn, called from `ClinicalCopilotAgent.run_turn`
*after* the verified final response is already appended to the conversation's own message history --
its output never re-enters the model's own context on a later turn.

**Known limitations, disclosed rather than papered over:**

- **Older DailyMed label formats return no reference.** `_extract_dailymed_excerpt` only handles the
  modern combined "WARNINGS AND PRECAUTIONS SECTION" (LOINC 43685-7), which carries a structured
  excerpt -- confirmed live against real drug labels before building this: Metformin, Lisinopril,
  Warfarin, Amoxicillin, and Albuterol use the modern format, but Penicillin's real label uses the
  older, separate "WARNINGS SECTION" (LOINC 34071-1) instead, which has no excerpt at all, only raw
  unstructured paragraphs. Deliberately does not fall back to extracting from that free-form text:
  there is no fixed, non-arbitrary slice to take without the relevance judgment this whole mechanism
  exists to avoid. How many real-world drugs fall on the older-format side is unmeasured -- the same
  honest-unknown treatment already given to the uncoded-allergy risk in §3.1.
- **MedlinePlus Connect does not work for this system's condition data.** Connect, the more commonly
  cited MedlinePlus API, requires a coded diagnosis (ICD-10-CM, ICD-9-CM, or SNOMED CT) as its query
  key and does not accept a free-text condition name. Checked live against this system's own real
  Condition FHIR data before choosing an API: every `ConditionFact` here has `code: {"text": "..."}`
  with no `coding` array at all -- no ICD-10, no SNOMED, nothing to feed Connect. The Health Topics
  Search service (`wsearch.nlm.nih.gov`) is used instead specifically because it accepts free text
  directly, which is what this system's uncoded diagnosis data actually has.

## 3.5 Confidence thresholds: deliberately absent

This system does not use a probabilistic confidence score or a tunable numeric threshold anywhere in
verification. A claim either matches real tool-call data (§3.1's source attribution check) or it
doesn't -- binary, not a percentage. This is a deliberate design choice, not an unaddressed gap: a
numeric threshold would introduce a tunable parameter that could silently let an ungrounded claim
through above some cutoff (e.g. "85% confident this is grounded"), which directly conflicts with
this project's zero-tolerance stance on unverified clinical claims -- KEY_METRICS.md's North Star,
verification pass rate, is itself binary pass/fail for exactly this reason. There is no cutoff to
tune, audit, or silently drift over time: a claim is either traceable to a real tool result, or it is
stripped/flagged, full stop.

**Not to be confused with** the "lower-confidence" qualitative label §3.1 and the executive summary
both use for narrative-sourced (uncoded) data: that is a disclosure flag on the *source's* own
reliability, shown to the resident alongside an already-grounded claim -- never a numeric score
gating whether the claim survives verification at all. The claim itself still passes or fails the
same binary check either way.

# 4. Failure modes

| Failure | Behavior |
|---|---|
| Tool call fails/times out | State the gap directly to the resident; never answer as if data was retrieved when it wasn't |
| Patient record is empty or clearly incomplete | Say so explicitly ("this chart has no recorded problems/meds/allergies") rather than presenting silence as "nothing to report", directly informed by the audit's data-quality finding that an empty chart currently renders with no warning at all |
| Duplicate patient records exist | Surface the ambiguity rather than silently picking one, the audit found OpenEMR does not flag this to a physician today, and picking the wrong one of two duplicate records with different medication doses is a real, demonstrated risk in this exact dataset |
| A claim fails source-attribution verification (3.1) | Strip or flag as unverified; never shown as fact |
| A domain constraint check (3.2) fails | Hard stop on that specific piece of the response; the agent explains what it can't confirm rather than proceeding around it |
| A tool succeeds but one of its own sub-fetches partially failed (4.2) | Force-disclosed, naming the specific failure, regardless of whether the model mentioned it |

## 4.1 FHIR call timeout, retry, and circuit breaker (2026-09-19)

**Timeout: 12s per attempt**, centralized once on `FhirClient.__init__`'s shared `httpx.Client`
(`app/fhir_client.py`, `Settings.request_timeout_seconds`, `REQUEST_TIMEOUT_SECONDS` env var) --
not per call site, so every current and future caller inherits it automatically, the same
centralization already used for the per-conversation FHIR cache (§6). This is a *change*, not a
new mechanism: a timeout has been configured here since the project's first commit, but at a round
15s never tied to real data. 12s is chosen from `PERFORMANCE_BASELINE.md`'s per-FHIR-call span data
(avg 2.66s, p95 20.87s, max 33.41s, pre-parallelization) -- generous headroom over the common case,
while bounding the tail well under what an unbounded hang could cost a resident.

**Retry: at most one, on a timeout or a 5xx-class response only**, at half the configured timeout
(6s) rather than a fresh full one (`FhirClient._get_with_retry`) -- a call that already failed once
is more likely to fail fast or succeed quickly than to need another full wait, and a second
full-length attempt would let one slow call cost up to 2x the timeout for no added benefit. Bounded
to a single attempt deliberately: `PERFORMANCE_BASELINE.md` Stage 2/3 found OpenEMR's own
database/CPU, not this service, is the real bottleneck under load, so repeated retries against an
already-struggling backend would compound the exact problem this exists to work around, not fix
it. A 4xx response, or a non-timeout `httpx.HTTPError` (e.g. a connection refusal), is not retried
-- out of this rule's deliberately narrow scope, a real future option if a case is found to justify
it, not assumed equivalent.

On exhaustion, this surfaces exactly like any other FHIR failure -- `FhirRequestError` ->
`ToolFailure` / `partial_failures` (§2, §4's table above), never an unhandled exception, never
fabricated data. Applies identically to every call path, including the three hard-bypass call
sites that skip the FHIR cache (`check_allergy_conflict`'s `search_fresh()`, `compare_signout_to_
chart`'s three delegated fetches, `get_recent_encounters`' `get_standard_api()` sensitivity
lookup) -- they're exactly as exposed to a slow/failing OpenEMR as the cached paths, arguably more
so since they can never fall back to a cached value. Verified directly against all three, not just
assumed from centralization (`evals/unit_tests.py`'s `fhir_timeout_retries_once_then_fails_
honestly` / `fhir_retry_then_succeed_returns_real_data`, both against `check_allergy_conflict`,
plus a live-verified run against `get_recent_encounters` and `compare_signout_to_chart`). A timeout
on one of `get_patient_snapshot`'s four concurrent futures (§6's parallelization design) does not
block the other three -- each future's `FhirRequestError` is caught independently, populating that
resource type's own `partial_failures` entry while the others complete normally; this was already
true of the pre-existing per-future error handling and continues to hold with retry/timeout errors
specifically (live-verified, not just inferred).

**Circuit breaker: deferred, not built.** Named as future work only, consistent with how it was
scoped from the start -- a broken or partially-tested circuit breaker would be worse than this
honestly-documented gap. If FHIR calls have been timing out repeatedly in a short recent window,
this service currently keeps sending new requests rather than failing fast and giving the backend
room to recover; the single-retry cap above bounds how much *each* request compounds the problem,
but nothing yet bounds the *aggregate* request rate during a sustained backend outage. Tracked in
`evals/COVERAGE.md`'s Known Scenario Gaps.

**Anthropic-side reliance: still the SDK's own default retry behavior, confirmed 2026-09-19** (this
note did not previously exist in this document -- added now, not merely confirmed, since Step 6 of
the spec that prompted this section assumed it already did). No application-level request
queuing/throttling exists in front of the Anthropic API today; this is a named future item for the
1,000-users cost tier (`COST_ANALYSIS.md` -- that document has no literal "300-user" tier; its
tiers are 100/1,000/10,000/100,000 users, and 1,000 is the nearest one whose architecture-change
list this reliance belongs under), not built now.

## 4.2 Partial-failure disclosure enforcement (2026-09-20)

**Closes a real inconsistency, identified investigating `get_recent_encounters`' sensitivity-lookup
401 (`evals/COVERAGE.md`'s corrected entry, `2026-09-20`).** That investigation's actual root cause
turned out to be a missing OAuth scope, not a platform bug -- but investigating it surfaced a
separate, more general problem worth naming and fixing on its own: `partial_failures`
(`app/schemas.py` -- `GetPatientSnapshotOutput`, `GetRecentEncountersOutput`,
`GetRecentObservationsOutput`, `CompareSignoutToChartOutput`) had **no code-level enforcement at
all**, unlike the two structural checks Section 4's table already names above it. Whether a resident
learned that a tool's own sub-fetch had failed -- e.g. the sensitivity lookup inside
`get_recent_encounters` -- depended entirely on the model choosing to surface it from the raw tool
JSON under `SYSTEM_PROMPT`'s generic "say so plainly" instruction. That is exactly the
compliance-dependent, no-backstop shape `check_allergy_conflict` (3.2) and
`_enforce_duplicate_and_empty_chart` (Section 4's table, above) exist specifically to avoid for
anything safety-relevant -- a prompt instruction can be forgotten or reasoned around; before this
fix, a `partial_failures` disclosure was exactly that kind of instruction and nothing more.

**Fix:** `app/verification.py`'s `_enforce_partial_failures`, wired into `verify_response()`
alongside `_enforce_duplicate_and_empty_chart`, in the same style -- scans every tool result from
the turn's `ToolCallRecord`s for a non-empty `partial_failures` list, and if the drafted response
doesn't already disclose it (checked the same way duplicate/empty-chart disclosure already is: cue
phrases plus the failure's own specific text, not a bare "failed"/"couldn't" match broad enough to
collide with an unrelated clinical hedge), force-appends a note naming the specific failure --
never a generic "something went wrong." A response that already disclosed the failure in its own
words is left alone, same "genuine self-correction is respected" guarantee the duplicate/empty-chart
checks already give.

Regression-tested in `evals/unit_tests.py` (`test_partial_failure_force_disclosed_when_not_mentioned`,
`test_partial_failure_not_double_injected_when_already_mentioned`), matching the existing pattern for
the two checks it sits alongside.

# 5. Observability

Every invocation is assigned a correlation ID at the moment the resident sends a message, carried
through every tool call, every LLM call, and every verification check tied to that request, so a
full trace is reconstructable from logs alone (per the engineering requirements). Minimum tracked
per invocation: total latency, per-tool-call latency, tool failure/retry counts, token usage and
cost, and verification pass/fail outcome for both layers in Section 3. This is the same discipline
already applied throughout the audit itself, every finding there states which method produced it;
the agent's own logs should make the same kind of statement possible about every response it gives.

# 6. Known tradeoffs

- **Latency vs. direct DB access.** The FHIR API is slower than direct SQL, and assembling a full
  patient picture (Section 2's tools) means multiple sequential API calls. The audit already
  identified OpenEMR's `$export` bulk-data operation as an available option if this becomes a
  binding constraint; the fix for latency is caching or bulk export, not dropping to direct SQL,
  since that would reintroduce exactly the authorization and audit-trail gaps documented in
  `AUDIT.md`.
- **OAuth2 setup is real friction** compared to a database connection string, but this is accepted
  as the cost of the only integration path that is both centrally authorized and fully audited.
- **The sensitivity compensating control (3.3) is a deliberate duplication of platform logic.**
  If OpenEMR's own filtering is later confirmed to work correctly for user-scoped tokens, this
  control becomes redundant rather than harmful, the safer failure direction.
- **Measured load-test latency and the configured alert threshold are different numbers doing
  different jobs, 2026-09-18 (documentation update, not a new measurement):** `PERFORMANCE_BASELINE.md`'s
  droplet load test measured p95 ≈ 37.0s at 10 concurrent residents (post-resize, all three
  independent sources agreeing within a few seconds). §7.7's alert threshold is 30,000ms / 30s. These
  are not interchangeable -- the 37s figure is a real, observed value from a specific concurrent-load
  test scenario; the 30s figure is a threshold chosen from single-user, uncontended baseline traffic
  (§7.7's "Where the p95 thresholds came from"), deliberately set to catch a genuine regression
  without firing on ordinary variance. That the load test's measured figure now exceeds the
  configured threshold is an honest, expected finding under concurrency, not a contradiction or a
  sign the threshold was set wrong for its actual purpose.

  The fix for this gap is not one thing. Four scaling levers exist because they solve four
  different problems, not the same one:
  - **(a) Separating the Co-Pilot service onto its own infrastructure, away from OpenEMR**
    (`COST_ANALYSIS.md`'s 1,000-user tier) -- fixes **resource contention**: the audit found the
    shared droplet already running near capacity with just the base OpenEMR stack, so concurrent
    agent traffic risks starving OpenEMR itself, independent of the agent's own latency.
  - **(b) Horizontal scaling behind a load balancer** (`COST_ANALYSIS.md`'s 10,000-user tier) --
    fixes **concurrency**: multiple service instances handling more simultaneous residents than one
    process can, independent of how fast any single request completes.
  - **(c) A managed database for Langfuse**, not a single self-hosted container -- protects
    **observability accuracy, not clinical-data speed**. This is not a hypothetical: the exact
    failure mode already happened (`PERFORMANCE_BASELINE.md`'s droplet CPU-contention incident,
    `ERROR_ANALYSIS.md` Entry 6) -- Langfuse's own span ingestion silently undercounted (5/20 spans)
    under load, meaning the p95-latency Monitor read `severity: OK` through a real 154-second
    resident wait. A managed DB is about trusting what the dashboard says, not making the agent
    faster.
  - **(d) FHIR caching / bulk-export** -- the actual fix for the measured latency bottleneck itself.
    `PERFORMANCE_BASELINE.md` Stage 2 confirms directly: OpenEMR's own FHIR backend, not the agent
    service, is consistently the most CPU-loaded container under load (116%→156%), while the
    agent's own process footprint stays trivial (peak 9.1% CPU) because it's I/O-bound waiting on
    FHIR round-trips. Levers (a)-(c) fix contention, concurrency capacity, and observability
    trustworthiness respectively; only (d) addresses why any individual request is slow. Compressing
    these into a single "we need a database" statement would misdescribe which problem each lever
    actually solves.
- **An LLM provider abstraction exists (`app/llm_client.py`, 2026-09-19), but Anthropic Claude is
  the only validated path.** `ClinicalCopilotAgent` no longer constructs `anthropic.Anthropic(...)`
  directly -- it calls through an `LLMClient` interface (`create_message`, `append_assistant_turn`,
  `append_tool_results`, `check_ready`), resolved by `build_llm_client(settings)` from
  `Settings.llm_provider` (`LLM_PROVIDER` env var, defaulting to `"anthropic"`). The reason for the
  seam is provider portability for a public release -- data residency, cost at scale, and vendor
  lock-in are all real reasons a deployer might want a self-hosted, open-source model instead of a
  hosted API -- not a belief that any other model is equivalent to Claude today. It is not: every
  safety behavior this document describes (Section 3's verification layer, `THREAT_MODEL.md`'s
  prompt-injection resistance, the Golden Set/Behavioral Coverage eval suites) was built and tested
  against Claude specifically, and nothing about API-level tool-calling compatibility with another
  model implies those same behaviors hold for it.

  `AnthropicLLMClient` is today's logic moved behind the interface verbatim, not rewritten --
  confirmed behavior-preserving by re-running the full eval suite after the refactor with
  `LLM_PROVIDER` unset (see below). `OpenAICompatibleLLMClient` (the documented extension point for
  Ollama/vLLM/LM Studio-style OpenAI-compatible self-hosted servers) is a stub: every method raises
  `NotImplementedError` unconditionally, by design -- `build_llm_client` resolves
  `LLM_PROVIDER=openai_compatible` to a real instance of this class (so the factory seam itself is
  testable end to end), but calling any of its methods refuses immediately, so it is not possible to
  accidentally run this project against real patient data through an unimplemented, unvalidated
  path. Real structural differences between the two providers' tool-calling wire formats (message
  history shape, tool-argument encoding, system-prompt delivery) were investigated and are not
  assumed away by this interface; a real `OpenAICompatibleLLMClient` implementation is future work,
  not attempted here.

  **Hard requirement, stated here and in `README.md`'s Model provider section, not duplicated in
  full a third time: switching `LLM_PROVIDER` away from `anthropic` requires re-running
  `evals/unit_tests.py` and `evals/cases.py` against that specific model and matching the pass
  rates documented for Claude (currently 32/32 and 18/18, 100%/100% gate) before it can be
  considered safe for any clinical-adjacent use.** A model that calls tools in an
  API-compatible shape is not the same claim as a model that respects this project's grounding,
  domain-constraint, and prompt-injection guarantees -- only the eval suite proves the latter, and
  it has only ever been run against Claude.
- **`get_recent_encounters` requests `user/Encounter.read` (a clinician-context OAuth scope), not
  `patient/Encounter.read` (a SMART patient-launch-context scope) -- a real, deferred architectural
  gap, not an oversight.** Found investigating a live 403 (`clinical-copilot/evals/COVERAGE.md`'s
  `get_recent_encounters` entry, 2026-09-20): OpenEMR's FHIR `GET /fhir/Encounter` route
  (`apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:297`) only skips its
  `encounters:auth_a` ("Authorize - any encounters," a practice-wide gacl right) ACL check when
  the request is recognized as patient-scoped (`HttpRestRequest::isPatientRequest()`), which itself
  is only ever true when the token's scope for `Encounter` is `patient/Encounter.read`
  (`HttpRestRouteHandler.php:65`, keyed off `getScopeContextForResource()`). Because this project
  requests the coarser `user/Encounter.read` scope, every account hits the `encounters:auth_a`
  check regardless of whether it's actually reading one bound patient's chart or not -- a
  clinician-scoped tool that only ever needs one patient's encounters is forced through a
  practice-wide authorization gate it doesn't conceptually need.

  Fixed for tonight (`COVERAGE.md`) by granting `encounters:auth_a` to the `clin` (Clinicians) ACL
  group directly -- a real, verified, narrower-than-`doc` (Physicians) fix, not a placeholder. But
  the **architecturally preferred fix, deferred, not forgotten:** request `patient/Encounter.read`
  scoped to the active `patient_id` instead, so `isPatientRequest()` is true and the
  `encounters:auth_a` check is skipped entirely -- no ACL grant needed at all, because the request
  is structurally provable as bounded to one patient rather than relying on a practice-wide
  permission a narrowly-scoped clinical account has to be separately granted. This is a bigger
  change than the ACL grant: it touches `app/config.py`'s `OPENEMR_OAUTH_SCOPE` default and how
  `get_recent_encounters` (and, for consistency, every other per-patient FHIR tool call) issues its
  request, not just an OpenEMR-side config edit. Not implemented tonight.

### Cost controls

Three independent layers guard against runaway LLM spend, at different levels of the stack:

1. **Account-level hard cap** -- a monthly spend limit set in the Anthropic Console,
   with alerts at 75%, as a last-resort backstop. Coarse (pauses all API access
   org-wide when hit) but unconditional.
2. **Application-level rate limiting** -- not yet implemented. No per-IP or per-session
   request throttling exists anywhere in `app/main.py` or elsewhere in the service today
   (confirmed by reading the code: no rate-limiting library in `requirements.txt`, no
   throttling middleware -- the only registered middleware is `request_timing_middleware`,
   which logs latency, not a limiter).
3. **Access gating on the deployed instance** -- `/chat` requires a real, authenticated
   OpenEMR resident session (Phase 1 `authorization_code` login via `/login`/`/callback`,
   `app/main.py`/`app/oauth_session.py`); no anonymous access, no shared fallback
   credential. This gates *identity*, not *volume* -- a legitimate authenticated session
   has no call-rate ceiling.

These are complementary, not redundant: (2) and (3) prevent the account cap in (1) from
ever being tested under normal operation; (1) exists in case they fail or are bypassed.

**Honest caveat on that framing, given (2)'s actual status:** with no rate limiting yet
built, (3) alone does not bound how much a single legitimate, authenticated session could
spend -- it restricts *who* can call `/chat`, not *how often*. Today, (1) is not purely a
backstop for failure/bypass of (2); it is the only volume-independent control in the
stack. Closing (2) is real, concrete future work, not a hypothetical hardening step.

# 7. Engineering requirements

Each item below is a concrete decision, not a placeholder, chosen to fit inside the program's
approved-vendor policy (Cursor, Anthropic, OpenAI, OpenRouter, Railway, Google One, DigitalOcean,
Vultr, ElevenLabs, Midjourney) without introducing a new vendor dependency where a free or
already-covered option does the job just as well.

## 7.1 Test design: boundaries, invariants, regression

Built directly on the audit's existing fabricated test patients (`audit-notes.md`, pid 1-6) rather
than new fixtures, since they already encode the exact edge cases this requirement asks for:

- **Boundary cases:** pid 3 (empty chart, no problems/meds/allergies at all), pid 6 (duplicate of
  pid 1 with a conflicting medication dose), a malformed/missing patient ID passed to a tool.
- **Invariant:** every response must carry only source-attributed claims (`ARCHITECTURE.md` §3.1)
  — a standing test asserts this holds across every eval case, not just the ones designed to test
  it directly.
- **Regression:** each finding in `AUDIT.md` that the agent's design specifically guards against
  (the sensitivity gap, Finding 12; the empty-chart-with-no-warning gap) becomes a permanent
  regression test, so a future change can't silently reintroduce a gap the audit already found.

Each test case documents which of the three categories it belongs to and which specific failure
mode it guards against, per the PRD's requirement that this be explicit, not implicit.

## 7.2 Correlation IDs

Already specified in Section 5: one correlation ID generated per resident message, propagated
through every tool call, LLM call, and verification check tied to that request. No additional
tooling required, this is implemented directly in the service's own logging.

## 7.3 Schema contracts

Every tool in Section 2's table gets a Pydantic model defining its exact input and output shape,
treated as the source of truth the implementation must match, not documentation written after the
fact. A tool that returns something outside its declared schema fails loudly (and is logged as a
tool failure per Section 4) rather than passing malformed data to the model silently.

## 7.4 Dashboards

**Self-hosted Langfuse, running as an additional container on the existing DigitalOcean droplet.**
Neither Langfuse nor Braintrust appear on the program's approved-vendor list, but Langfuse's
open-source (MIT) self-hosted deployment requires no purchase and no new vendor relationship at
all, it is software running on infrastructure already approved and already paying for. Braintrust
has no equivalent self-host path once usage exceeds its free tier, which would force a vendor
question this project doesn't need to raise. Tracks, at minimum: request count, error count,
p50/p95 latency, tool call counts, retry counts, and verification pass/fail rate (the North Star
metric from `KEY_METRICS.md`), satisfying the dashboard requirement with a single tool.
**Correction, 2026-09-20:** retry counts were previously listed here as dropped, on the claim that
"no retry logic exists anywhere in this system" -- that claim was already inaccurate when written,
since `FhirClient._get_with_retry`'s single-retry logic (§4.1) has existed since 2026-09-19; what
was actually missing was *measurement*, not the mechanism itself. Closed tonight:
`FhirClient.get_and_reset_retry_count()` (`app/fhir_client.py`) is read around each tool call in
`app/agent.py`'s `run_turn` and scored per call as `tool_call_retry_count`
(`TurnObserver.finish_tool_call`, `app/observability.py`) -- see the status note immediately below,
updated to match.

### Dashboard requirement status (confirmed 2026-09-16, not assumed)

**Confirmed live and covered, on both instances.** Langfuse ships a built-in, non-custom
`Langfuse Agent Dashboard` (owner: `LANGFUSE`, a stock template, not something this project built)
-- confirmed present via its API on both the local and droplet instances. Its 8 widgets: Total Tool
Calls, Total Tool Calls (over time), Top 20 Called Tools, Observations by Type, P95 Tool Latency by
Tool, P95 Tool Latency by Tool (over time), P95 Latency by Observation Type, and Tool Errors by
Tool. Combined with the three verification scores (`verification_pass_rate`,
`source_attribution_pass`, `domain_constraint_pass`) confirmed actually logged on every real trace
-- 682/681/681 scored entries on local, 218/218/218 on the droplet, queried directly from each
instance's own ClickHouse rather than assumed -- this covers the PRD's required minimum floor:
request count, error count, p50/p95 latency, tool call counts, and verification pass/fail rate.

**Queue depth: not applicable.** The current architecture is a single FastAPI process handling each
`/chat` request synchronously and directly (§1.2's single-agent design) -- there is no request
queue anywhere in front of it, so there is no "depth" for any metric to measure. This would become a
real, meaningful metric if the system moves to a queued/async request-handling model at higher
scale -- `COST_ANALYSIS.md`'s 10,000-user tier already names horizontal scaling of the service
behind a load balancer as a required architecture change at that volume, and a queue is a natural
component of that kind of redesign, though the cost doc doesn't name "queueing" explicitly itself.
This is future-architecture territory, not a gap in the current single-process design.

**Event retries: now tracked, corrected 2026-09-20.** This status previously read "not applicable,"
on the claim that no retry logic exists anywhere in `app/` -- that was wrong even at the time it was
written: `FhirClient._get_with_retry` (§4.1) has applied a single retry, on a timeout or a
5xx-class response, to every real FHIR GET since 2026-09-19. What was actually true is narrower, and
still holds: no retry logic exists around tool-call dispatch itself, or around LLM calls
(`app/agent.py` never overrides `max_retries` on the Anthropic client, so LLM calls rely only on the
SDK's own default transport-level retry -- vendor behavior this project hasn't disabled, not
something it built). The FHIR-level retry is now measured, not just present: `FhirClient.
get_and_reset_retry_count()` is read around each tool call and scored per call as
`tool_call_retry_count` (`TurnObserver.finish_tool_call`, `app/observability.py`) -- verified live
against the real local Langfuse/ClickHouse instance before this doc was updated to match. Failures
that exhaust the single retry still surface directly to the resident rather than retrying further,
matching §4's failure-mode table exactly (`state the gap directly... never answer as if data was
retrieved when it wasn't`) -- a deliberate design choice, not an oversight.

**What actually matters most here.** The PRD's dashboard requirement is a stated minimum floor, not
a ceiling -- it explicitly expects a team to add whatever metrics matter for their specific agent
design. For this design, that metric is `verification_pass_rate`: the one named in `KEY_METRICS.md`
as the North Star specifically because it is the most direct measurement of this project's actual
premise (a confidently wrong clinical answer is the failure this whole system exists to prevent).
It is also, of everything in this section, the metric that is real, live, and proven working
end-to-end multiple times over today -- the Warfarin hallucination-bait case and the stale
sign-out/duplicate-record case among them -- not just present in a dashboard widget.

## 7.5 Runnable API collection

A Bruno collection (free, open-source, no vendor relationship required) covering the agent's core
endpoints, checked into the repo so a grader can exercise every workflow without reading source
code first.

## 7.6 /health and /ready endpoints

- `/health`: returns 200 if the process is running. Minimal, standard practice.
- `/ready`: actively checks that OpenEMR's FHIR API, the Anthropic API, and the self-hosted
  Langfuse instance are all reachable, returning a per-dependency breakdown rather than a single
  yes/no, so a failure is diagnosable from the response itself.

## 7.7 Alert definitions

**Status: live, not just designed.** Self-hosted Langfuse (v4.36.1) ships a real, DB-backed
**Monitor** feature -- not an Enterprise-only add-on -- that evaluates a metric query on a rolling
window every ~1 minute and transitions a severity state (`OK` / `WARNING` / `ALERT`) when a
threshold is crossed. Confirmed directly against both running instances (not assumed): three
Monitors exist in the `clinical-copilot-dev` project on **each** of the local and droplet Langfuse
deployments (each self-hosted instance has its own independent Postgres/ClickHouse, so this is two
separate configurations, not one shared across both), each wired to a real Automation/Action so
severity changes are a genuine event, not just a number sitting in a table.

| Alert | Real threshold | Window | Plain-language meaning | Solo on-call response |
|---|---|---|---|---|
| p95 latency | `p95(latency)` on observations where `type = AGENT` (the whole `chat_turn` span) **> 20,000 ms local / > 30,000 ms droplet** | rolling 15 min | The slowest 5% of resident-facing turns are taking longer than the instance's threshold, end to end | Check `/ready` first to isolate which dependency is slow (OpenEMR FHIR, Anthropic, Langfuse), then open the specific slow trace in Langfuse to see which step -- an LLM call round, a tool call, or verification -- is eating the time |
| Error rate | `count` of observations where `level = ERROR` **> 3** | rolling 15 min | More than 3 failed operations of any kind happened recently | Look at which observations are `ERROR` in Langfuse; today this is populated exclusively by tool-call failures (see gap below), so correlate against OpenEMR/Anthropic status before assuming a code bug |
| Tool failure rate | `count` of observations where `type = TOOL AND level = ERROR` **> 3** | rolling 30 min | More than 3 tool calls (the FHIR-backed `get_patient_snapshot` / `check_allergy_conflict`) failed recently | Check whether failures cluster on one tool/input shape (e.g. repeated malformed patient IDs -- expected, not urgent) or spread across many real requests (FHIR/network-level, escalate) |

**Where the p95 thresholds came from:** `USERS.md` use case 3 states the rapid-response scenario
needs an answer "in seconds, not minutes" but names no exact number, so both thresholds were chosen
from real measured data rather than guessed -- and the two instances needed *different* numbers,
which is itself a finding worth recording. Querying every real `AGENT`-type observation recorded
directly in each instance's own ClickHouse to date:

- **Local** (677 real traces): p50 ≈ 7.95 s, p95 ≈ 12.47 s, max ≈ 18.4 s → threshold set to 20 s.
- **Droplet** (218 real traces): p50 ≈ 12.82 s, p95 ≈ 19.72 s, max ≈ 25.82 s → threshold set to 30 s.

The droplet is measurably slower across the board (consistent with its more modest resources,
the same pattern already documented for the `/ready` OpenEMR check's own dedicated timeout above);
using the local threshold on the droplet would have put its real p95 within striking distance of
"alert," which is not a meaningful signal. Each threshold sits comfortably above that instance's own
observed max so it does not fire on ordinary variance, while still catching a genuine regression
(e.g. the droplet resource-contention incident already logged in `COVERAGE.md`). It is also an
honest admission that the *current* system does not yet hit an idealized "few seconds" bound for its
tightest use case -- the multi-round tool-calling + verification design (Section 3) has a real
latency floor around 8-13 s median today, worse on the droplet; closing that gap is future work, not
something to paper over here.

**Why error rate and tool failure rate will show identical numbers right now:** confirmed by reading
`app/observability.py` directly -- `finish_tool_call()` is the *only* place that sets
`level="ERROR"` on a Langfuse observation. LLM generation spans (`start_llm_call` /
`finish_llm_call`) never get marked `ERROR` even if the underlying Anthropic call fails, and there is
no span-level error marking for an unhandled exception inside `/chat` itself. So today, every
`ERROR`-level observation is a tool failure, and the two alerts are deliberately scoped differently
(all types vs. `type = TOOL` only, plus different windows) so they *will* diverge once other error
sources get instrumented -- not invented to look distinct before they actually are.

**Notification channel, and its one real limitation:** each Monitor is linked to a Langfuse
Automation whose Action is type `WEBHOOK` (self-hosted Langfuse's alerting supports Webhook, Slack,
or GitHub Dispatch -- no native "email me" option; Slack would need a connected workspace not set up
for this solo project). Self-hosted Langfuse hard-codes its webhook validator to only allow target
port 80 or 443 (SSRF hardening, not configurable via env), and this project's own `/chat` service
runs on 8420 with no port-80/443 listener in front of it, so the configured webhook target
(`https://example.com/clinical-copilot-alerts`) is a placeholder that satisfies Langfuse's
"a Monitor needs at least one Automation" requirement but does not actually deliver anywhere. Per
this task's own explicit allowance for a solo-operator deployment, the real, live, checkable
notification target is **the Monitor's own state** -- `severity` and `alertedAt`, visible in
Langfuse's Alerts UI (`/project/clinical-copilot-dev/alerts`) and queryable via its API -- not the
webhook payload. Wiring real webhook delivery (a small reverse proxy on 80/443, or rebinding the
service) is a near-term next step, not done here, noted honestly rather than silently claimed.

**Proof it actually fires (2026-09-16, local instance), not just configured:**
- **Tool failure rate** and **Error rate** fired for real, with no threshold trick: 5 real
  malformed-patient-ID requests were sent to the live local `/chat` endpoint back to back. The
  scheduler's next tick (≤1 minute later, per Langfuse's own cadence for sub-day windows) picked up
  the 5 real `ERROR`-level tool observations and flipped both Monitors from `OK` to `ALERT`
  (`tool failure rate` at `22:12:08Z`, `error rate` at `22:12:38Z`), with `alertedAt` populated.
- **p95 latency** was proven by the threshold-lowering method instead (real traffic wasn't slow
  enough to trip 20,000 ms honestly): temporarily set `alertThreshold` to 100 ms via the Monitor's
  own update API, confirmed `severity` flip to `ALERT` within one scheduler tick, then restored
  `alertThreshold` to 20,000 and confirmed `severity` returned to `OK` on the following tick --
  full fire-then-recover cycle observed, not asserted.
- The two failure-driven alerts (`tool failure rate`, `error rate`) are expected to self-clear back
  to `OK` on their own once the 5 manufactured failures age out of their rolling windows (30 min and
  15 min respectively from `22:12Z`) -- no manual reset needed, which is itself a confirmation the
  rolling-window logic works as designed.

The same three Monitors were separately created on the **droplet** instance (its own Langfuse, its
own trigger IDs, its own 30,000 ms latency threshold per its own measured baseline above) and
confirmed evaluating against real droplet ClickHouse data within one scheduler tick of creation
(`severity: OK`, real `lastCompletedAt` timestamps). The fire-then-recover mechanism itself was
proven once, thoroughly, on local; it is the same Langfuse code path on both instances, so it was
not separately re-triggered on the droplet to avoid manufacturing needless load there.

## 7.8 Baseline CPU/memory/latency/throughput

Captured once the load tests below are run, using the same self-hosted Langfuse dashboard plus
`docker stats` on the droplet (already used during the audit's performance pass), so this reuses
tooling already in place rather than adding another.

## 7.9 Load/stress tests

**Locust** (free, Python-based, no vendor relationship required) simulating 10 and 50 concurrent
residents each running a mix of the four `USERS.md` use cases, not a single repeated request, since
a realistic mix is what actually exercises the tool-calling and verification paths under load.
p50/p95/p99 latency and error rate are recorded at each level and become the baseline in 7.8.

## 7.10 API key management

**Policy actually followed throughout this project, not a new one written now: every credential
lives only in a git-ignored `.env`-family file, never committed, never hardcoded in source, never
pasted into chat.** Confirmed directly, not assumed: `git ls-files` returns zero `.env` files
tracked anywhere in this repo, and `git log --all --diff-filter=A` across the entire history finds
no `.env` file ever added, at any commit. The root `.gitignore` excludes `.env`;
`clinical-copilot/.gitignore` separately excludes both `clinical-copilot/.env` and
`clinical-copilot/docker/.env.langfuse` (the self-hosted Langfuse stack's own secrets, a second
credential file this project maintains, not just the app's own). `.env.example` is the only
credential-shaped file actually committed, and it carries structural placeholders only (blank
values, or `admin`/`pass` for the disposable local-dev-only account) -- never a value pulled from a
real `.env`.

**Every real secret this project has generated tonight, and every night before it, followed the
same pattern:** the Anthropic API key (`ANTHROPIC_API_KEY`), the OpenEMR OAuth client secret
(`OPENEMR_OAUTH_CLIENT_SECRET`), the password-grant service credential
(`OPENEMR_OAUTH_PASSWORD`), the Phase 1 scope-restriction test credential
(`OPENEMR_SCOPE_TEST_USERNAME`/`OPENEMR_SCOPE_TEST_PASSWORD`, §1.3), Langfuse's own API keypair
(`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`), and the self-hosted Langfuse stack's internal
secrets (`ENCRYPTION_KEY`, `NEXTAUTH_SECRET`, database/object-storage passwords, all in
`docker/.env.langfuse`, `docker-compose.langfuse.yml`'s own `:?set ... in .env.langfuse` guards
refusing to start without them) -- every one of these lives only in its respective `.env` file on
the machine/droplet that needs it, referenced by name in code and compose files, never printed to
a terminal transcript or committed. The narrow-scope demo/test accounts created directly in
OpenEMR's own UI tonight (`copilot_resident_1`, `clin_1`, `test_clinician`) follow the identical
discipline: a real, high-entropy password, stored only where the credential needs to be read from
(`.env` for `OPENEMR_OAUTH_USERNAME`/`PASSWORD`-shaped values the app itself authenticates with),
never hardcoded.

**One deliberate, disclosed exception, not a violation of the policy above:** `test_clinician`'s login
password *is* committed, in plaintext, in `README.md` (root, droplet-facing) and
`clinical-copilot/README.md`. This is not an oversight -- `test_clinician` is a purpose-built,
narrow-scope (`Clinicians`, Provider off) demo account whose entire reason to exist is being handed
to a grader who has no other way to reach the live droplet (§4.5,
`clinical-copilot/THREAT_MODEL.md`); publishing it is the correct behavior for that specific
credential's job, categorically different from the application secrets above, none of which any
external party is meant to hold. Keeping this distinction explicit matters: it means "some
credential is committed in this repo" is true but does not contradict "secrets are never
committed" -- the two categories are handled according to what each one is actually for.

**Real, disclosed gap, not silently implied away: there is no secrets-rotation policy and no
vault/secrets-manager integration anywhere in this project.** Every credential above is a
long-lived, manually-set value in a flat file; nothing expires it, nothing rotates it on a
schedule, and nothing (HashiCorp Vault, AWS/GCP Secrets Manager, 1Password Connect, or similar)
manages, audits, or injects any of them at runtime. `test_clinician`'s rotation this week (from the
previously-published `admin`/`pass`, §4.5) was a manual, one-off response to a specific finding,
not evidence of an ongoing rotation cadence -- the next credential to leak or go stale has no
process forcing its replacement. This is an accepted, named tradeoff for a solo-operator project at
current scale (the same "approved-vendor policy, no new vendor dependency" reasoning as §7.4's
Langfuse choice), not a gap to carry unexamined into a real multi-resident deployment -- it belongs
alongside `COST_ANALYSIS.md`'s 1,000-user tier's other required architecture changes, not deferred
past it.

## 7.11 Audit logging requirements

**This system does not maintain its own application-level audit log, distinct from OpenEMR's.**
Every action a resident takes through the Co-Pilot is only as auditable as two things, combined,
and no more: **(a)** whatever OpenEMR's own built-in audit log captures for the API calls this
app makes on the resident's behalf, and **(b)** whatever this app's own Langfuse tracing captures
about the agent's own reasoning (correlation IDs, tool calls, verification outcomes) — a record of
*this service's internal behavior*, not an independent HIPAA-grade access log of *who touched
which patient's PHI*.

**(a) OpenEMR's own audit log — what it actually covers for this integration path, confirmed by
direct investigation, not assumed:**
- Used reactively tonight, for real: investigating the undocumented `reviewer` account on the
  droplet (`THREAT_MODEL.md` §4.5a) queried `users.date_created` (creation timestamp: 2026-09-17
  01:58:15), the `gacl_*` tables (ACL group: `doc`/Physicians), and the `log` table (login history:
  exactly 2 successful logins, both 2026-09-17, with source IPs) directly — 5,792 audit-log rows
  generated across those two sessions. This is a real, working example of the log answering "what
  happened, from where, when," not a hypothetical capability.
- `AUDIT.md` §1 (the underlying security audit this whole project is built on) found OpenEMR's
  audit logging genuinely substantial: broad event coverage (`enable_auditlog=1`, patient-record,
  query, security-administration, and more), and **API access specifically is logged**, in a
  dedicated `api_log` table carrying method, resource, full URL, IP, and token/registration events
  — this is the exact integration surface the Co-Pilot's FHIR/REST calls use (`ARCHITECTURE.md`
  §1.3/§4.4's "data access is exclusively through OpenEMR's API, never the database directly"),
  so this logging mechanism does apply to Co-Pilot traffic, not just OpenEMR's own UI.
- **But `AUDIT.md` §1.3 found a defect that directly undermines this for the Co-Pilot's own use
  case: `api_log.patient_id = 0` on every FHIR data call, even when a specific chart was fetched
  [TESTED].** A query over `api_log` asking "which patient records did this integration touch"
  returns nothing from that column; patient identity is recoverable only by parsing a UUID out of
  `request_url` by hand, per-row, not a structured, queryable field. This is the single most
  relevant gap named in `AUDIT.md` for exactly this project's own traffic.
- `AUDIT.md` §1.2/C-1 also found the log's tamper-evidence checksum is unkeyed and defeatable by
  anyone with database write access (demonstrated live: a row was edited and the tamper report
  reported clean). This doesn't mean the log is empty or useless — it means it should not be
  treated as forensically tamper-proof evidence on this deployment as configured today.

**(b) Langfuse's tracing — a gap found while writing this section, CLOSED 2026-09-19, same day:**
every trace records `correlation_id` and `patient_id` (`app/observability.py`'s `start_turn`, both
in the local structured JSON log and the Langfuse span's `input`); when this section was first
written, **no resident/user identity field was attached to any trace at all** —
`session.resident_username` was known to `app/main.py` at request time (it powers the `/session`
endpoint's own response) but was never passed into `run_turn()`/`start_turn()`. Fixed the same day
by threading `resident_username` through `app/main.py`'s `/chat` handler ->
`ClinicalCopilotAgent.run_turn()` -> `TurnObserver.start_turn()`, attached the same way
`correlation_id` already was (a plain key in the local structured log and the Langfuse span's
`metadata`) rather than a differently-shaped one-off mechanism. **Verified against a real trace,
not just code that compiles:** a real chat turn (real Anthropic call, real local FHIR data,
resident identity extracted via the actual production `SessionStore._extract_username` logic
against a real OpenEMR password-grant token response) produced a Langfuse trace whose
`events_core` row for the root `chat_turn` observation carries `input.patient_id =
"98c4b82b-b07e-11f1-8334-022958ad0af8"` and `metadata` entries
`correlation_id = "478a7fbb-cc33-4105-a6fc-8855248087a6"` and `resident_username =
"a2bed405-1149-46b5-9c4e-2e62099dbd6f"` (the real `sub` claim for this account — no
`preferred_username` claim was present on this token, an honest artifact of the test account used,
not a bug), all three together on the one record — queried directly from the local Langfuse
instance's own ClickHouse (`events_core`; this self-hosted deployment runs in "events_only" mode,
so the `/api/public/traces` REST endpoint used elsewhere in this document isn't available here --
querying the store directly, the same fallback already used for the alert-Monitor verification
above, worked instead). **Residual scope, named rather than silently assumed complete:** this uses
the same plain-metadata-key pattern as `correlation_id`, not Langfuse's separate native
`user_id`/`propagate_attributes()` trace API -- deliberately, for consistency with every other
field this module attaches, but it means a trace isn't filterable by Langfuse's own built-in
"Users" UI feature the way a native `user_id` would be; the resident identity is real and present
on every trace, just not wired into that specific platform feature. A regression test
(`evals/unit_tests.py`'s `test_turn_observer_logs_resident_identity_alongside_patient`) asserts
`patient_id`, `resident_username`, and `correlation_id` land in the same local log line -- gating
automated tests on live Langfuse ClickHouse state would be disproportionate infrastructure for
this fix, so the Langfuse-side confirmation above is a one-time live verification, not something
re-checked on every commit.

**Assessment, stated plainly rather than left open: this combination remains a real, disclosed gap
for HIPAA-relevant audit requirements overall, even after the fix above -- narrower than it was,
not closed.** §164.312(b) requires audit controls that can answer "who accessed this patient's
ePHI, and when" reliably. As of the fix above, Langfuse's own trace record now answers the
who/which-patient/when question **for the agent's own reasoning layer** directly from one place,
no cross-referencing needed. But two problems named in (a) are untouched by this fix, because
they're platform-level, not this project's code: OpenEMR's own `api_log` still can't answer *which
patient* (`api_log.patient_id = 0` is a defect in OpenEMR itself, not something `app/
observability.py` can fix), and the tamper-evidence weakness (C-1) still means neither side's
record is provable-tamper-proof evidence on this deployment as configured -- Langfuse's own store
is an observability tool with database-level write access from this app's own containers, not a
hardened, append-only compliance log either. None of this is a reason the current architecture is
wrong -- FHIR/REST-only access (§1.3/§4.4) is still strictly more auditable than the direct-DB
alternative `AUDIT.md` §4.3 found bypasses the audit log entirely, and today's Langfuse trace is
now strictly more useful for an accounting-of-disclosures-shaped question than it was this
morning -- but closing the remaining gaps (patient-id-bearing `api_log` rows, or hardened/
append-only storage for either side's record) is real, concrete future work, not something already
handled by the combination of tools now in place.

# 8. Iteration planning

## 8.1 User feedback collection

`POST /feedback` (`clinical-copilot/app/main.py`) is the answer to this question -- built and
deployed, but never explicitly connected to it in any doc until now. Every real, graded response in
`chat.html` carries a thumbs up/down control plus an optional comment; a submission writes a
durable `feedback.jsonl` entry (auth-gated the same way as `/chat`, `resident_username` always from
the session, never the request body) and attempts a `resident_feedback` Langfuse score on that
turn's own trace, found by re-deriving the trace id from the turn's `correlation_id`
(`TurnObserver.attach_feedback_score`). Per `KEY_METRICS.md`'s own reasoning, this is deliberately
qualitative/supplementary signal, never folded into or reported alongside verification pass rate --
raw satisfaction is gameable by a confident, likeable, wrong answer, which is exactly the failure
mode this whole project exists to prevent.

## 8.2 Feature prioritization approach

The approach used throughout this project, not a plan for a future one: every capability traces to
a specific `USERS.md` use case before it gets built, never the reverse. Section 2's tool table is
the concrete evidence of this, not just a stated intention -- every one of the six tools lists the
specific use case number(s) it backs, and nothing in `app/tools.py` exists without one. The same
discipline extends past tools: Section 1.2 rejected a multi-agent framework specifically because
nothing in `USERS.md`'s four use cases requires multiple agents coordinating or running in parallel
-- more moving parts to secure, log, and verify, with no corresponding requirement to justify them,
not adopted because the machinery was available or technically interesting. The reference layer
(Section 3.4) exists because a resident reading an unfamiliar warning label mid-shift, or a
documented condition they don't recognize, is a real workflow moment surfaced by those same use
cases, not because DailyMed/MedlinePlus happened to have APIs available to integrate.

## 8.3 Long-term maintenance

Stated honestly rather than implied: there is no formal maintenance plan beyond this program's own
required deliverables (Early Submission, Technical Interview, Final Submission). This is a one-week
sprint project, and pretending otherwise here would misrepresent its actual status. The real,
concrete next steps are the specific gaps already disclosed and named elsewhere in this
documentation, not a hypothetical roadmap:

- **`THREAT_MODEL.md` §4.10** -- OAuth `state` is not bound to the browser that started the login
  (login CSRF / session fixation risk). Identified, not yet investigated.
- **`THREAT_MODEL.md` §4.13** -- full PHP stack traces (file paths, internal class names, framework
  versions) returned directly in OpenEMR's own 500 response bodies to an unauthenticated caller.
  Flagged, not fixed.
- **Application-level rate limiting** (Section 6, "Cost controls") -- no per-IP or per-session
  request throttling exists anywhere in this service today. Not yet implemented.
- **Secrets rotation** (Section 7.10) -- every credential is a long-lived, manually-set value in a
  flat file; nothing expires or rotates it on a schedule, and no vault/secrets-manager integration
  exists.

Named here, together, specifically so "what's next" has one concrete answer pointing at real,
already-scoped work, not a vague commitment to keep iterating.
