# Clinical Co-Pilot — Early Submission Build

Build the minimal working version of the Clinical Co-Pilot described in ARCHITECTURE.md. This is
for Early Submission (deadline: tomorrow night), so scope is deliberately narrow: prove the four
required pieces work end-to-end (agentic chatbot, verification, observability, evals), not the
full production design. Full engineering requirements (load testing, alerts, /health /ready,
baselines) come later before Final Submission — skip them for now unless trivial.

## Scope for this build

**Language/framework:** Python, calling the Anthropic API directly with tool-calling (function
calling). No LangGraph/CrewAI/other agent framework — see ARCHITECTURE.md §1.2 for why.

**Two tools only, to start** (both map to USERS.md use cases):
1. `get_patient_snapshot(patient_id)` — calls OpenEMR's FHIR API for Patient, Condition,
   AllergyIntolerance, MedicationRequest. Covers USERS.md use case 1 (rapid orientation).
2. `check_allergy_conflict(patient_id, medication_name)` — calls AllergyIntolerance and checks for
   a conflict against a supplied medication. Covers use case 2/3 (backs the domain-constraint
   verification layer).

Do not build the other four tools from ARCHITECTURE.md's table yet (`get_recent_encounters`,
`get_recent_observations`, `compare_signout_to_chart`, `summarize_shift_events`) — those come after
Early Submission if time allows.

**Auth:** use a `user/`-scoped OAuth2 token against the deployed OpenEMR instance
(http://<deployed-host>:8300) for now — reuse the OAuth2 client registration process already proven
working during the audit's FHIR verification (see architecture-audit.md §6.1 for the exact flow
and the gotcha about the `aud` claim matching `site_addr_oath`).

**Verification (simplified for this pass, per ARCHITECTURE.md §3):**
- Source attribution: after the model drafts a response, do a simple check that any patient-specific
  fact mentioned (a medication name, an allergy, a condition) actually appears in that turn's tool
  call results. If it doesn't match, strip it and log a verification failure — don't need the full
  tagging system yet, a basic substring/entity match against tool outputs is enough for this pass.
- Domain constraint: `check_allergy_conflict` runs as a hard code check, not a prompt instruction,
  any time the agent's draft response mentions a medication for that patient.

**Observability:** wire in self-hosted Langfuse (per ARCHITECTURE.md §7.4) as a container alongside
the existing OpenEMR stack on the DigitalOcean droplet. Log, at minimum: a correlation ID per
conversation turn, each tool call and its latency, token usage, and the verification pass/fail
outcome for that turn (this is the North Star metric from KEY_METRICS.md — make sure it's actually
being recorded, not just latency/cost).

**Evals:** write 5-8 eval cases using the audit's existing fabricated test patients (pid 1-6, see
audit-notes.md). Include at least: pid 1 (normal patient, verify a correct response), pid 3 (empty
chart, verify the agent says so explicitly rather than hallucinating), pid 6 (duplicate of pid 1
with a conflicting medication dose, verify the agent doesn't silently pick one), and one case that
deliberately tries to get the agent to state an unverifiable claim (verify it gets stripped/flagged
by the verification layer, not just that the eval "passes").

**Interface:** simplest possible for now — a basic HTTP endpoint (`POST /chat`) that takes a message
and patient context, returns a response. A real UI/module inside OpenEMR's chart (ARCHITECTURE.md
§1.1) is not required for Early Submission — note in the demo video that this is planned but not
yet built, rather than building it under time pressure tonight.

## What "done" looks like for tonight

- The two tools successfully pull real data from the deployed OpenEMR instance via FHIR
- A conversation can happen: ask about a patient, get a response, ask a follow-up
- At least one deliberately-bad response gets caught and stripped by verification (prove this in
  the demo, don't just assert it)
- Langfuse dashboard shows real traces with the verification pass/fail field populated
- The 5-8 eval cases run and produce a pass/fail result, checked into the repo as part of the eval
  suite deliverable

Do not polish beyond this. A working narrow version with honest gaps documented is worth more right
now than a broader but shakier build.
