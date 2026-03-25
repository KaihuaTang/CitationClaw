"""Smart multi-source PDF downloader — fused from PaperRadar + CitationClaw.

Core logic ported from PaperRadar's smart_download_pdf (proven high success rate).
Added: GS sidebar PDF link, GS "all versions" scraping, MinerU Cloud parse cache.

Download priority (tried in order):
  0. Cache (instant)
  1. GS sidebar PDF link (direct from Google Scholar)
  2. CVF open access (CVPR/ICCV/WACV direct URL construction)
  3. openAccessPdf / S2 direct (non-arxiv, non-doi)
  4. S2 page citation_pdf_url (publisher PDF from S2 HTML)
  5. DBLP conference lookup (NeurIPS/ICML/ICLR/AAAI)
  6. Sci-Hub (3 mirrors)
  7. arXiv PDF
  8. GS paper_link + smart transform (CVF/OpenReview/MDPI/IEEE/Springer/ACL)
  9. Publisher page + Chrome Cookie (IEEE stamp 3-hop, etc.)
 10. DOI redirect
 11. GS "All versions" page (scrape cluster for additional links)
"""
import hashlib
import re
import os
import asyncio
from pathlib import Path
from typing import Optional, List
from urllib.parse import urlparse, quote

import subprocess
DEFAULT_CACHE_DIR = Path("data/cache/pdf_cache")

# Sci-Hub mirrors
SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
]

# Publisher domains that may need Chrome cookies
_PUBLISHER_DOMAINS = [
    "ieeexplore.ieee.org",
    "dl.acm.org",
    "link.springer.com",
    "www.sciencedirect.com",
    "onlinelibrary.wiley.com",
]

# Friendly source labels for logging
_SOURCE_LABELS = {
    "gs_pdf": "GS侧栏PDF",
    "cvf": "CVF开放获取",
    "openaccess": "S2开放获取",
    "s2_page": "S2页面PDF",
    "dblp": "DBLP会议版",
    "scihub": "Sci-Hub",
    "arxiv": "arXiv",
    "gs_link": "GS论文链接",
    "publisher": "出版商+Cookie",
    "doi": "DOI跳转",
    "gs_versions": "GS版本页",
    "oa_pdf": "OpenAlex开放获取",
    "unpaywall": "Unpaywall",
    "scraper_smart": "ScraperAPI智能下载",
}

# ── Proxy detection (same as PaperRadar: skip socks, use HTTP) ─────────
_HTTP_PROXY = None
for _var in ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"]:
    _val = os.environ.get(_var, "")
    if _val and _val.startswith("http"):
        _HTTP_PROXY = _val
        break


# ── Chrome cookie injection ────────────────────────────────────────────
_cookie_cache: dict = {}


# Auto-detect Chrome profile with most cookies (= institution login profile)
_chrome_profile_path: Optional[str] = None


def _detect_chrome_profile() -> str:
    """Find the Chrome profile cookie file with the most IEEE cookies."""
    global _chrome_profile_path
    if _chrome_profile_path is not None:
        return _chrome_profile_path

    import glob
    chrome_dir = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if not os.path.exists(chrome_dir):
        _chrome_profile_path = ""
        return ""

    best = ""
    best_n = 0
    for cp in glob.glob(f"{chrome_dir}/*/Cookies"):
        try:
            from pycookiecheat import chrome_cookies
            n = len(chrome_cookies("https://ieeexplore.ieee.org", cookie_file=cp))
            if n > best_n:
                best_n = n
                best = cp
        except Exception:
            pass
    _chrome_profile_path = best
    return best


def _get_cookies_for_url(url: str) -> dict:
    """Get Chrome cookies for publisher domains from the best profile."""
    try:
        host = urlparse(url).netloc
        for domain in _PUBLISHER_DOMAINS:
            if domain in host:
                if domain in _cookie_cache:
                    return _cookie_cache[domain]
                from pycookiecheat import chrome_cookies
                profile = _detect_chrome_profile()
                if profile:
                    cookies = chrome_cookies(f"https://{domain}", cookie_file=profile)
                else:
                    cookies = chrome_cookies(f"https://{domain}")
                _cookie_cache[domain] = cookies
                return cookies
    except Exception:
        pass
    return {}


