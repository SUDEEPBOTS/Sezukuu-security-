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
    ChatMemberHandler,
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
    LOGGER_CHAT_ID,
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

# user_id -> set(group_ids jaha se ban hua)
pending_appeals = {}

# user_id -> kitni bar appeal ki
appeal_attempt_counts = {}

# user_id -> kitni bar appeal APPROVE hui
appeal_approved_counts = {}

# (chat_id, user_id) -> verify-message-id
pending_verifications = {}

# ───────────── PERMISSIONS PRESETS ─────────────

# Full unmuted permissions (saari cheeze allow)
FULL_PERMS = ChatPermissions.all_permissions()  # PTB helper for all True [web:31]

# Fully muted permissions (kuch bhi send nahi kar sakta)
MUTE_PERMS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)


# ───────────── LOGGER HELPERS ─────────────

async def log_to_logger(text: str, bot):
    if not LOGGER_CHAT_ID:
        return
    try:
        await bot.send_message(LOGGER_CHAT_ID, text)
    except Exception:
        pass


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

async def send_temp_message(chat, text: str, seconds: int = 180):
    try:
        msg = await chat.send_message(text)
    except Exception:
        return

    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except Exception:
        pass


# ───────────── START + VERIFY ─────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    bot = context.bot

    add_user(user.id, user.username or user.first_name)

    await log_to_logger(
        f"🔹 /start used by {user.first_name} (id={user.id}) in chat {chat.id} ({chat.type})",
        bot,
    )

    # GROUP /start
    if chat.type != "private":
        add_group(chat.id, chat.title, user.id)
        return await update.message.reply_text(
            "🤖 AI Moderator Active.
Use /setrule to add rules."
        )

    # -----------------------------
    # DM: deep-link verify handler
    # -----------------------------
    if context.args and context.args[0].startswith("verify_"):
        try:
            group_id = int(context.args[0].split("_")[1])
        except Exception:
            return await update.message.reply_text("Invalid verify link.")

        # UNMUTE USER (full permissions)
        try:
            await context.bot.restrict_chat_member(
                group_id,
                user.id,
                permissions=FULL_PERMS,
            )
        except Exception as e:
            return await update.message.reply_text(
                f"⚠️ Unmute failed.
Reason: {e}
"
                f"Make sure bot has 'Restrict Members' permission in group."
            )

        # DELETE VERIFY BUTTON
        key = (group_id, user.id)
        msg_id = pending_verifications.pop(key, None)
        if msg_id:
            try:
                await bot.delete_message(group_id, msg_id)
            except Exception:
                pass

        # DM SUCCESS
        await update.message.reply_text(
            "✅ Successfully verified!
Ab aap group me freely chat kar sakte ho."
        )

        # GROUP ANNOUNCEMENT
        try:
            await bot.send_message(
                group_id,
                f"{user.first_name} is verified and unmuted! ✅"
            )
        except Exception:
            pass

        return

    # -----------------------------
    # Normal DM /start
    # -----------------------------
    await update.message.reply_text(
        "👋 This is AI moderation bot.
"
        "Agar aap banned ho gaye ho to /appeal <reason> bhejo."
    )


# ───────────── NEW MEMBER WELCOME + VERIFY ─────────────

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    bot = context.bot

    new_members = message.new_chat_members
    if not new_members:
        return

    bot_user = await bot.get_me()
    bot_username = bot_user.username

    for member in new_members:
        # agar bot khud group me add hua hai
        if member.id == bot_user.id:
            await log_to_logger(
                f"✅ Bot added to group: {chat.title} (id={chat.id})",
                bot,
            )
            continue

        if member.is_bot:
            continue

        add_user(member.id, member.username or member.first_name)

        # mute user until verify
        try:
            await bot.restrict_chat_member(
                chat.id,
                member.id,
                permissions=MUTE_PERMS,
            )
        except Exception:
            pass

        verify_link = f"https://t.me/{bot_username}?start=verify_{chat.id}"

        try:
            sent = await context.bot.send_message(
                chat.id,
                f"Welcome {member.first_name}! 👋
"
                f"Please verify to chat in this group.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Verify", url=verify_link)]]
                ),
            )
            pending_verifications[(chat.id, member.id)] = sent.message_id
        except Exception:
            pass


# ───────────── RULES COMMANDS ─────────────

