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