# SOCKS5 proxy for curl (httpx doesn't support socks5h)
_SOCKS_PROXY = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or ""
if not _SOCKS_PROXY.startswith("socks"):
    _SOCKS_PROXY = ""


# ── HTML PDF extraction (covers IEEE JSON pdfUrl, meta tags, etc.) ─────
def _extract_pdf_url_from_html(html: str, base_url: str) -> Optional[str]:
    """Extract PDF URL from HTML page (publisher landing pages)."""
    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    def _abs(url):
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{base_origin}{url}"
        return url

    # 1. citation_pdf_url meta tag (IEEE, ACM, Google Scholar standard)
    m = re.search(r'<meta\s+name=["\']citation_pdf_url["\']\s+content=["\'](.*?)["\']', html, re.I)
    if m:
        return _abs(m.group(1))

    # 2. IEEE pdfUrl/stampUrl in embedded JSON
    for pat in [r'"pdfUrl"\s*:\s*"(.*?)"', r'"stampUrl"\s*:\s*"(.*?)"']:
        m = re.search(pat, html)
        if m:
            return _abs(m.group(1))

    # 3. Direct PDF link patterns
    for pat in [
        r'href=["\'](https?://[^"\']*?\.pdf[^"\']*)["\']',
        r'href=["\']([^"\']*?/pdf/[^"\']*)["\']',
        r'href=["\']([^"\']*?download[^"\']*?\.pdf[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return _abs(m.group(1))

    # 4. iframe/embed src
    for pat in [
        r'<embed[^>]+src=["\'](.*?\.pdf[^"\']*)["\']',
        r'<iframe[^>]+src=["\'](.*?\.pdf[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return _abs(m.group(1))

    return None


def _extract_scihub_pdf_url(html: str, base_url: str) -> Optional[str]:
    """Extract PDF URL from Sci-Hub HTML page."""
    for pat in [
        r'<meta\s+name=["\']citation_pdf_url["\']\s+content=["\'](.*?)["\']',
        r'href=["\'](/storage/[^"\']+\.pdf[^"\']*)["\']',
        r'content=["\'](/storage/[^"\']+\.pdf[^"\']*)["\']',
        r'<embed[^>]+src=["\'](.*?\.pdf[^"\']*)["\']',
        r'<iframe[^>]+src=["\'](.*?\.pdf[^"\']*)["\']',
        r'<embed[^>]+src=["\']([^"\']+)["\']',
        r'<iframe[^>]+src=["\']([^"\']+)["\']',
        r'location\.href\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            url = m.group(1)
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                parsed = urlparse(base_url)
                url = f"{parsed.scheme}://{parsed.netloc}{url}"
            return url
    return None


# ── URL transform (paper page → direct PDF) ───────────────────────────
def _transform_url(url: str) -> str:
    """Transform known paper page URLs to direct PDF URLs."""
    # CVF open access
    if "openaccess.thecvf.com" in url and "/html/" in url and url.endswith(".html"):
        return url.replace("/html/", "/papers/").replace("_paper.html", "_paper.pdf")
    # OpenReview
    if "openreview.net/forum" in url:
        return url.replace("/forum?", "/pdf?")
    # ACL Anthology
    if "aclanthology.org" in url:
        if "/abs/" in url:
            return url.replace("/abs/", "/pdf/")
        if not url.endswith(".pdf"):
            return url.rstrip("/") + ".pdf"
    # arXiv
    if "arxiv.org/abs/" in url:
        return url.replace("/abs/", "/pdf/")
    # MDPI
    if "mdpi.com" in url:
        if "/htm" in url:
            return url.replace("/htm", "/pdf")
        if re.match(r'https?://www\.mdpi\.com/[\d-]+/\d+/\d+/\d+$', url):
            return url.rstrip("/") + "/pdf"
    # Springer: /article/DOI → /content/pdf/DOI.pdf
    if "link.springer.com" in url and "/article/" in url:
        m = re.search(r'/article/(10\.\d+/[^\s?#]+)', url)
        if m:
            doi = m.group(1).rstrip('/')
            return f"https://link.springer.com/content/pdf/{doi}.pdf"
    # IEEE abstract → stamp
    if "ieeexplore.ieee.org" in url and "/abstract/" in url:
        m = re.search(r'/document/(\d+)', url)
        if m:
            return f"https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber={m.group(1)}"
    # ScienceDirect: /pii/XXX → /pii/XXX/pdfft with download params
    if "sciencedirect.com" in url and "/pii/" in url and "/pdfft" not in url:
        return url.rstrip("/") + "/pdfft?isDTMRedir=true&download=true"
    # NeurIPS proceedings
    if "papers.nips.cc" in url or "proceedings.neurips.cc" in url:
        if "-Abstract" in url:
            return url.replace("-Abstract-Conference.html", "-Paper-Conference.pdf").replace("-Abstract.html", "-Paper.pdf")
    # PMLR (ICML, AISTATS)
    if "proceedings.mlr.press" in url and url.endswith(".html"):
        base = url[:-5]
        slug = base.rsplit("/", 1)[-1]
        return f"{base}/{slug}.pdf"
    # AAAI
    if "ojs.aaai.org" in url and "/article/view/" in url:
        return url
    return url


def _build_cvf_candidates(doi: str, venue: str, year, title: str, first_author: str) -> list:
    """Build CVF open-access PDF URL candidates (CVPR/ICCV/WACV)."""
    if not title:
        return []
    venue_lower = (venue or "").lower()
    doi_lower = (doi or "").lower()
    conf = None
    if "cvpr" in venue_lower or "cvpr" in doi_lower:
        conf = "CVPR"
    elif "iccv" in venue_lower or "iccv" in doi_lower:
        conf = "ICCV"
    elif "wacv" in venue_lower or "wacv" in doi_lower:
        conf = "WACV"
    if not conf or not year:
        return []
    safe_title = re.sub(r'[^a-zA-Z0-9\s\-]', '', title)
    safe_title = re.sub(r'\s+', '_', safe_title.strip())
    safe_author = re.sub(r'[^a-zA-Z]', '', first_author or "Unknown")
    base = "https://openaccess.thecvf.com"
    return [f"{base}/content/{conf}{year}/papers/{safe_author}_{safe_title}_{conf}_{year}_paper.pdf"]


# ═══════════════════════════════════════════════════════════════════════
class PDFDownloader:
    """Smart multi-source PDF downloader with caching."""

    def __init__(self, cache_dir: Optional[Path] = None, email: Optional[str] = None,
                 scraper_api_keys: Optional[list] = None,
                 llm_api_key: str = "", llm_base_url: str = "", llm_model: str = ""):
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._email = email or "citationclaw@research.tool"
        self._scraper_keys = scraper_api_keys or []
        self._llm_key = llm_api_key
        self._llm_base_url = llm_base_url
        self._llm_model = llm_model

    @staticmethod
    def _make_client(timeout: float = 30.0):
        """Create httpx client with HTTP proxy (skip socks5h). Ported from PaperRadar."""
        import httpx
        return httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, trust_env=False,
            proxy=_HTTP_PROXY,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            },
        )

    def _cache_path(self, paper: dict) -> Path:
        key = (paper.get("doi") or paper.get("Paper_Title")
               or paper.get("title") or "unknown")
        h = hashlib.md5(key.encode()).hexdigest()
        return self._cache_dir / f"{h}.pdf"

    # ── Core: try downloading a single URL ────────────────────────────
    async def _try_url(self, client, url: str, cookies: dict = None) -> Optional[bytes]:
        """Try downloading from a URL, handling HTML pages with PDF extraction."""
        try:
            resp = await client.get(url, cookies=cookies or {})
            if resp.status_code != 200:
                return None
            if resp.content[:5] == b"%PDF-":
                return resp.content
            # HTML page → try extracting real PDF link
            if len(resp.content) > 100:
                pdf_url = _extract_pdf_url_from_html(resp.text, str(resp.url))
                if pdf_url:
                    cookies2 = _get_cookies_for_url(pdf_url)
                    resp2 = await client.get(pdf_url, cookies=cookies2)
                    if resp2.status_code == 200 and resp2.content[:5] == b"%PDF-":
                        return resp2.content
                    # IEEE stamp returns another HTML → extract again
                    if resp2.status_code == 200 and resp2.content[:5] != b"%PDF-":
                        inner = _extract_pdf_url_from_html(resp2.text, str(resp2.url))
                        if inner:
                            resp3 = await client.get(inner, cookies=cookies2)
                            if resp3.status_code == 200 and resp3.content[:5] == b"%PDF-":
                                return resp3.content
        except Exception:
            pass
        return None

    # ── curl-based publisher download (socks5h + Chrome cookies) ────────
    async def _curl_publisher_download(self, url: str) -> Optional[bytes]:
        """Download from publisher using curl with socks5h proxy + Chrome cookies.

        This bypasses httpx's socks5 limitation and Cloudflare bot detection.
        Only used for publisher domains (IEEE/Springer/ScienceDirect/ACM).
        """
        if not _SOCKS_PROXY:
            return None  # No socks proxy configured

        host = urlparse(url).netloc
        if not any(d in host for d in _PUBLISHER_DOMAINS):
            return None  # Not a publisher domain

        cookies = _get_cookies_for_url(url)
        if not cookies:
            return None

        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())

        def _curl(u):
            try:
                r = subprocess.run([
                    'curl', '-x', _SOCKS_PROXY, '-s', '-L',
                    '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8',
                    '-H', 'Accept-Language: en-US,en;q=0.9',
                    '-b', cookie_str,
                    u
                ], capture_output=True, timeout=30)
                return r.stdout
            except Exception:
                return None

        try:
            # Step 1: Get publisher page
            data = await asyncio.to_thread(_curl, url)
            if not data or len(data) < 500:
                return None
            if data[:5] == b"%PDF-":
                return data

            # Step 2: Extract PDF URL from HTML
            html = data.decode('utf-8', errors='ignore')
            pdf_url = _extract_pdf_url_from_html(html, url)
            if not pdf_url:
                return None

            # Step 3: Download from extracted URL
            data2 = await asyncio.to_thread(_curl, pdf_url)
            if data2 and data2[:5] == b"%PDF-":
                return data2

            # Step 4: If stamp page, extract inner URL (IEEE getPDF.jsp)
            if data2 and len(data2) > 200 and data2[:5] != b"%PDF-":
                import re as _re
                inner_html = data2.decode('utf-8', errors='ignore')
                for pat in [r'src="(https?://[^"]*getPDF[^"]*?)"',
                            r'src="(https?://[^"]*\.pdf[^"]*?)"',
                            r'"(https?://[^"]*iel[^"]*\.pdf[^"]*?)"']:
                    m = _re.search(pat, inner_html)
                    if m:
                        data3 = await asyncio.to_thread(_curl, m.group(1))
                        if data3 and data3[:5] == b"%PDF-":
                            return data3
        except Exception:
            pass
        return None

    @staticmethod
    def _publisher_label(url: str) -> str:
        """Generate a descriptive label for publisher-based download."""
        host = urlparse(url).netloc.lower()
        if "ieee" in host:
            return "IEEE+Cookie"
        if "springer" in host:
            return "Springer+Cookie"
        if "sciencedirect" in host:
            return "ScienceDirect+Cookie"
        if "acm" in host:
            return "ACM+Cookie"
        if "wiley" in host:
            return "Wiley+Cookie"
        if "doi.org" in host:
            return "DOI+Cookie"
        return "出版商+Cookie"

    # ── ScraperAPI + LLM smart fallback (for stubborn publisher pages) ──
    async def _smart_scraper_download(self, url: str) -> Optional[bytes]:
        """Last-resort: use ScraperAPI to render publisher page, then find PDF link.

        ScraperAPI renders JavaScript, bypasses Cloudflare, handles cookies.
        If direct extraction fails, uses lightweight LLM to analyze the HTML.
        """
        if not self._scraper_keys:
            return None

        key = self._scraper_keys[0]
        scraper_url = (
            f"https://api.scraperapi.com?api_key={key}"
            f"&url={quote(url)}&render=true&country_code=us"
        )

        try:
            from citationclaw.core.http_utils import make_async_client
            client = make_async_client(timeout=60.0)

            resp = await client.get(scraper_url)
            if resp.status_code != 200:
                await client.aclose()
                return None

            # Direct PDF?
            if resp.content[:5] == b"%PDF-":
                await client.aclose()
                return resp.content

            html = resp.text
            if len(html) < 500:
                await client.aclose()
                return None

            # Try rule-based extraction first
            pdf_link = _extract_pdf_url_from_html(html, url)

            # If rules failed, use LLM to find the PDF download link
            if not pdf_link and self._llm_key and len(html) > 1000:
                pdf_link = await self._llm_find_pdf_link(html, url)

            if not pdf_link:
                await client.aclose()
                return None

            # Download the found PDF link (also through ScraperAPI for cookie/JS)
            pdf_scraper_url = (
                f"https://api.scraperapi.com?api_key={key}"
                f"&url={quote(pdf_link)}&render=false"
            )
            pdf_resp = await client.get(pdf_scraper_url)
            if pdf_resp.status_code == 200 and pdf_resp.content[:5] == b"%PDF-":
                await client.aclose()
                return pdf_resp.content

            # Try direct download (some PDF links don't need ScraperAPI)
            cookies = _get_cookies_for_url(pdf_link)
            pdf_resp2 = await client.get(pdf_link, cookies=cookies)
            await client.aclose()
            if pdf_resp2.status_code == 200 and pdf_resp2.content[:5] == b"%PDF-":
                return pdf_resp2.content

        except Exception:
            pass
        return None

    async def _llm_find_pdf_link(self, html: str, page_url: str) -> Optional[str]:
        """Use lightweight LLM to find PDF download link in HTML."""
        try:
            from openai import AsyncOpenAI
            from citationclaw.core.http_utils import make_async_client

            # Send only the relevant part of HTML (links, buttons, meta tags)
            import re
            # Extract all links and meta tags
            links = re.findall(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>', html[:50000])
            metas = re.findall(r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*>', html[:10000])
            buttons = re.findall(r'<button[^>]*>([^<]*)</button>', html[:20000])

            context = f"Page URL: {page_url}\n\nLinks found:\n"
            for href, text in links[:50]:
                if any(k in href.lower() or k in text.lower()
                       for k in ['pdf', 'download', 'full', 'view', 'access']):
                    context += f"  {text.strip()} → {href}\n"

            context += f"\nMeta tags: {metas[:10]}\nButtons: {buttons[:10]}"

            client = AsyncOpenAI(
                api_key=self._llm_key,
                base_url=self._llm_base_url.rstrip("/") + "/" if self._llm_base_url else None,
                http_client=make_async_client(timeout=15.0),
            )
            resp = await client.chat.completions.create(
                model=self._llm_model,
                messages=[{"role": "user", "content":
                    f"From this academic paper page, find the direct PDF download URL.\n\n"
                    f"{context}\n\n"
                    f"Output ONLY the URL, nothing else. If no PDF link found, output 'NONE'."}],
                temperature=0.0,
            )
            result = resp.choices[0].message.content.strip()
            if result and result != "NONE" and result.startswith("http"):
                return result
        except Exception:
            pass
        return None

    # ── Main download method (PaperRadar-style smart download) ────────
    async def download(self, paper: dict, log=None) -> Optional[Path]:
        """Smart multi-source PDF download. Returns cached path or None."""
        title = paper.get("Paper_Title", paper.get("title", "?"))[:40]
        cached = self._cache_path(paper)
        if cached.exists() and cached.stat().st_size > 0:
            if log:
                log(f"    [PDF缓存] {title}")
            return cached

        doi = (paper.get("doi") or "").replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        pdf_url = paper.get("pdf_url") or ""
        oa_pdf_url = paper.get("oa_pdf_url") or ""
        # ArXiv ID: from metadata (Phase 2) or extracted from pdf_url
        arxiv_id = paper.get("arxiv_id") or ""
        if not arxiv_id and pdf_url and "arxiv.org" in pdf_url:
            m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d+\.\d+)', pdf_url)
            if m:
                arxiv_id = m.group(1)
        paper_link = paper.get("paper_link") or ""
        gs_pdf_link = paper.get("gs_pdf_link") or ""
        s2_id = paper.get("s2_id") or ""
        venue = paper.get("venue") or ""
        year = paper.get("paper_year") or paper.get("year") or 0
        full_title = paper.get("Paper_Title") or paper.get("title") or ""

        # Ordered download attempts
        attempts = []

        def _ok(data: Optional[bytes], source: str) -> bool:
            """Check if download succeeded, save to cache."""
            if data and len(data) > 1000 and data[:5] == b"%PDF-":
                cached.write_bytes(data)
                if log:
                    label = _SOURCE_LABELS.get(source, source)
                    log(f"    [PDF✓] {label} ({len(data)//1024}KB): {title}")
                return True
            return False

        try:
            async with self._make_client(timeout=45.0) as client:

                # 0. GS sidebar PDF link (highest priority — GS already found the PDF)
                if gs_pdf_link:
                    url = _transform_url(gs_pdf_link)
                    cookies = _get_cookies_for_url(url)
                    data = await self._try_url(client, url, cookies)
                    if _ok(data, "gs_pdf"):
                        return cached

                # 1. OpenAlex OA PDF
                if oa_pdf_url:
                    data = await self._try_url(client, oa_pdf_url)
                    if _ok(data, "oa_pdf"):
                        return cached

                # 2. CVF open access (construct URL from metadata)
                first_author = ""
                authors_raw = paper.get("authors_raw") or {}
                if isinstance(authors_raw, dict):
                    for k in authors_raw:
                        m = re.match(r'author_\d+_(.*)', k)
                        if m:
                            first_author = m.group(1).split()[-1]
                            break
                cvf_urls = _build_cvf_candidates(doi, venue, year, full_title, first_author)
                for cvf_url in cvf_urls:
                    data = await self._try_url(client, cvf_url)
                    if _ok(data, "cvf"):
                        return cached

                # 3. openAccessPdf (non-arxiv direct link)
                if pdf_url and "arxiv.org" not in pdf_url and "doi.org" not in pdf_url:
                    data = await self._try_url(client, pdf_url)
                    if _ok(data, "openaccess"):
                        return cached

                # 4. S2 API lookup (PaperRadar-style: always try if we have s2_id)
                if s2_id:
                    s2_data = await self._fetch_s2_data(client, s2_id, "")
                    if s2_data:
                        s2_pdf = (s2_data.get("openAccessPdf") or {}).get("url", "")
                        if s2_pdf:
                            data = await self._try_url(client, s2_pdf)
                            if _ok(data, "s2_page"):
                                return cached
                        # Supplement: get ArXiv ID and DOI if not already set
                        ext = s2_data.get("externalIds") or {}
                        if not arxiv_id:
                            arxiv_id = ext.get("ArXiv", "")
                        if not doi:
                            doi = ext.get("DOI", "")

                # 5. DBLP conference lookup
                if full_title:
                    dblp_url = await self._fetch_dblp_pdf(client, full_title)
                    if dblp_url:
                        data = await self._try_url(client, dblp_url, _get_cookies_for_url(dblp_url))
                        if _ok(data, "dblp"):
                            return cached

                # 6. Sci-Hub
                if doi:
                    data = await self._try_scihub(client, doi)
                    if _ok(data, "scihub"):
                        return cached

                # 7. arXiv
                if arxiv_id:
                    data = await self._try_url(client, f"https://arxiv.org/pdf/{arxiv_id}")
                    if _ok(data, "arxiv"):
                        return cached

                # 8. GS paper_link + smart URL transform
                if paper_link and "scholar.google" not in paper_link:
                    transformed = _transform_url(paper_link)
                    cookies = _get_cookies_for_url(transformed)
                    data = await self._try_url(client, transformed, cookies)
                    if _ok(data, "gs_link"):
                        return cached
                    # If transform didn't change URL, also try original
                    if transformed != paper_link:
                        cookies2 = _get_cookies_for_url(paper_link)
                        data = await self._try_url(client, paper_link, cookies2)
                        if _ok(data, "gs_link"):
                            return cached

                # 9. curl + socks5 + Chrome cookies (for IEEE/Springer/ScienceDirect)
                # httpx can't use socks5h, but curl can — bypasses Cloudflare
                if paper_link and "scholar.google" not in paper_link:
                    data = await self._curl_publisher_download(paper_link)
                    if _ok(data, self._publisher_label(paper_link)):
                        return cached

                # 10. DOI landing with cookie (via curl if socks available)
                if doi:
                    doi_url = f"https://doi.org/{doi}"
                    data = await self._curl_publisher_download(doi_url)
                    if _ok(data, self._publisher_label(doi_url)):
                        return cached
                    cookies = _get_cookies_for_url(doi_url)
                    data = await self._try_url(client, doi_url, cookies)
                    if _ok(data, "doi"):
                        return cached

                # 10. Unpaywall
                if doi:
                    data = await self._try_unpaywall(client, doi)
                    if _ok(data, "unpaywall"):
                        return cached

        except Exception:
            pass

        # 11. ScraperAPI + LLM smart fallback (last resort for stubborn pages)
        if paper_link and "scholar.google" not in paper_link:
            data = await self._smart_scraper_download(paper_link)
            if data and len(data) > 1000 and data[:5] == b"%PDF-":
                cached.write_bytes(data)
                if log:
                    log(f"    [PDF✓] ScraperAPI智能下载 ({len(data)//1024}KB): {title}")
                return cached

        if log:
            log(f"    [PDF] 所有来源均失败: {title}")
        return None

    # ── Helper: fetch S2 data by ID or title ──────────────────────────
    _s2_dl_lock = asyncio.Lock()  # Serialize S2 API calls in downloader

    async def _fetch_s2_data(self, client, s2_id: str, title: str) -> Optional[dict]:
        """Get S2 paper data (openAccessPdf, externalIds) by ID or title search."""
        try:
            if s2_id:
                url = f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}?fields=openAccessPdf,externalIds"
            elif title:
                url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={quote(title)}&limit=1&fields=openAccessPdf,externalIds"
            else:
                return None
            async with self._s2_dl_lock:
                await asyncio.sleep(1.1)  # S2 rate limit: 1 req/s
                resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if "data" in data and data["data"]:  # Search result
                return data["data"][0]
            return data  # Direct paper result
        except Exception:
            return None

    # ── Helper: DBLP PDF lookup ───────────────────────────────────────
    async def _fetch_dblp_pdf(self, client, title: str) -> Optional[str]:
        """Query DBLP API for conference paper PDF URL."""
        try:
            api_url = f"https://dblp.org/search/publ/api?q={quote(title)}&format=json&h=3"
            resp = await client.get(api_url, timeout=10)
            if resp.status_code != 200:
                return None
            hits = resp.json().get("result", {}).get("hits", {}).get("hit", [])
            title_lower = title.lower().strip().rstrip(".")
            for hit in hits:
                info = hit.get("info", {})
                hit_title = (info.get("title") or "").lower().strip().rstrip(".")
                if hit_title != title_lower and title_lower not in hit_title:
                    continue
                ee = info.get("ee")
                if not ee:
                    continue
                urls = ee if isinstance(ee, list) else [ee]
                for venue_url in urls:
                    pdf_url = _transform_url(venue_url)
                    if pdf_url != venue_url or pdf_url.endswith(".pdf"):
                        return pdf_url
        except Exception:
            pass
        return None

    # ── Helper: Sci-Hub (uses curl+socks5 since httpx can't reach it) ──
    async def _try_scihub(self, client, doi: str) -> Optional[bytes]:
        """Try Sci-Hub mirrors for DOI. Uses curl+socks5 if available."""
        for mirror in SCIHUB_MIRRORS:
            try:
                data = await self._curl_scihub(mirror, doi)
                if data and data[:5] == b"%PDF-":
                    return data
            except Exception:
                continue

        # Fallback: try httpx (works if no socks needed)
        for mirror in SCIHUB_MIRRORS:
            try:
                resp = await client.get(f"{mirror}/{doi}", timeout=15)
                if resp.status_code != 200:
                    continue
                if resp.content[:5] == b"%PDF-":
                    return resp.content
                if "html" in resp.headers.get("content-type", ""):
                    html = resp.text
                    if "不可用" in html or "not available" in html.lower():
                        continue
                    pdf_url = _extract_scihub_pdf_url(html, str(resp.url))
                    if pdf_url:
                        r2 = await client.get(pdf_url, timeout=20)
                        if r2.status_code == 200 and r2.content[:5] == b"%PDF-":
                            return r2.content
            except Exception:
                continue
        return None

    async def _curl_scihub(self, mirror: str, doi: str) -> Optional[bytes]:
        """Download from Sci-Hub via curl+socks5."""
        if not _SOCKS_PROXY:
            return None

        def _do():
            try:
                # Step 1: Get Sci-Hub page
                r = subprocess.run([
                    'curl', '-x', _SOCKS_PROXY, '-s', '-L',
                    '-H', 'User-Agent: Mozilla/5.0',
                    f'{mirror}/{doi}'
                ], capture_output=True, timeout=20)
                if not r.stdout:
                    return None
                # Direct PDF?
                if r.stdout[:5] == b"%PDF-":
                    return r.stdout
                # Parse HTML for PDF URL
                html = r.stdout.decode('utf-8', errors='ignore')
                if "不可用" in html or "not available" in html.lower():
                    return None
                pdf_url = _extract_scihub_pdf_url(html, mirror)
                if not pdf_url:
                    return None
                # Step 2: Download PDF
                r2 = subprocess.run([
                    'curl', '-x', _SOCKS_PROXY, '-s', '-L',
                    '-H', 'User-Agent: Mozilla/5.0',
                    pdf_url
                ], capture_output=True, timeout=20)
                if r2.stdout and r2.stdout[:5] == b"%PDF-":
                    return r2.stdout
            except Exception:
                pass
            return None

        return await asyncio.to_thread(_do)

    # ── Helper: Unpaywall ─────────────────────────────────────────────
    async def _try_unpaywall(self, client, doi: str) -> Optional[bytes]:
        """Try Unpaywall API."""
        try:
            url = f"https://api.unpaywall.org/v2/{doi}?email={self._email}"
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            best = (resp.json().get("best_oa_location") or {}).get("url_for_pdf", "")
            if best:
                r2 = await client.get(best, timeout=20)
                if r2.status_code == 200 and r2.content[:5] == b"%PDF-":
                    return r2.content
        except Exception:
            pass
        return None

    # ── Batch download ────────────────────────────────────────────────
    async def batch_download(self, papers: List[dict], concurrency: int = 10,
                             log=None) -> List[Optional[Path]]:
        sem = asyncio.Semaphore(concurrency)
        async def _dl(p):
            async with sem:
                return await self.download(p, log=log)
        return await asyncio.gather(*[_dl(p) for p in papers])

    async def close(self):
        pass  # Client is created per-download via async context manager
