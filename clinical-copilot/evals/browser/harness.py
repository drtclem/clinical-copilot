"""Offline browser harness for the citation cards UI: runs the real FastAPI
app and the real citation builders with a fake FHIR client and a stubbed
graph turn. No OpenEMR, no Anthropic, no network beyond localhost.

Not part of the test suite or CI. See README.md in this directory.

Serves http://127.0.0.1: (default 8765). Extra routes, harness
only: /__login (sets the session cookie, redirects to /ui) and /__reset
(restarts the scripted turn counter). Turn 1 is the upload turn (same-turn
document citations + one guideline citation); turn 2 is a later turn
(citations rebuilt from get_uploaded_documents). OCR and bbox resolution
are stubbed with fixed boxes, so highlight *placement* here proves the UI
math, not OCR accuracy.
"""
import io, re, sys, uuid, os
from pathlib import Path
from types import SimpleNamespace
os.environ.update(OPENEMR_OAUTH_CLIENT_ID="x", OPENEMR_OAUTH_CLIENT_SECRET="x", OPENEMR_OAUTH_USERNAME="x",
                  OPENEMR_OAUTH_PASSWORD="x", ANTHROPIC_API_KEY="offline-dummy")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # clinical-copilot/
from PIL import Image, ImageDraw
from fastapi.responses import RedirectResponse
import app.main as m
from app.oauth_session import ResidentSession
from app.graph.build import GraphTurnResult
from app.graph import citations as C
from app.ingestion.schemas import ExtractionResult, LabPdfExtraction
from app.schemas import (AllergyFact, ConditionFact, GetPatientSnapshotOutput, GetUploadedDocumentsOutput, MedicationFact,
                         UploadedDocumentFactSummary, UploadedDocumentSummary)
from app.retrieval.chunking import load_corpus
from app.retrieval.types import RetrievedEvidence
from app.tools import ToolCallRecord
from app.graph.notices import no_guideline_match_notice
import dataclasses

PID = "11111111-1111-4111-8111-111111111111"
DOC = "a1b2c3d4e5f6.png"
NEG = "9f8e7d6c5b4a.png"
# Stored source-document ids for the scripted uploads (run_upload_check.py), by the uploaded file's name. The preview
# path must be able to resolve them, or the result card's thumbnail would show "No preview".
UPLOAD_DOC_IDS = {"slow_lab.png": "e1e1e1e1e1e1.png", "lab_many.png": "e2e2e2e2e2e2.png", "empty_fields.png": "e3e3e3e3e3e3.png",
                  "intake_ok.png": "e4e4e4e4e4e4.png", "hostile_intake.png": "e5e5e5e5e5e5.png"}

def page_png():
    img = Image.new("RGB", (850, 1100), "white"); d = ImageDraw.Draw(img)
    d.text((60, 60), "ACME CLINICAL LABORATORY", fill="black")
    d.text((60, 120), "Patient: TEST PATIENT   DOB 03/14/1972", fill="black")
    d.text((60, 300), "Potassium        7.8   mmol/L   (3.5-5.1)   HIGH", fill="black")
    d.text((60, 340), "Sodium           139   mmol/L   (135-145)", fill="black")
    b = io.BytesIO(); img.save(b, format="PNG"); return b.getvalue()

def docref(t, pid=PID, status="current"):
    return {"resourceType": "DocumentReference", "status": status, "subject": {"reference": f"Patient/{pid}"},
            "category": [{"text": "Source Document"}], "content": [{"attachment": {"title": t, "url": f"Binary/{t}"}}]}

class FakeFhir:
    def read(self, rt, rid): return {"resourceType": rt, "id": rid, "name": [{"given": ["Test"], "family": "Patient"}], "birthDate": "1972-03-14", "gender": "female"}
    def search(self, rt, params): return {"entry": [{"resource": docref(DOC)}, {"resource": docref(NEG, status="entered-in-error")}]}
    def get_standard_api(self, path):
        if "/document" not in path: return {"pid": 1}
        return [{"id": 1, "filename": DOC, "mimetype": "image/png"}]
    def get_standard_api_binary(self, path): return page_png()

class Tok:
    def get_token(self): return "t"

