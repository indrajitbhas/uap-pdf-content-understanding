"""Clean OCR text of curated pages before slicing, using an LLM only.

The model reads each page, re-joins words interrupted by a hyphen, space or
other stray character ("EN - VELOPE" -> "ENVELOPE") and removes extra spacing
("AF HQ - 33 ( 20 MAY 44 )" -> "AF HQ-33 (20 MAY 44)"). No regular-expression
rules edit the text.

Safety net: the model's answer for a page is accepted only if its digits are
identical to the input and its letters are at least 97% similar (ignoring case),
so it can join words, fix spacing and correct OCR letter errors but can never
change a number or rewrite the text. Before the text goes to the model, double
quotes are normalized to apostrophes and stray control characters are collapsed,
so the JSON round-trip cannot break; the answer is checked against the original
text and quotes are not compared. A rejected or failed page is
retried once; if it still fails, the original text is kept and the page number
is logged. Splits the model was unsure about are saved on the page as
``cleanupNotes``.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from typing import Optional

LOGGER = logging.getLogger(__name__)

MIN_LETTER_SIMILARITY = 0.97

CLEAN_PROMPT = (
    "You are cleaning OCR text from 1940s military documents. Read each page carefully and return the "
    "SAME text with ONLY these fixes:\n"
    "1. Search the whole page for words that are interrupted by a hyphen, a space, a line break or any "
    "other stray character, for example \"EN - VELOPE\", \"COMMUNI- CATION\", \"HEAD.QUARTERS\", "
    "\"AIR MAT ERIEL\". Re-join each one when the joined form is a real word, or a recognizable name or "
    "title in context (\"ENVELOPE\", \"COMMUNICATION\", \"HEADQUARTERS\", \"MATERIEL\").\n"
    "2. Remove extra spaces and spaces around punctuation, parentheses, slashes and colons, and around "
    "hyphens inside identifiers: \"AF HQ - 33 ( 20 MAY 44 )\" -> \"AF HQ-33 (20 MAY 44)\", "
    "\"Form No . 10-514 ( Rev 10 Sep 46 )\" -> \"Form No. 10-514 (Rev 10 Sep 46)\", "
    "\"TSNAD - 2B / ACL : eec\" -> \"TSNAD-2B/ACL: eec\".\n"
    "Keep a hyphen when it is real: compound words (AIR-SEA, RE-ENTRY) and identifiers containing digits "
    "(TSNAD-2B, V-32291-8, 000.9).\n"
    "3. Fix obvious OCR letter mistakes inside a word when the intended word is clear from context, for "
    "example \"contimue\" -> \"continue\", \"extrenely\" -> \"extremely\", \"airforee\" -> \"airforce\".\n"
    "Rules: NEVER change, add, remove or reorder any digit or number, and do not touch footnote markers. "
    "Do not rephrase, add or remove words. Keep line breaks. If you are unsure about a fix, leave the text "
    "as it is and list the exact text in \"uncertain\".\n"
    'Input is a JSON array of {"pageNumber", "text"}. Answer with ONLY a JSON array of '
    '{"pageNumber", "text", "uncertain": [strings]}, same order.'
)


FOUNDRY_HOST = "services.ai.azure.com"
DEFAULT_BASE_URL = "https://foundryforall.services.ai.azure.com/models"


def _parse_llm_json(content: str) -> Optional[list]:
    """Parse a (possibly markdown-fenced) JSON array, tolerating brackets in strings."""
    cleaned = re.sub(r"```(?:json)?", "", content or "").strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, ValueError):
        pass
    start = cleaned.find("[")
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
                    return data if isinstance(data, list) else None
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def llm_usable(base_url: Optional[str], api_key: Optional[str]) -> bool:
    return bool(api_key) or FOUNDRY_HOST in (base_url or "")


def _build_headers(base_url: str, api_key: Optional[str]) -> Optional[dict]:
    if api_key:
        if FOUNDRY_HOST in base_url:
            return {"api-key": api_key}
        return {"Authorization": f"Bearer {api_key}"}
    if FOUNDRY_HOST in base_url:
        from azure.identity import DefaultAzureCredential

        token = DefaultAzureCredential(exclude_managed_identity_credential=True).get_token(
            "https://cognitiveservices.azure.com/.default"
        ).token
        return {"Authorization": f"Bearer {token}"}
    return None


def _alnum_key(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def _letters_key(text: str) -> str:
    return re.sub(r"[\W\d_]+", "", text).lower()


def _digits_key(text: str) -> str:
    return re.sub(r"\D+", "", text)


def is_safe_edit(original: str, cleaned: str) -> bool:
    if _digits_key(original) != _digits_key(cleaned):
        return False
    a, b = _letters_key(original), _letters_key(cleaned)
    if a == b:
        return True
    if not a or not b or abs(len(a) - len(b)) > 0.03 * len(a):
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= MIN_LETTER_SIMILARITY


def sanitize_for_prompt(text: str) -> str:
    """Replace characters that break JSON round-trips before the text goes to the model."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace('"', "'")
    text = re.sub(r"[^\S\n]+", " ", text)
    return text


