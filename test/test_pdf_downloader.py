import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.pdf_downloader import PDFDownloader, _extract_pdf_url_from_html, _extract_scihub_pdf_url


def test_determine_sources_all():
    dl = PDFDownloader()
    paper = {
        "title": "Test", "doi": "10.1234/test",
        "pdf_url": "http://arxiv.org/pdf/2001.00001",
        "oa_pdf_url": "https://example.com/oa.pdf",
    }
    sources = dl._determine_sources(paper)
    names = [s["name"] for s in sources]
    assert names[0] == "openalex_oa"  # Highest priority
    assert "arxiv" in names
    assert "sci-hub" in names


def test_determine_sources_doi_only():
    dl = PDFDownloader()
    paper = {"title": "Test", "doi": "10.1234/test"}
    sources = dl._determine_sources(paper)
    names = [s["name"] for s in sources]
    assert "unpaywall" in names
    assert "sci-hub" in names
    assert "publisher" in names


def test_determine_sources_no_info():
    dl = PDFDownloader()
    paper = {"title": "Test"}
    sources = dl._determine_sources(paper)
    assert len(sources) == 0


def test_cache_path(tmp_path):
    dl = PDFDownloader(cache_dir=tmp_path)
    paper = {"doi": "10.1234/test"}
    path = dl._cache_path(paper)
    assert path.parent == tmp_path
    assert path.suffix == ".pdf"


def test_cache_hit(tmp_path):
    import asyncio
    dl = PDFDownloader(cache_dir=tmp_path)
    paper = {"doi": "10.1234/test", "title": "Test"}
    cached = dl._cache_path(paper)
    cached.write_bytes(b"%PDF-1.4 fake")
    result = asyncio.get_event_loop().run_until_complete(dl.download(paper))
    assert result == cached


def test_extract_pdf_url_from_html():
    html = '<meta name="citation_pdf_url" content="https://example.com/paper.pdf">'
    assert _extract_pdf_url_from_html(html, "https://example.com") == "https://example.com/paper.pdf"

    html2 = '<a href="/download/paper.pdf">Download</a>'
    result = _extract_pdf_url_from_html(html2, "https://example.com")
    assert result is not None
    assert "example.com" in result


def test_extract_scihub_pdf_url():
    html = '<embed src="https://sci-hub.se/storage/paper.pdf">'
    assert _extract_scihub_pdf_url(html, "https://sci-hub.se") is not None

    html2 = '<iframe src="/storage/12345.pdf"></iframe>'
    result = _extract_scihub_pdf_url(html2, "https://sci-hub.se")
    assert result is not None
