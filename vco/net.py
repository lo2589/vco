"""HTTP opening that never sends loopback traffic through a proxy."""

from __future__ import annotations

import ipaddress
import urllib.request
from urllib.parse import urlsplit

# A machine-wide HTTP_PROXY applies to 127.0.0.1 too unless NO_PROXY says
# otherwise, and a proxy that cannot reach the caller's loopback answers 500 or
# drops the connection. Local Ollama and OCR services then look broken while
# they are fine, so loopback always goes direct; everything else keeps the
# environment's proxy settings.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def open_url(request, timeout: float | None = None):
    """Drop-in for ``urllib.request.urlopen`` (Request or URL string)."""

    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if is_loopback_url(url):
        return _DIRECT.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)
