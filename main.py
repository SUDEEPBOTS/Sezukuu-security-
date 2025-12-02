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

# --------------- GLOBALS ----------------
pending_appeals = {}       # user_id → set of group_ids
appeal_counts  = {}        # user_id → count


# --------------- ADMIN CHECK ----------------
async def is_admin(update, context):
    chat = update.effective_chat
    user = update.effective_user

    if OWNER_ID and user.id == OWNER_ID:
        return True

    member = await chat.get_member(user.id)
    return member.status in ["administrator", "creator"]


# --------------- TEMP MESSAGE ----------------
async def send_temp_message(chat, text, seconds=15):
    try:
        msg = await chat.send_message(text)
    except:
        return
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
    except:
        pass


# ---------------------------------------------------------
#                      START + VERIFY
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    bot = context.bot

    add_user(user.id, user.username or user.first_name)

    # GROUP /start
    if chat.type != "private":
        add_group(chat.id, chat.title, user.id)
        return await update.message.reply_text("🤖 AI Moderator Ready.")

    # DM verification: /start verify_<groupid>
    if context.args and context.args[0].startswith("verify_"):
        try:
            group_id = int(context.args[0].split("_")[1])
        except:
            return await update.message.reply_text("Invalid verify link.")

        # Unmute user in group
        try:
            await bot.restrict_chat_member(
                group_id,
                user.id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                )
            )
        except:
            pass

        # DM success msg
        await update.message.reply_text("✅ Verified successfully!")

        # Inform group
        try:
            await bot.send_message(
                group_id,
                f"{user.first_name} is now verified and unmuted! ✅"
            )
        except:
            pass

        return

    # Normal DM start
    await update.message.reply_text("Use /appeal if banned.")


# ----------------- WELCOME + VERIFY BUTTON -----------------
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    bot = context.bot

    new_members = message.new_chat_members
    if not new_members:
        return

    bot_username = bot.username

    for member in new_members:
        if member.is_bot:
            continue

        add_user(member.id, member.username or member.first_name)

        # Mute new user
        try:
            await chat.restrict_member(member.id, ChatPermissions(can_send_messages=False))
        except:
            pass

        verify_link = f"https://t.me/{bot_username}?start=verify_{chat.id}"

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Verify", url=verify_link)]]
        )

        await chat.send_message(
            f"Welcome {member.first_name}! 👋\nPlease verify to chat.",
            reply_markup=kb
        )


# ---------------------------------------------------------
#                        RULES
# ---------------------------------------------------------
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

    warns = get_all_warnings(update.effective_chat.id)
    if not warns:
        return await update.message.reply_text("No warnings.")

    msg = "⚠️ WARNINGS:\n"
    for w in warns:
        msg += f"User {w['user_id']} → {w['warnings']}\n"

    await update.message.reply_text(msg)


# ---------------------------------------------------------
#                        APPEALS
# ---------------------------------------------------------
async def appeal(update, context):
    chat = update.effective_chat
    user  = update.effective_user
    bot   = context.bot

    if chat.type != "private":
        return await update.message.reply_text("DM me to use /appeal.")

    if user.id not in pending_appeals:
        return await update.message.reply_text("No active bans found.")

    appeal_text = " ".join(context.args)
    if not appeal_text:
        return await update.message.reply_text("Usage: /appeal <reason>")

    # ALL groups where user is banned
    groups = list(pending_appeals[user.id])

    # Count += 1
    count = appeal_counts.get(user.id, 0) + 1
    appeal_counts[user.id] = count

    # ---------- AI DECISION (1–2) ----------
    if count <= 2:
        decision = evaluate_appeal(appeal_text)

        if decision["approve"]:
            for gid in groups:
                log_appeal(user.id, gid, appeal_text, True)
                try:
                    await bot.unban_chat_member(gid, user.id)
                except:
                    pass

            await update.message.reply_text("✅ Appeal Approved! You are unbanned everywhere.")

            # cleanup
            pending_appeals.pop(user.id, None)
            appeal_counts.pop(user.id, None)

        else:
            for gid in groups:
                log_appeal(user.id, gid, appeal_text, False)

            await update.message.reply_text("❌ Appeal Rejected.\n" + decision["reason"])

        return

    # ------------ ADMIN REVIEW (3rd+) ------------
    primary_gid = groups[0]
    admin_target = OWNER_ID or primary_gid

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Approve User", callback_data=f"approve:{user.id}")]
    ])

    try:
        await bot.send_message(
            admin_target,
            f"⚠️ Manual Review Needed\nUser: {user.first_name} ({user.id})\nAppeal:\n{appeal_text}",
            reply_markup=kb
        )
    except:
        pass

    await update.message.reply_text("Your appeal has been sent to admin.")


