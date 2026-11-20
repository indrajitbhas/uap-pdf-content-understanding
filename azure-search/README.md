# Azure AI Search definitions (Tier 1 enrichment)

Search service: `uapdocumentssearchengine`. One curated JSON blob = one search record (no Text Split, no index projections).

| File | Apply to | How |
|---|---|---|
| `index-fields-tier1.json` | Index `index1795026330751` | Append the 7 objects to the end of the existing `fields` array (adding fields is allowed; changing existing ones is not). |
| `skillset.json` | Skillset `uap-combine-content` | Replace the whole definition. Put the real function key in place of `<FUNCTION_KEY>` in the portal only, never commit it. |
| `indexer.json` | Indexer `search-1794236321638-indexer` | Replace `skillsetName`, `targetIndexName`, `fieldMappings`, `outputFieldMappings`. |

Order: deploy the function → index → skillset → indexer → **Reset** indexer → **Run**.
The reset is required: the blobs have not changed, so a plain run skips all of them.