def _llm_clean_batch(batch: list[dict], model: str, url: str, headers: dict, timeout: float) -> Optional[list]:
    import httpx

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a precise OCR text cleaner. Output JSON only."},
            {
                "role": "user",
                "content": CLEAN_PROMPT
                + "\n\n"
                + json.dumps(
                    [{"pageNumber": p["pageNumber"], "text": sanitize_for_prompt(p["text"])} for p in batch],
                    ensure_ascii=False,
                ),
            },
        ],
    }
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 429 and attempt < max_attempts - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return _parse_llm_json(resp.json()["choices"][0]["message"]["content"])
        except Exception:  # noqa: BLE001
            if attempt < max_attempts - 1:
                time.sleep(3 * (attempt + 1))
                continue
            LOGGER.exception("LLM text-cleanup call failed")
            return None
    return None


def _apply_answer(batch: list[dict], answer: Optional[list]) -> list[dict]:
    """Apply safe answers to ``batch`` in place; return the pages still unresolved."""
    by_number = {a.get("pageNumber"): a for a in (answer or []) if isinstance(a, dict)}
    failed = []
    for page in batch:
        item = by_number.get(page.get("pageNumber"))
        new_text = item.get("text") if item else None
        if not isinstance(new_text, str) or not is_safe_edit(page["text"], new_text):
            failed.append(page)
            continue
        page["text"] = new_text
        uncertain = [u for u in (item.get("uncertain") or []) if isinstance(u, str) and u.strip()]
        if uncertain:
            page["cleanupNotes"] = uncertain
    return failed


def llm_clean_pages(
    pages: list[dict],
    *,
    model: str,
    base_url: str,
    api_key: Optional[str],
    batch_size: int = 10,
    delay: float = 0.0,
    timeout: float = 180.0,
) -> list[dict]:
    """Return copies of ``pages`` with ``text`` cleaned by the LLM."""
    cleaned = [dict(p) for p in pages]

    if not llm_usable(base_url, api_key):
        LOGGER.warning("Text cleanup skipped: LLM not configured; page text left unchanged")
        return cleaned
    headers = _build_headers(base_url, api_key)
    if headers is None:
        return cleaned
    url = base_url.rstrip("/") + "/chat/completions"

    targets = [p for p in cleaned if isinstance(p.get("text"), str) and p["text"].strip()]
    originals = {id(p): p["text"] for p in targets}
    unresolved: list[dict] = []
    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        failed = _apply_answer(batch, _llm_clean_batch(batch, model, url, headers, timeout))
        if failed:
            failed = _apply_answer(failed, _llm_clean_batch(failed, model, url, headers, timeout))
        unresolved.extend(failed)
        if start + batch_size < len(targets) and delay > 0:
            time.sleep(delay)

    for page in unresolved:
        LOGGER.warning("Text cleanup: page %s kept unchanged (LLM failed or altered digits or too many letters)", page.get("pageNumber"))
    changed = sum(1 for p in targets if p["text"] != originals[id(p)])
    LOGGER.info("Text cleanup: LLM adjusted %d of %d page(s)", changed, len(targets))
    return cleaned
