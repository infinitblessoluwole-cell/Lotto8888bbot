"""
Telegram Timer & Countdown Bot
Features:
  - Start countdown timers (seconds / minutes / hours)
  - Pause, resume, cancel
  - Notify on completion
  - Multiple named timers
  - Recurring daily reminders

Author: Senior Telegram Bot Dev
"""

import os
import re
import json
import uuid
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN not set in environment.")

DATA_FILE = Path("timers.json")
TIMEZONE = pytz.UTC  # change to pytz.timezone("Africa/Lagos") if you prefer

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("timer-bot")

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception as e:
            log.error("Failed to load data: %s", e)
    return {"timers": {}, "recurring": {}}


def _save_data(data: dict) -> None:
    try:
        DATA_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.error("Failed to save data: %s", e)


DATA = _load_data()

# ---------------------------------------------------------------------------
# Time parsing helpers
# ---------------------------------------------------------------------------
TIME_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}


def parse_duration(text: str) -> int | None:
    """
    Parse human-readable duration like '5m', '1h30m', '90s', '2 hours'.
    Returns seconds, or None if invalid.
    """
    text = text.lower().strip().replace(" ", "")
    if not text:
        return None

    # If it's just a number, assume seconds
    if text.isdigit():
        return int(text)

    total = 0
    matches = re.findall(r"(\d+)([a-z]+)", text)
    if not matches:
        return None
    for num, unit in matches:
        if unit not in TIME_UNITS:
            return None
        total += int(num) * TIME_UNITS[unit]
    return total if total > 0 else None


