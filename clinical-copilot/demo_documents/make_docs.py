"""Generate synthetic test documents (lab reports + intake forms) for the Clinical Co-Pilot.
ALL DATA IS SYNTHETIC. Edit PATIENTS below so name/DOB/MRN match your OpenEMR fixtures, then rerun.
"""
import os, random, subprocess
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from PIL import Image, ImageFilter, ImageEnhance

OUT = "out"
os.makedirs(OUT, exist_ok=True)
W, H = letter

PATIENTS = {
    "alice": {
        "first": "Alice", "last": "Testpatient", "dob": "03/14/1972", "sex": "Female", "mrn": "TEST-0001",
        "address": "101 Main St, Springfield, IL 62701", "phone": "555-0101",
        "collected": "09/21/2026 06:40", "reported": "09/21/2026 09:15",
        "ordering": "R. Alvarez, MD",
        "labs": [  # panel, test, value, unit, range, flag
            ("Basic Metabolic Panel", "Sodium", "138", "mmol/L", "136-145", ""),
            ("Basic Metabolic Panel", "Potassium", "5.9", "mmol/L", "3.5-5.1", "H"),
            ("Basic Metabolic Panel", "Chloride", "104", "mmol/L", "98-107", ""),
            ("Basic Metabolic Panel", "CO2", "21", "mmol/L", "22-29", "L"),
            ("Basic Metabolic Panel", "BUN", "28", "mg/dL", "7-20", "H"),
            ("Basic Metabolic Panel", "Creatinine", "1.6", "mg/dL", "0.6-1.1", "H"),
            ("Basic Metabolic Panel", "Glucose", "182", "mg/dL", "70-99", "H"),
            ("Diabetes Monitoring", "Hemoglobin A1c", "8.1", "%", "4.0-5.6", "H"),
        ],
        "visit_date": "09/22/2026",
        "concern": "Follow up for diabetes and blood pressure. Knee pain for 2 weeks, "
                   "taking ibuprofen most days. Feeling more tired than usual.",
        "meds": [
            ("Metformin", "500 mg", "Twice daily"),
            ("Lisinopril", "20 mg", "Once daily"),
            ("Ibuprofen (OTC)", "400 mg", "2-3 times daily"),
            ("Salt substitute (NoSalt)", "1 packet", "With meals"),
        ],
        "allergies": [("Penicillin", "Hives")],
        "family": [("Father", "Heart attack at 61"), ("Mother", "Type 2 diabetes")],
        "blank_field": "allergies",
    },
    "bob": {
        "first": "Bob", "last": "Testpatient", "dob": "11/02/1958", "sex": "Male", "mrn": "TEST-0002",
        "address": "102 Main St, Springfield, IL 62701", "phone": "555-0102",
        "collected": "09/22/2026 05:55", "reported": "09/22/2026 07:30",
        "ordering": "K. Osei, MD",
        "labs": [
            ("Complete Blood Count", "WBC", "15.8", "x10^3/uL", "4.5-11.0", "H"),
            ("Complete Blood Count", "Hemoglobin", "14.2", "g/dL", "13.5-17.5", ""),
            ("Complete Blood Count", "Platelets", "242", "x10^3/uL", "150-400", ""),
            ("Basic Metabolic Panel", "Sodium", "133", "mmol/L", "136-145", "L"),
            ("Basic Metabolic Panel", "Potassium", "4.2", "mmol/L", "3.5-5.1", ""),
            ("Basic Metabolic Panel", "Creatinine", "1.1", "mg/dL", "0.7-1.3", ""),
            ("Basic Metabolic Panel", "Glucose", "148", "mg/dL", "70-99", "H"),
            ("Other", "Lactate", "2.7", "mmol/L", "0.5-2.0", "H"),
        ],
        "visit_date": "09/22/2026",
        "concern": "Cough with yellow-green sputum and fever for 3 days. More short of breath "
                   "than usual, using rescue inhaler every 2-3 hours.",
        "meds": [
            ("Tiotropium inhaler", "18 mcg", "Once daily"),
            ("Albuterol inhaler", "2 puffs", "Every 4 hours as needed"),
        ],
        "allergies": [("Sulfa drugs (Bactrim)", "Rash")],
        "family": [("Mother", "COPD"), ("Father", "Lung cancer at 70")],
        "blank_field": "family",
    },
}

