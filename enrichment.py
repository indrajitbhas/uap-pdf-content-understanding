"""Tier 1 enrichment: zero-cost, regex/string-based metadata extraction.

No LLM calls and no Azure dependencies, so this module can be unit-tested
directly. Every public extractor returns None when nothing is found and never
raises for missing/odd input.
"""
import re

# ---------------------------------------------------------------------------
# Filename: "18_100754_ General 1946-7_Vol_2__p01-02.curated.json"
# ---------------------------------------------------------------------------
NARA_ID_RE = re.compile(r"^\s*(\d+)_(\d+)(?:_|\b)")


def _nara_parts(file_name, source_json=None):
    for name in (file_name, source_json):
        if isinstance(name, str):
            m = NARA_ID_RE.match(name.rsplit("/", 1)[-1])
            if m:
                return m.group(1), m.group(2)
    return None, None


def extract_nara_record_group(file_name, source_json=None):
    return _nara_parts(file_name, source_json)[0]


def extract_nara_item_number(file_name, source_json=None):
    return _nara_parts(file_name, source_json)[1]


# ---------------------------------------------------------------------------
# Declassification authority: "Authority: NND 700188"
# ---------------------------------------------------------------------------
NND_RE = re.compile(r"\bNND\s*-?\s*(\d{3,})\b")
AUTHORITY_RE = re.compile(r"Authority\s*:\s*([A-Z0-9][A-Z0-9 ]*)")


def extract_declassification_authority(text):
    if not text:
        return None
    m = NND_RE.search(text)
    if m:
        return f"NND {m.group(1)}"
    m = AUTHORITY_RE.search(text)
    if m:
        # OCR text is often one long line; keep only the first few tokens so we
        # don't swallow the following heading (e.g. "... WRIGHT FIELD").
        tokens = m.group(1).split()[:3]
        value = " ".join(tokens).strip()
        return value or None
    return None


# ---------------------------------------------------------------------------
# Classification level (highest wins)
# ---------------------------------------------------------------------------
CLASSIFICATION_ORDER = ["TOP SECRET", "SECRET", "CONFIDENTIAL", "RESTRICTED", "UNCLASSIFIED"]
# Uppercase-only, standalone words (headers/footers stamps). \b keeps
# "DECLASSIFIED" from matching UNCLASSIFIED.
CLASSIFICATION_RE = re.compile(r"\b(TOP\s+SECRET|SECRET|CONFIDENTIAL|RESTRICTED|UNCLASSIFIED)\b")


def extract_classification_level(text):
    if not text:
        return None
    found = {re.sub(r"\s+", " ", m.group(1)) for m in CLASSIFICATION_RE.finditer(text)}
    for level in CLASSIFICATION_ORDER:
        if level in found:
            return level
    return None


# ---------------------------------------------------------------------------
# Document type: Letter, Memo, Report, Telegram, Form, Other
# ---------------------------------------------------------------------------
DOC_TYPE_SIGNALS = {
    "Telegram": [r"\bTELEGRAM\b", r"\bTELETYPE\b", r"\bTWX\b", r"\bCABLEGRAM\b",
                 r"\bRADIOGRAM\b", r"\bMESSAGE FORM\b", r"\bINCOMING CLASSIFIED MESSAGE\b"],
    "Memo": [r"\bMEMORANDUM\b", r"\bMEMO\s+(?:FOR|TO)\b", r"\bR\s*&\s*R\b",
             r"\bROUTING AND RECORD\b", r"\bDISPOSITION FORM\b"],
    "Letter": [r"\bSUBJECT\s*:", r"\bTO\s*:", r"\bFROM\s*:", r"\bDear\s+(?:Sir|Mr|Mrs|Miss|Dr|General|Colonel)",
               r"\b(?:Sincerely|Very truly yours|Respectfully yours)\b"],
    "Report": [r"\bREPORT\s+(?:NO|NUMBER|OF)\b", r"\b(?:INTELLIGENCE|TECHNICAL|INCIDENT|SIGHTING)\s+REPORT\b",
               r"\bSUMMARY\s+OF\b", r"\bINCIDENT\s+(?:NO|NUMBER)\b", r"\bFINDINGS\b"],
    "Form": [r"\bFORM\s+NO\b", r"\bFORM\s+\d", r"\bFORM\s*#"],
}
_DOC_TYPE_PATTERNS = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in DOC_TYPE_SIGNALS.items()}
# Tie-break priority (earlier wins on equal score).
DOC_TYPE_PRIORITY = ["Telegram", "Memo", "Letter", "Report", "Form"]


def extract_document_type(text):
    if not text:
        return None
    scores = {k: sum(1 for p in pats if p.search(text)) for k, pats in _DOC_TYPE_PATTERNS.items()}
    best = max(DOC_TYPE_PRIORITY, key=lambda k: (scores[k], -DOC_TYPE_PRIORITY.index(k)))
    return best if scores[best] > 0 else "Other"


