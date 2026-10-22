"""A small, curated antibiotic drug-class and brand-name table.

`check_allergy_conflict` (app/tools.py) matches a proposed medication against
a patient's *recorded* allergy text -- it has no way to know, from patient
data alone, that amoxicillin and penicillin are the same drug class, that
Bactrim is a sulfa drug, or that Zithromax is azithromycin. That relationship
is general pharmacology, not something any FHIR resource for this patient will
ever state. This module supplies just enough of that general knowledge for
check_allergy_conflict to also catch a same-class conflict and a brand-name
conflict, not only an exact/substring name match.

**This is explicitly NOT a production drug-interaction database and must
never be presented or relied on as one.** A real deployment needs a licensed,
maintained clinical reference (First Databank, Medi-Span, Multum, or
equivalent) integrated at the EHR/pharmacy layer, with actual pharmacist-
reviewed cross-reactivity data, allergy-severity grading, and routine
updates. This table is a scoped stand-in covering the antibiotic classes an
overnight cross-cover resident is most likely to ask about; expand it only
when a real case needs another class.

2026-09-25 (found in a prod browser check): the table used to hold only the
penicillin class, so a recorded "Sulfa drugs" allergy matched no class at
all and check_allergy_conflict("Bactrim") reported no conflict. It now covers
the common antibiotic classes with generic AND brand names. Deliberate
choices, each pinned by a test:

- Aliases match as WHOLE WORDS, not substrings. "sulfa" is a substring of
  "sulfate", so substring matching would flag morphine/magnesium/ferrous
  sulfate against a sulfa allergy.
- The sulfonamide class is sulfonamide ANTIBIOTICS (and sulfasalazine).
  Non-antibiotic sulfonamides (furosemide, hydrochlorothiazide, celecoxib, ...)
  are not in it: cross-reactivity with the antibiotics is not established, and
  flagging them all would bury real conflicts. That is a clinical policy
  choice to revisit with a pharmacist, not an oversight.
- Penicillins and cephalosporins are separate classes. Cross-reactivity between
  them depends on shared side chains rather than the shared beta-lactam ring
  (see app/retrieval/corpus/antibiotic_cross_reactivity.md), so a penicillin
  allergy does not automatically flag a cephalosporin here.
"""

from __future__ import annotations

import re

# class name (shown to the resident) -> lowercase aliases: generic names, brand
# names, common abbreviations and the class's own names ("sulfa", "quinolone").
DRUG_CLASSES: dict[str, list[str]] = {
    "penicillins": [
        "penicillin", "penicillin g", "penicillin v", "pen vk", "bicillin", "amoxicillin", "amoxil", "moxatag",
        "ampicillin", "ampicillin-sulbactam", "unasyn", "amoxicillin-clavulanate", "amoxicillin/clavulanate",
        "augmentin", "piperacillin", "piperacillin-tazobactam", "pip-tazo", "zosyn", "nafcillin", "oxacillin",
        "dicloxacillin", "ticarcillin",
    ],
    "cephalosporins": [
        "cephalosporin", "cephalexin", "keflex", "cefazolin", "ancef", "cefadroxil", "cefuroxime", "ceftin",
        "cefprozil", "cefaclor", "cefdinir", "omnicef", "cefpodoxime", "vantin", "cefixime", "suprax", "cefditoren",
        "ceftriaxone", "rocephin", "cefotaxime", "claforan", "ceftazidime", "fortaz", "tazicef", "cefepime",
        "maxipime", "cefoxitin", "cefotetan", "ceftaroline", "teflaro",
    ],
    "sulfonamide antibiotics": [
        "sulfa", "sulfa drug", "sulfa antibiotic", "sulfonamide", "sulfonamide antibiotic", "sulfamethoxazole",
        "sulfamethoxazole-trimethoprim", "trimethoprim-sulfamethoxazole", "sulfamethoxazole/trimethoprim",
        "trimethoprim/sulfamethoxazole", "smx-tmp", "tmp-smx", "smx/tmp", "tmp/smx", "co-trimoxazole",
        "cotrimoxazole", "bactrim", "bactrim ds", "septra", "septra ds", "sulfatrim", "sulfadiazine",
        "silver sulfadiazine", "silvadene", "sulfisoxazole", "gantrisin", "sulfacetamide", "sulfasalazine",
        "azulfidine", "pediazole",
    ],
    "fluoroquinolones": [
        "fluoroquinolone", "quinolone", "ciprofloxacin", "cipro", "levofloxacin", "levaquin", "moxifloxacin",
        "avelox", "ofloxacin", "floxin", "norfloxacin", "gemifloxacin", "delafloxacin", "baxdela",
    ],
    "macrolides": [
        "macrolide", "azithromycin", "zithromax", "z-pak", "zpak", "clarithromycin", "biaxin", "erythromycin",
        "ery-tab",
    ],
    "tetracyclines": [
        "tetracycline", "doxycycline", "vibramycin", "doryx", "minocycline", "minocin",
    ],
    "carbapenems": [
        "carbapenem", "meropenem", "merrem", "imipenem", "imipenem-cilastatin", "primaxin", "ertapenem", "invanz",
        "doripenem",
    ],
    "aminoglycosides": [
        "aminoglycoside", "gentamicin", "tobramycin", "amikacin", "streptomycin", "neomycin",
    ],
    # Single-drug groups: the point is brand <-> generic ("Flagyl" against a metronidazole allergy).
    "clindamycin": ["clindamycin", "cleocin"],
    "metronidazole": ["metronidazole", "flagyl"],
    "vancomycin": ["vancomycin", "vancocin"],
    "nitrofurantoin": ["nitrofurantoin", "macrobid", "macrodantin"],
    "linezolid": ["linezolid", "zyvox"],
}

