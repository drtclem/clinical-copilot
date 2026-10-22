# Sepsis screening: general approach

General reference only -- not a protocol, not a substitute for the
resident's own judgment, a senior resident/attending, or the institution's
current sepsis order set/bundle. Always confirm against local protocol.

## Why screening matters for a page like "fever, patient's altered"

Sepsis is time-sensitive: delayed recognition and delayed antibiotics are
consistently associated with worse outcomes in the literature this
screening approach is drawn from. The goal of screening on a page like this
is to decide, quickly, whether this looks like a patient who needs the
sepsis pathway activated (bundle, cultures, broad-spectrum antibiotics,
fluids, closer monitoring) versus a more routine fever workup.

## Common screening tools

**qSOFA (quick Sequential Organ Failure Assessment)** -- three criteria,
each worth one point, intended as a fast bedside screen (not a diagnostic
tool) for patients with suspected infection who may be at higher risk of a
poor outcome:

- Respiratory rate ≥ 22/min
- Altered mentation (any acute change from baseline)
- Systolic blood pressure ≤ 100 mmHg

A qSOFA of 2 or more is commonly used as a trigger to escalate the workup
and consider the patient at meaningfully higher risk, but qSOFA is a
screening tool, not a rule-out: a patient with suspected infection and a
low qSOFA can still be septic, and clinical judgment (plus the full picture
below) should not be discarded because a screening score is reassuring.

**SIRS (Systemic Inflammatory Response Syndrome) criteria** -- older,
broader, more sensitive but less specific than qSOFA: temperature
>38°C or <36°C, heart rate >90/min, respiratory rate >20/min or PaCO2 <32
mmHg, and white blood cell count >12,000 or <4,000/mm³ (or >10% bands).
Two or more, with a suspected or confirmed infection source, has
historically been used as a sepsis-screening threshold; many institutions
have since shifted toward qSOFA/SOFA-based approaches, but SIRS criteria
remain widely taught and are still used in some order sets.

## What to gather for this specific page

For a fever + altered mental status page in particular (the exact scenario
`USERS.md` opens with), the material USERS.md itself flags as directly
relevant:

- Recent antibiotic exposure (recent courses can mask or partially treat
  an evolving infection, and matter for empiric antibiotic choice)
- Recent culture results (blood, urine, respiratory, wound -- anything
  pending or already resulting)
- Immunosuppression status (transplant, chemotherapy, chronic steroids,
  known immunodeficiency -- changes both threshold for concern and
  differential)
- Lines and devices (central lines, urinary catheters, surgical drains --
  common infection sources and a reason to consider source control, not
  just antibiotics)
- The allergy list, before any empiric antibiotic order is even
  considered -- `check_allergy_conflict` exists specifically so this step
  is never skipped.

## Escalation

A patient who screens positive (elevated qSOFA/SIRS with suspected
infection), or who simply looks sick regardless of the exact score, is
generally not a "wait and reassess" situation for a covering resident
alone -- this is a "call the senior resident / activate the sepsis pathway
now" scenario. Time-to-antibiotics is one of the most consistently cited
modifiable factors in sepsis outcomes in the literature this approach
reflects, so escalating early is favored over waiting for full
confirmation.
