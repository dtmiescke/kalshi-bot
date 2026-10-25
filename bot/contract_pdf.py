"""Read-only extraction of Kalshi's official contract-terms PDF.

Only public HTTPS contract links from Kalshi's own domain/public-docs bucket are
accepted. Downloads are size-limited, never executed, and text is evidence only;
the normal numeric risk gate and guarded execution path remain authoritative.
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
MAX_TEXT_CHARS = 6000


def extract_contract_pdf_text(url, get=requests.get):
    """Return bounded extracted text and provenance; fail closed on unsupported input."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        return None
    try:
        response = get(url, timeout=12, stream=True, allow_redirects=False,
                       headers={"User-Agent": "kalshi-bot/1.0 (public contract rules)"})
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_PDF_BYTES:
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
        parts = [(page.extract_text() or "") for page in reader.pages[:30]]
        full_text = "\n\n".join(parts).strip()
        if not full_text:
            return None
        return {
            "url": str(url)[:1000],
            "page_count": len(reader.pages),
            "text": full_text[:MAX_TEXT_CHARS],
            "truncated": len(full_text) > MAX_TEXT_CHARS,
        }
    except Exception:
        # A bad/unsupported PDF is an unknown rule source, never a reason to trade.
        return None


def load_series_contract_pdf(data_client, store, series_ticker, now):
    """Resolve and cache a series contract PDF through the existing Kalshi client."""
    if not series_ticker:
        return None
    cache_key = f"contract_rules_pdf_{series_ticker}"
    try:
        cached = store.get_kv(cache_key)
        if (isinstance(cached, dict) and cached.get("text")
                and 0 <= int(now) - int(cached.get("ts", 0) or 0) < 7 * 86400):
            return cached
        series = data_client.series(series_ticker)
        if not isinstance(series, dict):
            return None
        url = series.get("contract_terms_url") or series.get("contract_url")
        extracted = extract_contract_pdf_text(url) if url else None
        if not extracted:
            return None
        result = {"ts": int(now), **extracted}
        store.set_kv(cache_key, result)
        return result
    except Exception:
        # Metadata/PDF failures are unknown evidence, never permission to bypass counsel.
        return None
