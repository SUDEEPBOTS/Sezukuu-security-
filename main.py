import logging
from datetime import timedelta, datetime
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from config import (
    BOT_TOKEN,
    OWNER_ID,
    MAX_WARNINGS,
    MUTE_DURATION_MIN,
    ENABLE_AUTO_BAN,
    ENABLE_AUTO_DELETE,
    ENABLE_AUTO_MUTE,
)
from models import (
    add_group,
    add_user,
    add_rule_db,
    get_rules_db,
    increment_warning,
    reset_warnings,
    get_all_warnings,
    log_action,
    log_appeal
)
from moderation import moderate_message, evaluate_appeal


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pending_appeals = {}   # user_id → chat_id


# ───────────── ADMIN CHECK ─────────────

async def is_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if OWNER_ID and user.id == OWNER_ID:
        return True

    member = await chat.get_member(user.id)
    return member.status in ["administrator", "creator"]


# ───────────── COMMANDS ─────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    add_user(user.id, user.username or user.first_name)

    if chat.type != "private":
        add_group(chat.id, chat.title, user.id)
        return await update.message.reply_text(
            "🤖 AI Moderator Active.\nUse /setrule to add rules."
        )

    await update.message.reply_text(
        "👋 This is AI moderation bot.\n"
        "Use /appeal in DM if banned."
    )


async def setrule(update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("Admin only.")

    chat_id = update.effective_chat.id
    text = " ".join(context.args)

    if not text:
        return await update.message.reply_text("Usage: /setrule <rule>")

    add_rule_db(chat_id, text)

    rules = get_rules_db(chat_id)
    rr = "\n".join([f"{i+1}. {r}" for i, r in enumerate(rules)])

    await update.message.reply_text(f"Rule Added!\n\n{rr}")


async def show_rules(update, context):
    rules = get_rules_db(update.effective_chat.id)
    if not rules:
        return await update.message.reply_text("No rules set.")

    rr = "\n".join([f"{i+1}. {r}" for i, r in enumerate(rules)])
    await update.message.reply_text(f"📜 RULES:\n\n{rr}")


async def status(update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("Admin only.")

    chat_id = update.effective_chat.id
    warns = get_all_warnings(chat_id)

    if not warns:
        return await update.message.reply_text("No warnings.")

    msg = "⚠️ WARNINGS:\n"
    for w in warns:
        msg += f"User {w['user_id']} → {w['warnings']}\n"

    await update.message.reply_text(msg)


# ───────────── APPEAL ─────────────

async def appeal(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        return await update.message.reply_text("DM me to appeal.")

    if user.id not in pending_appeals:
        return await update.message.reply_text("No active ban appeal found.")

    appeal_text = " ".join(context.args)
    if not appeal_text:
        return await update.message.reply_text("Usage: /appeal <message>")

    group = pending_appeals[user.id]

    decision = evaluate_appeal(appeal_text)
    log_appeal(user.id, group, appeal_text, decision["approve"])

    if decision["approve"]:
        try:
            await context.bot.unban_chat_member(group, user.id)
        except:
            pass

        await update.message.reply_text("Appeal Approved! You are unbanned.")
        del pending_appeals[user.id]

        await context.bot.send_message(
            group,
            f"🔓 Appeal approved for {user.first_name}"
        )
    else:
        await update.message.reply_text("Appeal Rejected.\nReason: " + decision["reason"])


# ───────────── MODERATION ─────────────
 async def handle_message(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    if user.is_bot:
        return

    # ───────────── ADMIN BYPASS (NEW) ─────────────
    if await is_admin(update, context):
        return  # admin ko kuch na bole

    text = message.text or message.caption
    if not text:
        return

    chat_id = chat.id
    user_id = user.id

    rules = get_rules_db(chat_id)
    rules_text = "\n".join(rules)

    result = moderate_message(text, user, chat, rules_text)

    action = result["action"]
    reason = result["reason"]
    severity = result["severity"]
    should_delete = result["should_delete"]

    # Auto-delete or punishable actions
    if should_delete or action in ["warn", "mute", "ban", "delete"]:
        try:
            await message.delete()
        except:
            pass

    if action == "allow":
        return

    warns = increment_warning(chat_id, user_id)

    log_action(chat_id, user_id, action, reason)

    response = (
        f"🚨 Rule Violation\n"
        f"User: {user.first_name}\n"
        f"Reason: {reason}\n"
        f"Warnings: {warns}/{MAX_WARNINGS}"
    )

    # WARN
    if action == "warn":
        return await chat.send_message(response)

    # MUTE
    if action == "mute":
        until = datetime.utcnow() + timedelta(minutes=MUTE_DURATION_MIN)
        try:
            await chat.restrict_member(
                user_id,
                ChatPermissions(can_send_messages=False),
                until_date=until
            )
        except:
            pass
        return await chat.send_message(response + f"\nMuted {MUTE_DURATION_MIN} min.")

    # BAN
    if action == "ban" or warns >= MAX_WARNINGS:
        try:
            await chat.ban_member(user_id)
        except:
            pass

        pending_appeals[user_id] = chat_id

        try:
            await context.bot.send_message(
                user_id,
                "⛔ You were banned.\nAppeal: /appeal <reason>"
            )
        except:
            pass

        await chat.send_message(response + "\nUser Banned.")
        reset_warnings(chat_id, user_id)


# ───────────── BOT RUN ─────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setrule", setrule))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("appeal", appeal))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
