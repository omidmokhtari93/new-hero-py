import logging

import httpx

from bot.config import NOVINOPAY_MERCHANT_ID, PAYMENT_CALLBACK_BASE

log = logging.getLogger(__name__)

# https://novinopay.com/docs
NOVINOPAY_REQUEST_URL = "http://joorabino1.ir/payment/ipg/v2/request"
NOVINOPAY_VERIFY_URL = "http://joorabino1.ir/payment/ipg/v2/verification"


def _ok_status(status) -> bool:
    return str(status) in ("100", "101")


def _merchant_hint() -> str:
    m = NOVINOPAY_MERCHANT_ID
    if m == "test":
        return "test"
    return f"{m[:8]}..." if len(m) > 8 else m


async def create_payment_link(
    amount_rial: int, description: str, order_id: int
) -> tuple[str, str]:
    callback_url = f"{PAYMENT_CALLBACK_BASE}/payment/callback"
    log.info(
        "novinopay request order=%s amount=%s callback=%s merchant=%s",
        order_id,
        amount_rial,
        callback_url,
        _merchant_hint(),
    )
    body = {
        "merchant_id": NOVINOPAY_MERCHANT_ID,
        "amount": amount_rial,
        "callback_url": callback_url,
        "callback_method": "GET",
        "invoice_id": str(order_id),
        "description": description[:255],
    }


    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(NOVINOPAY_REQUEST_URL, json=body)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            log.error(
                "novinopay request HTTP %s body=%s",
                e.response.status_code,
                e.response.text[:500],
            )
            raise
        except httpx.HTTPError as e:
            log.error("novinopay request network error: %s", e)
            raise

    status = data.get("status")
    if not _ok_status(status):
        log.error(
            "novinopay request rejected status=%s message=%s errors=%s",
            status,
            data.get("message"),
            data.get("errors"),
        )
        raise RuntimeError(
            f"Novinopay request failed: status={status} message={data.get('message')}"
        )

    payload = data["data"]
    authority = payload["authority"]
    pay_url = payload["payment_url"]
    log.info(
        "novinopay request OK order=%s authority=%s trans_id=%s",
        order_id,
        authority,
        payload.get("trans_id"),
    )
    return authority, pay_url


async def verify_payment(amount_rial: int, authority: str) -> bool:
    body = {
        "merchant_id": NOVINOPAY_MERCHANT_ID,
        "amount": amount_rial,
        "authority": authority,
    }

    log.info("novinopay verify authority=%s amount=%s", authority, amount_rial)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(NOVINOPAY_VERIFY_URL, json=body)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            log.error(
                "novinopay verify HTTP %s body=%s",
                e.response.status_code,
                e.response.text[:500],
            )
            raise
        except httpx.HTTPError as e:
            log.error("novinopay verify network error: %s", e)
            raise

    status = data.get("status")
    ok = _ok_status(status)
    if ok:
        info = data.get("data") or {}
        log.info(
            "novinopay verify OK authority=%s ref_id=%s trans_id=%s",
            authority,
            info.get("ref_id"),
            info.get("trans_id"),
        )
    else:
        log.warning(
            "novinopay verify failed status=%s message=%s errors=%s",
            status,
            data.get("message"),
            data.get("errors"),
        )
    return ok
