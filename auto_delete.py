import asyncio

async def auto_delete(bot, chat_id, text):
    msg = await bot.send_message(chat_id, text)
    await asyncio.sleep(9)
    try:
        await msg.delete()
    except:
        pass
