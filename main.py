#!/usr/bin/env python3
"""Blob-driven PDF analyzer built with Microsoft Agent Framework workflows.

The source PDFs already live in an Azure Blob Storage container (``PDFS_CONTAINER``).
This CLI wires four workflow nodes (executors) together with ``WorkflowBuilder``:

1. ``list_pdf_blobs``   - lists the PDF blobs in the source container and fans the
                           ones that still need processing out to the next node.
2. ``analyze_document``  - downloads each PDF's bytes and runs the Azure AI
                           Content Understanding (document intelligence) analyzer.
3. ``save_result``       - saves the resulting analysis JSON to the results
                           container (``pdf-results`` by default).
4. ``user_review``       - scans the saved JSON for words with low confidence
                           (< ``CONFIDENCE_THRESHOLD``, default 0.8), builds a
                           review note referencing the PDF review app
                           (``REVIEW_APP_URL``), and yields it as workflow output.

A sixth stage (``--slice-only``) runs as a separate workflow *after* HITL word
review: ``list_curated_blobs`` scans the results container for ``*.curated.json``
blobs written by the review app and ``slice_curated`` breaks each into
per-communication files (``<stem>__pNN[-NN].curated.json``) saved back to the
cloud. Grouping uses deterministic heuristics by default and can
optionally be delegated to a low-cost LLM (``--slice-llm``).

A seventh stage (``--enrich-only``) runs *after* slicing and *before* indexing:
``list_slices`` fans the per-communication slices out to
``extract_page_details`` (per-page details plus the document-level ``research``
block, LLM or rules; one LLM call per slice), then
``standardize_details`` normalizes acronyms, dates and place names at document
level and assembles the ``research`` block, and ``save_enriched`` writes the enrichment back into the same curated
slice blob (in place, marked with ``document.enrichedAt``).

See: https://learn.microsoft.com/en-gb/agent-framework/get-started/workflows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, handler
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient
from azure.ai.contentunderstanding.aio import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput, AnalysisResult
from dotenv import load_dotenv
from typing_extensions import Never

from split_curated import (
    apply_groups,
    group_pages,
    is_already_sliced,
    parse_groups,
    slice_curated,
)
from enrich_slice import (
    is_already_enriched,
    enrich_slice_with_details,
    extract_document_details,
    build_llm_config_from_env,
)


LOGGER = logging.getLogger("pdf_content_understanding")
PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "change_me",
    "your_key_here",
    "your-value-here",
    "<your_value_here>",
    "<your-key>",
    "<key>",
    "placeholder",
    "none",
    "null",
}


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration for the CLI."""

    endpoint: Optional[str]
    analyzer_id: Optional[str]
    api_version: str
    content_understanding_key: Optional[str]
    storage_connection_string: Optional[str]
    storage_account_url: Optional[str]
    pdfs_container: str
    pdfs_prefix: str
    results_container: str
    results_prefix: str
    concurrency: int
    force: bool
    review_app_url: str
    confidence_threshold: float
    slice_enabled: bool
    slice_groups_override: Optional[str]
    slice_dry_run: bool
    slice_llm_enabled: bool
    slice_auto: bool
    enrich_enabled: bool
    enrich_dry_run: bool
    enrich_auto: bool
    enrich_llm_enabled: bool


@dataclass(frozen=True)
class AnalyzeTask:
    """A single PDF blob that needs to be analyzed."""

    blob_name: str


@dataclass(frozen=True)
class AnalysisOutcome:
    """Result of running the document intelligence analyzer on one blob."""

    blob_name: str
    status: str
    json_text: Optional[str]
    elapsed_seconds: float
    message: str = ""


@dataclass(frozen=True)
class FileOutcome:
    """Per-file result of the analysis/save stage, fed into the review node."""

    blob_name: str
    status: str
    elapsed_seconds: float
    message: str = ""


@dataclass(frozen=True)
class ReviewOutcome:
    """Terminal, per-file review output used for the run summary."""

    blob_name: str
    status: str
    low_confidence_count: int
    review_text: str
    message: str = ""


@dataclass(frozen=True)
class SliceTask:
    """A curated JSON blob that should be sliced into per-communication files."""

    curated_blob_name: str


@dataclass(frozen=True)
class SliceOutcome:
    """Result of slicing one curated JSON blob into per-communication files."""

    blob_name: str
    status: str
    slice_count: int
    message: str = ""


@dataclass(frozen=True)
class EnrichTask:
    """A curated slice blob that should be enriched for indexing."""

    slice_blob_name: str


@dataclass(frozen=True)
class ExtractionTask:
    """Slice JSON plus the per-page details extracted from it."""

    slice_blob_name: str
    json_text: Optional[str]
    page_details: list
    llm_used: bool
    message: str = ""
    research_payloads: tuple = ()


@dataclass(frozen=True)
class EnrichedSlice:
    """An enriched slice ready to be saved next to the curated blob."""

    slice_blob_name: str
    enriched_json_text: Optional[str]
    message: str = ""


@dataclass(frozen=True)
class EnrichOutcome:
    """Terminal, per-slice enrichment output used for the run summary."""

    blob_name: str
    status: str
    message: str = ""


def _normalize_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _is_real_secret(value: Optional[str]) -> bool:
    if value is None:
        return False
    normalized = value.strip()
    if not normalized:
        return False
    return normalized.lower() not in PLACEHOLDER_VALUES


def _result_blob_name(pdf_blob_name: str, prefix: str) -> str:
    """Blob name for the analysis JSON: same relative path, .json extension."""
    stem, _, _ = pdf_blob_name.rpartition(".")
    json_name = f"{stem or pdf_blob_name}.json"
    cleaned_prefix = prefix.strip("/")
    return f"{cleaned_prefix}/{json_name}" if cleaned_prefix else json_name


