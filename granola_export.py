import argparse
import logging
import re
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import json
import requests

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('granola_sync.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

API_HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Granola/7.41.2",
    "X-Client-Version": "7.41.2"
}

def get_headers(token):
    headers = dict(API_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token}"
    return headers

def load_credentials():
    creds_path = Path.home() / "Library/Application Support/Granola/supabase.json"
    if not creds_path.exists():
        logger.error(f"Credentials file not found at: {creds_path}")
        return None

    try:
        with open(creds_path, 'r') as f:
            data = json.load(f)

        workos_tokens = json.loads(data['workos_tokens'])
        access_token = workos_tokens.get('access_token')

        if not access_token:
            logger.error("No access token found in credentials file")
            return None

        logger.debug("Successfully loaded credentials")
        return access_token
    except Exception as e:
        logger.error(f"Error reading credentials file: {str(e)}")
        return None

def fetch_all_documents(token, batch_size=100):
    url = "https://api.granola.ai/v2/get-documents"
    headers = get_headers(token)

    all_docs = []
    offset = 0

    while True:
        data = {
            "limit": batch_size,
            "offset": offset,
            "include_last_viewed_panel": True
        }

        try:
            logger.info(f"Fetching documents (offset={offset}, limit={batch_size})...")
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            api_response = response.json()
        except Exception as e:
            logger.error(f"Error fetching documents at offset {offset}: {str(e)}")
            return None

        if "docs" not in api_response:
            logger.error("API response format is unexpected - 'docs' key not found")
            return None

        docs = api_response["docs"]
        all_docs.extend(docs)
        logger.info(f"Fetched {len(docs)} documents (total so far: {len(all_docs)})")

        if len(docs) < batch_size:
            break

        offset += batch_size

    return all_docs

def fetch_document_panels(token, doc_id):
    """
    Fetch panels for a document. Panels contain the AI-generated summaries as HTML.
    """
    url = "https://api.granola.ai/v1/get-document-panels"
    headers = get_headers(token)
    data = {"document_id": doc_id}

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.debug(f"Failed to fetch panels for {doc_id}: {str(e)}")
        return None

class HTMLToMarkdownConverter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
        self.list_stack = []
        self.current_line = []
        self.in_li = False

    def _flush_line(self):
        text = ''.join(self.current_line)
        self.current_line = []
        # Strip trailing whitespace but preserve leading indent/markers
        return text.rstrip()

    def _in_list(self):
        return len(self.list_stack) > 0

    def handle_starttag(self, tag, attrs):
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            level = int(tag[1])
            flushed = self._flush_line()
            if flushed:
                self.result.append(flushed)
            self.current_line.append('#' * level + ' ')
        elif tag == 'p':
            # Inside a list item, <p> just continues the line
            if not self._in_list():
                flushed = self._flush_line()
                if flushed:
                    self.result.append(flushed)
        elif tag == 'ul':
            if self._in_list():
                # Nested list: flush current li text first
                flushed = self._flush_line()
                if flushed:
                    self.result.append(flushed)
            self.list_stack.append('ul')
        elif tag == 'ol':
            if self._in_list():
                flushed = self._flush_line()
                if flushed:
                    self.result.append(flushed)
            self.list_stack.append(('ol', 0))
        elif tag == 'li':
            self.in_li = True
            flushed = self._flush_line()
            if flushed:
                self.result.append(flushed)
            indent = '  ' * max(0, len(self.list_stack) - 1)
            if self.list_stack and isinstance(self.list_stack[-1], tuple):
                _, count = self.list_stack[-1]
                count += 1
                self.list_stack[-1] = ('ol', count)
                self.current_line.append(f'{indent}{count}. ')
            else:
                self.current_line.append(f'{indent}- ')
        elif tag in ('strong', 'b'):
            self.current_line.append('**')
        elif tag in ('em', 'i'):
            self.current_line.append('*')
        elif tag == 'code':
            self.current_line.append('`')
        elif tag == 'br':
            self.current_line.append('\n')
        elif tag == 'a':
            attrs_dict = dict(attrs)
            self._pending_href = attrs_dict.get('href', '')
            self.current_line.append('[')

    def handle_endtag(self, tag):
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            flushed = self._flush_line()
            if flushed:
                self.result.append(flushed)
                self.result.append('')
        elif tag == 'p':
            if not self._in_list():
                flushed = self._flush_line()
                if flushed:
                    self.result.append(flushed)
                    self.result.append('')
        elif tag in ('ul', 'ol'):
            if self.list_stack:
                self.list_stack.pop()
            if not self.list_stack:
                flushed = self._flush_line()
                if flushed:
                    self.result.append(flushed)
                self.result.append('')
        elif tag == 'li':
            self.in_li = False
            flushed = self._flush_line()
            if flushed:
                self.result.append(flushed)
        elif tag in ('strong', 'b'):
            self.current_line.append('**')
        elif tag in ('em', 'i'):
            self.current_line.append('*')
        elif tag == 'code':
            self.current_line.append('`')
        elif tag == 'a':
            href = getattr(self, '_pending_href', '')
            self.current_line.append(f']({href})')

    def handle_data(self, data):
        # Skip whitespace-only text nodes inside lists (HTML formatting newlines)
        if self._in_list() and not data.strip():
            return
        self.current_line.append(data)

    def get_markdown(self):
        flushed = self._flush_line()
        if flushed:
            self.result.append(flushed)
        # Collapse multiple blank lines into one
        lines = []
        prev_blank = False
        for line in self.result:
            is_blank = line == ''
            if is_blank and prev_blank:
                continue
            lines.append(line)
            prev_blank = is_blank
        return '\n'.join(lines).strip()

