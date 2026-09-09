import asyncio
import json
import urllib.request
from .settings import HA_BASE_URL, HA_WEBHOOK_ID


def send_ha_webhook_sync(
    title,
    message,
    severity,
    settings=None,
):
    settings = settings or {}
    if not settings.get("enabled", True):
        raise RuntimeError("Gateway is disabled")
    ha_base_url = settings.get('url', HA_BASE_URL)
    ha_webhook_id = settings.get('webhook_id', HA_WEBHOOK_ID)


    if not ha_webhook_id:
        raise RuntimeError(
            "ha_webhook_id is not configured"
        )

    url = (
        f"{ha_base_url}/api/webhook/"
        f"{ha_webhook_id}"
    )

    payload = json.dumps({
        "title": title,
        "message": message,
        "severity": severity,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "piwatchd/1.4",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=3,
    ) as response:

        if not (
            200
            <= response.status
            < 400
        ):
            raise RuntimeError(
                f"HA webhook HTTP "
                f"{response.status}"
            )


async def send_ha_webhook(
    title,
    message,
    severity,
    settings=None,
):

    await asyncio.to_thread(
        send_ha_webhook_sync,
        title,
        message,
        severity,
        settings,
    )
