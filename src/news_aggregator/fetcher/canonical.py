"""URL canonicalisation and text normalisation used for exact-match dedupe."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ocid", "cmpid", "smid", "smtyp",
    "ref", "ref_src", "referrer", "src", "source", "guccounter", "taid", "ito", "at_medium",
    "at_campaign", "at_custom1", "at_custom2", "at_custom3", "at_custom4", "__twitter_impression",
}
TRACKING_PREFIXES = ("utm_", "mkt_", "pk_", "at_", "ga_")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]")


def canonicalize_url(url: str) -> str:
    """Lower-case scheme/host, drop www., fragments, tracking params and trailing slash."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query.sort()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def clean_text(text: str | None) -> str:
    """Strip HTML tags and entities, collapse whitespace."""
    if not text:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", text))
    return _WS_RE.sub(" ", text).strip()


def normalize_title(title: str) -> str:
    """Key for exact title matching: lower-case, no punctuation, no outlet suffix."""
    title = clean_text(title).lower()
    # Drop trailing " - Outlet" / " | Outlet" suffixes that syndicators add.
    title = re.split(r"\s+[|\-–—]\s+(?=[^|\-–—]{2,40}$)", title)[0]
    title = _NON_WORD_RE.sub(" ", title)
    return _WS_RE.sub(" ", title).strip()
