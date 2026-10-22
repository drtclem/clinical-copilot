# Guideline corpus (Checklist item 2)

Small, curated, scoped directly to `USERS.md`'s four target use cases -- not
a general medical knowledge base. Each `.md` file here is one `source_id`
(the filename stem) for `evidence_retriever`'s citations
(`{source_type: "guideline", source_id, page_or_section, field_or_chunk_id,
quote_or_value}`, PRD Section 4.4). `app/retrieval/chunking.py` (Checklist
item 3) splits each file with simple fixed-size, modest-overlap chunking --
deliberately not semantic/section-aware (PRD Section 4.3, Section 6's
explicit non-goal) -- so the `##` headings below exist for human
readability and citation-section labeling, not as chunk boundaries the
retriever respects.

**What this is:** a general clinical reference layer for an overnight
cross-covering resident to get oriented fast, in the same spirit as
`app/reference_layer.py`'s DailyMed/MedlinePlus footnotes -- informational,
never a protocol, never a substitute for the resident's own judgment, a
senior resident/attending, pharmacy, or the institution's actual current
order sets and protocols. Every document below says this explicitly and
repeatedly on purpose, matching this project's whole "decision-support, not
decision-maker" posture (`app/agent.py`'s SYSTEM_PROMPT, `THREAT_MODEL.md`).

**What this is NOT:** a licensed, maintained clinical decision-support
product (UpToDate, Lexicomp, a hospital's real order-set library). No
specific numeric dose, rate, or timing below should be treated as this
project's own clinical claim -- each is presented as commonly-taught,
widely-published general knowledge (the kind found in standard references
like UpToDate, the Surviving Sepsis Campaign, and ACC/AHA/ASH guidance),
attributed as such, not as something this project verified against a
primary source itself. `evidence_retriever`'s citations point back to
*which chunk of which file* grounded a claim -- never license this content
as clinically authoritative on its own.

## Files

- `hyperkalemia_management.md` -- UC2/UC3 example from PRD Section 4.3.
- `sepsis_screening.md` -- UC1/UC3 (the fever/altered-mental-status page
  scenario USERS.md opens with).
- `antibiotic_cross_reactivity.md` -- UC2/UC3; complements (does not
  replace) `app/clinical_reference.py`'s small curated drug-class table,
  which is deterministic/code-level (`check_allergy_conflict`'s hard-stop
  path). This corpus is the reasoning/explanation layer a resident can ask
  a follow-up question against ("why is amoxicillin still flagged for a
  penicillin allergy") -- `evidence_retriever` is retrieval/synthesis, not
  a second hard-stop mechanism.
- `anticoagulation_reversal.md` -- UC2/UC3 (a bleeding or pre-procedure
  cross-cover page).
