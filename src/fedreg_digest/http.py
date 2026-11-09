"""One HTTP session for both government APIs: retries, backoff, a polite User-Agent."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fedreg_digest import __version__

USER_AGENT = f"fedreg-digest/{__version__} (+https://github.com/franco-g-s/fedreg-digest)"


class APIError(RuntimeError):
    """A government API could not be reached or returned something unusable."""


def make_session(timeout: float = 30.0) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            # eCFR's full-text endpoint refuses uncompressed responses (406).
            "Accept-Encoding": "gzip, deflate",
        }
    )
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    timeout = getattr(session, "request_timeout", 30.0)
    try:
        response = session.get(url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise APIError(f"Request to {url} failed: {exc}") from exc
    if response.status_code >= 400:
        raise APIError(f"{url} returned HTTP {response.status_code}: {response.text[:200]}")
    return response
