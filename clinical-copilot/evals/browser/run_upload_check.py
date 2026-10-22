"""Drives the document-upload UI in headless Chromium against harness.py, prints what it observed, saves
screenshots, and exits non-zero if any CHECK fails. Not a pytest module and not run in CI; needs Playwright
(see README.md).

Covers: the attach control and panel, client-side file validation (no request is sent for a bad file), the
progress states, the extraction result card (thumbnail, fields, low-confidence note, click-to-source), the card for
a document with no extracted fields, the 5-page cap and duplicate errors, escape-first rendering of hostile
document text and a hostile file name, that the uploaded document rides along with exactly the next question,
cancel, that a new conversation drops a waiting document, and the phone-width layout.

Usage: python run_upload_check.py [screenshot_dir] [base_url]
"""
import json, os, re, sys, tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

SP = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="upload-ui-")
Path(SP).mkdir(parents=True, exist_ok=True)
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:" + os.environ.get("HARNESS_PORT", "8765")
print("screenshots ->", SP)

# A valid 1x1 PNG. The harness replaces OCR/structuring, so only the file's name and type matter.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000" "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

failures = []


def check(name, ok, detail=""):
    print(("CHECK ok   " if ok else "CHECK FAIL"), name, ("| " + str(detail)) if detail != "" else "")
    if not ok:
        failures.append(name)


