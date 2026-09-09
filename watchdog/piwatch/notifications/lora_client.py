import json
import urllib.request
from .settings import LORA_API_URL, LORA_TOKEN


def lora_api_send(
    message,
    mode="text",
    ack=True,
    request_id=None,
    settings=None,
):
    settings = settings or {}
    if not settings.get("enabled", True):
        raise RuntimeError("Gateway is disabled")
    lora_api_url = settings.get('url', LORA_API_URL)
    lora_token = settings.get('token', LORA_TOKEN)


    if not lora_token:
        raise RuntimeError(
            "lora_token is not configured"
        )

    if len(
        message.encode("utf-8")
    ) > 200:
        raise RuntimeError(
            "LoRa message exceeded "
            "200-byte PiWatch limit"
        )

    payload = json.dumps({
        "message": message,
        "mode": mode,
        "ack": ack,
        "request_id": request_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{lora_api_url}/v1/notify",
        data=payload,
        method="POST",
        headers={
            "Authorization":
                f"Bearer {lora_token}",
            "Content-Type":
                "application/json",
            "User-Agent":
                "piwatchd/1.4",
        },
    )

    with urllib.request.urlopen(
        req,
        timeout=65,
    ) as response:

        body = response.read().decode(
            "utf-8",
            errors="replace",
        )

    result = json.loads(body)

    if not result.get("ok"):

        raise RuntimeError(
            f"LoRa API error: {result}"
        )

    if (
        ack
        and not result.get("delivered")
    ):

        raise RuntimeError(
            "LoRa destination did not ACK: "
            f"{result}"
        )

    return result
