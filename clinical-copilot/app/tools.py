"""The agent's tools.

Each maps directly to a USERS.md use case and a row of ARCHITECTURE.md's
Section 2 table:

- get_patient_snapshot     -> Patient, Condition, AllergyIntolerance, MedicationRequest
- check_allergy_conflict   -> AllergyIntolerance vs. a supplied medication name
- get_recent_encounters    -> Encounter (Phase 6, sensitivity-filtered -- app/sensitivity.py)
- get_recent_observations  -> Observation (UC2, NOT sensitivity-filtered -- see its own
  docstring below for why that compensating control doesn't apply here)
- summarize_shift_events   -> UC4, deterministic aggregation only, no FHIR call --
  synthesizes purely from this conversation's already-accumulated ToolCallRecords.
- compare_signout_to_chart -> UC2, does a fresh, unconditional fetch (Patient,
  Condition, MedicationRequest, AllergyIntolerance, Encounter, Observation) and
  structurally diffs it against turn_records -- never parses or trusts the
  sign-out's own text, see its own docstring below for why.

Every tool shares the same impl(fhir, tool_input, turn_records) dispatch
signature (ClinicalCopilotAgent._call_tool, app/agent.py) even though most
ignore one parameter or the other -- unified once a second tool needed
turn_records, rather than growing per-tool special cases in the dispatcher.

Error handling follows ARCHITECTURE.md Section 2's hard rule: a tool call
that fails returns a structured ToolFailure the agent must surface directly,
never silently answered around.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pydantic import BaseModel

from app.clinical_reference import cross_reactive_class, drug_classes_for, normalize_for_match
from app.fhir_client import FhirClient, FhirRequestError, bundle_entries
from app.ingestion.schemas import ExtractionResult, IntakeFormExtraction, LabPdfExtraction
from app.ingestion.storage import list_extracted_documents
from app.schemas import (
    AllergyFact,
    ChartDiscrepancy,
    CheckAllergyConflictInput,
    CheckAllergyConflictOutput,
    CompareSignoutToChartInput,
    CompareSignoutToChartOutput,
    ConditionFact,
    DuplicatePatientWarning,
    EncounterFact,
    GetPatientSnapshotInput,
    GetPatientSnapshotOutput,
    GetRecentEncountersInput,
    GetRecentEncountersOutput,
    GetRecentObservationsInput,
    GetRecentObservationsOutput,
    GetUploadedDocumentsInput,
    GetUploadedDocumentsOutput,
    MedicationFact,
    ObservationFact,
    ShiftEventFact,
    SummarizeShiftEventsInput,
    SummarizeShiftEventsOutput,
    ToolFailure,
    UploadedDocumentFactSummary,
    UploadedDocumentSummary,
)
from app.sensitivity import Role, filter_encounters_by_sensitivity

_DATA_ABSENT_SYSTEM = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class ToolCallRecord:
    """Defined here, not in app/verification.py (which re-exports it for
    backward compatibility -- see its import comment), because
    summarize_shift_events (below) needs it too, and verification.py
    already imports from tools.py -- the reverse (tools.py importing
    from verification.py) would be circular. Every real tool call this
    conversation, in either module, is recorded as one of these; both
    verify_response()'s grounding and summarize_shift_events' aggregation
    read the same accumulated list."""

    tool_name: str
    patient_id: str
    output: object  # GetPatientSnapshotOutput | CheckAllergyConflictOutput | ...
    # True only for records inherited from an EARLIER turn (run_turn's
    # prior_tool_records), never for one created during the current turn's
    # own tool-call rounds -- added 2026-09-22 specifically for
    # compare_signout_to_chart's baseline_established check (see that
    # function's docstring), which needs genuine separation in time, not
    # just "something else was fetched a few seconds ago this same turn."
    # Re-stamped at the top of every run_turn() call (app/agent.py), not
    # baked in permanently at creation time -- a record that was "this
    # turn's own" in turn N becomes genuinely prior-turn data once turn
    # N+1 starts. Every other consumer of turn_records (verify_response's
    # grounding, summarize_shift_events' aggregation) deliberately ignores
    # this field -- both want the full accumulated list regardless of when
    # within the conversation a fact was actually fetched.
    from_prior_turn: bool = False


def collect_source_resources(records: list[ToolCallRecord], patient_id: str | None) -> list[str]:
    """Every distinct `source_resource` (e.g. "Condition/<fhir-id>") in the
    successful tool outputs recorded for `patient_id`, in first-seen order.

    Deliberately the whole conversation's records for the active patient,
    not just the current turn's: verify_response() grounds a follow-up
    answer against exactly this set (ERROR_ANALYSIS.md Entry 5), so a reply
    answered from an earlier fetch must still show what it was grounded on.
    Other patients' records are excluded for the same reason
    ClinicalCopilotAgent.run_turn() excludes them from grounding.
    """
    if not patient_id:
        return []
    seen: dict[str, None] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "source_resource" and isinstance(value, str):
                    seen.setdefault(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for record in records:
        if record.patient_id == patient_id and isinstance(record.output, BaseModel):
            walk(record.output.model_dump())
    return list(seen)


def _strip_html(narrative_div: str) -> str:
    return _HTML_TAG_RE.sub("", narrative_div).strip()


def _codeable_concept_text(concept: dict | None) -> tuple[str, bool]:
    """Returns (text, is_coded). Falls back to narrative when the structured
    `code` is a data-absent-reason -- ARCHITECTURE.md 3.1 / Finding 11."""
    if not concept:
        return ("unknown", False)
    coding = concept.get("coding") or []
    is_data_absent = any(c.get("system") == _DATA_ABSENT_SYSTEM for c in coding)
    if not is_data_absent:
        if concept.get("text"):
            return (concept["text"], True)
        if coding and coding[0].get("display"):
            return (coding[0]["display"], True)
    return ("unknown", False)


def _patient_display(patient: dict) -> tuple[str, str | None, str | None]:
    names = patient.get("name") or []
    if names:
        given = " ".join(names[0].get("given", []))
        family = names[0].get("family", "")
        name = f"{given} {family}".strip()
    else:
        name = "Unknown"
    return name, patient.get("birthDate"), patient.get("gender")


def _find_duplicates(fhir: FhirClient, patient: dict) -> list[DuplicatePatientWarning]:
    names = patient.get("name") or []
    birth_date = patient.get("birthDate")
    if not names or not birth_date:
        return []
    family = names[0].get("family")
    if not family:
        return []
    try:
        bundle = fhir.search(
            "Patient", {"family": family, "birthdate": birth_date}
        )
    except FhirRequestError:
        # Duplicate detection is a compensating safety check, not the primary
        # request; if it fails we say nothing rather than block the snapshot,
        # but we do NOT claim "no duplicates" either -- see partial_failures.
        return []
    matches = []
    for entry in bundle_entries(bundle):
        if entry.get("id") == patient.get("id"):
            continue
        matches.append(
            DuplicatePatientWarning(other_patient_id=entry["id"], matched_on="name+birthdate")
        )
    return matches


def get_patient_snapshot(
    fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord], *, bypass_cache: bool = False
) -> GetPatientSnapshotOutput | ToolFailure:
    # turn_records: unused -- accepted only for the uniform impl(fhir,
    # tool_input, turn_records) dispatch signature all five tools now
    # share (app/agent.py's _call_tool). See summarize_shift_events'
    # docstring below for why the signature was unified rather than kept
    # special-cased once a second tool (compare_signout_to_chart) needed
    # turn_records too.
    #
    # bypass_cache: keyword-only, defaulted False -- invisible to
    # _call_tool's uniform positional dispatch, so this tool's normal
    # (cached) path needs no changes there. compare_signout_to_chart passes
    # True explicitly when it calls this directly for its own fresh
    # baseline (2026-09-19 caching design) -- see its own docstring.
    fetch = fhir.search_fresh if bypass_cache else fhir.search
    try:
        params = GetPatientSnapshotInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_patient_snapshot",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    # 2026-09-19 parallelization design: only 4 of these 5 calls are
    # actually independent -- the duplicate-detection search (_find_
    # duplicates) needs the Patient read's own result (name/birthDate), so
    # it can't start at t=0 with the other four. Fire the Patient read and
    # the three resource searches concurrently; the moment the Patient read
    # resolves, fire the duplicate search too, so it can still overlap with
    # whatever's left of the other three. A fresh, function-scoped pool
    # (not a shared module-level one) -- avoids needing to size a shared
    # pool for worst-case concurrent residents, at the cost of small,
    # per-call thread-creation overhead the measured savings comfortably
    # absorb. Empirically verified against the real local OpenEMR instance
    # (uncontended: 1.45x; under simulated concurrent-resident load: 1.56x
    # -- see PERFORMANCE_BASELINE.md) -- the droplet re-run remains an open
    # validation step, not silently assumed to hold there too.
    with ThreadPoolExecutor(max_workers=5) as executor:
        patient_future = executor.submit(fhir.read, "Patient", params.patient_id)
        condition_future = executor.submit(fetch, "Condition", {"patient": params.patient_id})
        medication_future = executor.submit(fetch, "MedicationRequest", {"patient": params.patient_id})
        allergy_future = executor.submit(fetch, "AllergyIntolerance", {"patient": params.patient_id})

        try:
            patient = patient_future.result()
        except FhirRequestError as exc:
            return ToolFailure(
                tool="get_patient_snapshot",
                reason=f"I couldn't retrieve this patient's chart ({exc.detail_code}).",
                detail_code=exc.detail_code,  # type: ignore[arg-type]
            )

        # Submitted only once the Patient read resolves (it needs that
        # result) -- still overlaps with whatever's left of the three
        # resource searches above, rather than waiting for all of them.
        duplicates_future = executor.submit(_find_duplicates, fhir, patient)

        name, birth_date, gender = _patient_display(patient)
        partial_failures: list[str] = []

        # Each try/except below is unchanged from the sequential version --
        # Future.result() re-raises the exact exception a direct call would
        # have raised, so one resource type failing still doesn't block the
        # others from populating partial_failures independently. All
        # parsing happens here on the calling thread, never inside a worker
        # thread, so there's no shared-mutable-state race on partial_failures
        # or the fact lists below.
        conditions: list[ConditionFact] = []
        try:
            bundle = condition_future.result()
            for res in bundle_entries(bundle):
                text, _ = _codeable_concept_text(res.get("code"))
                category_codes = [
                    c.get("code")
                    for cat in (res.get("category") or [])
                    for c in (cat.get("coding") or [])
                ]
                category = "unknown"
                if "problem-list-item" in category_codes:
                    category = "problem-list-item"
                elif "health-concern" in category_codes:
                    category = "health-concern"
                conditions.append(
                    ConditionFact(
                        text=text,
                        category=category,  # type: ignore[arg-type]
                        clinical_status=(res.get("clinicalStatus", {}).get("coding", [{}])[0].get("code")),
                        source_resource=f"Condition/{res.get('id')}",
                    )
                )
        except FhirRequestError as exc:
            partial_failures.append(f"Condition: {exc.detail_code}")

        medications: list[MedicationFact] = []
        try:
            bundle = medication_future.result()
            for res in bundle_entries(bundle):
                text, _ = _codeable_concept_text(res.get("medicationCodeableConcept"))
                dosage = res.get("dosageInstruction") or [{}]
                medications.append(
                    MedicationFact(
                        text=text,
                        dosage_text=dosage[0].get("text"),
                        status=res.get("status"),
                        source_resource=f"MedicationRequest/{res.get('id')}",
                    )
                )
        except FhirRequestError as exc:
            partial_failures.append(f"MedicationRequest: {exc.detail_code}")

        allergies: list[AllergyFact] = []
        try:
            bundle = allergy_future.result()
            for res in bundle_entries(bundle):
                text, is_coded = _codeable_concept_text(res.get("code"))
                reaction = None
                if not is_coded:
                    narrative = (res.get("text") or {}).get("div")
                    if narrative:
                        text = _strip_html(narrative)
                reactions = res.get("reaction") or []
                if reactions:
                    manifestations = reactions[0].get("manifestation") or []
                    if manifestations:
                        reaction, _ = _codeable_concept_text(manifestations[0])
                allergies.append(
                    AllergyFact(
                        text=text,
                        reaction=reaction,
                        is_coded=is_coded,
                        source_resource=f"AllergyIntolerance/{res.get('id')}",
                    )
                )
        except FhirRequestError as exc:
            partial_failures.append(f"AllergyIntolerance: {exc.detail_code}")

        duplicate_warnings = duplicates_future.result()

    return GetPatientSnapshotOutput(
        patient_id=params.patient_id,
        name=name,
        birth_date=birth_date,
        gender=gender,
        conditions=conditions,
        medications=medications,
        allergies=allergies,
        chart_is_empty=not (conditions or medications or allergies),
        duplicate_warnings=duplicate_warnings,
        partial_failures=partial_failures,
    )


def check_allergy_conflict(
    fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord]
) -> CheckAllergyConflictOutput | ToolFailure:
    # turn_records: unused, see get_patient_snapshot's comment above.
    try:
        params = CheckAllergyConflictInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="check_allergy_conflict",
            reason="I couldn't process that request -- missing a patient ID or medication name.",
            detail_code="invalid_input",
        )

    try:
        # Hard bypass, not a config default: a stale allergy-conflict result
        # is one of the worst possible failure modes in this system, so this
        # fetch always goes to search_fresh(), never the conversation cache
        # get_patient_snapshot's own AllergyIntolerance fetch may have
        # populated (2026-09-19 caching design).
        bundle = fhir.search_fresh("AllergyIntolerance", {"patient": params.patient_id})
    except FhirRequestError as exc:
        return ToolFailure(
            tool="check_allergy_conflict",
            reason=f"I couldn't retrieve this patient's allergy list ({exc.detail_code}), "
            "so I can't confirm whether this medication is safe to give.",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )

    entries = bundle_entries(bundle)
    med_raw = params.medication_name.strip().lower()
    # Sulfate/sulfite/sulfonate forms are removed so "sulfa" can never substring-match "morphine sulfate"
    # (app/clinical_reference.py); the raw text is the fallback if that would leave nothing.
    med_norm = normalize_for_match(med_raw) or med_raw
    matched_text: str | None = None
    matched_cross_reactive_class: str | None = None
    low_confidence = False
    source_resources: list[str] = []

    for res in entries:
        text, is_coded = _codeable_concept_text(res.get("code"))
        if not is_coded:
            narrative = (res.get("text") or {}).get("div")
            if narrative:
                text = _strip_html(narrative)
        allergy_raw = text.strip().lower()
        if not allergy_raw or allergy_raw == "unknown":
            continue
        allergy_norm = normalize_for_match(allergy_raw) or allergy_raw
        # (a) Simple substring match against the allergy text itself.
        # (b) A small, curated drug-class cross-reactivity table
        # (app/clinical_reference.py) -- e.g. "amoxicillin" against a
        # documented "penicillin" allergy. That table is a scoped stand-in
        # for a production drug-interaction database (First Databank/
        # Medi-Span/Multum-style), not general clinical decision support;
        # see its module docstring. Neither (a) nor (b) is a drug-class
        # knowledge base beyond what's curated there -- a class this table
        # doesn't cover is still a known limitation, not assumed away.
        direct_match = med_norm in allergy_norm or allergy_norm in med_norm
        matched_class = None if direct_match else cross_reactive_class(allergy_norm, med_norm)
        if direct_match or matched_class:
            matched_text = text
            matched_cross_reactive_class = matched_class
            if not is_coded:
                low_confidence = True
            source_resources.append(f"AllergyIntolerance/{res.get('id')}")

    # A drug the curated table doesn't know, that also didn't name-match any recorded allergy, was never really
    # evaluated: only exact-name matching was possible, and cross-reactivity (a brand name, a drug class) is
    # exactly what it can't see. Say so explicitly rather than returning a "no conflict" that reads as safe.
    assessment = "conflict" if matched_text is not None else "no_conflict"
    not_assessed_reason: str | None = None
    if matched_text is None and entries and not drug_classes_for(params.medication_name):
        assessment = "not_assessed"
        not_assessed_reason = (
            f"{params.medication_name} is not in the curated drug-class table and its name does not match any "
            f"of the {len(entries)} recorded allergies, so cross-reactivity could not be evaluated"
        )

    return CheckAllergyConflictOutput(
        patient_id=params.patient_id,
        medication_name=params.medication_name,
        conflict_found=matched_text is not None,
        assessment=assessment,
        not_assessed_reason=not_assessed_reason,
        matched_allergy_text=matched_text,
        cross_reactive_class=matched_cross_reactive_class,
        checked_allergy_count=len(entries),
        low_confidence=low_confidence,
        source_resources=source_resources,
    )


# Every real user of get_recent_encounters maps to USERS.md's single
# overnight cross-covering resident persona, which ARCHITECTURE.md 3.3
# confirms never holds a High-sensitivity grant (audit-notes.md's gacl
# matrix -- the clin/physician-covering roles this persona maps to hold no
# High grant). Hard-coded here, not looked up, because no endpoint a
# resident's own user/-scoped token can call exposes their ACL role in this
# build: OpenEMR's OIDC discovery document advertises a `/userinfo`
# endpoint, but no handler for it actually exists (confirmed by reading
# AuthorizationController.php); the standard REST API's only user-lookup
# route (`GET /api/user*`) requires `admin/users` ACL, which this persona
# doesn't hold either. "clin" is therefore the correct behavior for every
# real user of this tool today, not an approximation standing in for a
# missing lookup -- if a broader-clearance persona is ever added, resolving
# the role per-session becomes a real gap to close, not before.
_RESIDENT_ROLE: Role = "clin"


def get_recent_encounters(
    fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord], *, bypass_cache: bool = False
) -> GetRecentEncountersOutput | ToolFailure:
    # turn_records: unused, see get_patient_snapshot's comment above.
    # bypass_cache: see get_patient_snapshot's comment above -- applies only
    # to the Encounter search below, not the sensitivity lookup, which
    # never caches regardless (app/fhir_client.py's CachingFhirClient.
    # get_standard_api() docstring explains why that one has no toggle at
    # all, hard-coded rather than convention-based).
    fetch = fhir.search_fresh if bypass_cache else fhir.search
    try:
        params = GetRecentEncountersInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_recent_encounters",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    try:
        bundle = fetch("Encounter", {"patient": params.patient_id})
    except FhirRequestError as exc:
        return ToolFailure(
            tool="get_recent_encounters",
            reason=f"I couldn't retrieve this patient's encounter history ({exc.detail_code}).",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )
    fhir_encounters = bundle_entries(bundle)

    # The FHIR Encounter resource carries no sensitivity field at all
    # (confirmed by code read and a live test -- ARCHITECTURE.md 3.3,
    # architecture-audit.md 6.9), so sensitivity is sourced from OpenEMR's
    # own standard REST API instead -- still "OpenEMR's REST/FHIR API" per
    # ARCHITECTURE.md 1.3, not a direct-database read (app/fhir_client.py's
    # get_standard_api()). Always fresh -- never cached, see that method's
    # own docstring.
    partial_failures: list[str] = []
    sensitivity_by_uuid: dict[str, str] = {}
    try:
        standard_encounters = fhir.get_standard_api(f"/patient/{params.patient_id}/encounter")
        if isinstance(standard_encounters, list):
            for enc in standard_encounters:
                euuid = enc.get("uuid")
                if euuid:
                    sensitivity_by_uuid[euuid] = (enc.get("sensitivity") or "").lower()
    except FhirRequestError as exc:
        partial_failures.append(f"sensitivity lookup: {exc.detail_code}")

    # Unknown sensitivity (the lookup above failed entirely, or this
    # specific encounter has no matching standard-API row) is treated as
    # "high" -- fail closed. This is the opposite failure mode from
    # get_patient_snapshot's other sub-fetches, which surface partial data
    # on failure: ARCHITECTURE.md 3.3's compensating control exists
    # specifically to fail closed, not open, so an unreadable sensitivity
    # value must exclude an encounter, never include it by default.
    raw_for_filter = [
        {"id": res.get("id"), "sensitivity": sensitivity_by_uuid.get(res.get("id"), "high")}
        for res in fhir_encounters
        if res.get("id")
    ]
    allowed_ids = {e["id"] for e in filter_encounters_by_sensitivity(raw_for_filter, _RESIDENT_ROLE)}

    encounters: list[EncounterFact] = []
    for res in fhir_encounters:
        if res.get("id") not in allowed_ids:
            continue
        type_concepts = res.get("type") or []
        text, _ = _codeable_concept_text(type_concepts[0]) if type_concepts else ("unknown", False)
        if text == "unknown":
            reason_codes = res.get("reasonCode") or []
            if reason_codes:
                text, _ = _codeable_concept_text(reason_codes[0])
        encounters.append(
            EncounterFact(
                text=text,
                status=res.get("status"),
                period_start=(res.get("period") or {}).get("start"),
                source_resource=f"Encounter/{res.get('id')}",
            )
        )

    return GetRecentEncountersOutput(
        patient_id=params.patient_id,
        encounters=encounters,
        sensitivity_filtered_count=len(fhir_encounters) - len(encounters),
        partial_failures=partial_failures,
    )


def _observation_value_text(res: dict) -> str | None:
    """Extracts a human-readable value from a FHIR Observation's
    polymorphic `value[x]` -- quantity (with unit), string, CodeableConcept,
    or (for panel-style observations like blood pressure) a `component`
    array of sub-measurements, each with its own code + value. None if no
    value is present at all (e.g. a pending/cancelled result) -- callers
    must not treat that as "value 0" or synthesize a placeholder; an
    ObservationFact with value=None and a real status is the honest
    representation of that case."""
    quantity = res.get("valueQuantity")
    if quantity and quantity.get("value") is not None:
        unit = quantity.get("unit") or quantity.get("code") or ""
        return f"{quantity['value']} {unit}".strip()
    if res.get("valueString"):
        return res["valueString"]
    concept_text, is_coded = _codeable_concept_text(res.get("valueCodeableConcept"))
    if is_coded:
        return concept_text
    components = res.get("component") or []
    parts: list[str] = []
    for comp in components:
        comp_text, _ = _codeable_concept_text(comp.get("code"))
        comp_quantity = comp.get("valueQuantity")
        if comp_quantity and comp_quantity.get("value") is not None:
            unit = comp_quantity.get("unit") or comp_quantity.get("code") or ""
            parts.append(f"{comp_text}: {comp_quantity['value']} {unit}".strip())
    return ", ".join(parts) if parts else None


def get_recent_observations(
    fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord], *, bypass_cache: bool = False
) -> GetRecentObservationsOutput | ToolFailure:
    """UC2 (USERS.md): "what changed" for labs/vitals -- the same sign-out-
    verification role get_recent_encounters plays for visit history.
    Deliberately NOT run through app/sensitivity.py's compensating filter;
    see GetRecentObservationsOutput's docstring in schemas.py for why that
    control (scoped specifically to Encounter data) doesn't apply here.

    turn_records: unused, see get_patient_snapshot's comment above.
    bypass_cache: see get_patient_snapshot's comment above. Observations get
    zero blind-trust window regardless (2026-09-19 caching design,
    app/fhir_client.py's _TRUST_WINDOW_SECONDS) -- labs/vitals are the most
    dangerous category to serve stale, so every reuse pays for the cheap
    since-check before this call would ever be served from cache at all;
    this flag only matters for the harder guarantee compare_signout_to_chart
    needs (skip the cache and its since-check entirely).
    """
    fetch = fhir.search_fresh if bypass_cache else fhir.search
    try:
        params = GetRecentObservationsInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_recent_observations",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    try:
        bundle = fetch("Observation", {"patient": params.patient_id})
    except FhirRequestError as exc:
        return ToolFailure(
            tool="get_recent_observations",
            reason=f"I couldn't retrieve this patient's recent labs/vitals ({exc.detail_code}).",
            detail_code=exc.detail_code,  # type: ignore[arg-type]
        )

    observations: list[ObservationFact] = []
    for res in bundle_entries(bundle):
        text, _ = _codeable_concept_text(res.get("code"))
        observations.append(
            ObservationFact(
                text=text,
                value=_observation_value_text(res),
                status=res.get("status"),
                effective_datetime=res.get("effectiveDateTime")
                or (res.get("effectivePeriod") or {}).get("start"),
                source_resource=f"Observation/{res.get('id')}",
            )
        )

    return GetRecentObservationsOutput(patient_id=params.patient_id, observations=observations)


def summarize_shift_events(
    fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord]
) -> SummarizeShiftEventsOutput | ToolFailure:
    """UC4 (USERS.md): synthesizes a shift-handoff summary purely from facts
    already gathered THIS conversation (turn_records -- accumulated across
    both this turn's own tool-call rounds and any earlier turns, see
    ClinicalCopilotAgent.run_turn's prior_tool_records docstring in
    app/agent.py). Makes NO new FHIR call -- `fhir` is accepted, unused, only
    for the uniform impl(fhir, tool_input, turn_records) dispatch signature
    every tool now shares (ClinicalCopilotAgent._call_tool, app/agent.py).

    That signature was originally special-cased just for this tool (the only
    one needing turn_records at the time); unified to a single shape once
    compare_signout_to_chart needed BOTH fhir (it does a fresh, unconditional
    fetch -- see its own docstring) and turn_records, rather than growing a
    second special case in _call_tool for every new shape a tool might need.

    Purely deterministic aggregation, no generation: this function never
    calls the model. The actual narrative synthesis into shift-summary
    prose happens the same place every other tool's output becomes prose --
    the main model turn, whose draft still passes through
    verify_response()'s existing grounding check. Keeping this tool
    mechanical means no new verification machinery is needed here; a nested
    LLM call inside a tool would draft text nothing in this architecture
    ever checks (verify_response only scans the FINAL response text, never
    an intermediate tool output).

    Because the compensating sensitivity filter (app/sensitivity.py) already
    ran inside get_recent_encounters before its output was ever added to
    turn_records, a filtered-out encounter is structurally absent from what
    this function reads -- no separate filtering step needed here. That
    reasoning holds only because this tool has no FHIR access of its own;
    if it ever gained one, it would need re-verifying, not assuming.
    """
    try:
        params = SummarizeShiftEventsInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="summarize_shift_events",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    patient_records = [rec for rec in turn_records if rec.patient_id == params.patient_id]
    if not patient_records:
        return SummarizeShiftEventsOutput(patient_id=params.patient_id, events=[], data_gathered=False)

    events: list[ShiftEventFact] = []
    for rec in patient_records:
        out = rec.output
        if isinstance(out, GetPatientSnapshotOutput):
            for c in out.conditions:
                events.append(ShiftEventFact(text=c.text, category="condition", source_resource=c.source_resource))
            for m in out.medications:
                events.append(ShiftEventFact(text=m.text, category="medication", source_resource=m.source_resource))
            for a in out.allergies:
                events.append(ShiftEventFact(text=a.text, category="allergy", source_resource=a.source_resource))
            for d in out.duplicate_warnings:
                events.append(
                    ShiftEventFact(
                        text=f"Possible duplicate record: {d.other_patient_id} (matched on {d.matched_on})",
                        category="duplicate_warning",
                        source_resource=f"Patient/{d.other_patient_id}",
                    )
                )
        elif isinstance(out, GetRecentEncountersOutput):
            for enc in out.encounters:
                events.append(ShiftEventFact(text=enc.text, category="encounter", source_resource=enc.source_resource))
        elif isinstance(out, GetRecentObservationsOutput):
            for obs in out.observations:
                text = f"{obs.text}: {obs.value}" if obs.value else obs.text
                events.append(ShiftEventFact(text=text, category="observation", source_resource=obs.source_resource))
        elif isinstance(out, CheckAllergyConflictOutput):
            if out.conflict_found:
                events.append(
                    ShiftEventFact(
                        text=f"Allergy conflict checked and found: {out.medication_name} vs {out.matched_allergy_text}",
                        category="allergy",
                        source_resource=(out.source_resources[0] if out.source_resources else "AllergyIntolerance/unknown"),
                    )
                )

    # De-dupe by (category, text) -- the same fact can legitimately be
    # gathered more than once this conversation (e.g. get_patient_snapshot
    # called in an earlier turn and again in this one), and a shift summary
    # shouldn't repeat it per gathering call.
    seen: set[tuple[str, str]] = set()
    deduped: list[ShiftEventFact] = []
    for event in events:
        key = (event.category, event.text.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(event)

    return SummarizeShiftEventsOutput(patient_id=params.patient_id, events=deduped, data_gathered=True)


def _diff_by_source_resource(old_items: list, new_items: list, category: str, text_fn=None) -> list[ChartDiscrepancy]:
    """Set-diffs two lists of Fact-shaped objects (each with .source_resource,
    identified per resource type by FHIR id) by that id. Returns a
    ChartDiscrepancy for every new_items entry whose id wasn't present in
    old_items -- a purely structural comparison (presence/absence by id),
    no interpretation of what a new entry clinically means."""
    text_fn = text_fn or (lambda item: item.text)
    old_ids = {item.source_resource for item in old_items}
    return [
        ChartDiscrepancy(category=category, text=f"{text_fn(item)} (new since last check)", source_resource=item.source_resource)
        for item in new_items
        if item.source_resource not in old_ids
    ]


def _diff_medication_status(old_meds: list[MedicationFact], new_meds: list[MedicationFact]) -> list[ChartDiscrepancy]:
    """Like _diff_by_source_resource, but also flags a status change (e.g.
    active -> stopped) for a medication present in both -- the one place a
    same-id comparison matters more than presence/absence for this tool's
    purpose (a sign-out claim about an active medication can go stale by
    the medication being discontinued, not just by a new one appearing)."""
    old_by_id = {m.source_resource: m for m in old_meds}
    discrepancies: list[ChartDiscrepancy] = []
    for m in new_meds:
        old = old_by_id.get(m.source_resource)
        if old is None:
            discrepancies.append(
                ChartDiscrepancy(category="medication", text=f"{m.text} (new since last check)", source_resource=m.source_resource)
            )
        elif old.status != m.status:
            discrepancies.append(
                ChartDiscrepancy(
                    category="medication",
                    text=f"{m.text}: {old.status} -> {m.status}",
                    source_resource=m.source_resource,
                )
            )
    return discrepancies


def compare_signout_to_chart(
    fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord]
) -> CompareSignoutToChartOutput | ToolFailure:
    """UC2 (USERS.md): "has something changed since sign-out was written" --
    verifying a conditional sign-out instruction ("if potassium is high,
    give X") against the real, current chart.

    Deliberately does NOT take the sign-out's own text as input, and does
    not attempt to parse or semantically compare it. Interpreting free text
    into a structured claim is inherently a natural-language judgment call;
    putting that interpretation inside this tool would mean either a
    second, nested LLM call whose output nothing in this architecture
    checks (verify_response() only scans the FINAL response text, never an
    intermediate tool output -- the exact risk named and avoided in
    summarize_shift_events' design), or a brittle hand-rolled parser that
    can't handle real sign-out prose. Neither is built here.

    Instead: this function does a FRESH, UNCONDITIONAL re-fetch -- by
    design, this tool's whole value is guaranteeing freshness, unlike
    summarize_shift_events, which deliberately reuses already-gathered
    data -- and structurally diffs it against whatever this conversation
    already knew (turn_records) for this patient. The sign-out's actual
    prose never enters this function at all; it stays in the resident's
    own message, already in the outer model's context. The model connects
    "sign-out said X" (its own context) to "here's what changed" (this
    tool's structured diff) in its final drafted response, the same
    division of labor every other tool uses, so that response still passes
    through verify_response()'s existing grounding check with no new
    verification machinery needed.

    baseline_established=False means turn_records had nothing for this
    patient yet -- this fetch becomes the new baseline, not a confirmed
    "nothing has changed." What "already knew" means here is itself a
    named approximation: this system has no record of the actual moment
    sign-out was written, so "what this conversation already fetched" is
    the best available proxy for it, not a full solution to comparing
    against sign-out time specifically.

    Delegates its fresh fetches to get_patient_snapshot/get_recent_
    encounters/get_recent_observations directly, rather than re-fetching
    raw FHIR itself -- reuses their tested parsing logic, and inherits
    get_recent_encounters' compensating sensitivity filter for free in the
    process (that filter runs inside get_recent_encounters itself, before
    this function ever sees its output).

    Passes bypass_cache=True into all three (2026-09-19 caching design):
    this tool's entire documented value above is guaranteeing freshness to
    diff against. If its "current" fetch used the conversation cache
    instead, the diff would compare cache to cache and structurally report
    zero discrepancies every time -- not just a staleness risk, an outright
    correctness bug that silently defeats this tool. Hard rule, not a
    tunable default.
    """
    try:
        params = CompareSignoutToChartInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="compare_signout_to_chart",
            reason="I couldn't process that patient reference -- it wasn't a valid patient ID.",
            detail_code="invalid_input",
        )

    # from_prior_turn=True only -- fixed 2026-09-22, a real bug found live:
    # turn_records also carries records from EARLIER ROUNDS OF THIS SAME
    # TURN (e.g. the model calling get_patient_snapshot in round 0, then
    # compare_signout_to_chart in round 1). That's not a genuine baseline --
    # it's diffing "now" against "a few seconds ago", which trivially shows
    # zero discrepancies and produces exactly the false confidence this
    # tool exists to prevent. Whether this fires was previously determined
    # by the MODEL's own, non-deterministic choice of tool-calling order,
    # not by whether this conversation genuinely has earlier-turn history --
    # live-confirmed: the identical first message of a fresh conversation
    # produced baseline_established=True on some calls and False on others,
    # purely from tool-call ordering. Only a record inherited from an
    # earlier turn (run_turn's prior_tool_records, real separation in time)
    # counts now.
    prior_snapshot = next(
        (
            r.output
            for r in turn_records
            if r.patient_id == params.patient_id and r.from_prior_turn
            and isinstance(r.output, GetPatientSnapshotOutput)
        ),
        None,
    )
    prior_encounters = next(
        (
            r.output
            for r in turn_records
            if r.patient_id == params.patient_id and r.from_prior_turn
            and isinstance(r.output, GetRecentEncountersOutput)
        ),
        None,
    )
    prior_observations = next(
        (
            r.output
            for r in turn_records
            if r.patient_id == params.patient_id and r.from_prior_turn
            and isinstance(r.output, GetRecentObservationsOutput)
        ),
        None,
    )
    baseline_established = any([prior_snapshot, prior_encounters, prior_observations])

    fresh_snapshot = get_patient_snapshot(fhir, {"patient_id": params.patient_id}, turn_records, bypass_cache=True)
    fresh_encounters = get_recent_encounters(fhir, {"patient_id": params.patient_id}, turn_records, bypass_cache=True)
    fresh_observations = get_recent_observations(
        fhir, {"patient_id": params.patient_id}, turn_records, bypass_cache=True
    )

    partial_failures: list[str] = []
    discrepancies: list[ChartDiscrepancy] = []

    if isinstance(fresh_snapshot, ToolFailure):
        partial_failures.append(f"snapshot: {fresh_snapshot.detail_code}")
        fresh_snapshot = None
    elif prior_snapshot:
        discrepancies += _diff_by_source_resource(prior_snapshot.conditions, fresh_snapshot.conditions, "condition")
        discrepancies += _diff_medication_status(prior_snapshot.medications, fresh_snapshot.medications)
        discrepancies += _diff_by_source_resource(prior_snapshot.allergies, fresh_snapshot.allergies, "allergy")

    if isinstance(fresh_encounters, ToolFailure):
        partial_failures.append(f"encounters: {fresh_encounters.detail_code}")
        fresh_encounters = None
    elif prior_encounters:
        discrepancies += _diff_by_source_resource(prior_encounters.encounters, fresh_encounters.encounters, "encounter")

    if isinstance(fresh_observations, ToolFailure):
        partial_failures.append(f"observations: {fresh_observations.detail_code}")
        fresh_observations = None
    elif prior_observations:
        discrepancies += _diff_by_source_resource(
            prior_observations.observations,
            fresh_observations.observations,
            "observation",
            text_fn=lambda o: f"{o.text}: {o.value}" if o.value else o.text,
        )

    return CompareSignoutToChartOutput(
        patient_id=params.patient_id,
        baseline_established=baseline_established,
        discrepancies=discrepancies,
        current_snapshot=fresh_snapshot,
        current_encounters=fresh_encounters,
        current_observations=fresh_observations,
        partial_failures=partial_failures,
    )


# --- get_uploaded_documents (W2 PRD Section 4.2 -- chart-data-worker's 7th
# tool) --------------------------------------------------------------------


def _flatten_extraction_facts(result: ExtractionResult) -> list[UploadedDocumentFactSummary]:
    """Every ExtractedField on a stored ExtractionResult, flattened into
    one list -- one summary per field, each carrying its own confidence
    and a source_resource identifying exactly which document/field it
    came from (this tool's own grounding-vocabulary contribution,
    app/verification.py's _grounded_vocabulary).

    doc_ref is deliberately result.doc_id (the ORIGINAL source scan's id,
    category 35), not extracted_doc_id (category 36) -- found live
    2026-09-24: GET /documents/preview (app/ingestion/storage.py's
    get_source_document_bytes) resolves a document by searching for the
    ORIGINAL scan's filename only; extracted_doc_id names a different
    stored file it can never find. Both this tool's citations
    (app/graph/citations.py's chart_citations) and document_citations()'s
    same-turn citations now consistently use doc_id, so click-to-source
    actually resolves on both paths, not just one that happens to render
    clickable."""
    doc_ref = result.doc_id
    facts: list[UploadedDocumentFactSummary] = []
    if isinstance(result.extraction, LabPdfExtraction):
        for i, lab in enumerate(result.extraction.results):
            for field_name in ("test_name", "value", "unit", "reference_range", "collection_date", "abnormal_flag"):
                field = getattr(lab, field_name)
                facts.append(
                    UploadedDocumentFactSummary(
                        field_name=f"results[{i}].{field_name}",
                        value=field.value,
                        confidence=field.confidence,
                        source_resource=f"document:{doc_ref}#results[{i}].{field_name}",
                        page_number=field.citation.page_number,
                    )
                )
    elif isinstance(result.extraction, IntakeFormExtraction):
        demo = result.extraction.demographics
        for demo_field_name in ("name", "date_of_birth", "sex"):
            field = getattr(demo, demo_field_name)
            if field is not None:
                facts.append(
                    UploadedDocumentFactSummary(
                        field_name=f"demographics.{demo_field_name}",
                        value=field.value,
                        confidence=field.confidence,
                        source_resource=f"document:{doc_ref}#demographics.{demo_field_name}",
                        page_number=field.citation.page_number,
                    )
                )
        if result.extraction.chief_concern is not None:
            facts.append(
                UploadedDocumentFactSummary(
                    field_name="chief_concern",
                    value=result.extraction.chief_concern.value,
                    confidence=result.extraction.chief_concern.confidence,
                    source_resource=f"document:{doc_ref}#chief_concern",
                    page_number=result.extraction.chief_concern.citation.page_number,
                )
            )
        for list_field_name in ("current_medications", "allergies", "family_history"):
            for i, field in enumerate(getattr(result.extraction, list_field_name)):
                facts.append(
                    UploadedDocumentFactSummary(
                        field_name=f"{list_field_name}[{i}]",
                        value=field.value,
                        confidence=field.confidence,
                        source_resource=f"document:{doc_ref}#{list_field_name}[{i}]",
                        page_number=field.citation.page_number,
                    )
                )
    return facts


def get_uploaded_documents(fhir: FhirClient, raw_input: dict, turn_records: list[ToolCallRecord]):
    """Reads back every previously-stored document extraction for this
    patient (app/ingestion/storage.py's list_extracted_documents) -- never
    re-runs OCR on a later turn (PRD Section 4.2). Every fact returned
    here is unreconciled, AI-Extracted-Pending-Review-bucketed data;
    app/verification.py's hard-rule enforcement requires any response
    drawing on it to say so explicitly."""
    # turn_records: unused, same as get_patient_snapshot/check_allergy_conflict above.
    try:
        params = GetUploadedDocumentsInput.model_validate(raw_input)
    except Exception:
        return ToolFailure(
            tool="get_uploaded_documents",
            reason="I couldn't process that request -- missing a patient ID.",
            detail_code="invalid_input",
        )

    try:
        results, partial_failures, omitted_count = list_extracted_documents(fhir, params.patient_id)
    except FhirRequestError as exc:
        return ToolFailure(
            tool="get_uploaded_documents",
            reason="Could not retrieve previously uploaded documents for this patient.",
            detail_code=exc.detail_code if exc.detail_code in ("timeout", "http_error", "not_found") else "http_error",
        )

    # results is already sorted most-recently-uploaded-first
    # (list_extracted_documents' own docstring) -- enumerate() directly
    # gives each document's upload_rank without a second sort.
    documents = [
        UploadedDocumentSummary(
            doc_id=r.doc_id,
            doc_type=r.doc_type,
            facts=_flatten_extraction_facts(r),
            ocr_avg_confidence=r.ocr_avg_confidence,
            uploaded_on=r.uploaded_on,
            upload_rank=i,
        )
        for i, r in enumerate(results)
    ]
    # partial_failures: a document genuinely on file that couldn't be read
    # back must never present as "no documents on file" -- see
    # list_extracted_documents' own docstring for the live gap this closes.
    # omitted_count: documents beyond MAX_UPLOADED_DOCUMENTS_RETURNED that
    # were never fetched at all -- app/verification.py enforces that a
    # response drawing on this tool discloses the omission when nonzero.
    return GetUploadedDocumentsOutput(
        patient_id=params.patient_id,
        documents=documents,
        partial_failures=partial_failures,
        omitted_count=omitted_count,
    )
