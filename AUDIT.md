# Executive Summary

We audited a local OpenEMR 8.2.0-dev instance across three passes, security, architecture, and
compliance, using fabricated patient data and one account per role (physician, front desk, nurse,
billing, break-glass, admin). Findings were live-tested wherever possible, not just read from code.

**Two authorization bypasses let non-clinical roles write clinical data.** A front-desk or nurse
account, with no clinical permission at all, can create fabricated allergies and problems on
any patient's chart by omitting one request parameter, and can create unauthorized encounters
outright because the save endpoint has no ACL check whatsoever. Both were reproduced live. This
matters clinically, not just technically: a bogus or missing allergy directly drives prescribing
decisions.

**The root cause is architectural, not incidental.** OpenEMR enforces permissions in the legacy UI
via ~500 individual checks scattered across 270 files, with no central gate, so a missed check on
any one of 1,048 page-entry-points is silent and unrecoverable. The REST/FHIR API, by contrast, has
a single centralized authorization checkpoint every request passes through. This split is the
single most important architectural fact in the audit and drove our integration recommendation:
**build the Clinical Co-Pilot against the FHIR API, not the database**, since the API is the only
layer that is both centrally authorized and fully audited.

**That recommendation required a live correction.** Completing a real OAuth2 flow and pulling data
through the API surfaced that it does **not** apply the same sensitivity filtering the UI enforces
, a backend-service-scoped token can retrieve a "high sensitivity" visit that clinicians themselves
are blocked from viewing. This is the audit's most consequential finding for the AI layer: it means
the default integration pattern would send an agent the exact records an organization has flagged
as most protected, unless an explicit compensating control is added before any real data flows.

**The audit log, the system's main accountability mechanism, is not fully trustworthy.** It
misattributes some writes to the wrong patient, and we proved its tamper-evidence checksum can be
silently defeated by anyone with database write access (we forged an edit, then made it pass the
tamper report undetected). Break-glass emergency access is a standing, unrestricted super-user
account that can remove itself from oversight and disable logging entirely. Separately, no
data-retention or purge mechanism exists anywhere in the application, retention is a procedural
promise, not a technical control, and the deployment itself runs with every service exposed on
all network interfaces and default credentials in place.

**Bottom line:** none of this blocks the project, but it reorders the work. Before any real PHI
reaches an LLM, three things must happen first: an executed Business Associate Agreement, a
compensating control for the sensitivity-filtering gap, and stripping non-clinical identifiers
(e.g., SSN) under a minimum-necessary standard. The architecture recommendation stands; it now
carries one mandatory precondition rather than being unconditionally safe.

---

# OpenEMR, Full Audit
**Security + Architecture + Compliance working notes**

Environment: local docker easy-dev stack, OpenEMR 8.2.0-dev, DB schema v541, 283 tables.
All testing performed 2026-09-14 against a local instance with fabricated patient data only.

Three passes:
- **Part I, Security audit**: role-based access control across all six roles, patient data handling,
 session/auth, audit logging, deployment posture, application security. 14 findings.
- **Part II, Architecture audit**: request lifecycle, layering, data layer, integration points,
 module architecture, plus §6, a fully executed OAuth2 + FHIR verification.
- **Part III, Compliance & regulatory**: HIPAA audit controls, retention, breach-notification
 readiness, and BAA requirements for sending PHI to an LLM provider.

Evidence discipline throughout: claims are marked as read/traced vs. actually executed, and gaps are
flagged rather than guessed. No credential ever passed through the assistant, every login password
was typed by the user.

---

# PART I, SECURITY AUDIT

# OpenEMR security audit, working notes

Environment: local docker easy-dev stack, OpenEMR **8.2.0-dev**, DB schema v541. All testing 2026-09-14.
Scope: role-based access control, patient data handling, session/auth, audit logging, deployment posture,
application security. Testing is against a local instance with fake patients only.

Method: each role has its own login (the user typed every password; no credential ever passed through the assistant).
Checks are a mix of (a) clicking through the real UI, (b) loading pages/POSTing with that role's session cookie, and
(c) reading the database and ACL tables directly. Every result below says which method produced it.

## Accounts used
| user | name | ACL group | role |
|---|---|---|---|
| dr_1 | Doc One | doc | Physician (Provider ON) |
| front_1 | Front Desk | front | Front Office |
| clin_1 | Nurse Clinician | clin | Clinician |
| back_1 | Billing Accounting | back | Accounting |
| break_1 | Emergency Access | breakglass | Break-glass / Emergency Login |
| admin | Taylor Administrator | admin | Administrator |

## SUMMARY OF FINDINGS
| # | Sev | Finding | Status |
|---|---|---|---|
| 1 | HIGH | Authorization bypass in `add_edit_issue.php`: the ACL check is guarded by a client-controlled `thistype` param. Omit it and a role with no clinical permission writes clinical issues (allergies/problems). Proven: same POST with param → 403, without → 200 + record created. | Confirmed, upstream code |
| 2 | HIGH | `interface/forms/newpatient/save.php` has **no ACL check at all**, any authenticated session can create encounters regardless of role. Reproduced by front_1 AND clin_1, neither of which has encounter rights. | Confirmed, role-independent |
| 3 | MEDIUM | Audit log attributes a write to the **wrong patient** (record written to pid 1, logged as patient_id 4 from the stale session pid). Undermines "whose chart was changed". | Confirmed |
| 4 | MEDIUM | Dev stack binds every service to **all network interfaces** with the **macOS firewall off**; Mailpit and Selenium have no auth; default creds (`admin`/`pass`, MySQL `root`/`root`). | Confirmed, deployment |
| 5 | MEDIUM | **No MFA anywhere**, including admin. Nothing enrolled, nothing configured. | Confirmed |
| 6 | MEDIUM | Physicians are not restricted to their own patients: any doctor can open, edit and create visits on any patient, and the practice-wide Patient List report. Stock ACL has no per-provider panel. | By design, worth flagging |
| 7 | LOW | Wrong current-password attempts on Change Password do **not** increment the lockout counter, unlimited guessing from a hijacked session. | Confirmed |
| 8 | LOW | Full SSN displayed in Patient Finder, dashboard and edit form for every role that can see patients. Session cookie is JS-readable (by design, for multi-login). | Confirmed |
| 9 | LOW | Break-glass role is effectively a **second administrator** (ACL admin, DB reporting, document delete), not a limited emergency view, no check-out, justification, expiry or alert. Confirmed live: all 15 probed pages 200. | By design, worth flagging |
| 10 | HIGH | **Break-glass can dismantle its own accountability**: it can edit its own group membership (verified: own user record opens with an editable group selector + Save) to leave `breakglass` and stop forced logging, and it holds Configuration access to disable `enable_auditlog` entirely. Chain verified read-only, not executed. | Confirmed |
| 11 | MEDIUM | **Free-text clinical data loses content in structured FHIR fields.** An uncoded allergy ("Penicillin (hives)") returns `code` = `data-absent-reason: unknown`; the allergen survives only in the narrative `text.div`. An agent reading `AllergyIntolerance.code` correctly would see "allergy: unknown". | Confirmed |
| 12 | HIGH | **FHIR API does not apply sensitivity filtering.** The `sensitivity='high'` encounter that the UI 403s for `clin_1`/`back_1` is returned in full via `GET /fhir/Encounter?patient=…` with a `system/` scope token. Sensitivity is a UI-layer control only; no check exists in the FHIR read path. | Confirmed |
| C-1 | HIGH | **Audit-log tamper evidence is defeatable.** The per-row SHA3-512 checksum is unkeyed and stored in the same DB with no hash chaining, a DB-level actor can edit a log row and recompute the checksum. Both halves proven live (edit → detected; recompute → undetectable), then reverted. See Part III §1.2, below. | Confirmed |
| C-2 | MEDIUM | **No data retention/purge mechanism exists.** No global, script or scheduled job implements time-based PHI deletion; retention is purely procedural. See Part III §2, below. | Confirmed |
| 13 | MEDIUM | **`DocumentService::search()` (the FHIR `DocumentReference` route) does not filter `deleted = 1` documents** -- a soft-deleted scan is still returned, just tagged `status: entered-in-error`, never actually excluded. `getAllAtPath()` (the Standard API route) filters it correctly; only the FHIR search path is affected. Found 2026-09-24, during clinical-copilot Week 2 work, not the original pass. See §6.9. | Confirmed, code-read only, not live-exploited |

**Positive findings (controls that work):** OAuth2 clients are registered **disabled** and require admin enablement before any token can be issued; API access is fully logged to a dedicated `api_log` table; no stored XSS (output correctly encoded on 7 screens); CSRF enforced
(tokenless POST → 400); logout fully invalidates the session server-side; `addonly` enforced on demographics at both
form and endpoint; sensitivity (`high`) enforced **in the UI** (but NOT via FHIR, see Finding 12); API/FHIR data endpoints require auth; document/DB/drive encryption
on and FileVault enabled; PHP not leaking errors or version.

---

# Physician role (dr_1)
dr_1 = "Doc One", ACL group `doc` (Physicians), facility 3. Provider + calendar flags turned ON 2026-09-14 (via DB).

## Test data (fake, inserted via SQL 2026-09-14)
Backup of affected tables before insert: `.pre-testdata-backup.sql` (repo root, git-excluded).
| pid | patient | provider | chart |
|---|---|---|---|
| 1 | Alice Testpatient 1972-03-14 | dr_1 | 2 problems, 2 meds, 1 allergy |
| 2 | Bob Testpatient 1958-11-02 | dr_1 | 1 problem, 1 med, 1 allergy |
| 3 | Carol Emptychart 1990-07-21 | dr_1 | empty (data-quality case) |
| 4 | Dan Otherprovider 1965-01-30 | admin | 1 problem, 1 med |
| 5 | Erin Otherprovider 1983-09-09 | admin | 1 problem, 1 allergy |
| 6 | Alice Testpatient 1972-03-14 (no SSN, "Lane", different phone) | dr_1 | duplicate of pid 1, conflicting metformin dose |
SSNs use the 900- range (never issued). No encounters, labs or insurance policies loaded.

## Physicians (`doc`) group permissions (from gacl tables)
Granted (write unless noted): Demographics, Medical/History, Prescriptions, Lab results + sign,
Documents, Patient notes, Appointments, Transactions, Amendments, Disclosures, Reminders/Alerts,
Patient Report (view); encounter Notes / Coding / Authorize for **my AND any** encounters,
Fix encounter dates (any); sensitivities normal + **high**; Price Discounting; Financial Reporting (my encounters);
**Inventory Administration**.
NOT granted: Billing (acct/bill), EOB/payments, Practice settings, any admin/super functions.
- Note for write-up: the `_a` ("any encounters") grants mean the ACL itself does not restrict physicians
 to their own patients or encounters. OpenEMR has no per-provider patient panel restriction in the stock ACL.

## Access boundaries
- [x] Admin pages via direct URL (dr_1 session). All returned HTTP 403 "Not Authorized":
   Users/Groups, Globals/Configuration, ACL admin, Logs viewer, Backup, Facilities,
   Layout editor, Audit Log Tamper Report. Editing admin's profile (user_admin.php?id=1): 403 "Authentication Error".
- [x] **Other providers' patients: fully accessible.** In the real UI, dr_1 opened Dan Otherprovider (pid 4, admin's patient):
   dashboard loaded, no warning or break-the-glass prompt. Also opened pid 5 by URL.
- [x] Billing / financial admin via direct URL. **403 Not Authorized**: Billing Manager, New Payment, EOB Posting,
   Search Payments, Practice Settings (incl. Insurance Companies, Pharmacies), Codes admin, List editor,
   Collections report, Duplicate Patient Management.
- [x] **Allowed (HTTP 200)**: Drug Inventory management, Sales by Item report, Patient Ledger.
   Inventory write access for a physician role is worth flagging (normally pharmacy/admin).
- [ ] Create/delete users through the UI (URL-level access to user admin already denied)
- [ ] Attending vs resident/supervisor distinctions (no resident account exists; stock ACL has no such split)

## Patient data handling
- [x] **Search scope: whole practice.** UI Patient Finder as dr_1 listed all 6 patients, including both of admin's.
- [x] **SSN exposure: full SSN** in a Patient Finder column (all rows), on the dashboard and on the demographics edit page,
   for dr_1's patients and admin's. No masking.