# ---------------------------------------------------------------------------
# Document year (earliest 1940-2000 year from real date patterns)
# ---------------------------------------------------------------------------
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_PATTERNS = [
    # 23 September 1947 / 22 Dec 47
    re.compile(rf"\b\d{{1,2}}\s+{_MON},?\s+(\d{{4}}|\d{{2}})\b", re.IGNORECASE),
    # Sep 23 1947 / DEC 19, 1947
    re.compile(rf"\b{_MON}\s+\d{{1,2}},?\s+(\d{{4}})\b", re.IGNORECASE),
    # NOV 1947
    re.compile(rf"\b{_MON},?\s+(\d{{4}})\b", re.IGNORECASE),
    # 9/23/47 or 9/23/1947
    re.compile(r"\b\d{1,2}/\d{1,2}/(\d{4}|\d{2})\b"),
]
# Skip form-revision / print codes like "(Rev 10 Sep 46)".
REVISION_PREFIX_RE = re.compile(r"\bRev\.?\s*$", re.IGNORECASE)
YEAR_MIN, YEAR_MAX = 1940, 2000


def _to_year(raw):
    y = int(raw)
    if len(raw) == 2:
        y += 1900
    return y if YEAR_MIN <= y <= YEAR_MAX else None


def extract_doc_year(text):
    if not text:
        return None
    years = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            if REVISION_PREFIX_RE.search(text[max(0, m.start() - 6):m.start()]):
                continue
            y = _to_year(m.group(1))
            if y:
                years.append(y)
    return min(years) if years else None


# ---------------------------------------------------------------------------
# Originating organisation: "HEADQUARTERS AIR MATERIEL COMMAND" or "FROM: ..."
# ---------------------------------------------------------------------------
HQ_RE = re.compile(r"\bHEADQUARTERS\b[,:]?\s+(.{0,160})", re.IGNORECASE)
FROM_RE = re.compile(
    r"\bFROM\s*:\s*(.+?)(?=\s+(?:TO|SUBJECT|DATE|TYPE|RE|CC|INFO|NO|REF)\s*:|[\r\n]|$)",
    re.IGNORECASE,
)
ORG_STOP_WORDS = {
    "DECLASSIFIED", "SECRET", "TOP", "CONFIDENTIAL", "RESTRICTED", "UNCLASSIFIED",
    "AUTHORITY", "SUBJECT", "TO", "FROM", "DATE", "IN", "REPLY", "REFER", "FILE",
}
ORG_TERMINAL_WORDS = {
    "COMMAND", "COMMANDS", "FORCE", "FORCES", "ARMY", "NAVY", "CORPS", "DIVISION", "WING",
    "GROUP", "SQUADRON", "BASE", "DEPARTMENT", "STAFF", "AGENCY", "CENTER", "CENTRE",
    "DISTRICT", "BUREAU", "SERVICE", "SERVICES", "ZONE", "AREA", "REGION",
}
KEEP_UPPER = {"USAF", "AAF", "AMC", "ADC", "SAC", "TAC", "ATC", "MATS", "AFB", "US", "USA",
              "USN", "CIA", "FBI", "ONI", "OSI", "AFOSI", "ATIC", "OIN"}


SMALL_WORDS = {"OF", "THE", "AND", "FOR"}


def _pretty(words):
    out = []
    for i, w in enumerate(words):
        u = w.upper()
        if u in KEEP_UPPER:
            out.append(u)
        elif u in SMALL_WORDS and i > 0:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _org_from_headquarters(text):
    m = HQ_RE.search(text)
    if not m:
        return None
    words = []
    for raw in m.group(1).split():
        tok = raw.strip(",.;:()")
        if not tok or not re.fullmatch(r"[A-Za-z&'-]+", tok) or tok.upper() in ORG_STOP_WORDS:
            break
        words.append(tok)
        if raw.endswith((",", ";", ":")) or len(words) >= 8:
            break
    # Trim trailing place names: cut back to the last organisational noun.
    term = [i for i, w in enumerate(words) if w.upper() in ORG_TERMINAL_WORDS]
    if term:
        words = words[: term[-1] + 1]
    if not words:
        return None
    return "Headquarters " + _pretty(words)


def _org_from_from_line(text):
    m = FROM_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip(" ,.;:")[:80].strip()
    return value or None


def extract_originating_org(text):
    if not text:
        return None
    return _org_from_headquarters(text) or _org_from_from_line(text)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------
TIER1_FIELDS = (
    "naraRecordGroup", "naraItemNumber", "declassificationAuthority",
    "classificationLevel", "documentType", "docYear", "originatingOrg",
)


def extract_tier1(text, file_name=None, source_json=None, warnings=None):
    """Return all Tier 1 fields. Any extractor failure yields None (+ warning)."""
    extractors = {
        "naraRecordGroup": lambda: extract_nara_record_group(file_name, source_json),
        "naraItemNumber": lambda: extract_nara_item_number(file_name, source_json),
        "declassificationAuthority": lambda: extract_declassification_authority(text),
        "classificationLevel": lambda: extract_classification_level(text),
        "documentType": lambda: extract_document_type(text),
        "docYear": lambda: extract_doc_year(text),
        "originatingOrg": lambda: extract_originating_org(text),
    }
    out = {}
    for name, fn in extractors.items():
        try:
            out[name] = fn()
        except Exception as ex:  # defensive: never fail the record on enrichment
            out[name] = None
            if warnings is not None:
                warnings.append({"message": f"{name} extraction failed: {ex}"})
    return out
