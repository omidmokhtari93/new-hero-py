import logging

import httpx

log = logging.getLogger(__name__)

_TGJU_URL = "https://call2.tgju.org/ajax.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vpn-bot/1.0)"}


async def fetch_prices() -> dict | None:
    """Fetch gold and currency prices from tgju.org public API."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_TGJU_URL, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json().get("current", {})

        def _p(key: str) -> str:
            """Return the price for `key` converted from Rial to Toman."""
            raw = data.get(key, {}).get("p")
            if not raw:
                return "—"
            try:
                return f"{int(raw.replace(',', '')) // 10:,}"
            except ValueError:
                return "—"

        return {
            "usd": _p("price_dollar_rl"),
            "eur": _p("price_eur"),
            "gold18": _p("geram18"),
            "coin": _p("sekeb"),
        }
    except Exception as e:
        log.error("Failed to fetch prices from tgju.org: %s", e)
        return None