def convert_html_to_markdown(html_content):
    if not html_content or not isinstance(html_content, str):
        return ""
    converter = HTMLToMarkdownConverter()
    converter.feed(html_content)
    return converter.get_markdown()

def extract_prosemirror_content(doc):
    """
    Extract ProseMirror content from a document, checking multiple possible locations.
    """
    # Try last_viewed_panel first
    if doc.get("last_viewed_panel") and \
       isinstance(doc["last_viewed_panel"], dict) and \
       doc["last_viewed_panel"].get("content") and \
       isinstance(doc["last_viewed_panel"]["content"], dict) and \
       doc["last_viewed_panel"]["content"].get("type") == "doc":
        return doc["last_viewed_panel"]["content"]

    # Try panels array
    for panel in doc.get("panels", []):
        if isinstance(panel, dict) and \
           panel.get("content") and \
           isinstance(panel["content"], dict) and \
           panel["content"].get("type") == "doc":
            return panel["content"]

    # Try top-level content
    if doc.get("content") and \
       isinstance(doc["content"], dict) and \
       doc["content"].get("type") == "doc":
        return doc["content"]

    # Try notes field
    if doc.get("notes") and \
       isinstance(doc["notes"], dict) and \
       doc["notes"].get("type") == "doc":
        return doc["notes"]

    return None

def convert_prosemirror_to_markdown(content):
    if not content or not isinstance(content, dict) or 'content' not in content:
        return ""

    def process_node(node):
        if not isinstance(node, dict):
            return ""

        node_type = node.get('type', '')
        content = node.get('content', [])
        text = node.get('text', '')

        if node_type == 'heading':
            level = node.get('attrs', {}).get('level', 1)
            heading_text = ''.join(process_node(child) for child in content)
            return f"{'#' * level} {heading_text}\n\n"

        elif node_type == 'paragraph':
            para_text = ''.join(process_node(child) for child in content)
            return f"{para_text}\n\n"

        elif node_type == 'bulletList':
            items = []
            for item in content:
                if item.get('type') == 'listItem':
                    item_content = ''.join(process_node(child) for child in item.get('content', []))
                    items.append(f"- {item_content.strip()}")
            return '\n'.join(items) + '\n\n'

        elif node_type == 'text':
            return text

        return ''.join(process_node(child) for child in content)

    result = process_node(content)
    # Collapse multiple blank lines
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()

def sanitize_filename(title):
    invalid_chars = '<>:"/\\|?*'
    filename = ''.join(c for c in title if c not in invalid_chars)
    filename = filename.replace(' ', '_')
    return filename

