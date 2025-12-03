import logging
import asyncio
import random
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
from telegram.constants import ParseMode  # ✅ ADD THIS

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

# Global dictionaries
pending_appeals = {}
appeal_attempt_counts = {}
appeal_approved_counts = {}
pending_verifications = {}


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

    if OWNER_ID and user.id == OWNER_ID:
        return True

    member = await chat.get_member(user.id)
    return member.status in ["administrator", "creator"]


# ───────────── HELPER: TEMP MESSAGE WITH STYLING ─────────────

async def send_temp_message(chat, text: str, seconds: int = 180, style: str = "normal"):
    """
    Telegram ke built-in formatting ke saath messages
    """
    # Different styles ke liye formatting
    if style == "warning":
        formatted = f"⚠️ <b>WARNING</b> ⚠️\n\n<blockquote>{text}</blockquote>"
    elif style == "info":
        formatted = f"ℹ️ <b>INFORMATION</b>\n\n<i>{text}</i>"
    elif style == "success":
        formatted = f"✅ <b>SUCCESS</b>\n\n{text}"
    elif style == "error":
        formatted = f"❌ <b>ERROR</b>\n\n<code>{text}</code>"
    elif style == "welcome":
        formatted = f"👋 <b>WELCOME</b>\n\n<blockquote>{text}</blockquote>"
    elif style == "goodbye":
        formatted = f"👋 <b>GOODBYE</b>\n\n<i>{text}</i>"
    elif style == "rules":
        formatted = f"📜 <b>RULES</b>\n\n<pre>{text}</pre>"
    else:
        formatted = text
    
    try:
        msg = await chat.send_message(
            formatted,
            parse_mode=ParseMode.HTML  # ✅ HTML PARSING
        )
    except Exception:
        # Agar HTML parse error ho to plain text
        msg = await chat.send_message(text)

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
            "🤖 <b>AI Moderator Active</b>\n\nUse /setrule to add rules.",
            parse_mode=ParseMode.HTML  # ✅ HTML
        )

    # DM: deep-link verify handler
    if context.args and context.args[0].startswith("verify_"):
        try:
            group_id = int(context.args[0].split("_")[1])
        except:
            return await update.message.reply_text(
                "<code>Invalid verify link.</code>",
                parse_mode=ParseMode.HTML
            )

        # UNMUTE USER
        try:
            await context.bot.restrict_chat_member(
                group_id,
                user.id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                ),
            )
        except Exception as e:
            return await update.message.reply_text(
                f"⚠️ <b>Unmute failed</b>\n\n<code>Reason: {e}</code>\n\nMake sure bot has 'Restrict Members' permission.",
                parse_mode=ParseMode.HTML
            )

        # DELETE VERIFY BUTTON
        key = (group_id, user.id)
        msg_id = pending_verifications.pop(key, None)
        if msg_id:
            try:
                await bot.delete_message(group_id, msg_id)
            except:
                pass

        # DM SUCCESS
        await update.message.reply_text(
            "✅ <b>Successfully verified!</b>\n\nAb aap group me freely chat kar sakte ho.",
            parse_mode=ParseMode.HTML
        )

        # GROUP ANNOUNCEMENT (Styled)
        try:
            await bot.send_message(
                group_id,
                f"✨ <b>{user.first_name} ɪꜱ ᴠᴇʀɪꜰɪᴇᴅ ᴀɴᴅ ᴜɴᴍᴜᴛᴇᴅ! 🍷</b>",
                parse_mode=ParseMode.HTML
            )
        except:
            pass

        return

    # Normal DM /start
    await update.message.reply_text(
        "👋 <b>Hello I am AI Admin</b>\n\n"
        "<i>Futures coming soon...</i>",
        parse_mode=ParseMode.HTML
    )


