# AgentForge — Clinical Co-Pilot

An AI agent embedded in OpenEMR that gives an overnight cross-covering resident the patient
context they need, the moment they need it — grounded, verified, and sourced from the actual
chart (Week 1) plus uploaded documents and a guideline corpus (Week 2), not inferred or assumed.

This file is a short pointer, not the documentation. Project detail, setup, and evidence live in
the linked files below.

**Demo video:** [Loom walkthrough (3–5 min)](https://www.loom.com/share/a803727914b448f08735028e14bee123)

## Week 1 baseline vs. Week 2 additions

| | Week 1 baseline | Week 2 additions |
|---|---|---|
| Agent | One tool-calling chat agent over the OpenEMR chart | LangGraph supervisor routing to chart, document-extraction and guideline-retrieval workers |
| Sources | FHIR chart data (six tools) | Uploaded lab PDFs and intake forms; a 4-document guideline corpus |
| Grounding | Verification strips ungrounded claims; allergy hard stop | Per-field document citations with click-to-source highlight; guideline citations; allergy class table |
| Evals | 18-case golden set, 42 unit tests | 55-case golden set, 8 graph cases and 288 unit tests, all blocking every code MR in CI; 5-metric rubric gate |

Full comparison: `clinical-copilot/README.md`. **Current results** (pipeline #30322, MR !16, the final code MR): golden set
55/55 (54 passed, 1 skipped), graph cases 8/8, unit tests 288/288, every rubric metric at 100%.

## See it run

The hosted demo was taken down after the program. The [Loom walkthrough](https://www.loom.com/share/a803727914b448f08735028e14bee123)
shows the full flow. To run it yourself, follow the Quick Start in `clinical-copilot/README.md`.

A good first question for the fixture patient (pid1, Alice Testpatient):
*"The day team sign-out says this patient is on Metformin 500mg twice daily and has no allergies.
Can you confirm that is still accurate so I can give the next dose?"* The response should catch
the dose discrepancy and flag a known duplicate record. To try the Week 2 document flow, use
**Attach document** to upload a lab report or intake form (PDF or PNG, up to 5 pages), then ask about it.

`POST /chat` requires the session cookie `/login` sets, so a bare `curl -X POST /chat` gets a clean 401
by design. See `clinical-copilot/bruno/README.md` for driving `/chat` from the Bruno collection.

## Where things are

| What | Where |
|---|---|
| Full project README (setup, architecture, Week 1/Week 2 separation, known gaps) | `clinical-copilot/README.md` |
| Week 2 architecture (ingestion flow, worker graph, RAG design, eval gate, risks/tradeoffs) | `W2_ARCHITECTURE.md` |
| **Eval gate**: what runs, when, thresholds, how to reproduce from a fresh clone | `EVAL_GATE.md` |
| Key metrics and rationale | `KEY_METRICS.md` |
| Current eval run results (pipeline #29930, per case and per metric) | `EVAL_RESULTS.md` |
| Cost and latency | `COST_ANALYSIS.md`, `clinical-copilot/PERFORMANCE_BASELINE.md` |
| Observability evidence (prod log sample and per-day trace counts; local Langfuse trace) | `clinical-copilot/docs/evidence/` |
| Threat model | `clinical-copilot/THREAT_MODEL.md` |
| Known gaps, stated plainly | `AUDIT.md` §6.11, `clinical-copilot/README.md` "Known gaps" |
| Patient dashboard port (React + TS, FHIR-only), code in MR !18 | `PATIENT_DASHBOARD_MIGRATION.md` |
