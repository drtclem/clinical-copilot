# Clinical Co-Pilot -- Multimodal Evidence Agent (Week 1 + Week 2)

This document covers two layers. Sections and headers below are labeled **(Week 1)** or
**(Week 2)** where it isn't obvious from context.

| Area | Week 1 baseline | Week 2 additions |
|---|---|---|
| Agent | One tool-calling chat agent (`ClinicalCopilotAgent`, `app/agent.py`) behind a swappable `LLMClient` (`app/llm_client.py`), validated only against Anthropic Claude | LangGraph supervisor (`app/graph/`) that routes each turn to `chart_data_worker`, `intake_extractor` and/or `evidence_retriever`, then synthesizes one answer |
| Sources | OpenEMR chart over FHIR, via six tools: `get_patient_snapshot`, `check_allergy_conflict`, `get_recent_encounters`, `get_recent_observations`, `summarize_shift_events`, `compare_signout_to_chart` | Uploaded documents (lab PDF, intake form) and a 4-document guideline corpus |
| Document ingestion | — | `attach_and_extract`: Tesseract OCR, then an Anthropic structuring call into strict Pydantic schemas, with a numeric-plausibility downgrade. Stored as the source document plus a JSON document in OpenEMR's "AI-Extracted Pending Review" category |
| Retrieval | — | Hybrid retrieval with cross-encoder rerank, a calibrated relevance floor, and a PHI-free query rewrite |
| Citations | `source_resources` list per response | One `Citation` object across `fhir_resource` / `uploaded_document` / `guideline`; per-document citation cards; click-to-source with a bounding-box highlight |
| Verification | Source attribution (strip ungrounded claims) and domain constraints (allergy hard stop) | Allergy class table (including sulfonamides), forced "pending review" disclosure for document facts, guideline text in the grounding set |
| Access control | Real OpenEMR `authorization_code` login per resident; encounter sensitivity filter; cross-patient tool-call block | Document preview endpoint checks subject, status and a Patient read under the resident's token; every denial is the same 404 |
| Observability | Langfuse traces, structured JSON logs | Per-node latency, tokens and cost (`turn_summary`); logged routing decisions |
| Evals | 18-case golden set, 42 unit tests, 49-case behavioral coverage (never gated) | **55-case golden set** and **288 LLM-free unit tests** and 8 graph cases, all blocking MRs in CI, with a five-metric rubric gate; parallel runner; per-case rerun plus job retry, with flakes reported |

Current results (pipeline #29930): golden set 55/55 (54 passed, 1 skipped), graph cases 8/8, unit
tests 288/288, all five rubric metrics at 100%. Detail: `../EVAL_RESULTS.md`. Gate mechanics:
`../EVAL_GATE.md`. `/ui` is a styled chat interface (`app/static/chat.html`) with a real citation
click-to-source modal, not a placeholder. See "Known gaps" below for what's still simplified, and
the deployment section for how the droplet is set up.

## Quick start -- the Week 2 flow, end to end

The fastest way to see Week 2's actual new capability (not just chart
lookups) working, once "Local setup" below is done and the app is running:

```bash
# 1. Log in at http://localhost:8420/ui (or https://localhost:8421/ui),
#    completing the real OpenEMR OAuth login for a test resident.
# 2. Upload a document for a patient -- POST /documents (multipart), doc_type
#    'lab_pdf' or 'intake_form'. From the UI: the upload control on /ui.
#    From curl (session cookie from step 1 required):
curl -i -X POST http://localhost:8420/documents \
  -H "Cookie: copilot_session=<value from your browser>" \
  -F "patient_id=<a real FHIR Patient id>" \
  -F "doc_type=lab_pdf" \
  -F "file=@/path/to/a/lab_report.png;type=image/png"
# 3. Ask about it in the same conversation -- the model routes to
#    intake_extractor automatically once uploaded_document_ref is set:
curl -i -X POST http://localhost:8420/chat \
  -H "Cookie: copilot_session=<value>" -H 'Content-Type: application/json' \
  -d '{"message": "What did the uploaded lab document show?", "patient_id": "<same patient id>"}'
# 4. Ask a guideline question for the same patient in a later turn -- routes
#    to evidence_retriever, cites a corpus doc:
curl -i -X POST http://localhost:8420/chat \
  -H "Cookie: copilot_session=<value>" -H 'Content-Type: application/json' \
  -d '{"message": "What is the general management approach for hyperkalemia?", "patient_id": "<same patient id>"}'
```

**From `/ui` instead of curl.** The composer has an **Attach document** control. Pick a PDF or PNG
(20 MB max, PDFs up to 5 pages) and a type (lab report or intake form). It uploads to
`POST /documents` for the active patient and shows progress, then a "reading the document" state
while OCR and structuring run. The result appears as a card with a blurred page thumbnail, a
**Pending review** badge, and one row per extracted field. Each row notes low or unreadable
confidence and opens the source page with the field highlighted. The document then rides along
as `uploaded_document_ref` with your **next question only**, shown as a chip in the composer. Too
many pages, a duplicate file, and other ingestion failures show a plain-language message.

A real response includes a `citations` array (`source_type`,
`source_id`, `page_or_section`, `field_or_chunk_id`, `quote_or_value` per
entry) and, in `/ui`, each citation is clickable to a document/guideline
preview with the extracted field's bounding box highlighted.

## Local setup (Week 1 + Week 2 -- shared)

Most of this section is one shared setup for both weeks; where a specific step is Week-2-only (the document-ingestion OAuth scopes, the categories/MIME-allowlist retrofit), it's called out inline as **(Week 2)**.

Prerequisites: the `docker/development-easy` stack running (`docker compose
up --detach --wait` from that directory), Python 3.12, and `tesseract-ocr`
installed as a system package (`apt-get install -y tesseract-ocr` on
Debian/Ubuntu) -- `pip install -r requirements.txt` only installs
`pytesseract`'s Python wrapper, not the OCR engine itself; document
ingestion (`app/ingestion/ocr.py`) will fail with `TesseractNotFoundError`
without it. Found missing on the staging CI runner 2026-09-24, tracked in
`../W2_ARCHITECTURE.md` §4.4.

```bash
cd clinical-copilot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Register + enable an OAuth2 client against the local instance

```bash
curl -sk -X POST -H 'Content-Type: application/json' \
  https://localhost:9300/oauth2/default/registration \
  --data '{
    "application_type": "private",
    "redirect_uris": ["https://localhost:9300/clinical-copilot/callback"],
    "client_name": "Clinical Co-Pilot (dev)",
    "token_endpoint_auth_method": "client_secret_post",
    "scope": "openid offline_access api:oemr api:fhir user/Patient.read user/Condition.read user/AllergyIntolerance.read user/MedicationRequest.read"
  }'
```

Save the returned `client_id` / `client_secret` into `.env`. Then, from
`docker/development-easy`, enable the client, grant it the password grant
type (both disabled by default -- see `architecture-audit.md` 6.1 and
"Known gaps" below), point its `redirect_uri` at this app's real `/callback`
(the registration above used a placeholder), and add `user/Encounter.read`,
`user/Observation.read`, and the three document-ingestion scopes below to
its allowed `scope` (needed for the Phase 1 sensitivity-filter work and
UC2's get_recent_observations respectively -- see ARCHITECTURE.md 3.3, the
FHIR API omits Encounter's sensitivity field entirely; and OpenEMR won't
grant a scope a client isn't registered for regardless of what the consent
screen shows):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET is_enabled=1, grant_types='authorization_code|password|refresh_token', \
   redirect_uri=CONCAT(redirect_uri, '|http://localhost:8420/callback'), \
   scope=CONCAT(scope, ' user/Encounter.read user/Observation.read user/document.c user/document.s user/DocumentReference.read') \
   WHERE client_id='<your client_id>';"
```

