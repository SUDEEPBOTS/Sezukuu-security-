# main.py  -- FastAPI + Webhook version WITH APPROVE SYSTEM INTEGRATED
import os
import asyncio
import logging
import random
from datetime import timedelta, datetime

from fastapi import FastAPI, Request, Response
from dotenv import load_dotenv

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

# ---------- LOAD ENV ----------
load_dotenv()

# ---------- IMPORT CONFIG ----------
from config import (
    BOT_TOKEN,
    OWNER_ID,
    MAX_WARNINGS,
    MUTE_DURATION_MIN,
    ENABLE_AUTO_BAN,
    ENABLE_AUTO_DELETE,
    ENABLE_AUTO_MUTE,
    LOGGER_CHAT_ID,
    validate_config,
)

# ---------- MODELS & DB ----------
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
    ensure_indexes,
)
from db import ensure_connection, close as close_db

# ---------- MODERATION (blocking helpers used in executor) ----------
from moderation import (
    moderate_message_sync as moderate_message,
    evaluate_appeal_sync as evaluate_appeal,
)

# ---------- ADMIN BYPASS ----------
from admin_bypass import is_admin_cached as is_admin

# ---------- APPROVALS ----------
# approvals.py must provide: approve_cmd, unapprove_cmd, unapprove_all_cmd, should_moderate
try:
    from approvals import approve_cmd, unapprove_cmd, unapprove_all_cmd, should_moderate
except Exception:
    # safe fallbacks (so main.py won't crash if approvals.py missing)
    def approve_cmd(update, context):  # pragma: no cover
        return
    def unapprove_cmd(update, context):  # pragma: no cover
        return
    def unapprove_all_cmd(update, context):  # pragma: no cover
        return
    def should_moderate(chat_id: int, user_id: int) -> bool:  # pragma: no cover
        return True

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- GLOBAL STATE ----------
pending_appeals = {}
appeal_attempt_counts = {}
appeal_approved_counts = {}
pending_verifications = {}

# ---------- FASTAPI + TELEGRAM APP ----------
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
if not WEBHOOK_HOST:
    raise RuntimeError("WEBHOOK_HOST missing")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

application = Application.builder().token(BOT_TOKEN).build()
app = FastAPI()


# ---------- HELPERS ----------
async def log_to_logger(text: str, bot):
    if LOGGER_CHAT_ID:
        try:
            await bot.send_message(LOGGER_CHAT_ID, text)
        except Exception:
            pass


async def send_temp_message(chat, text: str, seconds: int = 180, style: str = "normal"):
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
        msg = await chat.send_message(formatted, parse_mode=ParseMode.HTML)
    except Exception:
        msg = await chat.send_message(text)

    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except Exception:
        pass


async def _is_admin_from_update(update, context):
    try:
        chat = update.effective_chat
        user = update.effective_user
        return await is_admin(context.bot, chat.id, user.id)
    except Exception:
        return False


# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    bot = context.bot

    add_user(user.id, user.username or user.first_name)

    await log_to_logger(f"🔹 /start used by {user.first_name} (id={user.id}) in chat {chat.id} ({chat.type})", bot)

    if chat.type != "private":
        add_group(chat.id, chat.title, user.id)
        return await update.message.reply_text(
            "🤖 <b>AI Moderator Active</b>\n\nUse /setrule to add rules.",
            parse_mode=ParseMode.HTML,
        )

    if context.args and context.args[0].startswith("verify_"):
        try:
            group_id = int(context.args[0].split("_")[1])
        except Exception:
            return await update.message.reply_text("<code>Invalid verify link.</code>", parse_mode=ParseMode.HTML)

        try:
            await context.bot.restrict_chat_member(
                group_id,
                user.id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
        except Exception as e:
            return await update.message.reply_text(
                f"⚠️ <b>Unmute failed</b>\n\n<code>Reason: {e}</code>\n\nMake sure bot has 'Restrict Members' permission.",
                parse_mode=ParseMode.HTML,
            )

        key = (group_id, user.id)
        msg_id = pending_verifications.pop(key, None)
        if msg_id:
            try:
                await bot.delete_message(group_id, msg_id)
            except Exception:
                pass

        await update.message.reply_text("✅ <b>Successfully verified!</b>\n\nAb aap group me freely chat kar sakte ho.", parse_mode=ParseMode.HTML)

        try:
            await bot.send_message(group_id, f"✨ <b>{user.first_name} ɪꜱ ᴠᴇʀɪꜰɪᴇᴅ ᴀɴᴅ ᴜɴᴍᴜᴛᴇᴅ! 🍷</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

        return

    await update.message.reply_text("👋 <b>Hello I am AI Admin</b>\n\n<i>Futures coming soon...</i>", parse_mode=ParseMode.HTML)


# ---------- WELCOME NEW MEMBER ----------
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
            await log_to_logger(f"✅ Bot added to group: {chat.title} (id={chat.id})", bot)
            continue

        if member.is_bot:
            continue

        add_user(member.id, member.username or member.first_name)

        try:
            await bot.restrict_chat_member(chat.id, member.id, permissions=ChatPermissions(can_send_messages=False))
        except Exception:
            pass

        verify_link = f"https://t.me/{bot_username}?start=verify_{chat.id}"

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
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ VERIFY NOW", url=verify_link)]]),
            )
            pending_verifications[(chat.id, member.id)] = sent.message_id
        except Exception:
            pass


# ---------- RULES COMMANDS ----------
async def setrule(update, context):
    if not await _is_admin_from_update(update, context):
        return await update.message.reply_text("<code>Admin only.</code>", parse_mode=ParseMode.HTML)

    chat_id = update.effective_chat.id
    text = " ".join(context.args)

    if not text:
        return await update.message.reply_text("<code>Usage: /setrule &lt;rule&gt;</code>", parse_mode=ParseMode.HTML)

    add_rule_db(chat_id, text)

    rules = get_rules_db(chat_id)
    rr = "\n".join([f"{i+1}. {r}" for i, r in enumerate(rules)])

    response_html = f"""
✅ <b>RULE ADDED SUCCESSFULLY!</b>

<blockquote>{text}</blockquote>

📋 <b>ALL RULES:</b>
<pre>{rr}</pre>
    """

    await update.message.reply_text(response_html, parse_mode=ParseMode.HTML)


async def show_rules(update, context):
    rules = get_rules_db(update.effective_chat.id)
    if not rules:
        return await update.message.reply_text("<i>No rules set for this group.</i>", parse_mode=ParseMode.HTML)

    rr = "\n".join([f"{i+1}. {r}" for i, r in enumerate(rules)])

    rules_html = f"""
📜 <b>GROUP RULES</b> 📜

<pre>{rr}</pre>

<i>Please follow these rules to avoid moderation actions.</i>
    """

    await update.message.reply_text(rules_html, parse_mode=ParseMode.HTML)


async def status(update, context):
    if not await _is_admin_from_update(update, context):
        return await update.message.reply_text("<code>Admin only.</code>", parse_mode=ParseMode.HTML)

    chat_id = update.effective_chat.id
    warns = get_all_warnings(chat_id)

    if not warns:
        return await update.message.reply_text("<i>No warnings.</i>", parse_mode=ParseMode.HTML)

    msg = "⚠️ <b>WARNINGS:</b>\n\n"
    for w in warns:
        msg += f"<code>User {w['user_id']}</code> → <b>{w['warnings']} warnings</b>\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ---------- APPEAL SYSTEM ----------
async def appeal(update, context):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot
    user_id = user.id

    if chat.type != "private":
        return await update.message.reply_text("<code>DM me /appeal bhejo.</code>", parse_mode=ParseMode.HTML)

    if user_id not in pending_appeals or not pending_appeals[user_id]:
        return await update.message.reply_text("<i>No active ban/mute appeal found.</i>", parse_mode=ParseMode.HTML)

    appeal_text = " ".join(context.args)
    if not appeal_text:
        return await update.message.reply_text("<code>Usage: /appeal &lt;reason&gt;</code>", parse_mode=ParseMode.HTML)

    group_ids = list(pending_appeals[user_id])

    attempt_count = appeal_attempt_counts.get(user_id, 0) + 1
    appeal_attempt_counts[user_id] = attempt_count

    approved_count = appeal_approved_counts.get(user_id, 0)

    # AI AUTO-HANDLING -- run evaluate_appeal in executor (blocking)
    decision = {}
    try:
        loop = asyncio.get_running_loop()
        decision = await loop.run_in_executor(None, evaluate_appeal, appeal_text)
    except Exception as e:
        print("evaluate_appeal failed:", e)
        decision = {"approve": False, "reason": "AI error"}

    if approved_count < 3 and decision.get("approve"):
        for gid in group_ids:
            log_appeal(user_id, gid, appeal_text, True)
            try:
                await context.bot.unban_chat_member(gid, user_id)
            except Exception:
                pass
            try:
                await context.bot.restrict_chat_member(gid, user_id, permissions=ChatPermissions())
            except Exception:
                pass

        appeal_approved_counts[user_id] = approved_count + 1

        await update.message.reply_text(
            "✅ <b>Appeal Approved!</b>\n\n" "Aap sabhi groups me unbanned/unmuted ho gaye ho.",
            parse_mode=ParseMode.HTML,
        )

        for gid in group_ids:
            try:
                gc = await context.bot.get_chat(gid)
                asyncio.create_task(send_temp_message(gc, f"🔓 Appeal approved for {user.first_name}", 180, style="success"))
            except Exception:
                pass

        pending_appeals.pop(user_id, None)
        appeal_attempt_counts.pop(user_id, None)
        return

    # Admin review path...
    primary_gid = group_ids[0]
    try:
        primary_chat = await context.bot.get_chat(primary_gid)
        primary_name = primary_chat.title or str(primary_gid)
        join_button = []
        if primary_chat.username:
            join_button = [InlineKeyboardButton("➡ Join Group", url=f"https://t.me/{primary_chat.username}")]
    except Exception:
        primary_name = str(primary_gid)
        join_button = []

    admin_target = OWNER_ID or primary_gid

    admin_html = f"""
⚠️ <b>MAX AUTO-APPEAL LIMIT REACHED</b> ⚠️

<b>User:</b> <code>{user.first_name}</code> (ID: {user_id})
<b>Primary group:</b> {primary_name} (id={primary_gid})
<b>Total AI Approved Appeals:</b> {approved_count}

📝 <b>Last Appeal Message:</b>
<blockquote>{appeal_text}</blockquote>
    """

    keyboard_buttons = [InlineKeyboardButton("✅ Approve User", callback_data=f"approve:{user_id}")]
    if join_button:
        keyboard_buttons.append(join_button[0])

    reply_markup = InlineKeyboardMarkup([keyboard_buttons])

    try:
        await context.bot.send_message(admin_target, admin_html, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

    await update.message.reply_text("<i>Aapka appeal admin ke paas manual review ke liye bheja gaya hai.</i> ⏳", parse_mode=ParseMode.HTML)


# ---------- CALLBACK: ADMIN APPROVE ----------
async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot = context.bot
    await query.answer()

    try:
        _, user_id_str = query.data.split(":")
        user_id = int(user_id_str)
    except Exception:
        await query.edit_message_text("<code>Invalid approval data.</code>", parse_mode=ParseMode.HTML)
        return

    group_ids = list(pending_appeals.get(user_id, []))

    for gid in group_ids:
        try:
            await context.bot.unban_chat_member(gid, user_id)
        except Exception:
            pass

    pending_appeals.pop(user_id, None)
    appeal_attempt_counts.pop(user_id, None)
    appeal_approved_counts.pop(user_id, None)

    try:
        await context.bot.send_message(user_id, "✅ <b>Your appeal was approved by admin.</b>\n\nYou can now rejoin the group(s).", parse_mode=ParseMode.HTML)
    except Exception:
        pass

    try:
        await query.edit_message_text("✅ <b>User unbanned from all tracked groups.</b>", parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ---------- MODERATION (core) ----------
async def handle_message(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat.type == "private":
        return

    if user.is_bot:
        return

    # ---------- APPROVAL CHECK: skip approved users ----------
    try:
        chat_id = chat.id
        user_id = user.id if user else None
        if user_id is not None and not should_moderate(chat_id, user_id):
            # approved user -> ignore moderation entirely
            return
    except Exception:
        # If approvals check fails, continue to moderation to be safe
        pass

    if await _is_admin_from_update(update, context):
        return

    text = message.text or message.caption
    if not text:
        return

    chat_id = chat.id
    user_id = user.id

    rules = get_rules_db(chat_id)
    rules_text = "\n".join(rules)

    # Run blocking Gemini moderation in executor so the event loop isn't blocked
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, moderate_message, text, {"id": user_id, "username": user.username}, {"id": chat_id, "title": chat.title}, rules_text)
    except Exception as e:
        print("moderation call failed:", e)
        result = {"action": "allow", "reason": "ai error", "severity": 1, "should_delete": False}

    action = result.get("action", "allow")
    reason = result.get("reason", "Unknown")
    severity = result.get("severity", 1)
    should_delete = result.get("should_delete", False)

    # Delete message if required
    if should_delete or action in ["warn", "mute", "ban", "delete"]:
        try:
            await message.delete()
        except Exception:
            pass

    if action == "allow":
        return

    warns = increment_warning(chat_id, user_id)
    log_action(chat_id, user_id, action, reason)

    response = f"<b>User:</b> {user.first_name}\n<b>Reason:</b> <code>{reason}</code>\n<b>Warnings:</b> {warns}/{MAX_WARNINGS}"

    # WARN
    if action == "warn":
        warning_html = f"""
🚨 <b>RULE VIOLATION DETECTED</b> 🚨

{response}

<blockquote>⚠️ Please follow group rules!</blockquote>
        """
        asyncio.create_task(send_temp_message(chat, warning_html, seconds=180, style="warning"))
        return

    # MUTE
    if action == "mute":
        until = datetime.utcnow() + timedelta(minutes=MUTE_DURATION_MIN)
        try:
            await chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=until)
        except Exception:
            pass

        mute_html = f"""
🔇 <b>USER MUTED</b> 🔇

{response}

<b>Duration:</b> {MUTE_DURATION_MIN} minutes
        """
        asyncio.create_task(send_temp_message(chat, mute_html, seconds=180, style="error"))

        try:
            await bot.send_message(user.id,
                f"🔇 <b>You were muted in '{chat.title}'</b>\n\n"
                f"<b>Duration:</b> {MUTE_DURATION_MIN} minutes\n"
                f"<b>Reason:</b> <code>{reason}</code>\n\n"
                f"<i>Agar aapko lagta hai galti se hua, to /appeal &lt;reason&gt; bhejo.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    # BAN (temporary / immediate)
    if action == "ban" or warns >= MAX_WARNINGS:
        try:
            await chat.ban_member(user.id)
        except Exception:
            pass

        if user_id not in pending_appeals:
            pending_appeals[user_id] = set()
        pending_appeals[user_id].add(chat_id)

        ban_html = f"""
⛔ <b>USER BANNED</b> ⛔

{response}

<blockquote>User has been banned permanently.</blockquote>
        """
        asyncio.create_task(send_temp_message(chat, ban_html, seconds=180, style="error"))

        try:
            await bot.send_message(user.id,
                f"⛔ <b>You were banned from '{chat.title}'</b>\n\n"
                f"<b>Reason:</b> <code>{reason}</code>\n\n"
                f"<i>Agar aapko lagta hai galti se hua, to /appeal &lt;reason&gt; bhejo.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        reset_warnings(chat_id, user_id)
        return


# ---------- GOODBYE ----------
async def goodbye_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    bot = context.bot

    left_member = message.left_chat_member
    if not left_member:
        return

    bot_user = await bot.get_me()
    if left_member.id == bot_user.id:
        await log_to_logger(f"❌ Bot removed from group: {chat.title} (id={chat.id})", bot)
        return

    if left_member.is_bot:
        return

    goodbye_messages = [
        f"👋 <b>GOODBYE {left_member.first_name}!</b>\n\n<i>We'll miss you in this group!</i>",
        f"🚪 <b>{left_member.first_name} has left</b>\n\n<i>Farewell, friend!</i>",
        f"😢 <b>{left_member.first_name} exited</b>\n\n<i>Hope to see you again soon!</i>",
    ]

    goodbye_msg = random.choice(goodbye_messages)
    asyncio.create_task(send_temp_message(chat, goodbye_msg, seconds=120, style="goodbye"))


# ---------- COMING SOON ----------
async def coming_soon(update, context):
    await update.message.reply_text(
        "🚧 <b>Coming Soon:</b>\n\n"
        "<blockquote>"
        "- Advanced analytics dashboard\n"
        "- Custom punishments per rule\n"
        "- Flood / spam shield\n"
        "- Auto backup & restore"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
    )


# ---------- ERROR HANDLER ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    try:
        await log_to_logger(f"⚠️ Error occurred:\n{context.error}\n\nUpdate:\n{update}", context.bot)
    except Exception:
        pass


# ---------- Register handlers on the PTB Application ----------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setrule", setrule))
    app.add_handler(CommandHandler("rules", show_rules))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("appeal", appeal))
    app.add_handler(CommandHandler("soon", coming_soon))

    # Approval commands
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("unapprove", unapprove_cmd))
    app.add_handler(CommandHandler("unapprove_all", unapprove_all_cmd))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye_member))

    app.add_handler(CallbackQueryHandler(approve_user, pattern=r"^approve:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)


# ---------- Webhook receiver (FastAPI) ----------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    try:
        update = Update.de_json(data, application.bot)
    except Exception:
        return Response(status_code=400)
    await application.update_queue.put(update)
    return Response(status_code=200)


@app.get("/")
async def root():
    return {"status": "ok"}


# ---------- Startup / Shutdown hooks ----------
@app.on_event("startup")
async def startup():
    validate_config(raise_on_missing=True)
    try:
        ensure_connection()
    except Exception as e:
        logger.error("DB connection failed during startup: %s", e)
        raise

    try:
        ensure_indexes()
    except Exception as e:
        logger.warning("ensure_indexes failed: %s", e)

    await application.initialize()
    register_handlers(application)

    try:
        await application.bot.set_webhook(WEBHOOK_URL)
        logger.info("Webhook set to %s", WEBHOOK_URL)
    except Exception as e:
        logger.error("Failed to set webhook: %s", e)

    async def _process_queue():
        await asyncio.sleep(0.25)
        q = getattr(application, "update_queue", None)
        if q is None:
            logger.error("No update_queue on application.")
            return
        while True:
            update = await q.get()
            try:
                await application.process_update(update)
            except Exception as ex:
                logger.exception("Error processing update: %s", ex)

    asyncio.create_task(_process_queue())


@app.on_event("shutdown")
async def shutdown():
    try:
        await application.bot.delete_webhook()
    except Exception:
        pass
    try:
        await application.shutdown()
    except Exception:
        pass
    try:
        close_db()
    except Exception:
        pass


# ---------- For local debugging (not used in production webhook) ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