# ───────────── NEW MEMBER WELCOME + VERIFY (Styled) ─────────────

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
                permissions=ChatPermissions(can_send_messages=False),
            )
        except Exception:
            pass

        # verify link
        verify_link = f"https://t.me/{bot_username}?start=verify_{chat.id}"

        # STYLED WELCOME MESSAGE
        welcome_html = f"""
👋 <b>WELCOME {member.first_name.upper()}!</b> 👋

<blockquote>Please verify yourself to start chatting in this group.</blockquote>

👉 <a href="{verify_link}">CLICK TO VERIFY</a> 👈
        """
        
        try:
            sent = await context.bot.send_message(
                chat.id,
                welcome_html,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ VERIFY NOW", url=verify_link)]]
                ),
            )
            pending_verifications[(chat.id, member.id)] = sent.message_id
        except Exception:
            pass


# ───────────── RULES COMMANDS (Styled) ─────────────

async def setrule(update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text(
            "<code>Admin only.</code>",
            parse_mode=ParseMode.HTML
        )

    chat_id = update.effective_chat.id
    text = " ".join(context.args)

    if not text:
        return await update.message.reply_text(
            "<code>Usage: /setrule &lt;rule&gt;</code>",
            parse_mode=ParseMode.HTML
        )

    add_rule_db(chat_id, text)

    rules = get_rules_db(chat_id)
    rr = "\n".join([f"{i+1}. {r}" for i, r in enumerate(rules)])

    response_html = f"""
✅ <b>RULE ADDED SUCCESSFULLY!</b>

<blockquote>{text}</blockquote>

📋 <b>ALL RULES:</b>
<pre>{rr}</pre>
    """
    
    await update.message.reply_text(
        response_html,
        parse_mode=ParseMode.HTML
    )


async def show_rules(update, context):
    rules = get_rules_db(update.effective_chat.id)
    if not rules:
        return await update.message.reply_text(
            "<i>No rules set for this group.</i>",
            parse_mode=ParseMode.HTML
        )

    rr = "\n".join([f"{i+1}. {r}" for i, r in enumerate(rules)])
    
    rules_html = f"""
📜 <b>GROUP RULES</b> 📜

<pre>{rr}</pre>

<i>Please follow these rules to avoid moderation actions.</i>
    """
    
    await update.message.reply_text(
        rules_html,
        parse_mode=ParseMode.HTML
    )


async def status(update, context):
    if not await is_admin(update, context):
        return await update.message.reply_text(
            "<code>Admin only.</code>",
            parse_mode=ParseMode.HTML
        )

    chat_id = update.effective_chat.id
    warns = get_all_warnings(chat_id)

    if not warns:
        return await update.message.reply_text(
            "<i>No warnings.</i>",
            parse_mode=ParseMode.HTML
        )

    msg = "⚠️ <b>WARNINGS:</b>\n\n"
    for w in warns:
        msg += f"<code>User {w['user_id']}</code> → <b>{w['warnings']} warnings</b>\n"

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML
    )


# ───────────── APPEAL SYSTEM (Styled) ─────────────