def _extract_low_confidence_words(
    result_json: dict, threshold: float
) -> list[tuple[int, str, float]]:
    """Return (page_number, text, confidence) for every word below the threshold.

    Handles the shapes the review app supports: bare ``{"pages": [...]}``,
    ``{"kind": "document", "document": {"pages": [...]}}``, and the SDK
    ``as_dict()`` output where pages live under ``contents[*].pages``.
    """

    low_words: list[tuple[int, str, float]] = []

    def walk(obj: object, current_page: int) -> None:
        if isinstance(obj, dict):
            page_number = obj.get("pageNumber") or obj.get("page_number")
            if isinstance(page_number, int):
                current_page = page_number

            words = obj.get("words")
            if isinstance(words, list):
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    confidence = word.get("confidence")
                    if not isinstance(confidence, (int, float)):
                        continue
                    if confidence >= threshold:
                        continue
                    text = word.get("content") or word.get("text") or ""
                    low_words.append((current_page, str(text), float(confidence)))

            for value in obj.values():
                walk(value, current_page)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, current_page)

    walk(result_json, 0)
    low_words.sort(key=lambda item: (item[0], item[2]))
    return low_words


def _build_review_text(
    blob_name: str,
    low_words: list[tuple[int, str, float]],
    threshold: float,
    review_app_url: str,
) -> str:
    """Build the human-readable review note for one file."""
    lines = [
        f"Review requested for: {blob_name}",
        f"Open in the review app: {review_app_url}",
        f"{len(low_words)} word(s) with confidence < {threshold}:",
    ]
    for page_number, text, confidence in low_words[:100]:
        snippet = repr(text[:60]) if text else "<empty>"
        lines.append(f"  page {page_number}: {snippet} (confidence {confidence:.3f})")
    if len(low_words) > 100:
        lines.append(f"  ... and {len(low_words) - 100} more")
    return "\n".join(lines)


def _llm_group_pages_batch(
    pages: list[dict],
    *,
    model: str,
    base_url: str,
    headers: dict,
    page_index_offset: int = 0,
) -> Optional[list]:
    """Ask the LLM to group one batch of pages into page ranges.

    Pages are renumbered 1..N within the batch (the model sees "PAGE 1,2,...").
    Returns a list of [start, end] ranges in batch numbering, or None on failure.
    Includes retry/backoff on 429 (rate limit) so transient throttling does not
    hard-fall-back to heuristics.
    """
    import httpx
    import time as _time

    body_lines = [
        "You segment pages of a structured military document into independent communications.",
        "Each page is given as 'PAGE n' followed by its markdown (first 600 chars).",
        "A new communication usually starts with a form header, ROUTING AND RECORD SHEET, a classification heading,",
        "an indorsement, a fresh DATE:/FROM: block, or an envelope/folder-cover page.",
        "Return ONLY a JSON array of page ranges, e.g. [[1,2],[3],[4,6]] using every page from 1 to the last exactly once.",
        "",
    ]
    for local, page in enumerate(pages, start=1):
        md = page.get("markdown") or page.get("text") or ""
        snippet = " ".join(str(md).split())[:600]
        body_lines.append(f"PAGE {local}: {snippet}")
    body_lines.append("")
    body_lines.append("Ranges:")

    url = f"{base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a precise document segmentation engine. Output JSON only."},
            {"role": "user", "content": "\n".join(body_lines)},
        ],
    }

    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=180) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 429 and attempt < max_attempts - 1:
                    _time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                # Strip markdown fences and ALL whitespace so compact and
                # pretty-printed JSON (spaces/newlines/indentation) both parse.
                compact = re.sub(r"```(?:json)?", "", content).strip()
                compact = re.sub(r"\s+", "", compact)
                match = re.search(r"\[\[.*\]\]", compact, re.DOTALL)
                if not match:
                    return None
                ranges = json.loads(match.group(0))
                if not isinstance(ranges, list):
                    return None
                return ranges
        except Exception:  # noqa: BLE001
            if attempt < max_attempts - 1:
                _time.sleep(3 * (attempt + 1))
                continue
            LOGGER.exception("LLM grouping call failed; falling back to heuristics")
            return None
    return None


def _llm_group_spec(pages: list[dict], model: str, base_url: Optional[str], api_key: Optional[str]) -> Optional[str]:
    """Ask a small LLM to group consecutive pages into communications.

    Returns a ``"1-2,3,4-6"`` group spec, or None when the LLM is not
    configured / fails (caller falls back to the deterministic heuristics).
    Uses the OpenAI-compatible chat-completions API so any OpenAI-compatible
    endpoint works (OpenAI, Azure OpenAI, Azure AI Foundry, RouteLLM, ...).

    For Azure AI Foundry model endpoints (``*.services.ai.azure.com/models``)
    authentication is via Entra (``DefaultAzureCredential``, reusing the az CLI
    login) when ``LLM_GROUPING_API_KEY`` is unset. Pages are grouped in small
    batches (``LLM_GROUPING_BATCH`` pages per call, default 10) so the per-call
    input stays under `gpt-4.1-nano`'s per-minute token cap on serverless
    deployments; each call carries retry/backoff on 429.
    """
    if not api_key and not (base_url and "services.ai.azure.com" in base_url):
        return None

    try:
        import httpx  # noqa: F401
    except ImportError:  # pragma: no cover
        return None

    url = f"{base_url.rstrip('/')}/chat/completions" if base_url else None
    is_azure_foundry = bool(base_url and "services.ai.azure.com" in base_url)

    if api_key:
        headers = {"Authorization": f"Bearer {api_key}"} if not is_azure_foundry else {"api-key": api_key}
    else:
        from azure.identity import DefaultAzureCredential

        token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
        headers = {"Authorization": f"Bearer {token}"}

    if not url:
        url = "https://api.openai.com/v1/chat/completions"
    if is_azure_foundry:
        api_version = os.getenv("LLM_GROUPING_API_VERSION")
        if api_version:
            url += f"?api-version={api_version}"

    try:
        group_batch = int(os.getenv("LLM_GROUPING_BATCH", "10"))
    except (TypeError, ValueError):
        group_batch = 10
    if group_batch <= 0:
        group_batch = 10

    import time as _time
    delay = float(os.getenv("LLM_GROUPING_DELAY", "75"))
    try:
        batches = [pages[i : i + group_batch] for i in range(0, len(pages), group_batch)]
        all_parts: list[str] = []
        offset = 0
        for bi, batch in enumerate(batches):
            ranges = _llm_group_pages_batch(
                batch, model=model, base_url=base_url.rstrip("/"), headers=headers, page_index_offset=offset
            )
            if not ranges:
                return None
            parts: list[str] = []
            for item in ranges:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    a, b = item
                    a = int(a) + offset
                    b = int(b) + offset
                    parts.append(f"{a}-{b}" if a != b else str(a))
                elif isinstance(item, (list, tuple)) and len(item) == 1:
                    parts.append(str(int(item[0]) + offset))
                else:
                    return None
            all_parts.extend(parts)
            offset += len(batch)
            if bi < len(batches) - 1 and delay > 0:
                _time.sleep(delay)
        return ",".join(all_parts)
    except Exception:  # noqa: BLE001
        LOGGER.exception("LLM grouping returned unusable spec; falling back to heuristics")
        return None


