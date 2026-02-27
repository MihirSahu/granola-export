# Granola Export — Technical Notes

## Overview

Reverse-engineered Granola's local data storage and API to export all meeting notes as Markdown files. Starting from a basic script that fetched 100 notes, iterated to a solution that exports 207/210 documents with proper formatting.

## Starting Point

The original export script came from Joseph Thacker's blog post: https://josephthacker.com/hacking/2025/05/08/reverse-engineering-granola-notes.html

Limitations of the original script:
- **No pagination** — only fetched 100 notes (single request with `limit: 100`)
- **Basic ProseMirror converter** — only handled `heading`, `paragraph`, `bulletList`, and `text` node types
- **No content fallback** — only checked `last_viewed_panel.content`, missed documents where content was stored elsewhere
- **Stale token risk** — no guidance on token expiration
- **Flat output** — all files dumped into a single directory with no date organization

## API Discovery

### Authentication

Granola stores WorkOS tokens in `~/Library/Application Support/Granola/supabase.json`. The file contains a `workos_tokens` field (JSON string) with an `access_token` used as a Bearer token.

### Endpoints Used

| Endpoint | Method | Purpose |
|---|---|---|
| `POST /v2/get-documents` | Bulk fetch | Returns document metadata + `last_viewed_panel` content. Supports `limit`, `offset`, `include_last_viewed_panel` params. |
| `POST /v1/get-document-panels` | Per-document | Returns panel content as **HTML strings**. Takes `document_id`. |

### Endpoints That Failed

| Endpoint | Result | Notes |
|---|---|---|
| `POST /v2/get-document` | 404 | Does not exist. Was a guess based on the plural endpoint name. |
| `POST /v1/get-ydoc` | 403 | Exists but the WorkOS access token lacks the required scope. |

### Headers

The User-Agent and X-Client-Version must match a real Granola client version. Found `7.41.2` by extracting the Electron app's `app.asar`. Responses are gzip-encoded — `requests` handles this automatically, but raw `urllib` and `curl` need explicit handling.

## Content Storage Architecture

Granola uses **three layers** of content storage:

### 1. ProseMirror JSON (via `/v2/get-documents`)

The `last_viewed_panel.content` field contains ProseMirror document nodes. Only ~95/210 documents had actual text content here. The remaining 115 had structurally valid but empty ProseMirror docs:

```json
{"type": "doc", "content": [{"type": "paragraph", "attrs": {"id": "A897A494-..."}}]}
```

Node types found: `doc`, `paragraph`, `heading`, `bulletList`, `listItem`, `text`.

Content can appear in multiple locations within the document JSON:
- `doc.last_viewed_panel.content`
- `doc.panels[*].content`
- `doc.content`
- `doc.notes`

### 2. HTML Panels (via `/v1/get-document-panels`)

Panels contain AI-generated meeting summaries stored as **HTML strings** (not ProseMirror JSON). This was the key discovery — the panel `content` field type differs between the list endpoint (ProseMirror dict) and the panels endpoint (HTML string).

113/210 documents had HTML content via this endpoint. Combined with ProseMirror content from the list API, this covered 207/210 documents.

Panel response structure:
```json
{
  "document_id": "...",
  "id": "...",
  "title": "Summary",
  "content": "<h3>Meeting Title</h3><ul><li><p><strong>Key Point</strong></p>...</li></ul>",
  "original_content": "...",
  "ydoc_version": null,
  "generated_lines": [...]
}
```

### 3. Yjs Documents (local OPFS storage)

Granola uses Yjs (CRDT library) for collaborative editing. Feature flag `flag_ydoc_sqlite_storage: true` confirms SQLite-backed YDoc storage.

The local database is stored in Chromium's Origin Private File System (OPFS) at:
```
~/Library/Application Support/Granola/File System/000/t/00/
```

Files found:
- `00000001` (3.7MB) — virtual path `/granola.db`
- `00000003` (5.6MB) — virtual path `/granola.db-wal`

**OPFS SAH Pool format**: First 512 bytes contain the virtual filesystem path, followed by metadata, then the actual database content. The SQLite magic header (`SQLite format 3\000`) was not found — the database is either encrypted (SQLCipher) or uses a custom page format. Could not extract usable data from these files.

### 4. Local Cache (`cache-v4.json`)

`~/Library/Application Support/Granola/cache-v4.json` (664KB) contains a full document index under `cache.state.documents` (212 entries). Each document has a `notes` field with ProseMirror JSON, but these were the same empty paragraph structures as the API response. Fields `notes_markdown` and `notes_plain` were mostly empty (only 2/212 had `notes_markdown`).

## App Source Extraction

Extracted the Electron app's ASAR archive to discover API endpoints:

```bash
npx asar extract /Applications/Granola.app/Contents/Resources/app.asar /tmp/granola-app
```

Found 289 API endpoints in the minified JS bundles. Key file: `dist-app/assets/index-Bh0XU8df.js`.

