from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

appeals = {}

async def handle_appeal(bot, user_id, chat_id, reason, admin_id):
    count = appeals.get(user_id, 0)

    if count < 4:
        appeals[user_id] = count + 1
        return False  # normal appeal processed
    else:
        # send to admin for review
        btn = InlineKeyboardMarkup().add(
            InlineKeyboardButton("Approve User", callback_data=f"approve_{user_id}")
        )

        await bot.send_message(
            admin_id,
            f"⚠ Appeal limit reached\nUser: {user_id}\nReason: {reason}",
            reply_markup=btn
        )
        return True
