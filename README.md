# PDF Content Understanding Workflow

Production-ready Python CLI, built on **Microsoft Agent Framework workflows**, that analyzes PDFs
already stored in **Azure Blob Storage** with **Azure AI Content Understanding** (document
intelligence) and saves the analysis JSON back to Blob Storage.

Source PDFs are expected to already be uploaded to a blob container (`PDFS_CONTAINER`, default
`pdfs`). The tool no longer scans a local folder or uploads PDFs itself — it only reads from and
writes to Blob Storage.

## Workflow graph

Built with [`agent-framework`](https://learn.microsoft.com/en-gb/agent-framework/get-started/workflows?pivots=programming-language-python)
using `WorkflowBuilder`, `Executor` subclasses and the `@handler` decorator:

```
list_pdf_blobs  --> analyze_document  --> save_result  --> user_review
```

| Node | Type | Responsibility |
|---|---|---|
| `list_pdf_blobs` | start executor | Lists `*.pdf` blobs in `PDFS_CONTAINER`, skips ones whose result JSON already exists in `RESULTS_CONTAINER` (unless `--force`), and fans the rest out as `AnalyzeTask` messages. |
| `analyze_document` | executor | Downloads the PDF bytes for a blob and runs the Azure AI Content Understanding analyzer (`ANALYZER_ID`) against the bytes, bounded by a concurrency semaphore. |
| `save_result` | executor | Uploads the analysis JSON to `RESULTS_CONTAINER` (default container: `pdf-results`). |
| `user_review` | terminal executor | Re-reads the saved JSON and flags every word with confidence `< CONFIDENCE_THRESHOLD` (default `0.8`), builds a review note pointing to the PDF review app (`REVIEW_APP_URL`) and yields it as workflow output. |

Each PDF flows through the graph independently, so failures are isolated per file. The CLI runs
`workflow.run("start")` once and reads the aggregated review outcomes via `get_outputs()`.

## User review integration

When the analysis of a file contains words with confidence `< CONFIDENCE_THRESHOLD`, the
`user_review` node produces a review note that lists those words (page, text, confidence) and
asks the user to review them in the [PDF review app](https://uap-pdf-review.azurewebsites.net/)
([source](https://github.com/indrajitbhas/validationFromContentUnderstandingJSON)). The review
app reads the same containers (`pdfs` / `pdf-results`) in cloud mode, so the PDF and its raw
JSON are available there once this workflow has saved them.

## Installation

```bash
cd uap-pdf-content-understanding
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on Linux/macOS
pip install --upgrade pip
pip install -r requirements.txt
```

## Configuration

Set the environment variables below in your shell or in a `.env` file next to `main.py`
(`python-dotenv` loads it automatically at startup). Required variables can also be provided via
CLI flags (see "CLI usage").

### Required variables

- `AZURE_CONTENT_UNDERSTANDING_ENDPOINT`
- `ANALYZER_ID`
- Blob storage auth:
  - either `AZURE_STORAGE_CONNECTION_STRING`
  - or `AZURE_STORAGE_ACCOUNT_URL` (with `DefaultAzureCredential` available)
- `PDFS_CONTAINER` (unless you pass `--pdfs-container`)

### Optional variables

- `CONTENT_UNDERSTANDING_KEY` (if omitted/placeholder, tool uses `DefaultAzureCredential`)
- `API_VERSION` (default: `2025-11-01`)
- `PDFS_PREFIX` (optional virtual-folder prefix to scan within `PDFS_CONTAINER`)
- `RESULTS_CONTAINER` (default: `pdf-results`)
- `RESULTS_PREFIX` (optional prefix for JSON blobs inside `RESULTS_CONTAINER`)
- `WORKERS` (max concurrent analyses, default: `4`)
- `CONFIDENCE_THRESHOLD` (review words with confidence below this value, default: `0.8`)
- `REVIEW_APP_URL` (URL of the PDF review app, default: `https://uap-pdf-review.azurewebsites.net/`)

## Authentication behavior

### Content Understanding

- If `CONTENT_UNDERSTANDING_KEY` is present and not a placeholder, the tool uses `AzureKeyCredential`.
- Otherwise it falls back to `DefaultAzureCredential`.

### Blob Storage

- If `AZURE_STORAGE_CONNECTION_STRING` is present and not a placeholder, it is used.
- Otherwise the tool requires `AZURE_STORAGE_ACCOUNT_URL` and uses `DefaultAzureCredential`.

Both Azure SDK clients are used in their `aio` (async) form so the workflow executors can run
concurrently.

## Blob storage layout

Given a source blob `contracts/2026/sample.pdf` in `PDFS_CONTAINER`:

- Analysis JSON is saved to:
  - container: `RESULTS_CONTAINER` (default `pdf-results`)
  - blob name: `<RESULTS_PREFIX>/contracts/2026/sample.json` when a prefix is set, otherwise
    `contracts/2026/sample.json`

Both containers (`PDFS_CONTAINER` and `RESULTS_CONTAINER`) are expected to already exist; the tool does not create them.

## CLI usage

```bash
python main.py --help
```

### Main options

- `--pdfs-container`: overrides `PDFS_CONTAINER`.
- `--pdfs-prefix`: overrides `PDFS_PREFIX`.
- `--results-container`: overrides `RESULTS_CONTAINER`.
- `--force`: re-analyze even if a result blob already exists.
- `--concurrency`: max PDFs analyzed concurrently (overrides `WORKERS`).
- `--analyzer-id`: overrides `ANALYZER_ID`.
- `--api-version`: overrides `API_VERSION`.
- `--endpoint`: overrides `AZURE_CONTENT_UNDERSTANDING_ENDPOINT`.
- `--review-app-url`: overrides `REVIEW_APP_URL`.
- `--confidence-threshold`: overrides `CONFIDENCE_THRESHOLD` (must be in `(0, 1]`).

### Precedence

1. Explicit CLI args (highest)
2. Real environment variables
3. `.env` file values loaded by `python-dotenv`

## Examples

Analyze using `.env` defaults:

```bash
python main.py
```

Analyze a specific container/prefix with higher concurrency:

```bash
python main.py --pdfs-container pdfs --pdfs-prefix contracts/2026 --concurrency 8
```

Force re-analysis of everything:

```bash
python main.py --force
```

## Output behavior

At the end of the run, a summary is printed:

```text
Summary:
  needs review: X
  ok:           Y
  skipped:      Z
  failed:       W

Review app: https://uap-pdf-review.azurewebsites.net/

Review requested for: sample.pdf
Open in the review app: https://uap-pdf-review.azurewebsites.net/
3 word(s) with confidence < 0.8:
  page 1: 'qulity' (confidence 0.420)
  page 1: 'review' (confidence 0.650)
  page 2: 'BadWord!' (confidence 0.100)
```

Files whose analysis contains words below the confidence threshold are listed with the PDF review
app URL; open the app (which reads the same `pdf-results` / `pdfs` containers) to correct them.

## Notes

- The result-exists check (`<blob>.json` in `RESULTS_CONTAINER`) is what drives the skip/resume
  behavior; use `--force` to bypass it.
- SDK bytes input verified with `AnalysisInput(data=..., mime_type="application/pdf")` for the
  installed prerelease `azure-ai-contentunderstanding` package.