## HTML-to-Markdown Conversion Issues

### Problem: Excessive Newlines

Granola's HTML wraps list item content in `<p>` tags:

```html
<ul>
<li>
<p><strong>Bold Item</strong></p>
<ul>
<li>Sub item one</li>
</ul>
</li>
</ul>
```

The initial HTML parser treated every `<p>` open/close as a paragraph break, producing:

```markdown
-
**Bold Item**

  - Sub item one
```

### Root Cause

Two issues:
1. `<p>` tags inside `<li>` were flushing the current line, separating the bullet marker from its content
2. Whitespace text nodes (newlines in the HTML between tags) were being appended to the output

### Fix

1. Skip `<p>` start/end tag handling when inside a list (`self._in_list()`)
2. Discard whitespace-only text nodes inside lists
3. Changed `_flush_line()` to use `rstrip()` instead of `strip()` to preserve leading indent/markers like `- ` and `  - `
4. Added post-processing to collapse consecutive blank lines

Result:
```markdown
- **Bold Item**
  - Sub item one
```

## File Organization

Output structure: `YYYY/MM/DD/YYYY-MM-DD_Title.md`

### Duplicate Filename Handling

Two untitled documents on the same date (2025-07-04) produced identical filenames. Fixed by falling back to the first 8 characters of the document UUID when the title is empty:

```python
safe_title = sanitize_filename(title)
if not safe_title:
    safe_title = doc_id[:8]
```

## Final Results

| Metric | Count |
|---|---|
| Total API documents | 210 |
| Exported with content | 207 |
| Skipped (truly empty) | 3 |
| Source: ProseMirror (list API) | ~95 |
| Source: HTML panels (panels API) | ~113 |
| Synced to S3 | 208 (207 + INDEX.md) |

The 3 skipped documents had no content in either the ProseMirror or HTML panel sources — likely meetings where no notes were taken.

## Dead Ends & Failed Approaches

### 1. `/v2/get-document` (single document fetch)

Assumed the bulk `/v2/get-documents` endpoint would have a singular counterpart for fetching individual documents with full content. It returned 404 — the endpoint simply doesn't exist.

### 2. Local SQLite extraction

Spent significant time trying to extract the OPFS-backed `granola.db`. Attempted multiple header offsets (512, 1024, 4096 bytes) to find the SQLite data after the OPFS SAH Pool header. No SQLite magic bytes were found anywhere in the 3.7MB file. The database is likely encrypted or uses a non-standard page format.

### 3. IndexedDB / LevelDB

Checked `~/Library/Application Support/Granola/IndexedDB/file__0.indexeddb.leveldb/` for Yjs document updates. The LevelDB contained only key-value metadata (`last_flush_timestamp`, etc.) — no document content.

### 4. `notes_markdown` field in cache

The local cache (`cache-v4.json`) has a `notes_markdown` field on each document, which seemed promising. Only 2 out of 212 documents had it populated — Granola apparently doesn't pre-render markdown for most notes.

### 5. ProseMirror content from cache vs API

The local cache's `notes` field contained ProseMirror JSON for all 212 documents, but they were the same empty paragraph stubs as the API. The cache mirrors the API, not the rendered content.

## Content Overlap Analysis

The two working content sources are complementary, not redundant:

```
ProseMirror only (list API):   ~94 docs  ← user-edited notes
HTML panels only (panels API): ~113 docs ← AI-generated summaries
Both sources:                  ~1 doc
Neither source:                3 docs    ← truly empty
```

Documents with ProseMirror content tend to be older (pre-YDoc migration). Newer documents store their user-edited content in YDocs (inaccessible) but have AI-generated panel summaries available via the panels API.

## Potential Future Improvements

- **Token refresh**: The WorkOS access token will eventually expire. Could add automatic refresh using the `refresh_token` from `supabase.json`.
- **Incremental sync**: Track `updated_at` timestamps to only re-export changed documents instead of doing a full export each run.
- **YDoc decryption**: If the OPFS SQLite encryption key can be found (likely in the Electron app's main process or Keychain), the full YDoc content could be extracted, potentially recovering the 3 empty documents.
- **Transcript export**: The API has a `/v1/get-document-transcript` endpoint. Could export raw meeting transcripts alongside the summaries.
- **Multiple panels**: Some documents may have multiple panels (different summary templates). Currently all panels are concatenated — could be split into separate sections or files.

## File Structure

```
granola-export/
├── granola_export.py      # Main export script
├── requirements.txt       # Dependencies (requests)
├── TECHNICAL_NOTES.md     # This file
├── .gitignore             # Excludes log, cache, venv
└── granola-notes/         # Output directory (generated)
    ├── INDEX.md           # All notes listed by date (newest first)
    └── YYYY/MM/DD/        # Date-organized folders
        └── YYYY-MM-DD_Title.md
```
