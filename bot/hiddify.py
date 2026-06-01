import logging
from datetime import date

import httpx

from bot.config import (
    HIDDIFY_ADMIN_PATH,
    HIDDIFY_API_KEY,
    HIDDIFY_BASE_URL,
    HIDDIFY_USER_PATH,
    Plan,
)

log = logging.getLogger(__name__)


def _admin_url(path: str) -> str:
    return f"{HIDDIFY_BASE_URL}/{HIDDIFY_ADMIN_PATH}/api/v2/admin{path}"


def _headers() -> dict[str, str]:
    return {
        "Hiddify-API-Key": HIDDIFY_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def subscription_url(user_uuid: str) -> str:
    return f"{HIDDIFY_BASE_URL}/{HIDDIFY_USER_PATH}/{user_uuid}/"


async def create_user(telegram_id: int, plan: Plan) -> dict:
    name = f"tg_{telegram_id}_{date.today().isoformat()}"
    payload = {
        "added_by_uuid": HIDDIFY_API_KEY,
        "name": name,
        "comment": f"telegram:{telegram_id} plan:{plan.id}",
        "current_usage_GB": 0,
        "enable": True,
        "is_active": True,
        "mode": "monthly",
        "package_days": plan.days,
        "start_date": date.today().isoformat(),
        "telegram_id": telegram_id,
        "usage_limit_GB": plan.gb if plan.gb > 0 else 9000,
    }

    log.info(
        "hiddify create user telegram_id=%s plan=%s name=%s days=%s gb_limit=%s",
        telegram_id,
        plan.id,
        name,
        plan.days,
        payload["usage_limit_GB"],
    )

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(_admin_url("/user/"), headers=_headers(), json=payload)
            r.raise_for_status()
            user = r.json()
        except httpx.HTTPStatusError as e:
            log.error(
                "hiddify create HTTP %s body=%s",
                e.response.status_code,
                e.response.text[:500],
            )
            raise
        except httpx.HTTPError as e:
            log.error("hiddify create network error: %s", e)
            raise

        uid = user.get("uuid")
        log.info("hiddify user created uuid=%s", uid)

        if uid:
            try:
                await client.patch(
                    _admin_url(f"/user/{uid}/"),
                    headers=_headers(),
                    json={**payload, "uuid": uid},
                )
                log.debug("hiddify user %s patch OK", uid)
            except httpx.HTTPError as e:
                log.warning("hiddify user %s patch skipped: %s", uid, e)

        return user
