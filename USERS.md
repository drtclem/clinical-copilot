# USERS.md — Target User & Use Cases

## Target user

**The overnight cross-covering internal medicine resident** (PGY-1/PGY-2, "night float"), covering
inpatient teams they don't normally work with, for patients they have never met.

This is deliberately narrower than "a physician" or even "a hospitalist." A cross-cover resident's
situation is structurally different from a physician seeing their own patient panel:

- They are not managing their own patients — they are covering for several day teams at once,
  often 40-80 patients across multiple services, none of whom they have a prior relationship with.
- They are supervised, not fully autonomous — decisions of consequence are expected to be run by
  a senior resident or attending, especially anything outside routine cross-cover scope.
- Their only source of context is whatever the day team wrote down at sign-out, plus whatever they
  can pull from the chart themselves under real time pressure, at 2 a.m., usually by phone or at
  the bedside.
- The stakes of stale or incomplete information are well documented, not hypothetical: a published
  patient-safety case report describes a cross-cover intern who followed a written sign-out
  instruction that had not been updated since the previous day, and gave IV fluids the primary team
  had specifically decided against, because the printed sign-out she was handed was stale. That is
  exactly the "confidently stated hallucination" failure mode this whole project is trying to guard
  against, occurring already, without any AI involved.

This user is also comparatively underserved by the current market. The dominant category of
existing clinical AI tools (Abridge, Suki, Nabla, DeepScribe, and similar) is ambient scribing —
listening during a visit and generating a note afterward. That solves a documentation problem for a
clinician seeing their own patient. It does not solve the cross-cover resident's problem, which is
the opposite moment: rapidly getting oriented to an unfamiliar patient *before* acting, not
documenting an encounter after the fact.

## Workflow

**The thirty seconds before the agent is needed:** a nurse calls the resident's phone. "Room 412,
fever of 102.3, patient's altered." The resident has never met this patient. They do not have the
chart open. They have, at best, a one-line sign-out entry from six hours ago that may or may not
still be accurate.

**What they need from the agent, in order:**
1. Who is this patient, why are they admitted, what is their baseline (mental status, functional
   status, code status)
2. What did the day team's sign-out say to anticipate for this patient overnight, and is that plan
   still current against the chart, or has something changed since sign-out was written
3. What's directly relevant to the specific complaint just called in (e.g., for a fever: recent
   antibiotics, recent cultures, immunosuppression, lines/devices, allergy list before any empiric
   order)
4. A way to ask a specific follow-up question conversationally while still on the phone or walking
   to the room, without switching into the EHR and hunting through tabs

**What they do with the output:** decide whether this can be handled at the bedside independently,
needs a senior resident, or needs the attending paged now. The agent's job is to compress the "get
oriented" step from several minutes of chart-hunting down to something that fits inside the walk to
the room, not to make the clinical decision itself.

## Use cases

### 1. Rapid patient orientation on a page for an unfamiliar patient

**Scenario:** the page above. The resident asks the agent for a synthesized picture of the patient:
admission reason, active problems, current medications, allergies, code status, and the most recent
relevant vitals/labs, filtered toward whatever the page was about (a fever page should surface
recent cultures and immune status; a mental-status page should surface baseline cognition and
recent sedating medications).

**Why an agent, not a dashboard:** the resident doesn't know in advance which two or three pieces
of a 40-page chart matter for this specific page. A dashboard shows everything, evenly weighted,
and still requires the resident to do the synthesis themselves under time pressure. A conversational
agent lets them ask the actual question they have ("what's this patient's baseline mental status"
or "any recent culture results") and get a direct, sourced answer, then immediately ask a follow-up
("what antibiotic are they already on") without re-navigating anything. That back-and-forth is the
whole value; a static screen can't do it.

### 2. Verifying a sign-out instruction against the current chart before acting on it

**Scenario:** directly modeled on the documented failure case above. Before executing a
conditional instruction from sign-out ("if potassium is high, give X"), the resident asks the agent
to confirm the instruction still matches the chart's current state — has a new lab result, order,
or note since sign-out changed the picture.

**Why an agent, not a dashboard:** this is inherently a comparison-and-reasoning task ("does A
still match B"), not a lookup. A dashboard can display the sign-out text and the current labs side
by side, but it can't tell the resident whether they're consistent — that requires actually
reasoning over both and flagging a discrepancy, which is exactly the multi-turn, tool-using
behavior an agent (not a static view) is suited for. This is also the single clearest
verification-system use case in the whole project: every claim the agent makes here must be
traceable to a specific chart entry, because this is the exact scenario where a hallucinated
"looks fine" could directly cause harm.

### 3. Time-critical synthesis during a rapid response or acute event

**Scenario:** a patient is acutely decompensating. The resident needs, in seconds, not minutes: code
status, allergies, active medications (for interaction/contraindication checking), and the most
recent vitals trend — while already moving toward the room.

**Why an agent, not a dashboard:** speed and hands-free/eyes-elsewhere use matter more here than in
any other use case — the resident may be asking this out loud while already walking or already at
the bedside, not sitting at a workstation clicking through tabs. A conversational interface that can
answer a spoken or quickly-typed question directly is the only shape that fits this moment; this is
also the use case with the least tolerance for latency or ambiguity, which should directly shape
how the verification layer and response format are designed for this path specifically.

### 4. End-of-shift summary handoff back to the primary day team

**Scenario:** at the end of the overnight shift, the resident needs to communicate what happened
overnight, for each patient they touched, back to the primary team taking over in the morning.

**Why an agent, not a dashboard:** published research on exactly this problem found that
auto-compiling overnight clinical events and forwarding them to the right people measurably
shortened next-morning sign-out and was strongly endorsed by residents for reducing "loss of key
information between shifts." An agent that already has the full record of what it helped surface
and what actions were taken overnight is well positioned to draft this summary directly, rather
than requiring the resident to reconstruct the night from memory at 6 a.m. This is a lower-stakes,
higher-value-add use case than 1-3, and a reasonable candidate to build only if time allows.

## Note on OpenEMR's current fit for this user

The audit found that OpenEMR's stock access-control model has no built-in concept of a
"cross-covering resident" role, or of attending supervision, at all (see `AUDIT.md`, Part I,
Physicians section: "OpenEMR has no per-provider patient panel restriction in the stock ACL," and
no attending/resident distinction exists in any tested role). A cross-cover resident's actual
access needs are unusual: broader than a normal physician's own panel (they must be able to see
patients across several teams they don't normally work with), but narrower in another sense
(limited to the shift they're covering, with an expectation of escalation rather than unilateral
action). Designing this access boundary is new work this project must do explicitly — it does not
already exist in OpenEMR — and `ARCHITECTURE.md` should treat it as a first-class design decision,
not an assumption.
