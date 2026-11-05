"""Build answer context from the documents Agent Search retrieved.

The Standard edition of Agent Search only returns short highlighted snippets.
Extractive segments and answers require the Enterprise edition, so instead this
module reads each retrieved object from the bucket, extracts its text, and keeps
the passages that best match the question. Document text is treated as data.
"""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile
from pathlib import PurePosixPath

from google.cloud import storage

logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
CHUNK_CHARS = 900
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[\w'-]{3,}", re.UNICODE)
_STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "are", "from", "which", "what", "when", "where",
    "how", "does", "did", "into", "your", "you", "have", "has", "was", "were", "will", "can",
    "about", "according", "please", "tell", "los", "las", "del", "que", "para", "por", "con",
    "una", "uno", "como", "cual", "cuales", "donde", "cuando", "sobre", "esta", "este", "son",
}


def fetch_document_text(gcs_uri: str, project_id: str) -> str | None:
    """Download a supported bucket object and return its plain text, or None."""
    if not gcs_uri.startswith("gs://"):
        return None
    bucket_name, _, object_name = gcs_uri[len("gs://"):].partition("/")
    if not bucket_name or not object_name:
        return None

    client = None
    try:
        client = storage.Client(project=project_id)
        blob = client.bucket(bucket_name).get_blob(object_name)
        if blob is None or (blob.size or 0) > MAX_SOURCE_BYTES:
            return None
        data = blob.download_as_bytes()
    except Exception:  # noqa: BLE001 - context is best effort; the caller falls back to snippets
        logger.warning("Could not read %s for answer context", gcs_uri, exc_info=True)
        return None
    finally:
        if client is not None:
            client.close()

    try:
        text = _extract_text(PurePosixPath(object_name).suffix.lower(), data)
    except Exception:  # noqa: BLE001
        logger.warning("Could not extract text from %s", gcs_uri, exc_info=True)
        return None
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT_CHARS] or None


def _extract_text(suffix: str, data: bytes) -> str:
    if suffix in {".txt", ".md", ".json"}:
        return data.decode("utf-8", errors="replace")
    if suffix == ".html":
        return html.unescape(_TAG.sub(" ", data.decode("utf-8", errors="replace")))
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        import docx

        document = docx.Document(io.BytesIO(data))
        parts = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(part for part in parts if part.strip())
    if suffix in {".pptx", ".xlsx", ".xlsm"}:
        return _office_xml_text(data)
    return ""


def _office_xml_text(data: bytes) -> str:
    """Crude text extraction for PPTX/XLSX: strip tags from slide and shared-string XML."""
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted(
            name for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            or name == "xl/sharedStrings.xml"
            or (name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        )
        for name in names:
            xml = archive.read(name).decode("utf-8", errors="replace")
            xml = re.sub(r"</(a:p|t|row|si)>", "\n", xml)
            parts.append(html.unescape(_TAG.sub(" ", xml)))
    return "\n".join(parts)


def select_passages(text: str, question: str, max_chars: int) -> list[str]:
    """Return the chunks of `text` that share the most terms with `question`, in document order."""
    terms = {w.lower() for w in _WORD.findall(question)} - _STOP_WORDS
    chunks = _chunk(text)
    if not chunks:
        return []
    if not terms:
        return _take(chunks, range(len(chunks)), max_chars)

    scored = []
    for index, chunk in enumerate(chunks):
        words = {w.lower() for w in _WORD.findall(chunk)}
        overlap = len(terms & words)
        if overlap:
            scored.append((overlap, -index, index))
    if not scored:
        return _take(chunks, range(len(chunks)), max_chars)
    scored.sort(reverse=True)
    chosen = sorted(index for _, _, index in scored[:8])
    return _take(chunks, chosen, max_chars)


def _chunk(text: str) -> list[str]:
    chunks, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
        while len(current) > CHUNK_CHARS * 2:
            chunks.append(current[:CHUNK_CHARS * 2])
            current = current[CHUNK_CHARS * 2:]
    if current:
        chunks.append(current)
    return chunks


def _take(chunks: list[str], indexes, max_chars: int) -> list[str]:
    selected, used = [], 0
    for index in indexes:
        chunk = chunks[index]
        if used + len(chunk) > max_chars:
            break
        selected.append(chunk)
        used += len(chunk)
    return selected