LAB_NAME = "SPRINGFIELD REFERENCE LABORATORY"
FOOTER = "SYNTHETIC TEST DOCUMENT - NOT A REAL PATIENT - FOR SOFTWARE TESTING ONLY"


def lab_pdf(p, path):
    c = canvas.Canvas(path, pagesize=letter)
    c.setFillColor(colors.HexColor("#1f3a5f")); c.rect(0, H - 80, W, 80, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 18)
    c.drawString(40, H - 45, LAB_NAME)
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 62, "500 Laboratory Way, Springfield, IL 62702  |  Tel 555-0199  |  CLIA 00D0000000")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 13); c.drawString(40, H - 110, "FINAL LABORATORY REPORT")
    y = H - 135
    c.setFont("Helvetica", 10)
    left = [("Patient Name:", f"{p['last'].upper()}, {p['first'].upper()}"), ("Date of Birth:", p["dob"]),
            ("Sex:", p["sex"]), ("MRN:", p["mrn"])]
    right = [("Ordering Provider:", p["ordering"]), ("Collected:", p["collected"]),
             ("Reported:", p["reported"]), ("Specimen:", "Blood, venous")]
    for i, ((a, b), (d, e)) in enumerate(zip(left, right)):
        yy = y - i * 16
        c.setFont("Helvetica-Bold", 10); c.drawString(40, yy, a); c.drawString(320, yy, d)
        c.setFont("Helvetica", 10); c.drawString(130, yy, b); c.drawString(430, yy, e)
    y -= 80
    cols = [40, 250, 330, 420, 520]
    c.setFillColor(colors.HexColor("#e6ecf2")); c.rect(35, y - 5, W - 70, 20, fill=1, stroke=0)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 10)
    for x, t in zip(cols, ["Test", "Result", "Units", "Reference Range", "Flag"]):
        c.drawString(x, y, t)
    y -= 24
    panel = None
    for pn, test, val, unit, rng, flag in p["labs"]:
        if pn != panel:
            panel = pn
            c.setFont("Helvetica-BoldOblique", 10); c.drawString(40, y, pn); y -= 18
        c.setFont("Helvetica", 10); c.drawString(55, y, test)
        c.setFont("Helvetica-Bold" if flag else "Helvetica", 10); c.drawString(cols[1], y, val)
        c.setFont("Helvetica", 10); c.drawString(cols[2], y, unit); c.drawString(cols[3], y, rng)
        if flag:
            c.setFont("Helvetica-Bold", 10); c.drawString(cols[4], y, flag)
        c.setStrokeColor(colors.HexColor("#dddddd")); c.line(35, y - 5, W - 35, y - 5)
        y -= 20
    y -= 10
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Flag key: H = above reference range, L = below reference range.")
    c.drawString(40, y - 14, "Results verified by: M. Chen, MLS(ASCP)")
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.HexColor("#aa0000"))
    c.drawCentredString(W / 2, 30, FOOTER)
    c.save()


