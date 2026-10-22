"""FHIR patient ids for the audit's fixture patients (audit-notes.md).

These UUIDs are stack-specific -- a fresh OpenEMR instance (the droplet,
CI's staging runner, a new dev clone) assigns its own UUIDs when
evals/seed_fixtures.py creates these patients. The literals below are this
LOCAL docker/development-easy stack's values, used as defaults; each can be
overridden by an env var (set on the staging CI runner via
/etc/clinical-copilot-ci.env, populated from evals/seed_fixtures.py's
--out JSON) so the same eval code runs unmodified against either stack.
"""

import os

PID1_ALICE = os.environ.get("FIXTURE_PID1_ALICE", "98c4b82b-b07e-11f1-8334-022958ad0af8")  # normal: 2 problems, 2 meds, 1 (uncoded) allergy
PID2_BOB = os.environ.get("FIXTURE_PID2_BOB", "98c4c088-b07e-11f1-8334-022958ad0af8")  # normal: 1 problem, 1 med, 1 (uncoded) allergy
PID3_CAROL = os.environ.get("FIXTURE_PID3_CAROL", "98c4c247-b07e-11f1-8334-022958ad0af8")  # empty chart
PID4_DAN = os.environ.get("FIXTURE_PID4_DAN", "98c4c335-b07e-11f1-8334-022958ad0af8")  # other-provider patient
PID5_ERIN = os.environ.get("FIXTURE_PID5_ERIN", "98c4c3f8-b07e-11f1-8334-022958ad0af8")  # other-provider patient
PID6_ALICE_DUP = os.environ.get("FIXTURE_PID6_ALICE_DUP", "98c4c4c8-b07e-11f1-8334-022958ad0af8")  # duplicate of pid1, conflicting metformin dose
PID7_UPLOAD_EVAL = os.environ.get("FIXTURE_PID7_UPLOAD_EVAL", "a2d11c5d-e3c4-41bd-8832-765e3a5aa2ae")  # dedicated, blank -- every document-upload case targets this patient, never PID1/PID2, to bound accumulation to one place
