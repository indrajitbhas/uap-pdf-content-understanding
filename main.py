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

See: https://learn.microsoft.com/en-gb/agent-framework/get-started/workflows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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


async def run(config: AppConfig) -> int:
    blob_service_client = _build_blob_service_client(config)
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
