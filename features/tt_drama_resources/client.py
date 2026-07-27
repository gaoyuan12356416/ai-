"""Fixed-host HTTP client for fetching W2A server-rendered HTML source."""

import logging
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from .models import (
    DEFAULT_COVER_HOSTS,
    ResourceNotFoundError,
    ResourceSourceError,
    build_source_url,
    normalize_content_id,
    normalize_cover_hosts,
    normalize_landing_id,
)
from .parser import parse_w2a_resource_html


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None


class W2AHTMLClient:
    """Fetch one small HTML document; never execute scripts or fetch assets."""

    def __init__(
        self,
        landing_id=2049,
        timeout_seconds=5.0,
        max_html_bytes=512 * 1024,
        allowed_cover_hosts=None,
        opener=None,
    ):
        self.landing_id = normalize_landing_id(landing_id)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_html_bytes = max(1024, int(max_html_bytes))
        self.allowed_cover_hosts = normalize_cover_hosts(
            allowed_cover_hosts or DEFAULT_COVER_HOSTS
        )
        self._opener = opener or build_opener(_RejectRedirects())

    def _open(self, request):
        target = self._opener.open if hasattr(self._opener, "open") else self._opener
        return target(request, timeout=self.timeout_seconds)

    @staticmethod
    def _content_type(response):
        headers = getattr(response, "headers", None)
        if headers is None:
            return ""
        if hasattr(headers, "get_content_type"):
            return str(headers.get_content_type() or "").lower()
        return str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()

    @staticmethod
    def _charset(response):
        headers = getattr(response, "headers", None)
        if headers is not None and hasattr(headers, "get_content_charset"):
            return headers.get_content_charset() or "utf-8"
        return "utf-8"

    def fetch(self, content_id):
        normalized = normalize_content_id(content_id)
        source_url = build_source_url(self.landing_id, normalized)
        request = Request(
            source_url,
            method="GET",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "User-Agent": "TTDramaResourceCache/1.0",
            },
        )
        response = None
        try:
            response = self._open(request)
            status = int(
                getattr(response, "status", None)
                or getattr(response, "code", 0)
                or 0
            )
            if status != 200:
                if status == 404:
                    raise ResourceNotFoundError("W2A source returned 404")
                raise ResourceSourceError("W2A source returned HTTP %d" % status)
            if str(getattr(response, "geturl", lambda: source_url)()) != source_url:
                raise ResourceSourceError("W2A source redirected unexpectedly")
            content_type = self._content_type(response)
            if content_type not in ("text/html", "application/xhtml+xml"):
                raise ResourceSourceError("W2A source did not return HTML")
            body = response.read(self.max_html_bytes + 1)
            if len(body) > self.max_html_bytes:
                raise ResourceSourceError("W2A source HTML exceeded the byte limit")
            try:
                html = body.decode(self._charset(response), errors="strict")
            except (LookupError, UnicodeDecodeError):
                raise ResourceSourceError("W2A source HTML decoding failed") from None
            return parse_w2a_resource_html(
                html,
                normalized,
                landing_id=self.landing_id,
                allowed_cover_hosts=self.allowed_cover_hosts,
            )
        except (ResourceNotFoundError, ResourceSourceError):
            raise
        except HTTPError as exc:
            if int(exc.code or 0) == 404:
                raise ResourceNotFoundError("W2A source returned 404") from None
            logging.warning(
                "W2A resource HTTP request failed for %s: HTTP_%s",
                normalized,
                int(exc.code or 0),
            )
            raise ResourceSourceError("W2A source request failed") from None
        except (URLError, TimeoutError, OSError) as exc:
            logging.warning(
                "W2A resource request failed for %s: %s",
                normalized,
                type(exc).__name__,
            )
            raise ResourceSourceError("W2A source request failed") from None
        except Exception as exc:
            logging.warning(
                "W2A resource request failed for %s: %s",
                normalized,
                type(exc).__name__,
            )
            raise ResourceSourceError("W2A source request failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
