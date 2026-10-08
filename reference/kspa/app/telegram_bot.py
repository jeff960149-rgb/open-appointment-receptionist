import hashlib
import logging
from collections.abc import Collection

import httpx


logger = logging.getLogger("ai-receptionist.telegram")
TELEGRAM_API_BASE = "https://api.telegram.org"


def parse_telegram_id_list(raw_ids: str) -> frozenset[str]:
    """Parse a comma-separated Telegram user-id allowlist."""
    return frozenset(
        telegram_id.strip()
        for telegram_id in raw_ids.split(",")
        if telegram_id.strip()
    )


def telegram_sender_is_authorized(
    *,
    chat_id: str,
    chat_type: str,
    sender_id: str,
    configured_chat_id: str,
    admin_user_ids: Collection[str],
) -> bool:
    """Require both the configured chat and an allowed sender in group chats."""
    if not chat_id or chat_id != configured_chat_id or not sender_id:
        return False
    if admin_user_ids:
        return sender_id in admin_user_ids
    # Preserve the existing single-admin private chat until an allowlist is set.
    return chat_type == "private" and sender_id == chat_id


def telegram_webhook_secret(bot_token: str) -> str:
    """Derive a stable webhook secret without adding another stored credential."""
    return hashlib.sha256(f"kspa-telegram:{bot_token}".encode()).hexdigest()


async def configure_telegram_webhook(
    bot_token: str,
    webhook_url: str,
    secret_token: str,
) -> None:
    """Register the Kspa admin webhook with Telegram."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret_token,
                "allowed_updates": ["message"],
            },
        )
    if response.is_error:
        logger.error("Telegram webhook setup failed: status=%s", response.status_code)
    response.raise_for_status()


async def send_telegram_text(bot_token: str, chat_id: str, text: str) -> None:
    """Send one staff notification without logging credentials or message contents."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096]},
        )
    if response.is_error:
        details = " ".join(response.text.split())[:1000]
        logger.error(
            "Telegram API request failed: status=%s body=%s",
            response.status_code,
            details,
        )
    response.raise_for_status()