def main():
    logger.info("Starting Granola sync process")
    parser = argparse.ArgumentParser(description="Fetch Granola notes and save them as Markdown files.")
    parser.add_argument("output_dir", type=str, help="The full path to the folder where notes should be saved.")
    args = parser.parse_args()

    output_path = Path(args.output_dir)
    logger.info(f"Output directory set to: {output_path}")

    output_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Ensured output directory exists: {output_path}")

    logger.info("Attempting to load credentials...")
    token = load_credentials()
    if not token:
        logger.error("Failed to load credentials. Exiting.")
        return

    logger.info("Credentials loaded successfully. Fetching documents from Granola API...")
    documents = fetch_all_documents(token)

    if documents is None:
        logger.error("Failed to fetch documents.")
        return

    logger.info(f"Successfully fetched {len(documents)} total documents from Granola")

    synced_count = 0
    skipped_count = 0
    saved_notes = []
    for doc in documents:
        title = doc.get("title", "Untitled Granola Note")
        doc_id = doc.get("id", "unknown_id")
        logger.info(f"Processing document: {title} (ID: {doc_id})")

        markdown_content = ""

        # Strategy 1: Try ProseMirror content from the list response
        pm_content = extract_prosemirror_content(doc)
        if pm_content:
            markdown_content = convert_prosemirror_to_markdown(pm_content)

        # Strategy 2: If ProseMirror content is empty, fetch panels (HTML content)
        if not markdown_content.strip():
            logger.debug(f"No ProseMirror content for '{title}', fetching panels...")
            panels = fetch_document_panels(token, doc_id)
            if panels and isinstance(panels, list):
                for panel in panels:
                    panel_content = panel.get("content", "")
                    if isinstance(panel_content, str) and panel_content.strip():
                        panel_title = panel.get("title", "")
                        panel_md = convert_html_to_markdown(panel_content)
                        if panel_md.strip():
                            if panel_title:
                                markdown_content += f"## {panel_title}\n\n"
                            markdown_content += panel_md + "\n\n"

        if not markdown_content.strip():
            skipped_count += 1
            logger.warning(f"Skipping document '{title}' (ID: {doc_id}) - no content found")
            continue

        try:
            frontmatter = f"---\n"
            frontmatter += f"granola_id: {doc_id}\n"
            escaped_title_for_yaml = title.replace('"', '\\"')
            frontmatter += f'title: "{escaped_title_for_yaml}"\n'

            if doc.get("created_at"):
                frontmatter += f"created_at: {doc.get('created_at')}\n"
            if doc.get("updated_at"):
                frontmatter += f"updated_at: {doc.get('updated_at')}\n"
            frontmatter += f"---\n\n"

            final_markdown = frontmatter + markdown_content

            created_at = doc.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                dt = datetime.now()

            day_dir = output_path / str(dt.year) / f"{dt.month:02d}" / f"{dt.day:02d}"
            day_dir.mkdir(parents=True, exist_ok=True)

            date_prefix = dt.strftime("%Y-%m-%d")
            safe_title = sanitize_filename(title)
            if not safe_title:
                safe_title = doc_id[:8]
            filename = f"{date_prefix}_{safe_title}.md"
            filepath = day_dir / filename

            logger.debug(f"Writing file to: {filepath}")
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(final_markdown)
            logger.info(f"Successfully saved: {filepath}")
            synced_count += 1
            saved_notes.append((dt, title, filepath.relative_to(output_path)))
        except Exception as e:
            logger.error(f"Error processing document '{title}' (ID: {doc_id}): {str(e)}")
            logger.debug("Full traceback:", exc_info=True)

    # Generate index file sorted by date (newest first)
    saved_notes.sort(key=lambda x: x[0], reverse=True)
    index_lines = ["# Granola Notes Index", ""]
    current_month = None
    for dt, title, rel_path in saved_notes:
        month_key = dt.strftime("%B %Y")
        if month_key != current_month:
            current_month = month_key
            index_lines.append(f"## {month_key}")
            index_lines.append("")
        date_str = dt.strftime("%Y-%m-%d")
        index_lines.append(f"- {date_str} — [{title}]({rel_path})")
    index_lines.append("")

    index_path = output_path / "INDEX.md"
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(index_lines))
    logger.info(f"Index file written to: {index_path}")

    logger.info(f"Sync complete. {synced_count} notes saved, {skipped_count} skipped (no content) out of {len(documents)} total documents in '{output_path}'")

if __name__ == "__main__":
    main()
