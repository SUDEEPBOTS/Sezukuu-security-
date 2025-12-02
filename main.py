import logging
import asyncio
from datetime import timedelta, datetime

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
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
    log_appeal,
)
from moderation import moderate_message, evaluate_appeal


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pending_appeals = {}   # user_id → chat_id (group id)
appeal_counts = {}     # user_id → number of appeals


# ───────────── ADMIN CHECK ─────────────

async def is_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user

    # OWNER ko hamesha bypass
    if OWNER_ID and user.id == OWNER_ID:
        return True

    member = await chat.get_member(user.id)
    return member.status in ["administrator", "creator"]


# ───────────── HELPER: TEMP MESSAGE (AUTO DELETE) ─────────────

async def send_temp_message(chat, text: str, seconds: int = 20):
    """
    Group me action message send karega,
    aur 'seconds' ke baad auto delete kar dega.
    """
    try:
        msg = await chat.send_message(text)
    except Exception:
        return

    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except Exception:
        # agar delete fail ho jaye (rights / already deleted) to ignore
        pass


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

    # Only DM se appeal
    if chat.type != "private":
        return await update.message.reply_text("DM me to appeal.")

    if user.id not in pending_appeals:
        return await update.message.reply_text("No active ban appeal found.")

    appeal_text = " ".join(context.args)
    if not appeal_text:
        return await update.message.reply_text("Usage: /appeal <message>")

    group_id = pending_appeals[user.id]

    # Appeal count badhao
    count = appeal_counts.get(user.id, 0) + 1
    appeal_counts[user.id] = count

    # 1–4: AI se auto decision
    if count <= 4:
        decision = evaluate_appeal(appeal_text)
        log_appeal(user.id, group_id, appeal_text, decision["approve"])

        if decision["approve"]:
            try:
                await context.bot.unban_chat_member(group_id, user.id)
            except Exception:
                pass

            await update.message.reply_text("Appeal Approved! You are unbanned.")
            del pending_appeals[user.id]
            appeal_counts.pop(user.id, None)

            # Group ko info (ye bhi temp message bana sakte ho)
            try:
                await send_temp_message(
                    chat=await context.bot.get_chat(group_id),
                    text=f"🔓 Appeal approved for {user.first_name}"
                )
            except Exception:
                pass
        else:
            await update.message.reply_text(
                "Appeal Rejected.\nReason: " + decision["reason"]
            )
        return

    # 5th se aage: admin ke paas manual review ke liye bhejo
    admin_target = OWNER_ID or group_id

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Approve User",
                callback_data=f"approve:{group_id}:{user.id}",
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # admin ko detail bhejo
    try:
        await context.bot.send_message(
            admin_target,
            (
                f"⚠️ Max appeals reached.\n"
                f"User: {user.first_name} (ID: {user.id})\n"
                f"Group ID: {group_id}\n\n"
                f"Last appeal message:\n{appeal_text}"
            ),
            reply_markup=reply_markup,
        )
    except Exception:
        pass

    await update.message.reply_text(
        "Your appeal has been sent to the group admin for manual review."
    )


# ───────────── INLINE BUTTON: ADMIN APPROVE ─────────────

async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, chat_id_str, user_id_str = query.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
    except Exception:
        await query.edit_message_text("Invalid approval data.")
        return

    # Unban + cleanup
    try:
        await context.bot.unban_chat_member(chat_id, user_id)
    except Exception:
        pass

    pending_appeals.pop(user_id, None)
    appeal_counts.pop(user_id, None)

    # User ko DM
    try:
        await context.bot.send_message(
            user_id,
            "✅ Your appeal was approved by admin. You can now rejoin the group.",
        )
    except Exception:
        pass

    await query.edit_message_text(f"User {user_id} approved ✅")


# ───────────── MODERATION ─────────────

async def handle_message(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        return

    if user.is_bot:
        return

    # ───────────── ADMIN BYPASS ─────────────
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

    # User ka message delete (agar AI bole)
    if should_delete or action in ["warn", "mute", "ban", "delete"]:
        try:
            await message.delete()
        except Exception:
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
        # group message 9 sec baad auto delete
        return await send_temp_message(chat, response, seconds=10)

    # MUTE
    if action == "mute":
        until = datetime.utcnow() + timedelta(minutes=MUTE_DURATION_MIN)
        try:
            await chat.restrict_member(
                user_id,
                ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except Exception:
            pass
        return await send_temp_message(
            chat,
            response + f"\nMuted {MUTE_DURATION_MIN} min.",
            seconds=9,
        )

    # BAN
    if action == "ban" or warns >= MAX_WARNINGS:
        try:
            await chat.ban_member(user_id)
        except Exception:
            pass

        pending_appeals[user_id] = chat_id

        # User ko DM me appeal info
        try:
            await context.bot.send_message(
                user_id,
                "⛔ You were banned.\nAppeal: /appeal <reason>",
            )
        except Exception:
            pass

        await send_temp_message(
            chat,
            response + "\nUser Banned.",
            seconds=9,
        )
        reset_warnings(chat_id, user_id)


# ───────────── BOT RUN ─────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setrule", setrule))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("appeal", appeal))

    # Inline approve button handler
    app.add_handler(CallbackQueryHandler(approve_user, pattern=r"^approve:"))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
