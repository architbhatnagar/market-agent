"""
Telegram bot entrypoint.
Locked to a single Telegram user ID (yours). Provider is chosen via
LLM_PROVIDER in .env: "claude" or "gemini".
"""
import asyncio
import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
from prompts import SUGGEST_PROMPT  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("market-agent")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()

if PROVIDER == "claude":
    from agent_claude import ClaudeAgent as Agent
elif PROVIDER == "gemini":
    from agent_gemini import GeminiAgent as Agent
else:
    raise ValueError(f"Unknown LLM_PROVIDER: {PROVIDER!r} (use 'claude' or 'gemini')")

agent = Agent()


def owner_only(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    await update.message.reply_text(
        f"Market research agent ready (provider: {PROVIDER}). Try:\n\n"
        "\u2022 Quote for RELIANCE.NS and how it's done this year\n"
        "\u2022 Compare TCS.NS vs INFY.NS vs ^GSPC over 3y\n"
        "\u2022 Search Parag Parikh flexi cap and show 3y returns\n"
        "\u2022 Fundamentals of AAPL \u2014 is it expensive right now?\n\n"
        "Tell me your holdings in plain language ('I hold 10 TCS at 3600 avg') "
        "and I'll save them.\n"
        "/suggest \u2014 full portfolio review with buy/sell leanings\n"
        "/reset \u2014 clear conversation context"
    )


async def cmd_suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        answer = await asyncio.to_thread(agent.ask, chat_id, SUGGEST_PROMPT)
    except Exception as e:
        log.exception("Agent error")
        await update.message.reply_text(f"Something broke: {type(e).__name__}: {e}")
        return
    for i in range(0, len(answer), 4000):
        await update.message.reply_text(answer[i:i + 4000])


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    agent.reset(update.effective_chat.id)
    await update.message.reply_text("Context cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        log.warning("Ignored message from user %s",
                    update.effective_user.id if update.effective_user else "?")
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        # Agent calls are blocking (both SDKs are sync here); run off the event loop
        answer = await asyncio.to_thread(agent.ask, chat_id, update.message.text)
    except Exception as e:
        log.exception("Agent error")
        await update.message.reply_text(f"Something broke: {type(e).__name__}: {e}")
        return

    for i in range(0, len(answer), 4000):  # Telegram's 4096-char message limit
        await update.message.reply_text(answer[i:i + 4000])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("suggest", cmd_suggest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot running with provider=%s (polling)...", PROVIDER)
    app.run_polling()


if __name__ == "__main__":
    main()