class ListPdfBlobsExecutor(Executor):
    """Start node: lists PDF blobs in the source container and fans out work."""

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config
        self.skipped_count = 0
        self.discovered_count = 0

    @handler
    async def list_blobs(self, _trigger: str, ctx: WorkflowContext[AnalyzeTask]) -> None:
        pdfs_container = self._blob_service_client.get_container_client(self._config.pdfs_container)
        results_container = self._blob_service_client.get_container_client(self._config.results_container)

        async for blob in pdfs_container.list_blobs(name_starts_with=self._config.pdfs_prefix or None):
            if not blob.name.lower().endswith(".pdf"):
                continue
            self.discovered_count += 1

            if not self._config.force:
                result_blob_name = _result_blob_name(blob.name, self._config.results_prefix)
                if await results_container.get_blob_client(result_blob_name).exists():
                    self.skipped_count += 1
                    LOGGER.info("Skipping (result blob exists): %s", blob.name)
                    continue

            await ctx.send_message(AnalyzeTask(blob_name=blob.name))

        LOGGER.info(
            "Discovered %d PDF blob(s) under container=%s prefix=%r",
            self.discovered_count,
            self._config.pdfs_container,
            self._config.pdfs_prefix,
        )


class AnalyzeDocumentExecutor(Executor):
    """Downloads a PDF blob and runs the document intelligence analyzer."""

    def __init__(
        self,
        id: str,
        blob_service_client: BlobServiceClient,
        cu_client: ContentUnderstandingClient,
        config: AppConfig,
    ):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._cu_client = cu_client
        self._config = config
        self._semaphore = asyncio.Semaphore(config.concurrency)

    @handler
    async def analyze(self, task: AnalyzeTask, ctx: WorkflowContext[AnalysisOutcome]) -> None:
        start = time.perf_counter()
        async with self._semaphore:
            try:
                pdfs_container = self._blob_service_client.get_container_client(self._config.pdfs_container)
                downloader = await pdfs_container.download_blob(task.blob_name)
                pdf_bytes = await downloader.readall()

                poller = await self._cu_client.begin_analyze(
                    analyzer_id=self._config.analyzer_id,
                    inputs=[
                        AnalysisInput(
                            data=pdf_bytes,
                            name=task.blob_name,
                            mime_type="application/pdf",
                        )
                    ],
                )
                result: AnalysisResult = await poller.result()
                json_text = json.dumps(result.as_dict(), indent=2)

                elapsed = time.perf_counter() - start
                LOGGER.info("Analyzed: %s (%.2fs)", task.blob_name, elapsed)
                await ctx.send_message(
                    AnalysisOutcome(
                        blob_name=task.blob_name,
                        status="analyzed",
                        json_text=json_text,
                        elapsed_seconds=elapsed,
                    )
                )
            except AzureError as exc:
                elapsed = time.perf_counter() - start
                message = getattr(exc, "message", str(exc)) or str(exc)
                LOGGER.error("AzureError for %s: %s", task.blob_name, message)
                await ctx.send_message(
                    AnalysisOutcome(
                        blob_name=task.blob_name,
                        status="failed",
                        json_text=None,
                        elapsed_seconds=elapsed,
                        message=message,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                elapsed = time.perf_counter() - start
                LOGGER.exception("Unexpected error analyzing %s", task.blob_name)
                await ctx.send_message(
                    AnalysisOutcome(
                        blob_name=task.blob_name,
                        status="failed",
                        json_text=None,
                        elapsed_seconds=elapsed,
                        message=str(exc),
                    )
                )


class SaveResultExecutor(Executor):
    """Saves the analysis JSON to the results container, then fans out review."""

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config

    @handler
    async def save(self, outcome: AnalysisOutcome, ctx: WorkflowContext[FileOutcome]) -> None:
        if outcome.status != "analyzed" or outcome.json_text is None:
            await ctx.send_message(
                FileOutcome(
                    blob_name=outcome.blob_name,
                    status="failed",
                    elapsed_seconds=outcome.elapsed_seconds,
                    message=outcome.message,
                )
            )
            return

        try:
            result_blob_name = _result_blob_name(outcome.blob_name, self._config.results_prefix)
            results_container = self._blob_service_client.get_container_client(self._config.results_container)
            await results_container.upload_blob(
                name=result_blob_name,
                data=outcome.json_text.encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
            )
            LOGGER.info("Saved result: %s -> %s/%s", outcome.blob_name, self._config.results_container, result_blob_name)
            await ctx.send_message(
                FileOutcome(blob_name=outcome.blob_name, status="analyzed", elapsed_seconds=outcome.elapsed_seconds)
            )
        except AzureError as exc:
            message = getattr(exc, "message", str(exc)) or str(exc)
            LOGGER.error("Failed to save result for %s: %s", outcome.blob_name, message)
            await ctx.send_message(
                FileOutcome(
                    blob_name=outcome.blob_name,
                    status="failed",
                    elapsed_seconds=outcome.elapsed_seconds,
                    message=message,
                )
            )


class UserReviewExecutor(Executor):
    """Terminal node: flags words with low confidence (< threshold) for review.

    Re-reads the analysis JSON from the results container and asks the user to
    review the low-confidence words using the review app (``REVIEW_APP_URL``).
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config

    @handler
    async def review(self, outcome: FileOutcome, ctx: WorkflowContext[Never, ReviewOutcome]) -> None:
        if outcome.status != "analyzed":
            await ctx.yield_output(
                ReviewOutcome(
                    blob_name=outcome.blob_name,
                    status="failed",
                    low_confidence_count=0,
                    review_text="",
                    message=outcome.message,
                )
            )
            return

        try:
            result_blob_name = _result_blob_name(outcome.blob_name, self._config.results_prefix)
            results_container = self._blob_service_client.get_container_client(self._config.results_container)
            downloader = await results_container.download_blob(result_blob_name)
            result_json = json.loads((await downloader.readall()).decode("utf-8"))

            low_words = _extract_low_confidence_words(result_json, self._config.confidence_threshold)
            if not low_words:
                LOGGER.info("No low-confidence words in: %s", outcome.blob_name)
                await ctx.yield_output(
                    ReviewOutcome(
                        blob_name=outcome.blob_name,
                        status="ok",
                        low_confidence_count=0,
                        review_text=(
                            f"No words below confidence threshold {self._config.confidence_threshold} "
                            f"in {outcome.blob_name}."
                        ),
                    )
                )
                return

            LOGGER.info(
                "Review needed for %s: %d word(s) below confidence %s",
                outcome.blob_name,
                len(low_words),
                self._config.confidence_threshold,
            )
            await ctx.yield_output(
                ReviewOutcome(
                    blob_name=outcome.blob_name,
                    status="needs_review",
                    low_confidence_count=len(low_words),
                    review_text=_build_review_text(
                        outcome.blob_name,
                        low_words,
                        self._config.confidence_threshold,
                        self._config.review_app_url,
                    ),
                )
            )
        except (AzureError, json.JSONDecodeError) as exc:
            message = getattr(exc, "message", str(exc)) or str(exc)
            LOGGER.error("Failed to review %s: %s", outcome.blob_name, message)
            await ctx.yield_output(
                ReviewOutcome(
                    blob_name=outcome.blob_name,
                    status="failed",
                    low_confidence_count=0,
                    review_text="",
                    message=message,
                )
            )


class ListCuratedBlobsExecutor(Executor):
    """Start node for the slice stage: fans out curated JSON blobs to slice.

    Scans the results container for ``*.curated.json`` blobs (written by the
    review app after HITL) that are not already per-communication slices
    (names matching ``<stem>__pNN...curated.json`` are skipped).
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config
        self.discovered_count = 0
        self.skipped_count = 0

    @handler
    async def list_curated(self, _trigger: str, ctx: WorkflowContext[SliceTask]) -> None:
        results_container = self._blob_service_client.get_container_client(self._config.results_container)
        async for blob in results_container.list_blobs(name_starts_with=self._config.results_prefix or None):
            name = blob.name
            if not name.lower().endswith(".curated.json"):
                continue
            if is_already_sliced(name) or ".bak" in name.lower():
                self.skipped_count += 1
                LOGGER.info("Skipping (already a slice or backup): %s", name)
                continue
            self.discovered_count += 1
            await ctx.send_message(SliceTask(curated_blob_name=name))

        LOGGER.info(
            "Slice stage: discovered %d curated blob(s), skipped %d",
            self.discovered_count,
            self.skipped_count,
        )


class SliceCuratedExecutor(Executor):
    """Slices a curated JSON into per-communication files and saves each to the
    results container (cloud), named with its page range.

    Groups consecutive pages via deterministic heuristics (see ``split_curated.py``)
    or optionally via a low-cost LLM (e.g. ``gpt-4.1-nano``) when enabled.
    Each group becomes ``<stem>__pNN[-NN].curated.json`` in the same virtual
    folder as the original curated blob.
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config

    @handler
    async def slice_and_save(self, task: SliceTask, ctx: WorkflowContext[Never, SliceOutcome]) -> None:
        results_container = self._blob_service_client.get_container_client(self._config.results_container)
        try:
            downloader = await results_container.download_blob(task.curated_blob_name)
            curated = json.loads((await downloader.readall()).decode("utf-8"))
            pages = curated.get("pages")
            if not isinstance(pages, list) or not pages:
                await ctx.yield_output(
                    SliceOutcome(
                        blob_name=task.curated_blob_name,
                        status="skipped",
                        slice_count=0,
                        message="No pages in curated JSON.",
                    )
                )
                return

            # Grouping: explicit override > LLM > deterministic heuristics.
            groups: list[list[dict]]
            if self._config.slice_groups_override:
                groups = apply_groups(pages, parse_groups(self._config.slice_groups_override))
            elif self._config.slice_llm_enabled:
                spec = _llm_group_spec(
                    pages,
                    model=os.getenv("LLM_GROUPING_MODEL", "gpt-4.1-nano"),
                    base_url=os.getenv("LLM_GROUPING_BASE_URL") or "https://api.openai.com/v1",
                    api_key=os.getenv("LLM_GROUPING_API_KEY"),
                )
                if spec:
                    LOGGER.info("LLM grouping for %s: %s", task.curated_blob_name, spec)
                    try:
                        groups = apply_groups(pages, parse_groups(spec))
                    except ValueError:
                        LOGGER.warning("LLM group spec invalid (%s); falling back to heuristics", spec)
                        groups = group_pages(pages)
                else:
                    groups = group_pages(pages)
            else:
                groups = group_pages(pages)

            slash = task.curated_blob_name.rfind("/")
            folder = task.curated_blob_name[: slash + 1] if slash >= 0 else ""
            bare = task.curated_blob_name[slash + 1:] if slash >= 0 else task.curated_blob_name
            stem = bare[: -len(".curated.json")] if bare.lower().endswith(".curated.json") else bare

            slices = slice_curated(
                curated,
                groups,
                stem=stem,
                source_file_name=task.curated_blob_name,
            )

            if self._config.slice_dry_run:
                LOGGER.info("DRY RUN: would write %d slice(s) for %s", len(slices), task.curated_blob_name)
            else:
                for name, data in slices:
                    target = f"{folder}{name}"
                    await results_container.upload_blob(
                        name=target,
                        data=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                        overwrite=True,
                        content_settings=ContentSettings(content_type="application/json"),
                    )
                    LOGGER.info("Saved slice: %s/%s -> %s", self._config.results_container, target, len(data["pages"]))

            await ctx.yield_output(
                SliceOutcome(
                    blob_name=task.curated_blob_name,
                    status="ok",
                    slice_count=len(slices),
                )
            )
        except (AzureError, json.JSONDecodeError) as exc:
            message = getattr(exc, "message", str(exc)) or str(exc)
            LOGGER.error("Failed to slice %s: %s", task.curated_blob_name, message)
            await ctx.yield_output(
                SliceOutcome(
                    blob_name=task.curated_blob_name,
                    status="failed",
                    slice_count=0,
                    message=message,
                )
            )


class ListSlicesExecutor(Executor):
    """Start node for the enrich stage: fans out curated slices to enrich.

    Scans the results container for per-communication slices
    (``<stem>__pNN[-NN].curated.json``) and skips the ones that already carry
    in-place enrichment (``document.enrichedAt`` marker) unless ``--force``.
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config
        self.discovered_count = 0
        self.skipped_count = 0

    @handler
    async def list_slices(self, _trigger: str, ctx: WorkflowContext[EnrichTask]) -> None:
        results_container = self._blob_service_client.get_container_client(self._config.results_container)
        async for blob in results_container.list_blobs(name_starts_with=self._config.results_prefix or None):
            name = blob.name
            if not name.lower().endswith(".curated.json"):
                continue
            if not is_already_sliced(name) or ".bak" in name.lower():
                continue
            if not self._config.force:
                already_enriched = False
                try:
                    downloader = await results_container.download_blob(name)
                    data = json.loads((await downloader.readall()).decode("utf-8"))
                    already_enriched = is_already_enriched(data)
                except (AzureError, json.JSONDecodeError) as exc:
                    LOGGER.error(
                        "Enrich stage: cannot read %s (%s); will (re)enrich",
                        name,
                        getattr(exc, "message", str(exc)) or str(exc),
                    )
                if already_enriched:
                    self.skipped_count += 1
                    LOGGER.info("Enrich stage: skipping (already enriched): %s", name)
                    continue
            self.discovered_count += 1
            await ctx.send_message(EnrichTask(slice_blob_name=name))

        LOGGER.info(
            "Enrich stage: discovered %d slice(s), skipped %d",
            self.discovered_count,
            self.skipped_count,
        )


class ExtractDetailsExecutor(Executor):
    """First enrichment node: per-page detail extraction.

    Downloads a curated slice and extracts suitable details from each page
    (docType, date, from/to, subject, classification, persons, organizations,
    units, acronyms, dates, places, summary) — via the LLM (batched, when
    enabled and configured) or deterministic rule-based heuristics. A message
    carrying the slice JSON plus one detail dict per page is sent downstream.
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._llm_config = build_llm_config_from_env()

    @handler
    async def extract_details(self, task: EnrichTask, ctx: WorkflowContext[ExtractionTask]) -> None:
        async with self._semaphore:
            try:
                results_container = self._blob_service_client.get_container_client(self._config.results_container)
                downloader = await results_container.download_blob(task.slice_blob_name)
                json_text = (await downloader.readall()).decode("utf-8")
                data = json.loads(json_text)
                pages = data.get("pages") or []

                cfg = self._llm_config if self._config.enrich_llm_enabled else None
                page_details, research_payloads, llm_used = extract_document_details(pages, cfg)
                LOGGER.info(
                    "Enrich: extracted details for %s (%d page(s), %s)",
                    task.slice_blob_name,
                    len(pages),
                    "llm" if llm_used else "rules",
                )
                await ctx.send_message(
                    ExtractionTask(
                        slice_blob_name=task.slice_blob_name,
                        json_text=json_text,
                        page_details=page_details,
                        llm_used=llm_used,
                        research_payloads=tuple(research_payloads),
                    )
                )
            except (AzureError, json.JSONDecodeError) as exc:
                message = getattr(exc, "message", str(exc)) or str(exc)
                LOGGER.error("Failed to extract details for %s: %s", task.slice_blob_name, message)
                await ctx.send_message(
                    ExtractionTask(
                        slice_blob_name=task.slice_blob_name,
                        json_text=None,
                        page_details=[],
                        llm_used=False,
                        message=message,
                    )
                )


class StandardizeDetailsExecutor(Executor):
    """Second enrichment node: document-level standardization.

    Builds the first-occurrence acronym glossary, normalizes dates to ISO 8601
    and maps place names to canonical forms, then adds a per-page
    ``enrichment`` block and ``standardText`` (acronyms expanded on first
    occurrence) to a copy of the curated slice.
    """

    def __init__(self, id: str, config: AppConfig):
        super().__init__(id=id)
        self._config = config

    @handler
    async def standardize(self, task: ExtractionTask, ctx: WorkflowContext[EnrichedSlice]) -> None:
        if task.json_text is None:
            await ctx.send_message(
                EnrichedSlice(
                    slice_blob_name=task.slice_blob_name,
                    enriched_json_text=None,
                    message=task.message,
                )
            )
            return
        try:
            data = json.loads(task.json_text)
            enriched = enrich_slice_with_details(
                data, task.page_details, task.llm_used, list(task.research_payloads)
            )
            await ctx.send_message(
                EnrichedSlice(
                    slice_blob_name=task.slice_blob_name,
                    enriched_json_text=json.dumps(enriched, ensure_ascii=False, indent=2),
                )
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed to standardize %s", task.slice_blob_name)
            await ctx.send_message(
                EnrichedSlice(
                    slice_blob_name=task.slice_blob_name,
                    enriched_json_text=None,
                    message=str(exc),
                )
            )


class SaveEnrichedExecutor(Executor):
    """Terminal enrichment node: writes the enrichment back into the slice.

    The enriched JSON (per-page ``enrichment``/``standardText`` plus the
    document-level ``normalized`` block) is uploaded **in place**, overwriting
    the same curated slice blob (or simply logged, with ``--enrich-dry-run``).
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config

    @handler
    async def save_enriched(self, task: EnrichedSlice, ctx: WorkflowContext[Never, EnrichOutcome]) -> None:
        if task.enriched_json_text is None:
            await ctx.yield_output(
                EnrichOutcome(
                    blob_name=task.slice_blob_name,
                    status="failed",
                    message=task.message,
                )
            )
            return

        try:
            if self._config.enrich_dry_run:
                LOGGER.info("Enrich DRY RUN: would write %s", task.slice_blob_name)
            else:
                results_container = self._blob_service_client.get_container_client(self._config.results_container)
                await results_container.upload_blob(
                    name=task.slice_blob_name,
                    data=task.enriched_json_text.encode("utf-8"),
                    overwrite=True,
                    content_settings=ContentSettings(content_type="application/json"),
                )
                LOGGER.info(
                    "Saved enriched (in place): %s/%s",
                    self._config.results_container,
                    task.slice_blob_name,
                )
            await ctx.yield_output(
                EnrichOutcome(
                    blob_name=task.slice_blob_name,
                    status="ok",
                )
            )
        except AzureError as exc:
            message = getattr(exc, "message", str(exc)) or str(exc)
            LOGGER.error("Failed to save enriched for %s: %s", task.slice_blob_name, message)
            await ctx.yield_output(
                EnrichOutcome(
                    blob_name=task.slice_blob_name,
                    status="failed",
                    message=message,
                )
            )


def build_workflow(
    blob_service_client: BlobServiceClient,
    cu_client: ContentUnderstandingClient,
    config: AppConfig,
) -> tuple[Workflow, ListPdfBlobsExecutor]:
    """Build a fresh workflow graph: list -> analyze -> save -> user_review."""
    list_pdf_blobs = ListPdfBlobsExecutor("list_pdf_blobs", blob_service_client, config)
    analyze_document = AnalyzeDocumentExecutor("analyze_document", blob_service_client, cu_client, config)
    save_result = SaveResultExecutor("save_result", blob_service_client, config)
    user_review = UserReviewExecutor("user_review", blob_service_client, config)

    workflow = (
        WorkflowBuilder(start_executor=list_pdf_blobs)
        .add_edge(list_pdf_blobs, analyze_document)
        .add_edge(analyze_document, save_result)
        .add_edge(save_result, user_review)
        .build()
    )
    return workflow, list_pdf_blobs


def build_slice_workflow(
    blob_service_client: BlobServiceClient,
    config: AppConfig,
) -> tuple[Workflow, ListCuratedBlobsExecutor]:
    """Build a workflow for the post-HITL slice stage: curated JSONs -> slices.

    Graph: list_curated_blobs -> slice_curated (each slice saved to the cloud).
    """
    list_curated = ListCuratedBlobsExecutor("list_curated_blobs", blob_service_client, config)
    slice_curated_node = SliceCuratedExecutor("slice_curated", blob_service_client, config)

    workflow = (
        WorkflowBuilder(start_executor=list_curated)
        .add_edge(list_curated, slice_curated_node)
        .build()
    )
    return workflow, list_curated


def build_enrich_workflow(
    blob_service_client: BlobServiceClient,
    config: AppConfig,
) -> tuple[Workflow, ListSlicesExecutor]:
    """Build the post-slice enrichment workflow used before indexing.

    Graph: list_slices -> extract_page_details -> standardize_details ->
    save_enriched. Extraction is per page (LLM or rules), standardization is
    document-level (acronyms, dates, place names); the enriched slice is
    written back over the same curated blob, in place.
    """
    list_slices = ListSlicesExecutor("list_slices", blob_service_client, config)
    extract_details = ExtractDetailsExecutor("extract_page_details", blob_service_client, config)
    standardize_details = StandardizeDetailsExecutor("standardize_details", config)
    save_enriched = SaveEnrichedExecutor("save_enriched", blob_service_client, config)

    workflow = (
        WorkflowBuilder(start_executor=list_slices)
        .add_edge(list_slices, extract_details)
        .add_edge(extract_details, standardize_details)
        .add_edge(standardize_details, save_enriched)
        .build()
    )
    return workflow, list_slices


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze PDFs already stored in Azure Blob Storage with Azure AI Content "
            "Understanding (document intelligence) using an Agent Framework workflow, "
            "and save the analysis JSON to the results container."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--pdfs-container",
        default=None,
        help="Container holding the source PDFs. Falls back to PDFS_CONTAINER.",
    )
    parser.add_argument(
        "--pdfs-prefix",
        default=None,
        help="Optional virtual-folder prefix to scan within the PDFs container.",
    )
    parser.add_argument(
        "--results-container",
        default=None,
        help="Container to save analysis JSON into. Falls back to RESULTS_CONTAINER (default: pdf-results).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze PDFs even when a result blob already exists.",
    )
    parser.add_argument("--concurrency", type=int, default=None, help="Maximum number of PDFs analyzed concurrently.")
    parser.add_argument("--analyzer-id", default=None, help="Azure Content Understanding analyzer ID.")
    parser.add_argument("--api-version", default=None, help="Azure Content Understanding API version.")
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Azure Content Understanding endpoint (https://...services.ai.azure.com/).",
    )
    parser.add_argument(
        "--review-app-url",
        default=None,
        help="URL of the PDF review app the user opens to review low-confidence words.",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=None,
        help="Words with confidence below this value are flagged for review (default 0.8).",
    )
    parser.add_argument(
        "--slice-only",
        action="store_true",
        help="Run only the post-HITL slice stage: slice every existing *.curated.json "
        "blob into per-communication files and save them to the cloud.",
    )
    parser.add_argument(
        "--slice-groups",
        default=None,
        help='Explicit page groups for slicing, e.g. "1-2,3,4-6" (HITL override).',
    )
    parser.add_argument(
        "--slice-dry-run",
        action="store_true",
        help="In slice mode: compute groups/slices without uploading anything.",
    )
    parser.add_argument(
        "--slice-llm",
        action="store_true",
        help="In slice mode: use a low-cost LLM (default gpt-4.1-nano) for page grouping "
        "instead of deterministic heuristics. Requires LLM_GROUPING_API_KEY "
        "(OpenAI-compatible).",
    )
    parser.add_argument(
        "--slice-auto",
        action="store_true",
        help="After the analyze/review stage, also run the slice stage in the same "
        "invocation for curated JSONs that already exist (files uploaded to the "
        "review app previously). Files that still need review this run are left "
        "for the --slice-only run after the human reviews them.",
    )
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="Run only the enrichment stage: enrich every existing per-communication "
        "slice (<stem>__pNN[-NN].curated.json) in place, adding per-page "
        "enrichment/standardText and document-level normalized details "
        "(acronyms, dates, places). Already-enriched slices are skipped "
        "unless --force.",
    )
    parser.add_argument(
        "--enrich-dry-run",
        action="store_true",
        help="In enrich mode: compute enrichment without uploading anything.",
    )
    parser.add_argument(
        "--enrich-llm",
        action="store_true",
        help="In enrich mode: use the LLM (ENRICH_MODEL, default gpt-4.1-nano) for "
        "per-page details and the research block (one call per slice) instead of "
        "deterministic heuristics. Requires "
        "ENRICH_API_KEY (OpenAI-compatible) or an AI Foundry endpoint "
        "(DefaultAzureCredential).",
    )
    parser.add_argument(
        "--enrich-auto",
        action="store_true",
        help="After the slice stage, also run the enrichment stage in the same "
        "invocation for the slices that exist (--slice-only --enrich-auto, or with "
        "--slice-auto after analyze).",
    )
    return parser


