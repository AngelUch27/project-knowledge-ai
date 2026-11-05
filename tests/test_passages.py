from backend.passages import _extract_text, select_passages

DOCUMENT = """# Project Knowledge AI

A private document Q&A demo.

## Run the application locally

py -m venv .venv
Copy-Item .env.example .env

## Authentication

Authenticate with gcloud auth login and gcloud auth application-default login before starting.

## Upload and automatic indexing

Supported formats are PDF, DOCX, PPTX, XLSX, XLSM, TXT, MD, HTML, and JSON.
"""


def test_select_passages_prefers_chunks_sharing_terms_with_the_question():
    passages = select_passages(DOCUMENT, "which gcloud commands authenticate the application?", max_chars=400)
    joined = "\n".join(passages)
    assert "gcloud auth login" in joined
    assert len(joined) <= 400


def test_select_passages_keeps_document_order_and_respects_the_cap():
    passages = select_passages(DOCUMENT, "formats venv authenticate", max_chars=10_000)
    assert passages == [DOCUMENT.strip()] or DOCUMENT.index(passages[0]) < DOCUMENT.index(passages[-1])


def test_select_passages_falls_back_to_leading_text_when_nothing_matches():
    passages = select_passages(DOCUMENT, "zzzz qqqq", max_chars=10_000)
    assert passages and passages[0].startswith("# Project Knowledge AI")


def test_select_passages_returns_empty_for_empty_text():
    assert select_passages("", "anything at all", max_chars=100) == []


def test_extract_text_strips_html_and_decodes_entities():
    assert _extract_text(".html", b"<p>Tom &amp; Jerry</p>") .strip() == "Tom & Jerry"


def test_extract_text_reads_markdown_as_utf8():
    assert _extract_text(".md", "# Título".encode("utf-8")) == "# Título"


def test_extract_text_ignores_unknown_suffix():
    assert _extract_text(".exe", b"\x00\x01") == ""