- [x] **Demographics editable for another provider's patient (real save).** In the UI, dr_1 opened Edit Demographics for
   pid 4 (admin's patient), changed home phone 555-0104 → 555-0144, clicked Save. It saved (confirmed in DB).
   Reverted afterwards directly in the DB, so the revert is NOT in the audit log.
   Edit form exposes ~115 editable fields including SSN, name, DOB.
- [x] **Insurance editable by physician.** In the UI (dashboard → Insurance pencil), dr_1 got the full
   "Edit Current Insurance" form for pid 4: Primary/Secondary/Tertiary, plan, policy #, group #, subscriber,
   47 editable fields, Save buttons. Not saved (no insurance companies exist). Insurance edit rides on the
   Demographics write permission, not on billing permissions.
- [ ] Whether the "Search/Add/Edit" insurance-company button lets dr_1 create insurance companies (Practice Settings is 403)
- [x] **Encounter attributed to another provider (real save).** In the UI, dr_1 created a New Encounter on pid 4
   (admin's patient) with Provider = **Taylor Administrator**, Sensitivity = **high**, Office Visit, billing facility set.
   Saved as form_encounter id 1 / encounter 7 with provider_id=1 (admin). The provider dropdown lists every provider.
   Traceability: `forms.user` = dr_1 and the audit log has `patient-record-insert` (user dr_1, patient_id 4).
   So the chart says "admin's visit", but the log shows dr_1 created it. LEFT IN PLACE as test data.
   The form also exposes billing facility, "In Collection" and place of service to the physician.
- [x] **Fee sheet open to physician.** Fee Sheet for that encounter loads for dr_1 with price level, code search
   (CPT4/HCPCS/ICD10), Add Copay, Rendering/Supervising provider pickers (all providers), Save. Billing lines are
   NOT live-tested: the code tables are empty (0 CPT4 codes) and the Add Copay flow didn't complete in automation.
- [x] Also allowed for dr_1 on admin's patient (HTTP 200): full Patient Report (chart export page), Documents, Disclosures,
   Prescriptions, Amendments, editing the encounter form (view.php, includes the date).
- [x] Practice-wide reports allowed (HTTP 200): **Patient List (all providers)**, Appointments, Unique Seen Patients, Clinical.
   Patient List = bulk listing of the practice's patients. Care Coordination (CCDA) module: Not Authorized.
- Side finding: opening insurance_edit.php as a standalone page (outside the main tab frame) hangs on "Loading…"
 (JS error: `Cannot read properties of undefined (reading 'assetVersion')`). UI bug, not a security issue.

---

# Front desk role (front_1), tested 2026-09-14
front_1 = "Front Desk", id 6, ACL group `front` (Front Office), facility 3, Provider OFF, calendar flag OFF.
Created via admin Add User (user typed both passwords). Main Menu Role = Front Office.

## Front Office (`front`) group permissions (from gacl tables)
Granted: Demographics (write), Appointments (write), Clinical Reminders/Alerts (**view only**),
group calendar (write). That is all. No medical/history, no notes, no labs, no rx, no encounters, no billing, no admin.

## What is correctly blocked (403 / "Not Authorized")
Medical Issues list, Patient Report, Prescriptions, Documents, Procedure/Lab Results, Billing Manager,
Users/Groups admin, Globals/Configuration, Logs Viewer. Visit History renders inline "Encounters not authorized".
Menu is correctly reduced (File / View / Patient / Popups / Miscellaneous only).

## What front desk CAN reach (by design, demographics/appointments)
- Patient dashboard for **any** patient (incl. other providers'), with **full SSN**. Clinical widgets are suppressed
 (no meds/allergies/problems text leaked), the dashboard degrades correctly.
- Edit Demographics for any patient (~115 fields incl. SSN/DOB), expected from `patients/demo` write.
- Insurance editor (`insurance_edit.php`), same as dr_1, rides on Demographics write, NOT on billing perms.
 Front desk editing insurance is normal practice, but note it is NOT gated by any billing ACL.
- Practice-wide **Patient List report** (all providers), bulk patient listing.
- Patient Finder (all patients).

## FINDING 1 (HIGH): authorization bypass on issue creation via client-controlled `thistype`
**Front desk can write clinical records (allergies / health concerns / problems) it has no permission for.**
- `interface/patient_file/summary/add_edit_issue.php:79`:
 `if ($thistype && !$issue && !AclMain::aclCheckIssue($thistype, '', ['write','addonly'])) { deny(); }`
 `$thistype` comes from `$_REQUEST['thistype']`, **client-controlled**. If the parameter is absent the entire
 ACL check is skipped. The issue type actually written comes from a *different* field (`form_type`).
- `issue_types.aco_spec` for health_concern / allergy / medication / medical_problem = `patients|med`,
 which Front Office does NOT have, so the check *would* deny if it ran.
- **Proof (same session, same patient, same body, only the param differs):**
 | request | result |
 |---|---|
 | POST with `thistype=health_concern` | **403 Access denied** |
 | POST without `thistype` | **200, issue created** |
- Live result: front_1 created `lists` id 14 and 15 on pid 1, type `health_concern`, severity "mild",
 reaction "hives", user=front_1. Left in place as evidence, titled "AUDIT TEST …".
- Twist: front_1 **cannot read** the issues list (stats_full.php → 403) but **can write** to it. Write-without-read.
- Impact: a front-desk account can inject false clinical data (allergies, problems) into any patient's chart.
 Clinically this is the dangerous direction, a bogus or deleted allergy drives prescribing decisions.

## FINDING 2 (HIGH): no ACL check at all on encounter save
- `interface/forms/newpatient/save.php` contains **zero** ACL/aclCheck calls (grep: no matches), although the
 Front Office role has no `encounters` permission of any kind.
- Live result: front_1 POSTed to save.php and created `form_encounter` id 2 / encounter 8 on pid 4,
 provider_id = 6 (itself). `forms.user` = front_1.
- Front desk creating a visit shell at check-in may be intended workflow, but it is **not enforced by any ACL**,
 the gate is only that the menu hides it. Anyone with a session can POST directly.

## FINDING 3 (MEDIUM): audit log records the wrong patient
- The issue written to **pid 1** was logged as `patient-record-insert` with **patient_id = 4** (the stale
 session patient), user=front_1. The log attributes a clinical write to the wrong chart.
- Impact: audit trail cannot be trusted to answer "whose record was modified", directly relevant to the
 Compliance section (HIPAA §164.312(b) audit controls).

## Notes
- Writes by front_1 ARE logged (user attribution correct, patient attribution wrong, see Finding 3).
- These are upstream OpenEMR code issues (8.2.0-dev), not misconfiguration of this instance.
- 2026-09-14: Taylor indicated these appear to be already documented/known upstream, so no disclosure action taken.
 NOT independently verified against a current OpenEMR release or the upstream issue tracker, if these findings get
 used in the written audit, confirm that status first.

---

# Clinician role (clin_1), tested 2026-09-14
clin_1 = "Nurse Clinician", id 7, ACL group `clin`, Provider OFF, Main Menu Role Standard.

## Correctly enforced (good news)
- **`addonly` on Demographics is REAL and enforced at both layers.** Edit Demographics form → **403 Access denied**,
 and a direct POST to `demographics_save.php` with a valid CSRF token → **403 Access denied**. DB confirmed unchanged
 (pid 1 phone still 555-0101). This is the correct pattern, contrast with Findings 1 and 2.
- **Sensitivity enforcement works.** The `high` sensitivity encounter dr_1 created (form id 1) → **403 Not Authorized**
 for clin_1, which has no High grant. The nurse cannot open the sensitive visit.
- Procedure/Lab Results → 403. Billing Manager → 403. Users/Groups admin → 403.

## Accessible (consistent with the matrix)
Patient dashboard, Medical Issues list, Patient Report (view), Prescriptions list, Documents list, Visit History.

## FINDING 2 confirmed role-independent
clin_1, which has no encounter-creation grant, POSTed to `interface/forms/newpatient/save.php` and created
`form_encounter` id 3 / encounter 9 on pid 1, provider_id = 7 (itself), `forms.user` = clin_1.
Same as front_1. The endpoint has **no ACL check at all**, so *any* authenticated session can create encounters
regardless of role. This is not a front-desk quirk, it is a missing check on the endpoint.

## Also noted
- Fee Sheet (`/interface/forms/fee_sheet/new.php`) returns 200 for clin_1 (and did for front_1), despite neither role
 holding a Billing ACL. Without an encounter in context it renders an empty sheet, so impact is unclear, but the
 page itself is not permission-gated. Worth a closer look if billing integrity matters to the write-up.

---

# Accounting role (back_1), tested 2026-09-14
back_1 = "Billing Accounting", id 8, ACL group `back`, Provider OFF.

## Correctly blocked (clinical wall holds)
Medical Issues, Patient Report, Prescriptions, Documents, Procedure/Lab Results → all **403**.
Users/Groups, Configuration (globals), Logs Viewer → **403**. No clinical text (meds/allergies/problems) ever rendered.

## Billing access present (as expected for the role)
Billing Manager, New Payment, EOB Posting, Collections Report, Patient Ledger → **200**.
**Practice Settings** (Insurance Companies, Pharmacies, X12 partners) → **200**, clinic-wide config, broad for a billing role.
Can also edit Demographics (matrix: back demo=write) and sees **full SSN** on the dashboard/edit page.

## Sensitivity is a HARD gate (good), overrides "any encounter" grants
back_1 holds `encounters|auth_a`, `coding_a`, `date_a` (authorize / code / fix-dates on **any** encounter),
but NO sensitivity grant (neither Normal nor High).
- High-sensitivity encounter (enc 7): view/edit page → **403 Not Authorized**. Sensitivity blocks it *despite* the
 "any encounter" grants. This is the correct, defense-in-depth outcome.
- Normal-sensitivity encounter of the active patient (enc 8): view/edit page → **200, date field editable**.
 So `date_a` does let Accounting open a normal visit's encounter form and change its **service date**, with no
 Medical/History, Notes, or base Coding access. Re-dating a visit shifts claim/billing timing; plausibly intended for
 billing staff, but it is a genuine write to the clinical encounter record. (Did not execute the date-change save to
 avoid corrupting test data; save.php has no ACL check per Finding 2, and date_a explicitly authorizes it, so it would
 succeed.)

## Fee Sheet is NOT accessible to Accounting (nuance vs. the matrix)
Despite holding **Coding – any encounters** (`coding_a`), the Fee Sheet bounced ("Not authorized" → formJump) for
back_1. `interface/forms/fee_sheet/new.php:32` gates on `aclCheckForm('fee_sheet')` = registry aco `encounters|coding`
(the **base** coding ACO), which back does NOT hold, it only has `coding_a`. So "Coding – any encounters" does not, by
itself, open the actual coding tool. Net: Accounting can re-date encounters but cannot use the Fee Sheet to add charge
codes. Worth stating precisely in the write-up so the matrix isn't over-read.

---

# Break-glass / Emergency Login role (break_1), tested 2026-09-14
break_1 = "Emergency Access", id 9, ACL group `breakglass`.

## How break-glass actually works here (code inspection)
- Membership is **just the `breakglass` ACL group**. There is no check-out, no justification prompt, no time limit,
 no expiry, and no notification to anyone. It is a permanent standing super-user, not a temporary elevation.
- `gbl_force_log_breakglass=1` is its only distinguishing control: `EventAuditLogger` logs break-glass activity even
 when audit logging is globally disabled and even for query events that are otherwise switched off
 (`EventAuditLogger.php:411,441,516`), via `BreakglassChecker::isBreakglassUser()`.

## Live access: total
All 15 probed pages returned **200**, no denials anywhere:
high-sensitivity encounter (**content fully readable**, incl. the visit dr_1 marked `high` that clin_1 and back_1 were
denied), Medical Issues, Patient Report, Prescriptions, Lab Results, Edit Demographics, Billing Manager,
Practice Settings, **Users/Groups admin, Configuration (globals), ACL Administration, Logs Viewer, Backup
(incl. Eventlog backup), Audit Log Tamper Report, Database/Rules Reporting**.

## Forced logging IS working
930 log rows attributed to `break_1` within minutes of use, the control operates as designed, and noisily.

## FINDING 10 (HIGH): break-glass can dismantle its own accountability
The forced-logging control keys off **group membership**, and break-glass holds the access needed to change that
membership and to disable logging outright. Verified (read-only, chain NOT executed):
1. `break_1` opened **its own** user record (`user_admin.php?id=9`) → 200, showing the Access Group multi-select with
  all six groups, `Emergency Login` currently selected, and a working Save button. It can move itself to
  **Administrators**, which per the matrix retains full clinical + financial + admin rights, and thereby stop being
  a breakglass user, ending forced logging.
2. `break_1` has **Configuration (globals)** → 200, where `enable_auditlog` lives, so it can switch audit logging off.
3. `break_1` has **Backup**, **Logs Viewer** and the **Tamper Report**, so it can read and export the audit trail.
Net: the one accountability mechanism on the emergency role is administrable *by that role*. The group change itself
would be logged (still breakglass at that moment), so the trail is not invisible, but a reviewer would have to be
watching for exactly that event, and nothing alerts on it.
**Not executed**, this would have altered ACLs and logging config on the instance. Evidence is page access + the
logging code path, not a performed privilege change.

## Checked and clean: case-sensitivity bypass does NOT work
`BreakglassChecker` matches the username with `BINARY` (case-sensitive), which would be bypassable if login were
case-insensitive. It is not: `AuthUtils` also uses `BINARY \`username\`` throughout (lines 329, 380, 845, 1092…),
and both `users.username` and `gacl_aro.value` are `utf8mb4_general_ci` but always queried with BINARY.
So a break-glass user cannot log in as `BREAK_1` to evade the forced-logging check. Consistent handling, good.

---

# Role permission matrix (all 6 groups, from gacl tables, 2026-09-14)
Read from the DB, no account needed. `write` = full, `addonly` = may add but not modify, `view` = read-only,
blank = no access.

| Capability | doc | clin | front | back | breakglass | admin |
|---|---|---|---|---|---|---|
| Demographics | write | addonly | write | write | write | write |
| Medical/History | write | **write** | - | - | write | write |
| Prescriptions | write | addonly | - | - | write | write |
| Lab Results | write | addonly | - | - | write | write |
| Sign Lab Results | write | - | - | - | write | write |
| Patient Notes | write | addonly | - | - | write | write |
| Documents | write | addonly | - | - | write | write |
| Documents Delete | - | - | - | - | **write** | write |
| Patient Report | view | view | - | - | write | write |
| Sensitivity: Normal | write | addonly | - | **-** | write | write |
| Sensitivity: High | write | **-** | - | **-** | write | write |
| Coding – any encounters | write | - | - | **write** | write | write |
| Notes – any encounters | write | - | - | - | write | write |
| Billing | - | - | - | **write** | write | write |
| EOB Data Entry | - | - | - | write | write | write |
| Financial Reporting – anything | - | - | - | write | write | write |
| Practice Settings | - | - | - | **write** | write | write |
| ACL Administration | - | - | - | - | **write** | write |
| Database Reporting | - | - | - | - | **write** | write |

## Observations from the matrix (no live testing needed)
- **Clinicians are `addonly` almost everywhere** (demographics, rx, labs, notes, documents) but **full `write` on
 Medical/History**. Worth live-testing whether `addonly` genuinely blocks *modifying* an existing record, that is the
 whole point of the setting, and Findings 1–2 show OpenEMR's enforcement is inconsistent.
- **Clinicians cannot see High-sensitivity encounters** (doc can). A nurse is locked out of the sensitive visits.
- **Accounting (`back`) has no sensitivity grant at all**, neither Normal nor High, yet holds
 **Coding on any encounter** and **Fix encounter dates**. So billing staff may be able to re-code and re-date visits
 they arguably cannot properly view. Prime live test.
- **Accounting holds Practice Settings (write)**, clinic-wide configuration, incl. insurance companies and pharmacies.
 That is a broad grant for a billing role.
- **Break-glass is effectively a second administrator**: ACL Administration, Database Reporting, Documents Delete,
 full clinical + financial. It is not a "temporary extra visibility" role, it is root.
 Mitigating control: `gbl_force_log_breakglass=1`, so its use is forced into the audit log.
- Only break-glass and admin can **delete documents**; no clinical role can.

---

# Cross-cutting findings (not role-specific)
These apply to the instance as a whole. Where a check was run from one role's session, that role is named.

## Session & auth
- [x] Idle timeout global `timeout` = 7200 s (2 h). Long for shared clinical workstations.
- [x] Password policy: secure_password=1, min length 9, expires 180 days, lockout after 20 failed logins (lenient).
- [x] Related lockout globals: failed-login counter resets after 3600 s; per-IP lockout after 100 failures;
   password history = 5 (can't reuse last 5); password expiry grace 30 days; max length 72. Portal idle timeout 1800 s.
- [x] **Change password requires the current password (live UI test + code).** In Change Password as dr_1:
   wrong current → "Incorrect password!"; empty current → "Password update error! Empty username or password.";
   mismatched repeat → "Passwords Don`t match!". The server enforces all three (AuthUtils::updatePassword), not just the page.
   Password hash confirmed unchanged afterwards (backup of the dr_1 users_secure row: `.pre-testdata-dr1-secure.sql`, git-excluded).
- [x] Order of checks in code: current password → min length (9) → max length (72) → strength → history. A weak new password
   ("abc") isn't rejected in the browser; it's only caught on the server after the current password verifies.
   Strength rejection itself NOT live-tested (would need dr_1's real password).
- [x] Failed change-password attempts ARE audit-logged (`password-change`, success=0, with IP and reason), but do
   **NOT** increment `login_fail_counter` (still 0 after 3 wrong attempts). Anyone with an unlocked dr_1 session
   can guess the current password without triggering lockout.
- [x] Session cookie: name `OpenEMR`, **readable by JavaScript (not HttpOnly)**. Confirmed live via `document.cookie`.
   This is deliberate in core OpenEMR (source comment: JS `restoreSession()` needs it for multi-login), but any XSS
   could steal the session. SameSite=Strict (CSRF mitigation); Secure=false on http://localhost (expected for dev).
- [x] PHP session storage: files in `/tmp` in the openemr container; gc_maxlifetime 1440 s (php.ini) vs 14400 in OpenEMR's
   session builder; cookie_lifetime 28000 s. use_strict_mode on.
- [x] **Logout properly invalidates the session (live test, dr_1).** Logged out via the UI; landed on login page.
   - Direct fetch of a patient page (demographics.php?set_pid=4) after logout returns NOT patient data but a
    session-timeout handler script, no cached PHI served.
   - A new session id is issued on logout (old e8d1e1… → new 629d89…).
   - Server-side session file for the old id still exists on disk but is **emptied of auth** (no authUser/authUserID/
    authProvider; 111 bytes). Replaying the old cookie would not re-authenticate. Physical file lingers until PHP GC, inert.
   - Logout is audit-logged (`logout` events).
- [ ] Simultaneous sessions in two browsers (needs a second concurrent login)
- [ ] Back button rendering a cached PHI page from bfcache (fetch test above is clean; a real click-Back on a rendered
   page not yet tried, worth confirming visually)
- [ ] Idle timeout actually enforced at 2 h (not live-tested; would require waiting)

## Deployment / network exposure (this dev stack on the Mac)
- [x] **All published ports bind to 0.0.0.0 / [::]** (every interface), not 127.0.0.1:
   8300 OpenEMR http, 9300 https, 8310 phpMyAdmin, 8320 MySQL, 5984/6984 CouchDB, 8025 Mailpit UI, 1025 SMTP,
   4444/7900 Selenium. OpenLDAP is not published.
- [x] **macOS application firewall is disabled.** Via the Mac's LAN IP (10.0.0.143): OpenEMR 302, phpMyAdmin 200,
   CouchDB 200, Mailpit 200, Selenium 302. Tested from the Mac itself, so this proves the listeners, not reachability
   from another device (router/Wi-Fi isolation not checked). Practical meaning: anyone on the same network can likely reach
   the login page, phpMyAdmin, and the DB port.
- [x] Default/dev credentials in use: OpenEMR `admin`/`pass`, MySQL `root`/`root` (both confirmed working locally),
   reachable through the exposed phpMyAdmin (login page served) and port 8320.
- [x] **Mailpit (8025) has no authentication.** It holds any email OpenEMR sends (e.g. password resets, portal invites).
- [x] **Selenium Grid (4444) has no authentication.** /status responds openly; an open grid lets others run browser sessions
   inside the docker network.
- [x] CouchDB requires auth (`_all_dbs` → "You are not a server admin").
- [x] HTTP security headers on the login page: X-Frame-Options DENY + CSP `frame-ancestors 'none'` (clickjacking protected),
   HSTS set (no effect over plain http). **Missing:** X-Content-Type-Options, Referrer-Policy, full CSP.
   `Server: Apache` (no version). Session cookie Max-Age 28000 s (~7.8 h).
- Context for write-up: this is the upstream *development* compose file, so these are expected for dev, but they are
 exactly the settings that must not carry into a real deployment.

## Application security (input handling, API, crypto), tested 2026-09-14
### XSS (stored)
- [x] Injected `<img onerror>`, `<svg onload>`, breakout `">`, and `<script>` payloads into pid 3's
   mname / street / city / email via the real Edit Demographics form, saved successfully.
- [x] **Output is safely encoded everywhere checked**, Dashboard, Patient Finder, Patient Report, New Encounter,
   Patient List report, Messages: payload rendered as inert text, 0 live injected elements, `top.__xss` flags never set.
   On the edit form the values come back only inside input `value=` attributes. **No stored XSS found.**
- Note: the app stores the raw markup in the DB (escaping is on output, not input). Any *future* screen or export that
 forgets to encode could still fire it, and because the session cookie is JS-readable that would be high impact. Test
 data was reverted (pid 3 fields cleared).

### Injection / error disclosure
- [x] The "OpenEMR SQL Escaping ERROR" seen earlier is a **whitelist guard** in `library/formdata.inc.php`
   (`escape_identifier`): unknown SQL identifiers hit `die()` before reaching the DB. It's a safe-by-default control,
   not an injection. Downside: it echoes the rejected string and halts (verbose error to screen).
- [x] PHP hardening in the container: `display_errors` OFF, `expose_php` OFF, no `X-Powered-By`. `Server: Apache` only.
   system_error_logging=WARNING.

### CSRF
- [x] Forms carry `csrf_token_form`; cookie is SameSite=Strict. POST to `demographics_save.php` with no token → **400**.
   Scripted document upload without a valid token → **400** (nothing written; documents table still 0).
   Server rejects tokenless writes, not just the page. (Full authed-session-minus-token test not isolated, but the
   400s + SameSite=Strict indicate CSRF is enforced.)

### REST / FHIR API
- [x] All APIs enabled: rest_api, rest_fhir_api, rest_portal_api, rest_system_scopes_api = 1; oauth_password_grant=3.
   Base URL https://localhost:9300.
- [x] Auth enforced on data endpoints: `/apis/default/api/facility` → 401, `/apis/default/fhir/Patient` → 401.
   `/apis/default/fhir/metadata` → 200 (public CapabilityStatement, expected per FHIR spec; no PHI).
- [x] **OAuth2 dynamic client registration is open**: unauthenticated POST `/oauth2/default/registration` → **200**.
   This is OpenEMR's standard RFC-7591 behavior (a registered client still needs a user to authorize + scopes),
   but an open registration endpoint is worth flagging for a production posture / rate-limiting review.
- [x] `gbl_force_log_breakglass=1` (emergency access forced to log); api_log_option on.

### Encryption at rest
- [x] Config: `drive_encryption=1` (document store AES), `secure_upload=1`, `couchdb_encryption=1`, `database_encryption=1`,
   document_storage_method=0 (local disk). Upload type restriction global present.
- [x] Host disk: **FileVault is On** (macOS). Docker named volumes hold DB/sites/couch data; encrypted at rest via FileVault.
- [ ] Not verified: that an actual stored document is unreadable on disk without the key (no documents exist yet).

### Version / patch level
- [x] OpenEMR **8.2.0-dev**, DB schema v541. A -dev build, not a tagged release, version-to-CVE mapping is fuzzy;
   for a real audit, pin to a released tag. CouchDB **3.5.2** version banner is exposed on :5984. Selenium/Mailpit unversioned here.
- [ ] Formal CVE cross-check of composer.lock deps (composer not installed on host; `composer audit` pending).

## Audit logging
- [x] Audit log enabled (enable_auditlog=1; query, patient-record, security-administration events on).
- [x] dr_1's denied admin/billing page attempts logged as `security-access-denied`, success=0, with the ACL that failed.
- [x] Failed logins recorded (9 in log so far).
- [x] **Chart views logged per patient.** `log` has `view` + `patient-record-select` rows with user=dr_1 and patient_id
   for pids 1, 3, 4, 6.
- [x] **Edits logged.** The pid 4 save produced `patient-record-update`, user=dr_1, patient_id=4, containing the SQL with
   the NEW value. The OLD value is not recorded, so the log alone can't show what changed from.
- [x] Direct DB changes bypass the audit log entirely (the phone revert left no entry). Expected, but relevant to tamper-evidence.
- [x] **Admin actions logged with actor (admin session, 2026-09-14).** Creating user `front_1` produced:
   `password-create` "Success for new user front_1" with source IP, `security-administration-insert` for the
   gacl_aro row, and the surrounding selects, all attributed to user=admin. Account creation is traceable.
- [x] **MFA: nothing enrolled.** `login_mfa_registrations` is empty (0 rows) and no MFA/TOTP/U2F globals are set.
   No second factor on any account, including admin. OpenEMR supports TOTP/U2F, it's simply unconfigured here.
- Note: a duplicate Save on the Add User form pops "User front_1 already exists.", the guard works, only one row created.
- [x] Encounter creation logged (`patient-record-insert` into form_encounter, user dr_1, patient_id 4), even though the
   encounter's provider is admin.
- Note: `log.comments` is base64-encoded; decode with `FROM_BASE64(comments)`. Volume is high: one dashboard view of
 pid 4 produced hundreds of `patient-record-select` rows, which makes human review hard.

## Data quality (as seen by dr_1)
- [x] pid 1: meds (Metformin 500 mg), problems and allergy (Penicillin) all render on the dashboard.
- [x] pid 3 (empty chart): dashboard loads with only "nothing recorded"-type placeholders. No warning that the chart is empty.
- [x] **Duplicate not flagged.** Patient Finder shows two "Testpatient, Alice" rows with the same DOB (TEST-0001 and TEST-0006).
   No duplicate warning to the physician. pid 6 shows Metformin **1000 mg** vs pid 1's **500 mg**: a physician
   could open the wrong record. Duplicate Patient Management exists but is 403 for dr_1.
- [x] Clinical reminders pop up as a blocking alert when opening a chart (pid 4: Weight, Colon/Prostate Cancer Screening,
   Influenza Vaccine, Tobacco). They fire from demographics alone.

## Performance (localhost docker, single user)
- [x] Denied admin/billing pages: 160–475 ms.
- [x] Patient dashboard (demographics.php): 1.5–1.8 s for dr_1's patients, 2.6 s for pid 4 (first load, reminders computed).
- [x] Edit demographics page: 0.7–1.1 s. Medical issues list: ~1.1 s. Patient Finder data: ~0.5 s.
- [x] **Admin vs dr_1 comparison (same pages, same box).** Admin is consistently FASTER; role permission checks do add
   measurable latency, but caching/first-load effects dominate:
   | page | dr_1 | admin |
   |---|---|---|
   | dashboard pid 1 | 1643 ms | 912 ms |
   | dashboard pid 4 | 2629 ms | 830 ms |
   | edit demographics pid 4 | 700–1064 ms | 471 ms |
   | medical issues pid 1 | 1093 ms | 255 ms |
   | patient report pid 4 | 774 ms | 238 ms |
   Caveat: dr_1's numbers were mostly cold first-loads (incl. clinical-reminder computation); admin's ran after
   those pages were warm. Not a controlled benchmark, treat as indicative, not proof that ACL checks cost ~2x.
- [ ] Labs timing (no lab data loaded)


---

# PART II, ARCHITECTURE AUDIT

- **[READ]**, traced by reading source at the cited path:line. Not executed.
- **[TESTED]**, actually executed against the running instance during this or the security audit.
- **[DB]**, read from the live database / information_schema.
- **[UNVERIFIED]**, inference or gap, explicitly flagged.

---

## 1. Request lifecycle

### 1.1 Traced example: loading a patient's demographics
Concrete request used throughout the security audit **[TESTED]**:
`GET /interface/patient_file/summary/demographics.php?set_pid=1` → HTTP 200, ~1.1–1.6 s.

Hop by hop:

| # | Layer | What actually happens | Evidence |
|---|---|---|---|
| 1 | Browser | Request to Apache in the `openemr` container, port 80 (published 8300) | **[TESTED]** |
| 2 | Apache | **No front controller.** The URL maps 1:1 to a file on disk; Apache hands the `.php` file to PHP-FPM/mod_php directly | **[READ]** no rewrite-to-router; file exists at that exact path |
| 3 | PHP entry | `interface/patient_file/summary/demographics.php` **is itself the entry point**, 2,080 lines mixing bootstrap, auth, business logic, SQL and HTML | **[READ]** `wc -l` = 2080 |
| 4 | Bootstrap | Line 30: `require_once("../../globals.php")`, the universal prologue. `interface/globals.php` (863 lines) loads Composer autoload (line 30), starts/validates the session (line 268 `SessionWrapperFactory`), builds the Symfony `Kernel` + event dispatcher (line 376), then loads `library/sql.inc.php` (line 384) | **[READ]** |
| 5 | AuthN | `globals.php:284` / `:726` → `require_once("$srcdir/auth.inc.php")`. If no valid session, `authLoginScreen()` → `exit` (`library/auth.inc.php:138,159`) | **[READ]** |
| 6 | AuthZ | **In the page itself**, not in bootstrap. `demographics.php:1056`: `$thisauth = AclMain::aclCheckCore('patients','demo');` plus ~12 further inline `aclCheck*` calls (lines 629, 1069, 1095–1098, 1219, 1271, 1358, 1384, 1396…) each gating a card/section | **[READ]** |
| 7 | "Business logic" | Inline in the page + procedural includes: `library/patient.inc.php`, `lists.inc.php`, `options.inc.php`, `clinical_rules.php` (required at lines 37–43) | **[READ]** |
| 8 | Data access | `getPatientData($pid)` → `library/patient.inc.php:68-72`, which is literally `"select $given from patient_data where pid=? order by date DESC limit 0,1"` → `sqlQuery()` | **[READ]** |
| 9 | DB driver | `library/sql.inc.php`, ~25 procedural functions (`sqlQuery:262`, `sqlStatement:96`, `sqlInsert:241`, `privQuery:555`…) wrapping ADODB. Every call also feeds `EventAuditLogger` (hence the audit-log volume noted in the security pass) | **[READ]** + **[TESTED]** (one dashboard view produced hundreds of `patient-record-select` rows) |
| 10 | Response | The same PHP file echoes HTML inline; Twig (`TwigContainer`) and card-render events are used for *parts* of the page only | **[READ]** `use OpenEMR\Common\Twig\TwigContainer` line 49 |

### 1.2 Routing: two different worlds
- **Legacy UI, no router.** Filesystem *is* the routing table: **1,048 `.php` files under `interface/`**, each independently reachable as a URL. **[DB/READ]** (`find interface -name "*.php" | wc -l`)
- **REST/FHIR API, real routing.** `apis/dispatch.php` → `OpenEMR\RestControllers\ApiApplication`, with route maps registered in `_rest_routes.inc.php:32-36` pointing at `apis/routes/_rest_routes_standard.inc.php`, `_rest_routes_fhir_r4_us_core_3_1_0.inc.php`, `_rest_routes_portal.inc.php`. **[READ]**

**This split is the single most important architectural fact in this document** and everything in §2 and §4 follows from it.

---

## 2. Layering

### 2.1 There are two architectures in one codebase
OpenEMR is mid-migration. Both styles are live and both are used in production paths:

**(a) Legacy procedural pages**, presentation, logic and data access in one file.
- Example: `interface/patient_file/summary/demographics.php`, 2,080 lines; SQL, ACL checks, and `<html>` interleaved.
- Example: `interface/patient_file/summary/add_edit_issue.php`, ACL check at line 79, business logic at 274, raw `$_POST['type']` read at 930.
- Scale: **1,938 direct `sqlQuery(`/`sqlStatement(` calls inside `interface/`**. **[READ]**

**(b) Modern service layer**, `src/Services/`, **83 service classes**, PSR-4 autoloaded, namespaced `OpenEMR\Services`. **[READ]**
- `BaseService` (`src/Services/BaseService.php:32`) provides `getOne()`, `search()`, `insert()`, `update()`, UUID handling, field introspection via `QueryUtils::listTableFields()` (line 69).
- Concrete: `PatientService` (`insert:219`, `update:307`, `search:418`, `getOne:632`), `EncounterService`, `ConditionService`, `AllergyIntoleranceService`, `PrescriptionService`.
- Scale: 512 direct SQL calls in `src/`, so even the "clean" layer writes SQL, but behind class boundaries.

**Verdict:** there *is* a real service layer, but it is **not** the layer the UI uses. The web UI largely bypasses `src/Services` and talks to the database directly through `library/*.inc.php` helpers. The service layer exists primarily to serve the **API**.

### 2.2 Where ACL checking actually happens, and the root cause of the security findings
Call-site census **[READ]**:

| Location | `aclCheck*` call sites | Distinct files |
|---|---|---|
| `interface/` (page scripts) | **497** | **270** |
| `src/` (classes) | 37 | - |
| `library/` | 23 | - |

`AclMain` (`src/Common/Acl/AclMain.php`) is a centralized *library*, `aclCheckCore()`, `aclCheckIssue():365`, `aclCheckForm()`, but **enforcement is not centralized**. There is no gate that every request passes through. Each of 270 files is individually responsible for calling it, correctly, before doing work.

**This is the architectural root cause of the security findings in Part I, above:**
- *Finding 2* (`interface/forms/newpatient/save.php` has **zero** ACL calls → any role can create encounters, reproduced by `front_1` and `clin_1` **[TESTED]**) is not an exotic bug. It is the predictable failure mode of per-file enforcement: one of 1,048 entry points simply omitted the check, and nothing upstream catches it.
- *Finding 1* (`add_edit_issue.php:79` guards its ACL check behind `if ($thistype && …)`, where `$thistype` comes from `$_REQUEST` **[TESTED]**: with the param → 403, without → 200 + record written) is the same class of defect, the check is *local, conditional, and client-influenced* because nothing forces it.
- The counter-example proves the point: `demographics_save.php` **does** check properly (`clin_1` blocked at both form and endpoint, DB unchanged **[TESTED]**). Correctness here is per-file craftsmanship, not a structural guarantee.

### 2.3 The API layer does it right, and the contrast is stark
The REST/FHIR stack has a genuine **Policy Enforcement Point**:
- `src/RestControllers/Subscriber/AuthorizationListener.php:37` implements `EventSubscriberInterface`, subscribing to `KernelEvents::REQUEST` at priority 50 (`:43`). Its own docblock (`:6`) calls `onKernelRequest` "the first PEP that checks the request and authorizes it".
- Scope enforcement at `:186-193`: builds `scopeType/Resource.permission`, and `if (!$restRequest->requestHasScopeEntity($scopeEntity)) throw new AccessDeniedException(...)`.

So: **every** API request is authorized centrally before reaching a controller; **each** UI request is authorized (or not) by whoever wrote that page. Same application, opposite architectures.

---

## 3. Data layer

### 3.1 Where patient data lives **[DB]**
| Domain | Table(s) | Notes |
|---|---|---|
| Demographics | `patient_data` | **132 columns**, wide/denormalized; `pid` is the key |
| Problems, allergies, meds-as-issues | `lists` | **single table, discriminated by `type`**, live values: `allergy`, `medical_problem`, `medication`, `health_concern` |
| Issue→ACL mapping | `issue_types.aco_spec` | e.g. all clinical types → `patients|med` |
| Encounters (visits) | `form_encounter` | 35 columns; `sensitivity` column drives the high-sensitivity gate |
| Encounter form registry | `forms` | maps `encounter`+`formdir`+`form_id` → the form's own table; has `deleted` flag |
| Encounter form data | **39 `form_*` tables** | one table per form type (`form_vitals`, `form_soap`, …) |
| Prescriptions | `prescriptions` (+ `drugs`) | **separate from** `lists.type='medication'` |
| Labs | `procedure_order` → `procedure_report` → `procedure_result` | 3-table chain |
| Documents | `documents` | |
| Insurance | `insurance_data` | |
| Billing | `billing` | |
| FHIR identity | `uuid_registry` (+ `uuid` column on 40 tables) | maps binary(16) UUIDs to table rows |

### 3.2 Abstraction: partial and bypassable
- `library/sql.inc.php`, procedural driver wrapper, used everywhere.
- `src/Services/*` + `QueryUtils`, the real abstraction, but only consistently used by the API.
- Service→table mapping is direct and, notably, **many-to-one** **[READ]**:
 - `ConditionService::CONDITION_TABLE = "lists"` (`:27`)
 - `AllergyIntoleranceService::ALLERGY_TABLE = "lists"` (`:27`)
 - `EncounterService::ENCOUNTER_TABLE = "form_encounter"`
 - `PrescriptionService::PRESCRIPTION_TABLE = "prescriptions"`

### 3.3 Schema realities that matter for an AI agent **[DB]**, read this section before writing any query
1. **Zero foreign-key constraints.** `information_schema.key_column_usage` where `referenced_table_name is not null` → **0**. Referential integrity is enforced only in application code, where it is enforced at all. Orphan rows are possible and joins cannot be validated by the schema.
2. **Inconsistent patient key naming.** **70 tables** use `pid`; **19 tables** use `patient_id`. A generic "join everything on the patient key" strategy will silently miss tables.
3. **Type-discriminated clinical table.** Problems, allergies and medications are all rows in `lists`, separated by `type`. Getting "the allergy list" means `WHERE type='allergy' AND activity=1`, and an agent that forgets `activity` will surface resolved/inactive items as current. (Our own test row landed as `type='health_concern'`, not `allergy`, because the form's numeric `form_type` maps differently than the label suggests, **[TESTED]**, see Part I, Finding 1, above.)
4. **Medications exist in two places.** `lists.type='medication'` *and* `prescriptions`. These are not synchronized by the schema. A co-pilot summarizing "current meds" must decide which is authoritative or reconcile both, **this is a clinical-safety-relevant modeling trap.**
5. **Encounter content requires double indirection.** To read a visit's notes: `form_encounter` → `forms` (filter `deleted=0`) → read `formdir` → query *that* form's own table. There is no single "encounter contents" table.
6. **Sensitivity is a column, not a row filter.** `form_encounter.sensitivity` ('high'/'normal'/'') is enforced in PHP (`interface/patient_file/encounter/forms.php:563,699`), **not** by the database. Any direct-SQL integration that ignores it will expose restricted visits that the UI correctly hides, confirmed in the security pass, where `clin_1` and `back_1` were both denied that visit **[TESTED]**.

**Can you get a clean patient snapshot in one query? No.** A minimally complete snapshot (demographics + problems + allergies + meds + encounters + labs) requires roughly 6–8 queries/joins across differently-keyed tables, with the `lists` discriminator, the `prescriptions`/`lists` medication split, the `forms` indirection, and PHP-only sensitivity rules all handled by hand. **[READ/DB]**

---

## 4. Integration points

### 4.1 What the API actually exposes **[READ]**
- **FHIR R4 / US Core 3.1.0**: **71 routes**, **33 resource types**,
 `AllergyIntolerance, Appointment, CarePlan, CareTeam, Condition, Coverage, Device, DiagnosticReport, DocumentReference, Encounter, Goal, Group, Immunization, Location, Media, Medication, MedicationDispense, MedicationRequest, Observation, OperationDefinition, Organization, Patient, Person, Practitioner, PractitionerRole, Procedure, Provenance, Questionnaire, QuestionnaireResponse, RelatedPerson, ServiceRequest, Specimen, ValueSet, metadata`
- **Standard (non-FHIR) REST API**: richer write surface, e.g. `GET|POST /api/patient`, `/api/patient/:puuid/encounter`, `/api/patient/:puuid/medical_problem`, `/api/patient/:puuid/allergy`, `/api/patient/:pid/medication`, `/api/patient/:pid/encounter/:eid/soap_note`, `/vital`.

**Co-Pilot coverage check**, demographics → `Patient`; meds → `MedicationRequest`/`MedicationDispense`; allergies → `AllergyIntolerance`; problems → `Condition`; labs → `Observation`/`DiagnosticReport`; encounters → `Encounter`. **All six required domains are covered.** ✅

### 4.2 Auth model **[READ]** + **[TESTED]**
- OAuth2 (league/oauth2-server) with grant types `authorization_code` (default), `refresh_token`, `password`, `client_credentials` (`src/RestControllers/AuthorizationController.php:99-100,571,669,725`). Live config: `oauth_password_grant=3`, all four `rest_*_api` globals = 1 **[DB]**.
- **SMART on FHIR** is implemented, `src/FHIR/SMART/` (`SmartLaunchController`, `SMARTLaunchToken`, `Capability`, `ResourceConstraintFilterer`).
- Scope-based authorization, centrally enforced (see §2.3). Scope strings are `user/Resource.perm`, `patient/Resource.perm`, `system/Resource.perm`.
- **[TESTED]** (security pass): `/apis/default/fhir/Patient` → **401**, `/apis/default/api/facility` → **401**, `/apis/default/fhir/metadata` → **200** (public CapabilityStatement, no PHI). Dynamic client registration `POST /oauth2/default/registration` → **200** (open, RFC-7591 standard behavior).
- **[UNVERIFIED]** I did **not** complete an OAuth2 flow or fetch real patient data through the API. Doing so needs either a user credential (which I don't handle) or a registered client with keys. **Everything in §4.1 about resource coverage is from route definitions, not from observed responses.** Worth confirming with one real token before relying on it.

### 4.3 Direct DB access as the alternative
Available and trivially fast (that's how most of this audit's verification was done **[TESTED]**), but as an integration path for a clinical agent it inherits every problem in §3.3, and critically:
- **It bypasses the only working authorization layer.** All the ACL logic, sensitivity gates, issue-type ACOs, role scoping, lives in PHP, not in the database (§3.3 #6). A direct-SQL agent has *no* access control unless it reimplements OpenEMR's ACL semantics, and the security audit shows those semantics are subtle enough that OpenEMR itself gets them wrong in places.
- **It bypasses the audit log.** `EventAuditLogger` is invoked from `library/sql.inc.php`, not from the DB. Confirmed **[TESTED]** in the security pass: a direct DB update left **no** audit entry. For a clinical AI touching PHI, unlogged access is a compliance problem (HIPAA §164.312(b)), not just an architectural preference.

### 4.4 Recommendation, use the FHIR API, not direct DB

**Use the REST/FHIR API with OAuth2 + SMART scopes. Do not integrate at the database layer.**

Reasoning, in priority order:
1. **Authorization.** The API is the only layer with centralized, enforced access control (§2.3). Direct SQL has none.
2. **Auditability.** API access is logged through the normal path; direct SQL is invisible (§4.3).
3. **Semantic correctness.** FHIR resources resolve the traps in §3.3 for you, the `lists` discriminator, the meds split, the `forms` indirection, instead of requiring the agent to re-derive them and get them subtly wrong on clinical data.
4. **Stability.** 283 tables with zero FKs and a live legacy→service migration is an unstable contract; FHIR R4 US Core is a versioned standard.

**Honest tradeoffs against that recommendation:**
- The API is **slower** than direct SQL, and a co-pilot assembling a full patient picture will make many resource calls (an N+1 pattern across 6 domains). If latency becomes the binding constraint, the right fix is a **read-through cache or a `$export`/bulk-data pull**, not dropping to raw SQL. Note `FhirOperationExportRestController` exists (`src/RestControllers/FHIR/Operations/`), bulk export is available **[READ]**.
- OAuth2 setup is real friction versus a DB connection string.
- **[UNVERIFIED]** FHIR write coverage for the specific fields a co-pilot might write back (e.g. a draft note) is not confirmed; the non-FHIR standard API is the likely write path (`soap_note`, `vital` endpoints exist).
- If a need arises that the API genuinely cannot serve, the correct escalation is **read-only** DB access for that narrow case, explicitly documented, never writes.

---

## 5. Module / extension architecture

### 5.1 How OpenEMR modules work **[READ]**
Formal system at `interface/modules/`, split into `custom_modules/` (modern) and `zend_modules/` (legacy Laminas, avoid). Eight shipped examples including `oe-module-ehi-exporter`, `oe-module-faxsms`, `oe-module-comlink-telehealth`.

Standard module shape (from `oe-module-ehi-exporter`):
```
composer.json      # PSR-4 autoload, type openemr-module
openemr.bootstrap.php  # entry point OpenEMR loads
info.txt         # module metadata
table.sql        # module's own schema
src/Bootstrap.php    # wires into the app
src/GlobalConfig.php   # module settings
public/, templates/
```
Modules attach via the **Symfony event dispatcher**, not by patching core: `src/Bootstrap.php:118 subscribeToEvents()` → `:121 $this->eventDispatcher->addListener(MenuEvent::MENU_UPDATE, ...)`. There are **24 event namespaces** under `src/Events/` (`Patient`, `PatientDemographics`, `Encounter`, `PatientReport`, `Billing`, …), including the card-render events `demographics.php` itself consumes (`CardRenderEvent`, `SectionEvent`, lines 52-55).

So: a module can inject UI into existing screens and react to domain events, genuinely useful for *surfacing* a co-pilot in the chart.

### 5.2 The significant find: OpenEMR already has a sanctioned AI extension point
`src/FHIR/SMART/ExternalClinicalDecisionSupport/` contains **[READ]**:
- `DecisionSupportInterventionEntity.php`
- `PredictiveDSIServiceEntity.php`, `const TYPE = 'predictive'`
- `EvidenceBasedDSIServiceEntity.php`
- `RouteController.php`

And it is wired into OAuth2 client registration **[DB]**:
- `oauth_clients` has a **`dsi_type`** column
- `dsi_source_attributes` table keyed by **`client_id`**, with `clinical_rule_id` and `source_value`

This is OpenEMR's implementation of **Decision Support Intervention registration**, the "predictive DSI" / source-attribute transparency model (ONC HTI-1 territory: disclosing the attributes behind AI-driven recommendations). A predictive decision-support service is expected to be a **registered SMART/OAuth2 client** declaring `dsi_type='predictive'` and its source attributes.

**[UNVERIFIED]** I did not exercise the DSI registration flow or confirm what the UI does with `dsi_source_attributes` at render time. The schema and classes exist; the end-to-end behavior is untested.

### 5.3 Recommendation, SMART app first, thin module only for UI

**Build the Clinical Co-Pilot as a separate service, registered as a SMART on FHIR / OAuth2 client with `dsi_type='predictive'`, consuming the FHIR API. Add a thin OpenEMR module only if you need it embedded in the chart UI.**

Why:
1. **It is not a novel integration, it is the intended one.** §5.2 shows OpenEMR 8.2 anticipates exactly this: an external predictive decision-support service registered as a client with declared source attributes. Building it as an in-process module would *bypass* the transparency mechanism the platform provides for AI.
2. **Separation of runtime.** A co-pilot wants its own dependencies, its own scaling, its own release cadence, and quite possibly Python. A PHP module inside OpenEMR gets none of that.
3. **Blast radius.** A module runs inside the app with ambient DB access and, per §2.2, inside a codebase where per-file authorization is demonstrably unreliable. An external client is constrained by OAuth2 scopes, centrally enforced.
4. **The module is still the right answer for *presentation*.** If the co-pilot needs to appear as a card on the demographics screen or a button in the chart, a thin module subscribing to `CardRenderEvent`/`MenuEvent` (§5.1) is the clean way to render it, while the intelligence stays in the external service.

**Recommended shape:**
```
[OpenEMR] --FHIR R4 + OAuth2/SMART scopes--> [Co-Pilot service]
  ^                       |
  | thin module: CardRenderEvent / MenuEvent  |
  +---- renders co-pilot UI in chart ------------+
  registered in oauth_clients with dsi_type='predictive'
```

**What to verify before committing to this design** (open items, honestly flagged):
- [ ] Complete one real OAuth2 flow and fetch a live `Patient` + `Condition` + `MedicationRequest`, confirm §4.1 against actual responses, not route tables.
- [ ] Confirm FHIR/standard-API **write** coverage for anything the co-pilot needs to persist.
- [ ] Exercise DSI registration (`dsi_type`, `dsi_source_attributes`) end-to-end and see what the UI surfaces.
- [ ] Decide the medications question (§3.3 #4): is `prescriptions` or `lists.type='medication'` authoritative, and does `MedicationRequest` read from one or both?
- [ ] Confirm the API applies the same `sensitivity` filtering the UI does (§3.3 #6). If it does not, that is a **finding**, not a design detail.

---

# 6. FHIR API verification, END-TO-END TESTED (2026-09-14)

Closes the §4.4 / §5.3 open item ("complete one real OAuth2 flow"). Everything below is **[TESTED]**
against the running instance unless marked otherwise. **The §4.4 recommendation survives, but with two
material corrections, see 6.5 and 6.6.**

## 6.1 OAuth2 client_credentials flow, COMPLETED
| Step | Result |
|---|---|
| `POST /oauth2/default/registration` (RFC-7591, unauthenticated) | **HTTP 200**, client created |
| Client created as | `client_role='user'`, **`is_enabled=0`**, `dsi_type=0` |
| Enablement | Required. Done via admin UI `interface/smart/admin-client.php` → "Enable Client" (as `break_1`) |
| `POST /oauth2/default/token`, grant `client_credentials`, RS384 JWT client assertion | **HTTP 200**, real Bearer token, `expires_in=300` |
| Scopes granted | all six requested: `system/{Patient,Condition,MedicationRequest,AllergyIntolerance,Observation,Encounter}.read` |

**Positive security finding:** open registration is *not* open access. A freshly registered client is
**disabled by default** and cannot obtain a token until an administrator enables it. This meaningfully
downgrades the "open dynamic registration" note in Part I, "Application security" &, registration
alone grants nothing.

**Gotcha for whoever builds this** (cost me one failed attempt): the JWT `aud` claim must be the
**configured** OAuth base URL from the `site_addr_oath` global, here `https://localhost:9300/oauth2/default/token`,
**not** the URL you actually POST to. Mismatch → `invalid_client` / "Client authentication failed". The real
reason is only visible in the container error log: `"The token is not allowed to be used by this audience"`.

## 6.2 Live data retrieval, CONFIRMED WORKING
Test patient: Alice Testpatient, pid 1, FHIR id `98c4b82b-b07e-11f1-8334-022958ad0af8`.

| Endpoint | HTTP | Returned |
|---|---|---|
| `GET /fhir/Patient/{id}` | 200 | name, DOB 1972-03-14, gender, SSN `900-00-0001` (us-ssn), MRN `TEST-0001` |
| `GET /fhir/Condition?patient={id}` | 200 | **total=4**, both problems + both health-concerns |
| `GET /fhir/MedicationRequest?patient={id}` | 200 | **total=2**, Metformin 500 mg, Lisinopril 10 mg |
| `GET /fhir/AllergyIntolerance?patient={id}` | 200 | **total=1** |

Cross-checked against `lists` for pid 1 **[DB]**: 2 `medical_problem`, 2 `medication`, 1 `allergy`,
2 `health_concern`. **Counts and clinical content match exactly.** Condition correctly maps
`medical_problem` → `problem-list-item` and `health_concern` → `health-concern` category.
§4.1's resource-coverage claim is now **observed, not inferred**.

## 6.3 RESOLVED, the medications question (§3.3 #4)
`FhirMedicationRequestService` delegates to `PrescriptionService` (`:94`, `:212`), whose base SQL is
explicitly **a UNION of `prescriptions` + `lists`** (`src/Services/PrescriptionService.php:88`, aliased
`combined_prescriptions` with a **`source_table`** discriminator column). **[READ]**

So the API *does* resolve the two-sources-of-truth trap: `MedicationRequest` returns both prescribed meds
and `lists.type='medication'` entries, tagged by origin. Confirmed live, our 2 meds exist only in `lists`
(`prescriptions` has 0 rows for pid 1 **[DB]**) and both came back. **This is a point in favour of the
API recommendation and against direct SQL**, where you would have to know to union these yourself.

## 6.4 RESOLVED, API access IS audited
A dedicated **`api_log`** table (separate from `log`) captured every call **[DB]**:

| id | user_id | method | request | url | time |
|---|---|---|---|---|---|
| 12 | 4 | GET | AllergyIntolerance | /apis/dispatch.php/default/fhir/AllergyIntolerance?patient=… | 22:42:26 |
| 11 | 4 | GET | MedicationRequest | …/fhir/MedicationRequest?patient=… | 22:42:26 |
| 10 | 4 | GET | Condition | …/fhir/Condition?patient=… | 22:42:25 |
| 9 | 4 | GET | Patient | …/fhir/Patient/98c4b82b-… | 22:42:25 |
| 8,7 | 0 | POST | | /oauth2/…/token (both the failed and successful attempt) | 22:41:57, 22:42:15 |
| 6 | 0 | POST | | /oauth2/…/registration | 22:40:43 |

Each row carries `log_id` linking into the main `log` table, plus `ip_address`, `method`, `request_url`,
`request_body`, `response`. Token *and* registration attempts are logged, including the failure.

**This confirms the §4.4 auditability argument**: API access is logged, direct DB access is not
(that bypass was confirmed **[TESTED]** in the security pass). For a PHI-touching agent this is the
decisive practical difference.

**Caveat:** `api_log.patient_id = 0` on all four data calls even though a specific patient's chart was
fetched. The URL preserves the patient UUID, so the information is recoverable by parsing, but a
"who accessed this patient's record" query over `api_log` will **not** find these. This is the same
class of defect as security-audit **Finding 3** (wrong patient attributed in `log`).

## 6.5 CORRECTION 1, FINDING 11 (MEDIUM): free-text clinical data loses its content in structured FHIR fields
The allergy stored as `Penicillin (hives)` came back as:
```json
"code": { "coding": [{ "system": ".../data-absent-reason", "code": "unknown", "display": "Unknown" }] },
"text": { "div": "<div ...>Penicillin (hives)</div>" }
```
**The allergen name is absent from the structured `code` field and survives only in the human-readable
narrative `text.div`.** `category` came back `["medication"]` and `reaction` was `null`, the "(hives)"
reaction was not structured either.

Cause: the entry has no coded (RxNorm/SNOMED) value; OpenEMR emits a US-Core `data-absent-reason`
rather than putting free text in `code.text`. **[TESTED]** + **[READ]**

**Why this matters more than it looks:** an agent reading `AllergyIntolerance.code`, the correct,
spec-compliant thing to do, sees *"allergy: unknown"* for this patient. A co-pilot summarizing
"known allergies" would silently omit a penicillin allergy. This is a **clinical-safety-relevant**
data-fidelity gap, not a formatting nit.

This **partially contradicts §4.4 reasoning point 3** ("FHIR resolves the traps for you"). It resolves the
*structural* traps (§6.3 proves the medication union) but **not** the *data quality* ones. Any co-pilot
must read `text.div` as a fallback whenever `code` is `data-absent-reason`, and should treat
uncoded entries as lower-confidence. How much real-world OpenEMR data is uncoded is **[UNVERIFIED]**,
worth measuring on a realistic dataset before sizing this risk.

## 6.6 CORRECTION 2, FINDING 12 (HIGH): the FHIR API does NOT apply sensitivity filtering
The security pass established that `form_encounter.sensitivity='high'` is a **hard gate in the UI**,
both `clin_1` and `back_1` got **403** on that exact visit (encounter 7, pid 4).

Via FHIR with a `system/Encounter.read` token **[TESTED]**:
```
GET /apis/default/fhir/Encounter?patient=98c4c335-…  → HTTP 200, total = 2
 - encounter 8 (sensitivity '')   "AUDIT TEST: encounter save attempted by front_1"
 - encounter 7 (sensitivity 'high') "AUDIT TEST: encounter created by dr_1, attributed to admin, sensitivity high"
```
**The restricted visit was returned in full, including its reason text.**

Code confirms it **[READ]**: the only `sensitivity` ACL check in `EncounterService` is at `:449-451`,
inside the **update** path (`updateEncounter`), there is no sensitivity filtering anywhere in the FHIR
**read/search** path, and `grep sensitivity src/Services/FHIR/` returns nothing. The UI's gate lives in
`interface/patient_file/encounter/forms.php:563,699`, i.e. in the presentation layer the API never touches.
This is §2.2's architectural split showing up as a concrete data-exposure difference.

**Fairness / scope of the claim:** this token used `system/` scopes (backend-service, no user context), so
OpenEMR arguably has no user ACL to apply. That is a reasonable design position for a backend service,
but it means **the sensitivity control is a UI-layer control, not a data-layer one**, and any integration
using `system/` scopes inherits *no* sensitivity restrictions at all.
**[UNVERIFIED]:** whether a `user/`-scoped token (authorization_code flow, bound to e.g. `clin_1`) *would*
enforce sensitivity. That test needs an interactive user login and was not performed. **Do not assume it does.**

**Consequence for the recommendation:** §4.4 stands, but requires a **compensating control**. A Clinical
Co-Pilot on `system/` scopes will see restricted visits that the treating clinicians themselves cannot.
Options, in order of preference:
1. Use `user/`-scoped tokens bound to the requesting clinician, *first verify sensitivity is enforced there*;
2. Filter on `sensitivity` in the co-pilot's own ingestion layer (requires reading it, note FHIR does not
  expose it as a field, so this may force a DB or standard-API read purely to obtain the flag);
3. Exclude sensitive encounters from co-pilot context entirely by policy.
This needs an explicit decision before any PHI flows to an agent.

## 6.7 Net effect on the §4.4 / §5.3 recommendation
**Unchanged, still use the FHIR API, not direct DB.** Strengthened on two of four original grounds:
auditability is now *proven* (§6.4), and semantic correctness is *proven* for the medication union (§6.3).
Weakened on data fidelity (§6.5). And a new, mandatory caveat: sensitivity filtering does not come for
free with the API (§6.6), direct DB would be *worse* here (no filtering either, plus no audit trail),
so this is not an argument for SQL; it is an argument for an explicit compensating control.

## 6.8 Test artifacts, CLEANED UP 2026-09-14
- **OAuth2 test client DISABLED** (done, verified **[TESTED]**). `Audit FHIR Verification Client`,
 id `NMUCnPnhntg2PlX1UixLZc9p5pp39gPrzYdiZ7lFiL0` → `is_enabled=0` **[DB]**, and a fresh token request now
 returns **HTTP 401 `invalid_client`**. The credential is revoked in effect, not merely hidden from the list.
 The client row still exists (disabled) so the registration remains as audit evidence; delete it entirely if
 you prefer no trace.
- **Key material destroyed**: the RS384 private/public key, all access tokens, and the signed assertions were
 deleted from `~/.openclaw/tmp/fhir-test/` and from the container. The `client_secret`, registration access
 token and JWKS were stripped from the saved registration response.
- **Retained as evidence** (no secrets): the four FHIR response bodies (`resp_*.json`), the redacted registration
 response, and the two small PHP helper scripts used to build the JWK/JWT.
- Two extra `health_concern` rows (ids 14, 15) remain on pid 1 from the security pass, and encounters
 7/8/9 remain, all clearly titled "AUDIT TEST".

## 6.9 CORRECTION 3, FINDING 13 (MEDIUM): `DocumentService::search()` does not filter soft-deleted documents, so FHIR `DocumentReference` can serve a deleted scan

Found 2026-09-24, not during the original 2026-09-14 pass -- while building the Clinical Co-Pilot's
Week 2 document-upload flow and auditing whether a maintenance script that soft-deletes an uploaded
document (`documents.deleted = 1`, the same flag `library/classes/Document.class.php::process_deleted()`
sets) actually keeps that document from being served back out. **[READ]**, not live-tested against a
running exploit -- traced two independent code paths, not executed against the instance.

**Two read paths, two different outcomes for the identical `deleted = 1` row:**
- `src/Services/DocumentService.php::getAllAtPath()` (the Standard API's `GET /patient/:pid/document`
  route, what the co-pilot's own `get_uploaded_documents` tool reads) has `AND doc.deleted = 0` directly
  in its SQL (`:107`). This path correctly excludes a soft-deleted document.
- `src/Services/DocumentService.php::search()` (the generic `BaseService`-style search the FHIR
  `DocumentReference` route uses, via
  `src/Services/FHIR/DocumentReference/FhirPatientDocumentReferenceService.php::searchForOpenEMRRecords()`,
  `:129`) builds its `WHERE` clause **entirely from the caller-supplied search parameters** via
  `FhirSearchWhereClauseBuilder::build()` (`:265`) -- there is no hardcoded `deleted = 0` anywhere in that
  query. A soft-deleted row is still returned by `GET /apis/default/fhir/DocumentReference?patient=...`.

The only place `deleted` is used at all in that second path is cosmetic: `parseOpenEMRRecord()`
(`:216-224`) sets the resource's FHIR `status` to `"entered-in-error"` instead of `"current"` for a
deleted row, but never removes it from the result set.

**Why this matters:** a document a clinician (or an operator) believed was removed -- superseded,
uploaded to the wrong record, or pruned as test-data cleanup -- is still fetchable by anyone who can
already reach that patient's `DocumentReference` search and knows (or can enumerate) the attachment's
title, via the exact same auth context that would have been authorized to see it before deletion. Not a
privilege escalation (no new principal gains access), but a **record-accuracy / "delete doesn't mean
gone" gap** -- the same class of concern as document-deletion permissions already discussed in the role
matrix (§ "Role permission matrix": only break-glass and admin can delete documents in the legacy UI;
this FHIR path shows that even a legitimate delete doesn't fully take effect at the API layer).

**Consumer-specific consequence, found via the clinical-copilot codebase, not the base OpenEMR audit:**
`clinical-copilot/app/ingestion/storage.py::get_source_document_bytes()` (the click-to-source preview
endpoint's backing function) makes this concrete -- it never reads `resource.get("status")` on the
matched `DocumentReference` at all, so it has no way to distinguish a live document from a soft-deleted
one even after the fact. See `clinical-copilot/app/main.py`'s `/documents/preview` route and
`get_source_document_bytes()`'s own fix (adds an explicit `deleted`/status check with a 404) for the
consumer-side mitigation; this AUDIT.md entry documents the underlying OpenEMR platform gap that made
that consumer-side fix necessary in the first place.

**Not fixed at the platform level here** -- out of scope for an application built on top of OpenEMR, not
a fork-maintainer decision to take lightly. Flagged for whoever owns `src/Services/DocumentService.php`
next: either `search()` should default to excluding `deleted = 1` unless a caller explicitly asks
otherwise (matching `getAllAtPath()`'s own behavior), or every FHIR-layer consumer needs to filter on
`status` itself, which `FhirPatientDocumentReferenceService` does not currently do or document as a
caller responsibility.

### 6.10 Clinical Co-Pilot known gaps found 2026-09-24/25 (application layer, not OpenEMR)

Detail and commit references are in `W2_ARCHITECTURE.md` §§4.10-4.15. Summary of what is still a gap:

- **Preview endpoint.** `/documents/preview` checked authentication but not that the matched document's
  subject was the requested patient (bounded by what the resident's token could already read; found by
  review, not exploited). Fixed in `d157078` (`fix/preview-endpoint-authz`) with fail-closed subject and
  `status: current` checks, on top of `7b32c29` (`entered-in-error`, MR !6). Both are now on `main`. A
  read-only staging check found every DocumentReference for the two fixture patients with uploads (33
  in total) carries a `Patient/<uuid>` subject naming that patient, so the fail-closed check would not
  have refused any of them; that covers two fixture patients only. See `W2_ARCHITECTURE.md` §4.13.
- **Click-to-source previews required OpenEMR admin (found 2026-09-25, fixed on `fix/preview-non-admin`).**
  After the security-hardening deploy, `test_clinician` (Clinicians group) got "This document preview is not
  available right now" on every citation. The preview read the source document through OpenEMR's FHIR
  `DocumentReference` search and `Binary` routes, which OpenEMR gates behind `admin`/`super`
  (`apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`, `RestConfig::request_authorization_check`)
  and `admin`/`users` respectively -- neither is held by Clinicians or Physicians by default
  (`library/classes/Installer.class.php`). Every non-admin clinician was affected; it looked fine in
  testing because the developer account is an Administrator. The 404s carried `document_preview_denied`
  reason `not_authorized` (an upstream 403), not a code regression from MR !10 (that MR touched none of
  this path); adding `test_clinician` to Physicians changed nothing, and Administrators fixed it. **Fix:** the
  preview now uses the Standard API document routes under the resident's own token (ACL `patients`/`docs`,
  which Clinicians hold, and the route `get_uploaded_documents` already used): resolve the pid, list the
  patient's Source Document category, fetch the id read from that listing. No privileges are borrowed. The
  status/subject checks on the FHIR resource are replaced by the listing's own SQL
  (`DocumentService::getAllAtPath`: category, `foreign_id` = this patient, `deleted = 0`), so another
  patient's, soft-deleted and ownerless documents are absent and answer the same 404. Denial reasons are
  now `not_found`/`not_authorized`/`invalid_patient_id` (the finer `deleted`/`patient_mismatch` split came
  from the FHIR resource and no longer exists). Not verified yet: the fix against a live non-admin account
  (staging and prod), see the MR. The `user/DocumentReference.s` and `user/Binary.read` scopes are still
  requested but no longer needed by the preview.
- **A guideline's drug name is stripped as an unverified patient claim (albuterol) -- known gap, not fixed.**
  In a final-run golden-set attempt (pipeline #29194, first attempt), `graph_answer_separates_patient_facts_from_guideline_evidence`
  drafted the guideline's general hyperkalemia management steps, including albuterol. Source attribution could not
  confirm "albuterol" against the patient's chart, removed that sentence ("I removed 1 detail(s) I could not
  confirm against this patient's chart: albuterol"), and left a numbered list that skips step 2; `verification_passed`
  went false and the gate blocked. The sentence was general guideline content, not a claim about the patient, and the
  verifier cannot tell the two apart -- the same family as "guideline text can falsely ground a patient claim" in the
  Week 2 known gaps, seen from the other side. Effect: an unpredictable gap in a clinically useful answer and an
  occasional gate block. Not fixed before submission; a real fix has to carry the source (chart vs guideline) of each
  drug mention into source attribution.
- **The allergy enforcer's window check accepts "not contraindicated" (pre-existing).** `_CONFLICT_SAFE_CUES` in
  `app/verification.py` includes the substring `contraindicat`, so "Amoxicillin is not contraindicated" near a
  documented penicillin allergy counts as the draft acknowledging the conflict and no HARD STOP is appended. Found
  2026-09-26 while testing the avoidance path below (which does reject it); the window check itself is unchanged.
  Fail-open direction: worth fixing before any real use.
- **Avoidance acceptance is a heuristic.** A drug named only in sentences that say to avoid it (e.g. "Within
  penicillins (...): high cross-reactivity is assumed -- this is a hard-stop") no longer gets a HARD STOP appended
  (2026-09-26; the final-run golden set flagged six class members that the model had listed, not recommended). It is
  sentence-scoped and fails closed on contrast, permission and double-negative wording, but no cue list is complete,
  so an unusual phrasing of a recommendation that contains an avoidance word and none of the blocking words would be
  accepted. Tested with 20+ phrasings and mutation-checked; not a substitute for the deterministic check.
- **Eval flake handling does not cover a verification failure on a case whose own check passed.** The per-case
  retry only fires when `check()` fails; a turn that passes its check but trips `verification_passed=False` blocks the
  gate immediately, with no rerun. Pipeline #29194 was blocked twice, on two different cases, before a manual retry
  passed.
- **Empty extractions.** An intake form with clean OCR came back with no fields (2026-09-26): the structuring call
  succeeded and the pipeline stored the empty result, which also blocked re-uploading the file (uploads are
  de-duplicated by content). Now an extraction with zero fields from a page with 25+ OCR words is retried once and
  otherwise refused before anything is stored; a `structuring_result` event logs counts only. The exact cause of the
  original empty result is unknown (the model's tool call is not logged, by design). The 25-word threshold is a
  judgment call, not a measured figure.
- **Dependency pinning is a snapshot.** After a CI-only crash from an unpinned `anthropic` upgrade, all
  dependencies are pinned exactly (`chore/pin-deps`, now on `main`), from the CI runner's
  `pip freeze` as of pipeline #28200. No hashes; updates need a deliberate re-freeze.
- **Degraded answers are disclosed, not retried.** A crashed worker is contained and named in
  `partial_failures`; the resident still gets an answer without that worker's evidence.
- **Truncation is disclosed, not prevented.** Caps are 4096 for chart and synthesis; a hit cap adds a
  note. Latency and cost rise with longer answers.
- **Eval-suite shared fixtures.** `eval_unit_tests` writes to PID1 while the Golden Set runs in the same
  stage; a possible flake source, not observed to fail.
- **Latency.** Measured in pipeline #28900 on the staging runner: graph turns that touch documents or
  guidelines ran at p50 17.2s / p95 74.4s (n=20); chart-only graph turns at p50 13.9s / p95 21.9s
  (n=10). The tail is `chart_data_worker` plus synthesis, not retrieval or OCR (`KEY_METRICS.md`). Not
  addressed.

### 6.11 Clinical Co-Pilot known gaps at final submission (2026-09-25)

Stated plainly. None of these is fixed. Each entry names the mechanism, so the gap can be
checked in the code.

- **`citation_present` checks presence, not precision.** `grade_citation_present` (`evals/gate.py`)
  passes a case if the response carries any citation at all. It doesn't check that each citation
  backs something the response says, or that each claim has one. The 29/29 in pipelines #28900 and #29930 says
  nothing about precision, and precision isn't among the five gate metrics.
- **Verification can't tell screening mentions from patient facts.** `app/verification.py` finds
  candidate terms by matching a curated vocabulary plus dose and lab patterns. It strips any
  sentence containing a term that isn't in the patient's data, however the term is used. "Screen
  for sepsis", "could this be pneumonia?", and a drug name echoed back from the resident's own
  question are all removed as if they were claims about the patient. Pipeline #28900 stripped
  `pneumonia` from a Bactrim-safety answer, and the graph cases stripped `heparin` from an
  anticoagulation answer. The gap also runs the other way. Retrieved guideline text is added to
  the grounded set, so a false patient claim that uses a term appearing in a retrieved guideline
  passes as grounded. The same gap strips general guideline advice. See the albuterol entry
  in §6.10. The planned fix is a role classifier
  that carries each mention's source, chart or guideline (`W2_ARCHITECTURE.md` §4.17).
- **An upload can be left half-written, and dedupe has a race.** `store_extraction`
  (`app/ingestion/storage.py`) runs four steps as separate HTTP calls:
  1. list the patient's source documents;
  2. compare content hashes;
  3. write the source document;
  4. write the extraction JSON.

  There is no rollback. If step 3 succeeds and step 4 fails, the scan stays in OpenEMR with no
  extraction, and the resident sees `storage_failed`. Retrying the same file is then rejected as a
  duplicate, because its hash is already on file. So that document can't be extracted again until
  someone deletes the orphan by hand. The agent reads only the extraction category, so it never
  sees the orphan either. Separately, the duplicate check and the write are not atomic: two
  identical uploads at the same moment can both pass the check and both be stored.
- **OCR runs on every citation click.** `/documents/preview` (`app/main.py`) fetches the file,
  rasterizes the page, and calls `run_ocr` to locate the quoted text on each request. `run_ocr`
  OCRs every page of the document, not just the one shown. Responses are `no-store` and nothing is
  cached. The bounding box computed at upload is already stored on each field
  (`DocumentCitation.bbox`) but isn't used here. Each click costs a full-document Tesseract pass
  on the droplet's CPU.
- **Click-to-source can highlight the wrong row for a short repeated value** (observed on
  production, 2026-09-26, on a scanned lab report). `resolve_bbox` (`app/ingestion/bbox.py`) returns
  the **first** run of OCR words on the page that matches the cited value, in reading order. It
  doesn't know which result row the field came from. A short value that repeats down the page,
  such as the abnormal flag `H`, therefore highlights the first `H` on the page, which can be a
  different result's row. Numeric values are usually unique on the page and resolved to the
  correct row in the same check. The citation's *value* is right; only the highlight box can be on
  the wrong line. Planned fix: anchor the search to the result's row, by searching near the bbox of
  the same result's test name or value instead of the whole page.
- **An extraction error on a noisy scan was not flagged** (observed on production, 2026-09-26, on
  the same scanned lab). The potassium reference range was extracted as `3.5-5.4`, but the page
  says `3.5-5.1`, and the field was not marked low-confidence. The plausibility check can't catch
  this kind of misread. It compares a result's value with that result's own printed reference
  range, so it has nothing to compare a misread range against, and `3.5-5.4` looks like a normal
  potassium range. Confidence tiers catch many misreads (the
  degraded-scan eval case marks its misread fields `low` or `unreadable`), but not all of them.
  This is why extracted facts are never written into the chart as authoritative data. They stay
  in the AI-Extracted Pending Review category, and every answer that uses them carries a forced
  "pending review" disclosure, so a clinician checks the source before relying on a value (§6.13).
- **Langfuse traces still contain PHI.** Log redaction (MR !10) replaces patient and resident
  identifiers with salted hashes in the app's structured logs, the access log and `feedback.jsonl`.
  It doesn't touch Langfuse. Trace payloads still carry the patient id and the chart content the
  model saw. Langfuse is self-hosted on the droplet, so the data doesn't leave the host, but the
  Langfuse UI and database are PHI stores. They need the same access control and retention rules as
  OpenEMR itself, and nothing in this project sets those up. The `no_phi_in_logs` gate doesn't
  inspect Langfuse.
- **No HTTPS on port 8420.** uvicorn serves plain HTTP on `0.0.0.0:8420`
  (`deploy/clinical-copilot.service`), and the firewall allows 8420. The session cookie is marked
  `secure` only when the app's base URL is https (`app/main.py`), so on the droplet the session
  cookie and the OAuth redirect travel unencrypted. The fix is a TLS-terminating reverse proxy in
  front of 8420. It isn't configured.
- **The CI shell runner sources the CI secrets file.** Every eval job runs `source
  /etc/clinical-copilot-ci.env` (`.gitlab-ci.yml`, `.staging_runner`) on a `shell` executor that
  shares a host with the staging MySQL. The file holds the Anthropic API key and the prune job's
  database account (SELECT on `patient_data`, UPDATE on `documents`). It also holds the OpenEMR
  OAuth credentials, since the evals can't start without them. Jobs run on merge-request
  pipelines. So code on any MR branch, whether an eval script, `requirements.txt`, or
  `.gitlab-ci.yml` itself, runs with those values in its environment and can reach the staging
  database directly. With one maintainer this is contained by who can open MRs, not by a control.
  The fix is a container executor, plus masked and protected CI variables scoped per job.
- **The sulfonamide allergy policy needs pharmacist review.** The class table
  (`app/clinical_reference.py`) treats a sulfa allergy as conflicting with sulfonamide antibiotics:
  - sulfamethoxazole-trimethoprim and its brand names (Bactrim, Septra, Sulfatrim);
  - sulfadiazine and silver sulfadiazine;
  - sulfisoxazole, sulfacetamide and sulfasalazine.

  It deliberately excludes non-antibiotic sulfonamides such as furosemide, hydrochlorothiazide and
  celecoxib, and it strips sulfate, sulfite and similar forms (so morphine sulfate doesn't match).
  Those are clinical policy choices. The project owner accepted them on 2026-09-25, but no
  pharmacist has reviewed them. They need that review before any real use
  (`W2_ARCHITECTURE.md` §4.16). The table is not a drug-interaction database.

### 6.12 Fixed before final submission (2026-09-25/26)

Details are in `W2_ARCHITECTURE.md` §§4.20-4.21.

- **Log redaction and a real `no_phi_in_logs` grader** (MR !10). Patient ids, other-patient ids and
  resident usernames are replaced in every log line by a salted HMAC token (`h_<12 hex>`, keyed by
  `LOG_HASH_SALT`). Tool-call argument values, routing rationales and flagged-claim text are dropped
  or reduced to counts. Query strings are stripped from the access log. The grader used to search
  five fixed strings and allowlisted `patient_id`. It now fails on any raw UUID other than
  `correlation_id`, on fixture identities, on resident usernames, and on clinical vocabulary.
  Twelve mutations of the redaction and the grader are each caught by tests. The redaction doesn't
  cover Langfuse (see §6.11) or server tracebacks.
- **Non-admin citation previews** (MR !12). OpenEMR gates the FHIR document routes behind
  `admin`/`super`. Previews now read through the Standard API under the resident's own token,
  with no borrowed privileges. See §6.10 for the finding.
- **Empty extractions and the allergy class-list false positive** (MR !14). The fixes and their
  limits are described in §6.10 ("Empty extractions", "Avoidance acceptance is a heuristic") and in
  `W2_ARCHITECTURE.md` §4.21.

### 6.13 How two PRD requirements are met: derived-fact persistence and citations

**Derived facts are persisted as OpenEMR records, which meets the PRD's "FHIR resources or OpenEMR
records" wording.** Each upload produces two OpenEMR document records:

- the original scan, in the Source Document category;
- the structured extraction as JSON, in the AI-Extracted Pending Review category (id 36).

The extraction carries the source document's id (`ExtractionResult.doc_id`), so every derived
fact can be traced to its source page. Uploads are deduplicated by content hash, with the caveat
in §6.11 that the check and the write aren't atomic.

**Why not FHIR Observations:**

1. This OpenEMR version's FHIR API has no create route for `Observation` (nor `DocumentReference`,
   `Binary` or `QuestionnaireResponse`). This was confirmed three ways: the route table, a
   service-layer no-op, and a live 404 on `POST` (`app/ingestion/storage.py`).
2. Even with a create route, writing unreviewed OCR-plus-LLM extractions straight into the
   structured chart would bypass clinician reconciliation. The values would become authoritative
   results that every FHIR client and every Observation-based rule reads, with nothing marking
   them as machine-read. Keeping them in a Pending Review document, always shown to the resident
   with a forced "pending review" disclosure, keeps them out of the chart until a clinician
   decides.

The cost: the values aren't queryable as discrete Observations, and no review-and-promote workflow
exists yet.

**Citations use the PRD's machine-readable shape, and the verified claims map to cited sources.**
Every citation in a final response is a structured object (`source_type`, `source_id`,
`page_or_section`, `field_or_chunk_id`, `quote_or_value`) for a FHIR resource, an uploaded-document
field, or a guideline chunk. Two mechanisms connect the text to those citations:

- **Verification strips what it can't ground.** Every term the verifier detects is checked against
  the data retrieved this turn: drug, allergy and condition names from its curated vocabulary
  (about 50 terms), plus medication doses and lab values. That data is exactly what the citations
  are built from. A detected term with no grounding is removed, along with its sentence, before
  the resident sees it.
- **Uploaded-document citations are filtered to stated facts.** An uploaded-document citation is
  kept only if its extracted value appears in the final text (`filter_citations_to_stated_facts`).

Limits, stated so this isn't overstated:

- A claim phrased with words outside the verifier's vocabulary and patterns isn't checked.
- Retrieved guideline text also counts as grounding (§6.11).
- FHIR and guideline citations aren't filtered to stated facts, so a response can carry more
  citations than it uses.

**Future enhancement: sentence-level claim-to-citation mapping.** Citations are attached per
source (per FHIR fact, extracted field, or guideline chunk) and returned as one list per response.
Nothing links a particular sentence to a particular citation. A model-emitted citation id per
claim would add that link (`W2_ARCHITECTURE.md` §4.7). That is an enhancement beyond the
citation contract, not a missing requirement.

---

# PART III, COMPLIANCE & REGULATORY AUDIT

**Evidence key:** **[TESTED]** executed live · **[READ]** traced in source · **[DB]** read from the database ·
**[UNVERIFIED]** flagged gap or inference.

This pass **synthesizes evidence already gathered** in the other two passes and cites it rather than
re-testing. Two things were newly tested here: audit-log tamper evidence (§1.2) and retention config (§2).

> **Scope caveat, stated once:** this is a *technical* audit of what the software does and does not enforce.
> HIPAA compliance is organizational as well as technical, policies, training, workforce sanctions and
> executed agreements sit outside anything that can be observed in a codebase. Nothing here is legal advice;
> the BAA discussion in §4 in particular should be reviewed by whoever owns legal/compliance.

---

## 1. Audit logging & controls, HIPAA §164.312(b)

§164.312(b) ("Audit controls") requires mechanisms that *record and examine activity* in systems containing ePHI.
OpenEMR has a genuinely substantial implementation, and four gaps that undermine what it can actually prove.

### 1.1 What works
- Audit logging is **on** and broad: `enable_auditlog=1`, with patient-record, query, security-administration,
 scheduling, order, lab-results, backup and http-request event classes enabled **[DB]**.
- Coverage is real, not nominal: one patient-dashboard view generated **hundreds** of `patient-record-select`
 rows **[TESTED]**, chart views are attributed per patient, and failed logins, failed password changes
 (with source IP and reason) and denied access attempts all land in the log **[TESTED]**.
- **API access is logged too**, in a dedicated `api_log` table carrying method, resource, full URL, IP and a
 `log_id` back-reference, including token requests and registration attempts, successful *and* failed
 (Part II §6.4) **[TESTED]**.
- Break-glass activity is **force-logged** (`gbl_force_log_breakglass=1`) even if audit logging were disabled
, 930 rows generated in minutes of use (Part I, break-glass section) **[TESTED]**.

### 1.2 Tamper evidence, NEWLY TESTED, and the result is two-sided
OpenEMR *does* implement audit-log tamper evidence: a **SHA3-512 checksum per log row**, stored in
`log_comment_encrypt` (35,900 rows **[DB]**), with an **Audit Log Tamper Report**
(`interface/reports/audit_log_tamper_report.php`) that recomputes and compares, and which also detects
**deleted** rows (`:233`) **[READ]**.

**Test performed [TESTED]**, on my own audit row (log id 23960, a `front_1` write), fully reverted afterwards:

| Step | Action | Result |
|---|---|---|
| 1 | Baseline recorded | `user='front_1'`, checksum stored, version 4 |
| 2 | Direct DB edit: `UPDATE log SET user='admin' WHERE id=23960` (a cover-up: reattributing a write to another user) | Row changed |
| 3 | Recompute checksum per the v4 formula (`audit_log_tamper_report.php:249`) | **MISMATCH → tampering detected** ✅ |
| 4 | Recompute the checksum *the same way the application does* and `UPDATE log_comment_encrypt SET checksum=…` | **MATCH → tampering now UNDETECTABLE** ❌ |
| 5 | Restored `user='front_1'` + correct checksum; verified against backup `23960\|front_1\|4\|1` | Clean |

**FINDING C-1 (HIGH): audit-log tamper evidence is defeatable by any actor with database write access.**
The checksum is an **unkeyed** `hash('sha3-512', date . event . category . user . groupname . comments .
user_notes . patient_id . success . crt_user . log_from . menu_item_id . ccda_doc_id)`, no HMAC, no secret,
no salt, and it is stored **in the same database** as the row it protects, with **no hash chaining** between
rows **[READ]** + **[TESTED]**.

So the control detects *naive* tampering (someone editing the `log` table directly, or a corrupted/dropped row)
but not *informed* tampering: anyone who can write to MySQL, which on this deployment means anyone with
`root`/`root` on the exposed port 8320, see Part I, Finding 4, can rewrite history and the tamper
report will report clean. **I proved both halves of this.**

What would close it: an HMAC keyed with a secret held outside the database, a hash chain linking each row to
its predecessor (so a single edit invalidates the tail), or shipping logs off-host to append-only/WORM storage.
None of the three exists here **[READ]**.

Also note: there is **no append-only enforcement**, the `log` table is an ordinary InnoDB table with normal
DML permitted **[DB]**; and the tamper report itself is reachable only by admin/break-glass, i.e. by exactly
the roles most capable of tampering (Part I, role matrix).

### 1.3 Audit-control gaps, framed as §164.312(b) deficiencies
These were found as security/architecture bugs in earlier passes; restated here as compliance gaps.

| Ref | Gap | Why it matters under §164.312(b) |
|---|---|---|
| Finding 3 | Log attributes a write to the **wrong patient**, record written to pid 1 was logged as `patient_id=4` (stale session pid) **[TESTED]** | The log cannot reliably answer *"who accessed/altered this patient's record?"*, the core question an audit control exists to answer, and the exact question asked in a breach investigation or a §164.528 accounting-of-disclosures request. |
| §6.4 | `api_log.patient_id = 0` on every FHIR data call, even when a specific chart was fetched **[TESTED]** | Same defect on the integration path the Co-Pilot would use. A "who touched this patient" query over `api_log` returns nothing. Patient identity is recoverable only by parsing UUIDs out of `request_url`. |
| §4.3 | Direct DB access **bypasses the audit log entirely**, a direct `UPDATE` left no entry **[TESTED]** | Any integration or administrator working at the SQL layer is invisible to audit. This is the single strongest technical argument against a direct-DB Co-Pilot integration. |
| Finding 5 | **No MFA anywhere**, including admin; nothing enrolled, nothing configured **[DB]** | Not §164.312(b) itself but §164.312(d) (person/entity authentication) and §164.308(a)(5)(ii)(D). It also weakens every audit record: attribution is only as trustworthy as the single factor behind it. Combined with Finding 7 (password guessing doesn't trip lockout), attribution to a named user is weaker than it looks. |
| Finding 10 | Break-glass can **remove itself** from the breakglass group and can disable `enable_auditlog` outright **[TESTED, read-only]** | The accountability control over the most privileged role is administrable *by that role*. With C-1, a break-glass actor could also erase the traces. |

**Combined effect:** OpenEMR logs a great deal, but on this deployment the log is **not trustworthy evidence**.
It can be edited undetectably by a DB-level actor (C-1), it misattributes patients (Finding 3, §6.4), and it can
be bypassed (§4.3) or switched off (Finding 10) by the privileged roles.

---

## 2. Data retention, NEWLY TESTED

**FINDING C-2 (MEDIUM): OpenEMR enforces no data retention or purge policy. There is nothing to configure.**

Searched `globals` for every retention-adjacent term, `%purge%`, `%retention%`, `%archive%`, `%expire%`,
`%delete%`, `%days%`, `%old%`, `%prune%` **[DB]**. The complete set of matches:
- `allow_pat_delete`, permits *manual* deletion of a patient by a user; not time-based, not automatic
- `password_expiration_days` (180), password ageing, unrelated to PHI
- `daysheet_provider_totals`, `use_custom_daysheet`, `weekend_days`, irrelevant matches

No "delete/de-identify PHI after N years" mechanism exists. No purge scripts under `contrib/util/`
(which contains backup and data-loading utilities only) **[READ]**. The audit `log` table itself has no
rotation either, it grows without bound, which given the volume in §1.1 is also an operational concern.

**This is a valid finding in the negative:** retention is **entirely a procedural/organizational control** here.
State-law medical-record retention periods (commonly 6–10 years, longer for minors) would have to be met by
policy plus manual or externally-scripted deletion, with **no application support and no enforcement**.
HIPAA §164.316(b)(2)(i) requires retention of *documentation* for six years; record retention itself is
governed by state law. Anyone claiming a retention schedule for this system is describing a promise, not a
technical control.

---

## 3. Breach notification readiness

HIPAA's Breach Notification Rule (§164.400–414) requires notice **without unreasonable delay and no later than
60 days** from **discovery**, and §164.404(a)(2) deems a breach discovered when it *would have been known by
exercising reasonable diligence*. That makes detection capability, not just response, a compliance question.

### 3.1 Honest answer: a breach here would very likely go unnoticed
Specific to this deployment, not generic:

- **Nothing alerts.** There is no alerting, SIEM integration, anomaly detection or log-forwarding configured
 **[DB/READ]**. `atna_audit_host` is empty and `enable_atna_audit` is unset **[DB]**, OpenEMR *supports* ATNA
 syslog audit forwarding to an external collector, and it is **switched off**. So all audit data stays on the
 same host as the application and database.
- **Detection is manual and pull-based.** The only review surface is the Logs Viewer, which a human must open
 and read. Given a single dashboard view emits hundreds of rows (§1.1), meaningful manual review is impractical
 at any real volume.
- **The exposure is real, not theoretical.** Per Finding 4 **[TESTED]**: every service binds to all network
 interfaces, the macOS firewall is **off**, MySQL is reachable on 8320, phpMyAdmin on 8310, and default
 credentials (`admin`/`pass`, MySQL `root`/`root`) are in use. Mailpit (8025) and Selenium (4444) require no
 authentication at all.
- **The most likely intrusion path is also the least logged.** An attacker reaching MySQL on 8320 with
 `root`/`root` reads all PHI **without generating a single audit row** (§4.3), and per C-1 could also rewrite
 any log rows that did exist. The scenario with the highest likelihood has the lowest detectability.
- **No integrity baseline.** No file-integrity monitoring, no config-change alerting. Finding 10's chain
 (break-glass leaving its own group, disabling audit logging) would be visible only to someone already
 reading the log for that specific event.

**Conclusion, plainly:** today this deployment could not reliably *discover* a breach, which means the 60-day
clock would likely never start. The gap is not the notification process, it is that **nothing would tell you
there was anything to notify about**. For a production deployment the minimum viable detection set would be:
enable ATNA/syslog forwarding to an off-host collector (OpenEMR already supports it), alert on
break-glass use and on `enable_auditlog`/ACL changes, bind services to localhost, enable the firewall, and
rotate the default credentials.

---

## 4. BAA and sending PHI to an LLM provider

This is the section the PRD calls out, and it is the central compliance question for the Clinical Co-Pilot.

### 4.1 What a BAA is and why it is required first
Under §164.502(e) and §164.308(b), a covered entity may disclose PHI to a **business associate**, a vendor
that creates, receives, maintains or transmits PHI on its behalf, only under a written **Business Associate
Agreement**. An LLM provider processing patient data via API is squarely a business associate.

A BAA obliges the vendor to: use/disclose PHI only as permitted; apply Security Rule safeguards; report
security incidents and breaches to the covered entity; flow equivalent terms to subcontractors; make records
available to HHS; and return or destroy PHI at termination. **Disclosing PHI to a provider with no BAA in
place is itself an impermissible disclosure**, a violation independent of whether the vendor mishandles
anything. Note that most consumer/default API tiers are *not* BAA-covered; BAA coverage is typically a
specific enterprise arrangement, and **the covered entity must verify it, not assume it**.

### 4.2 How §6.5 and §6.6 convert into concrete compliance exposure
The FHIR verification produced two findings that bear directly on this.

**§6.6 / Finding 12, system-scoped FHIR tokens bypass sensitivity filtering [TESTED].**
The `sensitivity='high'` encounter that the UI refuses to show `clin_1` and `back_1` (403) was returned **in
full, reason text included**, to a `system/Encounter.read` token. Sensitivity is enforced in the presentation
layer only; there is no check in the FHIR read path **[READ]**.

Exposure: a Co-Pilot using the natural backend-service pattern (client_credentials + `system/` scopes) would
ingest **exactly the visits the organization has flagged as most sensitive**, the category most likely to
carry heightened protection (behavioral health, substance use, reproductive health; note 42 CFR Part 2 imposes
*stricter* consent rules than HIPAA for substance-use-disorder records, and a sensitivity flag is precisely how
a practice would mark those). Sending that to an external LLM would be a disclosure of the most tightly held
records in the system, made by the component least aware they were restricted, and, per §1.3, **logged
against the wrong patient or none at all**, so the accounting of that disclosure would be wrong too.

**§6.5 / Finding 11, free-text clinical data loses fidelity [TESTED].**
An uncoded allergy ("Penicillin (hives)") returns `code` = `data-absent-reason: unknown`, with the allergen
surviving only in the narrative `text.div`.

This is primarily a **patient-safety** risk (a co-pilot summarizing allergies omits a penicillin allergy), but
it has a compliance edge: the mitigation is to fall back to reading `text.div`, i.e. **free-text narrative**.
Narrative fields are where identifiers and incidental third-party information accumulate, so the fix for the
safety problem *increases* the volume of loosely structured PHI leaving the system, which raises the bar on
minimum-necessary (§164.502(b)) and on de-identification if that route is ever considered.

### 4.3 What must be true before any real PHI reaches an LLM call
The data used in this entire audit was **fabricated** (six fake patients, 900-range SSNs that are never issued).
Everything below applies the moment real patient data is involved.

**Preconditions, all must hold:**
1. **BAA executed** with the LLM provider, covering the specific API/endpoint and model tier in use, verified
  in writing. Not assumed from marketing pages.
2. **No-training / no-retention terms** confirmed: data not used for model training, zero or bounded retention,
  documented deletion. Confirm what the provider logs on its side and for how long.
3. **Sensitivity gap closed first (§6.6).** This is the blocking one. Pick and implement one of the three
  compensating controls from Part II §6.6:
  (a) `user/`-scoped tokens bound to the requesting clinician, **only after testing that they actually enforce
  sensitivity, which is [UNVERIFIED]**; (b) filter on `sensitivity` in the Co-Pilot's own ingestion layer,
  note FHIR does not expose the flag, so this needs a second read path to obtain it; (c) exclude
  sensitivity-flagged encounters from Co-Pilot context entirely by policy. **(c) is the safest default and the
  only one requiring no further verification.**
4. **Field-level data inventory**, written down and reviewed: exactly which fields leave OpenEMR. Everything
  observed live in §6.2 flows today, including **full SSN** (`900-00-0001` came back in `Patient.identifier`
  as `us-ssn` **[TESTED]**) and full name and DOB. Under minimum-necessary, **SSN should be stripped before any
  LLM call**, it serves no clinical-reasoning purpose. Same question for full name, address and MRN.
5. **Audit the disclosure.** Given §1.3, `api_log` will not record which patient was sent. The Co-Pilot must
  maintain its **own** disclosure log (patient, fields, timestamp, requesting user, purpose) to support
  §164.528 accounting of disclosures. Do not rely on OpenEMR's log for this.
6. **Human-in-the-loop and DSI transparency.** Output influencing clinical decisions should be clinician-reviewed
  and marked as AI-generated; OpenEMR's predictive-DSI registration (`oauth_clients.dsi_type='predictive'`,
  `dsi_source_attributes`, Part II §5.2) is the platform's intended mechanism for declaring
  the attributes behind such recommendations, use it rather than bypassing it.

**Until 1–3 hold, the honest position is: no real PHI may be sent.** Development should continue on synthetic
data, which is exactly what this audit used, and is a reusable dataset for that purpose.

---

## 5. Summary, can this system safely send PHI to an LLM?

**Not today, without changes, but the blockers are specific and fixable, not architectural dead ends.**

The two FHIR-verification corrections are the crux:

- **§6.6 (sensitivity bypass) is the blocking compliance defect.** The integration path recommended for the
 Co-Pilot is the one path that does *not* honour the system's own sensitivity restrictions. A backend-service
 token sees everything, including the records clinicians are explicitly denied. Any PHI pipeline built on
 `system/` scopes without a compensating control would send the most protected records to an external
 processor. **This must be resolved before real data flows, it is not a footnote.**
- **§6.5 (fidelity loss) is chiefly a safety problem** that pushes you toward shipping free-text narrative,
 which in turn enlarges the PHI surface leaving the system and makes minimum-necessary harder to satisfy.

Around those sit the audit-control gaps: the log misattributes patients (Finding 3), the API log records none
(§6.4), direct DB access bypasses logging entirely (§4.3), and, newly proven here, **the tamper-evidence
checksum can be recomputed by anyone with DB write access (C-1)**. Together these mean that if PHI did reach an
LLM improperly, the system would struggle to reconstruct what was disclosed, for whom, or by whom, and could not
prove its own log had not been altered. That is the same evidentiary capability a breach investigation depends
on (§3).

**The constructive read:** the recommendation from Part II §4.4, FHIR API over direct DB,
holds and is *reinforced* on compliance grounds, because the API is the only path that is both centrally
authorized and audited at all. The work needed before real PHI moves is a short, concrete list: execute a BAA,
close the sensitivity gap (option (c) needs no further verification), strip SSN and other non-clinical
identifiers, and keep an independent disclosure log. None of that requires changing OpenEMR itself.

### Cost / Operational Controls

The deployed agent is publicly reachable (see clinical-copilot/THREAT_MODEL.md §4.5 —
Public, unauthenticated /chat and /ui), which
creates a cost-exposure risk independent of data exposure: any reachable endpoint that
triggers LLM calls can be driven to unexpected spend by legitimate load, misuse, or an
unbounded tool-calling loop.

Mitigation: a monthly spend limit is configured at the Anthropic Console (account) level,
with email alerts at 75% of the limit, providing a hard stop independent of
application-layer controls. This is deliberately account-level, not application code --
it's a last-resort backstop, not a substitute for the access gating and rate limiting
described in ARCHITECTURE.md.

Known limitation: a Console-level spend cap is coarse -- it protects the account, not the
user experience. Hitting it pauses all API access for the remainder of the billing period,
including for legitimate use. It is a backstop, not a graceful-degradation strategy.
