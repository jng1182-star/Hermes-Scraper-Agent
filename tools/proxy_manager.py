"""
Geo Proxy Manager — routes scraper sessions through localized residential proxies.

Why this matters for ad intelligence:
  Social platforms serve geo-targeted ads based on the requesting IP's geolocation.
  A scraper running from a Singapore or US IP will see ads served to that audience,
  not to the target market (e.g. Philippines). Residential proxies with the correct
  country egress ensure the scraper observes the same ad inventory as real users in
  that market.

Configuration:
  Set proxy URLs in .env using the format:
    PROXY_TH=socks5://username:password@host:port
    PROXY_PH=socks5://username:password@host:port
    PROXY_VN=socks5://username:password@host:port
    PROXY_ID=socks5://username:password@host:port
    PROXY_MY=socks5://username:password@host:port
    PROXY_SG=socks5://username:password@host:port

  Supports HTTP, HTTPS, and SOCKS5 — any format Playwright's proxy parameter accepts.
  If a proxy is not configured for a market, the scraper degrades gracefully (warning,
  no crash) but the data quality caveat is noted in the report output.

Usage:
  from tools.proxy_manager import get_proxy
  proxy_cfg = get_proxy("PH")   # returns dict or None
  context = await browser.new_context(..., proxy=proxy_cfg)
"""

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Market code → env var mapping ────────────────────────────────────────────
_PROXY_ENV_VARS = {
    "TH": "PROXY_TH",   # Thailand
    "PH": "PROXY_PH",   # Philippines
    "VN": "PROXY_VN",   # Vietnam
    "ID": "PROXY_ID",   # Indonesia
    "MY": "PROXY_MY",   # Malaysia
    "SG": "PROXY_SG",   # Singapore
}

# ── Full country name → market code lookup ────────────────────────────────────
COUNTRY_TO_CODE = {
    "Thailand":    "TH",
    "Philippines": "PH",
    "Vietnam":     "VN",
    "Indonesia":   "ID",
    "Malaysia":    "MY",
    "Singapore":   "SG",
}


def get_proxy(market: str) -> dict | None:
    """
    Return a Playwright-compatible proxy config dict for the given market,
    or None if no proxy is configured.

    Args:
        market: ISO 2-letter market code (e.g. "PH") OR full country name (e.g. "Philippines").

    Returns:
        {"server": "...", "username": "...", "password": "..."} or None.
        Playwright treats proxy=None as no proxy — no special handling needed by callers.
    """
    # Normalize: accept both "Philippines" and "PH"
    code = COUNTRY_TO_CODE.get(market, market.upper() if len(market) == 2 else None)
    if not code:
        return None

    env_var = _PROXY_ENV_VARS.get(code)
    if not env_var:
        return None

    raw_url = os.getenv(env_var, "").strip()
    if not raw_url:
        logger.warning(
            "[ProxyManager] No geo proxy configured for market '%s' (%s not set). "
            "Ad data may not reflect local inventory — scraper IP geolocation will differ "
            "from target market. Set %s=<proxy_url> in .env to enable geo-accurate scraping.",
            code, env_var, env_var,
        )
        return None

    _ALLOWED_SCHEMES = {"http", "https", "socks5"}
    try:
        parsed = urlparse(raw_url)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            logger.error(
                "[ProxyManager] Rejected proxy URL for market '%s': scheme '%s' not allowed "
                "(must be http, https, or socks5). Check %s in .env.",
                code, parsed.scheme, env_var,
            )
            return None
        if not parsed.hostname:
            logger.error("[ProxyManager] Rejected proxy URL for market '%s': no hostname.", code)
            return None
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        proxy_cfg: dict = {"server": server}
        if parsed.username:
            proxy_cfg["username"] = parsed.username
        if parsed.password:
            proxy_cfg["password"] = parsed.password
        logger.info("[ProxyManager] Geo proxy active for market '%s' via %s", code, server)
        return proxy_cfg
    except Exception as exc:
        logger.error(
            "[ProxyManager] Failed to parse proxy URL for market '%s' from %s: %s",
            code, env_var, exc,
        )
        return None


def proxy_status(market: str) -> str:
    """Return a human-readable audit string describing proxy status (no server address exposed)."""
    code = COUNTRY_TO_CODE.get(market, market.upper() if len(market) == 2 else "")
    env_var = _PROXY_ENV_VARS.get(code, "")
    configured = bool(env_var and os.getenv(env_var, "").strip())
    if configured:
        return "proxied (active)"
    return "unproxied — ad data may not reflect local market inventory"
