"""Research-grade metadata for curated slices (the ``research`` block).

This module owns three things used by ``enrich_slice.py``:

1. ``DOCUMENT_PROMPT`` - the single prompt that asks the LLM to fill every
   per-page detail and the whole ``research`` block for one curated JSON in
   one call.
2. Rule-based extractors (``rule_page_research``) that pull the identifiers
   printed on 1940s military paperwork (form numbers, office symbols, control
   numbers, index codes, mail stamps, declassification authority, transmittal
   form fields). They need no LLM and are more precise than an LLM for these.
3. ``build_research_block`` - merges rule output and LLM output across all
   pages into the final ``research`` block (version ``RESEARCH_VERSION``).

The block is plain JSON written next to ``pages`` in the curated file; the
indexing step can later turn its fields into facets and filters.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

RESEARCH_VERSION = 2

DOCUMENT_PROMPT = """You are enriching historical military correspondence (1940s US Army Air Forces) so \
researchers and investigators can search it. The input is ONE document made of pages of OCR'd, \
human-reviewed text. Read all pages together and answer with ONE JSON object and nothing else.

Rules:
- Use only what the text says. If something is not stated, use null, "" or [] - never guess.
- Never judge whether a witness is credible. If the text states something about reliability or \
character, quote it in "statedReliability".
- Dates: copy as written in "dates"/"date"; use ISO "YYYY-MM-DD" only in fields ending in "Iso" and \
only when day, month and year are all certain (two-digit years are 19xx).
- Every list item that came from specific pages carries "pages": [page numbers as given in PAGE n].
- Keep each string short (under 200 characters).

Answer with this shape:
{
 "pages": [ one object per page, same order as the input:
   {"pageNumber": n,
    "docType": "letter"|"memo"|"routing_slip"|"report"|"form"|"envelope"|"other",
    "date": "as written or empty", "from": "", "to": "", "subject": "",
    "classification": "TOP SECRET"|"SECRET"|"CONFIDENTIAL"|"RESTRICTED"|"UNCLASSIFIED"|"",
    "summary": "one or two plain sentences",
    "persons": [names], "organizations": [names], "units": [designations],
    "acronyms": [{"raw": "AMC", "expansion": "Air Materiel Command or null"}],
    "dates": [dates as written], "places": [place names as written]} ],
 "research": {
  "archivalIdentity": {"classifications": [], "declassification": [{"authority": "", "statement": ""}],
                       "restrictions": [], "sanitized": null},
  "recordsManagement": {
    "formNumbers": [], "printEditions": [], "officeSymbols": [], "drafterInitials": [],
    "controlNumbers": [], "fileNumbers": [], "indexCodes": [{"code": "000.9", "label": "Phenomena"}],
    "stamps": [{"text": "", "number": "", "dateIso": null, "pages": []}],
    "suspenseDates": [{"dateIso": null, "pages": []}],
    "transmittalForms": [{"summary": "", "no": "", "fileDesignation": "", "repliedOrIndorsedTo": "",
                          "filed": "", "indexer": "", "pages": []}],
    "copies": [{"kind": "carbon_copy|photostat|original|other", "mark": "COPY", "pages": []}],
    "handwrittenMarks": [], "enclosures": [{"name": "", "pages": []}]},
  "correspondenceLineage": {
    "chains": [{"role": "basic_letter|1st_indorsement|2nd_indorsement|3rd_indorsement|routing_comment|memo|report|transmittal_form|telegram|other",
                "commentNo": null, "dateIso": null, "from": [], "to": [], "subject": "", "action": "", "pages": []}],
    "actions": [{"action": "interview_recommended|interrogation_not_recommended|investigation_requested|case_closed|information_requested|information_provided|referred|other",
                 "subject": "", "dateIso": null, "pages": []}],
    "references": [{"docId": "", "what": "", "pages": []}]},
  "entities": {
    "persons": [{"name": "", "rank": "", "roles": ["author|signer|addressee|witness|source|informant|scientist|other"],
                 "organizations": [], "location": "", "serviceNumber": "", "statedReliability": [], "pages": []}],
    "organizations": [{"name": "", "abbr": "", "type": "military_command|agency|government_research|company|other", "pages": []}],
    "units": [{"designation": "", "station": "", "pages": []}],
    "places": [{"name": "", "canonical": "", "type": "air_base|city|region|country|other", "note": "", "pages": []}]},
  "sightings": [{"summary": "", "eventDateIso": null, "reportedDateIso": null, "time": "", "location": "",
                 "duration": "",
                 "witnesses": {"count": null, "types": ["military|civilian|pilot|radar_operator|other"],
                               "names": [], "statedReliability": []},
                 "object": {"count": "", "shape": ["disc|circular|elliptical|oval|sphere|cigar|triangle|light|flat_bottom|domed_top|other"],
                            "color": "", "size": "", "altitude": "", "speed": "", "direction": "",
                            "trajectory": "", "maneuvers": ["hover|formation_flight|lateral_oscillation|rapid_climb|high_speed|sudden_turn|vanish|other"],
                            "trail": "", "sound": "", "brightness": ""},
                 "instruments": [], "radarVisual": null, "pages": []}],
  "evidence": [{"type": "photograph|drawing|radar|film|physical_object|report|other", "name": "", "caseNo": "",
                "finding": "", "outcome": "explained|unresolved|case_closed|referred|other", "pages": []}],
  "evaluations": [{"conclusion": "", "by": "", "dateIso": null,
                   "outcome": "explained|unresolved|case_closed|concurred|referred|other", "pages": []}],
  "historicalRefs": {"documents": [{"name": "", "pages": []}], "projects": [],
                     "foreignActors": [{"name": "", "claims": [], "pages": []}], "crossCatalog": []},
  "topics": ["short snake_case tags for the main subjects"],
  "quality": {"ocrNoise": ["examples of garbled OCR text"], "note": ""}}
}
Use empty lists for sightings/evidence/evaluations when the pages contain none. Do not invent a \
sighting for a page that only discusses paperwork."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clean(value: str) -> str:
    s = " ".join(str(value).split())
    s = re.sub(r"\s+\.", ".", s)
    s = re.sub(r"\s*-\s*", "-", s)
    return s.strip(" ,;:")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _str_items(value: Any) -> list[str]:
    out = []
    for item in _as_list(value):
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            text = " ".join(str(item).split())
            if text:
                out.append(text)
    return out