async def setrule(update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("Admin only.")

    chat_id = update.effective_chat.id
    text = " ".join(context.args)

    if not text:
        return await update.message.reply_text("Usage: /setrule <rule>")

    add_rule_db(chat_id, text)

    rules = get_rules_db(chat_id)
    rr = "
".join([f"{i+1}. {r}" for i, r in enumerate(rules)])

    await update.message.reply_text(f"Rule Added!

{rr}")


async def show_rules(update, context):
    rules = get_rules_db(update.effective_chat.id)
    if not rules:
        return await update.message.reply_text("No rules set.")

    rr = "
".join([f"{i+1}. {r}" for i, r in enumerate(rules)])
    await update.message.reply_text(f"📜 RULES:

{rr}")


async def status(update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text("Admin only.")

    chat_id = update.effective_chat.id
    warns = get_all_warnings(chat_id)

    if not warns:
        return await update.message.reply_text("No warnings.")

    msg = "⚠️ WARNINGS:
"
    for w in warns:
        msg += f"User {w['user_id']} → {w['warnings']}
"

    await update.message.reply_text(msg)


# ───────────── APPEAL SYSTEM ─────────────

async def appeal(update, context):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot
    user_id = user.id

    # Only DM me appeal
    if chat.type != "private":
        return await update.message.reply_text("DM me /appeal bhejo.")

    # Appeal exist check
    if user_id not in pending_appeals or not pending_appeals[user_id]:
        return await update.message.reply_text("No active ban/mute appeal found.")

    appeal_text = " ".join(context.args)
    if not appeal_text:
        return await update.message.reply_text("Usage: /appeal <reason>")

    group_ids = list(pending_appeals[user_id])

    # Attempt Counter
    attempt_count = appeal_attempt_counts.get(user_id, 0) + 1
    appeal_attempt_counts[user_id] = attempt_count

    # Approved Counter
    approved_count = appeal_approved_counts.get(user_id, 0)

    # ------------------------------
    # ⭐ AI AUTO-HANDLING (Until 3 approvals)
    # ------------------------------
    if approved_count < 3:
        decision = evaluate_appeal(appeal_text)

        if decision["approve"]:
            # UNBAN + UNMUTE sabhi groups me
            for gid in group_ids:
                log_appeal(user_id, gid, appeal_text, True)

                # Try unban
                try:
                    await bot.unban_chat_member(gid, user_id)
                except Exception:
                    pass

                # Try unmute (full unrestricted)
                try:
                    await bot.restrict_chat_member(
                        gid,
                        user_id,
                        permissions=FULL_PERMS,
                    )
                except Exception:
                    pass

            # Approved count +1
            appeal_approved_counts[user_id] = approved_count + 1

            await update.message.reply_text(
                "✅ Appeal Approved!
"
                "Aap sabhi groups me unbanned/unmuted ho gaye ho."
            )

            # Background group notifications
            for gid in group_ids:
                try:
                    gc = await bot.get_chat(gid)
                    asyncio.create_task(
                        send_temp_message(
                            gc,
                            f"🔓 Appeal approved for {user.first_name}",
                            180,
                        )
                    )
                except Exception:
                    pass

            # Clear appeal record
            pending_appeals.pop(user_id, None)
            appeal_attempt_counts.pop(user_id, None)

            return

        else:
            # AI Rejection
            for gid in group_ids:
                log_appeal(user_id, gid, appeal_text, False)

            return await update.message.reply_text(
                "❌ Appeal Rejected.
Reason: " + decision["reason"]
            )

    # ------------------------------
    # ⭐ ADMIN REVIEW (3+ AI approvals)
    # ------------------------------
    primary_gid = group_ids[0]

    try:
        primary_chat = await bot.get_chat(primary_gid)
        primary_name = primary_chat.title or str(primary_gid)
        join_button = []

        if primary_chat.username:
            join_button = [
                InlineKeyboardButton(
                    "➡ Join Group",
                    url=f"https://t.me/{primary_chat.username}",
                )
            ]
    except Exception:
        primary_name = str(primary_gid)
        join_button = []

    admin_target = OWNER_ID or primary_gid

    keyboard_buttons = [
        InlineKeyboardButton(
            "✅ Approve User",
            callback_data=f"approve:{user_id}",
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject:{user_id}",
        ),
    ]

    if join_button:
        keyboard_buttons.append(join_button[0])

    reply_markup = InlineKeyboardMarkup([keyboard_buttons])

    try:
        await bot.send_message(
            admin_target,
            (
                f"⚠️ Max auto-appeal limit reached.
"
                f"User: {user.first_name} (ID: {user_id})
"
                f"Primary group: {primary_name} (id={primary_gid})
"
                f"Total AI Approved Appeals: {approved_count}

"
                f"Last Appeal Message:
{appeal_text}"
            ),
            reply_markup=reply_markup,
        )
    except Exception:
        pass

    await update.message.reply_text(
        "Aapka appeal admin ke paas manual review ke liye bheja gaya hai. ⏳"
    )


# ───────────── INLINE BUTTON: ADMIN APPROVE ─────────────

async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot = context.bot

    await query.answer()

    try:
        _, user_id_str = query.data.split(":")
        user_id = int(user_id_str)
    except Exception:
        await query.edit_message_text("Invalid approval data.")
        return

    group_ids = list(pending_appeals.get(user_id, []))

    # Unban + cleanup
    for gid in group_ids:
        try:
            await bot.unban_chat_member(gid, user_id)
        except Exception:
            pass
        try:
            await bot.restrict_chat_member(
                gid,
                user_id,
                permissions=FULL_PERMS,
            )
        except Exception:
            pass

    pending_appeals.pop(user_id, None)
    appeal_attempt_counts.pop(user_id, None)
    appeal_approved_counts.pop(user_id, None)

    # User ko DM
    try:
        await bot.send_message(
            user_id,
            "✅ Your appeal was approved by admin. You can now rejoin the group(s).",
        )
    except Exception:
        pass

    try:
        await query.edit_message_text("User unbanned/unmuted from all tracked groups. ✅")
    except Exception:
        pass


# ───────────── INLINE BUTTON: ADMIN REJECT ─────────────

async def reject_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    try:
        _, user_id_str = query.data.split(":")
        user_id = int(user_id_str)
    except Exception:
        await query.edit_message_text("Invalid rejection data.")
        return

    # Just clear tracking; user banned/muted hi rahega
    pending_appeals.pop(user_id, None)
    appeal_attempt_counts.pop(user_id, None)
    # approved count ko bhi reset kar sakte hain
    appeal_approved_counts.pop(user_id, None)

    # User ko DM
    try:
        await context.bot.send_message(
            user_id,
            "❌ Your appeal was rejected by admin.",
        )
    except Exception:
        pass

    try:
        await query.edit_message_text("Appeal rejected. User stays banned/muted.")
    except Exception:
        pass


# ───────────── MODERATION ─────────────

async def handle_message(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat.type == "private":
        return

    if user.is_bot:
        return

    # ───────────── ADMIN BYPASS ─────────────
    if await is_admin(update, context):
        return

    text = message.text or message.caption
    if not text:
        return

    chat_id = chat.id
    user_id = user.id

    rules = get_rules_db(chat_id)
    rules_text = "
".join(rules)

    result = moderate_message(text, user, chat, rules_text)

    action = result["action"]
    reason = result["reason"]
    severity = result["severity"]
    should_delete = result["should_delete"]

    # Delete user message if needed
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
        f"🚨 Rule Violation
"
        f"User: {user.first_name}
"
        f"Reason: {reason}
"
        f"Warnings: {warns}/{MAX_WARNINGS}"
    )

    # WARN
    if action == "warn":
        asyncio.create_task(send_temp_message(chat, response, seconds=180))
        return

    # MUTE
    if action == "mute":
        until = datetime.utcnow() + timedelta(minutes=MUTE_DURATION_MIN)
        try:
            await chat.restrict_member(
                user_id,
                MUTE_PERMS,
                until_date=until,
            )
        except Exception:
            pass

        asyncio.create_task(
            send_temp_message(
                chat,
                response + f"
Muted {MUTE_DURATION_MIN} min.",
                seconds=180,
            )
        )
        return

    # BAN
    if action == "ban" or warns >= MAX_WARNINGS:
        try:
            await chat.ban_member(user_id)
        except Exception:
            pass

        # multi-group track
        if user_id not in pending_appeals:
            pending_appeals[user_id] = set()
        pending_appeals[user_id].add(chat_id)

        # User ko DM
        try:
            await bot.send_message(
                user_id,
                f"⛔ You were banned from '{chat.title}'.
"
                f"Reason: {reason}

"
                f"Agar aapko lagta hai galti se hua, to /appeal <reason> bhejo.",
            )
        except Exception:
            pass

        asyncio.create_task(
            send_temp_message(
                chat,
                response + "
User Banned.",
                seconds=180,
            )
        )

        reset_warnings(chat_id, user_id)
        return


# ───────────── COMING SOON ─────────────

async def coming_soon(update, context):
    await update.message.reply_text(
        "🚧 Coming Soon:
"
        "- Advanced analytics dashboard
"
        "- Custom punishments per rule
"
        "- Flood / spam shield
"
        "- Auto backup & restore
"
    )


# ───────────── GOODBYE ON LEAVE ─────────────

async def goodbye_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    LEFT_CHAT_MEMBER: jab koi user group chhodta hai, simple goodbye.
    """
    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not msg.left_chat_member:
        return

    user = msg.left_chat_member

    try:
        await context.bot.send_message(
            chat.id,
            f"Goodbye {user.first_name} 👋",
        )
    except Exception:
        pass


# ───────────── ERROR HANDLER (LOGGER) ─────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    try:
        await log_to_logger(
            f"⚠️ Error occurred:
{context.error}

Update:
{update}",
            context.bot,
        )
    except Exception:
        pass


# ───────────── BOT RUN ─────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setrule", setrule))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("appeal", appeal))
    app.add_handler(CommandHandler("soon", coming_soon))

    # new members welcome + verify
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome_new_member,
        )
    )

    # user left goodbye
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.LEFT_CHAT_MEMBER,
            goodbye_message,
        )
    )

    # Inline approve/reject button handlers
    app.add_handler(CallbackQueryHandler(approve_user, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(reject_user, pattern=r"^reject:"))

    # Message moderation
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Error handler -> logger GC
    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
