# Hyperkalemia: general management approach

General reference only -- not a protocol, not a substitute for the
resident's own judgment, a senior resident/attending, or the institution's
current order sets. Always confirm against local protocol before acting,
especially on dosing.

## Recognition and severity

Hyperkalemia is commonly stratified by serum potassium level and by whether
ECG changes are present, since ECG changes (not the lab value alone) drive
urgency:

- Mild: roughly 5.1-5.9 mEq/L, usually no ECG changes.
- Moderate: roughly 6.0-6.4 mEq/L.
- Severe: 6.5 mEq/L or higher, or any level with ECG changes -- treated as
  an emergency regardless of the exact number.

A single elevated value should be interpreted with caution before acting:
hemolyzed samples, a difficult draw, or tourniquet time can produce a
falsely elevated ("pseudohyperkalemia") result. A repeat sample (ideally
non-hemolyzed, free-flowing) is commonly obtained in parallel with starting
workup when the clinical picture doesn't otherwise fit -- but a patient with
ECG changes or a truly severe level is treated urgently while that repeat is
pending, not held for it.

## ECG changes, in rough order of severity

Peaked T waves are typically the earliest change. Progression can include
PR prolongation, QRS widening, loss of P waves, and -- in severe,
unaddressed cases -- a sine-wave pattern that precedes ventricular
arrhythmia or cardiac arrest. Any new ECG change in a hyperkalemic patient
is generally treated as an indication for immediate stabilization (see
below) rather than waiting for lab confirmation of severity.

## The three-part general approach

Widely taught as three categories, usually pursued together rather than
strictly sequentially when the picture is severe:

**1. Stabilize the cardiac membrane.** IV calcium (calcium gluconate or
calcium chloride, per local protocol -- chloride is more irritating to
peripheral veins and often reserved for central access) does not lower
potassium but rapidly antagonizes its effect on cardiac conduction,
typically within minutes. Given for ECG changes or severe hyperkalemia;
effect is short-lived (roughly 30-60 minutes), so it buys time for the
next steps rather than replacing them.

**2. Shift potassium intracellularly.** IV insulin (typically given with
concurrent dextrose to prevent hypoglycemia, especially in a
non-hyperglycemic patient) is a standard first-line shifting agent, with
onset within 15-30 minutes and duration of several hours. Nebulized or IV
beta-2 agonists (e.g. albuterol) are a commonly used adjunct with a similar
shifting mechanism, generally in addition to insulin, not as a substitute
in a severe case. Sodium bicarbonate's role in shifting is more limited and
is not considered reliable as monotherapy for hyperkalemia; it is
occasionally used adjunctively in specific contexts (e.g. concurrent severe
metabolic acidosis).

**3. Remove potassium from the body.** Loop diuretics (if the patient has
adequate renal function and urine output) promote renal excretion.
Potassium binders (e.g. patiromer, sodium zirconium cyclosilicate, or
older agents like sodium polystyrene sulfonate) act over hours, not
minutes, and are not appropriate as the sole treatment for a severe or
ECG-changed presentation. Dialysis is the definitive removal method for
severe, refractory, or renal-failure-associated hyperkalemia, and is
typically involved early (often via nephrology) when the patient is
dialysis-dependent, has significant renal impairment, or isn't responding
to medical management.

## Things that specifically matter for a cross-covering resident

- Check the medication list for anything that raises potassium or impairs
  excretion before treating reflexively: ACE inhibitors/ARBs,
  potassium-sparing diuretics (spironolactone, amiloride, eplerenone),
  NSAIDs, trimethoprim-sulfamethoxazole, and potassium supplements are
  common culprits. Holding these is often part of the plan, alongside
  treatment.
- A conditional sign-out instruction ("if potassium is high, give X") can
  only be safely acted on after confirming both the current value is real
  (not stale, not hemolyzed) and that nothing about the patient's picture
  has changed since the instruction was written -- this is exactly the
  kind of check `compare_signout_to_chart` exists to support (USERS.md
  UC2).
- This is a "page the senior resident / attending" situation for anything
  beyond mild, asymptomatic hyperkalemia with no ECG changes, not a
  cross-cover-resident-alone management decision.