def intake_pdf(p, path, blank=None):
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont("Helvetica-Bold", 16); c.drawString(40, H - 50, "Springfield Family Medicine")
    c.setFont("Helvetica", 12); c.drawString(40, H - 68, "New / Returning Patient Intake Form")
    c.setFont("Helvetica", 9); c.drawRightString(W - 40, H - 50, f"Visit date: {p['visit_date']}")
    fill = "Courier"  # values look typed in by front desk

    def section(y, title):
        c.setFillColor(colors.HexColor("#333333")); c.rect(40, y - 4, W - 80, 18, fill=1, stroke=0)
        c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 10); c.drawString(46, y + 1, title)
        c.setFillColor(colors.black)
        return y - 24

    def field(x, y, label, value, width):
        c.setFont("Helvetica", 8); c.drawString(x, y + 12, label)
        c.line(x, y - 3, x + width, y - 3)
        c.setFont(fill, 11); c.drawString(x + 2, y, value)

    y = section(H - 100, "1. PATIENT INFORMATION")
    field(40, y - 10, "Last name", p["last"], 160); field(215, y - 10, "First name", p["first"], 140)
    field(370, y - 10, "Date of birth (MM/DD/YYYY)", p["dob"], 110); field(495, y - 10, "Sex", p["sex"], 75)
    y -= 45
    field(40, y - 10, "Address", p["address"], 330); field(385, y - 10, "Phone", p["phone"], 185)
    y -= 50

    y = section(y, "2. REASON FOR TODAY'S VISIT (CHIEF CONCERN)")
    c.setFont(fill, 11)
    words, line, lines = p["concern"].split(), "", []
    for w in words:
        if c.stringWidth(line + " " + w, fill, 11) > W - 100:
            lines.append(line); line = w
        else:
            line = (line + " " + w).strip()
    lines.append(line)
    for i in range(3):
        c.line(40, y - 3 - i * 20, W - 40, y - 3 - i * 20)
        if i < len(lines):
            c.drawString(42, y - i * 20, lines[i])
    y -= 70

    def table(y, title, headers, xs, rows, nrows=4, is_blank=False):
        y = section(y, title)
        c.setFont("Helvetica-Bold", 9)
        for x, h in zip(xs, headers):
            c.drawString(x, y, h)
        y -= 18
        for i in range(nrows):
            c.line(40, y - 3, W - 40, y - 3)
            if not is_blank and i < len(rows):
                c.setFont(fill, 11)
                for x, v in zip(xs, rows[i]):
                    c.drawString(x, y, v)
            y -= 20
        return y - 10

    y = table(y, "3. CURRENT MEDICATIONS (include over-the-counter and supplements)",
              ["Medication", "Dose", "How often"], [42, 260, 360], p["meds"], 5)
    y = table(y, "4. ALLERGIES (medications, foods, other)", ["Allergen", "Reaction"], [42, 300],
              p["allergies"], 3, is_blank=(blank == "allergies"))
    y = table(y, "5. FAMILY HISTORY", ["Relative", "Condition"], [42, 200], p["family"], 3,
              is_blank=(blank == "family"))

    c.setFont("Helvetica", 9); c.drawString(40, y - 5, "Patient signature: ____________________________")
    c.drawString(330, y - 5, f"Date: {p['visit_date']}")
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.HexColor("#aa0000"))
    c.drawCentredString(W / 2, 30, FOOTER)
    c.save()


def to_png(pdf, png, dpi=150):
    base = png[:-4]
    subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-singlefile", pdf, base], check=True)


def degrade(png_in, png_out, pdf_out, seed):
    rnd = random.Random(seed)
    im = Image.open(png_in).convert("L")
    im = im.rotate(rnd.uniform(1.2, 2.2) * rnd.choice([-1, 1]), expand=True, fillcolor=235)
    im = im.resize((int(im.width * 0.6), int(im.height * 0.6))).resize((im.width, im.height))
    im = ImageEnhance.Contrast(im).enhance(0.65)
    im = ImageEnhance.Brightness(im).enhance(1.05)
    px = im.load()
    for _ in range(im.width * im.height // 60):  # speckle noise
        x, y = rnd.randrange(im.width), rnd.randrange(im.height)
        px[x, y] = rnd.choice([40, 90, 200])
    im = im.filter(ImageFilter.GaussianBlur(0.8))
    im.save(png_out)
    im.convert("RGB").save(pdf_out, "PDF", resolution=150)


for key, p in PATIENTS.items():
    lab = f"{OUT}/{key}_lab_clean.pdf"; lab_pdf(p, lab); to_png(lab, lab[:-4] + ".png")
    degrade(lab[:-4] + ".png", f"{OUT}/{key}_lab_scanned.png", f"{OUT}/{key}_lab_scanned.pdf",
            seed=len(key) * 7)
    intake = f"{OUT}/{key}_intake_complete.pdf"; intake_pdf(p, intake); to_png(intake, intake[:-4] + ".png")
    blank = f"{OUT}/{key}_intake_blank_{p['blank_field']}.pdf"
    intake_pdf(p, blank, blank=p["blank_field"]); to_png(blank, blank[:-4] + ".png")
print(sorted(os.listdir(OUT)))
