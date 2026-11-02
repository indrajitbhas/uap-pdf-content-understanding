#!/usr/bin/env python3
"""Enrich a curated slice (per-communication JSON) for search indexing.

Runs after HITL review and slicing. For each page it extracts suitable
details (document type, date, from/to, subject, classification, persons,
organizations, units, acronyms, dates, places, summary) and then standardizes
acronyms, dates and place names at document level, so the indexed document
carries canonical forms.

Two-node split, mirrored in ``main.py``:

1. **extract_page_details** — one LLM call per page (batched): pulls the
   details above straight from the (user-corrected) page text.
2. **standardize_details** — document-level normalization: first-occurrence
   acronym glossary, ISO 8601 dates, canonical place names, and a
   ``standardText`` per page where acronyms with known expansions are
   expanded on their first occurrence.

The original curated slice is never copied into a new file: the enrichment is
written back **in place** (same file / same blob, overwritten). The curated
schema is preserved and the slice gains, per page, an ``enrichment`` block and
a ``standardText``, plus a document-level ``normalized`` block::

    {
      "schema": "curated/1",
      "document": { ... original metadata ..., "enrichedAt": "2026-..." },
      "pages": [ { ...original page..., "enrichment": {...}, "standardText": "..." } ],
      "normalized": {
        "acronyms": { "AMC": { "expansion": "Air Materiel Command", ... } },
        "dates":   [ { "raw": "24 September 1947", "iso": "1947-09-24", ... } ],
        "places":  { "Washington, D.C.": { "raws": [...], "pages": [...] } }
      }
    }

The slice also gains a top-level ``research`` block (see ``research_block.py``):
archival identity, records-management identifiers, correspondence lineage,
entities, sightings, evidence, evaluations, historical references, topics and
quality notes. With ``--llm`` the per-page details AND the research block come
from ONE LLM call per slice; identifiers printed on the pages (form numbers,
office symbols, index codes...) are always read by rules and stay authoritative.

``document.enrichedAt`` marks the slice as enriched so re-runs skip it (see
``is_already_enriched``); ``--force`` re-enriches anyway.

LLM extraction is opt-in (``--llm`` / ``--enrich-llm``) and uses any
OpenAI-compatible chat-completions endpoint — the same family as the
slice-llm grouping in ``split_curated.py``/``main.py`` (OpenAI, Azure OpenAI,
Azure AI Foundry, RouteLLM, ...). Without an LLM the extraction falls back to
cheap deterministic heuristics (docType via layout markers, classification,
acronym detection, date normalization), so the stage still produces useful
output and costs nothing.

Usage::

    python enrich_slice.py --input path/to/doc__p14.curated.json               # enrich in place
    python enrich_slice.py --input path/to/doc__p14.curated.json --llm         # LLM extraction
    python enrich_slice.py --input path/to/doc__p14.curated.json --dry-run
    python enrich_slice.py --input path/to/doc__p14.curated.json --output-dir copies/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from research_block import DOCUMENT_PROMPT, build_research_block, rule_page_research

LOGGER = logging.getLogger("pdf_content_understanding")

# ---------------------------------------------------------------------------
# Rule-based pieces (work without an LLM)
# ---------------------------------------------------------------------------

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
)
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_PATTERNS = [
    re.compile(rf"(\d{{1,2}})\s+({_MONTH})\w*\s*,?\s+(\d{{2,4}})", re.IGNORECASE),
    re.compile(rf"({_MONTH})\w*\s+(\d{{1,2}})\s*,?\s+(\d{{2,4}})", re.IGNORECASE),
    re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})"),
]

_FORM_HEAD = re.compile(r"(?:form|FORM)\s*No\.?\s*[\d-]+")
_CLASSIFICATION_RE = re.compile(
    r"\b(TOP\s*SECRET|SECRET|CONFIDENTIAL|RESTRICTED|UNCLASSIFIED)\b",
    re.IGNORECASE,
)
_ENVELOPE_KINDS = {"envelope", "folder_cover", "book_cover", "index_or_list", "newspaper_clipping"}
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9&.]{1,}\b")
_ACRONYM_STOP = {
    "COPY", "SECRET", "TOP", "SUBJECT", "ATTN", "AFTER", "BEFORE", "AROUND",
    "INTO", "UPON", "FROM", "WITH", "THAT", "THIS", "THESE", "THOSE", "THEY",
    "WERE", "WHERE", "WHEN", "WHAT", "NOT", "BUT", "AND", "FOR", "THE",
    "TO", "IN", "ON", "OR", "AS", "AT", "BY", "AN", "NO", "IT", "IS", "BE",
    "ALL", "ARE", "HAS", "HAD", "HAVE", "ITS", "OUR", "YOUR", "OUT", "OFF",
}

_PLACE_ABBR = [
    (r"D\.?\s*C\.?", "D.C."),
    (r"\bSt\.", "Saint"),
    (r"\bFt\.?", "Fort"),
    (r"\bMt\.?", "Mount"),
    (r"\bAve\.?", "Avenue"),
    (r"\bBlvd\.?", "Boulevard"),
    (r"\bRd\.?", "Road"),
    (r"\bDr\.?", "Drive"),
]


def normalize_date(raw: str) -> Optional[str]:
    """Best-effort normalization of a date string to ISO 8601 (YYYY-MM-DD).

    Handles "24 September 1947", "Sep 24, 1947", "24 Sep 47", "12/3/46",
    "12-3-46" and already-ISO strings. Two-digit years are treated as 19xx
    (these collections are 20th century military records). Numeric dates with
    a first part > 12 are read as day/month (European), otherwise month/day.
    Returns None when the string cannot be parsed.
    """
    s = " ".join(str(raw).split())
    if not s:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        year, month, day = (int(p) for p in s.split("-"))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return s
        return None

    match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
    if match:
        a, b, year = (int(p) for p in match.groups())
        if year < 100:
            year += 1900
        if a > 12 and b <= 12:  # European day/month ordering
            day, month = a, b
        else:
            month, day = a, b
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return None

    for pattern in _DATE_PATTERNS[:2]:
        match = re.fullmatch(pattern, s)
        if not match:
            continue
        groups = match.groups()
        if groups[0].lower()[:3] in _MONTH_NUM:  # "September 24, 1947"
            month_name, day, year_text = groups
        elif groups[1].lower()[:3] in _MONTH_NUM:  # "24 September 1947"
            day, month_name, year_text = groups
        else:
            continue
        month = _MONTH_NUM[month_name.lower()[:3]]
        day = int(day)
        year = int(year_text)
        if year < 100:
            year += 1900
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def detect_acronyms(text: str) -> list[dict]:
    """Cheap no-LLM acronym detection: uppercase tokens of >= 2 letters."""
    out: list[dict] = []
    seen: set = set()
    for token in _ACRONYM_RE.findall(text or ""):
        letters = sum(1 for ch in token if ch.isalpha())
        if letters < 2:
            continue
        key = token.upper()
        if key in _ACRONYM_STOP or key in seen:
            continue
        seen.add(key)
        out.append({"raw": token, "expansion": None})
        if len(out) >= 100:
            break
    return out


def extract_rule_details(page: dict) -> dict:
    """Deterministic fallback extraction (no LLM): layout/pattern based."""
    md = page.get("markdown") or ""
    text = page.get("text") or ""
    lines = md.splitlines()
    head = "\n".join(lines[:12])
    kind = str(page.get("kind") or "").lower()

    details = {
        "docType": "other",
        "date": "",
        "from": "",
        "to": "",
        "subject": "",
        "classification": "",
        "summary": "",
        "persons": [],
        "organizations": [],
        "units": [],
        "acronyms": [],
        "dates": [],
        "places": [],
    }

    if kind in _ENVELOPE_KINDS:
        details["docType"] = "envelope"
    elif re.search(r"^DATE\s*:|^FROM\s*:", head, re.IGNORECASE | re.MULTILINE):
        details["docType"] = "routing_slip"
        from_match = re.search(r"^FROM\s*:\s*(.+)", head, re.IGNORECASE | re.MULTILINE)
        if from_match:
            details["from"] = " ".join(from_match.group(1).split())
    elif _FORM_HEAD.search(head):
        details["docType"] = "form"
    elif re.search(r"^TO\s*:|SUBJECT\s*:|^\d+[.!?]\s", md, re.IGNORECASE | re.MULTILINE):
        details["docType"] = "letter"

    classification = _CLASSIFICATION_RE.search(head)
    if classification:
        details["classification"] = classification.group(1).upper()

    subject_match = re.search(r"SUBJECT\s*:\s*(.+)", md, re.IGNORECASE | re.MULTILINE)
    if subject_match:
        details["subject"] = " ".join(subject_match.group(1).split())[:200]

    details["acronyms"] = detect_acronyms(text)

    date_strings: list[str] = []
    seen_dates: set = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            raw = " ".join(match.group(0).split())
            if raw not in seen_dates:
                seen_dates.add(raw)
                date_strings.append(raw)
        if len(date_strings) >= 30:
            break
    details["dates"] = date_strings
    if date_strings and not details["date"]:
        details["date"] = date_strings[0]

    return details


def canonicalize_place(raw: str) -> str:
    """Canonical form of a place name: drop zip zones, expand abbreviations."""
    s = " ".join(str(raw).split())
    if not s:
        return ""
    s = re.sub(r"\b\s*\d{1,5}(?=,)", "", s)  # city postal zone: "Washington 25, ..."
    s = re.sub(r",?\s*\d{4,5}(?=\s*$)", "", s)  # trailing ZIP
    for pattern, replacement in _PLACE_ABBR:
        s = re.sub(pattern, replacement, s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(",")
    s = re.sub(r"\s*,\s*", ", ", s)
    return s


# ---------------------------------------------------------------------------
# Document-level standardization
# ---------------------------------------------------------------------------

def _find_context(page: dict, raw: str) -> str:
    text = page.get("text") or ""
    match = re.search(re.escape(raw), text, re.IGNORECASE)
    if not match:
        return ""
    window = text[max(0, match.start() - 50) : match.end() + 50]
    return " ".join(window.split())


def build_acronym_glossary(pages: list[dict], page_details: list[dict]) -> dict:
    """First-occurrence glossary: raw acronym -> expansion + context + pages.

    The first expansion suggested anywhere in the document wins (OCR
    consistency); the exact casings and page contexts are accumulated.
    """
    glossary: dict = {}
    order: list[str] = []
    for idx, details in enumerate(page_details):
        if idx >= len(pages):
            break
        page_number = pages[idx].get("pageNumber")
        for item in details.get("acronyms") or []:
            raw = str(item.get("raw") or "").strip()
            if not raw:
                continue
            key = raw.upper()
            entry = glossary.setdefault(
                key,
                {"expansion": None, "firstSeenPage": page_number, "raws": [], "pages": [], "contexts": []},
            )
            if key not in order:
                order.append(key)
            if raw not in entry["raws"]:
                entry["raws"].append(raw)
            if page_number not in entry["pages"]:
                entry["pages"].append(page_number)
            expansion = item.get("expansion")
            if isinstance(expansion, str) and expansion.strip() and not entry["expansion"]:
                if expansion.strip().upper() != raw.upper():
                    entry["expansion"] = " ".join(expansion.split())
            context = item.get("context")
            if isinstance(context, str) and context.strip() and context not in entry["contexts"]:
                entry["contexts"].append(" ".join(context.split()))
            if not entry["contexts"] and idx < len(pages):
                found = _find_context(pages[idx], raw)
                if found:
                    entry["contexts"].append(found)
    return glossary


def build_date_entries(pages: list[dict], page_details: list[dict]) -> list[dict]:
    """Standardized date list: raw -> ISO 8601 + page + context."""
    entries: list[dict] = []
    for idx, details in enumerate(page_details):
        if idx >= len(pages):
            break
        page_number = pages[idx].get("pageNumber")
        for raw in details.get("dates") or []:
            raw = " ".join(str(raw).split())
            if not raw:
                continue
            if any(e == {"raw": raw, "page": page_number} for e in entries):
                continue
            entries.append(
                {
                    "raw": raw,
                    "iso": normalize_date(raw),
                    "page": page_number,
                    "context": _find_context(pages[idx], raw),
                }
            )
    return entries


def build_place_map(pages: list[dict], page_details: list[dict]) -> dict:
    """Canonical place name -> the exact written forms and pages that map to it."""
    out: dict = {}
    for idx, details in enumerate(page_details):
        if idx >= len(pages):
            break
        page_number = pages[idx].get("pageNumber")
        for raw in details.get("places") or []:
            raw = " ".join(str(raw).split())
            if not raw:
                continue
            canonical = canonicalize_place(raw)
            if not canonical:
                continue
            entry = out.setdefault(canonical, {"raws": [], "pages": []})
            if raw not in entry["raws"]:
                entry["raws"].append(raw)
            if page_number not in entry["pages"]:
                entry["pages"].append(page_number)
    return out


def standard_text(text: str, glossary: dict) -> str:
    """Page text with each glossary acronym expanded on its first occurrence.

    Only acronyms with a known expansion are touched; the raw token stays and
    the expansion is appended in parentheses (``AMC (Air Materiel Command)``)
    so both the acronym and the full form are indexed.
    """
    expanded_keys: set = set()
    words: list[str] = []

    def key_of(word: str) -> str:
        return word.strip(".,;:()[]\"'").upper()

    for chunk in re.split(r"(\s+)", text):
        if not chunk.strip() or chunk.isspace():
            words.append(chunk)
            continue
        key = key_of(chunk)
        if (
            key in glossary
            and key not in expanded_keys
            and glossary[key].get("expansion")
        ):
            raw = chunk.strip(".,;:()[]\"'")
            words.append(chunk.replace(raw, f"{raw} ({glossary[key]['expansion']})", 1))
            expanded_keys.add(key)
        else:
            words.append(chunk)
    return "".join(words)


# ---------------------------------------------------------------------------
# LLM extraction (opt-in)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LlmConfig:
    model: str
    base_url: str
    api_key: Optional[str]
    batch: int
    delay: float
    timeout: float = 180.0


def build_llm_config_from_env() -> Optional[LlmConfig]:
    """Read ENRICH_* env vars; always returns a config (LLM usable or not)."""
    base_url = os.getenv("ENRICH_BASE_URL") or "https://foundryforall.services.ai.azure.com/models"
    api_key = os.getenv("ENRICH_API_KEY")
    try:
        batch = int(os.getenv("ENRICH_BATCH", "12"))
    except (TypeError, ValueError):
        batch = 12
    try:
        delay = float(os.getenv("ENRICH_DELAY", "10"))
    except (TypeError, ValueError):
        delay = 10.0
    if batch <= 0:
        batch = 12
    return LlmConfig(
        model=os.getenv("ENRICH_MODEL", "gpt-4.1-nano"),
        base_url=base_url,
        api_key=api_key,
        batch=batch,
        delay=delay,
    )


def llm_usable(cfg: Optional[LlmConfig]) -> bool:
    """A config is usable when it carries an API key or targets AI Foundry
    (in which case authentication comes from Entra DefaultAzureCredential)."""
    if cfg is None:
        return False
    return bool(cfg.api_key) or "services.ai.azure.com" in (cfg.base_url or "")


def _build_headers(cfg: LlmConfig) -> Optional[dict]:
    if cfg.api_key:
        if "services.ai.azure.com" in cfg.base_url:
            return {"api-key": cfg.api_key}
        return {"Authorization": f"Bearer {cfg.api_key}"}
    if "services.ai.azure.com" in cfg.base_url:
        from azure.identity import DefaultAzureCredential

        token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
        return {"Authorization": f"Bearer {token}"}
    return None


def _parse_llm_json(content: str, expect: type = list) -> Optional[Any]:
    """Parse a (possibly markdown-fenced) JSON list or dict, tolerating brackets in strings."""
    cleaned = re.sub(r"```(?:json)?", "", content or "").strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, expect) else None
    except (json.JSONDecodeError, ValueError):
        pass
    start = cleaned.find("{" if expect is dict else "[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(cleaned[start : i + 1])
                    return data if isinstance(data, expect) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _llm_call_document(
    batch: list[dict],
    cfg: LlmConfig,
    url: str,
    headers: dict,
    start_index: int,
) -> Optional[dict]:
    """One extraction call for a whole document (or chunk); returns the
    ``{"pages": [...], "research": {...}}`` dict, None on failure."""
    import httpx

    body_lines = [DOCUMENT_PROMPT, ""]
    for local, page in enumerate(batch, start=start_index + 1):
        text = page.get("text") or page.get("markdown") or ""
        snippet = " ".join(str(text).split())[:6000]
        body_lines.append(f"PAGE {page.get('pageNumber') or local}: {snippet}")
    body_lines.append("")
    body_lines.append("JSON:")

    payload = {
        "model": cfg.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a precise document metadata extractor. Output JSON only."},
            {"role": "user", "content": "\n".join(body_lines)},
        ],
    }

    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=cfg.timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 429 and attempt < max_attempts - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                data = _parse_llm_json(content, dict)
                if data is None or not isinstance(data.get("pages"), list):
                    return None
                data["pages"] = [item for item in data["pages"] if isinstance(item, dict)]
                if not isinstance(data.get("research"), dict):
                    data["research"] = {}
                return data
        except Exception:  # noqa: BLE001
            if attempt < max_attempts - 1:
                time.sleep(3 * (attempt + 1))
                continue
            LOGGER.exception("LLM extraction call failed")
            return None
    return None


def _merge_details(rule_details: dict, llm_details: dict) -> dict:
    """LLM values win over rules, except acronyms are unioned (raw-keyed)."""
    out = dict(rule_details)
    if not isinstance(llm_details, dict):
        return out
    for key, value in llm_details.items():
        if key == "acronyms" and isinstance(value, list):
            llm_items = [item for item in value if isinstance(item, dict) and item.get("raw")]
            llm_by_raw = {str(item["raw"]).upper() for item in llm_items}
            extra = [item for item in rule_details.get("acronyms") or [] if str(item.get("raw", "")).upper() not in llm_by_raw]
            out[key] = llm_items + extra
        elif value not in (None, "", [], {}):
            out[key] = value
    return out


def _align_page_entries(batch: list[dict], entries: list[dict]) -> list[dict]:
    """Match LLM page entries to input pages by pageNumber, else by position."""
    by_number = {e.get("pageNumber"): e for e in entries if isinstance(e.get("pageNumber"), int)}
    out = []
    for i, page in enumerate(batch):
        entry = by_number.get(page.get("pageNumber"))
        if entry is None and i < len(entries) and not by_number:
            entry = entries[i]
        out.append(entry or {})
    return out


def extract_document_details(
    pages: list[dict], cfg: Optional[LlmConfig] = None
) -> tuple[list[dict], list[dict], bool]:
    """Per-page details plus the LLM ``research`` payloads, in ONE call per slice.

    Returns (page_details, research_payloads, llm_used). Slices with more than
    ``cfg.batch`` pages are split into chunks (one call each). Without a usable
    LLM, or if a call fails, rule-based extraction is used so the stage always
    produces output.
    """
    rule_details = [extract_rule_details(page) for page in pages]
    if not llm_usable(cfg) or not pages:
        return rule_details, [], False

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = _build_headers(cfg)
    if headers is None:
        return rule_details, [], False

    merged: list[dict] = []
    payloads: list[dict] = []
    for start in range(0, len(pages), cfg.batch):
        batch = pages[start : start + cfg.batch]
        data = _llm_call_document(batch, cfg, url, headers, start_index=start)
        if data is None:
            LOGGER.warning("LLM extraction failed; falling back to rules for %d page(s)", len(pages))
            return rule_details, [], False
        aligned = _align_page_entries(batch, data["pages"])
        for offset, llm_part in enumerate(aligned):
            merged.append(_merge_details(rule_details[start + offset], llm_part))
        payloads.append(data["research"])
        if start + cfg.batch < len(pages) and cfg.delay > 0:
            time.sleep(cfg.delay)
    return merged, payloads, True


def extract_pages_details(pages: list[dict], cfg: Optional[LlmConfig] = None) -> tuple[list[dict], bool]:
    details, _, llm_used = extract_document_details(pages, cfg)
    return details, llm_used


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _fresh_details(details: dict) -> dict:
    return {
        "docType": details.get("docType", "other") or "other",
        "date": details.get("date", "") or "",
        "from": details.get("from", "") or "",
        "to": details.get("to", "") or "",
        "subject": details.get("subject", "") or "",
        "classification": details.get("classification", "") or "",
        "summary": details.get("summary", "") or "",
        "persons": list(details.get("persons") or []),
        "organizations": list(details.get("organizations") or []),
        "units": list(details.get("units") or []),
        "acronyms": list(details.get("acronyms") or []),
        "dates": list(details.get("dates") or []),
        "places": list(details.get("places") or []),
    }


def enrich_slice_with_details(
    data: dict,
    page_details: list[dict],
    llm_used: bool,
    research_payloads: Optional[list[dict]] = None,
) -> dict:
    """Build the enriched document from a curated slice + per-page details.

    The input dict is deep-copied and the enrichment is added to the copy
    (which is then written back over the same curated file/blob, in place).
    """
    import copy

    pages = data.get("pages") or []
    enriched = copy.deepcopy(data)

    clean_details = [_fresh_details(d) for d in page_details]
    glossary = build_acronym_glossary(pages, clean_details)
    date_entries = build_date_entries(pages, clean_details)
    place_map = build_place_map(pages, clean_details)

    for idx, page in enumerate(enriched.get("pages") or []):
        page["enrichment"] = clean_details[idx] if idx < len(clean_details) else _fresh_details({})
        text = page.get("text")
        if isinstance(text, str) and text:
            page["standardText"] = standard_text(text, glossary)

    document = enriched.get("document")
    if isinstance(document, dict):
        document["enrichedAt"] = datetime.now(timezone.utc).isoformat()  # skip/resume marker

    enriched["normalized"] = {
        "acronyms": glossary,
        "dates": date_entries,
        "places": place_map,
        "standardization": "llm" if llm_used else "rules",
    }
    enriched["research"] = build_research_block(
        pages,
        [rule_page_research(p) for p in pages],
        research_payloads or [],
        date_entries,
        enriched.get("document"),
    )
    return enriched


def enrich_slice(data: dict, cfg: Optional[LlmConfig] = None) -> dict:
    """Extract details + research (LLM or rules) then standardize. One-shot entry."""
    pages = data.get("pages") or []
    page_details, research_payloads, llm_used = extract_document_details(pages, cfg)
    return enrich_slice_with_details(data, page_details, llm_used, research_payloads)


def is_already_enriched(data: dict) -> bool:
    """True when a curated slice already carries in-place enrichment.

    Detected via the ``document.enrichedAt`` marker or per-page
    ``enrichment`` blocks (fallback for files enriched before the marker
    existed). Drives the skip/resume behavior of the enrich stage.
    """
    document = data.get("document")
    if isinstance(document, dict) and document.get("enrichedAt"):
        return True
    for page in data.get("pages") or []:
        if isinstance(page, dict) and page.get("enrichment") is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Local CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich a curated slice (per-communication JSON) in place for search "
            "indexing: per-page detail extraction plus document-level standardization "
            "of acronyms, dates and place names, written back into the same file."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="Curated slice JSON file to enrich.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional directory to write the enriched copy into (same filename). "
        "Default: overwrite the input file in place.",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Use the LLM for detail extraction (ENRICH_MODEL / ENRICH_BASE_URL / ENRICH_API_KEY).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the normalization output without writing any file."
    )
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = _build_parser().parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    cfg = build_llm_config_from_env() if args.llm else None
    enriched = enrich_slice(data, cfg)

    summary = enriched["normalized"]
    print(
        f"Pages: {len(enriched.get('pages') or [])} | "
        f"acronyms: {len(summary['acronyms'])} | "
        f"dates: {len(summary['dates'])} | "
        f"places: {len(summary['places'])} | "
        f"standardization: {summary['standardization']}"
    )
    if args.dry_run:
        print(json.dumps({"normalized": enriched["normalized"], "research": enriched["research"]}, ensure_ascii=False, indent=2))
        return 0

    target = (args.output_dir / args.input.name) if args.output_dir is not None else args.input
    target.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