def _build_config(args: argparse.Namespace) -> AppConfig:
    endpoint = args.endpoint or os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")
    analyzer_id = args.analyzer_id or os.getenv("ANALYZER_ID")
    api_version = args.api_version or os.getenv("API_VERSION", "2025-11-01")

    pdfs_container = args.pdfs_container or os.getenv("PDFS_CONTAINER")
    if not pdfs_container:
        raise ValueError("Missing PDFS_CONTAINER in environment/.env (or pass --pdfs-container).")

    results_container = args.results_container or os.getenv("RESULTS_CONTAINER", "pdf-results")

    concurrency = args.concurrency if args.concurrency is not None else int(os.getenv("WORKERS", "4"))
    if concurrency <= 0:
        raise ValueError("--concurrency must be >= 1")

    review_app_url = args.review_app_url or os.getenv("REVIEW_APP_URL", "https://uap-pdf-review.azurewebsites.net/")

    confidence_threshold = (
        args.confidence_threshold
        if args.confidence_threshold is not None
        else float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))
    )
    if not 0 < confidence_threshold <= 1:
        raise ValueError("--confidence-threshold must be in (0, 1]")

    return AppConfig(
        endpoint=endpoint,
        analyzer_id=analyzer_id,
        api_version=api_version,
        content_understanding_key=_normalize_env(os.getenv("CONTENT_UNDERSTANDING_KEY")),
        storage_connection_string=_normalize_env(os.getenv("AZURE_STORAGE_CONNECTION_STRING")),
        storage_account_url=_normalize_env(os.getenv("AZURE_STORAGE_ACCOUNT_URL")),
        pdfs_container=pdfs_container,
        pdfs_prefix=(args.pdfs_prefix if args.pdfs_prefix is not None else os.getenv("PDFS_PREFIX", "")).strip("/"),
        results_container=results_container,
        results_prefix=os.getenv("RESULTS_PREFIX", "").strip("/"),
        concurrency=concurrency,
        force=args.force,
        review_app_url=review_app_url,
        confidence_threshold=confidence_threshold,
        slice_enabled=args.slice_only,
        slice_groups_override=args.slice_groups,
        slice_dry_run=args.slice_dry_run,
        slice_llm_enabled=args.slice_llm,
        slice_auto=args.slice_auto,
        enrich_enabled=args.enrich_only,
        enrich_dry_run=args.enrich_dry_run,
        enrich_auto=args.enrich_auto,
        enrich_llm_enabled=args.enrich_llm,
    )


