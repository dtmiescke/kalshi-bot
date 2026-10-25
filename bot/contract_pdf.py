"""Read-only extraction of official Kalshi contract-terms PDFs.

Only public HTTPS links from explicitly allowlisted Kalshi document hosts are
accepted. Downloads are bounded and never followed across redirects. Extracted
text is evidence for existing counsel only; it never authorizes an order.
"""
from __future__ import annotations

import io
from urllib.parse import urlparse

import requests

ALLOWED_HOSTS = {
    "kalshi.com",
    "www.kalshi.com",
    "kalshi-public-docs.s3.amazonaws.com",
    "kalshi-public-docs.s3.us-east-1.amazonaws.com",
    "kalshi-public-docs.s3.us-east-2.amazonaws.com",
}
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PAGES = 30
MAX_TEXT_CHARS = 6000
CACHE_SECONDS = 7 * 86400


def is_trusted_contract_url(url):
    try:
        parsed = urlparse(str(url or ""))
        return (
            parsed.scheme == "https"
            and parsed.hostname in ALLOWED_HOSTS
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and len(str(url)) <= 2000
        )
    except (TypeError, ValueError):
        return False


def extract_contract_pdf_text(url, get=requests.get):
    """Return bounded extracted text and provenance, or None on uncertainty."""
    if not is_trusted_contract_url(url):
        return None
    response = None
    try:
        response = get(
            url, timeout=(3, 12), stream=True, allow_redirects=False,
            headers={"User-Agent": "kalshi-bot/1.0 (public contract rules)"},
        )
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_PDF_BYTES:
                    return None
            except (TypeError, ValueError):
                return None
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw.startswith(b"%PDF"):
            return None
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        pages = reader.pages
        parts = [(pages[i].extract_text() or "") for i in range(min(len(pages), MAX_PAGES))]
        text = "\n\n".join(parts).strip()
        if not text:
            return None
        return {
            "url": str(url)[:1000],
            "page_count": len(pages),
            "text": text[:MAX_TEXT_CHARS],
            "truncated": len(text) > MAX_TEXT_CHARS or len(pages) > MAX_PAGES,
        }
    except Exception:
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def load_series_contract_pdf(data_client, store, series_ticker, now, series_metadata=None):
    """Load/cached PDF text for one series using metadata from the existing client."""
    series_ticker = str(series_ticker or "").strip()
    if not series_ticker or "/" in series_ticker or "?" in series_ticker:
        return None
    cache_key = f"contract_rules_pdf_{series_ticker}"
    try:
        cached = store.get_kv(cache_key)
        age = int(now) - int((cached or {}).get("ts", 0) or 0) if isinstance(cached, dict) else -1
        if isinstance(cached, dict) and cached.get("text") and 0 <= age < CACHE_SECONDS:
            return cached
        series = series_metadata
        if not isinstance(series, dict):
            series = data_client.series(series_ticker)
        if not isinstance(series, dict):
            return None
        url = series.get("contract_terms_url") or series.get("contract_url")
        if not is_trusted_contract_url(url):
            return None
        extracted = extract_contract_pdf_text(url)
        if not extracted:
            return None
        result = {"ts": int(now), **extracted}
        store.set_kv(cache_key, result)
        return result
    except Exception:
        return None
