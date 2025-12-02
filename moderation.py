import json
import google.generativeai as genai
from config import GEMINI_API_KEY

# Gemini Init
genai.configure(api_key=GEMINI_API_KEY)
moderation_model = genai.GenerativeModel("gemini-2.5-flash")
appeal_model = genai.GenerativeModel("gemini-2.5-flash")


MODERATION_SYS = """
You are an AI moderator for a Telegram group chat.

Follow:
1. Universal safety rules
2. Custom group rules provided

Actions:
- allow
- warn
- mute
- ban
- delete

Return ONLY a JSON:
{
 "action": "...",
 "reason": "...",
 "category": "...",
 "severity": 1-5,
 "should_delete": true/false
}
"""


def safe_json(text, default):
    try:
        j = json.loads(text)
        return j if isinstance(j, dict) else default
    except:
        return default


def moderate_message(text, user, chat, rules_text: str):
    username = f"@{user.username}" if user.username else user.first_name
    chat_title = chat.title or str(chat.id)

    prompt = f"""
{MODERATION_SYS}

GROUP RULES:
{rules_text}

CHAT:
{chat_title}

USER:
{username} (ID: {user.id})

MESSAGE:
{text}
"""

    default = {
        "action": "allow",
        "reason": "AI error",
        "category": "other",
        "severity": 1,
        "should_delete": False
    }

    try:
        res = moderation_model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        data = safe_json(res.text.strip(), default)
        return data
    except:
        return default


# ───────────── APPEAL ─────────────

APPEAL_SYS = """
You review Telegram ban appeals.

Approve if:
- user is genuinely sorry
- promises to follow rules

Reject if:
- still abusive
- fake apology
- trolling

Return only JSON:
{
 "approve": true/false,
 "reason": "..."
}
"""


def evaluate_appeal(text: str):
    prompt = f"""
{APPEAL_SYS}

USER APPEAL:
{text}
"""

    default = {"approve": False, "reason": "AI error"}

    try:
        res = appeal_model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        return safe_json(res.text.strip(), default)
    except:
        return default
