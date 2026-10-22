"""Load test for /chat -- ARCHITECTURE.md 7.9.

Each simulated user runs one bounded, realistic conversation (not a single
repeated request): a normal orientation, a natural follow-up in the same
conversation (exercising the multi-turn grounding fix, ERROR_ANALYSIS.md
Entry 5, under real concurrency instead of single-user testing), and --
for roughly 1 in 10 users -- a third message with a malformed patient ID,
so the error path is represented in the load, not just the happy path.

This is a load test proving the system holds up under concurrency, not a
soak test: every user sends exactly 2 or 3 messages total, then stops.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task
from locust.exception import StopUser

PID1_ALICE = "98c4b82b-b07e-11f1-8334-022958ad0af8"
PID2_BOB = "98c4c088-b07e-11f1-8334-022958ad0af8"
MALFORMED_PATIENT_ID = "not-a-real-patient-id-1234"

ORIENTATION_MESSAGE = (
    "Give me a quick orientation on this patient: active problems, current meds, and allergies."
)
FOLLOW_UP_MESSAGE = "Given her allergy history, is amoxicillin safe to start empirically?"


class ResidentSession(HttpUser):
    """One resident's bounded chat session: 2 messages normally, 3 for ~1 in 10 users."""

    wait_time = between(1, 3)

    def on_start(self) -> None:
        patient_id = random.choice([PID1_ALICE, PID2_BOB])

        with self.client.post(
            "/chat",
            json={"message": ORIENTATION_MESSAGE, "patient_id": patient_id},
            name="/chat [orientation]",
            catch_response=True,
        ) as resp:
            conversation_id = None
            if resp.ok:
                conversation_id = resp.json().get("conversation_id")
            else:
                resp.failure(f"orientation request failed: {resp.status_code}")

        self.client.post(
            "/chat",
            json={
                "message": FOLLOW_UP_MESSAGE,
                "patient_id": patient_id,
                "conversation_id": conversation_id,
            },
            name="/chat [follow-up]",
        )

        if random.random() < 0.1:
            self.client.post(
                "/chat",
                json={
                    "message": ORIENTATION_MESSAGE,
                    "patient_id": MALFORMED_PATIENT_ID,
                    "conversation_id": conversation_id,
                },
                name="/chat [malformed-id]",
            )

        raise StopUser()

    @task
    def noop(self) -> None:
        """Never reached -- on_start raises StopUser before Locust would call a task."""