def format_seconds(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Timer engine
# ---------------------------------------------------------------------------
def user_timers(chat_id: int) -> dict:
    return DATA["timers"].setdefault(str(chat_id), {})


def schedule_timer(app: Application, chat_id: int, name: str, seconds: int,
                   end_ts: float | None = None, message_id: int | None = None,
                   paused: bool = False):
    """Schedule (or resume) a countdown timer as a JobQueue job."""
    timers = user_timers(chat_id)
    job_name = f"timer:{chat_id}:{name}"

    # Cancel an existing job with same name (idempotent)
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    if paused:
        timers[name] = {
            "remaining": seconds,
            "end_ts": None,
            "paused": True,
            "message_id": message_id,
        }
        _save_data(DATA)
        return

    end = datetime.now(TIMEZONE) + timedelta(seconds=seconds)
    timers[name] = {
        "remaining": seconds,
        "end_ts": end.timestamp(),
        "paused": False,
        "message_id": message_id,
    }
    _save_data(DATA)

    app.job_queue.run_once(
        callback=timer_finished,
        when=seconds,
        name=job_name,
        chat_id=chat_id,
        data={"chat_id": chat_id, "name": name},
    )


async def timer_finished(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    name = job.data["name"]

    timers = user_timers(chat_id)
    if name in timers:
        del timers[name]
        _save_data(DATA)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ *TIME'S UP!*\n\n"
                f"🏷️ Timer: `{name}`\n"
                f"✅ Your countdown has finished."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        log.error("Failed to send timer notification: %s", e)


# ---------------------------------------------------------------------------
# Recurring reminders
# ---------------------------------------------------------------------------
def schedule_recurring(app: Application, chat_id: int, name: str,
                       time_str: str, text: str):
    """
    Schedule a daily reminder at HH:MM (UTC).
    time_str expected as 'HH:MM'.
    """
    hh, mm = map(int, time_str.split(":"))
    tz = TIMEZONE
    run_at = datetime.now(tz).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run_at < datetime.now(tz):
        run_at += timedelta(days=1)

    job_name = f"recurring:{chat_id}:{name}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    app.job_queue.run_daily(
        callback=recurring_fired,
        time=run_at.timetz(),
        name=job_name,
        chat_id=chat_id,
        data={"chat_id": chat_id, "name": name, "text": text},
    )

    recurring = DATA["recurring"].setdefault(str(chat_id), {})
    recurring[name] = {"time": time_str, "text": text}
    _save_data(DATA)


async def recurring_fired(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    d = job.data
    try:
        await context.bot.send_message(
            chat_id=d["chat_id"],
            text=f"🔔 *Recurring Reminder:* `{d['name']}`\n\n{d['text']}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        log.error("Recurring reminder failed: %s", e)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
WELCOME = (
    "👋 *Welcome to Lotto8888 Timer Bot!*\n\n"
    "I can manage countdown timers and daily reminders for you.\n\n"
    "*Commands:*\n"
    "• `/timer <name> <duration>` — start a timer\n"
    "    e.g. `/timer pizza 10m`, `/timer workout 1h30m`, `/timer tea 90s`\n"
    "• `/list` — list your active timers\n"
    "• `/pause <name>` — pause a timer\n"
    "• `/resume <name>` — resume a paused timer\n"
    "• `/cancel <name>` — cancel a timer\n"
    "• `/cancelall` — cancel every timer\n"
    "• `/remind <name> <HH:MM> <text>` — daily recurring reminder (UTC)\n"
    "• `/reminders` — list recurring reminders\n"
    "• `/unremind <name>` — delete a recurring reminder\n"
    "• `/help` — show this message\n\n"
    "⏱️ *Duration format:* `30s`, `5m`, `2h`, `1h30m`, or plain seconds."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❗ Usage: `/timer <name> <duration>`\nExample: `/timer pizza 10m`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    name = args[0].lower()
    duration_text = "".join(args[1:])
    seconds = parse_duration(duration_text)

    if not seconds:
        await update.message.reply_text(
            "❌ Invalid duration. Try `30s`, `5m`, `1h30m`, or seconds.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat_id = update.effective_chat.id
    timers = user_timers(chat_id)
    if name in timers:
        await update.message.reply_text(
            f"⚠️ A timer named `{name}` already exists. Cancel it first or pick another name.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.message.reply_text(
        f"⏱️ Timer *{name}* started for *{format_seconds(seconds)}*.\n"
        f"I'll notify you when it's done!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_timer_keyboard(name),
    )

    schedule_timer(
        context.application, chat_id, name, seconds,
        message_id=msg.message_id,
    )


def _timer_keyboard(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data=f"pause:{name}"),
            InlineKeyboardButton("▶️ Resume", callback_data=f"resume:{name}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{name}"),
        ]
    ])


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    timers = user_timers(chat_id)
    if not timers:
        await update.message.reply_text("📭 You have no active timers.")
        return

    now = datetime.now(TIMEZONE).timestamp()
    lines = ["⏱️ *Your Active Timers:*\n"]
    for name, info in timers.items():
        if info.get("paused"):
            status = f"⏸ Paused — {format_seconds(int(info['remaining']))} left"
        else:
            remaining = max(0, int(info["end_ts"] - now))
            status = f"▶️ Running — {format_seconds(remaining)} left"
        lines.append(f"• `{name}` — {status}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]
        ]),
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Usage: `/pause <name>`", parse_mode=ParseMode.MARKDOWN)
        return
    await _do_pause(update.effective_chat.id, context.args[0].lower(), update, context)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Usage: `/resume <name>`", parse_mode=ParseMode.MARKDOWN)
        return
    await _do_resume(update.effective_chat.id, context.args[0].lower(), update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Usage: `/cancel <name>`", parse_mode=ParseMode.MARKDOWN)
        return
    await _do_cancel(update.effective_chat.id, context.args[0].lower(), update, context)


async def cmd_cancelall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    timers = user_timers(chat_id)
    if not timers:
        await update.message.reply_text("📭 No timers to cancel.")
        return
    names = list(timers.keys())
    for name in names:
        job_name = f"timer:{chat_id}:{name}"
        for job in context.application.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        timers.pop(name, None)
    _save_data(DATA)
    await update.message.reply_text(f"🗑️ Cancelled *{len(names)}* timer(s).", parse_mode=ParseMode.MARKDOWN)


# ---- Internal pause/resume/cancel helpers --------------------------------
async def _do_pause(chat_id: int, name: str, update, context):
    timers = user_timers(chat_id)
    info = timers.get(name)
    if not info:
        await update.message.reply_text(f"❓ No timer named `{name}`.", parse_mode=ParseMode.MARKDOWN)
        return
    if info.get("paused"):
        await update.message.reply_text(f"⏸ `{name}` is already paused.", parse_mode=ParseMode.MARKDOWN)
        return

    remaining = max(0, int(info["end_ts"] - datetime.now(TIMEZONE).timestamp()))
    info["paused"] = True
    info["remaining"] = remaining
    info["end_ts"] = None
    _save_data(DATA)

    for job in context.application.job_queue.get_jobs_by_name(f"timer:{chat_id}:{name}"):
        job.schedule_removal()

    await update.message.reply_text(
        f"⏸ Timer `{name}` paused. Remaining: *{format_seconds(remaining)}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_resume(chat_id: int, name: str, update, context):
    timers = user_timers(chat_id)
    info = timers.get(name)
    if not info:
        await update.message.reply_text(f"❓ No timer named `{name}`.", parse_mode=ParseMode.MARKDOWN)
        return
    if not info.get("paused"):
        await update.message.reply_text(f"▶️ `{name}` is not paused.", parse_mode=ParseMode.MARKDOWN)
        return

    seconds = int(info["remaining"])
    schedule_timer(context.application, chat_id, name, seconds, message_id=info.get("message_id"))
    await update.message.reply_text(
        f"▶️ Timer `{name}` resumed for *{format_seconds(seconds)}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_cancel(chat_id: int, name: str, update, context):
    timers = user_timers(chat_id)
    if name not in timers:
        await update.message.reply_text(f"❓ No timer named `{name}`.", parse_mode=ParseMode.MARKDOWN)
        return
    timers.pop(name)
    _save_data(DATA)
    for job in context.application.job_queue.get_jobs_by_name(f"timer:{chat_id}:{name}"):
        job.schedule_removal()
    await update.message.reply_text(f"🗑️ Timer `{name}` cancelled.", parse_mode=ParseMode.MARKDOWN)


# ---- Recurring reminder commands -----------------------------------------
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❗ Usage: `/remind <name> <HH:MM> <text>`\n"
            "Example: `/remind gym 06:30 Time to exercise!`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    name = args[0].lower()
    time_str = args[1]
    text = " ".join(args[2:])

    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_str):
        await update.message.reply_text(
            "❌ Time must be in 24-hour `HH:MM` format (UTC).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat_id = update.effective_chat.id
    schedule_recurring(context.application, chat_id, name, time_str, text)

    await update.message.reply_text(
        f"🔔 Daily reminder `{name}` set for *{time_str} UTC*.\n📝 {text}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rec = DATA["recurring"].get(str(chat_id), {})
    if not rec:
        await update.message.reply_text("📭 No recurring reminders set.")
        return
    lines = ["🔔 *Your Daily Reminders:*\n"]
    for name, info in rec.items():
        lines.append(f"• `{name}` at *{info['time']} UTC* — {info['text']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_unremind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Usage: `/unremind <name>`", parse_mode=ParseMode.MARKDOWN)
        return
    name = context.args[0].lower()
    chat_id = update.effective_chat.id
    rec = DATA["recurring"].get(str(chat_id), {})
    if name not in rec:
        await update.message.reply_text(f"❓ No reminder named `{name}`.", parse_mode=ParseMode.MARKDOWN)
        return
    rec.pop(name)
    _save_data(DATA)
    for job in context.application.job_queue.get_jobs_by_name(f"recurring:{chat_id}:{name}"):
        job.schedule_removal()
    await update.message.reply_text(f"🗑️ Reminder `{name}` removed.", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Inline button callbacks
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "refresh":
        chat_id = query.message.chat_id
        timers = user_timers(chat_id)
        if not timers:
            await query.edit_message_text("📭 You have no active timers.")
            return
        now = datetime.now(TIMEZONE).timestamp()
        lines = ["⏱️ *Your Active Timers:*\n"]
        for name, info in timers.items():
            if info.get("paused"):
                status = f"⏸ Paused — {format_seconds(int(info['remaining']))} left"
            else:
                remaining = max(0, int(info["end_ts"] - now))
                status = f"▶️ Running — {format_seconds(remaining)} left"
            lines.append(f"• `{name}` — {status}")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]
            ]),
        )
        return

    action, _, name = data.partition(":")
    chat_id = query.message.chat_id

    if action == "pause":
        await _do_pause_from_cb(chat_id, name, context, query)
    elif action == "resume":
        await _do_resume_from_cb(chat_id, name, context, query)
    elif action == "cancel":
        await _do_cancel_from_cb(chat_id, name, context, query)


async def _do_pause_from_cb(chat_id, name, context, query):
    timers = user_timers(chat_id)
    info = timers.get(name)
    if not info or info.get("paused"):
        await query.answer("Timer not running.", show_alert=True)
        return
    remaining = max(0, int(info["end_ts"] - datetime.now(TIMEZONE).timestamp()))
    info["paused"] = True
    info["remaining"] = remaining
    info["end_ts"] = None
    _save_data(DATA)
    for job in context.application.job_queue.get_jobs_by_name(f"timer:{chat_id}:{name}"):
        job.schedule_removal()
    await query.edit_message_text(
        f"⏸ Timer `{name}` paused. Remaining: *{format_seconds(remaining)}*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_timer_keyboard(name),
    )


async def _do_resume_from_cb(chat_id, name, context, query):
    timers = user_timers(chat_id)
    info = timers.get(name)
    if not info or not info.get("paused"):
        await query.answer("Timer is not paused.", show_alert=True)
        return
    seconds = int(info["remaining"])
    schedule_timer(context.application, chat_id, name, seconds, message_id=query.message.message_id)
    await query.edit_message_text(
        f"▶️ Timer `{name}` resumed for *{format_seconds(seconds)}*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_timer_keyboard(name),
    )


async def _do_cancel_from_cb(chat_id, name, context, query):
    timers = user_timers(chat_id)
    if name not in timers:
        await query.answer("Timer not found.", show_alert=True)
        return
    timers.pop(name)
    _save_data(DATA)
    for job in context.application.job_queue.get_jobs_by_name(f"timer:{chat_id}:{name}"):
        job.schedule_removal()
    await query.edit_message_text(f"🗑️ Timer `{name}` cancelled.")


# ---------------------------------------------------------------------------
# Fallback for unknown text
# ---------------------------------------------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 I didn't understand that. Use /help to see available commands.",
    )


# ---------------------------------------------------------------------------
# Startup: restore jobs after restart (Railway redeploys, crashes, etc.)
# ---------------------------------------------------------------------------
async def post_init(app: Application):
    log.info("Restoring persisted timers & reminders...")
    now = datetime.now(TIMEZONE).timestamp()

    # Restore countdown timers
    for chat_id_str, timers in list(DATA["timers"].items()):
        chat_id = int(chat_id_str)
        for name, info in list(timers.items()):
            if info.get("paused"):
                continue
            end_ts = info.get("end_ts")
            if not end_ts:
                continue
            remaining = int(end_ts - now)
            if remaining <= 0:
                # Missed while offline → notify immediately
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏰ *TIME'S UP!* (missed while offline)\n\n🏷️ Timer: `{name}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception as e:
                    log.error("Missed-timer notify failed: %s", e)
                timers.pop(name, None)
            else:
                schedule_timer(app, chat_id, name, remaining)

    # Restore recurring reminders
    for chat_id_str, recs in DATA["recurring"].items():
        chat_id = int(chat_id_str)
        for name, info in recs.items():
            schedule_recurring(app, chat_id, name, info["time"], info["text"])

    _save_data(DATA)
    log.info("Restore complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("Starting Lotto8888 Timer Bot...")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("timer", cmd_timer))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("timers", cmd_list))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("cancelall", cmd_cancelall))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("unremind", cmd_unremind))

    # Callbacks + fallback
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
