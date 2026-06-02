import logging
from datetime import date

import httpx

from bot.config import Plan, Server

log = logging.getLogger(__name__)


def _admin_url(server: Server, path: str) -> str:
    return f"{server.base_url}/{server.admin_path}/api/v2/admin{path}"


def _headers(server: Server) -> dict[str, str]:
    return {
        "Hiddify-API-Key": server.api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def subscription_url(server: Server, user_uuid: str, label: str = "") -> str:
    url = f"{server.base_url}/{server.user_path}/{user_uuid}/"
    if label:
        from urllib.parse import quote
        url += f"#{quote(label)}"
    return url


async def create_user(server: Server, telegram_id: int, plan: Plan) -> dict:
    name = f"tg_{telegram_id}_{date.today().isoformat()}"
    payload = {
        "added_by_uuid": server.api_key,
        "name": name,
        "comment": f"telegram:{telegram_id} plan:{plan.id} server:{server.id}",
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
        "hiddify create user server=%s telegram_id=%s plan=%s name=%s",
        server.id,
        telegram_id,
        plan.id,
        name,
    )

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            r = await client.post(
                _admin_url(server, "/user/"), headers=_headers(server), json=payload
            )
            r.raise_for_status()
            user = r.json()
        except httpx.HTTPStatusError as e:
            log.error(
                "hiddify create HTTP %s server=%s body=%s",
                e.response.status_code,
                server.id,
                e.response.text[:500],
            )
            raise
        except httpx.HTTPError as e:
            log.error("hiddify create network error server=%s: %s", server.id, e)
            raise

        uid = user.get("uuid")
        log.info("hiddify user created server=%s uuid=%s", server.id, uid)

        if uid:
            try:
                await client.patch(
                    _admin_url(server, f"/user/{uid}/"),
                    headers=_headers(server),
                    json={**payload, "uuid": uid},
                )
                log.debug("hiddify user %s patch OK", uid)
            except httpx.HTTPError as e:
                log.warning("hiddify user %s patch skipped: %s", uid, e)

        return user


async def get_user(server: Server, user_uuid: str, client: httpx.AsyncClient = None) -> dict:
    if client is None:
        async with httpx.AsyncClient(timeout=10) as new_client:
            return await _get_user_req(server, user_uuid, new_client)
    return await _get_user_req(server, user_uuid, client)


async def _get_user_req(server: Server, user_uuid: str, client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(
            _admin_url(server, f"/user/{user_uuid}/"), headers=_headers(server)
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.error("hiddify get user error server=%s uuid=%s: %s", server.id, user_uuid, e)
        return {}