def _build_cu_client(config: AppConfig) -> ContentUnderstandingClient:
    if not config.endpoint:
        raise ValueError("Missing endpoint. Provide --endpoint or AZURE_CONTENT_UNDERSTANDING_ENDPOINT.")
    if not config.analyzer_id:
        raise ValueError("Missing analyzer ID. Provide --analyzer-id or ANALYZER_ID.")

    if _is_real_secret(config.content_understanding_key):
        cu_credential = AzureKeyCredential(config.content_understanding_key.strip())
        LOGGER.info("Using AzureKeyCredential for Content Understanding.")
    else:
        cu_credential = DefaultAzureCredential()
        LOGGER.info("Using DefaultAzureCredential for Content Understanding.")

    return ContentUnderstandingClient(
        endpoint=config.endpoint,
        credential=cu_credential,
        api_version=config.api_version,
    )


def _build_blob_service_client(config: AppConfig) -> BlobServiceClient:
    if _is_real_secret(config.storage_connection_string):
        LOGGER.info("Using storage connection string for Blob Storage authentication.")
        return BlobServiceClient.from_connection_string(config.storage_connection_string)

    if not config.storage_account_url:
        raise ValueError(
            "Missing blob storage auth. Set AZURE_STORAGE_CONNECTION_STRING or "
            "AZURE_STORAGE_ACCOUNT_URL (with DefaultAzureCredential available)."
        )
    LOGGER.info("Using DefaultAzureCredential for Blob Storage authentication.")
    return BlobServiceClient(account_url=config.storage_account_url, credential=DefaultAzureCredential())


