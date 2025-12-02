from datetime import datetime
from db import db

# ───────────── GROUPS ─────────────

def add_group(chat_id: int, title: str, added_by: int):
    db.groups.update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "chat_id": chat_id,
                "title": title,
                "added_by": added_by,
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True
    )


# ───────────── USERS ─────────────

def add_user(user_id: int, username: str):
    db.users.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username,
                "updated_at": datetime.utcnow()
            },
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True
    )


# ───────────── RULES ─────────────

def add_rule_db(chat_id: int, rule: str):
    db.rules.insert_one({
        "chat_id": chat_id,
        "rule": rule,
        "created_at": datetime.utcnow()
    })


def get_rules_db(chat_id: int):
    return [r["rule"] for r in db.rules.find({"chat_id": chat_id})]


# ───────────── WARNINGS ─────────────

def increment_warning(chat_id: int, user_id: int):
    data = db.warnings.find_one({"chat_id": chat_id, "user_id": user_id})
    if data:
        new = data["warnings"] + 1
        db.warnings.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"warnings": new}}
        )
        return new
    else:
        db.warnings.insert_one({
            "chat_id": chat_id,
            "user_id": user_id,
            "warnings": 1
        })
        return 1


def reset_warnings(chat_id: int, user_id: int):
    db.warnings.delete_one({"chat_id": chat_id, "user_id": user_id})


def get_all_warnings(chat_id: int):
    return list(db.warnings.find({"chat_id": chat_id}))


# ───────────── APPEALS ─────────────

def log_appeal(user_id: int, chat_id: int, appeal_text: str, approved: bool):
    db.appeals.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "appeal_text": appeal_text,
        "approved": approved,
        "created_at": datetime.utcnow()
    })


# ───────────── MODERATION LOGS ─────────────

def log_action(chat_id: int, user_id: int, action: str, reason: str):
    db.moderation_logs.insert_one({
        "chat_id": chat_id,
        "user_id": user_id,
        "action": action,
        "reason": reason,
        "created_at": datetime.utcnow()
    })