Retrofitting an existing client that's missing just `user/Observation.read`
(e.g. it already has `Encounter.read` from an earlier setup):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET scope=CONCAT(scope, ' user/Observation.read') \
   WHERE client_id='<your client_id>' AND scope NOT LIKE '%user/Observation.read%';"
```

**Prerequisite, checked before any of the scope retrofitting below matters
at all:** the two document categories this project's ingestion pipeline
uses -- Source Document (id 35) and AI-Extracted Pending Review (id 36) --
must actually exist in the target OpenEMR instance's `categories` table.
Found live on staging (2026-09-23): both were missing there even though
every scope was correctly granted, because these categories were
apparently created by hand on local dev at some point and never
replicated anywhere else, and this step was never written down until now.
Effect when missing: `attach_and_extract`'s upload POST 404s with
`"/patient/:pid/document not found"` -- not a scope error, so don't
mistake it for one; check this first before chasing scopes for a document-
upload 404 specifically (a document-upload 401 is still a scope issue,
see below).

`categories` is a nested-set (MPTT) tree, not a flat table -- **do not
hand-write an `INSERT`** without first reading `library/classes/
Tree.class.php`'s real `add_node()` (confirmed live 2026-09-23: an earlier
attempt at a raw `INSERT` for these same two rows was abandoned for
exactly this reason). `add_node()` shifts every existing row's `lft`/
`rght` at or past the insertion point by +2, and allocates the new row's
`id` via `GenID('categories_seq')` -- ADODB's sequence-emulation table
(confirmed via the vendored driver, `vendor/adodb/adodb-php/drivers/
adodb-mysqli.inc.php`'s `_genIDSQL`: `UPDATE categories_seq SET
id=LAST_INSERT_ID(id+1)`, i.e. increment-then-return, so the stored value
is the *last issued* id, not the next one) -- skipping that table leaves
it stale, and the next real category created through the OpenEMR UI would
collide with the id `GenID()` hands out. Read the exact current values
off a working instance first (every column, plus `categories_seq`) rather
than reconstructing them from pattern -- what worked on staging:

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE categories SET rght = rght + 4 WHERE id = 1;
   INSERT INTO categories (id, name, value, parent, lft, rght, aco_spec, codes)
     VALUES (35, 'Source Document', '', 1, 67, 68, 'patients|docs', '');
   INSERT INTO categories (id, name, value, parent, lft, rght, aco_spec, codes)
     VALUES (36, 'AI-Extracted Pending Review', '', 1, 69, 70, 'patients|docs', '');
   UPDATE categories_seq SET id = 36;"
```

The exact `lft`/`rght`/id values above are specific to an instance whose
tree matches this project's own local dev exactly up through category id
34 (true for both local dev and staging, verified directly) -- on a
instance with a different category tree, derive the real values from
`add_node()`'s algorithm against that instance's actual current state,
don't copy these blindly.

Retrofitting an existing client that's missing the document-ingestion
scopes (Decision 2's revision, Investigation 1/2/6 in the Week 2 build
notes, corrected 2026-09-22 -- see below): OpenEMR's FHIR API has no create
route for `DocumentReference`/`Binary` at all, so document upload goes
through the Standard API instead (`POST /api/patient/:pid/document`):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET scope=CONCAT(scope, ' user/document.c user/document.s user/document.r user/DocumentReference.s user/Binary.read user/patient.r') \
   WHERE client_id='<your client_id>' AND scope NOT LIKE '%user/document.c%';"
```

Retrofitting an existing client that already has the document-ingestion
scopes above but is missing `user/Binary.read` and/or `user/patient.r`
(e.g. it was set up before these scopes existed) -- **separate,
independently-guarded steps, not covered by the block above**: that
block's own `WHERE ... NOT LIKE '%user/document.c%'` guard matches 0 rows
once a client already has `document.c`, so it silently skips adding
either newer scope for exactly this case (caught this while documenting
the `Binary.read` fix, not by a report -- the same "one narrowly-scoped
retrofit per newly-added scope" pattern as the `Observation.read` block
above, deliberately not folded into the combined block for this reason):

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET scope=CONCAT(scope, ' user/Binary.read') \
   WHERE client_id='<your client_id>' AND scope NOT LIKE '%user/Binary.read%';"
```

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET scope=CONCAT(scope, ' user/patient.r') \
   WHERE client_id='<your client_id>' AND scope NOT LIKE '%user/patient.r%';"
```

Retrofitting an existing client that already has `user/document.r`/
`user/document.s` but is missing `user/document.c` specifically -- found
live on staging (2026-09-22): its client had `.r`/`.s`/`Binary.read`/
`patient.r` registered but never `.c`, so `attach_and_extract`'s own
upload step (and the new `get_source_document_bytes_via_standard_api`
regression test, which uploads a small test document before reading it
back) 401'd with `scope user/document.c not in access token` --
confirmed via the server's own error log, same `AccessDeniedException`
mechanism as `Binary.read`/`patient.r` above, not a new failure mode.
Same "one narrowly-scoped retrofit per newly-added-on-this-client scope"
pattern, verified via a direct `SELECT` before applying (not a blind
`WHERE ... NOT LIKE` guess) to rule out a substring collision with
`document.r`/`document.s`/`encounter.rs`:

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "SELECT scope FROM oauth_clients WHERE client_id='<your client_id>'\G"
# confirm 'user/document.c' is genuinely absent from the printed scope, then:
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "UPDATE oauth_clients SET scope=CONCAT(scope, ' user/document.c') \
   WHERE client_id='<your client_id>';"
```

**2026-09-22 correction, re-verified live end-to-end against this exact
deployment (token claims decoded + a real request against each route, not
log inference -- an earlier pass here had `.read` and briefly `.c`-for-
everything, both wrong):** the Standard API's scope suffix is derived from
`src/Common/Http/HttpRestRouteHandler.php`'s own `getGetRequestPermission()`
-- a GET on a **collection** route (no id segment, e.g. `/document`) needs
`.s` (search); a GET on a **single-instance** route (e.g. `/document/{did}`)
needs `.r` (read); POST is always `.c`. Concretely: `user/document.s` to
list a patient's documents, `user/document.c` to create one,
`user/document.r` to download one back, and `user/DocumentReference.s` (not
`.read`) for a patient-filtered FHIR `DocumentReference` search -- citations
here always filter by patient, never fetch a single `DocumentReference` by
id. Also confirmed: the registered client's own `scope` column above did
**not** actually gate what a password-grant token received in testing for
`user/DocumentReference.s` on the client used during that testing (it was
granted and worked even before this column listed it) -- but this does
**not** generalize to every scope, OR even to the same scope on a
different client: see `user/Binary.read` below, which the same column
silently DID gate, and (2026-09-23) `user/DocumentReference.s` itself
gating cleanly on staging's separate client record -- genuinely missing
there, causing `get_source_document_bytes()`'s `DocumentReference` search
step to 401 until registered the same way as every other scope here.
Confirm per-scope, per-client, via a decoded token's own `scopes` claim
or the client's own row directly -- never assume from one prior result on
one instance.

