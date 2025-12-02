import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "telegram_ai_mod")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

MAX_WARNINGS = 3
MUTE_DURATION_MIN = 10

ENABLE_AUTO_DELETE = True
ENABLE_AUTO_MUTE = True
ENABLE_AUTO_BAN = True
# Logger group ka chat id (int me)
LOGGER_CHAT_ID = -1003289105130 # yaha apna logger group ka chat id daalo, jaise -1001234567890
