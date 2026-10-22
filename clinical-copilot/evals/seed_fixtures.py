"""Real, scripted fixture-seeding tool -- replaces the manual SQL import
(`.pre-testdata-backup.sql` / `.pre-testdata-dr1-secure.sql`) Week 1 flagged
as debt (Decision 5.1). Seeds the six audit fixture patients (audit-notes.md)
via OpenEMR's Standard REST API -- the same API surface the rest of this
project uses, not direct DB writes.

Idempotent: each patient is looked up by `pubpid` first; if it already
exists, creation is skipped and its existing puuid is reused. Safe to run
repeatedly against the same OpenEMR instance (including local dev, where
these patients may already exist).

Usage:
    python -m evals.seed_fixtures --base-url https://147.182.143.252:9300 \\
        --client-id <id> --client-secret <secret> \\
        --username admin --password pass \\
        --out evals/fixtures.staging.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

import httpx


@dataclass
class PatientSpec:
    pubpid: str
    fname: str
    lname: str
    dob: str
    sex: str
    ss: str = ""
    street: str = ""
    city: str = "Springfield"
    state: str = "IL"
    postal_code: str = "62701"
    phone_home: str = ""
    problems: list[str] = field(default_factory=list)
    meds: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)


# Per audit-notes.md's original fixture spec (lines 53-58) -- content below
# is copied VERBATIM from the real local-dev database's `lists` table (not
# reconstructed from audit-notes.md's high-level summary, which is how the
# first version of this script drifted from it: several unit tests
# (evals/golden_facts.py) hardcode exact expected drug/diagnosis substrings
# against this specific patient's specific chart, so a synthetic
# approximation silently fails them on a fresh stack even though the
# fixture's *shape* -- N problems, N meds, N allergies -- matches).
PATIENTS: list[PatientSpec] = [
    PatientSpec(
        pubpid="TEST-0001", fname="Alice", lname="Testpatient", dob="1972-03-14",
        sex="Female", ss="123-45-6789", street="100 Main St", phone_home="555-0101",
        problems=["Type 2 diabetes mellitus", "Essential hypertension"],
        meds=["Metformin 500 mg PO BID", "Lisinopril 10 mg PO daily"],
        allergies=["Penicillin (hives)"],
    ),
    PatientSpec(
        pubpid="TEST-0002", fname="Bob", lname="Testpatient", dob="1958-11-02",
        sex="Male", ss="123-45-6790", street="102 Main St", phone_home="555-0102",
        problems=["Chronic obstructive pulmonary disease"],
        meds=["Tiotropium 18 mcg inhaled daily"],
        allergies=["Sulfa drugs (rash)"],
    ),
    PatientSpec(
        pubpid="TEST-0003", fname="Carol", lname="Emptychart", dob="1990-07-21",
        sex="Female", ss="123-45-6791", street="103 Main St", phone_home="555-0103",
        # deliberately empty -- data-quality case
    ),
    PatientSpec(
        pubpid="TEST-0004", fname="Dan", lname="Otherprovider", dob="1965-01-30",
        sex="Male", ss="123-45-6792", street="104 Main St", phone_home="555-0104",
        problems=["Hyperlipidemia"],
        meds=["Atorvastatin 20 mg PO nightly"],
    ),
    PatientSpec(
        pubpid="TEST-0005", fname="Erin", lname="Otherprovider", dob="1983-09-09",
        sex="Female", ss="123-45-6793", street="105 Main St", phone_home="555-0105",
        problems=["Asthma, mild intermittent"],
        allergies=["Latex"],
    ),
    PatientSpec(
        # Duplicate of TEST-0001: same name/DOB, but no SSN and a different
        # street/phone -- and a CONFLICTING Metformin dose (1000 mg vs
        # TEST-0001's 500 mg). This is the point of the fixture.
        pubpid="TEST-0006", fname="Alice", lname="Testpatient", dob="1972-03-14",
        sex="Female", ss="", street="100 Main Lane", phone_home="555-0106",
        meds=["Metformin 1000 mg PO BID"],
    ),
    # 2026-09-24: dedicated to the Week 2 golden-set's document-upload cases
    # (evals/cases.py) -- deliberately blank (no problems/meds/allergies,
    # nothing else to conflict with). Every upload-performing case's own
    # content bytes carry a fresh per-run UUID nonce (so dedupe_key never
    # collides across runs -- see app/ingestion/storage.py's dedupe_key),
    # but that alone doesn't stop uploaded documents from accumulating on
    # whichever patient they target over months of CI runs. Confining that
    # accumulation to this one patient -- never PID1/PID2, both used by the
    # rest of the golden set -- keeps a future bulk cleanup (delete every
    # document where foreign_id = this patient's internal pid) safe and
    # trivial, with zero risk to any other fixture's data.
    PatientSpec(
        pubpid="TEST-0007", fname="Uploads", lname="Evalfixture", dob="1995-05-05",
        sex="Female", ss="123-45-6794", street="107 Main St", phone_home="555-0107",
    ),
]


class SeedClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, username: str, password: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self._base_url, verify=False, timeout=30.0)
        token_resp = self._http.post(
            "/oauth2/default/token",
            data={
                "grant_type": "password",
                "client_id": client_id,
                "client_secret": client_secret,
                "user_role": "users",
                "username": username,
                "password": password,
                "scope": (
                    "openid api:oemr api:fhir user/Patient.read "
                    "user/patient.c user/patient.r user/patient.u user/patient.s "
                    "user/medical_problem.c user/medical_problem.r "
                    "user/allergy.c user/allergy.r "
                    "user/prescription.c user/prescription.r "
                    "user/encounter.c user/encounter.r"
                ),
            },
        )
        token_resp.raise_for_status()
        self._token = token_resp.json()["access_token"]
        self._http.headers["Authorization"] = f"Bearer {self._token}"

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._http.post(f"/apis/default/api{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def find_patient_by_pubpid(self, pubpid: str) -> dict | None:
        resp = self._http.get("/apis/default/api/patient", params={"pubpid": pubpid})
        resp.raise_for_status()
        data = resp.json().get("data") or []
        matches = [p for p in data if p.get("pubpid") == pubpid]
        return matches[0] if matches else None

    def create_patient(self, spec: PatientSpec) -> dict:
        payload = {
            "pubpid": spec.pubpid,
            "fname": spec.fname,
            "lname": spec.lname,
            "DOB": spec.dob,
            "sex": spec.sex,
            "ss": spec.ss,
            "street": spec.street,
            "city": spec.city,
            "state": spec.state,
            "postal_code": spec.postal_code,
            "phone_home": spec.phone_home,
            "country_code": "US",
        }
        result = self._post("/patient", payload)
        return result["data"]

    def add_problem(self, puuid: str, title: str) -> None:
        self._post(f"/patient/{puuid}/medical_problem", {"title": title, "begdate": "2026-01-01"})

    def add_allergy(self, puuid: str, title: str) -> None:
        self._post(f"/patient/{puuid}/allergy", {"title": title, "begdate": "2026-01-01"})

    def add_medication(self, pid: str, drug: str) -> None:
        # NOT /patient/{pid}/medication (writes a bare `lists` row with no
        # matching `lists_medication` companion row) -- FHIR MedicationRequest
        # (what get_patient_snapshot actually reads) is backed by
        # PrescriptionService, whose search is a UNION of the real
        # `prescriptions` table with `lists JOIN lists_medication`; a
        # `lists`-only row never appears on either side of that join.
        # POST /api/prescription writes directly to `prescriptions`, the
        # simpler and correct path (src/Services/PrescriptionService.php's
        # only required fields are `drug` and `patient_id`).
        self._post("/prescription", {"drug": drug, "patient_id": pid})

    def add_encounter(self, puuid: str, sensitivity: str = "high", reason: str = "Office Visit") -> dict:
        payload = {
            "date": "2026-01-15",
            "reason": reason,
            "facility": "Your Clinic Name Here",
            "pc_catid": "5",
            "facility_id": "3",
            "billing_facility": "3",
            "sensitivity": sensitivity,
            "pos_code": "0",
            "class_code": "AMB",
        }
        return self._post(f"/patient/{puuid}/encounter", payload)


def seed(client: SeedClient) -> dict[str, str]:
    """Returns {pubpid: puuid} for all six fixture patients."""
    puuids: dict[str, str] = {}
    for spec in PATIENTS:
        existing = client.find_patient_by_pubpid(spec.pubpid)
        if existing is not None:
            print(f"[skip] {spec.pubpid} ({spec.fname} {spec.lname}) already exists, puuid={existing['uuid']}")
            puuids[spec.pubpid] = existing["uuid"]
            continue

        patient = client.create_patient(spec)
        puuid = patient["uuid"]
        pid = patient["pid"]
        puuids[spec.pubpid] = puuid
        print(f"[create] {spec.pubpid} ({spec.fname} {spec.lname}) -> pid={pid} puuid={puuid}")

        for problem in spec.problems:
            client.add_problem(puuid, problem)
            print(f"  + problem: {problem}")
        for allergy in spec.allergies:
            client.add_allergy(puuid, allergy)
            print(f"  + allergy: {allergy}")
        for med in spec.meds:
            client.add_medication(pid, med)
            print(f"  + medication: {med}")

        if spec.pubpid == "TEST-0004":
            client.add_encounter(puuid, sensitivity="high", reason="Sensitive Visit")
            print("  + encounter: sensitivity=high")

    return puuids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. https://147.182.143.252:9300")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="pass")
    parser.add_argument("--out", default=None, help="Write {pubpid: puuid} JSON to this path")
    args = parser.parse_args()

    client = SeedClient(args.base_url, args.client_id, args.client_secret, args.username, args.password)
    puuids = seed(client)

    print("\n--- puuid map ---")
    print(json.dumps(puuids, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(puuids, f, indent=2)
        print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
