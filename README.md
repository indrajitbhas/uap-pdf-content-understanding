# PDF Content Understanding CLI

Production-ready Python CLI to batch-process PDF files with **Azure AI Content Understanding** and **Azure Blob Storage**.

For each PDF found in a folder tree, the tool:
1. Uploads the source PDF to Blob Storage.
2. Sends the local PDF **bytes** to Content Understanding (`AnalysisInput(data=...)`) for analysis.
3. Saves the analysis JSON:
   - Locally next to the PDF as `<pdf_path>.json`.
   - In Blob Storage as a `.json` blob with matching relative path.

> Scope note: this tool only analyzes PDFs and stores results. Azure AI Search indexing is intentionally out of scope.

## Features

- Recursive folder scan for `.pdf` files.
- Skip already processed files when `<pdf>.json` exists (resume mode).
- `--force` to re-analyze regardless of local JSON existence.
- Parallel processing with `--workers`.
- Per-file fault isolation (one failure does not stop the batch).
- Final summary counters: analyzed, skipped, failed.
- Config via `.env` + environment variables + CLI args (precedence documented below).

## Installation

```bash
cd /home/ubuntu/pdf_content_understanding
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install --pre azure-ai-contentunderstanding azure-identity azure-storage-blob python-dotenv
# or
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and edit values.

```bash
cp .env.example .env
```

### Required variables

- `AZURE_CONTENT_UNDERSTANDING_ENDPOINT`
- `ANALYZER_ID` (default: `uapGeneral`)
- Blob storage auth:
  - either `AZURE_STORAGE_CONNECTION_STRING`
  - or `AZURE_STORAGE_ACCOUNT_URL` (with `DefaultAzureCredential` available)
- `PDFS_CONTAINER`
- `INPUT_PDF_FOLDER` (unless you pass `--folder`)

### Optional variables

- `CONTENT_UNDERSTANDING_KEY` (if omitted/placeholder, tool uses `DefaultAzureCredential`)
- `API_VERSION` (default: `2025-11-01`)
- `RESULTS_CONTAINER` (default: same as `PDFS_CONTAINER`)
- `RESULTS_PREFIX` (default behavior explained below)
- `WORKERS` (default: `4`)

## Authentication behavior

### Content Understanding

- If `CONTENT_UNDERSTANDING_KEY` is present and not a placeholder, the tool uses `AzureKeyCredential`.
- Otherwise it falls back to `DefaultAzureCredential`.

### Blob Storage

- If `AZURE_STORAGE_CONNECTION_STRING` is present and not a placeholder, it is used.
- Otherwise the tool requires `AZURE_STORAGE_ACCOUNT_URL` and uses `DefaultAzureCredential`.

## Blob storage layout

Relative paths are preserved from `--folder` / `INPUT_PDF_FOLDER`.

Given PDF:

`<root>/contracts/2026/sample.pdf`

Uploads:

- Source PDF blob:
  - container: `PDFS_CONTAINER`
  - blob name: `contracts/2026/sample.pdf`

- Analysis JSON blob:
  - container: `RESULTS_CONTAINER` (or same as PDF container if omitted)
  - blob name:
    - `<RESULTS_PREFIX>/contracts/2026/sample.json` when prefix is set
    - `contracts/2026/sample.json` when prefix is empty

Default prefix behavior:
- If `RESULTS_CONTAINER` is omitted and reuses `PDFS_CONTAINER`, default prefix is `results`.
- Otherwise default prefix is empty.

The tool creates missing containers automatically (idempotent).

## CLI usage

```bash
python main.py --help
```

### Main options

- `--folder`: root folder to scan recursively for PDFs.
- `--force`: re-analyze even if `<pdf>.json` already exists.
- `--workers`: number of parallel workers.
- `--analyzer-id`: overrides `ANALYZER_ID`.
- `--api-version`: overrides `API_VERSION`.
- `--endpoint`: overrides `AZURE_CONTENT_UNDERSTANDING_ENDPOINT`.

### Precedence

1. Explicit CLI args (highest)
2. Real environment variables
3. `.env` file values loaded by `python-dotenv`

## Examples

Analyze using `.env` defaults:

```bash
python main.py
```

Analyze explicit folder with 8 workers:

```bash
python main.py --folder /data/pdfs --workers 8
```

Force re-analysis:

```bash
python main.py --folder /data/pdfs --force
```

## Output behavior

For each `some/path/doc.pdf`:

- Local JSON written to: `some/path/doc.pdf.json`
- Blob JSON written with same relative structure and `.json` extension.

At the end, a summary is printed:

```text
Summary:
  analyzed: X
  skipped:  Y
  failed:   Z
```

## Troubleshooting

### `URL has an invalid label` when creating containers

This usually means Blob Storage endpoint settings are malformed.

Check these first:

- `AZURE_STORAGE_CONNECTION_STRING`
  - `AccountName` must be **3-24 lowercase letters/digits only** (no uppercase, underscore, spaces).
  - If `BlobEndpoint` is present, it must be a valid URL like `https://<account>.blob.core.windows.net`.
- `AZURE_STORAGE_ACCOUNT_URL`
  - Must be a valid URL like `https://<account>.blob.core.windows.net`.
- Container names (`PDFS_CONTAINER`, `RESULTS_CONTAINER`)
  - 3-63 chars, lowercase letters/digits/hyphen, start/end with letter or digit, no `--`.

Tip on Windows: if values come from PowerShell environment variables, remove accidental quotes and trailing spaces.

## Notes

- Local skip check is based solely on existence of `<pdf>.json`, allowing offline test of skip logic without Azure credentials.
- SDK bytes input verified with `AnalysisInput(data=..., mime_type="application/pdf")` for the installed prerelease package.
