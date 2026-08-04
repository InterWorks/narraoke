"""Article extraction using trafilatura with newspaper3k fallback."""
from __future__ import annotations

from utils import clean_text, info, warn

MIN_WORDS = 200

# A modern browser UA — many sites 403 the default python-requests/trafilatura UAs.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # `requests` auto-decompresses gzip/deflate but not brotli (`br`) or
    # zstd (`zstd`) without extra packages. Advertise only what we can decode
    # so servers don't hand us binary blobs.
    "Accept-Encoding": "gzip, deflate",
}


def _download_html(url: str) -> str:
    """Download *url* as text using a browser-like User-Agent."""
    import requests
    resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _extract_trafilatura(url: str, html: str | None = None) -> tuple[str, str]:
    """Return (title, text) via trafilatura or raise ValueError."""
    import trafilatura

    downloaded = html if html is not None else trafilatura.fetch_url(url)
    if not downloaded:
        raise ValueError("trafilatura: could not download URL")

    result = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        output_format="txt",
    )
    if not result:
        raise ValueError("trafilatura: extraction returned nothing")

    # Try to get the title separately via metadata
    meta = trafilatura.extract_metadata(downloaded)
    title = (meta.title if meta and meta.title else "") or ""

    return title.strip(), result.strip()


def _extract_newspaper(url: str, html: str | None = None) -> tuple[str, str]:
    """Return (title, text) via newspaper3k or raise ValueError."""
    from newspaper import Article

    article = Article(url)
    if html is not None:
        article.set_html(html)
    else:
        article.download()
    article.parse()

    text = article.text or ""
    title = article.title or ""
    if not text:
        raise ValueError("newspaper3k: extraction returned nothing")

    return title.strip(), text.strip()


def fetch_article(url: str) -> dict:
    """
    Fetch and extract the main article content from *url*.

    Returns a dict with keys:
        url, title, text, word_count
    Raises SystemExit with a user-friendly message when content is too short.
    """
    info(f"Fetching article: {url}")

    # Pre-fetch with a browser User-Agent so sites that 403 default UAs work.
    # If this fails, fall back to letting each extractor fetch the URL itself.
    html: str | None = None
    try:
        html = _download_html(url)
    except Exception as exc:
        warn(f"browser-UA fetch failed ({exc}); extractors will retry their own download")

    title, text = "", ""
    tried = []

    # ── Primary: trafilatura ─────────────────────────────────────────────────
    try:
        title, text = _extract_trafilatura(url, html=html)
        tried.append("trafilatura")
        words = len(text.split())
        if words < MIN_WORDS:
            warn(f"trafilatura returned only {words} words — trying fallback")
            raise ValueError("too short")
    except Exception as exc:
        warn(f"trafilatura failed ({exc}), trying newspaper3k …")
        try:
            title_fb, text_fb = _extract_newspaper(url, html=html)
            tried.append("newspaper3k")
            # Use fallback if it's longer
            if len(text_fb.split()) > len(text.split()):
                title, text = title_fb, text_fb
            if not title and title_fb:
                title = title_fb
        except Exception as exc2:
            warn(f"newspaper3k also failed: {exc2}")

    text = clean_text(text)
    word_count = len(text.split())

    if word_count < MIN_WORDS:
        import sys
        from utils import error
        error(
            f"Extracted only {word_count} words (minimum {MIN_WORDS}).\n"
            "The article may be behind a paywall, require JavaScript, or be "
            "very short.\nExiting."
        )
        sys.exit(1)

    title = title or "Untitled Article"
    info(f"Title    : {title}")
    info(f"Words    : {word_count:,}  (extractor: {', '.join(tried)})")

    return {
        "url": url,
        "title": title,
        "text": text,
        "word_count": word_count,
    }
