#!/usr/bin/env python3
"""Slice a curated JSON (or all curated JSONs in the results container) into
individual per-communication JSON files after HITL word review.

A "communication" is a set of consecutive pages that belong to the same
letter/document (e.g. basic letter + routing slip + carbons). Pages are grouped
deterministically with cheap heuristics (no LLM required — see the markers
below) and each group is written as its own curated JSON file whose name
carries the page range, e.g.::

    18_100754_ General 1946-7_Vol_2__p01-02.curated.json
    18_100754_ General 1946-7_Vol_2__p03-06.curated.json

The sliced files keep the full curated schema: ``document`` metadata (with a
new ``pageRange`` field) and the exact ``pages`` belonging to the group
(pageNumber untouched, so page references stay stable). A ``sourceCurated``
field records the original parent file.

Usage::

    python split_curated.py --input path/to/doc.curated.json --output-dir out/
    python split_curated.py --input path/to/doc.curated.json --groups "1-2,3,4-6" --output-dir out/
    python split_curated.py --all --dry-run                 # blob mode, uses container env vars

Grouping (no LLM): a page opens a new group when its layout markdown starts
with any of:

* a form header line       (``PageHeader: ... form No. ...``, ``FORM NO. ...``)
* a ROUTING AND RECORD / ACTION SHEET header
* a classification heading (``# SECRET`` / ``## CONFIDENTIAL`` / ``RESTRICTED``)
* an "Nth Indorsement" or ``Basic Ltr fr/fm ...`` citation line
* a fresh date block like  ``DATE:`` at the top of a routing slip
* an envelope / folder cover / index page kind

Otherwise the page continues the previous group. ``--groups`` overrides
everything (a HITL fallback when the heuristics disagree).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

FORM_HEADER = re.compile(r"(?:form|FORM)\s*No\.?\s*[\d-]+", re.IGNORECASE)
ROUTING_HEADER = re.compile(r"ROUTING\s+AND\s+RECORD|ACTION\s+SHEET|ROUTING\s+AND\s+ACTION", re.IGNORECASE)
CLASSIFICATION_HEADING = re.compile(
    r"(?:^|\s)(?:#+\s*)?(?:TOP\s*SECRET|SECRET|CONFIDENTIAL|RESTRICTED|UNCLASSIFIED)\b",
    re.IGNORECASE,
)
INDORSEMENT_OR_CITATION = re.compile(
    r"(?:^|\s)(?:\d+(?:st|nd|rd|th)\s+Ind(?:orsement)?|Basic\s+Ltr\s+fr|Basic\s+Ltr\s+fm|B/L\s+fr|B/L\s+fm)",
    re.IGNORECASE,
)
DATE_BLOCK = re.compile(r"^DATE\s*:\s*$|^DATE\s*:", re.IGNORECASE | re.MULTILINE)
ENVELOPE_KIND = {"envelope", "folder_cover", "book_cover", "index_or_list", "newspaper_clipping"}

NEW_GROUP_START_MARKERS = (
    ("*", r"PageHeader\s*:\s*.*?(?:form|FORM)\s*No\.?\s*[\d-]+"),
    ("*", r"PageHeader\s*:\s*.*?ROUTING\s+AND\s+RECORD"),
    ("*", r"^\s*FORM\s*NO\.?\s*[\d-]+"),
    ("*", r"^\s*ROUTING\s+AND\s+RECORD\s+SHEET"),
    ("!", r"^\s*#\s*(?:TOP\s*SECRET|SECRET|CONFIDENTIAL|RESTRICTED|UNCLASSIFIED)\b.*"),
    ("!", r"(?:^|\s)\d+(?:st|nd|rd|th)\s+Ind(?:orsement|ors|.)?\b"),
    ("!", r"(?:^|\s)Basic\s+Ltr\s+fr(?:om|m)?\b"),
    ("!", r"(?:^|\s)B/L\s+fr(?:om|m)?\b"),
    ("!", r"^\s*\(\d+\)\s*$"),
)


def _is_routing_slip_page(md: str) -> bool:
    """A routing/action slip: DATE: + FROM: block near the top, no letter body."""
    head = "\n".join(md.splitlines()[:10])
    return bool(DATE_BLOCK.search(head) and re.search(r"^FROM\s*:", head, re.MULTILINE | re.IGNORECASE))


def _page_markdown(page: dict) -> str:
    md = page.get("markdown")
    if isinstance(md, str) and md.strip():
        return md
    text = page.get("text")
    return text if isinstance(text, str) else ""


def _looks_like_new_group(page: dict) -> bool:
    kind = str(page.get("kind") or "").lower()
    if kind in ENVELOPE_KIND:
        return True
    if kind == "routing_slip":
        return True

    md = _page_markdown(page)
    if not md:
        return False
    head = "\n".join(md.splitlines()[:8])
    for flag, pat in NEW_GROUP_START_MARKERS:
        if re.search(pat, head, re.IGNORECASE | re.MULTILINE):
            return True

    # A routing slip that directly follows a letter belongs to that letter:
    # only start a new group with a routing slip when it is the very first page.
    if _is_routing_slip_page(md):
        return False
    if CLASSIFICATION_HEADING.search(head) and re.search(r"HEADQUARTERS|AIR MATERIEL|ARMY AIR FORCES", head, re.IGNORECASE):
        return True
    return False


def group_pages(pages: list[dict]) -> list[list[dict]]:
    """Split consecutive pages into communication groups (page order kept)."""
    groups: list[list[dict]] = []
    for idx, page in enumerate(pages):
        if idx == 0:
            groups.append([page])
            continue
        if _looks_like_new_group(page):
            groups.append([page])
        else:
            groups[-1].append(page)
    return groups


def make_slice_name(stem: str, group: list[dict]) -> str:
    """Name a slice file so its page range is identifiable.

    Single page -> ``<stem>__p03.curated.json``; multi -> ``<stem>__p03-06.curated.json``.
    """
    numbers = [int(p.get("pageNumber", 0)) for p in group if isinstance(p, dict)]
    if not numbers:
        raise ValueError("Cannot name a slice without page numbers.")
    page_range = f"p{numbers[0]:02d}"
    if len(numbers) > 1:
        page_range += f"-{numbers[-1]:02d}"
    return f"{stem}__{page_range}.curated.json"


SLICED_NAME_RE = re.compile(r"__p\d+(?:-\d+)?\.curated\.json$", re.IGNORECASE)


def is_already_sliced(name: str) -> bool:
    """True when a blob/file name is itself a slice output (``<stem>__pNN...``)."""
    return bool(SLICED_NAME_RE.search(name))


def slice_curated(
    curated: dict,
    groups: list[list[dict]],
    *,
    stem: str,
    source_file_name: Optional[str] = None,
) -> list[tuple[str, dict]]:
    """Build one (file_name, curated_dict) per group, preserving full schema."""
    document = dict(curated.get("document") or {})
    document["sourceCurated"] = source_file_name or document.get("fileName", "unknown.curated.json")

    slices: list[tuple[str, dict]] = []
    for group in groups:
        name = make_slice_name(stem, group)
        doc_copy = dict(document)
        numbers = [int(p.get("pageNumber", 0)) for p in group if isinstance(p, dict)]
        doc_copy["fileName"] = name
        doc_copy["pageRange"] = {"from": min(numbers), "to": max(numbers)} if numbers else None
        slices.append(
            (
                name,
                {
                    "schema": curated.get("schema", "curated/1"),
                    "document": doc_copy,
                    "pages": list(group),
                },
            )
        )
    return slices


def parse_groups(spec: str) -> list[tuple[int, int]]:
    """Parse '1-2,3,4-6' into [(1,2),(3,3),(4,6)]."""
    ranges: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            n = int(chunk)
            ranges.append((n, n))
            continue
        a, _, b = chunk.partition("-")
        start = int(a)
        end = int(b)
        if end < start:
            start, end = end, start
        ranges.append((start, end))
    return ranges


def apply_groups(pages: list[dict], ranges: list[tuple[int, int]]) -> list[list[dict]]:
    """Build page groups from explicit page-number ranges.

    Raises ValueError when a page number appears in more than one range
    (duplicate) or when the ranges leave any page out.
    """
    by_number = {int(p.get("pageNumber", -1)): p for p in pages if isinstance(p, dict)}
    groups: list[list[dict]] = []
    seen: set = set()
    for start, end in ranges:
        group = []
        for n in range(start, end + 1):
            if n in seen:
                raise ValueError(f"--groups range ({start}, {end}) reuses page {n}")
            page = by_number.get(n)
            if page is not None:
                group.append(page)
                seen.add(n)
        if group:
            groups.append(group)

    grouped_numbers = {int(p.get("pageNumber", -1)) for g in groups for p in g}
    ungrouped = [p for p in pages if int(p.get("pageNumber", -1)) not in grouped_numbers]
    if ungrouped:
        raise ValueError(
            f"--groups ranges {ranges} do not cover all pages; missing: "
            f"{[int(p.get('pageNumber', -1)) for p in ungrouped]}"
        )
    return groups


def load_curated(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_slices(slices: list[tuple[str, dict]], output_dir: Path, dry_run: bool) -> None:
    for name, data in slices:
        target = output_dir / name
        if dry_run:
            print(f"  [dry-run] would write {target.name} ({len(data['pages'])} page(s))")
            continue
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  wrote {target.name} ({len(data['pages'])} page(s))")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Slice a curated JSON into per-communication JSON files after HITL "
            "word review. Pages are grouped with heuristics (no LLM) or with "
            "--groups for explicit page ranges. Output files are named with the "
            "page range, e.g. <stem>__p01-02.curated.json."
        )
    )
    parser.add_argument("--input", type=Path, help="Curated JSON file to slice (local mode). Mutually exclusive with --all.")
    parser.add_argument("--output-dir", type=Path, default=Path("slices"), help="Directory to write slice files into.")
    parser.add_argument(
        "--groups",
        default=None,
        help='Explicit page groups, e.g. "1-2,3,4-6". Overrides the heuristics (HITL fallback).',
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Slice every *.curated.json blob in the results container (blob mode; needs Azure storage env).",
    )
    parser.add_argument(
        "--all-local",
        type=Path,
        default=None,
        help="Slice every *.curated.json file in a local folder (for testing without Azure).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print groups/slices without writing anything.")
    parser.add_argument(
        "--results-container",
        default=None,
        help="Blob storage results container (default: env RESULTS_CONTAINER or 'pdf-results').",
    )
    return parser


def _group_report(pages: list[dict], groups: list[list[dict]]) -> None:
    print("Groups (page ranges):")
    for gi, group in enumerate(groups, start=1):
        numbers = [int(p.get("pageNumber", 0)) for p in group]
        kinds = {str(p.get("kind") or "") for p in group}
        print(f"  {gi:2d}. pages {numbers[0]:>2d}-{numbers[-1]:>2d}  kind={sorted(kinds)}")


def _blob_slice_all(storage_container: Any, results_container: str, output_dir: Path, dry_run: bool) -> None:
    from azure.storage.blob import BlobServiceClient  # lazy import

    conn = _first_env("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        service = BlobServiceClient.from_connection_string(conn)
    else:
        account_url = _first_env("AZURE_STORAGE_ACCOUNT_URL")
        if not account_url:
            raise ValueError("Blob mode needs AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL.")
        from azure.identity import DefaultAzureCredential

        service = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())

    container = service.get_container_client(results_container)
    output_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for blob in container.list_blobs(name_starts_with=(_first_env("RESULTS_PREFIX") or "")):
        if not blob.name.lower().endswith(".curated.json"):
            continue
        if is_already_sliced(blob.name):
            print(f"  [skip] already a slice: {blob.name}")
            continue
        print(f"== {blob.name}")
        curated = json.loads(container.download_blob(blob.name).readall().decode("utf-8"))
        groups = group_pages(curated.get("pages", []))
        _group_report(curated.get("pages", []), groups)
        slices = slice_curated(
            curated,
            groups,
            stem=Path(blob.name).stem.replace(".curated", "") if blob.name.endswith(".curated.json") else Path(blob.name).stem,
            source_file_name=blob.name,
        )
        write_slices(slices, output_dir, dry_run)
        total += len(slices)
    print(f"Total slices: {total}")


def _first_env(*names: str) -> Optional[str]:
    import os

    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def run_local(input_path: Path, output_dir: Path, groups_spec: Optional[str], dry_run: bool) -> int:
    curated = load_curated(input_path)
    pages = curated.get("pages")
    if not isinstance(pages, list) or not pages:
        print(f"No pages in {input_path}", file=sys.stderr)
        return 1

    if groups_spec:
        groups = apply_groups(pages, parse_groups(groups_spec))
    else:
        groups = group_pages(pages)

    _group_report(pages, groups)

    stem = input_path.name
    if stem.lower().endswith(".curated.json"):
        stem = stem[: -len(".curated.json")]
    elif stem.lower().endswith(".json"):
        stem = stem[: -len(".json")]

    slices = slice_curated(curated, groups, stem=stem, source_file_name=input_path.name)
    print(f"\nSlices -> {output_dir}:")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_slices(slices, output_dir, dry_run)
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.all:
        results_container = args.results_container or _first_env("RESULTS_CONTAINER") or "pdf-results"
        _blob_slice_all(None, results_container, args.output_dir, args.dry_run)
        return 0

    if args.all_local:
        base = args.all_local
        for curated_file in sorted(base.glob("*.curated.json")):
            if is_already_sliced(curated_file.name):
                print(f"  [skip] already a slice: {curated_file.name}")
                continue
            print(f"== {curated_file.name}")
            rc = run_local(curated_file, args.output_dir, args.groups, args.dry_run)
            if rc:
                return rc
        return 0

    if args.input is None:
        parser.error("Provide --input (local file) or --all / --all-local.")
    return run_local(args.input, args.output_dir, args.groups, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
