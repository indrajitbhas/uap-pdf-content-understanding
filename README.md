# PDF Content Understanding Workflow

A command-line tool that takes scanned PDFs (here: historical Army Air Forces correspondence) and
turns them into clean JSON, one file per letter/memo/form, ready to be indexed by Azure AI Search.

1. **Analyze** - read each PDF with Azure AI Content Understanding (OCR + layout).
2. **Review** - a person fixes low-confidence words in a web app.
3. **Slice** - split each reviewed file into one JSON per communication.
4. **Standardize text** - an LLM reviews each slice and fixes OCR text problems (split words, extra
   spaces, OCR letter errors). It never changes a digit.

Finding people, places, sightings, document types and so on is **not** done here. That enrichment
happens later, in the AI Search skillset while indexing. This tool only delivers clean, sliced text.

Everything lives in Azure Blob Storage. The tool reads from containers and writes to containers. It
never scans local folders.

It is built on [Microsoft Agent Framework workflows](https://learn.microsoft.com/en-gb/agent-framework/get-started/workflows?pivots=programming-language-python).
Each step below is a small "workflow": a chain of nodes that each do one job.

## The pipeline at a glance

```text
 PDFs in PDFS_CONTAINER
        |
        v
 [1 ANALYZE]   python main.py
        |         list_pdf_blobs -> analyze_document -> save_result -> user_review
        v         result: <doc>.json in RESULTS_CONTAINER (pdf-results)
 [2 REVIEW]    a human fixes doubtful words in the review app   (outside this tool)
        |         result: <doc>.curated.json in pdf-results
        v
 [3 SLICE]     python main.py --slice-only
                  list_curated_blobs -> slice_curated -> standardize_text -> save_sliced
                  result: <doc>__p26-27.curated.json (one file per communication)
                          in SLICED_CONTAINER (pdf-curated-sliced)
```

The tool stops after step 1 on purpose. Review is done by a person, so you come back and run step 3
when they are finished.

## Quick start

```bash
cd uap-pdf-content-understanding
python -m venv .venv
.venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Put your settings in a `.env` file next to `main.py` (see "Settings" below), then:

```bash
python main.py                        # 1. analyze every new PDF
python main.py --slice-only           # 3. after review: slice, fix the text, save to pdf-curated-sliced
```

Try before you commit - this does all the work (including the LLM calls) but uploads nothing:

```bash
python main.py --slice-only --slice-dry-run
```

## What each node does

| Step | Node | In plain words |
|---|---|---|
| Analyze | `list_pdf_blobs` | Finds `*.pdf` files in `PDFS_CONTAINER`. Skips any that already have a result (unless `--force`). |
| Analyze | `analyze_document` | Sends the PDF to Content Understanding (`ANALYZER_ID`). Several run at once, limited by `WORKERS`. |
| Analyze | `save_result` | Saves the analysis JSON in `RESULTS_CONTAINER` (default `pdf-results`). |
| Analyze | `user_review` | Lists every word with confidence below `CONFIDENCE_THRESHOLD` (default 0.8) and points to the review app. |
| Slice | `list_curated_blobs` | Finds reviewed `*.curated.json` files in `pdf-results`. Skips slices, backups, and files already sliced in `pdf-curated-sliced` (unless `--force`). |
| Slice | `slice_curated` | Only decides which pages belong together (LLM grouping, built-in heuristics as fallback, or your own `--slice-groups`) and builds one slice per group. The per-page `words` array is left out. |
| Slice | `standardize_text` | An LLM reviews the text of every page and fixes OCR problems: re-joins split words (`EN - VELOPE` becomes `ENVELOPE`) and removes extra spaces. See "How the text fix works". |
| Slice | `save_sliced` | Writes each slice to `SLICED_CONTAINER` (default `pdf-curated-sliced`) as `<stem>__pNN[-NN].curated.json`. With `--slice-dry-run` it only logs. |

Each PDF moves through the graph independently, so one failure never stops the others.

### The review step (human in the loop)

When words are doubtful, `user_review` prints a note like this and the workflow ends:

```text
Review requested for: sample.pdf
Open in the review app: https://uap-pdf-review.azurewebsites.net/
3 word(s) with confidence < 0.8:
  page 1: 'qulity' (confidence 0.420)
  page 1: 'review' (confidence 0.650)
  page 2: 'BadWord!' (confidence 0.100)
```

Open the [review app](https://uap-pdf-review.azurewebsites.net/)
([source](https://github.com/indrajitbhas/validationFromContentUnderstandingJSON)), fix the words,
and it saves `<doc>.curated.json` next to the analysis result. Then run the slice step.

Two ways to run the slice step:

- `python main.py --slice-only` - run it by itself whenever review is finished (the normal way).
- `python main.py --slice-auto` - run analyze, then immediately slice every curated file that
  already exists. Files still waiting for review are left alone, so review is never skipped.

## How the text fix works: a before/after example

Take the first slice of "General 1946-7 Vol 2" (pages 1-2, an AMC letter about flying discs). The OCR
text looks like this:

```text
HqAMC form No . 10-514 ( Rev 10 Sep 46 ) WF - L - 7 JAN 47 300M SECRET HEADQUARTERS AIR MATERIEL
COMMAND ... SAVE IN REPLY ADDRESS BOTH . COMMUNICATION AND EN - VELOPE TO COMMANDING GENERAL , AIR
MATERIEL COMMAND ... Intelli - gence Department of this Command is continuing the collection ...
```

After `standardize_text`:

```text
HqAMC form No. 10-514 (Rev 10 Sep 46) WF-L-7 JAN 47 300M SECRET HEADQUARTERS AIR MATERIEL
COMMAND ... SAVE IN REPLY ADDRESS BOTH. COMMUNICATION AND ENVELOPE TO COMMANDING GENERAL, AIR
MATERIEL COMMAND ... Intelligence Department of this Command is continuing the collection ...
```

(Illustrative: the exact output depends on the model.) What the LLM is asked to do:

- Search each page for words interrupted by a hyphen, space, line break or other stray character, and
  re-join them when the joined form is a real word or a recognizable name.
- Remove extra spaces and spaces around punctuation, parentheses, slashes and colons.
- Keep real hyphens: compound words (`AIR-SEA`) and identifiers with digits (`TSNAD-2B`, `V-32291-8`).
- Never correct spelling, and never add, remove or reorder a word, number or letter.
- If it is unsure about a split, leave it and report it.

The safety check: a page's new text is accepted only if its digits are identical to the
original and its letters are at least 97% similar (ignoring case). So OCR letter errors can be fixed,
but numbers never change and the text cannot be rewritten. If a
page fails the check, it is retried once; if it still fails, the page keeps its original text and the
log names the page number. Splits the model was unsure about are saved on the page as `cleanupNotes`.
Every slice gets `document.textStandardizedAt`.

If the LLM is not configured, grouping falls back to heuristics and the text is left unchanged, with a
warning in the log.

## What a slice file looks like

```json
{
  "schema": "curated/1",
  "document": {
    "fileName": "..._Vol_2__p01-02.curated.json",
    "sourceCurated": "..._Vol_2.curated.json",
    "pageRange": { "from": 1, "to": 2 },
    "textStandardizedAt": "2026-11-07T10:15:00+00:00"
  },
  "pages": [
    { "pageNumber": 1, "text": "...cleaned text...", "cleanupNotes": ["optional unsure splits"] }
  ]
}
```

## Where results go

- Analysis JSON: `RESULTS_CONTAINER` (default `pdf-results`), as `<RESULTS_PREFIX>/<path>/sample.json`
  (no prefix if `RESULTS_PREFIX` is empty).
- Reviewed file: `<doc>.curated.json` in the same container.
- Slices: `SLICED_CONTAINER` (default `pdf-curated-sliced`), as `<stem>__pNN[-NN].curated.json` in the
  same folder as the curated file. Each page keeps its `text`/`markdown`, but the per-page `words`
  array is left out; it stays in the full curated file.

All containers must already exist. The tool does not create them.

## Settings

Put these in your shell or in a `.env` file next to `main.py`. Order of priority: command-line flags,
then real environment variables, then `.env`.

### To analyze PDFs (required)

- `AZURE_CONTENT_UNDERSTANDING_ENDPOINT`
- `ANALYZER_ID`
- `PDFS_CONTAINER` (or pass `--pdfs-container`)
- Storage login, one of:
  - `AZURE_STORAGE_CONNECTION_STRING`
  - `AZURE_STORAGE_ACCOUNT_URL` (uses your Azure login via `DefaultAzureCredential`)

### To analyze PDFs (optional)

- `CONTENT_UNDERSTANDING_KEY` - if missing, your Azure login is used instead.
- `API_VERSION` - default `2025-11-01`.
- `PDFS_PREFIX` - only scan this folder inside `PDFS_CONTAINER`.
- `RESULTS_CONTAINER` - default `pdf-results`.
- `RESULTS_PREFIX` - folder prefix for result JSON.
- `WORKERS` - how many PDFs at once, default `4`.
- `CONFIDENCE_THRESHOLD` - words below this go to review, default `0.8`.
- `REVIEW_APP_URL` - default `https://uap-pdf-review.azurewebsites.net/`.

### Slice stage

- `SLICED_CONTAINER` - where finished slices go, default `pdf-curated-sliced`.
- `LLM_GROUPING_MODEL` - default `gpt-4.1-nano`. Used for grouping pages.
- `CLEANUP_MODEL` - model for the text fix; defaults to `LLM_GROUPING_MODEL`. Use a stronger model
  here if split words are being missed.
- `LLM_GROUPING_BASE_URL` - default `https://foundryforall.services.ai.azure.com/models`.
- `LLM_GROUPING_API_KEY` - optional for Foundry endpoints (your Azure login is used).
- `LLM_GROUPING_API_VERSION` - only sent when set.
- `LLM_GROUPING_BATCH` - pages per LLM call (grouping and text fix), default `10`.
- `LLM_GROUPING_DELAY` - seconds between calls, default `75` (avoids `429` rate limits).

### How the tool logs in

- Content Understanding: uses `CONTENT_UNDERSTANDING_KEY` if set, otherwise `DefaultAzureCredential`.
- Blob Storage: uses `AZURE_STORAGE_CONNECTION_STRING` if set, otherwise `AZURE_STORAGE_ACCOUNT_URL`
  with `DefaultAzureCredential`.
- LLM (Foundry endpoints): `LLM_GROUPING_API_KEY` if set, otherwise your `az login`.

## Command-line options

Run `python main.py --help` for the full list.

### Analyze

- `--pdfs-container`, `--pdfs-prefix`, `--results-container` - override the matching settings.
- `--force` - redo work even if a result already exists (analyze), or re-slice files that already
  have slices in the sliced container.
- `--concurrency` - PDFs at once (overrides `WORKERS`).
- `--analyzer-id`, `--api-version`, `--endpoint` - override the matching settings.
- `--review-app-url` - override `REVIEW_APP_URL`.
- `--confidence-threshold` - between 0 (exclusive) and 1.

### Slice

- `--slice-only` - run only the slice step.
- `--slice-dry-run` - do everything, upload nothing.
- `--slice-groups "1-2,3,4-6"` - set the groups yourself (must cover every page exactly once).
- `--slice-auto` - slice right after analyze, for files already reviewed.
- `--sliced-container` - override `SLICED_CONTAINER`.
- `--slice-llm` - no longer needed (accepted and ignored); the LLM is always used.

## More examples

Analyze a specific folder with more parallelism:

```bash
python main.py --pdfs-container pdfs --pdfs-prefix contracts/2026 --concurrency 8
```

Redo every analysis:

```bash
python main.py --force
```

Slice with your own page groups:

```bash
python main.py --slice-only --slice-groups "1-2,3,4-6"
```

Re-slice and re-clean everything, even files already in `pdf-curated-sliced`:

```bash
python main.py --slice-only --force
```

At the end of a slice run you get a summary:

```text
Slice summary:
  sliced:   2 (15 files)
  skipped:  1
  failed:   0
  dry-run:  no
  target:   pdf-curated-sliced
```

## Troubleshooting

- **"My file was skipped."** Analyze skips PDFs that already have a result; slicing skips curated
  files that already have slices in `pdf-curated-sliced`. Add `--force` to redo them.
- **Slicing fails with a container error.** `pdf-results` and `pdf-curated-sliced` must already exist.
- **Words are still split in the output (`EN - VELOPE`).** The model missed them. Check the log for
  `Text cleanup: LLM adjusted N of M page(s)` and for pages kept unchanged. Try a stronger
  `CLEANUP_MODEL`, then re-run with `--force`.
- **"Text cleanup skipped: LLM not configured".** Set `LLM_GROUPING_API_KEY`, or use a Foundry
  endpoint (`*.services.ai.azure.com`) and run `az login`.
- **`429` rate-limit errors.** Raise `LLM_GROUPING_DELAY`.
- **The run is slow at the start.** `DefaultAzureCredential` first probes the Azure metadata endpoint,
  which only exists inside Azure. On a desktop this can take a minute. Run `az login` first.
- **I want to try without changing anything.** Use `--slice-dry-run`.

## Notes

- Slicing never modifies the original curated file. It only writes new files to `pdf-curated-sliced`.
- The "already sliced" check is what makes slice runs resumable.
- SDK bytes input uses `AnalysisInput(data=..., mime_type="application/pdf")` with the installed
  prerelease `azure-ai-contentunderstanding` package.