async def _run_slice_stage(blob_service_client: BlobServiceClient, config: AppConfig) -> int:
    """Run the post-HITL slice stage: curated JSONs -> per-communication slices."""
    slice_workflow, list_curated = build_slice_workflow(blob_service_client, config)
    slice_result = await slice_workflow.run("start")
    outcomes: list[SliceOutcome] = slice_result.get_outputs()

    ok_slices = [o for o in outcomes if o.status == "ok"]
    failed = [o for o in outcomes if o.status == "failed"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    for outcome in failed:
        LOGGER.error("Failed to slice: %s - %s", outcome.blob_name, outcome.message)

    print("Slice summary:")
    print(f"  sliced:   {len(ok_slices)} ({sum(o.slice_count for o in ok_slices)} files)")
    print(f"  skipped:  {len(skipped) + list_curated.skipped_count}")
    print(f"  failed:   {len(failed)}")
    print(f"  dry-run:  {'yes' if config.slice_dry_run else 'no'}")

    for outcome in ok_slices:
        print(f"  - {outcome.blob_name}: {outcome.slice_count} slice(s)")

    return 1 if failed else 0


async def _run_enrich_stage(blob_service_client: BlobServiceClient, config: AppConfig) -> int:
    """Run the enrichment stage: curated slices -> per-slice enriched JSON."""
    enrich_workflow, list_slices = build_enrich_workflow(blob_service_client, config)
    enrich_result = await enrich_workflow.run("start")
    outcomes: list[EnrichOutcome] = enrich_result.get_outputs()

    ok_enrich = [o for o in outcomes if o.status == "ok"]
    failed = [o for o in outcomes if o.status == "failed"]
    for outcome in failed:
        LOGGER.error("Failed to enrich: %s - %s", outcome.blob_name, outcome.message)

    print("Enrichment summary:")
    print(f"  enriched:   {len(ok_enrich)}")
    print(f"  skipped:    {list_slices.skipped_count}")
    print(f"  failed:     {len(failed)}")
    print(f"  dry-run:    {'yes' if config.enrich_dry_run else 'no'}")
    print(f"  extraction: {'llm' if config.enrich_llm_enabled else 'rules'}")

    for outcome in ok_enrich:
        print(f"  - {outcome.blob_name} (enriched in place)")

    return 1 if failed else 0


async def run(config: AppConfig) -> int:
    blob_service_client = _build_blob_service_client(config)

    if config.slice_enabled or config.enrich_enabled:
        cu_client = None
        try:
            slice_rc = 0
            if config.slice_enabled:
                slice_rc = await _run_slice_stage(blob_service_client, config)
            enrich_rc = 0
            if config.enrich_enabled or config.enrich_auto:
                enrich_rc = await _run_enrich_stage(blob_service_client, config)
            return 1 if (slice_rc or enrich_rc) else 0
        finally:
            await blob_service_client.close()

    cu_client = _build_cu_client(config)

    try:
        workflow, list_pdf_blobs = build_workflow(blob_service_client, cu_client, config)
        run_result = await workflow.run("start")
        outcomes: list[ReviewOutcome] = run_result.get_outputs()

        needs_review = [o for o in outcomes if o.status == "needs_review"]
        ok = [o for o in outcomes if o.status == "ok"]
        failed = [o for o in outcomes if o.status == "failed"]
        for outcome in failed:
            LOGGER.error("Failed: %s - %s", outcome.blob_name, outcome.message)

        print("Summary:")
        print(f"  needs review: {len(needs_review)}")
        print(f"  ok:           {len(ok)}")
        print(f"  skipped:      {list_pdf_blobs.skipped_count}")
        print(f"  failed:       {len(failed)}")

        if needs_review:
            print()
            print(f"Review app: {config.review_app_url}")
            for outcome in needs_review:
                print()
                print(outcome.review_text)

        if config.slice_auto:
            print()
            print("=== Auto-continuing to slice stage (files already reviewed) ===")
            slice_rc = await _run_slice_stage(blob_service_client, config)
            if config.enrich_auto:
                print()
                print("=== Auto-continuing to enrich stage ===")
                enrich_rc = await _run_enrich_stage(blob_service_client, config)
                return 1 if (failed or slice_rc or enrich_rc) else 0
            return 1 if (failed or slice_rc) else 0

        return 1 if failed else 0
    finally:
        await cu_client.close()
        await blob_service_client.close()


async def main_async() -> int:
    load_dotenv(override=False)

    parser = _create_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    try:
        config = _build_config(args)
        return await run(config)
    except ValueError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except Exception:  # noqa: BLE001
        LOGGER.exception("Fatal error")
        return 3


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
