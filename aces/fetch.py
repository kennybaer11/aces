"""HTTP with a disk cache and a polite pace.

A finished match never changes, so its response is cached for good and a
re-run costs nothing. Lists that grow (a season's tournaments, a tournament
still in progress) are fetched with cache=False.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CACHE = Path(os.environ.get("ACES_CACHE") or Path(__file__).resolve().parent.parent / "cache")
MIN_GAP = 0.25          # seconds between live requests
_session = requests.Session()
_session.headers["User-Agent"] = "Mozilla/5.0 (aces research; personal use)"
_last = 0.0


def _path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()
    return CACHE / h[:2] / f"{h}.json"


def get_json(url: str, cache: bool = True):
    """The JSON at url, or None for a 404. Other failures are retried, then raised."""
    global _last
    p = _path(url)
    if cache and p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    for attempt in range(4):
        wait = MIN_GAP - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        try:
            r = _session.get(url, timeout=30)
        except requests.RequestException as e:
            log.warning("%s: %s (attempt %d)", url, e, attempt + 1)
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            log.warning("%s: HTTP %d (attempt %d)", url, r.status_code, attempt + 1)
            time.sleep(2 ** attempt * 5)
            continue
        r.raise_for_status()
        data = r.json()
        if cache:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data), encoding="utf-8")
        return data
    raise RuntimeError(f"gave up on {url}")
