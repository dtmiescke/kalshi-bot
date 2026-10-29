"""Never let a credential reach a log, an alert or the saved database.

The odds client sends its key as a URL parameter, and `requests` puts the whole URL in its error text. A failed
call therefore stored the key in plain text in the database that is published on the public `bot-data` branch.
GitHub masks secrets in the Actions logs, but not inside our own files, so every error text that is stored,
logged or sent goes through here."""
import re

_SECRET_NAME = r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd|key)"
_PARAM = re.compile(rf"(?i)(\b{_SECRET_NAME}\b[\"']?\s*[=:]\s*[\"']?)([^&\s\"',;)}}\]]+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_QUICK = re.compile(r"(?i)key|token|secret|password|passwd|bearer|basic ")


def redact(text):
    """Text with every secret-looking value replaced by ***. Non-strings pass through unchanged."""
    if not isinstance(text, str) or not _QUICK.search(text):
        return text
    text = _PARAM.sub(lambda m: m.group(1) + "***", text)
    return _BEARER.sub(lambda m: m.group(1) + " ***", text)


_URL_SECRET = re.compile(r"(?i)([?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|key|secret)=)[^&\s\"'<>)\\]+")


def scrub_urls(text):
    """Strict version for text that is stored as data: only a secret inside a URL query string is replaced, so
    ordinary fields that merely happen to be called "key" or "token" are never touched."""
    if not isinstance(text, str) or "=" not in text:
        return text
    return _URL_SECRET.sub(lambda m: m.group(1) + "***", text)