class UploadAwareFhir(FakeFhir):
    """FakeFhir plus the scripted uploads' source documents, on BOTH document-read paths (the FHIR DocumentReference/
    Binary pair and the Standard API listing/download), so this harness serves thumbnails whichever one the app uses."""
    def search(self, rt, params):
        entries = super().search(rt, params)["entry"] + [{"resource": docref(d)} for d in UPLOAD_DOC_IDS.values()]
        return {"entry": entries}
    def get_standard_api(self, path):
        if "/document" not in path: return {"pid": 1}
        return [{"id": 1, "filename": DOC, "mimetype": "image/png"}] + [
            {"id": 10 + i, "filename": d, "mimetype": "image/png"} for i, d in enumerate(UPLOAD_DOC_IDS.values())]
    def get_standard_api_binary(self, path): return page_png()

SID = "harness-session"
m._sessions._sessions[SID] = ResidentSession(session_id=SID, tokens=Tok(), fhir=UploadAwareFhir(), resident_username="dr.harness")

BBOX = {"7.8": (0.145, 0.270, 0.170, 0.287), "139": (0.145, 0.306, 0.170, 0.323)}
m.run_ocr = lambda content, ct: None
m.resolve_bbox = lambda ocr, page, quote: BBOX.get(quote)

def lab(value, name, unit, rng, flag, date="2026-09-24"):
    from app.ingestion.schemas import ExtractedField, DocumentCitation, LabResultFact as LabResult
    def f(v, q): return ExtractedField(value=v, confidence="high", citation=DocumentCitation(page_number=1, quote_or_value=q))
    return LabResult(test_name=f(name, name), value=f(value, value), unit=f(unit, unit), reference_range=f(rng, rng),
                     collection_date=f(date, date), abnormal_flag=f(flag, flag))

extraction = ExtractionResult(patient_id=PID, doc_id=DOC, doc_type="lab_pdf", ocr_avg_confidence=91.0, uploaded_on="2026-09-25",
    extraction=LabPdfExtraction(results=[lab("7.8", "Potassium", "mmol/L", "3.5-5.1", "high"), lab("139", "Sodium", "mmol/L", "135-145", "normal")]))

# Real corpus chunks, so titles, markdown and mid-word chunk starts are exactly what production retrieves: three
# passages of one guideline (must become ONE card) and one of another. The third hyperkalemia passage also carries
# an HTML payload mid-passage, which must show as text and never execute.
_corpus = load_corpus()
_hk = [c for c in _corpus if c.source_id == "hyperkalemia_management"]
_sepsis = [c for c in _corpus if c.source_id == "sepsis_screening"]
_PAYLOAD = "<img src=x onerror=alert(1)> <script>alert(2)</script>"
_hostile = dataclasses.replace(_hk[3], text=re.sub(r"(replacing\s+them\.)\s", lambda m: m.group(1) + " " + _PAYLOAD + " ", _hk[3].text, count=1))
assert _PAYLOAD in _hostile.text
guidelines = [RetrievedEvidence(chunk=c, rerank_score=0.9) for c in (_hk[1], _hk[2], _hostile, _sepsis[1])]

snapshot = GetPatientSnapshotOutput(
    patient_id=PID, name="Test Patient", birth_date="1972-03-14", gender="female", chart_is_empty=False, duplicate_warnings=[],
    conditions=[ConditionFact(text="COPD", category="problem-list-item", clinical_status="active", source_resource="Condition/c-copd"),
                ConditionFact(text="Type 2 diabetes mellitus", category="problem-list-item", clinical_status="active", source_resource="Condition/c-t2dm")],
    medications=[MedicationFact(text="Lisinopril 10 mg", dosage_text="daily", status="active", source_resource="MedicationRequest/m-lis")],
    allergies=[AllergyFact(text="Sulfa drugs", reaction="rash", is_coded=True, source_resource="AllergyIntolerance/a-sulfa")])
snapshot_rec = ToolCallRecord(tool_name="get_patient_snapshot", patient_id=PID, output=snapshot)

# The same page uploaded four times: four stored documents (different ids), identical extracted content. The real
# translate_citations() must collapse them into ONE card that says how many copies exist.
COPIES = [DOC, "b2c3d4e5f6a1.png", "c3d4e5f6a1b2.png", "d4e5f6a1b2c3.png"]

