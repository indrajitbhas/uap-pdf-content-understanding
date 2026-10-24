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
list_pdf_blobs  --> analyze_document  --> save_result
```

| Node | Type | Responsibility |
|---|---|---|
| `list_pdf_blobs` | start executor | Lists `*.pdf` blobs in `PDFS_CONTAINER`, skips ones whose result JSON already exists in `RESULTS_CONTAINER` (unless `--force`), and fans the rest out as `AnalyzeTask` messages. |
| `analyze_document` | executor | Downloads the PDF bytes for a blob and runs the Azure AI Content Understanding analyzer (`ANALYZER_ID`) against the bytes, bounded by a concurrency semaphore. |
| `save_result` | terminal executor | Uploads the analysis JSON to `RESULTS_CONTAINER` (default container: `pdf-results`) and yields a per-file `FileOutcome` as workflow output. |

Each PDF flows through the graph independently, so failures are isolated per file. The CLI runs
`workflow.run("start")` once and reads the aggregated `FileOutcome` list via `get_outputs()`.

## Installation

```bash
cd uap-pdf-content-understanding
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on Linux/macOS
pip install --upgrade pip
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and edit values.

```bash
cp .env.example .env
```

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
  analyzed: X
  skipped:  Y
  failed:   Z
```

## Notes

- The result-exists check (`<blob>.json` in `RESULTS_CONTAINER`) is what drives the skip/resume
  behavior; use `--force` to bypass it.
- SDK bytes input verified with `AnalysisInput(data=..., mime_type="application/pdf")` for the
  installed prerelease `azure-ai-contentunderstanding` package.
