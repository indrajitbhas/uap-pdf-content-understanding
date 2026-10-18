#!/usr/bin/env python3
"""Batch PDF analyzer using Azure AI Content Understanding and Azure Blob Storage.

This CLI scans a folder recursively for PDFs, uploads each source PDF to Blob Storage,
submits bytes to Azure AI Content Understanding, and saves analysis JSON both locally
(next to the PDF) and in Blob Storage.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

if sys.version_info >= (3, 10):
    from typing import TypeGuard
else:  # Python 3.9: typing_extensions is installed as a dependency of azure-core
    from typing_extensions import TypeGuard

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ServiceResponseError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput, AnalysisResult
from dotenv import load_dotenv


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
CONTAINER_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")
STORAGE_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration for the CLI."""

    folder: Path
    endpoint: Optional[str]
    analyzer_id: str
    api_version: str
    content_understanding_key: Optional[str]
    storage_connection_string: Optional[str]
    storage_account_url: Optional[str]
    pdfs_container: str
    results_container: str
    results_prefix: str
    workers: int
    force: bool


@dataclass(frozen=True)
class FileOutcome:
    """Per-file execution outcome for summary reporting."""

    pdf_path: Path
    status: str
    elapsed_seconds: float
    message: str = ""


@dataclass(frozen=True)
class RuntimeClients:
    """Initialized Azure SDK clients and normalized storage settings."""

    cu_client: ContentUnderstandingClient
    blob_service_client: BlobServiceClient
    pdfs_container: str
    results_container: str
    results_prefix: str


@dataclass(frozen=True)
class PlannedFile:
    """Represents one source PDF and its derived local JSON path."""

    pdf_path: Path
    local_json_path: Path


def _normalize_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _is_real_secret(value: Optional[str]) -> TypeGuard[str]:
    """True if value is a non-empty, non-placeholder string (narrows the type to str)."""
    if value is None:
        return False
    normalized = value.strip().strip('"').strip("'").strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    if lowered in PLACEHOLDER_VALUES:
        return False
    # Template values such as "your-content-understanding-key", "<key>" or "AccountName=...;"
    if lowered.startswith(("your-", "your_", "<")) or "..." in normalized:
        return False
    return True


def _warn_if_placeholder(name: str, value: Optional[str]) -> None:
    """Log a warning when a variable is set but still holds a template placeholder."""
    if value and not _is_real_secret(value):
        LOGGER.warning("%s looks like a template placeholder and will be ignored.", name)


def _is_valid_container_name(value: str) -> bool:
    """Azure Blob container naming rules (subset): 3-63, lowercase alnum/hyphen."""
    return bool(CONTAINER_NAME_RE.fullmatch(value))


def _is_valid_dns_host(hostname: str) -> bool:
    """Best-effort DNS hostname validation for account URLs/endpoints."""
    if not hostname or len(hostname) > 253:
        return False
    labels = hostname.split(".")
    label_re = re.compile(r"^[A-Za-z0-9-]{1,63}$")
    for label in labels:
        if not label:
            return False
        if not label_re.fullmatch(label):
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
    return True