**`user/Binary.read` (2026-09-22, added investigating the CSRF gap's
blast radius on the click-to-source preview -- see "Known gap" below):**
powers `app/ingestion/storage.py`'s `get_source_document_bytes()`
(`app/main.py`'s `/documents/preview` endpoint, Checklist item 7's
click-to-source bounding-box overlay). Requires the `.read` suffix
specifically, not `.r` -- confirmed via
`src/Common/Auth/OpenIDConnect/Entities/ServerScopeListEntity.php`'s
`fhirResourceScopesV1()`: the server only defines a `Binary` scope under
FHIR's older V1 naming convention, not the `.r`/`.s`/`.rs` scheme every
other resource here uses; requesting `user/Binary.r` gets silently dropped
from the issued token with no error (confirmed by decoding the JWT).
Separately, **this scope IS gated by the client's own registered `scope`
column** -- confirmed via the server's own error log
(`AccessDeniedException: scope user/Binary.r not in access token`, thrown
by `ScopeRepository::finalizeScopes()`) and by querying the row directly:
this client had zero `Binary` entries in any form before the `UPDATE`
above. Without it, the `/fhir/Binary/:id` route itself is fine (see below)
but every request to it 401s.

**A second, unrelated fix is also required before any ingestion upload
will succeed**, found the same session: OpenEMR's `secure_upload` global
(enabled by default) rejects any upload whose sniffed MIME type isn't in
the `files_white_list` list-option -- the AI-Extracted-Pending-Review
category's whole purpose is storing structured JSON, and `application/json`
is not in that list by default (confirmed live: `mime_content_type()` on
well-formed JSON returns exactly `application/json`, not `text/plain`).
Add it once per instance:

```bash
docker compose exec openemr mysql -h mysql -uroot -proot -D openemr -e \
  "INSERT INTO list_options (list_id, option_id, title, activity) \
   VALUES ('files_white_list', 'application/json', 'application/json', 1);"
```

Without this, the scanned-source-document upload (image/PDF) succeeds but
the paired structured-extraction-JSON upload silently fails -- confirmed
live, both before and after this fix, on the same patient/category.

**CSRF gap, fixed 2026-09-23 (the click-to-source preview half was closed
2026-09-22; this closes the underlying CSRF cause for the extraction-
read-back half too -- see the separate `BinaryFileResponse` item below,
also fixed the same day, which was the other thing blocking that
specific read-back caller once CSRF stopped masking it):** reading a
stored document back via the Standard API
(`GET /api/patient/:pid/document/:did`) used to 500 with `"OpenEMR is
potentially not secure because CSRF key is empty."` Traced past the
misleading call chain: `DocumentRestController::downloadFile()` doesn't
call `CsrfUtils` directly, and neither does the legacy `C_Document`
controller's `retrieve_action()` -- the actual throw is
`C_Document::__construct()` (`controllers/C_Document.class.php`)
unconditionally calling `CsrfUtils::collectCsrfToken($session)` to
populate a `CSRF_TOKEN_FORM` template variable that `retrieve_action()`
(the only action a REST caller can reach) never uses at all. The session
itself is real and already-authenticated for a bearer-token request --
confirmed via `BearerTokenAuthorizationStrategy::setupSessionForUserRole()`
(`src/RestControllers/Authorization/`), which independently verifies the
token and populates real session state before this constructor ever
runs -- it's specifically `csrf_private_key` that's unset, because that's
only ever set by the interactive browser-login bootstrap
(`CsrfUtils::setupCsrfKey()`), which a bearer-token request structurally
never goes through. CSRF defends against a browser *automatically*
attaching an ambient session cookie to a forged cross-site request; a
bearer token must be deliberately included by the calling code, which a
malicious page cannot compel a victim's browser to do (RFC 6750; the same
basis OWASP's own CSRF guidance uses to exclude bearer-token APIs from
the threat model) -- so skipping this specific, unused-in-this-context
call for an already-verified bearer-token session doesn't reopen anything
CSRF exists to prevent.

Fixed narrowly, in `controllers/C_Document.class.php`'s constructor only
-- `CsrfUtils.php` itself is completely untouched, so every other CSRF
check anywhere else in this codebase is unaffected. Gated on `userRole`
being set on the session, not `authUserID`/`pid` as an earlier draft of
this fix used -- confirmed live that `authUserID` is *also* set by
genuinely interactive logins (`AuthUtils::setUserSessionVariables()`),
with `setupCsrfKey()` called separately from whatever page loads next,
not atomically -- a real window that gate would have missed. `userRole`
has exactly one write site anywhere in this codebase
(`BearerTokenAuthorizationStrategy::setupSessionForUserRole()`); no
interactive/portal/session-cookie login flow sets it, confirmed by
grepping every write site, not assumed. Verified locally both directions,
not just the happy path: a real bearer-token request through this exact
route now has the CSRF exception gone entirely (confirmed via the live
server log); a real interactive login (Selenium, admin/pass, through the
actual login form) followed by the real `C_Document` `view_action` page
still renders a genuine, live `csrf_token_form` value in the page --
proof the check still fires and succeeds exactly as before for a real
browser session, completely unaffected.

**`BinaryFileResponse` type mismatch, fixed 2026-09-24 (found investigating
the CSRF fix above, 2026-09-23; tracked separately, not folded into that
fix -- a genuinely different bug that only became reachable once CSRF
stopped masking it):** with the CSRF exception gone, the same request
started failing with:

```
The file "{...the full JSON extraction content, as a string...}" does not exist
```
at `Symfony\Component\HttpFoundation\File\File::__construct()`, called from
`BinaryFileResponse::setFile()`, called from
`src/RestControllers/DocumentRestController.php:161`
(`new BinaryFileResponse($results['file'], ...)`). Investigated before
fixing, not assumed from one case: read every branch of
`C_Document::retrieve_action()` (CouchDB-direct, CouchDB-converted-jpg,
hard-disk-original, hard-disk-converted-jpg) -- every single one follows
the identical `if ($disable_exit == true) { return $filetext; }` pattern,
where `$filetext` is always fully-materialized raw content
(`file_get_contents()`/`base64_decode()`/`decryptFromFilesystem()`),
never a path, regardless of storage backend. `DocumentService::getFile()`
always passes `disable_exit=true`, so this is unconditional for every
REST caller -- `BinaryFileResponse` (which expects an existing path or
`SplFileInfo` to stream from disk) was architecturally the wrong response
class here, not a one-branch edge case. This was never reachable before:
the CSRF exception fired unconditionally, before any of this code ever
ran, so this defect has apparently been present the whole time, fully
masked.

