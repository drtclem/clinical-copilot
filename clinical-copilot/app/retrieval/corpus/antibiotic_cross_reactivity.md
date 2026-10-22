# Antibiotic allergy cross-reactivity: general reference

General reference only -- not a protocol, not a substitute for the
resident's own judgment, a senior resident/attending, allergy/immunology
consultation, or the institution's current protocol. This document is the
reasoning/explanation layer for a resident's follow-up question; it does
NOT replace `check_allergy_conflict`'s deterministic, code-level hard-stop
check (`app/tools.py`, `app/clinical_reference.py`), which is what actually
gates a medication mention in this system and runs regardless of whether
this corpus is consulted.

## Why "penicillin allergy" alone isn't the whole answer

Not all reactions labeled "penicillin allergy" in a chart carry the same
risk. What matters clinically is the reaction type, not just the drug name:

- A documented severe IgE-mediated reaction (anaphylaxis, angioedema,
  urticaria/hives occurring rapidly after exposure) is treated with real
  caution around cross-reactive beta-lactams.
- A vague or non-specific entry ("GI upset," "rash" of unclear timing/type,
  or an entry with no reaction described at all) is a materially different
  risk profile, though still worth flagging and clarifying, not
  discarding -- this system's own `low_confidence` flag on an uncoded
  allergy entry (`CheckAllergyConflictOutput`) reflects exactly this
  uncertainty.
- A reaction consistent with a non-allergic side effect (e.g. diarrhea from
  disrupting gut flora) is often not a true allergy at all, but a chart
  entry rarely distinguishes this without a clarifying conversation.

## Beta-lactam cross-reactivity, general pattern

- **Within penicillins** (penicillin, amoxicillin, ampicillin,
  piperacillin, nafcillin, oxacillin, dicloxacillin, and related agents):
  high cross-reactivity is assumed across this class for a patient with a
  true penicillin allergy -- this is the exact relationship
  `app/clinical_reference.py`'s curated table encodes for this system's
  hard-stop check.
- **Penicillins and cephalosporins:** commonly taught historical teaching
  cited cross-reactivity rates as high as 10%, but more recent literature
  (looking at shared side-chain structure rather than the beta-lactam ring
  itself) suggests true cross-reactivity is substantially lower for most
  penicillin-cephalosporin pairs, and depends heavily on which specific
  cephalosporin (side-chain similarity, not just "is it a cephalosporin,"
  is the more precise driver). This is a genuinely nuanced, evolving area
  -- an allergy/immunology consult or a documented penicillin skin test is
  the appropriate way to resolve it for a specific patient, not a blanket
  chart-level rule.
- **Penicillins and carbapenems:** cross-reactivity is now understood to be
  low, lower than historically taught, though a fully allergy-free
  guarantee still isn't appropriate to state from chart data alone.
- **Aztreonam** (a monobactam) has minimal cross-reactivity with
  penicillins in general (it does have some structural cross-reactivity
  specifically with ceftazidime, a distinct consideration).

## What this means practically for a cross-cover resident

- A documented penicillin allergy does not automatically rule out every
  beta-lactam -- but it also isn't something to reason around informally
  at 2 a.m. without senior input, given how much the real risk depends on
  reaction type and specific drug.
- The safest default when starting empiric antibiotics overnight for a
  patient with a documented beta-lactam allergy of uncertain severity is
  to choose a non-cross-reactive alternative (e.g. a
  non-beta-lactam class appropriate to the suspected source) or to get
  senior/pharmacy input before using a cross-reactive agent, rather than
  independently deciding the allergy "probably doesn't count."
- If the resident is specifically asking "why did the system flag this
  medication," the answer should point back to the actual documented
  allergy text and, if applicable, the specific drug-class relationship
  that triggered it -- not a generic cross-reactivity lecture disconnected
  from what's actually in the chart.