def _conn_str_dict(conn_str: str) -> dict[str, str]:
    """Parse Azure connection string into key/value pairs (case-sensitive keys)."""
    pairs: dict[str, str] = {}
    for part in conn_str.split(";"):
        if not part:
            continue
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def _validate_storage_settings(config: AppConfig) -> None:
    """Validate container names and storage endpoint/account settings before any network call."""
    for name, label in ((config.pdfs_container, "PDFS_CONTAINER"), (config.results_container, "RESULTS_CONTAINER")):
        if not _is_valid_container_name(name):
            raise ValueError(
                f"Invalid {label}='{name}'. Container names must be 3-63 chars, lowercase letters/numbers/hyphens, "
                "start/end with letter or number, and not contain consecutive hyphens."
            )

    if _is_real_secret(config.storage_connection_string):
        conn_values = _conn_str_dict(config.storage_connection_string)
        account_name = conn_values.get("AccountName", "")
        blob_endpoint = conn_values.get("BlobEndpoint", "")

        if account_name and not STORAGE_ACCOUNT_RE.fullmatch(account_name):
            raise ValueError(
                f"Invalid storage AccountName='{account_name}' in AZURE_STORAGE_CONNECTION_STRING. "
                "Use only lowercase letters and digits, length 3-24."
            )

        if blob_endpoint:
            parsed = urlparse(blob_endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not _is_valid_dns_host(parsed.hostname or ""):
                raise ValueError(
                    "Invalid BlobEndpoint in AZURE_STORAGE_CONNECTION_STRING. "
                    "Expected a valid URL like 'https://<account>.blob.core.windows.net'."
                )

        return

    if not config.storage_account_url:
        raise ValueError(
            "Missing blob storage auth. Set AZURE_STORAGE_CONNECTION_STRING or "
            "AZURE_STORAGE_ACCOUNT_URL (with DefaultAzureCredential available)."
        )

    parsed = urlparse(config.storage_account_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not _is_valid_dns_host(parsed.hostname or ""):
        raise ValueError(
            f"Invalid AZURE_STORAGE_ACCOUNT_URL='{config.storage_account_url}'. "
            "Expected a valid URL like 'https://<account>.blob.core.windows.net'."
        )



def _rel_blob_path(pdf_path: Path, root_folder: Path) -> str:
    """Blob path for source PDF preserving folder structure."""
    return pdf_path.relative_to(root_folder).as_posix()


def _result_blob_path(pdf_path: Path, root_folder: Path, prefix: str) -> str:
    """Blob path for analysis JSON preserving folder structure and .json extension."""
    relative_pdf = pdf_path.relative_to(root_folder)
    relative_json = relative_pdf.with_suffix(".json").as_posix()
    cleaned_prefix = prefix.strip("/")
    return f"{cleaned_prefix}/{relative_json}" if cleaned_prefix else relative_json


def _local_json_path(pdf_path: Path) -> Path:
    """Local JSON output path: <pdf_path>.json (append .json)."""
    return Path(f"{pdf_path}.json")


def discover_pdf_files(folder: Path) -> list[Path]:
    """Find all PDFs recursively from folder (case-insensitive extension)."""
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")


def plan_files(pdf_paths: list[Path], force: bool) -> tuple[list[PlannedFile], int]:
    """Plan which files need processing and count skipped files."""
    to_process: list[PlannedFile] = []
    skipped = 0

    for pdf_path in pdf_paths:
        local_json = _local_json_path(pdf_path)
        if not force and local_json.exists():
            skipped += 1
            LOGGER.info("Skipping (local JSON exists): %s", pdf_path)
            continue
        to_process.append(PlannedFile(pdf_path=pdf_path, local_json_path=local_json))

    return to_process, skipped


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze local PDFs with Azure AI Content Understanding, upload source PDFs "
            "to Blob Storage, and save analysis JSON both locally and in Blob Storage."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--folder",
        help="Folder containing PDF files (recursive scan). Falls back to INPUT_PDF_FOLDER.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze PDFs even when <pdf_path>.json already exists next to the PDF.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of parallel worker threads for per-file processing.",
    )
    parser.add_argument(
        "--analyzer-id",
        default=None,
        help="Azure Content Understanding analyzer ID.",
    )
    parser.add_argument(
        "--api-version",
        default=None,
        help="Azure Content Understanding API version.",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Azure Content Understanding endpoint (https://...services.ai.azure.com/).",
    )
    return parser


def _build_config(args: argparse.Namespace) -> AppConfig:
    endpoint = args.endpoint or os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")
    analyzer_id = args.analyzer_id or os.getenv("ANALYZER_ID") or "uapGeneral"
    api_version = args.api_version or os.getenv("API_VERSION", "2025-11-01")

    folder_raw = args.folder or os.getenv("INPUT_PDF_FOLDER")
    if not folder_raw:
        raise ValueError("Missing input folder. Provide --folder or INPUT_PDF_FOLDER in environment/.env.")

    workers = args.workers if args.workers is not None else int(os.getenv("WORKERS", "4"))
    if workers <= 0:
        raise ValueError("--workers must be >= 1")

    pdfs_container = os.getenv("PDFS_CONTAINER")
    if not pdfs_container:
        raise ValueError("Missing PDFS_CONTAINER in environment/.env.")

    results_container = os.getenv("RESULTS_CONTAINER") or pdfs_container
    results_prefix = os.getenv("RESULTS_PREFIX", "results" if results_container == pdfs_container else "")

    return AppConfig(
        folder=Path(folder_raw).expanduser().resolve(),
        endpoint=endpoint,
        analyzer_id=analyzer_id,
        api_version=api_version,
        content_understanding_key=_normalize_env(os.getenv("CONTENT_UNDERSTANDING_KEY")),
        storage_connection_string=_normalize_env(os.getenv("AZURE_STORAGE_CONNECTION_STRING")),
        storage_account_url=_normalize_env(os.getenv("AZURE_STORAGE_ACCOUNT_URL")),
        pdfs_container=pdfs_container,
        results_container=results_container,
        results_prefix=results_prefix,
        workers=workers,
        force=args.force,
    )


def _build_runtime_clients(config: AppConfig) -> RuntimeClients:
    _warn_if_placeholder("CONTENT_UNDERSTANDING_KEY", config.content_understanding_key)
    _warn_if_placeholder("AZURE_STORAGE_CONNECTION_STRING", config.storage_connection_string)
    _validate_storage_settings(config)

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

    cu_client = ContentUnderstandingClient(
        endpoint=config.endpoint,
        credential=cu_credential,
        api_version=config.api_version,
    )

    if _is_real_secret(config.storage_connection_string):
        blob_service_client = BlobServiceClient.from_connection_string(config.storage_connection_string)
        LOGGER.info("Using storage connection string for Blob Storage authentication.")
    else:
        if not config.storage_account_url:
            raise ValueError(
                "Missing blob storage auth. Set AZURE_STORAGE_CONNECTION_STRING or "
                "AZURE_STORAGE_ACCOUNT_URL (with DefaultAzureCredential available)."
            )
        blob_service_client = BlobServiceClient(
            account_url=config.storage_account_url,
            credential=DefaultAzureCredential(),
        )
        LOGGER.info("Using DefaultAzureCredential for Blob Storage authentication.")

    return RuntimeClients(
        cu_client=cu_client,
        blob_service_client=blob_service_client,
        pdfs_container=config.pdfs_container,
        results_container=config.results_container,
        results_prefix=config.results_prefix,
    )


def _ensure_containers(clients: RuntimeClients) -> None:
    """Ensure required containers exist (idempotent)."""
    names = {clients.pdfs_container, clients.results_container}
    for container_name in names:
        container_client = clients.blob_service_client.get_container_client(container_name)
        try:
            container_client.create_container()
            LOGGER.info("Created container: %s", container_name)
        except ResourceExistsError:
            LOGGER.info("Verified container exists: %s", container_name)
        except ServiceResponseError as exc:
            raise ValueError(
                "Could not reach Azure Blob endpoint while creating/verifying container "
                f"'{container_name}'. This usually means a malformed storage account URL/connection string "
                "(for example an invalid account name label)."
            ) from exc
        except ClientAuthenticationError as exc:
            raise ValueError(
                "Blob Storage authentication failed. Either set a real AZURE_STORAGE_CONNECTION_STRING, "
                "or sign in for DefaultAzureCredential (e.g. run 'az login') with an identity that has the "
                f"'Storage Blob Data Contributor' role on the storage account. Details: {exc.message}"
            ) from exc
        except HttpResponseError as exc:
            if exc.status_code == 403:
                raise ValueError(
                    f"Access denied (403) to container '{container_name}'. Grant your identity the "
                    "'Storage Blob Data Contributor' role on the storage account (role changes can take a few "
                    "minutes), or use AZURE_STORAGE_CONNECTION_STRING."
                ) from exc
            raise


def _process_one_file(plan: PlannedFile, config: AppConfig, clients: RuntimeClients) -> FileOutcome:
    start = time.perf_counter()
    pdf_path = plan.pdf_path
    local_json_path = plan.local_json_path

    try:
        pdf_blob_name = _rel_blob_path(pdf_path, config.folder)
        result_blob_name = _result_blob_path(pdf_path, config.folder, clients.results_prefix)

        pdf_container = clients.blob_service_client.get_container_client(clients.pdfs_container)
        with pdf_path.open("rb") as stream:
            pdf_container.upload_blob(name=pdf_blob_name, data=stream, overwrite=True)

        with pdf_path.open("rb") as stream:
            pdf_bytes = stream.read()

        poller = clients.cu_client.begin_analyze(
            analyzer_id=config.analyzer_id,
            inputs=[
                AnalysisInput(
                    data=pdf_bytes,
                    name=pdf_path.name,
                    mime_type="application/pdf",
                )
            ],
        )
        result: AnalysisResult = poller.result()

        json_text = json.dumps(result.as_dict(), indent=2)

        local_json_path.parent.mkdir(parents=True, exist_ok=True)
        local_json_path.write_text(json_text, encoding="utf-8")

        results_container = clients.blob_service_client.get_container_client(clients.results_container)
        results_container.upload_blob(
            name=result_blob_name,
            data=json_text.encode("utf-8"),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

        elapsed = time.perf_counter() - start
        LOGGER.info("Analyzed: %s (%.2fs)", pdf_path, elapsed)
        return FileOutcome(pdf_path=pdf_path, status="analyzed", elapsed_seconds=elapsed)

    except AzureError as exc:
        elapsed = time.perf_counter() - start
        message = getattr(exc, "message", str(exc)) or str(exc)
        LOGGER.error("AzureError for %s: %s", pdf_path, message)
        return FileOutcome(pdf_path=pdf_path, status="failed", elapsed_seconds=elapsed, message=message)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        LOGGER.exception("Unexpected error for %s", pdf_path)
        return FileOutcome(pdf_path=pdf_path, status="failed", elapsed_seconds=elapsed, message=str(exc))


def run(config: AppConfig) -> int:
    if not config.folder.exists() or not config.folder.is_dir():
        LOGGER.error("Input folder does not exist or is not a directory: %s", config.folder)
        return 2

    pdf_files = discover_pdf_files(config.folder)
    LOGGER.info("Discovered %d PDF file(s) under %s", len(pdf_files), config.folder)

    plans, skipped = plan_files(pdf_files, force=config.force)

    if not plans:
        print("Summary:")
        print(f"  analyzed: 0")
        print(f"  skipped:  {skipped}")
        print("  failed:   0")
        return 0

    clients = _build_runtime_clients(config)
    _ensure_containers(clients)

    analyzed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        futures = [executor.submit(_process_one_file, plan, config, clients) for plan in plans]
        for future in as_completed(futures):
            outcome = future.result()
            if outcome.status == "analyzed":
                analyzed += 1
            else:
                failed += 1

    print("Summary:")
    print(f"  analyzed: {analyzed}")
    print(f"  skipped:  {skipped}")
    print(f"  failed:   {failed}")

    return 1 if failed > 0 else 0


def main() -> int:
    load_dotenv(override=False)

    parser = _create_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    # Silence per-request HTTP logs from the Azure SDK; keep warnings and errors.
    logging.getLogger("azure").setLevel(logging.WARNING)

    try:
        config = _build_config(args)
        return run(config)
    except ValueError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Fatal error: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
