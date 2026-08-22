"""Minimal Frigate HTTP client used only to fetch event thumbnails for the app's extension.

Frigate 0.14+ can require auth: POST /api/login sets a JWT cookie the client then reuses.
Self-signed certs are common on LAN installs, hence the verify toggle.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx

from .config import FrigateConfig

log = logging.getLogger("agent.frigate")

# Frigate event IDs commonly contain a period; path separators and other unsafe characters do not.
_SAFE_EVENT_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


class FrigateClient:
    def __init__(self, cfg: FrigateConfig, cache_dir: str = "thumb_cache"):
        self._cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._client = httpx.AsyncClient(verify=cfg.verify_tls, timeout=10.0)
        self._logged_in = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _login(self) -> bool:
        if not self._cfg.user:
            return True
        try:
            resp = await self._client.post(
                f"{self._base}/api/login",
                json={"user": self._cfg.user, "password": self._cfg.password},
            )
            self._logged_in = resp.status_code == 200
            if not self._logged_in:
                log.warning("Frigate login failed: %s", resp.status_code)
            return self._logged_in
        except httpx.HTTPError as exc:
            log.warning("Frigate login error: %s", exc)
            return False

    async def fetch_event_thumbnail(self, event_id: str) -> Optional[str]:
        """Fetch /api/events/{id}/thumbnail.jpg, cache it, return the local path."""
        if not _SAFE_EVENT_ID.match(event_id):
            log.warning("rejecting unsafe event_id: %r", event_id)
            return None
        path = os.path.join(self._cache_dir, f"{event_id}.jpg")
        if os.path.exists(path):
            return path

        url = f"{self._base}/api/events/{event_id}/thumbnail.jpg"
        for attempt in range(2):
            if self._cfg.user and not self._logged_in and not await self._login():
                return None
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as exc:
                log.warning("thumbnail fetch error for %s: %s", event_id, exc)
                return None
            if resp.status_code == 401:
                if not self._cfg.user:
                    log.warning(
                        "Frigate requires auth but frigate.user/password are not configured — "
                        "set them in config.yaml (thumbnails will fail until then)"
                    )
                    return None
                if attempt == 0:
                    self._logged_in = False
                    continue
            if resp.status_code != 200:
                log.info("no thumbnail for event %s (%s)", event_id, resp.status_code)
                return None
            with open(path, "wb") as fh:
                fh.write(resp.content)
            return path
        return None