# -------- ADMIN APPROVE BUTTON --------
async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot = context.bot

    await query.answer()

    try:
        _, user_id_str = query.data.split(":")
        user_id = int(user_id_str)
    except:
        return await query.edit_message_text("Error.")

    groups = list(pending_appeals.get(user_id, []))

    for gid in groups:
        try:
            await bot.unban_chat_member(gid, user_id)
        except:
            pass

    # cleanup
    pending_appeals.pop(user_id, None)
    appeal_counts.pop(user_id, None)

    try:
        await bot.send_message(user_id, "Your appeal was approved by admin.")
    except:
        pass

    await query.edit_message_text("User unbanned from all groups. ✅")


# ---------------------------------------------------------
#                      MESSAGE MODERATION
# ---------------------------------------------------------
async def handle_message(update, context):
    message = update.effective_message
    chat    = update.effective_chat
    user    = update.effective_user
    bot     = context.bot

    if chat.type == "private":
        return
    if user.is_bot:
        return
    if await is_admin(update, context):
        return

    text = message.text or message.caption
    if not text:
        return

    chat_id = chat.id
    user_id = user.id

    rules = get_rules_db(chat_id)
    rules_text = "\n".join(rules)

    result = moderate_message(text, user, chat, rules_text)

    action       = result["action"]
    reason       = result["reason"]
    should_delete = result["should_delete"]

    # delete user message
    if should_delete or action in ["warn", "mute", "ban"]:
        try:
            await message.delete()
        except:
            pass

    if action == "allow":
        return

    # Warning count update
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
        return await send_temp_message(chat, response, 10)

    # MUTE
    if action == "mute":
        until = datetime.utcnow() + timedelta(minutes=MUTE_DURATION_MIN)
        try:
            await chat.restrict_member(user_id, ChatPermissions(can_send_messages=False), until)
        except:
            pass
        return await send_temp_message(chat, response + "\nMuted.", 10)

    # BAN
    if action == "ban" or warns >= MAX_WARNINGS:
        try:
            await chat.ban_member(user_id)
        except:
            pass

        # Track multi-group ban
        if user_id not in pending_appeals:
            pending_appeals[user_id] = set()
        pending_appeals[user_id].add(chat_id)

        # DM user
        try:
            await bot.send_message(user_id, "⛔ You were banned.\nUse /appeal <reason>")
        except:
            pass

        await send_temp_message(chat, response + "\nUser Banned.", 10)
        reset_warnings(chat_id, user_id)


# ---------------------------------------------------------
#                   COMING SOON
# ---------------------------------------------------------
async def coming_soon(update, context):
    await update.message.reply_text(
        "🚧 Coming Soon:\n"
        "- Advanced analytics dashboard\n"
        "- Custom punishments\n"
        "- Auto profile levels\n"
        "- Flood control upgrade"
    )


# ---------------------------------------------------------
#                     BOT RUNNER
# ---------------------------------------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setrule", setrule))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("appeal", appeal))
    app.add_handler(CommandHandler("soon", coming_soon))

    # Verify / Welcome
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # Admin approval
    app.add_handler(CallbackQueryHandler(approve_user, pattern=r"^approve"))

    # Message moderation
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()
