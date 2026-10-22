# Error analysis log

A running log, not a one-time document. After any real eval run -- not just
the entries seeded below -- add an entry here for anything unexpected
observed, whether or not it was a "failure" in the pass/fail sense. The
point is catching patterns over time, not just tracking a pass rate.

Format per entry: date, what was observed, root cause, fix/resolution (or
status, if not yet fixed).

---

## Planned: clinician (SME) review process — not yet implemented

Everything in the entries below was reviewed for *technical* correctness

(did the code behave as
designed, did a claim trace to real data) — not for *clinical* soundness. Nobody with actual
clinical training has looked at whether the agent's responses represent good clinical judgment,
only whether they're technically grounded in the chart. Those are genuinely different questions,
and conflating them would be a real gap this project shouldn't paper over.

**What a real clinician review process would add, that technical review can't:**
- Whether a technically-correct, fully-sourced response is actually the *clinically useful* thing
  to say in that moment — e.g., is the level of detail right, is anything technically true but
  clinically misleading by omission, would a real resident actually find this helpful at 2 a.m.
- Judgment calls no eval check can encode: is this specific allergy conflict a hard stop or a
  "use clinical judgment" situation; is this duplicate-record warning appropriately urgent or
  overstated
- Catching a category of error that's invisible to source-attribution checks entirely: a response
  can cite real data accurately and still represent bad clinical reasoning about what that data
  means

**Planned process (once a clinical advisor/reviewer is available):**
1. **What gets reviewed:** not just failures — a periodic sample of both flagged cases (where
   verification caught something) and a random sample of *passing* cases, since a false sense of
   safety from "it passed eval" is itself a risk this project is specifically trying to avoid.
2. **Cadence:** reviewed in batches (e.g., weekly during active development, monthly once stable)
   rather than one-off — this is meant to be an ongoing practice, matching the same "running log,
   not a one-time document" principle as the rest of this file.