# Chemical forms that merely CONTAIN "sulf" and are not sulfa drugs. They are removed before any
# matching, so "morphine sulfate" or "sodium polystyrene sulfonate" can never look like "sulfa".
_NOT_SULFA_RE = re.compile(r"\bsulf(?:ate|ite|ide|onate)s?\b")
_WS_RE = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """Lowercased, whitespace-collapsed, with sulfate/sulfite/sulfide/sulfonate forms removed."""
    return _WS_RE.sub(" ", _NOT_SULFA_RE.sub(" ", text.lower())).strip()


def _alias_pattern(alias: str) -> re.Pattern[str]:
    # Whole word (letters/digits on either side disqualify), optional plural: "penicillins", "sulfa drugs".
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?:s|es)?(?![a-z0-9])")


_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    drug_class: [_alias_pattern(alias) for alias in aliases] for drug_class, aliases in DRUG_CLASSES.items()
}


def drug_classes_for(name: str) -> list[str]:
    """Every curated class whose alias appears (as a whole word) in `name`, in table order. A recorded
    allergy such as "penicillin and sulfa (hives)" can belong to more than one."""
    normalized = normalize_for_match(name)
    if not normalized:
        return []
    return [drug_class for drug_class, patterns in _PATTERNS.items() if any(p.search(normalized) for p in patterns)]


def cross_reactive_class(allergy_text: str, medication_name: str) -> str | None:
    """The shared curated class if `allergy_text` (a recorded allergy) and `medication_name` (a proposed
    medication) are in the same class -- e.g. ("Sulfa drugs", "Bactrim DS") -> "sulfonamide antibiotics",
    ("penicillin", "Augmentin") -> "penicillins". None if either is in no curated class or they share none.
    Does not decide whether this is a "direct" name match vs. a class match -- app/tools.py only treats
    this as a cross-reactive finding when a direct match didn't already fire."""
    shared = set(drug_classes_for(allergy_text)) & set(drug_classes_for(medication_name))
    return next((c for c in DRUG_CLASSES if c in shared), None)


# Class NAMES ("sulfa", "cephalosporin", ...) describe a group, not a drug someone could be about to give. They
# identify an allergy's class but are never treated as a proposed medication.
_CLASS_NAME_ONLY = {
    "sulfa", "sulfa drug", "sulfa antibiotic", "sulfonamide", "sulfonamide antibiotic", "cephalosporin",
    "fluoroquinolone", "quinolone", "macrolide", "carbapenem", "aminoglycoside",
}
_DRUG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (alias, _alias_pattern(alias))
    for aliases in DRUG_CLASSES.values()
    for alias in aliases
    if alias not in _CLASS_NAME_ONLY
]


def drug_names_in(text: str) -> list[str]:
    """Curated DRUG names (generic and brand, never a class name) mentioned as whole words in `text`, in order
    of first appearance, longest match winning where aliases overlap ("bactrim ds" over "bactrim"). Used by
    verification to find every drug the allergy check could evaluate; sulfate/sulfite forms never match."""
    normalized = normalize_for_match(text)
    spans: list[tuple[int, int, str]] = []
    for alias, pattern in _DRUG_PATTERNS:
        spans.extend((m.start(), m.end(), alias) for m in pattern.finditer(normalized))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    chosen: list[tuple[int, int, str]] = []
    for start, end, alias in spans:
        if all(end <= c_start or start >= c_end for c_start, c_end, _ in chosen):
            chosen.append((start, end, alias))
    seen: set[str] = set()
    ordered = []
    for _, _, alias in sorted(chosen):
        if alias not in seen:
            seen.add(alias)
            ordered.append(alias)
    return ordered
