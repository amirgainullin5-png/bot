import json
from config import WHITELIST_FILE, ADMIN_IDS

def load_whitelist() -> set[int]:
    if WHITELIST_FILE.exists():
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return {572460798}

def save_whitelist(ids: set[int]):
    WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f, ensure_ascii=False)

WHITELIST = load_whitelist()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def has_access(uid: int) -> bool:
    return uid in WHITELIST or uid in ADMIN_IDS