"""Drives the citation UI in headless Chromium against harness.py, prints what
it observed, and exits non-zero if any CHECK fails. Not a pytest module and not
run in CI; needs Playwright (see README.md).

Usage: python run_browser_check.py [screenshot_dir] [base_url]
"""
import os, re, sys, tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

SP = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="citation-cards-")
Path(SP).mkdir(parents=True, exist_ok=True)
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:" + os.environ.get("HARNESS_PORT", "8765")
CORPUS = Path(__file__).resolve().parents[2] / "app" / "retrieval" / "corpus"
print("screenshots ->", SP)

failures = []


def check(name, ok, detail=""):
    print(("CHECK ok   " if ok else "CHECK FAIL"), name, ("| " + str(detail)) if detail != "" else "")
    if not ok:
        failures.append(name)


def whole_word_in_corpus(word, stem):
    text = (CORPUS / f"{stem}.md").read_text(encoding="utf-8")
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", text) is not None


errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 900})
    pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    dialogs = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    previews = []
    pg.on("request", lambda r: previews.append(r.url) if "/documents/preview" in r.url else None)
    pg.request.get(BASE + "/__reset")
    pg.goto(BASE + "/__login")
    pg.wait_for_selector("#chat-input", timeout=15000)

    def send(text):
        box = pg.locator("#chat-input")
        box.fill(text)
        box.press("Enter")

    # ---- turn 1: upload turn (chart records + same-turn document + 3+1 guideline passages)
    send("I just uploaded a lab report. What does it show, and what should I do about it?")
    pg.wait_for_selector(".cite-card", timeout=20000)
    pg.wait_for_timeout(800)
    pg.screenshot(path=f"{SP}/shot1_cards.png", full_page=True)

    chips = pg.locator(".chip").all_inner_texts()
    print("turn1 chips:", chips)
    check("chart chips are readable labels", {"Condition: COPD", "Allergy: Sulfa drugs"} <= set(chips), chips)
    check("no 'record' type-count chips", not any(re.search(r"\brecord\b|×\d", c) for c in chips), chips)
    check("no raw document-id chips", not any(("document:" in c or "#" in c or ".png" in c or "results[" in c) for c in chips), chips)

    print("turn1 doc cards:", pg.locator(".cite-card:not(.guideline)").count(), "guideline cards:", pg.locator(".cite-card.guideline").count())
    check("one document card (4 identical uploads collapsed)", pg.locator(".cite-card:not(.guideline)").count() == 1)
    dup_badge = pg.locator(".cite-card:not(.guideline) .cite-badge.dup")
    check("card says how many copies were uploaded", dup_badge.count() == 1 and dup_badge.inner_text().lower() == "uploaded 4×",
          dup_badge.all_inner_texts())
    # 4 copies x 12 fields = 48 raw; all 12 distinct fields remain (unit/date values repeat across the 2 results,
    # but they are different fields, so both stay).
    check("collapsed card lists each field once (12, not 48; shared values not hidden)", pg.locator(".cite-card:not(.guideline) .cite-fields li").count() == 12,
          pg.locator(".cite-card:not(.guideline) .cite-fields li").count())
    print("turn1 card title:", pg.locator(".cite-card:not(.guideline) .cite-title").first.inner_text())
    print("turn1 field labels:", pg.locator(".cite-card:not(.guideline) .cite-field-label").all_inner_texts())
    thumb = pg.locator(".cite-thumb img").first
    check("thumbnail loaded, blurred", thumb.evaluate("e=>e.complete && e.naturalWidth>0") and "blur" in thumb.evaluate("e=>getComputedStyle(e).filter"))
    check("thumbnail requests carry no quote param", [u for u in previews if "quote" in u] == [])
    if pg.locator(".cite-more").count():
        pg.locator(".cite-more").first.click()
        check("expand shows all fields", pg.locator(".cite-card:not(.guideline) .cite-fields li:not([hidden])").count() == 12)
        pg.locator(".cite-more").first.click()
        check("collapse back to 4", pg.locator(".cite-card:not(.guideline) .cite-fields li:not([hidden])").count() == 4)

    # ---- guideline cards: one per document
    gcards = pg.locator(".cite-card.guideline")
    titles = gcards.locator(".cite-title").all_inner_texts()
    print("guideline card titles:", titles)
    check("one guideline card per document (4 chunks, 2 documents -> 2 cards)", gcards.count() == 2, gcards.count())
    check("guideline title is the document heading",
          titles[:1] == ["Hyperkalemia: general management approach"] and "Sepsis screening: general approach" in titles, titles)
    excerpts = gcards.locator(".cite-excerpt").all_inner_texts()
    print("guideline excerpts:", excerpts)
    check("excerpts have no markdown syntax", not any(re.search(r"(^|\s)#{1,6}\s|\*\*|__|`|^\s*[-*]\s|\]\(", e) for e in excerpts), excerpts)
    for e, stem in zip(excerpts, ("hyperkalemia_management", "sepsis_screening")):
        first = re.split(r"\s+", e.strip().lstrip("…"))[0].strip(",.;:()")
        check(f"excerpt for {stem} starts on a whole word", whole_word_in_corpus(first, stem), first)
        check(f"excerpt for {stem} is marked with a leading … (it starts mid-guideline)", e.startswith("…"), e[:30])
        check(f"excerpt for {stem} shows a trailing … when cut", e.rstrip().endswith("…"), e[-20:])
    sub = gcards.first.locator(".cite-sub").inner_text()
    check("card counts its passages", "3 passages cited" in sub, sub)
    gcards.first.scroll_into_view_if_needed()
    pg.screenshot(path=f"{SP}/shot1b_guideline_cards.png")

    # ---- document modals (unchanged behaviour)
    pg.locator(".cite-card-main").first.click()
    pg.wait_for_selector(".citation-preview-wrap img", timeout=10000)
    pg.wait_for_timeout(500)
    check("document card opens the sharp page", pg.locator(".citation-highlight").count() == 0)
    pg.screenshot(path=f"{SP}/shot2_page_modal.png")
    pg.keyboard.press("Escape")
    pg.locator(".cite-field", has_text="Value (result 1)").first.click()
    pg.wait_for_selector(".citation-highlight", timeout=10000)
    pg.wait_for_timeout(300)
    check("field click highlights the value", pg.locator(".citation-highlight").count() == 1)
    pg.screenshot(path=f"{SP}/shot3_field_modal.png")
    pg.locator(".citation-modal-close").click()

    # ---- guideline modal: every cited passage, rendered safely
    gcards.first.click()
    try:
        pg.wait_for_selector(".cite-passage", timeout=5000)
        modal_lists_passages = True
    except Exception:
        modal_lists_passages = False
        check("guideline modal lists the cited passages", False, "no .cite-passage elements")
    print("guideline modal title:", pg.locator(".citation-modal-title").inner_text(), "|", pg.locator(".citation-modal-subtitle").inner_text())
    if modal_lists_passages:
        passages = pg.locator(".cite-passage")
        print("modal passages:", passages.count(), "headings:", pg.locator(".cite-passage-heading").all_inner_texts())
        check("modal lists all 3 cited passages of the document", passages.count() == 3, passages.count())
        check("modal title is the document heading", pg.locator(".citation-modal-title").inner_text() == "Hyperkalemia: general management approach")
        bodies = pg.locator(".cite-passage-body").all_inner_texts()
        check("passage text has no heading/bold markers", not any("##" in t or "**" in t for t in bodies))
        check("no passage starts on a cut-off word",
              all(whole_word_in_corpus(re.split(r"\s+", t.strip().lstrip("…"))[0].strip(",.;:()"), "hyperkalemia_management") for t in bodies),
              [t[:24] for t in bodies])
        check("passages start at a sentence and end at one, with … where cut",
              all(re.match(r"…?[A-Z0-9*]", t.strip()) for t in bodies) and all(re.search(r"[.!?…]$", t.strip()) for t in bodies),
              [(t.strip()[:14], t.strip()[-14:]) for t in bodies])
        joined = " ".join(bodies)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", joined) if len(s.strip()) > 40]
        check("no sentence is repeated across passages (chunk overlap removed)", len(sentences) == len(set(sentences)),
              [s[:30] for s in sentences if sentences.count(s) > 1])
        check("HTML in a passage is inert (no img/script elements inside the modal)", pg.locator(".citation-modal img, .citation-modal script").count() == 0)
        check("...and is shown as literal text", "<img src=x onerror=alert(1)>" in " ".join(bodies))
    else:
        check("HTML in the modal text is inert (no img/script elements)", pg.locator(".citation-modal img, .citation-modal script").count() == 0)
    pg.screenshot(path=f"{SP}/shot4_guideline_modal.png")
    pg.keyboard.press("Escape")
    check("modal closes on Esc", pg.locator(".citation-modal").count() == 0)

    # ---- turn 2: later turn (citations rebuilt from get_uploaded_documents; source_resources now hold document: refs)
    send("What was the potassium on that lab again?")
    pg.wait_for_function("document.querySelectorAll('.cite-card:not(.guideline)').length >= 2", timeout=20000)
    pg.wait_for_timeout(800)
    pg.screenshot(path=f"{SP}/shot5_turn2.png", full_page=True)
    chips2 = pg.locator(".chip").all_inner_texts()[len(chips):]  # only chips added by turn 2
    print("turn2 chips:", chips2)
    check("turn 2: chart chips readable, no document refs", {"Condition: COPD", "Allergy: Sulfa drugs"} <= set(chips2)
          and not any(("document:" in c or "#" in c or "record" in c) for c in chips2), chips2)
    check("turn 2: the 4 copies read back as one card with the 4× badge",
          pg.locator(".cite-card:not(.guideline)").count() == 2 and pg.locator(".cite-badge.dup").count() == 2,
          (pg.locator(".cite-card:not(.guideline)").count(), pg.locator(".cite-badge.dup").all_inner_texts()))
    last = pg.locator(".cite-card:not(.guideline)").last
    print("turn2 title:", last.locator(".cite-title").inner_text(), "| fields:", last.locator(".cite-field-label").all_inner_texts())
    last.locator(".cite-field", has_text="Value").first.click()
    pg.wait_for_selector(".citation-highlight", timeout=10000)
    check("turn 2 field modal highlights", pg.locator(".citation-highlight").count() == 1)
    pg.keyboard.press("Escape")
    # the developer-facing details block must not list document refs as chart records either
    pg.locator("details").last.evaluate("e=>e.open=true")
    details_text = pg.locator("details").last.inner_text()
    check("details 'Chart source records' has no document refs", "document:" not in details_text)

    # ---- turn 3: no matching guideline -> neutral notice, verified check kept, no warning box
    bubbles_before = pg.locator(".bubble.agent").count()
    send("What is the guideline approach to acute pancreatitis?")
    pg.wait_for_function(f"document.querySelectorAll('.bubble.agent').length > {bubbles_before}", timeout=20000)
    pg.wait_for_selector(".chip.notice", timeout=10000)
    pg.wait_for_timeout(400)
    notice_chip = pg.locator(".chip.notice").last
    notice_text = pg.locator(".notice-text").last.inner_text()
    print("notice chip:", notice_chip.inner_text(), "|", notice_text)
    last = pg.locator(".bubble.agent").last
    check("no-match notice is a neutral info chip with plain wording",
          notice_chip.inner_text() == "No matching guideline" and "didn't find a guideline in the reference set" in notice_text,
          (notice_chip.inner_text(), notice_text))
    check("no-match notice keeps the verified check", last.locator(".verified-line").count() == 1)
    check("no-match notice raises no warning box and no raw code", last.locator(".warn").count() == 0
          and "worker_partial_failure" not in last.inner_text() and "[Verification note" not in last.inner_text(), last.inner_text()[:80])
    pg.screenshot(path=f"{SP}/shot6_no_match_notice.png", full_page=True)

    # ---- turn 4: a real failure is still a warning (contrast)
    bubbles_before = pg.locator(".bubble.agent").count()
    send("And what about sepsis?")
    pg.wait_for_function(f"document.querySelectorAll('.bubble.agent').length > {bubbles_before}", timeout=20000)
    pg.wait_for_timeout(500)
    last = pg.locator(".bubble.agent").last
    check("a real failure still shows the warning box and no verified check",
          last.locator(".warn").count() == 1 and last.locator(".verified-line").count() == 0, last.locator(".warn").count())
    check("...listing worker_partial_failure", "worker_partial_failure" in last.locator(".warn").inner_text())
    pg.screenshot(path=f"{SP}/shot7_real_failure_warning.png", full_page=True)

    # ---- turn 5: provider outage -> friendly message + reference, warning chip, NO verified check, no verification box
    bubbles_before = pg.locator(".bubble.agent").count()
    send("Try again please")
    pg.wait_for_function(f"document.querySelectorAll('.bubble.agent').length > {bubbles_before}", timeout=20000)
    pg.wait_for_timeout(500)
    last = pg.locator(".bubble.agent").last
    text = last.inner_text()
    check("outage shows the friendly message with a reference id", "temporarily unavailable" in text
          and re.search(r"reference: [0-9a-f-]{36}", text) is not None, text[:120])
    check("outage never shows the verified check or a verification warning box", last.locator(".verified-line").count() == 0
          and last.locator(".warn").count() == 0)
    check("outage shows a warning-styled notice chip", pg.locator(".chip.notice.warning").last.inner_text() == "Assistant unavailable")
    pg.screenshot(path=f"{SP}/shot8_assistant_unavailable.png", full_page=True)

    r = b.new_context().request.get(f"{BASE}/documents/preview?patient_id=11111111-1111-4111-8111-111111111111&doc_id=a1b2c3d4e5f6.png")
    check("preview without a session is 401", r.status == 401, r.status)
    check("no dialogs fired", dialogs == [], dialogs)
    check("no console errors/warnings", errors == [], errors)
    b.close()

print(f"\n{len(failures)} check(s) failed" + (": " + ", ".join(failures) if failures else ""))
sys.exit(1 if failures else 0)
