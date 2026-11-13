"""Build answer context from the documents Agent Search retrieved.

The Standard edition of Agent Search only returns short highlighted snippets.
Extractive segments and answers require the Enterprise edition, so instead this
module reads each retrieved object from the bucket, extracts its text, and keeps
the passages that best match the question. Document text is treated as data.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import io
import logging
import re
import time
import zipfile
from pathlib import PurePosixPath

from google.cloud import storage

logger = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
MAX_PDF_PAGES = 400
EXTRACT_TIME_BUDGET_S = 20.0
CHUNK_CHARS = 900
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[\w'-]{3,}", re.UNICODE)
_STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "are", "from", "which", "what", "when", "where",
    "how", "does", "did", "into", "your", "you", "have", "has", "was", "were", "will", "can",
    "about", "according", "please", "tell", "los", "las", "del", "que", "para", "por", "con",
    "una", "uno", "como", "cual", "cuales", "donde", "cuando", "sobre", "esta", "este", "son",
}


@dataclass(frozen=True)
class FetchedDocument:
    text: str | None
    metadata: dict[str, str]


def split_gcs_uri(gcs_uri: str) -> tuple[str, str] | None:
    """Return (bucket, object name) for a ``gs://bucket/name`` URI, else None."""
    if not gcs_uri.startswith("gs://"):
        return None
    bucket_name, _, object_name = gcs_uri[len("gs://"):].partition("/")
    if not bucket_name or not object_name:
        return None
    return bucket_name, object_name


def fetch_document(gcs_uri: str, project_id: str) -> FetchedDocument | None:
    """Download a supported bucket object; return its plain text and custom metadata."""
    parts = split_gcs_uri(gcs_uri)
    if parts is None:
        return None
    bucket_name, object_name = parts

    client = None
    try:
        client = storage.Client(project=project_id)
        blob = client.bucket(bucket_name).get_blob(object_name)
        if blob is None:
            return None
        metadata = dict(blob.metadata or {})
        if (blob.size or 0) > MAX_SOURCE_BYTES:
            return FetchedDocument(text=None, metadata=metadata)
        cache_key = f"{gcs_uri}#{blob.md5_hash or blob.generation}"
        cached = _TEXT_CACHE.get(cache_key)
        if cached is not None:
            return FetchedDocument(text=cached, metadata=metadata)
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
        return FetchedDocument(text=None, metadata=metadata)
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_TEXT_CHARS]
    _remember(cache_key, text)
    return FetchedDocument(text=text or None, metadata=metadata)


# Extracted text per object version: the same document is read again and again
# within a conversation, and PDF extraction is the slow part.
_TEXT_CACHE: dict[str, str] = {}
TEXT_CACHE_SIZE = 16


def _remember(key: str, text: str) -> None:
    if len(_TEXT_CACHE) >= TEXT_CACHE_SIZE:
        _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
    _TEXT_CACHE[key] = text


def object_metadata(gcs_uri: str, project_id: str) -> dict[str, str] | None:
    """The object's custom metadata; None when it no longer exists; {} on a transient error."""
    parts = split_gcs_uri(gcs_uri)
    if parts is None:
        return None
    client = None
    try:
        client = storage.Client(project=project_id)
        blob = client.bucket(parts[0]).get_blob(parts[1])
        return None if blob is None else dict(blob.metadata or {})
    except Exception:  # noqa: BLE001 - never hide a result because of a transient error
        logger.warning("Could not read metadata of %s", gcs_uri, exc_info=True)
        return {}
    finally:
        if client is not None:
            client.close()


def object_exists(gcs_uri: str, project_id: str) -> bool:
    """True when the object is still in the bucket; on any error assume it exists."""
    return object_metadata(gcs_uri, project_id) is not None


def fetch_document_text(gcs_uri: str, project_id: str) -> str | None:
    """Download a supported bucket object and return its plain text, or None."""
    document = fetch_document(gcs_uri, project_id)
    return document.text if document else None


def _extract_text(suffix: str, data: bytes) -> str:
    if suffix in {".txt", ".md", ".json"}:
        return data.decode("utf-8", errors="replace")
    if suffix == ".html":
        return html.unescape(_TAG.sub(" ", data.decode("utf-8", errors="replace")))
    if suffix == ".pdf":
        return _extract_pdf(data)
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


def _extract_pdf(data: bytes) -> str:
    """Page by page, stopping at the text budget, the page cap or the time budget.

    A 10 MB, several-hundred-page PDF took minutes to extract in full on a
    1-vCPU Cloud Run instance; the answer only needs the first MAX_TEXT_CHARS
    anyway, and a bounded extraction keeps every question under the request
    timeout.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    started = time.monotonic()
    parts: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        if index >= MAX_PDF_PAGES or total >= MAX_TEXT_CHARS or time.monotonic() - started > EXTRACT_TIME_BUDGET_S:
            logger.info("PDF extraction stopped at page %d (%d chars, %.1fs)", index, total, time.monotonic() - started)
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a broken page must not lose the rest
            text = ""
        parts.append(text)
        total += len(text)
    return "\n\n".join(parts)


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
