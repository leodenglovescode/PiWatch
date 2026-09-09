"""Notification environment, loaded only by the watchdog process."""
import os

HA_BASE_URL = os.environ.get(
    "HA_BASE_URL",
    "http://127.0.0.1:8123",
).rstrip("/")

HA_WEBHOOK_ID = os.environ.get("HA_WEBHOOK_ID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "ssl").lower()
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)
SMTP_TO = os.environ.get("SMTP_TO", SMTP_USERNAME)

LORA_API_URL = os.environ.get(
    "LORA_API_URL",
    "http://127.0.0.1:8989",
).rstrip("/")

LORA_TOKEN = os.environ.get("LORA_TOKEN", "")