def _dict_items(value: Any) -> list[dict]:
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _page_ints(value: Any) -> list[int]:
    pages = []
    for item in _as_list(value):
        try:
            pages.append(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(set(pages))


def _uniq(items: list, key: Optional[Callable[[Any], Any]] = None) -> list:
    seen: set = set()
    out = []
    for item in items:
        marker = key(item) if key else (
            json.dumps(item, sort_keys=True, default=str) if isinstance(item, (dict, list)) else str(item).lower()
        )
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


_RANK_WORDS = {
    "col", "colonel", "lt", "lieutenant", "gen", "general", "maj", "major", "brig", "capt", "captain",
    "dr", "mr", "mrs", "miss", "ms", "prof", "sgt", "jr", "sr", "usaf", "usa", "usar",
}


def _name_key(name: str) -> str:
    tokens = [t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t and t not in _RANK_WORDS]
    return " ".join(tokens)


def _lower_key(name: str) -> str:
    return " ".join(str(name).lower().split())


def _merge_named(items: list[dict], key_fields: tuple, key_fn: Callable[[str], str]) -> list[dict]:
    """Merge dict items that describe the same thing (same normalized key)."""
    merged: dict = {}
    for item in items:
        raw_key = next((str(item.get(f)).strip() for f in key_fields if item.get(f)), "")
        key = key_fn(raw_key)
        if not key:
            continue
        entry = merged.setdefault(key, {})
        for field, value in item.items():
            if field == "pages":
                entry["pages"] = sorted(set(entry.get("pages", [])) | set(_page_ints(value)))
            elif isinstance(value, list):
                existing = entry.get(field) or []
                entry[field] = _uniq(list(existing) + [v for v in value if v not in (None, "")])
            elif value not in (None, "") and entry.get(field) in (None, ""):
                entry[field] = value
    for entry in merged.values():
        entry.setdefault("pages", [])
    return list(merged.values())


# ---------------------------------------------------------------------------
# Rule-based extractors (no LLM)
# ---------------------------------------------------------------------------

_FORM_NO = re.compile(r"\b(?:Hq\s?AMC\s+)?[Ff][Oo][Rr][Mm]\s+No\s*\.?\s*([A-Z]{0,3}\s*-?\s*\d[\d\-\s]*\d|[A-Z]{1,3}\s*-\s*\d+)", re.IGNORECASE)
_AF_HQ = re.compile(r"\bAF\s*HQ\s*-\s*(\d+)\b")
_PRINT_EDITION = re.compile(r"\(\s*((?:Rev\s*\.?\s*)?\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{2})\s*\)", re.IGNORECASE)
_OFFICE_SYMBOL = re.compile(r"\b(?=[A-Z0-9]*[A-Z])[A-Z0-9]{3,}-[A-Z0-9]{1,3}(?:-\d)?(?:/[A-Z]{2,4})?\b")
_OFFICE_SLASH = re.compile(r"\bAC/[A-Z]{1,2}-\d\b")
_INITIALS_CHAIN = re.compile(r"/([a-z0-9]{2,4})/(\d{4,5})(?!\d)")
_INITIALS_COLON = re.compile(r"/[A-Z]{2,4}\s*:\s*([a-z]{2,4})\b")
_CONTROL_LETTER = re.compile(r"\b([UVP])\s?-?\s?(\d{4,6}(?:\s?-\s?\d)?)\b")
_CONTROL_LABELED = re.compile(r"\bNO\s*\.?\s*:\s*(\d{4,8})\b")
_CONTROL_FILEDES = re.compile(r"FILE DESIGNATION AND DATE\s*:\s*(\d{4,8})")
_CONTROL_AERO = re.compile(r"\bAERO\s*(\d{3,5})\b")
_CONTROL_FSU = re.compile(r"\bF\s*-\s*SU\s*-\s*\d+\s*-\s*[A-Za-z]+")
_FILE_REF = re.compile(r"\bFile\s+([A-Z]{3,}-[A-Z0-9]+)")
_INDEX_LABELED = re.compile(
    r"\b(?:INDEX|FILED)\s*:\s*(\d{3,4}\.?\d{0,2})\s+([A-Za-z][A-Za-z \-]{2,30}?)(?=\s+(?:TO|FROM|SUMMARY|SUMMARY|Memo|FILED|INDEXER|INDEX|REMARKS)\b|$)"
)
_INDEX_PLAIN = re.compile(r"\b(\d{3}\.\d{1,2})\b")
_STAMP_MAIL = re.compile(r"\b([A-Z]{2,4}\s+MAIL\s+BRANCH)\s*([A-Z]?\s?\d{4,8})?")
_DECLASS = re.compile(r"Authority\s*:\s*(NND\s*\d{4,})", re.IGNORECASE)
_CLASS_WORDS = re.compile(r"\b(TOP\s*SECRET|SECRET|CONFIDENTIAL|RESTRICTED)\b")
_COPY_MARK = re.compile(r"\bC\s?O\s?P\s?Y\b")

_FORM_LABELS = [
    ("REPLIED OR INDORSED TO", "repliedOrIndorsedTo"),
    ("FILE DESIGNATION AND DATE", "fileDesignation"),
    ("SUSPENSE DATE", "suspenseDate"),
    ("SUMMARY", "summary"),
    ("SMRY", "summary"),
    ("INDEXER", "indexer"),
    ("REMARKS", "remarks"),
    ("INDEX", "index"),
    ("FILED", "filed"),
    ("FROM", "from"),
    ("TYPE", "type"),
    ("DATE", "date"),
    ("NO", "no"),
    ("TO", "to"),
]
_FORM_LABEL_RE = re.compile(
    r"\b(" + "|".join(label.replace(" ", r"\s+") + (r"\s*\.?" if label == "NO" else "") for label, _ in _FORM_LABELS) + r")\s*:"
)
_FORM_LABEL_KEYS = {label.replace(" ", ""): key for label, key in _FORM_LABELS}


def parse_transmittal_form(text: str) -> dict:
    """Parse the labelled fields of a records-routing form (MR-1 / disposition form).

    Returns {} when the page does not look like such a form.
    """
    t = " ".join(text.split())
    if not re.search(r"CORRESPONDENCE REFERENCE FORM|FORM NO\s*\.?\s*MR|FILE DESIGNATION AND DATE|\bSMRY\s*:", t):
        return {}
    matches = list(_FORM_LABEL_RE.finditer(t))
    out: dict = {}
    for i, match in enumerate(matches):
        label = re.sub(r"[\s.]+", "", match.group(1))
        key = _FORM_LABEL_KEYS.get(label)
        if not key:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        value = re.split(r"\bHEADQUARTERS\b", t[match.end():end])[0]
        value = " ".join(value.split())[:120].strip(" ,;")
        if value and key not in out:
            out[key] = value
    meaningful = {k: v for k, v in out.items() if k in ("summary", "no", "fileDesignation", "filed", "index", "indexer")}
    return out if meaningful else {}


def extract_records_management(text: str, page_number: Optional[int]) -> dict:
    """Identifiers printed on the page (forms, symbols, numbers, codes, stamps)."""
    t = " ".join((text or "").split())
    n = re.sub(r"\s*([/\-])\s*", r"\1", t)

    forms = [f"Form No. {_clean(m.group(1))}" for m in _FORM_NO.finditer(t)]
    forms += [f"AF HQ-{m.group(1)}" for m in _AF_HQ.finditer(t)]
    editions = [_clean(m.group(1)) for m in _PRINT_EDITION.finditer(t)]

    symbols = [m.group(0) for m in _OFFICE_SYMBOL.finditer(n)] + [m.group(0) for m in _OFFICE_SLASH.finditer(n)]

    initials = [m.group(1) for m in _INITIALS_CHAIN.finditer(n)] + [m.group(1) for m in _INITIALS_COLON.finditer(n)]

    controls = [m.group(2) for m in _INITIALS_CHAIN.finditer(n)]
    controls += [_clean(f"{m.group(1)}-{m.group(2)}") for m in _CONTROL_LETTER.finditer(t)]
    controls += [m.group(1) for m in _CONTROL_LABELED.finditer(t)]
    controls += [m.group(1) for m in _CONTROL_FILEDES.finditer(t)]
    controls += [f"AERO {m.group(1)}" for m in _CONTROL_AERO.finditer(t)]
    controls += [_clean(m.group(0)) for m in _CONTROL_FSU.finditer(t)]

    file_numbers = [m.group(1) for m in _FILE_REF.finditer(n)]

    index_codes: list[dict] = []
    for m in _INDEX_LABELED.finditer(t):
        index_codes.append({"code": m.group(1), "label": " ".join(m.group(2).split())})
    labeled = {c["code"] for c in index_codes}
    for m in _INDEX_PLAIN.finditer(t):
        if m.group(1) not in labeled:
            index_codes.append({"code": m.group(1), "label": ""})

    stamps = []
    for m in _STAMP_MAIL.finditer(t):
        number = re.sub(r"\s+", " ", m.group(2) or "").strip()
        stamps.append({"text": " ".join(m.group(1).split()), "number": number, "dateIso": None, "pages": [page_number]})

    copies = []
    if _COPY_MARK.search(t):
        copies.append({"kind": "other", "mark": "COPY", "pages": [page_number]})
    if re.search(r"photostat", t, re.IGNORECASE):
        copies.append({"kind": "photostat", "mark": "photostat", "pages": [page_number]})

    transmittal = parse_transmittal_form(text or "")
    forms_out = []
    if transmittal:
        forms_out.append({**transmittal, "pages": [page_number]})

    return {
        "formNumbers": _uniq(forms),
        "printEditions": _uniq(editions),
        "officeSymbols": _uniq(symbols),
        "drafterInitials": _uniq(initials),
        "controlNumbers": _uniq(controls),
        "fileNumbers": _uniq(file_numbers),
        "indexCodes": _uniq(index_codes, key=lambda c: (c["code"], c["label"].lower())),
        "stamps": stamps,
        "copies": copies,
        "transmittalForms": forms_out,
    }


def rule_page_research(page: dict) -> dict:
    """Everything the rules can read from one page, for ``build_research_block``."""
    text = page.get("text") or page.get("markdown") or ""
    page_number = page.get("pageNumber")
    t = " ".join(text.split())
    classifications = _uniq([" ".join(m.group(1).split()).upper() for m in _CLASS_WORDS.finditer(t[:600])])
    declass = [
        {"authority": " ".join(m.group(1).split()).upper(), "statement": f"DECLASSIFIED Authority {' '.join(m.group(1).split()).upper()}"}
        for m in _DECLASS.finditer(t)
    ]
    return {
        "pageNumber": page_number,
        "classifications": classifications,
        "declassification": declass,
        "recordsManagement": extract_records_management(text, page_number),
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _rm_lists(llm_payloads: list[dict], key: str) -> list:
    out: list = []
    for payload in llm_payloads:
        out.extend(_as_list(payload.get("recordsManagement", {}).get(key)) if isinstance(payload.get("recordsManagement"), dict) else [])
    return out


def _chain_sort_key(chain: dict) -> tuple:
    pages = _page_ints(chain.get("pages"))
    return (str(chain.get("dateIso") or "9999-99-99"), pages[0] if pages else 9999)


def build_research_block(
    pages: list[dict],
    rule_pages: list[dict],
    llm_payloads: list[dict],
    date_entries: list[dict],
    document: Optional[dict],
) -> dict:
    """Merge rule output and LLM output into the final ``research`` block."""
    payloads = [p for p in llm_payloads if isinstance(p, dict)]
    arch_llm = [p.get("archivalIdentity") for p in payloads if isinstance(p.get("archivalIdentity"), dict)]
    lineage = [p.get("correspondenceLineage") for p in payloads if isinstance(p.get("correspondenceLineage"), dict)]
    entities = [p.get("entities") for p in payloads if isinstance(p.get("entities"), dict)]
    hist = [p.get("historicalRefs") for p in payloads if isinstance(p.get("historicalRefs"), dict)]
    quality = [p.get("quality") for p in payloads if isinstance(p.get("quality"), dict)]

    page_numbers = [p.get("pageNumber") for p in pages if isinstance(p.get("pageNumber"), int)]
    iso_dates = sorted(e["iso"] for e in date_entries if e.get("iso"))

    def rule_values(key: str) -> list:
        return [v for rp in rule_pages for v in rp["recordsManagement"].get(key, [])]

    def combined_strings(key: str) -> list[str]:
        return _uniq(rule_values(key) + _str_items(_rm_lists(payloads, key)))

    index_codes = _uniq(
        [c for c in rule_values("indexCodes")]
        + [
            {"code": str(c.get("code", "")).strip(), "label": str(c.get("label") or "").strip()}
            for c in _dict_items(_rm_lists(payloads, "indexCodes"))
            if c.get("code")
        ],
        key=lambda c: (c["code"], c["label"].lower()),
    )
    labeled_codes = {c["code"] for c in index_codes if c["label"]}
    index_codes = [c for c in index_codes if c["label"] or c["code"] not in labeled_codes]

    def pages_of(item: dict) -> list[int]:
        return _page_ints(item.get("pages"))

    stamps = _uniq(
        rule_values("stamps") + [{**s, "pages": pages_of(s)} for s in _dict_items(_rm_lists(payloads, "stamps"))],
        key=lambda s: (str(s.get("text", "")).lower(), tuple(_page_ints(s.get("pages")))),
    )
    for stamp in stamps:
        stamp["pages"] = [p for p in _page_ints(stamp.get("pages"))]

    rule_form_pages = {p for f in rule_values("transmittalForms") for p in _page_ints(f.get("pages"))}
    forms = rule_values("transmittalForms") + [
        {**f, "pages": pages_of(f)}
        for f in _dict_items(_rm_lists(payloads, "transmittalForms"))
        if not (set(pages_of(f)) & rule_form_pages)
    ]
    copies = _uniq(
        [{**c, "pages": _page_ints(c.get("pages"))} for c in rule_values("copies") + _dict_items(_rm_lists(payloads, "copies"))],
        key=lambda c: (str(c.get("mark", "")).lower(), tuple(c["pages"])),
    )

    chains = _uniq(
        [{**c, "pages": pages_of(c)} for ch in lineage for c in _dict_items(ch.get("chains"))],
        key=lambda c: json.dumps(c, sort_keys=True, default=str),
    )
    chains.sort(key=_chain_sort_key)
    actions = _uniq(
        [{**a, "pages": pages_of(a)} for ch in lineage for a in _dict_items(ch.get("actions"))],
        key=lambda a: json.dumps(a, sort_keys=True, default=str),
    )
    references = _merge_named(
        [{**r, "pages": pages_of(r)} for ch in lineage for r in _dict_items(ch.get("references"))],
        ("docId", "what"),
        _lower_key,
    )

    def ent(key: str) -> list[dict]:
        return [{**e, "pages": pages_of(e)} for grp in entities for e in _dict_items(grp.get(key))]

    persons = _merge_named(ent("persons"), ("name",), _name_key)
    organizations = _merge_named(ent("organizations"), ("abbr", "name"), _lower_key)
    units = _merge_named(ent("units"), ("designation",), _lower_key)
    places = _merge_named(ent("places"), ("canonical", "name"), _lower_key)

    def collect(key: str) -> list[dict]:
        return [{**i, "pages": pages_of(i)} for p in payloads for i in _dict_items(p.get(key))]

    sightings = collect("sightings")
    for i, sighting in enumerate(sightings, start=1):
        sighting["id"] = f"S{i}"
    evidence = _uniq(collect("evidence"))
    evaluations = _uniq(collect("evaluations"))

    declass_rules = [d for rp in rule_pages for d in rp["declassification"]]
    declass_llm = [d for a in arch_llm for d in _dict_items(a.get("declassification")) if d.get("authority") or d.get("statement")]
    declassification = _uniq(
        declass_rules + declass_llm,
        key=lambda d: re.sub(r"\s+", "", str(d.get("authority") or d.get("statement", ""))).lower(),
    )
    classifications = _uniq(
        [c for rp in rule_pages for c in rp["classifications"]]
        + [c.upper() for a in arch_llm for c in _str_items(a.get("classifications"))]
    )
    sanitized = next((a.get("sanitized") for a in arch_llm if isinstance(a.get("sanitized"), bool)), None)

    doc = document if isinstance(document, dict) else {}
    return {
        "v": RESEARCH_VERSION,
        "archivalIdentity": {
            "sourceFile": doc.get("sourceFile") or doc.get("fileName"),
            "pageRange": {"from": min(page_numbers), "to": max(page_numbers)} if page_numbers else None,
            "datesCovered": {"start": iso_dates[0], "end": iso_dates[-1]} if iso_dates else None,
            "classifications": classifications,
            "declassification": declassification,
            "recordGroup": None,
            "series": None,
            "naid": None,
            "microfilm": None,
            "restrictions": _uniq([r for a in arch_llm for r in _str_items(a.get("restrictions"))]),
            "sanitized": sanitized,
        },
        "recordsManagement": {
            "formNumbers": combined_strings("formNumbers"),
            "printEditions": combined_strings("printEditions"),
            "officeSymbols": combined_strings("officeSymbols"),
            "drafterInitials": combined_strings("drafterInitials"),
            "controlNumbers": combined_strings("controlNumbers"),
            "fileNumbers": combined_strings("fileNumbers"),
            "indexCodes": index_codes,
            "stamps": stamps,
            "suspenseDates": _uniq(
                [{**s, "pages": pages_of(s)} for s in _dict_items(_rm_lists(payloads, "suspenseDates")) if s.get("dateIso")]
            ),
            "transmittalForms": forms,
            "copies": copies,
            "handwrittenMarks": _uniq(_str_items(_rm_lists(payloads, "handwrittenMarks"))),
            "enclosures": _merge_named(
                [{**e, "pages": pages_of(e)} for e in _dict_items(_rm_lists(payloads, "enclosures"))],
                ("name",),
                _lower_key,
            ),
        },
        "correspondenceLineage": {"chains": chains, "actions": actions, "references": references},
        "entities": {"persons": persons, "organizations": organizations, "units": units, "places": places},
        "sightings": sightings,
        "evidence": evidence,
        "evaluations": evaluations,
        "historicalRefs": {
            "documents": _merge_named(
                [{**d, "pages": pages_of(d)} for h in hist for d in _dict_items(h.get("documents"))], ("name",), _lower_key
            ),
            "projects": _uniq([p for h in hist for p in _str_items(h.get("projects"))]),
            "foreignActors": _merge_named(
                [{**a, "pages": pages_of(a)} for h in hist for a in _dict_items(h.get("foreignActors"))], ("name",), _lower_key
            ),
            "crossCatalog": _uniq([c for h in hist for c in _str_items(h.get("crossCatalog"))]),
        },
        "topics": _uniq([t for p in payloads for t in _str_items(p.get("topics"))]),
        "quality": {
            "llmAssisted": bool(payloads),
            "copyMultiples": len({p for c in copies for p in c["pages"]}) > 1,
            "ocrNoise": _uniq([n for q in quality for n in _str_items(q.get("ocrNoise"))])[:10],
            "note": next((str(q["note"]) for q in quality if q.get("note")), None),
        },
    }