3. **What happens with findings:** any clinically-flagged issue gets written up in this same file
   as a new entry, and — critically — gets converted into either a new eval case (if it's a
   pattern worth guarding against permanently) or a new domain constraint rule in `verification.py`
   (if it's a hard clinical rule that should never depend on model judgment), not just noted and
   forgotten.
4. **Who:** a licensed clinician (ideally someone with experience in the actual target workflow —
   overnight cross-coverage or hospitalist medicine, per `USERS.md`) reviewing specific
   transcripts, not a general medical advisor reviewing the concept in the abstract.

**Why this is stated as a plan rather than something faked today:** simulating clinical review
without an actual clinician would be worse than admitting the gap — it would create false
confidence in exactly the dimension (real clinical judgment) that matters most to this project's
core premise. This is deliberately left as an open, named next step rather than something checked
off prematurely.



## Entry 1 — Langfuse span timing bug (2026-09-16)

**Observed:** In the Langfuse trace view, `llm_call_0` (and every other
`llm_call_*` / `tool:*` observation) showed `Latency: 0.00s`, `Input: null`,
`Output: undefined`, despite the parent trace and the token-usage numbers
being correct.

**Root cause:** `TurnTrace.log_llm_call` / `log_tool_call` in
`app/observability.py` called `span.start_observation(...)` and
`child.end()` back-to-back, *after* the actual Anthropic API call or tool
call had already completed. Since both the start and end timestamps were
stamped at essentially the same instant, every span had zero-width
duration. `input`/`output` were never passed to either call -- only
`usage_details`, which is why token counts alone survived.

**Fix:** Restructured to `start_llm_call()` / `finish_llm_call()` (and the
tool-call equivalents) that bracket the *real* work: the span opens before
`self._client.messages.create(...)` (or the tool call) and closes after,
with the actual request/response content serialized and passed as
`input`/`output` at close. Also fixed a related gap found while touching
this: `thinking` content blocks were being serialized as bare
`{"type": "thinking"}` with the reasoning text dropped.

**Verification:** Re-ran the full eval suite (8/8) both locally and against
the droplet after the fix. Queried ClickHouse directly on both instances for
a fresh trace and confirmed real, non-zero, sequential durations that sum
correctly to the parent span's total, with real `input`/`output` content at
every step.

---

## Entry 2 — `ambiguous_query_unspecified_medication` eval flakiness (2026-09-16)

**Observed:** The same eval case, same code, same patient/message, passed on
one run and would plausibly fail on another depending on the model's exact
phrasing (e.g. "which medication" vs. "which specific medication" -- the
latter doesn't substring-match the former).

**Root cause:** The check (`_check_ambiguous_query` in `cases.py`) relies on
substring-matching a fixed list of clarification phrases
(`clarification_cues`). This makes it sensitive to model wording variance
rather than testing the actual underlying behavior (did the agent ask for
clarification, in any phrasing, rather than guess) robustly.

**Status:** Not yet fixed. Flagged as a known limitation of this specific
check, worth revisiting with a more semantic/flexible check (e.g. a cheap
LLM-judge pass, or a broader token-level heuristic) before Final Submission.
This is the same class of fragility already documented for the
source-attribution vocabulary match in `app/verification.py`.

---

## Entry 3 — GitHub Actions cannot reach the droplet (2026-09-16)

**Observed:** `.github/workflows/tests.yml`, once wired up to run
`evals/unit_tests.py` against the droplet's OpenEMR on every push, failed
every run (0/6 tests) with `detail_code='http_error'` on every FHIR/OAuth
call -- but the suite itself ran cleanly to completion (no crash, no
missing-config traceback), so this was never a missing-secrets problem.

**Investigation, in order:**
1. Ruled out missing/misconfigured GitHub secrets -- if `ANTHROPIC_API_KEY`
   or the `OPENEMR_*` secrets were unset, `app/config.py`'s `get_settings()`
   would raise `RuntimeError` before any test ran. It didn't; all 6 tests
   executed and failed identically.
2. Tested `https://...:9300` vs. `http://...:8300` (TLS/cert isolation
   test) -- identical failure on both ports, ruling out a TLS/self-signed-
   certificate-specific cause.
3. Confirmed via the DigitalOcean control panel that no Cloud Firewall is
   assigned to the droplet -- ruled out.
4. Checked for `fail2ban` on the droplet -- not installed.
5. Checked `iptables -L -n -v` -- nothing beyond the standard `ufw` +
   Docker chains, no exotic blocking rule.
6. Checked `ufw status verbose` -- both 8300 and 9300 correctly
   `ALLOW IN Anywhere`; confirmed both ports actually listening
   (`ss -tlnp`).
7. Checked `ufw`'s own packet log for the exact failure window -- full of
   ordinary internet background-noise port-scanning traffic, but **zero**
   blocked entries for port 8300 or 9300 (a `ufw`-level block would have
   logged here; it didn't).
8. Checked OpenEMR/Apache access logs for the same window -- **completely
   empty**. Not one request, on any port, from anywhere.

**Root cause:** Not fully determined, but narrowed precisely: nothing on
the droplet (`ufw`, `iptables`, `fail2ban`, the app itself) is blocking or
even seeing this traffic, yet it also never arrives. That combination means
the traffic is being dropped upstream of the droplet entirely -- before it
reaches `eth0` -- most likely DigitalOcean's automatic network-edge/
anti-abuse protection (a layer separate from, and not disabled by leaving
off, the optional Cloud Firewall product) filtering traffic from GitHub
Actions' datacenter IP ranges, or a routing/peering issue specific to
GitHub's network and this droplet's datacenter (NYC1). Normal traffic
(this session's own SSH-based testing, browser access, etc.) reaches the
droplet with no issue -- this is specific to GitHub Actions' network path.

**Status:** Not fixed; deliberately deprioritized (2026-09-16) to focus on
the demo video, since it doesn't block anything else. `.github/workflows/
tests.yml`'s automatic `push` trigger was removed (kept as
`workflow_dispatch`-only) so it stops failing on every commit; the LLM-free
unit suite goes back to fully manual (`python3 -m evals.unit_tests`) for
now, same as the LLM-based eval suite already is.

**Options considered:**
1. **Contact DigitalOcean support** and ask directly whether they're
   dropping traffic from GitHub Actions' IP ranges to this droplet -- they
   have edge-level visibility this investigation doesn't. Not yet done.
2. **Self-hosted GitHub Actions runner on the droplet itself** -- sidesteps
   the external-reachability question entirely, since the runner would hit
   `localhost:8300`/`9300` directly, exactly like this session's own
   successful SSH-based testing did throughout. Tradeoff: a self-hosted
   runner means GitHub-orchestrated code execution happens directly on a
   machine that also runs the droplet's real (if fixture) OpenEMR/Langfuse
   services -- a materially larger trust boundary than GitHub's own
   ephemeral hosted runners, worth deciding deliberately, not defaulting
   into.
3. **Keep the unit suite manually-triggered** (`python3 -m evals.unit_tests`
   / `workflow_dispatch`), same as the LLM-based eval suite already is --
   loses "runs safely on every commit for free," but requires no further
   infrastructure work. **This is the current state**, chosen deliberately
   to unblock today's demo-video work, not because options 1-2 were ruled
   out -- either remains available as a near-term follow-up.

---

## Entry 4 — COPD grounding false positive, rediscovered via manual trace review (2026-09-16)

**Observed:** Reading through a raw export of 126 real conversation traces (84 local, 42 droplet; the export itself was removed from the repo on 2026-09-25 because it held full prompts, responses and patient identifiers -- only these aggregate figures are kept) (built for Behavioral Coverage case-writing, per this file's own
stated process) surfaced a local trace, timestamp `2026-09-16 01:41:10`
(trace_id `e4cf75d1bef4d6e700708e1a6ff04680`): asked a routine orientation
question about pid2 (Bob Testpatient, who genuinely has COPD per
`get_patient_snapshot`'s real tool output), the model correctly said the
patient has "COPD" -- and the source-attribution check flagged and
stripped it as an unverified claim, leaving an almost-empty, unhelpful
response. This is the inverse of Entry 1/the Warfarin case: a **false
positive** on the safety net (a true, grounded fact wrongly stripped), not
a missed hallucination -- and false positives degrade usefulness on
completely routine questions, which is its own real cost.

**Root cause:** `verification.py`'s grounding check did literal
substring/text matching between a claimed term and the tool's returned
text. The tool stores the condition as the full term ("Chronic obstructive
pulmonary disease"); the model used the standard clinical abbreviation
("COPD"), which never substring-matches the full term.

**Status: already fixed -- not a live bug.** This is not new work; it's
independent rediscovery, via manual trace review, of an issue that was
found and fixed during this project's initial development, before the very
first commit. The exact timeline is the evidence:

- The buggy trace: `2026-09-16 01:41:10` UTC.
- Commit `6481789` ("feat(clinical-copilot): add Early Submission build of
  Clinical Co-Pilot"), the first commit made in this repo, timestamped
  `2026-09-15 20:48:54 -0500` = **`2026-09-16 01:48:54` UTC** -- roughly
  seven minutes *after* the buggy trace -- already contains
  `app/verification.py`'s `_CLINICAL_ALIASES` map (`"copd": "chronic
  obstructive pulmonary disease"`, plus `htn`, `afib`, `ckd`, `t2dm`/`dm`,
  `mi`, `chf`) and the `_is_grounded()` helper that checks a claimed term's
  alias against the grounded set in both directions. `git log -S
  "_CLINICAL_ALIASES"` confirms this string has existed since that first
  commit and no other commit has touched it since.
- In other words: the bug was found and fixed live during initial
  development, in the roughly seven-minute gap between the buggy trace and
  the first commit, and has been fixed in every commit since.

**Verification (re-confirming the fix holds, not fixing anything new):**
1. Live call against pid2 today: `flagged_claims: []`, COPD appears
   unstripped in the response.
2. Deterministic check (no LLM call) of `verify_response()` against pid1's
   real tool output with a hand-built draft using "T2DM" and "HTN" (the
   other clinically-relevant abbreviations in this fixture set) --
   `flagged_claims: []` for both, confirming those aliases work too, not
   just COPD.
3. True-negative control: the same deterministic check with a fabricated
   Warfarin claim against pid1 -- `flagged_claims: ['warfarin']`,
   `passed_source_attribution: False`. The alias map is a small fixed
   dict, not fuzzy matching, so there's no mechanism by which it could
   have started letting real hallucinations through.

**Why this is still worth logging as an entry, even though nothing needed
fixing:** it's genuine evidence that manual trace review adds value beyond
what the automated eval suite alone catches -- this specific false-positive
pattern was never written up as a named eval case, and reading real
conversations surfaced it (or rather, surfaced proof that it had already
been caught) in a way the Golden Set's 8 cases don't specifically test for.
That's the exact case `COVERAGE.md`'s stated Behavioral Coverage process is
meant to make routine.

---

## Entry 5 — multi-turn grounding gap: verification only sees the current turn's tool calls (2026-09-16)

**Observed:** Running the newly-built Behavioral Coverage suite's
`c4_8_repeat_warning_next_turn` case (droplet run) -- a two-turn
conversation about pid1 (Alice Testpatient) -- surfaced an unexpected
`[HARD STOP -- allergy conflict]` injection and `flagged_claims:
['penicillin']` on turn 2, even though penicillin is genuinely pid1's real,
documented allergy, correctly fetched in turn 1 of the very same
conversation.

**What triggered it, concretely:** Turn 1: *"Give me a quick orientation on
this patient."* -- the agent calls `get_patient_snapshot`, gets back her
real chart (including her real penicillin allergy), and answers correctly.
Turn 2, same conversation: *"And what medications is she on?"* -- a natural
follow-up. The agent answered from its memory of turn 1's data rather than
redundantly re-calling `get_patient_snapshot` (reasonable, efficient
behavior), and its draft mentioned the real penicillin allergy as a
reminder. Verification flagged and stripped it as unverified, and the
domain-constraint layer (seeing "penicillin" as an apparently-unverified
medication-shaped term) fired a hard-stop warning about it too.

**Root cause:** `app/agent.py`'s `run_turn()` built `turn_records` --  the
list of real tool results passed to `verify_response()` for grounding --
fresh on every call, from only the tool calls made *during that specific
call*. It had no visibility into tool calls from earlier turns in the same
conversation. So on any follow-up turn where the model correctly reuses
already-fetched data instead of redundantly re-fetching, the grounded-facts
list was empty, and anything the model said -- even something 100% true and
already-verified one turn earlier -- had nothing to check against and got
treated as if it were fabricated. Reproduced deterministically, zero LLM
cost: `verify_response("...documented penicillin allergy.", [], PID1,
fhir)` (empty `turn_records`, simulating a turn with no new tool call)
yields `flagged_claims: ['penicillin']` on demand.

This is more than a single-case bug: USERS.md explicitly designs around a
resident asking an orientation question and then naturally asking
follow-ups in the same conversation. Every follow-up turn where the model
efficiently reused earlier data (instead of redundantly re-fetching) was at
risk of having true information wrongly stripped.

**Fix:** Thread accumulated tool records through the conversation the same
way `history` already is, rather than resetting them every turn:
- `ChatTurnResult` gained `accumulated_tool_records: list[ToolCallRecord]`
  -- every real tool result fetched anywhere in the conversation so far,
  not just this turn.
- `run_turn()` gained an optional `prior_tool_records` parameter; callers
  pass back the previous turn's `accumulated_tool_records`.
  `turn_records` now starts from that list instead of `[]`, so new tool
  calls append to the accumulated history instead of replacing it.
- **Cross-patient guard, not just accumulate-everything:** naively
  accumulating every prior record risked a different bug -- if a
  conversation switches patients mid-way, patient A's data could wrongly
  ground claims about patient B. `ToolCallRecord` already carries its own
  `patient_id`, so `verify_response()` is now called with only the records
  matching the *current* turn's active patient
  (`records_for_active_patient = [r for r in turn_records if r.patient_id
  == patient_id]`), while the full unfiltered history is still kept in
  `accumulated_tool_records` in case the conversation switches back to an
  earlier patient later.
- `app/main.py`'s per-conversation state (`_ConversationState`) now carries
  `tool_records` alongside `history` between HTTP requests, the same way
  `history` already was.
- `evals/behavioral_coverage.py`'s multi-turn runner threads
  `accumulated_tool_records` between turns the same way, so multi-turn
  eval cases actually exercise the fixed behavior.

**Verification:**
1. Reproduced the exact original bug scenario live (two real turns, pid1,
   the orientation-then-medications sequence) -- before the fix, turn 2
   flagged `['penicillin']`; after the fix, `flagged_claims: []` on turn 2,
   with the real allergy correctly mentioned.
2. True-negative control: same setup, but the draft also asserts a
   genuinely false claim (Warfarin) alongside the true recalled one
   (penicillin) -- confirmed the true claim now passes through correctly
   grounded and unflagged, while the false claim is still correctly
   flagged and stripped, proving the fix didn't just loosen grounding
   generally.
3. Cross-patient guard: a two-turn conversation switching from pid1 to
   pid2 mid-conversation, with pid1's records still accumulated -- pid2's
   turn correctly reported only pid2's real data, no cross-contamination
   observed.
4. Re-ran everything after the fix, both locally and against the droplet:
   `evals/unit_tests.py` 6/6 both, Golden Set (`evals/run_evals.py`) 8/8
   both with Gate: PASS both, Behavioral Coverage (`evals/
   run_behavioral_coverage.py`) 43/49 (88%) both. The drop from the
   pre-fix 46/49 (local) / 45/49 (droplet) baseline is fully accounted for
   by pre-existing LLM-response variance on single-turn cases whose code
   path is byte-identical before and after this fix (confirmed by
   re-running `c4_9_vitals_only_question` standalone and observing the
   model call `get_patient_snapshot` on one run and not the other, same
   code, same message) -- not a regression from this change. Directly
   confirmed on the original discovery case: `c4_8_repeat_warning_next_turn`
   still fails on both instances, but only for its own separately-documented,
   expected reason (the duplicate-warning-repetition design tension); its
   `flagged_claims` is now `[]` on both, where it was previously
   `['penicillin']` on the droplet run that discovered this bug.

## Entry 6 — Langfuse's own telemetry went blind under droplet CPU contention (2026-09-16)

**Observed:** `PERFORMANCE_BASELINE.md`'s droplet load test (10 concurrent users, 20 real
completed requests, 0 errors) showed a stark mismatch between what residents actually experienced
and what our own observability recorded:
- Locust (client-side, real wall-clock): p95 = 154.0s, max = 154.19s.
- Langfuse's own recorded `AGENT`-span duration for the same window, queried directly from the
  droplet's ClickHouse: only **5 of 20** real requests produced an `AGENT` span at all, and the
  ones that did showed p95 = 17.2s, max = 17.7s — nowhere near what the client actually waited.

Locally, the same query against the same kind of load (130 real requests across two stages) showed
a clean 130/130 `AGENT`-span match with latency tracking the client-observed numbers reasonably
closely. This was droplet-specific, not a general bug in the instrumentation code.

**Root cause (confirmed, not just theorized):** severe CPU contention on the droplet's original
2-vCPU/4GB sizing. `clickhouse` alone peaked at 165.8% CPU — more than one full core — on a box
with only two. Under that contention, requests spent real time queued before our own instrumented
code path (and therefore any Langfuse span) even began, and `app/observability.py`'s deliberate
best-effort design (a Langfuse outage must never break the resident-facing response — see Entry 1's
`start_llm_call`/`finish_llm_call` split for the same underlying philosophy) meant span-create
calls to the self-hosted Langfuse ingestion endpoint could silently fail under that contention
without surfacing anywhere. The system stayed correct (0 errors) but its own telemetry didn't.

**Why this mattered concretely:** the droplet's p95-latency Monitor (ARCHITECTURE.md §7.7,
threshold 30,000 ms) read `severity: OK` throughout the entire 154-second-real-wait load test. A
real, severe degradation produced no alert, because the alert's data source was itself a casualty
of the same contention it exists to catch.

**Fix, in two parts, both real and both verified:**

1. **Scoped, Langfuse-independent request-timing log** (`app/main.py`): a small ASGI middleware
   (`request_timing_middleware`) that starts timing at the moment a request enters the middleware
   stack — before routing, before the `/chat` handler, before any Langfuse span opens — and appends
   one line per request (timestamp, method, path, duration, status) to `request_timing.log`. Plain
   Python file I/O, no import from `app/observability.py`, no dependency on Langfuse being
   configured or reachable. This is deliberately the small, scoped fix, not the full independent
   monitoring pipeline described below.
2. **Droplet resized 2 vCPU/4GB → 4 vCPU/8GB**, to test directly whether the root cause was
   resource contention (fixable by headroom) rather than a Langfuse limitation (which resizing
   wouldn't fix).

**Verification — re-ran the exact same 10-concurrent-user droplet stage after both changes, and
compared three independent sources for the same 22 real completed requests (0 errors):**

| Source | p50 | p95 | max | count vs. real requests |
|---|---|---|---|---|
| Locust (ground truth) | 14.0s | 37.0s | 36.64s | 22/22 |
| Langfuse `AGENT`-span | 11.68s | 34.50s | 34.90s | **22/22** (was 5/20 before the resize) |
| `request_timing.log` | 12.72s | 36.62s | 36.62s | 22/22, mean 19.03s vs. Locust's own mean 19.04s |

All three sources now agree within a few seconds of each other, and Langfuse's own span count
exactly matches the real request count. Peak container CPU on the resized box (`openemr` 212.9%,
`langfuse-web` 139.8%, `clickhouse` 86.1%) sits comfortably under the new ~400% ceiling, where
`clickhouse` alone previously exceeded the old ~200% ceiling by itself. As direct confirmation the
telemetry is trustworthy again: the droplet's p95-latency Monitor correctly fired
(`severity: ALERT`, `23:37:10Z`) on this very run, because the real p95 (34.5-37s) genuinely
exceeds its 30,000 ms threshold — the alert working exactly as designed once its data source could
be trusted, in contrast to the silent `OK` it showed during the original, undiagnosed contention.

**Stated honestly, not oversold:** the resize resolved the *observability* gap at its root — this
was a resource-contention problem, not a fundamental Langfuse limitation, confirmed rather than
assumed. It did not fully resolve *latency itself*: p95 at 10 concurrent droplet users (34.5-37s)
remains roughly 2x the droplet's own single-user baseline (p50≈12.8s/p95≈19.7s, ARCHITECTURE.md
§7.7) — a real, smaller, remaining concurrency cost, consistent with `PERFORMANCE_BASELINE.md`'s
broader finding that OpenEMR/FHIR access, not the agent's own code, is the dominant latency lever
at scale.

**Deliberately not attempted here — named as separate future work:** a proper independent
monitoring pipeline that doesn't depend on the resident-facing request path at all (e.g., a
lightweight reverse proxy logging its own timing independent of the application process, or a
synthetic canary request run on a fixed schedule regardless of real traffic). `request_timing.log`
is a genuinely useful, permanent, zero-cost safety net that stays in place regardless of what
caused tonight's specific gap, but it still lives inside the same application process as everything
else — a canary or proxy-level signal would be a strictly stronger, independent check, and is real
infrastructure work requiring its own careful build and verification, not something to fold into a
one-evening fix.
