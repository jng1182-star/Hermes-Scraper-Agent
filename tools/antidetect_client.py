"""
Anti-Detect Browser Client — wraps Multilogin / GoLogin / AdsPower profile APIs.

Why this exists:
  Instagram and TikTok FYP feed scraping require authenticated, pre-warmed browser
  profiles to avoid bot-detection friction. This module abstracts the provider API
  so the rest of the codebase doesn't care which anti-detect browser is in use.

Supported providers (set ANTIDETECT_PROVIDER in .env):
  "multilogin"  — Multilogin X (MLX) API
  "gologin"     — GoLogin API
  "adspower"    — AdsPower Local API (runs on localhost)

Configuration (.env):
  ANTIDETECT_PROVIDER=gologin           # multilogin | gologin | adspower
  ANTIDETECT_API_KEY=<your_api_key>     # not needed for adspower (local)
  ANTIDETECT_PROFILE_TH=<profile_id>   # pre-warmed profile ID for Thailand market
  ANTIDETECT_PROFILE_PH=<profile_id>   # Philippines
  ANTIDETECT_PROFILE_VN=<profile_id>   # Vietnam
  ANTIDETECT_PROFILE_ID=<profile_id>   # Indonesia
  ANTIDETECT_PROFILE_MY=<profile_id>   # Malaysia
  ANTIDETECT_PROFILE_SG=<profile_id>   # Singapore
  ANTIDETECT_PROFILE_DEFAULT=<id>      # fallback for markets without a dedicated profile

Usage:
  from tools.antidetect_client import AntidetectClient
  client = AntidetectClient()
  ws_url = client.start_profile("PH")      # returns CDP WebSocket URL
  # connect Playwright: await p.chromium.connect_over_cdp(ws_url)
  client.stop_profile("PH")               # release the profile slot

Degradation:
  If no provider is configured, returns None from start_profile() and the caller
  falls back to standard headless Playwright (lower bot-detection resistance).
  The scraper will log a warning but will not crash.
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# ── Market code → profile ID env var ─────────────────────────────────────────
_PROFILE_ENV_VARS = {
    "TH": "ANTIDETECT_PROFILE_TH",
    "PH": "ANTIDETECT_PROFILE_PH",
    "VN": "ANTIDETECT_PROFILE_VN",
    "ID": "ANTIDETECT_PROFILE_ID",
    "MY": "ANTIDETECT_PROFILE_MY",
    "SG": "ANTIDETECT_PROFILE_SG",
}
_COUNTRY_TO_CODE = {
    "Thailand": "TH", "Philippines": "PH", "Vietnam": "VN",
    "Indonesia": "ID", "Malaysia": "MY", "Singapore": "SG",
}

_SESSION_TIMEOUT = 30   # seconds for API calls
_START_RETRIES   = 2    # retries on profile start failure


class AntidetectClient:
    """
    Provider-agnostic anti-detect browser client.
    Returns a CDP WebSocket URL that Playwright can connect to.
    """

    def __init__(self):
        self.provider   = os.getenv("ANTIDETECT_PROVIDER", "").lower().strip()
        self.api_key    = os.getenv("ANTIDETECT_API_KEY", "").strip()
        self._active: dict[str, str] = {}   # market_code → profile_id

        if not self.provider:
            logger.debug(
                "[AntidetectClient] ANTIDETECT_PROVIDER not set — using standard headless Playwright."
            )

    def _profile_id(self, market: str) -> str | None:
        code = _COUNTRY_TO_CODE.get(market, market.upper() if len(market) == 2 else None)
        if not code:
            return None
        env_var = _PROFILE_ENV_VARS.get(code, "")
        pid = os.getenv(env_var, "").strip() if env_var else ""
        if not pid:
            pid = os.getenv("ANTIDETECT_PROFILE_DEFAULT", "").strip()
        if not pid:
            logger.warning(
                "[AntidetectClient] No profile ID configured for market '%s'. "
                "Set %s or ANTIDETECT_PROFILE_DEFAULT in .env.",
                code, env_var or f"ANTIDETECT_PROFILE_{code}",
            )
        return pid or None

    # ── Provider: GoLogin ─────────────────────────────────────────────────────

    def _gologin_start(self, profile_id: str) -> str | None:
        url = f"https://api.gologin.com/browser/{profile_id}/start-remote"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = requests.get(url, headers=headers, timeout=_SESSION_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            ws = data.get("wsUrl") or data.get("ws_endpoint")
            if not ws:
                logger.error("[AntidetectClient] GoLogin start_remote: no wsUrl in response: %s", data)
                return None
            logger.info("[AntidetectClient] GoLogin profile %s started.", profile_id)
            return ws
        except Exception as exc:
            logger.error("[AntidetectClient] GoLogin start failed for profile %s: %s", profile_id, exc)
            return None

    def _gologin_stop(self, profile_id: str) -> None:
        url = f"https://api.gologin.com/browser/{profile_id}/stop"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            requests.get(url, headers=headers, timeout=_SESSION_TIMEOUT)
            logger.info("[AntidetectClient] GoLogin profile %s stopped.", profile_id)
        except Exception as exc:
            logger.warning("[AntidetectClient] GoLogin stop failed for profile %s: %s", profile_id, exc)

    # ── Provider: AdsPower (local API, no key required) ───────────────────────

    def _adspower_start(self, profile_id: str) -> str | None:
        base = os.getenv("ADSPOWER_LOCAL_URL", "http://local.adspower.net:50325")
        url  = f"{base}/api/v1/browser/start?user_id={profile_id}&open_tabs=1&ip_tab=0"
        try:
            resp = requests.get(url, timeout=_SESSION_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.error("[AntidetectClient] AdsPower start error: %s", data)
                return None
            ws = data.get("data", {}).get("ws", {}).get("puppeteer")
            if not ws:
                logger.error("[AntidetectClient] AdsPower: no ws.puppeteer in response: %s", data)
                return None
            logger.info("[AntidetectClient] AdsPower profile %s started.", profile_id)
            return ws
        except Exception as exc:
            logger.error("[AntidetectClient] AdsPower start failed for profile %s: %s", profile_id, exc)
            return None

    def _adspower_stop(self, profile_id: str) -> None:
        base = os.getenv("ADSPOWER_LOCAL_URL", "http://local.adspower.net:50325")
        url  = f"{base}/api/v1/browser/stop?user_id={profile_id}"
        try:
            requests.get(url, timeout=_SESSION_TIMEOUT)
            logger.info("[AntidetectClient] AdsPower profile %s stopped.", profile_id)
        except Exception as exc:
            logger.warning("[AntidetectClient] AdsPower stop failed for profile %s: %s", profile_id, exc)

    # ── Provider: Multilogin X (MLX) ─────────────────────────────────────────

    def _multilogin_start(self, profile_id: str) -> str | None:
        url = "https://launcher.mlx.yt:45001/api/v1/profile/start"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"profile_id": profile_id, "automation": True}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_SESSION_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            ws = data.get("value", {}).get("ws")
            if not ws:
                logger.error("[AntidetectClient] Multilogin start: no ws in response: %s", data)
                return None
            logger.info("[AntidetectClient] Multilogin profile %s started.", profile_id)
            return ws
        except Exception as exc:
            logger.error("[AntidetectClient] Multilogin start failed for profile %s: %s", profile_id, exc)
            return None

    def _multilogin_stop(self, profile_id: str) -> None:
        url = f"https://launcher.mlx.yt:45001/api/v1/profile/stop/{profile_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            requests.get(url, headers=headers, timeout=_SESSION_TIMEOUT)
            logger.info("[AntidetectClient] Multilogin profile %s stopped.", profile_id)
        except Exception as exc:
            logger.warning("[AntidetectClient] Multilogin stop failed for profile %s: %s", profile_id, exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def start_profile(self, market: str) -> str | None:
        """
        Start the pre-warmed profile for the given market and return the CDP
        WebSocket URL for Playwright to connect to.
        Returns None if no provider is configured or the profile fails to start
        (caller falls back to standard headless Playwright).
        """
        if not self.provider:
            return None

        profile_id = self._profile_id(market)
        if not profile_id:
            return None

        starters = {
            "gologin":     self._gologin_start,
            "adspower":    self._adspower_start,
            "multilogin":  self._multilogin_start,
        }
        start_fn = starters.get(self.provider)
        if not start_fn:
            logger.error(
                "[AntidetectClient] Unknown provider '%s'. "
                "Valid values: gologin, adspower, multilogin.", self.provider
            )
            return None

        for attempt in range(1, _START_RETRIES + 1):
            ws_url = start_fn(profile_id)
            if ws_url:
                self._active[market] = profile_id
                return ws_url
            if attempt < _START_RETRIES:
                logger.warning(
                    "[AntidetectClient] Profile start attempt %d/%d failed for market '%s'. Retrying…",
                    attempt, _START_RETRIES, market,
                )
                time.sleep(3)

        logger.error(
            "[AntidetectClient] All %d start attempts failed for market '%s' profile '%s'. "
            "Falling back to standard headless Playwright.",
            _START_RETRIES, market, profile_id,
        )
        return None

    def stop_profile(self, market: str) -> None:
        """Release the profile slot for the given market."""
        profile_id = self._active.pop(market, None)
        if not profile_id or not self.provider:
            return

        stoppers = {
            "gologin":    self._gologin_stop,
            "adspower":   self._adspower_stop,
            "multilogin": self._multilogin_stop,
        }
        stop_fn = stoppers.get(self.provider)
        if stop_fn:
            stop_fn(profile_id)

    def stop_all(self) -> None:
        """Release all active profile slots — call in finally blocks."""
        for market in list(self._active.keys()):
            self.stop_profile(market)

    @property
    def available(self) -> bool:
        """True if a provider is configured and can be used."""
        return bool(self.provider)