def _plain_turn(text, *, notices=(), enforced=(), partial=()):
    return GraphTurnResult(correlation_id=str(uuid.uuid4()), response_text=text, verification_passed=True, passed_domain_constraint=True,
        flagged_claims=[], enforced_warnings=list(enforced), workers_invoked=["evidence_retriever"], routing_rationale="harness",
        citations=[], updated_history=[], accumulated_tool_records=[], partial_failures=list(partial), notices=list(notices))

def turn(n):
    if n == 5:   # the model provider is down: friendly message + correlation id, a warning notice, no verified check
        cid = str(uuid.uuid4())
        return GraphTurnResult(correlation_id=cid,
            response_text=f"The assistant is temporarily unavailable, so I couldn't answer this. Please try again in a few minutes. If it keeps happening, quote this reference: {cid}.",
            verification_passed=True, passed_domain_constraint=True, flagged_claims=[], enforced_warnings=[], workers_invoked=[],
            routing_rationale="", citations=[], updated_history=[], accumulated_tool_records=[],
            notices=[{"worker": "supervisor", "kind": "assistant_unavailable", "severity": "warning", "label": "Assistant unavailable",
                      "text": "This question was not answered, and nothing from it was added to the conversation."}])
    if n == 3:   # a guideline search that worked but found nothing relevant: an INFO notice, not a failure
        return _plain_turn("Pancreatitis management isn't something I can ground in the reference set.", notices=[no_guideline_match_notice()])
    if n == 4:   # a REAL failure (retrieval service down): stays a warning
        msg = "evidence_retriever: retrieval service unavailable after retry -- answering without guideline evidence"
        return _plain_turn("I could not search the guidelines this turn.\n\n[Verification note: " + msg + ".]",
                           enforced=["worker_partial_failure"], partial=[msg])
    if n == 1:   # the upload turn: same-turn document_citations
        extracted = [extraction.model_copy(update={"doc_id": d}) for d in COPIES]
        cites = C.translate_citations(chart_facts=SimpleNamespace(turn_records=[snapshot_rec]), extracted_facts=extracted,
                                      retrieved_evidence=guidelines, patient_id=PID)
        text = "Uploaded lab report: potassium 7.8 mmol/L (high). Guideline: give IV calcium first."
        records = [snapshot_rec]
    else:        # a later turn: chart_citations from get_uploaded_documents
        def summary(doc, rank):
            return UploadedDocumentSummary(doc_id=doc, doc_type="lab_pdf", ocr_avg_confidence=91.0, uploaded_on="2026-09-25", upload_rank=rank,
                facts=[UploadedDocumentFactSummary(field_name=f"results[0].{k}", value=v, confidence="high", source_resource=f"document:{doc}#results[0].{k}", page_number=1)
                       for k, v in (("test_name","Potassium"),("value","7.8"),("unit","mmol/L"),("reference_range","3.5-5.1"),("collection_date","2026-09-24"),("abnormal_flag","high"))])
        docs = GetUploadedDocumentsOutput(patient_id=PID, documents=[summary(d, i) for i, d in enumerate(COPIES)])
        rec = ToolCallRecord(tool_name="get_uploaded_documents", patient_id=PID, output=docs)
        cites = C.translate_citations(chart_facts=SimpleNamespace(turn_records=[snapshot_rec, rec]), extracted_facts=[],
                                      retrieved_evidence=[], patient_id=PID)
        text = "The most recent uploaded lab shows potassium 7.8 mmol/L."
        records = [snapshot_rec, rec]
    return GraphTurnResult(correlation_id=str(uuid.uuid4()), response_text=text, verification_passed=True, passed_domain_constraint=True,
        flagged_claims=[], enforced_warnings=[], workers_invoked=["chart_data_worker"], routing_rationale="harness",
        citations=cites, updated_history=[], accumulated_tool_records=records)

calls = {"n": 0}
turn_refs = []   # the uploaded_document_ref each answered chat turn actually received server-side, in order
def fake_run_turn(**kw):
    calls["n"] += 1
    turn_refs.append(kw.get("uploaded_document_ref"))
    return turn(calls["n"])
