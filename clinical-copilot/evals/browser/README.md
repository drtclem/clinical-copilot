# Citation display browser check

An offline, manual check of the citation cards UI (`app/static/chat.html`).
It is **not** collected by pytest, **not** registered in `evals/unit_tests.py`
and **not** part of `.gitlab-ci.yml`.

`harness.py` starts the real FastAPI app with a fake FHIR client and a
scripted graph (real citation builders, no OpenEMR, no Anthropic).
`run_browser_check.py` drives it in headless Chromium, prints what it sees, and
exits non-zero if a `CHECK` fails: card grouping and titles, the blurred
thumbnail, expand/collapse of fields, the page modal, the field modal with its
highlight box, a later turn, and a 401 from the preview endpoint without a
session. Since the citation-display fix it also asserts that:

- the "From this patient's chart" chips are readable labels ("Allergy: Sulfa
  drugs"), never "Condition record" and never a raw `document:<id>#field` ref,
  on the upload turn and on a later turn (where `source_resources` carries
  those refs);
- guideline evidence is one card per guideline document (real corpus chunks: 3
  passages of one document + 1 of another -> 2 cards), titled with the
  document's own heading, with a markdown-free excerpt that starts on a whole
  word, and a modal listing every cited passage in document order;
- an HTML payload inside a guideline passage shows as literal text and creates
  no `<img>`/`<script>` element.

- a guideline search with no relevant result shows as a neutral info notice ("No matching guideline" + plain
  wording), keeps the verified check and shows no warning box, while a real failure (`worker_partial_failure`) still
  shows the warning box and loses the check (screenshots `shot6_*` / `shot7_*`).

It also saves screenshots.

## Run

Playwright is deliberately not in `requirements.txt` (that file is pinned and
installed by CI). Use a separate venv:

```bash
cd clinical-copilot

# 1. the app's own pinned dependencies
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. a separate venv for the browser driver
python3 -m venv /tmp/pwvenv
/tmp/pwvenv/bin/pip install playwright
/tmp/pwvenv/bin/playwright install chromium

# 3. start the harness (port via HARNESS_PORT, default 8765)
.venv/bin/python evals/browser/harness.py &

# 4. run the check; screenshots go to the given directory
/tmp/pwvenv/bin/python evals/browser/run_browser_check.py /tmp/citation-shots
kill %1
```

Read the printed `CHECK ok` / `CHECK FAIL` lines; the exit status is non-zero
if any check failed. A console error or dialog also fails a check.

## Document upload check

`run_upload_check.py` drives the composer's "Attach document" control against the same harness (which also scripts
`POST /documents` outcomes by file name: success, many fields, zero fields, the 5-page cap, a duplicate, a slow read,
hostile intake text). It checks the panel and client-side validation (a bad file never reaches the server), the
progress states, the extraction result card (thumbnail, field rows, low-confidence and unreadable notes,
click-to-source), a document with no extracted fields, the 5-page and duplicate errors, escape-first rendering of
hostile document text and file names, that `uploaded_document_ref` rides with exactly the next question (checked in the
browser's request and in what the server received), cancel, that a new conversation drops a waiting document, and the
phone-width layout. Screenshots are `u0_*` to `u12_*`.

```bash
/tmp/pwvenv/bin/python evals/browser/run_upload_check.py /tmp/upload-shots
```

Like the citation check it is manual: not collected by pytest and not in `.gitlab-ci.yml`. It runs against the
harness's fake OCR/structuring, so it proves the page's behaviour, not extraction accuracy.

## Limits

- OCR and bounding boxes are stubbed with fixed values (see `BBOX` in
  `harness.py`), so this checks the UI's highlight placement, not OCR accuracy.
- The page image is a generated stand-in, not a real scan.
- Nothing here talks to a real OpenEMR or the Anthropic API.
