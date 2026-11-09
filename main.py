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

A second workflow (``--slice-only``) runs *after* HITL word review:
``list_curated_blobs`` scans the results container for ``*.curated.json`` blobs
written by the review app, ``slice_curated`` splits each into per-communication
slices (LLM grouping, heuristics as fallback), ``standardize_text`` reviews and
fixes the OCR text with an LLM (split words, extra spaces), and ``save_sliced``
writes ``<stem>__pNN[-NN].curated.json`` to the sliced container
(``SLICED_CONTAINER``, default ``pdf-curated-sliced``). Entity and research
enrichment is left to the AI Search skillset at indexing time.

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
from datetime import datetime, timezone
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

from text_cleanup import DEFAULT_BASE_URL, llm_clean_pages
from split_curated import (
    apply_groups,
    group_pages,
    is_already_sliced,
    parse_groups,
    slice_curated,
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
    slice_auto: bool
    sliced_container: str


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
class SlicedDoc:
    """Slices built from one curated blob, passed between the slice nodes."""

    curated_blob_name: str
    folder: str
    slices: tuple


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
        sliced_names: list[str] = []
        if not self._config.force:
            sliced_container = self._blob_service_client.get_container_client(self._config.sliced_container)
            async for blob in sliced_container.list_blobs(name_starts_with=self._config.results_prefix or None):
                sliced_names.append(blob.name)
        async for blob in results_container.list_blobs(name_starts_with=self._config.results_prefix or None):
            name = blob.name
            if not name.lower().endswith(".curated.json"):
                continue
            if is_already_sliced(name) or ".bak" in name.lower():
                self.skipped_count += 1
                LOGGER.info("Skipping (already a slice or backup): %s", name)
                continue
            slice_prefix = name[: -len(".curated.json")] + "__p"
            if any(existing.startswith(slice_prefix) for existing in sliced_names):
                self.skipped_count += 1
                LOGGER.info("Skipping (already sliced in %s; use --force): %s", self._config.sliced_container, name)
                continue
            self.discovered_count += 1
            await ctx.send_message(SliceTask(curated_blob_name=name))

        LOGGER.info(
            "Slice stage: discovered %d curated blob(s), skipped %d",
            self.discovered_count,
            self.skipped_count,
        )


class SliceCuratedExecutor(Executor):
    """Decides the page groups of a curated JSON and builds one slice per group.

    Grouping: explicit ``--slice-groups`` override, else a low-cost LLM
    (``LLM_GROUPING_*``), else deterministic heuristics (see ``split_curated.py``).
    Nothing is written here: the slices go on to ``standardize_text`` and then
    ``save_sliced``.
    """

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config

    @handler
    async def slice_curated(self, task: SliceTask, ctx: WorkflowContext[SlicedDoc, SliceOutcome]) -> None:
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

            groups: list[list[dict]]
            if self._config.slice_groups_override:
                groups = apply_groups(pages, parse_groups(self._config.slice_groups_override))
            else:
                spec = await asyncio.to_thread(
                    _llm_group_spec,
                    pages,
                    os.getenv("LLM_GROUPING_MODEL", "gpt-4.1-nano"),
                    os.getenv("LLM_GROUPING_BASE_URL") or DEFAULT_BASE_URL,
                    os.getenv("LLM_GROUPING_API_KEY"),
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

            slash = task.curated_blob_name.rfind("/")
            folder = task.curated_blob_name[: slash + 1] if slash >= 0 else ""
            bare = task.curated_blob_name[slash + 1:] if slash >= 0 else task.curated_blob_name
            stem = bare[: -len(".curated.json")] if bare.lower().endswith(".curated.json") else bare

            slices = slice_curated(curated, groups, stem=stem, source_file_name=task.curated_blob_name)
            await ctx.send_message(
                SlicedDoc(curated_blob_name=task.curated_blob_name, folder=folder, slices=tuple(slices))
            )
        except (AzureError, json.JSONDecodeError, ValueError) as exc:
            message = getattr(exc, "message", str(exc)) or str(exc)
            LOGGER.error("Failed to slice %s: %s", task.curated_blob_name, message)
            await ctx.yield_output(
                SliceOutcome(blob_name=task.curated_blob_name, status="failed", slice_count=0, message=message)
            )


class StandardizeTextExecutor(Executor):
    """Reviews and fixes the OCR text of every page in the slices with an LLM.

    Re-joins words split by hyphens or stray characters, removes extra spaces and
    spacing around punctuation, and fixes OCR letter errors. A page's text is only
    replaced when its digits are unchanged and its letters are at least 97% similar
    (see ``text_cleanup.py``). Pages are processed in batches across all slices of one
    curated file.
    """

    def __init__(self, id: str, config: AppConfig):
        super().__init__(id=id)
        self._config = config

    @handler
    async def standardize(self, doc: SlicedDoc, ctx: WorkflowContext[SlicedDoc]) -> None:
        try:
            batch_size = int(os.getenv("LLM_GROUPING_BATCH", "10"))
        except (TypeError, ValueError):
            batch_size = 10
        flat = [page for _, data in doc.slices for page in data["pages"]]
        cleaned = await asyncio.to_thread(
            llm_clean_pages,
            flat,
            model=os.getenv("CLEANUP_MODEL") or os.getenv("LLM_GROUPING_MODEL", "gpt-4.1-nano"),
            base_url=os.getenv("LLM_GROUPING_BASE_URL") or DEFAULT_BASE_URL,
            api_key=os.getenv("LLM_GROUPING_API_KEY"),
            batch_size=batch_size if batch_size > 0 else 10,
            delay=float(os.getenv("LLM_GROUPING_DELAY", "75")),
        )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = []
        cursor = 0
        for name, data in doc.slices:
            count = len(data["pages"])
            new_data = dict(data)
            new_data["pages"] = cleaned[cursor : cursor + count]
            new_data["document"] = {**data["document"], "textStandardizedAt": stamp}
            cursor += count
            out.append((name, new_data))
        LOGGER.info("Standardized text: %s (%d slice(s), %d page(s))", doc.curated_blob_name, len(out), len(flat))
        await ctx.send_message(SlicedDoc(curated_blob_name=doc.curated_blob_name, folder=doc.folder, slices=tuple(out)))


class SaveSlicedExecutor(Executor):
    """Terminal node: uploads the finished slices to the sliced container."""

    def __init__(self, id: str, blob_service_client: BlobServiceClient, config: AppConfig):
        super().__init__(id=id)
        self._blob_service_client = blob_service_client
        self._config = config

    @handler
    async def save_sliced(self, doc: SlicedDoc, ctx: WorkflowContext[Never, SliceOutcome]) -> None:
        try:
            if self._config.slice_dry_run:
                LOGGER.info("DRY RUN: would write %d slice(s) for %s", len(doc.slices), doc.curated_blob_name)
            else:
                container = self._blob_service_client.get_container_client(self._config.sliced_container)
                for name, data in doc.slices:
                    target = f"{doc.folder}{name}"
                    await container.upload_blob(
                        name=target,
                        data=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                        overwrite=True,
                        content_settings=ContentSettings(content_type="application/json"),
                    )
                    LOGGER.info("Saved slice: %s/%s (%d page(s))", self._config.sliced_container, target, len(data["pages"]))
            await ctx.yield_output(
                SliceOutcome(blob_name=doc.curated_blob_name, status="ok", slice_count=len(doc.slices))
            )
        except AzureError as exc:
            message = getattr(exc, "message", str(exc)) or str(exc)
            LOGGER.error("Failed to save slices for %s: %s", doc.curated_blob_name, message)
            await ctx.yield_output(
                SliceOutcome(blob_name=doc.curated_blob_name, status="failed", slice_count=0, message=message)
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
    """Build the post-HITL slice workflow: curated JSONs -> cleaned slices in the sliced container.

    Graph: list_curated_blobs -> slice_curated -> standardize_text -> save_sliced.
    """
    list_curated = ListCuratedBlobsExecutor("list_curated_blobs", blob_service_client, config)
    slice_curated_node = SliceCuratedExecutor("slice_curated", blob_service_client, config)
    standardize_text = StandardizeTextExecutor("standardize_text", config)
    save_sliced = SaveSlicedExecutor("save_sliced", blob_service_client, config)

    workflow = (
        WorkflowBuilder(start_executor=list_curated)
        .add_edge(list_curated, slice_curated_node)
        .add_edge(slice_curated_node, standardize_text)
        .add_edge(standardize_text, save_sliced)
        .build()
    )
    return workflow, list_curated


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
        help="Re-analyze PDFs even when a result blob already exists, and re-slice curated "
        "files that already have slices in the sliced container.",
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
        "blob into per-communication files, fix their text and save them to the sliced container.",
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
        help="Deprecated no-op: slicing always uses the LLM for page grouping.",
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
        "--sliced-container",
        default=None,
        help="Container for the finished slices. Falls back to SLICED_CONTAINER (default: pdf-curated-sliced).",
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
    sliced_container = args.sliced_container or os.getenv("SLICED_CONTAINER", "pdf-curated-sliced")

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
        slice_auto=args.slice_auto,
        sliced_container=sliced_container,
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
    print(f"  target:   {config.sliced_container}")

    for outcome in ok_slices:
        print(f"  - {outcome.blob_name}: {outcome.slice_count} slice(s)")

    return 1 if failed else 0


async def run(config: AppConfig) -> int:
    blob_service_client = _build_blob_service_client(config)

    if config.slice_enabled:
        try:
            return await _run_slice_stage(blob_service_client, config)
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