m.graph_run_turn = fake_run_turn

@m.app.get("/__login")
def _login():
    r = RedirectResponse(f"/ui?patient={PID}"); r.set_cookie("copilot_session", SID); return r

@m.app.get("/__reset")
def _reset():
    calls["n"] = 0; turn_refs.clear(); return {"ok": True}

@m.app.get("/__turn_refs")
def _turn_refs():
    return turn_refs

# ---- scripted POST /documents outcomes for run_upload_check.py, keyed by the uploaded file's name. attach_and_extract
# is replaced wholesale (no OCR, no LLM, no OpenEMR); the route, form parsing, the size cap and the JSON envelope are real.
import time
from app.ingestion.schemas import (DemographicsFact, DocumentCitation, DocumentIngestionFailure, ExtractedField,
                                   IntakeFormExtraction, LabResultFact)

def ef(value, conf="high", page=1, quote=None):
    return ExtractedField(value=value, confidence=conf, citation=DocumentCitation(page_number=page, quote_or_value=quote or value))

def _lab(name, value, unit, rng, flag, conf="high"):
    return LabResultFact(test_name=ef(name), value=ef(value, conf), unit=ef(unit), reference_range=ef(rng),
                         collection_date=ef("2026-09-24"), abnormal_flag=ef(flag))

_HOSTILE = "<img src=x onerror=alert(1)><script>alert(2)</script>"

def _result(name, doc_type, extraction):
    return ExtractionResult(patient_id=PID, doc_id=UPLOAD_DOC_IDS[name], doc_type=doc_type, extraction=extraction,
                            ocr_avg_confidence=88.0, uploaded_on="2026-09-25")

def fake_attach(patient_id, file_bytes, filename, content_type, doc_type, **kw):
    if filename == "toomany.pdf":
        return None, DocumentIngestionFailure(reason="This document has 9 pages; the limit is 5.", detail_code="too_many_pages"), 0, 0
    if filename == "dup.png":
        return None, DocumentIngestionFailure(reason="Already stored.", detail_code="duplicate"), 0, 0
    if filename in ("slow_lab.png", "cancel_me.png"):
        time.sleep(1.5)   # long enough to observe (and cancel during) the reading phase
    if filename in ("slow_lab.png", "cancel_me.png"):
        name = "slow_lab.png"
        return _result(name, "lab_pdf", LabPdfExtraction(results=[
            _lab("Potassium", "7.8", "mmol/L", "3.5-5.1", "high"),
            _lab("Sodium", "139", "mmol/L", "135-145", "normal", conf="low")])), None, 0, 0
    if filename == "lab_many.png":
        results = [_lab(f"Analyte {i}", str(i), "u", "0-9", "normal") for i in range(1, 8)]
        return _result(filename, "lab_pdf", LabPdfExtraction(results=results)), None, 0, 0
    if filename == "empty_fields.png":
        return _result(filename, "lab_pdf", LabPdfExtraction(results=[])), None, 0, 0
    # A browser percent-encodes '"' in a multipart filename, so the hostile name is recognized by its payload.
    if filename in ("intake_ok.png", "hostile_intake.png") or "onerror" in filename:
        hostile = filename != "intake_ok.png"
        name = "hostile_intake.png" if hostile else filename
        payload = _HOSTILE if hostile else ""
        ext = IntakeFormExtraction(
            demographics=DemographicsFact(name=ef("Test Patient" + payload), date_of_birth=ef("1972-03-14"), sex=ef("F", "unreadable")),
            chief_concern=ef("Shortness of breath" + payload),
            current_medications=[ef("Lisinopril 10 mg"), ef("Albuterol inhaler" + payload)],
            allergies=[ef("Sulfa drugs")], family_history=[ef("Father: MI at 55")])
        return _result("hostile_intake.png", "intake_form", ext), None, 0, 0
    return None, DocumentIngestionFailure(reason="Unscripted file in the harness.", detail_code="ocr_failed"), 0, 0

m.attach_and_extract = fake_attach

if __name__ == "__main__":
    import uvicorn; uvicorn.run(m.app, host="127.0.0.1", port=int(os.environ.get("HARNESS_PORT", "8765")), log_level="warning")