Fixed by replacing `BinaryFileResponse` with a plain `Response`
constructed directly from the bytes (never `StreamedResponse` --
`retrieve_action()` already fully materializes the content into one PHP
string before returning it, so there's nothing left to stream), with
`Content-Type`/`Content-Disposition` set explicitly
(`ResponseHeaderBag::makeDisposition()`) and `setPublic()` called
explicitly to replicate exactly what `BinaryFileResponse`'s own
constructor does internally -- not just "close enough" behavior. Removed
the now-dead `BinaryFileResponse` import. Verified locally against the
exact request that originally surfaced this: real `200`, full 1691-byte
JSON body, parses correctly and matches the original upload exactly,
correct `Content-Type`/`Content-Disposition` headers. (One pre-existing,
unrelated cosmetic artifact traced, not assumed: `Cache-Control` shows
`must-revalidate` twice and `private`+`public` together -- confirmed
`BinaryFileResponse extends Response` without overriding `setCache()`/
`setPublic()`/`setExpires()` at all, so this is identical output the
original code would always have produced; just never observed before
since nothing ever reached this line successfully.)

**Verified for the hard-disk branch only -- the CouchDB branch is fixed
by code inspection (identical `return $filetext` pattern, confirmed by
reading, not assumed) but not by a live test, because of a separate,
unrelated bug that blocks creating a CouchDB-stored document to test
against in the first place:** local dev defaults to `document_storage_
method=0` (Hard Disk; `1` = CouchDB, per `library/globals.inc.php`'s own
option labels) and had zero existing CouchDB-backed documents. Switching
to CouchDB storage and attempting a real upload through the actual
ingestion pipeline failed with `Undefined property: stdClass::$id` at
`library/classes/Document.class.php:1023` --
`$couch->save_doc($couchdata)` returns a response object with no `id`/
`rev` properties, tripping OpenEMR's own existing `"CouchDB save
failed"` handling (that handling itself is correct, pre-existing
defensive code, not part of this bug). Isolated before giving up on it:
confirmed CouchDB itself is network-reachable and TLS-terminating
correctly from the openemr container (`200` on an unauthenticated
request to its root endpoint), so this isn't connectivity -- more likely
an auth/credential or CouchDB-library-version mismatch. Not investigated
further; `document_storage_method` was reverted back to `0` afterward,
restoring local dev to its original state. **New, separate, tracked
item** -- if CouchDB storage matters for a real deployment, this write-
path bug needs its own investigation before the CouchDB read branch above
can be empirically confirmed the way the hard-disk branch now is.

