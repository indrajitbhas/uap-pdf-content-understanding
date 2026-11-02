# PDF Content Understanding Workflow

A command-line tool that takes scanned PDFs (here: historical Army Air Forces correspondence) and
turns them into clean, searchable JSON, in four steps:

1. **Analyze** - read each PDF with Azure AI Content Understanding (OCR + layout).
2. **Review** - a person fixes low-confidence words in a web app.
3. **Slice** - split each reviewed file into one JSON per letter/memo/form.
4. **Enrich** - add the details a researcher would look for (who wrote to whom, which file number,
   which index code, who saw what...) so the search index can use them.

Everything lives in Azure Blob Storage. The tool reads PDFs from one container and writes JSON to
another. It never scans local folders.

It is built on [Microsoft Agent Framework workflows](https://learn.microsoft.com/en-gb/agent-framework/get-started/workflows?pivots=programming-language-python).
Each step below is a small "workflow": a chain of nodes that each do one job.

## The pipeline at a glance

```
 PDFs in Blob Storage
        |
        v
 [1 ANALYZE]   python main.py
        |         list_pdf_blobs -> analyze_document -> save_result -> user_review
        v
 [2 REVIEW]    a human fixes doubtful words in the review app   (outside this tool)
        |         result: <doc>.curated.json
        v
 [3 SLICE]     python main.py --slice-only
        |         list_curated_blobs -> slice_curated
        |         result: <doc>__p26-27.curated.json  (one file per communication)
        v
 [4 ENRICH]    python main.py --enrich-only
                  list_slices -> extract_page_details -> standardize_details -> save_enriched
                  result: the SAME slice file, now with extra fields
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
python main.py --slice-only           # 3. after review: split into communications
python main.py --enrich-only          # 4. add research details (no LLM needed)
python main.py --enrich-only --enrich-llm   # 4. same, but let an LLM fill in more
```

Try before you commit - these do the work but upload nothing:

```bash
python main.py --slice-only --slice-dry-run
python main.py --enrich-only --enrich-dry-run
```

You can also enrich a single local file without touching Azure:

```bash
python enrich_slice.py --input "slices_test/some_slice.curated.json" --output-dir out --dry-run
```

(`--output-dir` writes a copy there. Without it, the input file is overwritten in place.
`--dry-run` prints the result and writes nothing.)

## What each node does

| Step | Node | In plain words |
|---|---|---|
| Analyze | `list_pdf_blobs` | Finds `*.pdf` files in `PDFS_CONTAINER`. Skips any that already have a result (unless `--force`). |
| Analyze | `analyze_document` | Sends the PDF to Content Understanding (`ANALYZER_ID`). Several run at once, limited by `WORKERS`. |
| Analyze | `save_result` | Saves the analysis JSON in `RESULTS_CONTAINER` (default `pdf-results`). |
| Analyze | `user_review` | Lists every word with confidence below `CONFIDENCE_THRESHOLD` (default 0.8) and points to the review app. |
| Slice | `list_curated_blobs` | Finds reviewed `*.curated.json` files. Ignores files that are already slices or backups. |
| Slice | `slice_curated` | Groups consecutive pages that belong to the same communication and saves one file per group, named `<stem>__pNN[-NN].curated.json`. |
| Enrich | `list_slices` | Finds slice files. Skips ones already enriched (marked by `document.enrichedAt`) unless `--force`. |
| Enrich | `extract_page_details` | Reads each slice and collects per-page details plus the document-level `research` block. With `--enrich-llm` this is **one LLM call per slice**; rules always run too. |
| Enrich | `standardize_details` | Cleans up: acronym glossary, ISO dates, canonical place names, a `standardText` per page, and assembles the final `research` block. |
| Enrich | `save_enriched` | Writes the result back into the same file/blob and stamps `document.enrichedAt`. With `--enrich-dry-run` it only logs. |

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

## What enrichment adds: a before/after example

Take the two-page slice `...__p26-27.curated.json` from the "General 1946-7 Vol 2" file. Page 26 is
a Routing and Record Sheet dated 22 Aug 1947 ("Comment No. 1", signed by a colonel, describing
disc-shaped objects). Page 27 is a Form MR-1 transmittal sheet dated 5 Aug 47 that says
`INDEX: 000.9 Phenomena`, `FILED: 333.5 Investigation`, `INDEXER: ejb`.

**Before** (what slicing produced - OCR text and words only):

```json
{
  "schema": "curated/1",
  "document": { "...": "..." },
  "pages": [
    { "pageNumber": 27, "text": "... Form MR-1 ... INDEX : 000.9 Phenomena ... FILED : 333.5 Investigation ... INDEXER : ejb ..." }
  ]
}
```

**After** (same file, extra fields added; shortened here for reading):

```json
{
  "schema": "curated/1",
  "document": { "...": "...", "enrichedAt": "2026-11-02T10:15:00+00:00" },
  "pages": [
    {
      "pageNumber": 27,
      "text": "...original text, untouched...",
      "enrichment": { "docType": "form", "date": "5 Aug 47", "persons": [], "acronyms": [] },
      "standardText": "...same text, acronyms spelled out on first use..."
    }
  ],
  "normalized": { "acronyms": {}, "dates": [], "places": {}, "standardization": "rules" },
  "research": {
    "v": 2,
    "recordsManagement": {
      "formNumbers": ["MR-1"],
      "indexCodes": [{ "code": "000.9", "label": "Phenomena" }],
      "transmittalForms": [{ "filed": "333.5 Investigation", "indexer": "ejb", "pages": [27] }]
    },
    "correspondenceLineage": { "chains": ["... one entry per letter / indorsement / routing comment ..."] },
    "...": "..."
  }
}
```

This example is illustrative and abbreviated: it shows where each fact lands, not the exact full
output. Values such as the `enrichedAt` timestamp will differ. The original text and word data are
never changed; enrichment only adds fields.

Another real case: the Merchant letter (pages 19-20) names Mrs. Madeline Gwynne Merchant of Santa
Fe. She appears once under `research.entities.persons` with her pages, even though her name is
spread over two pages.

## The `research` block, field by field

`research` sits at the top level of each slice, next to `pages`. When a fact is not in the text, the
field is empty (`null`, `""` or `[]`). Nothing is guessed. Witness credibility is never judged; if
a document itself says something about reliability, it is quoted in `statedReliability`.

| Section | What is in it | Why a researcher cares |
|---|---|---|
| `archivalIdentity` | Classification marks, declassification authority (for example `NND 700188`), restrictions. | Tells you how the record was handled and when it was released. |
| `recordsManagement` | Form numbers and print editions, office symbols, drafter initials, control and file numbers, index codes (`000.9 Phenomena`), mail stamps, suspense dates, transmittal-form fields, copies, enclosures. | These are the identifiers archivists and other researchers cite. They let you find the same document in other collections. |
| `correspondenceLineage` | The chain of letter -> indorsements -> routing comments, with dates, senders, addressees, actions taken, and references to other documents. | Shows who asked what and who answered, in order. |
| `entities` | People (with rank, role, organization), organizations, units, places. Same person across pages is merged. | Lets you search "everything involving this person or base". |
| `sightings` | One entry per reported event: when, where, witnesses, object shape/size/speed/maneuvers, instruments. Uses fixed word lists plus a free-text `summary`. | Makes reports comparable and filterable. |
| `evidence` | Photographs, drawings, radar records, and what was found or concluded. | Separates the claim from the physical evidence. |
| `evaluations` | Stated conclusions (explained, unresolved, concurred...) and who gave them. | Shows how officials judged the case. |
| `historicalRefs` | Other documents, projects, foreign actors and catalog numbers mentioned. | Helps you follow leads to other files. |
| `topics` | Short tags for the main subjects. | Quick filters and facets. |
| `quality` | Examples of garbled OCR and a short note. | Warns you where the text may be unreliable. |

Not filled in yet (left `null`): geographic coordinates and NARA identifiers (record group, NAID,
microfilm). These are planned for a later step.

## With and without an LLM

| | Without `--enrich-llm` | With `--enrich-llm` |
|---|---|---|
| Cost | Free, instant | One model call per slice |
| Identifiers (form numbers, office symbols, index codes, stamps, control numbers, declassification authority) | Read by rules | Still read by rules - they stay authoritative |
| Summaries, people, sightings, evidence, lineage, topics | Only the simple rule-based details (date, classification, acronyms, places) | Filled in by the model |
| If it fails | n/a | The tool logs a warning and falls back to rules, so the stage still finishes |

How the LLM call works:

- **One call per file.** All pages of a slice go in together, and the model returns the per-page
  details and the whole `research` block in a single JSON answer.
- Slices with more pages than `ENRICH_BATCH` (default 12) are split into chunks, one call each.
  Most slices are 1-6 pages, so this is rare.
- Pages are matched back by page number. If the answer is malformed JSON, the tool falls back to
  rules instead of failing.

## Where results go

- Analysis JSON: `RESULTS_CONTAINER` (default `pdf-results`), as `<RESULTS_PREFIX>/<path>/sample.json`
  (no prefix if `RESULTS_PREFIX` is empty).
- Reviewed file: `<doc>.curated.json` in the same container.
- Slices: `<stem>__pNN[-NN].curated.json`.
- Enrichment: written **into the slice itself**, overwriting the same blob. No new file is created.
  `document.enrichedAt` marks it as done.

Both containers must already exist. The tool does not create them.

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

### Slice stage (optional LLM grouping, only with `--slice-llm`)

- `LLM_GROUPING_MODEL` - default `gpt-4.1-nano`.
- `LLM_GROUPING_BASE_URL` - default `https://foundryforall.services.ai.azure.com/models`.
- `LLM_GROUPING_API_KEY` - optional for Foundry endpoints (your Azure login is used).
- `LLM_GROUPING_API_VERSION` - only sent when set.
- `LLM_GROUPING_BATCH` - pages per call, default `10`.
- `LLM_GROUPING_DELAY` - seconds between calls, default `75` (avoids `429` rate limits).

### Enrich stage (only with `--enrich-llm`)

- `ENRICH_MODEL` - default `gpt-4.1-nano`.
- `ENRICH_BASE_URL` - default `https://foundryforall.services.ai.azure.com/models`.
- `ENRICH_API_KEY` - optional for Foundry endpoints (your Azure login is used); needed for other OpenAI-compatible endpoints.
- `ENRICH_BATCH` - maximum pages per LLM call, default `12`.
- `ENRICH_DELAY` - seconds to wait between calls when a slice is split into chunks, default `10`.

### How the tool logs in

- Content Understanding: uses `CONTENT_UNDERSTANDING_KEY` if set, otherwise `DefaultAzureCredential`.
- Blob Storage: uses `AZURE_STORAGE_CONNECTION_STRING` if set, otherwise `AZURE_STORAGE_ACCOUNT_URL`
  with `DefaultAzureCredential`.

## Command-line options

Run `python main.py --help` for the full list.

### Analyze

- `--pdfs-container`, `--pdfs-prefix`, `--results-container` - override the matching settings.
- `--force` - redo work even if a result already exists (analyze and enrich).
- `--concurrency` - PDFs at once (overrides `WORKERS`).
- `--analyzer-id`, `--api-version`, `--endpoint` - override the matching settings.
- `--review-app-url` - override `REVIEW_APP_URL`.
- `--confidence-threshold` - between 0 (exclusive) and 1.

### Slice

- `--slice-only` - run only the slice step.
- `--slice-dry-run` - work out the groups without uploading.
- `--slice-llm` - let a cheap LLM decide the groups instead of heuristics.
- `--slice-groups "1-2,3,4-6"` - set the groups yourself (must cover every page exactly once).
- `--slice-auto` - slice right after analyze, for files already reviewed.

### Enrich

- `--enrich-only` - run only the enrich step.
- `--enrich-dry-run` - compute everything, upload nothing.
- `--enrich-llm` - use the LLM (one call per slice).
- `--enrich-auto` - run enrich right after slice (`--slice-only --enrich-auto`, or with `--slice-auto`).

## More examples

Analyze a specific folder with more parallelism:

```bash
python main.py --pdfs-container pdfs --pdfs-prefix contracts/2026 --concurrency 8
```

Redo every analysis:

```bash
python main.py --force
```

Slice with your own page groups, then enrich in one go:

```bash
python main.py --slice-only --slice-groups "1-2,3,4-6" --enrich-auto
```

Re-enrich everything with the LLM, even files already enriched:

```bash
python main.py --enrich-only --enrich-llm --force
```

At the end of an analyze run you get a summary:

```text
Summary:
  needs review: X
  ok:           Y
  skipped:      Z
  failed:       W
```

## Troubleshooting

- **"My file was skipped."** Analyze skips PDFs that already have a result; enrich skips slices
  that already have `document.enrichedAt`. Add `--force` to redo them.
- **`--enrich-llm` seems to do nothing / "falling back to rules".** The LLM is unusable without
  `ENRICH_API_KEY` or a Foundry endpoint (`*.services.ai.azure.com`) with a working Azure login
  (`az login`). Check the log for a warning. The stage still finishes using rules.
- **`429` rate-limit errors.** Raise `ENRICH_DELAY` (or `LLM_GROUPING_DELAY` for slicing).
- **"Where did the enrichment go?"** Into the slice file itself. Open `<stem>__pNN.curated.json`
  and look for `research`, `normalized` and per-page `enrichment`.
- **I want to try without changing anything.** Use `--slice-dry-run`, `--enrich-dry-run`, or
  `python enrich_slice.py --input <file> --output-dir out`.
- **The enrich stage finds nothing.** Slices must exist first (run `--slice-only`), and the file
  names must look like `<stem>__pNN[-NN].curated.json`.
- **Container errors.** `PDFS_CONTAINER` and `RESULTS_CONTAINER` must already exist.

## Notes

- Enrichment never modifies the original text or word data. It only adds fields.
- The "result already exists" check is what makes runs resumable.
- SDK bytes input uses `AnalysisInput(data=..., mime_type="application/pdf")` with the installed
  prerelease `azure-ai-contentunderstanding` package.
