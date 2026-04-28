"""
OpenClaw Telegram bot — forwards messages to the /chat API endpoint and
streams the reply back to the user.

Each Telegram user gets their own memory thread (keyed as ``tg:<telegram_user_id>``).
The same thread can be accessed from the web chat by using the same key.

Environment variables (set in .env or /etc/openclaw-control.env):
    TELEGRAM_BOT_TOKEN  — bot token from @BotFather (required)
    CHAT_API_URL        — base URL of the OpenClaw web app
                          (default: http://127.0.0.1:8001)

Usage:
    python -m telegram_bot.bot
    # or via systemd: see systemd/openclaw-telegram-bot.service
"""

from __future__ import annotations

import logging
import os

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

_CHAT_API_BASE = os.environ.get("CHAT_API_URL", "http://127.0.0.1:8001").rstrip("/")
# Note: 8001 is the OpenClaw cockpit port (see Dockerfile/docker-compose.cockpit.yml).
# Override via CHAT_API_URL if the web app runs on a different host or port.
_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


# ── Handlers ──────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hello! I'm the OpenClaw AI assistant, powered by OpenAI.\n\n"
        "Just send me a message and I'll reply. Your conversation history is "
        "preserved between sessions.\n\n"
        "Commands:\n"
        "  /start  — show this message\n"
        "  /clear  — wipe your conversation history\n"
        "  /help   — show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = f"tg:{update.effective_user.id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"{_CHAT_API_BASE}/chat/clear",
                json={"user_id": user_id},
            )
        await update.message.reply_text("🗑️ Conversation history cleared.")
    except Exception as exc:
        log.warning("Failed to clear history for %s: %s", user_id, exc)
        await update.message.reply_text("⚠️ Could not clear history — please try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the user's message to the /chat API and relay the reply."""
    user_id = f"tg:{update.effective_user.id}"
    text = (update.message.text or "").strip()
    if not text:
        return

    await update.message.chat.send_action("typing")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_CHAT_API_BASE}/chat",
                json={"user_id": user_id, "message": text},
            )
            resp.raise_for_status()
            data = resp.json()
            reply: str = data.get("reply") or data.get("error") or "(no response)"
    except httpx.HTTPStatusError as exc:
        log.error("Chat API returned %s: %s", exc.response.status_code, exc.response.text)
        reply = "⚠️ The chat service returned an error. Please try again shortly."
    except Exception as exc:
        log.error("Chat API request failed: %s", exc)
        reply = "⚠️ Could not reach the chat service. Please try again."

    await update.message.reply_text(reply)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    if not _BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. "
            "Add it to your .env or /etc/openclaw-control.env file."
        )

    application = ApplicationBuilder().token(_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    log.info("OpenClaw Telegram bot starting (polling)…")
    application.run_polling()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    main()