**Two different callers hit this route -- both now work, fixed by two
different, separately-tracked changes:** `app/ingestion/storage.py`'s
`list_extracted_documents` (used by `get_uploaded_documents`,
`intake_extractor_node`'s same-turn read-back -- reading back the
AI-Extracted Pending Review structured JSON, category 36) calls the
Standard API's `GET /patient/:pid/document/:did` directly, and **is now
fixed** by the CSRF and `BinaryFileResponse` items above (verified live,
2026-09-23/24, for hard-disk-stored documents -- see those items for the
CouchDB caveat). `get_source_document_bytes` (used by `app/main.py`'s
`/documents/preview` endpoint -- the original scanned document, category
35, behind Checklist item 7's click-to-source bounding-box overlay) took
a different, independent path to get fixed, earlier (2026-09-23):
investigated
`GET /fhir/Binary/:id` directly before assuming it shared the same bug --
traced `FhirDocumentRestController::downloadDocument()` ->
`BaseDocumentDownloader`, confirmed zero `CsrfUtils`/`C_Document`
dependency (a genuinely different, modern PSR-7 route, not a thin wrapper
around the same broken legacy code), then live-tested it end to end
(real PNG bytes streamed back, correct `Content-Type`). The only real
gap was OAuth scope registration -- see `user/Binary.read` above --
not a second CSRF bug. `get_source_document_bytes` called
`GET /fhir/DocumentReference?patient=...` then `GET /fhir/Binary/:id`
from 2026-09-22 to 2026-09-25.

**2026-09-25 correction -- previews required OpenEMR admin.** Those two
FHIR routes are gated in OpenEMR behind `admin`/`super` and
`admin`/`users`, which Clinicians and Physicians do not hold, so for any
non-admin resident every citation preview failed ("This document preview
is not available right now"; log reason `not_authorized`). It went
unnoticed because the development account is an Administrator (AUDIT.md
§6.10). `get_source_document_bytes` now reads through the Standard API
under the resident's own token (ACL `patients`/`docs`, which Clinicians
hold -- the same routes `get_uploaded_documents` uses): resolve the pid,
list the patient's Source Document category, then
`GET /patient/:pid/document/:did` for the id that listing returned.

**Effect today:** `attach_and_extract` (upload) is fully confirmed working
end-to-end for hard-disk storage; the click-to-source preview and
bounding-box overlay are fully confirmed working end-to-end (2026-09-22);
reading a previously-stored extraction's structured JSON back
(`get_uploaded_documents`, `intake_extractor_node`'s same-turn read-back)
**is fixed and verified end-to-end for hard-disk-stored documents**
(2026-09-23/24, both the CSRF gap and the `BinaryFileResponse` mismatch
above are resolved). CouchDB storage remains untested on the read side
(fixed by code inspection only, not live-verified) because a separate,
unrelated CouchDB write-path bug (documented above) currently prevents
creating a CouchDB-stored document to test against at all.

**Separately (2026-09-22), found investigating a staging CI failure on the
above:** `resolve_internal_pid()` -- the step every document upload/
read-back call goes through first, to translate the FHIR patient UUID into
OpenEMR's internal numeric pid -- originally read `pid` off the patient's
first encounter (`GET /patient/:puuid/encounter`), which raised `not_found`
for any patient with zero encounters on file. `evals/seed_fixtures.py`
only gives one of its six fixture patients (`TEST-0004`) a real encounter;
staging's `PID1` (`TEST-0001`) genuinely has none, so document ingestion
was silently broken for that patient on staging specifically, even though
every local test that night passed (local dev's `PID1` happens to carry
an old, unrelated encounter from earlier manual testing). The encounter
dependency was never a real requirement -- `resolve_internal_pid()` now
reads `pid` directly off `GET /patient/:puuid` (Standard API
single-patient read, no encounter involved), needing `user/patient.r`
instead of `user/encounter.rs` -- see the retrofit blocks above.

Password grant stays enabled for one reason: the automated eval suite
(`evals/run_evals.py`) has no browser to complete an interactive login, so it
authenticates with a fixed service credential -- that's an intentional,
scoped decision for offline/CI-style regression runs only. **The live
`/chat`/`/ui` path never uses it** -- every resident-facing request is
scoped to whichever real login completed `authorization_code` (Phase 1,
`CLAUDE_CODE_BUILD_INSTRUCTIONS.md`; see `app/oauth_session.py`).

### 2. Set the remaining `.env` values

- `OPENEMR_OAUTH_USERNAME` / `OPENEMR_OAUTH_PASSWORD`: `admin` / `pass` (the
  documented local dev default). If password-grant login fails with
  `invalid_grant`, the `users_secure` hash may have drifted from that default
  on your instance -- reset it directly for local dev only:
  `UPDATE users_secure SET password='$(php -r "echo password_hash('pass', PASSWORD_BCRYPT, ['cost'=>12]);")' WHERE username='admin';`
- `ANTHROPIC_API_KEY`: required, no default.
- `LOG_HASH_SALT`: **required in production**, optional for local dev. It keys the salted HMAC
  that replaces patient and resident identifiers in every log line (`h_<12 hex>`, see
  `app/log_redaction.py` and `../W2_ARCHITECTURE.md` §4.20). Generate one with
  `openssl rand -hex 32` and keep it stable. If it's unset, the app uses a random salt per
  process, so the same patient gets a different token after every restart and logs can't be
  correlated across restarts. Treat it as a secret: anyone with the salt can test a guessed
  identifier against a token.
- `ENABLE_API_DOCS`: **leave unset.** Setting it serves `/docs`, `/redoc` and `/openapi.json`,
  which is for local development only. It is off by default.
- `OPENEMR_VERIFY_TLS=false` (self-signed local dev cert only).
- `COPILOT_BASE_URL`: where this app itself is reachable -- defaults to
  `http://localhost:8420`, matching the `redirect_uri` registered above.
  Must be updated (and the client's registered `redirect_uri` updated to
  match) if you run this somewhere else, e.g. the droplet.
- `OPENEMR_SCOPE_TEST_USERNAME` / `OPENEMR_SCOPE_TEST_PASSWORD`: optional.
  A dedicated low-privilege demo account (`copilot_resident_1`, ACL group
  `clin`, Provider off, no High-sensitivity grant -- mirrors `clin_1` per
  `audit-notes.md`, created the same way via the real Add User admin form)
  used only by `evals/cases.py`'s `oauth_scope_enforcement_denied` case and
  for manually verifying scope restriction against a real
  `authorization_code` token. Never a real resident's credential; the eval
  case skips (not fails) if unset. **Confirmed not present on the droplet
  as of 2026-09-20** (checked directly against the droplet's `users` table
  while investigating `evals/COVERAGE.md`'s `get_recent_encounters` ACL
  gap) -- create it there yourself via the real Add User admin form if you
  want this eval case to run against the droplet rather than skip.
  `test_clinician` is the only documented grading credential confirmed to exist
  on the droplet today, ACL group `clin` (Clinicians).

### 3. (Optional but recommended) bring up Langfuse

```bash
cd docker
openssl rand -hex 32  # run 3x for ENCRYPTION_KEY, NEXTAUTH_SECRET, and a public/secret key pair
```

Fill in `docker/.env.langfuse` (git-ignored, never commit it) with those
values plus `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` (any
`pk-lf-...` / `sk-lf-...`-shaped strings) and `LANGFUSE_INIT_USER_EMAIL` /
`_PASSWORD`, then:

```bash
docker compose -f docker-compose.langfuse.yml --env-file .env.langfuse up -d
```

Web UI: http://localhost:3300 (login with the `LANGFUSE_INIT_USER_*` values).
Put the generated public/secret key pair into `clinical-copilot/.env`'s
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`. If left blank, the app still
runs and still logs everything locally (structured JSON to stdout) -- it just
skips sending to Langfuse.

## Running it (Week 1 + Week 2 -- shared)

```bash
uvicorn app.main:app --reload --port 8420
```

`/chat` requires a real OpenEMR login (Phase 1, `CLAUDE_CODE_BUILD_INSTRUCTIONS.md`) -- there is no
more anonymous/shared-credential access. Open http://localhost:8420/ui in a browser, click **Log in
with OpenEMR** using a non-admin test account you create (ACL group `Clinicians`, Provider off,
the same way as `copilot_resident_1` below; see `THREAT_MODEL.md` 4.5 for why a narrow account
replaced `admin`), or any real account. OpenEMR will then show a one-time OAuth consent screen listing the specific
resource types being requested (`AllergyIntolerance`, `Condition`, `Encounter`, etc.) -- this is
expected, standard OAuth2 behavior, not an error; scroll down and click **Authorize** to proceed.
The **Remember Me** checkbox there is optional and lets you skip re-consenting on future logins
from the same session/device. Then use the chat form; the page handles the session cookie for you
and keeps `conversation_id` across turns automatically.

A bare `curl -X POST /chat` with no session cookie now gets a clean 401 by design:

```bash
curl -i -X POST http://localhost:8420/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "Give me a quick orientation on this patient.", "patient_id": "<a FHIR Patient id>"}'
# HTTP/1.1 401 Unauthorized
```

To drive `/chat` from curl/scripts instead of `/ui`, complete the browser login once, capture the
`copilot_session` cookie your browser's dev tools shows after `/callback` redirects, and pass it as
`-H "Cookie: copilot_session=<value>"` on subsequent requests -- the session lives in-memory on the
server for as long as the process runs (see `app/oauth_session.py`).

## Model provider (Week 1 + Week 2 -- shared)

This project defaults to, and is validated against, **Anthropic Claude** (`LLM_PROVIDER=anthropic`,
the default if `LLM_PROVIDER` is unset). `app/llm_client.py` defines the provider seam
(`LLMClient`/`build_llm_client`) that makes a different provider possible for a future public
release -- data residency, cost at scale, and vendor lock-in are real reasons a deployer might want
a self-hosted, open-source model instead. `LLM_PROVIDER=openai_compatible` (plus
`OPENAI_COMPATIBLE_BASE_URL`/`OPENAI_COMPATIBLE_MODEL`) is wired into the same factory today, but
resolves to a **documented stub** (`OpenAICompatibleLLMClient`) that raises `NotImplementedError` on
every real method -- there is no working non-Anthropic path yet, on purpose.

> ⚠️ **Hard requirement, not a suggestion, once a real second provider exists:** every clinical-safety
> behavior this project documents was built and is only proven true for Claude. **Before running any
> different model against real or synthetic patient data in any clinical-adjacent context, you MUST
> re-run `evals/unit_tests.py` AND `evals/cases.py` against that specific model and confirm the same
> pass rates documented for Claude (currently `evals/unit_tests.py` 288/288, and the
> `evals/gate.py` **gate** -- all 5 rubric metrics (`schema_valid`, `citation_present`,
> `factually_consistent`, `safe_refusal`, `no_phi_in_logs`), each at its 100% floor with no more than
> 5% relative regression from baseline -- see `EVAL_GATE.md` §3 and `KEY_METRICS.md` for the current
> run) -- a lower pass rate on any gate metric, or a new unit-test failure, means that model is not
> validated for this use, full stop.** The Golden Set's raw case count (55/55 most recently, see
> `EVAL_RESULTS.md`) is a separate, softer signal from the hard gate -- a single non-gate case
> regressing is worth investigating, not an automatic disqualification the way a gate-metric drop is.
> See `ARCHITECTURE.md`'s Known tradeoffs (§6) for the fuller design rationale and what's structurally
> different between the two provider paths; not duplicated here.

## Running the eval suite (Week 1 + Week 2 -- the golden set covers both)

```bash
python3 -m evals.run_evals
```

Runs all 55 cases (`evals/cases.py`) against the live agent -- real Anthropic
calls, real FHIR calls. Prints a pass/fail table and writes full detail to
`evals/last_run_results.json` (git-ignored: it's a run artifact, not the
deliverable -- the suite code is the deliverable). Cases run 4 at a time by
default (`EVAL_CONCURRENCY`; `1` runs them serially). A case that fails is
rerun once and, if it then passes, reported as a `FLAKE`, not silently
counted as a pass. The graph-level cases run separately:
`python3 -m evals.graph_cases`. Each case documents its
category (boundary / invariant / regression / adversarial, per
`ARCHITECTURE.md` 7.1) and the specific failure mode it guards against.
`oauth_scope_enforcement_denied` (Phase 1) records a labeled `SKIP`, not a
pass or failure, if `OPENEMR_SCOPE_TEST_USERNAME`/`_PASSWORD` aren't set.
See `EVAL_GATE.md` for the full gate mechanics (the 5 rubric floors and the
5%-regression rule) that `run_evals.py` enforces on top of the per-case
pass/fail table.

**CI.** The MR pipeline runs this suite for you (`eval_golden_set`/
`eval_graph_cases` in `.gitlab-ci.yml`) — you don't need to run it locally
before pushing, though it's faster to catch a failure here first. To cut
pipeline cost, those two jobs skip on docs-only MRs, skip while an MR is
Draft, and don't re-run on `main` after merge; none of that weakens the
gate itself, since a non-draft MR touching `clinical-copilot/` code always
runs the full stage before it can merge. Set `FORCE_FULL_EVAL=true` on a
manually triggered pipeline to force a full run regardless. Full rationale
and exact rule conditions: `EVAL_GATE.md` #2.

## Bruno API collection (Week 1 + Week 2 -- shared)

`bruno/` is a [Bruno](https://www.usebruno.com/) collection covering
`/health`, `/ready`, `/login`, `/callback`, `/me`, `/patient`, and 7 proven
`/chat` scenarios pulled directly from the Golden Set / behavioral
coverage work, for manual poking at either instance without writing curl
by hand.

Open the `bruno/` folder in the Bruno app, pick an environment (`local` =
`http://localhost:8420`, `droplet` = `http://<deployed-host>:8420`), and
run any request. Each request's `docs` tab explains what the scenario is
proving and what a correct response looks like. **`chat/` and `patient/`
requests need a real, logged-in session first** (Phase 1 removed
anonymous access) -- see `bruno/README.md` for how to obtain a
`copilot_session` cookie via a real browser login and set it as this
collection's `copilot_session` environment variable; `auth/01 Start
Login`, `/02 Callback`, and `/03 Me` don't need one.

**Environments:**
- `local` — talks straight to a `uvicorn` instance on the host.
- `droplet` — points at the droplet's public IP. As of 2026-09-17, this is
  genuinely externally reachable: `uvicorn` is bound to `0.0.0.0:8420` and
  `ufw allow 8420/tcp` is in place, confirmed with a real curl from
  outside the droplet (not just SSH-local), so Bruno's `droplet`
  environment can be pointed at it directly, no SSH tunnel needed.
  **Security note, worth knowing before using this:** `/chat` on that port
  has no authentication at all -- anyone who finds the port can trigger
  real Anthropic API calls (real cost) against it. Fine for a graded
  submission's reachability requirement, but not something to leave open
  indefinitely without at least considering an allowlist or rate limit.
  **Before running any `ufw allow` command on this droplet:** Docker's own
  `FORWARD`/`DOCKER` iptables chains bypass `ufw`'s `INPUT` chain entirely
  for any container-published port -- a `ufw allow` rule provides no real
  protection for a Docker-published port, only for a bare host process
  (like `uvicorn` on 8420 above). This was the root cause of a real
  incident (2026-09-19): six services (MySQL, CouchDB, MinIO, phpMyAdmin,
  Selenium, Mailpit) were reachable from the public internet with `ufw`
  showing no rule for any of them, and one (Selenium) was found running an
  unauthorized process as a result. The fix for a Docker-published port is
  always in the port binding itself (`127.0.0.1:<port>:<port>`, or remove
  the `ports:` mapping entirely if nothing outside the host needs it --
  never leave it unbound, which defaults to `0.0.0.0`), not a `ufw` rule.
  Specifically: **`ufw` must never allow 2375/2376** (Docker's remote
  daemon API) unless that API is deliberately enabled with its own
  authentication in place first -- nothing in this stack listens there
  today, and the `ufw allow` rules for both were removed on 2026-09-19 as
  a hardening measure after this incident. Re-add only deliberately, only
  alongside real authentication, never by default.

**Requests:**

| # | Request | Scenario |
|---|---------|----------|
| health/01 | Health Check | Liveness -- always 200 if the process is up |
| health/02 | Readiness Check | Per-dependency check (OpenEMR, Anthropic, Langfuse); 503 if any are down |
| auth/01 | Start Login | Confirms `/login` redirects (307) to OpenEMR's real authorize URL |
| auth/02 | Callback (not independently testable) | Confirms a stale/invalid code+state fails closed (400), not open -- can't test the success path standalone, see its own docs |
| auth/03 | Me | Confirms whether `copilot_session` is currently valid -- `authenticated: true`/`false` |
| patient/01 | Patient Header (pid1) | Confirms `/patient` resolves a real chart header (name/DOB/gender) for the active session |
| chat/01 | Normal Orientation (pid1) | Grounded, multi-fact summary + duplicate-record warning |
| chat/02 | Empty Chart (pid3) | True negative -- chart genuinely has nothing, must be reported honestly |
| chat/03 | Malformed Patient ID | Graceful failure, no crash, still HTTP 200 |
| chat/04 | Allergy Hard-Block (pid1) | Domain-constraint hard block on an uncoded/free-text allergy |
| chat/05 | Adversarial Hallucination Bait (pid1) | Asks about a med the patient isn't on -- no fabricated dose/date reaches the user, whether by refusal or by the verification layer stripping it |
| chat/06 | Duplicate Record Stale Signout (pid6) | Cross-checks a stale verbal sign-out against the live chart across a known duplicate record pair |
| chat/07 | Ambiguous Query (pid1) | Underspecified question with no prior turn context -- known to have some run-to-run variance |

`bru` (the Bruno CLI) was not installed in this environment, so these were
verified by hand: every request was run as an equivalent curl call against
the live droplet (the 7 `/chat` scenarios and `/health`/`/ready` originally;
`/login`, `/callback`, `/me`, and `/patient` added and verified live
2026-09-20, `/chat` re-verified live the same night with a real
`copilot_session` cookie after Phase 1's auth migration), and each response
was confirmed to match its documented expectation before this collection
was committed/updated.

## Production deployment (Week 1 + Week 2 -- shared)

The app runs as a real systemd service on the production droplet
(`<deployed-host>`), not an ad hoc process -- unit file committed at
`deploy/clinical-copilot.service` (no secrets in it; every credential comes
from `.env` in the working directory below, which is never committed).

- **Checkout:** `/opt/openemr-base-clean_clem` (the real one -- confirmed
  live 2026-09-24 that `clinical-copilot.service` runs directly out of
  this checkout's venv). A second, stale, several-commits-behind checkout
  also exists at `/root/openemr-base-clean_clem` -- not referenced by
  anything running; leave it alone, it's flagged in `../W2_ARCHITECTURE.md`
  §4.0 for a later cleanup pass, not something a deploy needs to touch.
- **Service:** `clinical-copilot.service`, `User=copilot`, runs
  `uvicorn app.main:app --host 0.0.0.0 --port 8420` from
  `/opt/openemr-base-clean_clem/clinical-copilot/.venv`. Plain HTTP; there
  is no TLS on 8420 (see `../AUDIT.md` §6.11).
- **Python:** CPython 3.12.14, managed by [uv](https://docs.astral.sh/uv/) 0.12.19 (binary at
  `/opt/uv/bin/uv`), with interpreters installed under `/opt/uv-python`
  (`UV_PYTHON_INSTALL_DIR`). It is not the distribution's system Python. The service venv
  `.venv` was built on it with `--relocatable` and swapped in on 2026-09-25. The previous
  Python 3.10 venv is kept alongside as `.venv310` for rollback. The swap took about 22s of
  downtime.
- **The venv has no `pip` module.** `.venv/bin/pip` and `python -m pip` don't exist. Use uv
  against the venv's interpreter: `uv pip install --python .venv/bin/python ...`.

**Rebuild the venv.** The build and check steps are the ones used on 2026-09-25. The swap lines
show the shape of the swap and are not a recorded transcript. `--python` on `uv pip check` points
it at the new venv rather than the live `.venv`.

```bash
cd /opt/openemr-base-clean_clem/clinical-copilot
export PATH=/opt/uv/bin:$PATH UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv venv --python 3.12 --relocatable .venv312
uv pip install --python .venv312/bin/python -r requirements.txt
uv pip check --python .venv312/bin/python
# swap in (brief downtime), keeping the old venv for rollback
systemctl stop clinical-copilot
mv .venv .venv.old && mv .venv312 .venv
systemctl start clinical-copilot && curl -s http://localhost:8420/health
```

To roll back, stop the service, move the old venv (`.venv310`, or `.venv.old` after a later rebuild)
back to `.venv`, and start it again.

**Deploy by git bundle.** The droplet has no GitLab credentials, so it can't fetch from
labs.gauntletai.com. Code is shipped as a git bundle built on a machine that can. This is the
procedure used for MRs !10, !12 and !13:

```bash
# 1. Record prod's current SHA -- this is the rollback target
PROD_SHA=$(ssh root@<deployed-host> git -C /opt/openemr-base-clean_clem rev-parse HEAD)

# 2. Locally: build and check a bundle of only the new commits
git fetch gitlab
git bundle create copilot.bundle "$PROD_SHA"..gitlab/main
git bundle verify copilot.bundle
git rev-parse gitlab/main          # note this SHA for step 4

# 3. Copy it over
scp copilot.bundle root@<deployed-host>:/root/

# 4. On the droplet: verify, fetch, and confirm the SHA
ssh root@<deployed-host>
cd /opt/openemr-base-clean_clem
git bundle verify /root/copilot.bundle
git fetch /root/copilot.bundle gitlab/main
git rev-parse FETCH_HEAD           # must equal the gitlab/main SHA from step 2

# 5. STOP if any requirements*.txt changed -- use the uv venv procedure above instead
git diff --name-only HEAD FETCH_HEAD | grep -E 'requirements[^/]*\.txt$' && echo "requirements changed: stop"

# 6. Fast-forward and restart
git merge --ff-only FETCH_HEAD
systemctl restart clinical-copilot

# 7. Wait for health (every 3s, up to 90s), then readiness and service state
for i in $(seq 1 30); do curl -sf http://localhost:8420/health >/dev/null && break; sleep 3; done
curl -s http://localhost:8420/health; curl -s http://localhost:8420/ready
systemctl is-active clinical-copilot

# 8. On any failure: roll back to the recorded SHA
#    git checkout "$PROD_SHA" && systemctl restart clinical-copilot

# 9. Clean up
rm /root/copilot.bundle
```

**Fetch `gitlab/main` from the bundle, not `main`.** The bundle stores the ref under the name it
was created from, `refs/remotes/gitlab/main`. `git fetch /root/copilot.bundle main` fails with
"couldn't find remote ref main". This was checked against a bundle built exactly as in step 2.
`git bundle list-heads /root/copilot.bundle` shows the stored name. `PROD_SHA` in step 8 is the
value from step 1: set it again on the droplet, or paste the SHA in.

A `git pull` alone is not enough -- the running Python process already has
the old code loaded in memory; the service **must** be restarted for a
change to take effect.

**Set `LOG_HASH_SALT` in the production `.env`** (see `.env.example`, `W2_ARCHITECTURE.md` 4.20): it keys the salted
hashes that replace patient and resident identifiers in every log line. Unset, a random per-process salt is used and the
tokens change on each restart. `ENABLE_API_DOCS` must stay unset in production (it serves `/docs`, `/redoc`,
`/openapi.json`).

**Verify:** `/health` returns 200, then a real end-to-end check -- a
document upload, a read-back question, and a guideline question in the
same conversation -- the same shape used to verify every deploy tonight.

**Rollback:** `git checkout <previous-commit-sha>` (note the current SHA
*before* deploying, via `git log -1 --oneline`, specifically so this step
has something to roll back to) inside the same checkout, then
`systemctl restart clinical-copilot` again. Nothing about a code deploy
touches the database, so this is a plain revert-and-restart, not a
destructive operation.

**Memory headroom:** the embedding + reranker models
(`app/retrieval/embeddings.py`, `app/retrieval/rerank.py`) load lazily,
once, into whatever process imports `GuidelineRetriever` -- combined
~565MB measured resident memory (`app/retrieval/rerank.py`'s own
comment). A restart reloads the same single process rather than running a
second copy alongside it, so it does not need double the currently-used
memory. Check with `free -h` before deploying if genuinely uncertain --
"available" (reclaimable page cache), not just "free," is the number that
matters.

## Known gaps (stated honestly, not fixed under time pressure) -- Week 1 + Week 2

- **(Week 2) Extracted facts are stored as a structured JSON document, not FHIR
  Observation resources.** This OpenEMR version's FHIR API has no create
  route for `Observation`/`DocumentReference`/`Binary`/`QuestionnaireResponse`
  at all -- confirmed three independent ways (`app/ingestion/storage.py`'s
  own docstring): the FHIR route table, a service-layer no-op, and a live
  404 against a real `POST`. `attach_and_extract` stores both the original
  scanned document and a second document containing the extraction's
  structured JSON via `POST /api/patient/:pid/document` (OpenEMR's Standard
  API, not FHIR), under a dedicated "AI-Extracted Pending Review" category
  (id 36) -- a real, traceable OpenEMR record, per PRD Core Requirement 1's
  "FHIR resources **or** OpenEMR records," but not a discrete, individually
  queryable Observation the way a native FHIR write would produce. See
  `app/ingestion/storage.py`'s module docstring for the full diagnosis, and `../AUDIT.md`
  §6.13 for why this meets the PRD's "FHIR resources or OpenEMR records" wording and why
  unreviewed AI output shouldn't be written into the structured chart.
- **OAuth grant type -- RESOLVED for live traffic (Phase 1,
  `CLAUDE_CODE_BUILD_INSTRUCTIONS.md`).** `/chat`/`/ui` now use a real
  `authorization_code` login bound to whichever resident actually
  authenticates (`app/oauth_session.py`), matching `ARCHITECTURE.md` 1.3.
  Password grant still exists in `app/auth.py` but is used only by the
  automated eval suite (`evals/run_evals.py`), which has no browser to
  complete an interactive login -- that's a deliberate, scoped exception
  for offline regression runs, not a live-traffic path.
- **Cross-provider patient access is a separate, still-open OpenEMR ACL
  gap** that the `authorization_code` migration does not fix, and can't --
  `audit-notes.md` confirmed live in the UI that a physician (`dr_1`) could
  fully open, edit, and create encounters on another provider's patient
  (pid 4, admin's). Because that's a platform-level, UI-visible gap (not
  something the FHIR API adds on top of), a resident-scoped token's access
  ceiling is only as good as what OpenEMR's own ACL already grants that
  resident directly -- which today includes other providers' patients.
  Named honestly rather than silently tested around; a genuine future
  threat-model item.
- **The FHIR Encounter resource carries no sensitivity field at all** --
  confirmed by both code read and a live test (`architecture-audit.md`
  6.9): a real `authorization_code`-obtained `user/`-scoped token's
  `Encounter` search returns a known `sensitivity='high'` test encounter in
  full, same as a `system/`-scoped one. The compensating filter
  (`app/sensitivity.py`) is real, load-bearing work, not a defensive
  no-op -- see `ARCHITECTURE.md` 3.3. **`get_recent_encounters` is now
  built and does call it** (Phase 6), **and its live sensitivity lookup is
  RESOLVED as of 2026-09-20** -- it previously 401ed on every real call.
  An earlier version of this note (and of `evals/COVERAGE.md`) attributed
  that to an OpenEMR-side `RS256`/`HS256` bearer-token signature mismatch;
  that diagnosis was wrong, corrected after re-investigating live against
  the real container error log. The actual cause: OpenEMR's standard REST
  API requires a request-specific granular OAuth scope
  (`user/encounter.rs`, distinct from FHIR's coarser `user/Encounter.read`)
  that this project's OAuth client was never registered for -- a
  client-side registration gap, not an OpenEMR bug, and the same class of
  issue as the earlier `get_recent_observations` scope fix. Fixed by
  adding `user/encounter.rs` to `app/config.py`'s `OPENEMR_OAUTH_SCOPE`
  and to the registered client's `oauth_clients.scope` row.

  **A second, separate issue surfaced after that fix, also RESOLVED
  2026-09-20:** the scope fix closed a 401 (OAuth-scope layer); a
  different 403 (OpenEMR's own gacl ACL layer, `encounters:auth_a` --
  "Authorize - any encounters") then surfaced for `test_clinician` specifically,
  since the `clin` (Clinicians) ACL group this project's grading/test
  accounts are deliberately scoped to didn't carry that right by default.
  Fixed by granting `encounters:auth_a` to the `clin` group (via OpenEMR's
  own `AclExtended::updateAcl()` helper, not a schema change), verified
  live end-to-end for `test_clinician`, and confirmed still holding in the
  2026-09-20 droplet eval run (`../EVAL_RESULTS.md` §4) -- see
  `evals/COVERAGE.md`'s corrected entry for the full investigation of
  both issues, and `ARCHITECTURE.md`'s Known Tradeoffs for the deferred,
  architecturally preferred fix (`patient/Encounter.read` scoping).
- **Source-attribution verification is a curated-vocabulary substring match**
  (`app/verification.py`), not full per-claim tagging. It will miss a
  clinical claim phrased outside `_MEDICATION_TERMS` / `_ALLERGY_TERMS` /
  `_CONDITION_TERMS`, and can't distinguish a positive assertion ("on
  Warfarin") from a negation ("not on Warfarin") -- both are exactly what the
  build prompt asked to defer past this pass.
- **Domain constraint / duplicate / empty-chart enforcement all assume a
  single active patient per turn** (the `patient_id` passed to `/chat`).
  Multi-patient conversations in one turn aren't handled.
- **`check_allergy_conflict` catches direct name matches plus a small,
  curated cross-reactivity table** (`app/clinical_reference.py`, Phase 5) --
  it now catches amoxicillin against a documented penicillin allergy, but
  only for the classes actually curated there (penicillins today). It is
  explicitly not a drug-class knowledge base or a substitute for a
  production drug-interaction database (First Databank/Medi-Span/
  Multum-style) -- a proposed medication in an uncurated class still isn't
  caught. Documented in `app/tools.py` and `ARCHITECTURE.md` 3.2.
- **Conversation state is in-memory**, single-process. Fine for this demo,
  not for more than one server instance.
- All six planned tools are implemented: the two in scope for Early Submission
  (`get_patient_snapshot`, `check_allergy_conflict`), plus `get_recent_encounters`
  (Phase 6, UC2), `get_recent_observations` (UC2), `summarize_shift_events`
  (UC4), and `compare_signout_to_chart` (UC2), added afterward.
- **RESOLVED: `/health`/`/ready`, the Bruno collection, load tests, and
  alerts are all now built**, superseding the earlier "all explicitly
  deferred to Final Submission" note here -- `/health`/`/ready`
  (`ARCHITECTURE.md` §7.6), the `bruno/` collection (§7.5), Locust load
  tests against 10/50 concurrent residents (§7.9, `PERFORMANCE_BASELINE.md`),
  and three live, DB-backed Langfuse Monitors (p95 latency, error rate,
  tool failure rate -- §7.7, confirmed firing and recovering on real
  traffic, not just configured).
- **No application-level rate limiting exists** (`ARCHITECTURE.md` §6,
  "Cost controls") -- confirmed by reading the code: no rate-limiting
  library, no throttling middleware anywhere in `app/`. `/chat` is gated
  by identity (a real OpenEMR login, above), not by call volume; a
  monthly Anthropic Console spend cap is the only volume-independent
  backstop today.
- **No secrets-rotation policy or vault/secrets-manager integration**
  (`ARCHITECTURE.md` §7.10) -- every credential is a long-lived value in a
  git-ignored `.env` file; nothing expires or rotates it on a schedule.
- **Langfuse runs as a single self-hosted container**, not a managed
  database (`ARCHITECTURE.md` §6) -- already caused one real incident
  (span ingestion silently undercounting under droplet CPU contention,
  `PERFORMANCE_BASELINE.md`/`ERROR_ANALYSIS.md` Entry 6); a managed DB is
  a named future architecture change, not built.
- **The `origin` GitHub remote is behind this project's actual work** --
  22 commits ahead on the primary working history as of this writing
  (`git log origin/main..HEAD`, checked 2026-09-20; this number grows
  every session and isn't re-verified between edits to this note) have
  not been pushed to `origin`; a separate `gitlab` remote is kept current
  instead, and is what the droplet actually deploys from. Anyone cloning
  from `origin` today gets a stale snapshot of this project.
- **`/chat` on the droplet (port 8420) no longer has the open, no-auth
  reachability previously documented here** -- superseded by Phase 1's
  `authorization_code` login requirement above. A bare `curl -X POST
  /chat` now gets a clean 401; a real OpenEMR login is required first.
  This removes the earlier accepted risk (anyone triggering real,
  cost-incurring Anthropic calls with zero auth) as a side effect of doing
  the auth migration properly, not as a separate fix.
- **RESOLVED 2026-09-20: the Bruno collection now documents and wires up
  the `copilot_session` requirement** the note below used to flag as
  missing -- every `chat/`/`patient/` request sends
  `Cookie: copilot_session={{copilot_session}}`, `bruno/README.md` walks
  through obtaining that cookie via a real browser login, and `auth/`
  (`/login`, `/callback`, `/me`) and `patient/` (`/patient`) requests were
  added, none of which existed in the collection before tonight.
  `/health` and `/ready` remain unaffected, they never required auth.
- No OpenEMR module / chart UI entry point (`ARCHITECTURE.md` 1.1) yet.
  `GET /ui` (`app/static/chat.html`, plain HTML/CSS/JS, no build step,
  same FastAPI app/port; rebuilt 2026-09-19 into a real styled chat
  interface, not the earlier bare page) exists as a standalone grader
  convenience, gated behind the same real OpenEMR login as `/chat`. It
  does not replace the real chart-embedded module, which remains the
  actual planned next step per `ARCHITECTURE.md` 1.1.