# The two error uploads below are supposed to get a 4xx; Chromium logs each as a console error.
EXPECTED_4XX = re.compile(r"Failed to load resource: the server responded with a status of (400|409)")
errors = []
posts = []          # POST /documents requests actually sent
chat_bodies = []    # bodies of POST /chat

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 900})
    pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") and not EXPECTED_4XX.search(m.text) else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    dialogs = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    pg.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/documents") else None)
    pg.on("request", lambda r: chat_bodies.append(r.post_data_json) if r.method == "POST" and r.url.endswith("/chat") else None)
    pg.request.get(BASE + "/__reset")
    pg.goto(BASE + "/__login")
    pg.wait_for_selector("#chat-input", timeout=15000)

    def pick(name, mime="image/png", data=PNG, doc_type=None):
        pg.set_input_files("#file-input", {"name": name, "mimeType": mime, "buffer": data})
        if doc_type:
            pg.select_option("#doctype-select", doc_type)

    def open_panel():
        if pg.locator("#upload-panel").is_hidden():
            pg.click("#attach-btn")
        pg.wait_for_selector("#upload-panel", state="visible")

    def bubbles():
        return pg.locator(".row.agent").count()

    def last_card():
        return pg.locator(".row.agent .cite-card").last

    def turn_refs():
        return json.loads(pg.request.get(BASE + "/__turn_refs").text())

    # ---- the control exists and the panel is closed until asked for
    check("Attach document control is present", pg.locator("#attach-btn").count() == 1 and pg.locator("#attach-btn").is_enabled())
    check("upload panel starts closed", pg.locator("#upload-panel").is_hidden() and pg.get_attribute("#attach-btn", "aria-expanded") == "false")
    pg.screenshot(path=f"{SP}/u0_composer.png", full_page=True)

    open_panel()
    check("panel opens and the button says so (aria-expanded)", pg.get_attribute("#attach-btn", "aria-expanded") == "true")
    check("file input accepts PDF and PNG only", set(re.split(r",\s*", pg.get_attribute("#file-input", "accept"))) == {".pdf", ".png", "application/pdf", "image/png"},
          pg.get_attribute("#file-input", "accept"))
    opts = pg.locator("#doctype-select option").evaluate_all("els => els.map(e => [e.value, e.textContent])")
    check("document type offers lab report and intake form", opts == [["lab_pdf", "Lab report"], ["intake_form", "Intake form"]], opts)
    check("focus moved into the panel", pg.evaluate("document.activeElement && document.activeElement.id") == "file-input")
    pg.screenshot(path=f"{SP}/u1_panel_open.png", full_page=True)

    # ---- client-side validation: a bad file never reaches the server
    pg.click("#upload-btn")
    check("no file chosen -> asks for one", "Choose a PDF or PNG" in pg.locator("#upload-error").inner_text())
    pick("notes.txt", "text/plain", b"hello")
    pg.click("#upload-btn")
    check("wrong file type is refused in the page", "Only PDF or PNG" in pg.locator("#upload-error").inner_text(), pg.locator("#upload-error").inner_text())
    pick("big.png", "image/png", b"\0" * (20 * 1024 * 1024 + 1))
    pg.click("#upload-btn")
    check("a file over 20 MB is refused in the page", "larger than 20 MB" in pg.locator("#upload-error").inner_text())
    pick("empty.png", "image/png", b"")
    pg.click("#upload-btn")
    check("an empty file is refused in the page", "empty" in pg.locator("#upload-error").inner_text().lower())
    check("none of those sent a request", posts == [], posts)
    check("the error is an alert region", pg.get_attribute("#upload-error", "role") == "alert")
    pg.screenshot(path=f"{SP}/u2_validation_error.png", full_page=True)
    pg.keyboard.press("Escape")
    check("Esc closes the panel", pg.locator("#upload-panel").is_hidden())
    check("...and returns focus to the Attach button", pg.evaluate("document.activeElement && document.activeElement.id") == "attach-btn")

    # ---- a real upload: progress, then the result card
    open_panel()
    pick("slow_lab.png", doc_type="lab_pdf")
    n_before = bubbles()
    pg.click("#upload-btn")
    pg.wait_for_function("document.getElementById('upload-status-text').textContent.includes('Reading the document')", timeout=10000)
    check("while the server reads it, the bar is indeterminate", pg.evaluate("!document.getElementById('upload-progress').hasAttribute('value')"))
    check("progress is announced (status region)", pg.get_attribute("#upload-status", "role") == "status")
    check("Send and the question box are disabled during the upload", pg.locator("#chat-input").is_disabled() and pg.locator("#send-btn").is_disabled())
    check("Upload is disabled so it can't be sent twice", pg.locator("#upload-btn").is_disabled())
    pg.screenshot(path=f"{SP}/u3_progress.png", full_page=True)

    pg.wait_for_function(f"document.querySelectorAll('.row.agent').length > {n_before}", timeout=20000)
    pg.wait_for_selector(".row.agent .cite-card .cite-thumb img", timeout=10000)
    pg.wait_for_timeout(400)
    card = last_card()
    check("exactly one upload request was sent", len(posts) == 1, posts)
    check("result card title has type and upload date", card.locator(".cite-title").inner_text() == "Lab report · uploaded 2026-09-25", card.locator(".cite-title").inner_text())
    check("card is badged pending review", card.locator(".cite-badge").first.inner_text().lower() == "pending review")  # CSS uppercases it
    check("card names the file it came from", "From slow_lab.png" in card.inner_text())
    check("card says how many fields were extracted", "2 fields extracted" in card.inner_text(), card.locator(".cite-meta").inner_text())
    fields = card.locator(".cite-field").all_inner_texts()
    print("fields:", fields)
    check("each lab result is one readable row", len(fields) == 2 and "Potassium" in fields[0] and "7.8 mmol/L" in fields[0] and "ref 3.5-5.1" in fields[0], fields)
    check("a low-confidence field says so", "low confidence" in fields[1] and "low confidence" not in fields[0], fields)
    check("the page thumbnail loaded (no 'No preview')", card.locator(".cite-thumb img").count() == 1 and "No preview" not in card.locator(".cite-thumb").inner_text())
    check("upload panel closed itself after success", pg.locator("#upload-panel").is_hidden())
    chip = pg.locator("#doc-chip-text").inner_text()
    check("the document is set as the conversation's uploaded document (chip)", pg.locator("#doc-chip").is_visible() and "lab report" in chip and "slow_lab.png" in chip, chip)
    check("the question box now points at the document", "uploaded document" in pg.get_attribute("#chat-input", "placeholder"))
    check("the question box is usable again and focused", pg.locator("#chat-input").is_enabled() and pg.evaluate("document.activeElement.id") == "chat-input")
    pg.screenshot(path=f"{SP}/u4_result_card.png", full_page=True)

    card.locator(".cite-field").first.click()
    pg.wait_for_selector(".citation-highlight", timeout=10000)
    sub = pg.locator(".citation-modal-subtitle").inner_text()
    check("clicking a field opens the page with the value highlighted", pg.locator(".citation-highlight").count() == 1 and "Potassium: 7.8" in sub, sub)
    pg.screenshot(path=f"{SP}/u5_field_click_to_source.png", full_page=True)
    pg.keyboard.press("Escape")

    # ---- the next question carries the document, and only that one
    n_before = bubbles()
    pg.fill("#chat-input", "What does the potassium on the uploaded lab mean?")
    pg.press("#chat-input", "Enter")
    pg.wait_for_function(f"document.querySelectorAll('.row.agent').length > {n_before}", timeout=20000)
    pg.wait_for_timeout(500)
    ref = {"doc_id": "e1e1e1e1e1e1.png", "doc_type": "lab_pdf"}
    check("the browser sent uploaded_document_ref with that question", chat_bodies[-1].get("uploaded_document_ref") == ref, chat_bodies[-1])
    check("the server received exactly that reference", turn_refs() == [ref], turn_refs())
    check("the chip is gone once the question was answered", pg.locator("#doc-chip").is_hidden())
    check("the placeholder is back to normal", pg.get_attribute("#chat-input", "placeholder") == "Ask about this patient…")
    n_before = bubbles()
    pg.fill("#chat-input", "And anything else notable?")
    pg.press("#chat-input", "Enter")
    pg.wait_for_function(f"document.querySelectorAll('.row.agent').length > {n_before}", timeout=20000)
    check("the following question does NOT resend it", chat_bodies[-1].get("uploaded_document_ref") is None and turn_refs()[-1] is None, turn_refs())
    pg.screenshot(path=f"{SP}/u6_after_question.png", full_page=True)

    # ---- a document with no extracted fields still gets its card
    open_panel()
    pick("empty_fields.png")
    n_before = bubbles()
    pg.click("#upload-btn")
    pg.wait_for_function(f"document.querySelectorAll('.row.agent').length > {n_before}", timeout=20000)
    pg.wait_for_selector(".row.agent .cite-card .cite-thumb img", timeout=10000)
    card = last_card()
    check("a document with no fields still shows its card", card.count() == 1)
    check("...with its thumbnail", card.locator(".cite-thumb img").count() == 1)
    check("...and says 'No fields extracted'", "No fields extracted" in card.locator(".cite-meta").inner_text(), card.locator(".cite-meta").inner_text())
    check("...with a hint and no empty field list", card.locator(".upload-result-empty").count() == 1 and card.locator(".cite-fields").count() == 0)
    check("...and is still set as the uploaded document", pg.locator("#doc-chip").is_visible() and "empty_fields.png" in pg.locator("#doc-chip-text").inner_text())
    pg.screenshot(path=f"{SP}/u7_no_fields_card.png", full_page=True)
    pg.click("#doc-chip-remove")
    check("the attached document can be dropped before asking", pg.locator("#doc-chip").is_hidden())

    # ---- errors: the 5-page cap and a duplicate
    open_panel()
    pick("toomany.pdf", "application/pdf", PDF, doc_type="lab_pdf")
    n_before = bubbles()
    pg.click("#upload-btn")
    pg.wait_for_selector("#upload-error:not([hidden])", timeout=15000)
    msg = pg.locator("#upload-error").inner_text()
    check("5-page cap shows a plain-language message", "more than 5 pages" in msg and "split it" in msg, msg)
    check("...naming no HTTP code or internals", not re.search(r"HTTP|400|too_many_pages|detail", msg), msg)
    check("...the panel stays open for another try", pg.locator("#upload-panel").is_visible() and pg.locator("#upload-btn").is_enabled())
    check("...and nothing was added to the conversation", bubbles() == n_before and pg.locator("#doc-chip").is_hidden())
    pg.screenshot(path=f"{SP}/u8_page_cap_error.png", full_page=True)
    pick("dup.png")
    pg.click("#upload-btn")
    pg.wait_for_function("document.getElementById('upload-error').textContent.includes('already on this patient')", timeout=15000)
    check("a duplicate is explained, not shown as a failure code", "not added again" in pg.locator("#upload-error").inner_text())
    pick("lab_many.png")
    pg.click("#upload-btn")
    pg.wait_for_function(f"document.querySelectorAll('.row.agent').length > {n_before}", timeout=20000)
    check("a failed upload clears its error when the next one succeeds", pg.locator("#upload-panel").is_hidden() and pg.locator("#upload-error").is_hidden())

    # ---- many fields: the first few, the rest behind a toggle
    card = last_card()
    check("only the first fields show at first", card.locator(".cite-fields li:not([hidden])").count() == 4, card.locator(".cite-fields li:not([hidden])").count())
    more = card.locator(".cite-more")
    check("the rest are offered as 'Show 3 more fields'", more.count() == 1 and more.inner_text() == "Show 3 more fields", more.all_inner_texts())
    more.click()
    check("expanding shows all 7", card.locator(".cite-fields li:not([hidden])").count() == 7)
    pg.screenshot(path=f"{SP}/u9_many_fields_expanded.png", full_page=True)
    pg.click("#doc-chip-remove")

    # ---- escape-first: hostile text in the document AND in the file name
    hostile_name = '"><img src=x onerror=alert(1)>.png'
    open_panel()
    pick(hostile_name, doc_type="intake_form")
    n_before = bubbles()
    pg.click("#upload-btn")
    pg.wait_for_function(f"document.querySelectorAll('.row.agent').length > {n_before}", timeout=20000)
    pg.wait_for_timeout(500)
    card = last_card()
    text = pg.locator("#chat-log").inner_text()
    check("hostile document text is shown literally", "<img src=x onerror=alert(1)>" in text and "<script>alert(2)</script>" in text)
    check("hostile file name is shown literally", hostile_name in text)
    check("the intro line reads correctly for an intake form", "as an intake form" in text and "as a lab report" in text)
    check("no element was created from any of it", pg.locator("#chat-log img[src='x'], #chat-log script, #upload-panel img, #doc-chip img").count() == 0)
    check("no dialog fired", dialogs == [], dialogs)
    check("the intake card lists demographics, medications and allergies", {"Name", "Chief concern", "Medication", "Allergy"} <= set(card.locator(".cite-field-label").all_inner_texts()),
          card.locator(".cite-field-label").all_inner_texts())
    check("an unreadable field is marked", any("unreadable" in t for t in card.locator(".cite-field").all_inner_texts()))
    pg.screenshot(path=f"{SP}/u10_hostile_intake_inert.png", full_page=True)

    # ---- cancel during the reading phase
    pg.click("#doc-chip-remove")
    open_panel()
    pick("cancel_me.png", doc_type="lab_pdf")
    n_before = bubbles()
    posts_before = len(posts)
    pg.click("#upload-btn")
    pg.wait_for_function("document.getElementById('upload-status-text').textContent.includes('Reading the document')", timeout=10000)
    pg.click("#upload-cancel")
    check("Cancel closes the panel and frees the controls", pg.locator("#upload-panel").is_hidden() and pg.locator("#chat-input").is_enabled())
    pg.wait_for_timeout(2200)   # the server finishes its 1.5s of 'work' after we left; the page must ignore it
    check("...an in-flight result that arrives later is ignored", last_card().locator(".cite-title").count() == 1 and pg.locator("#doc-chip").is_hidden())
    check("...and the resident is told the file may still appear", "may still appear on the chart" in pg.locator(".bubble.agent").last.inner_text(), pg.locator(".bubble.agent").last.inner_text())
    check("...exactly one request went out", len(posts) == posts_before + 1)
    pg.screenshot(path=f"{SP}/u11_cancelled.png", full_page=True)

    # ---- new conversation drops a waiting document and any open panel
    open_panel()
    pick("lab_many.png")
    pg.click("#upload-btn")
    pg.wait_for_selector("#doc-chip:not([hidden])", timeout=20000)
    open_panel()
    pg.click("#new-convo")
    pg.wait_for_timeout(800)
    check("New conversation clears the waiting document", pg.locator("#doc-chip").is_hidden() and pg.get_attribute("#chat-input", "placeholder") == "Ask about this patient…")
    check("...and closes the panel", pg.locator("#upload-panel").is_hidden())
    pg.fill("#chat-input", "Fresh start")
    pg.press("#chat-input", "Enter")
    pg.wait_for_function("document.querySelectorAll('.bubble.agent').length >= 2", timeout=20000)
    check("...so the next question carries no document", chat_bodies[-1].get("uploaded_document_ref") is None, chat_bodies[-1])

    # ---- phone width
    pg.set_viewport_size({"width": 390, "height": 800})
    open_panel()
    pg.wait_for_timeout(300)
    overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check("no horizontal overflow at 390px with the panel open", overflow <= 0, overflow)
    check("panel controls stay inside the viewport", pg.evaluate("""() => ['file-input','doctype-select','upload-btn','upload-cancel'].every(id => {
        const r = document.getElementById(id).getBoundingClientRect(); return r.left >= 0 && r.right <= window.innerWidth; })"""))
    pg.screenshot(path=f"{SP}/u12_phone_panel.png", full_page=True)

    # ---- the endpoint still refuses without a session
    r = b.new_context().request.post(f"{BASE}/documents", multipart={"patient_id": "11111111-1111-4111-8111-111111111111", "doc_type": "lab_pdf",
                                     "file": {"name": "x.png", "mimeType": "image/png", "buffer": PNG}})
    check("POST /documents without a session is 401", r.status == 401, r.status)
    check("no dialogs fired (whole run)", dialogs == [], dialogs)
    check("no console errors/warnings (whole run)", errors == [], errors)
    b.close()

print(f"\n{len(failures)} check(s) failed" + (": " + ", ".join(failures) if failures else ""))
sys.exit(1 if failures else 0)