async def appeal(update, context):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot
    user_id = user.id

    # Only DM me appeal
    if chat.type != "private":
        return await update.message.reply_text(
            "<code>DM me /appeal bhejo.</code>",
            parse_mode=ParseMode.HTML
        )

    # Appeal exist check
    if user_id not in pending_appeals or not pending_appeals[user_id]:
        return await update.message.reply_text(
            "<i>No active ban/mute appeal found.</i>",
            parse_mode=ParseMode.HTML
        )

    appeal_text = " ".join(context.args)
    if not appeal_text:
        return await update.message.reply_text(
            "<code>Usage: /appeal &lt;reason&gt;</code>",
            parse_mode=ParseMode.HTML
        )

    group_ids = list(pending_appeals[user_id])

    # Attempt Counter
    attempt_count = appeal_attempt_counts.get(user_id, 0) + 1
    appeal_attempt_counts[user_id] = attempt_count

    # Approved Counter
    approved_count = appeal_approved_counts.get(user_id, 0)

    # AI AUTO-HANDLING
    if approved_count < 3:
        decision = evaluate_appeal(appeal_text)

        if decision["approve"]:
            # UNBAN + UNMUTE sabhi groups me
            for gid in group_ids:
                log_appeal(user_id, gid, appeal_text, True)

                # Try unban
                try:
                    await bot.unban_chat_member(gid, user_id)
                except:
                    pass

                # Try unmute
                try:
                    await bot.restrict_chat_member(
                        gid,
                        user_id,
                        permissions=ChatPermissions()
                    )
                except:
                    pass

            # Approved count +1
            appeal_approved_counts[user_id] = approved_count + 1

            await update.message.reply_text(
                "✅ <b>Appeal Approved!</b>\n\n"
                "Aap sabhi groups me unbanned/unmuted ho gaye ho.",
                parse_mode=ParseMode.HTML
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
                            style="success"
                        )
                    )
                except:
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
                f"❌ <b>Appeal Rejected</b>\n\n"
                f"<blockquote>Reason: {decision['reason']}</blockquote>",
                parse_mode=ParseMode.HTML
            )

    # ADMIN REVIEW
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
    except:
        primary_name = str(primary_gid)
        join_button = []

    admin_target = OWNER_ID or primary_gid

    # STYLED ADMIN MESSAGE
    admin_html = f"""
⚠️ <b>MAX AUTO-APPEAL LIMIT REACHED</b> ⚠️

<b>User:</b> <code>{user.first_name}</code> (ID: {user_id})
<b>Primary group:</b> {primary_name} (id={primary_gid})
<b>Total AI Approved Appeals:</b> {approved_count}

📝 <b>Last Appeal Message:</b>
<blockquote>{appeal_text}</blockquote>
    """
    
    # Inline buttons
    keyboard_buttons = [
        InlineKeyboardButton(
            "✅ Approve User",
            callback_data=f"approve:{user_id}",
        )
    ]

    if join_button:
        keyboard_buttons.append(join_button[0])

    reply_markup = InlineKeyboardMarkup([keyboard_buttons])

    # Send to admin
    try:
        await bot.send_message(
            admin_target,
            admin_html,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except:
        pass

    await update.message.reply_text(
        "<i>Aapka appeal admin ke paas manual review ke liye bheja gaya hai.</i> ⏳",
        parse_mode=ParseMode.HTML
    )


# ───────────── INLINE BUTTON: ADMIN APPROVE (Styled) ─────────────

async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot = context.bot

    await query.answer()

    try:
        _, user_id_str = query.data.split(":")
        user_id = int(user_id_str)
    except Exception:
        await query.edit_message_text(
            "<code>Invalid approval data.</code>",
            parse_mode=ParseMode.HTML
        )
        return

    group_ids = list(pending_appeals.get(user_id, []))

    # Unban + cleanup
    for gid in group_ids:
        try:
            await bot.unban_chat_member(gid, user_id)
        except Exception:
            pass

    pending_appeals.pop(user_id, None)
    appeal_attempt_counts.pop(user_id, None)
    appeal_approved_counts.pop(user_id, None)

    # User ko DM
    try:
        await bot.send_message(
            user_id,
            "✅ <b>Your appeal was approved by admin.</b>\n\nYou can now rejoin the group(s).",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    try:
        await query.edit_message_text(
            "✅ <b>User unbanned from all tracked groups.</b>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# ───────────── MODERATION (Styled) ─────────────

async def handle_message(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat.type == "private":
        return

    if user.is_bot:
        return

    # ADMIN BYPASS
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

    action = result["action"]
    reason = result["reason"]
    severity = result["severity"]
    should_delete = result["should_delete"]

    # User ka message delete
    if should_delete or action in ["warn", "mute", "ban", "delete"]:
        try:
            await message.delete()
        except Exception:
            pass

    if action == "allow":
        return

    warns = increment_warning(chat_id, user_id)

    log_action(chat_id, user_id, action, reason)

    # STYLED RESPONSES
    response = (
        f"<b>User:</b> {user.first_name}\n"
        f"<b>Reason:</b> <code>{reason}</code>\n"
        f"<b>Warnings:</b> {warns}/{MAX_WARNINGS}"
    )

    # WARN
    if action == "warn":
        warning_html = f"""
🚨 <b>RULE VIOLATION DETECTED</b> 🚨

{response}

<blockquote>⚠️ Please follow group rules!</blockquote>
        """
        asyncio.create_task(
            send_temp_message(chat, warning_html, seconds=180, style="warning")
        )
        return
        
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

        mute_html = f"""
🔇 <b>USER MUTED</b> 🔇

{response}

<b>Duration:</b> {MUTE_DURATION_MIN} minutes
        """
        asyncio.create_task(
            send_temp_message(chat, mute_html, seconds=180, style="error")
        )
        
        # Send DM to user
        try:
            await bot.send_message(
                user_id,
                f"🔇 <b>You were muted in '{chat.title}'</b>\n\n"
                f"<b>Duration:</b> {MUTE_DURATION_MIN} minutes\n"
                f"<b>Reason:</b> <code>{reason}</code>\n\n"
                f"<i>Agar aapko lagta hai galti se hua, to /appeal &lt;reason&gt; bhejo.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
            
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

        ban_html = f"""
⛔ <b>USER BANNED</b> ⛔

{response}

<b>Duration:</b> {MUTE_DURATION_MIN} minutes
        """
        asyncio.create_task(
            send_temp_message(chat, mute_html, seconds=180, style="error")
        )
        
        # Send DM to user
        try:
            await bot.send_message(
                user_id,
                f"🔇 <b>You were muted in '{chat.title}'</b>\n\n"
                f"<b>Duration:</b> {MUTE_DURATION_MIN} minutes\n"
                f"<b>Reason:</b> <code>{reason}</code>\n\n"
                f"<i>Agar aapko lagta hai galti se hua, to /appeal &lt;reason&gt; bhejo.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
            
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

        ban_html = f"""
⛔ <b>USER BANNED</b> ⛔

{response}

<blockquote>User has been banned permanently.</blockquote>
        """
        asyncio.create_task(
            send_temp_message(chat, ban_html, seconds=180, style="error")
        )

        # User ko DM
        try:
            await bot.send_message(
                user_id,
                f"⛔ <b>You were banned from '{chat.title}'</b>\n\n"
                f"<b>Reason:</b> <code>{reason}</code>\n\n"
                f"<i>Agar aapko lagta hai galti se hua, to /appeal &lt;reason&gt; bhejo.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        reset_warnings(chat_id, user_id)
        return


# ───────────── GOODBYE MESSAGE (New) ─────────────

async def goodbye_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    bot = context.bot

    left_member = message.left_chat_member
    if not left_member:
        return

    # Ignore if bot left
    bot_user = await bot.get_me()
    if left_member.id == bot_user.id:
        await log_to_logger(
            f"❌ Bot removed from group: {chat.title} (id={chat.id})",
            bot,
        )
        return

    if left_member.is_bot:
        return

    # Goodbye messages
    goodbye_messages = [
        f"👋 <b>GOODBYE {left_member.first_name}!</b>\n\n<i>We'll miss you in this group!</i>",
        f"🚪 <b>{left_member.first_name} has left</b>\n\n<i>Farewell, friend!</i>",
        f"😢 <b>{left_member.first_name} exited</b>\n\n<i>Hope to see you again soon!</i>",
    ]
    
    goodbye_msg = random.choice(goodbye_messages)
    
    # Send goodbye message (temporary)
    asyncio.create_task(
        send_temp_message(chat, goodbye_msg, seconds=120, style="goodbye")
    )


# ───────────── COMING SOON ─────────────

async def coming_soon(update, context):
    await update.message.reply_text(
        "🚧 <b>Coming Soon:</b>\n\n"
        "<blockquote>"
        "- Advanced analytics dashboard\n"
        "- Custom punishments per rule\n"
        "- Flood / spam shield\n"
        "- Auto backup & restore"
        "</blockquote>",
        parse_mode=ParseMode.HTML
    )


# ───────────── ERROR HANDLER (LOGGER) ─────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    try:
        await log_to_logger(
            f"⚠️ Error occurred:\n{context.error}\n\nUpdate:\n{update}",
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
    
    # goodbye members
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.LEFT_CHAT_MEMBER,
            goodbye_member,
        )
    )

    # Inline approve button handler
    app.add_handler(CallbackQueryHandler(approve_user, pattern=r"^approve:"))

    # Message moderation
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Error handler -> logger GC
    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
