"""
Система доступа: подписки, премиум, лимиты, пауза, кулдауны, согласия.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    WHITELIST_FILE,
    ACCESS_STATE_FILE,
    CONSENTS_FILE,
    ADMIN_IDS,
    DEFAULT_LIMIT_STANDARD,
    DEFAULT_LIMIT_FAST,
    PREMIUM_ADD_CD_SEC,
)

logger = logging.getLogger("whitelist")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _window_start(now: datetime | None = None) -> datetime:
    now = now or _now()
    hour = (now.hour // 3) * 3
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def _empty_user() -> dict[str, Any]:
    return {
        "expires_at": None,
        "premium": False,
        "limits": {},
        "used": {"standard": 0, "fast": 0, "window": None},
    }


def load_data() -> dict[str, Any]:
    data: dict[str, Any] = {
        "users": {},
        "global_limits": {
            "standard": DEFAULT_LIMIT_STANDARD,
            "fast": DEFAULT_LIMIT_FAST,
        },
        "cooldown_seconds": 0,
        "paused": False,
        "premium_add_cd": {},
        "reload_add_once": {},
        "user_cooldowns": {},
        "support_report_cd": {},
        "report_mode_cd": {},
    }

    if WHITELIST_FILE.exists():
        try:
            raw = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                far = _to_iso(_now() + timedelta(days=3650))
                for uid in raw:
                    data["users"][str(int(uid))] = {
                        **_empty_user(),
                        "expires_at": far,
                    }
            elif isinstance(raw, dict) and "users" in raw:
                data.update(raw)
        except Exception as e:
            logger.warning(f"load whitelist: {e}")

    if ACCESS_STATE_FILE.exists():
        try:
            extra = json.loads(ACCESS_STATE_FILE.read_text(encoding="utf-8"))
            for k in (
                "global_limits", "cooldown_seconds", "paused",
                "premium_add_cd", "reload_add_once",
                "user_cooldowns", "support_report_cd", "report_mode_cd",
            ):
                if k in extra:
                    data[k] = extra[k]
        except Exception as e:
            logger.warning(f"load access_state: {e}")

    return data


def save_data(data: dict[str, Any]) -> None:
    WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "users": data.get("users", {}),
        "global_limits": data.get("global_limits", {
            "standard": DEFAULT_LIMIT_STANDARD,
            "fast": DEFAULT_LIMIT_FAST,
        }),
        "cooldown_seconds": int(data.get("cooldown_seconds", 0)),
        "paused": bool(data.get("paused", False)),
        "premium_add_cd": data.get("premium_add_cd", {}),
        "reload_add_once": data.get("reload_add_once", {}),
        "user_cooldowns": data.get("user_cooldowns", {}),
        "support_report_cd": data.get("support_report_cd", {}),
        "report_mode_cd": data.get("report_mode_cd", {}),
    }
    WHITELIST_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ACCESS_STATE_FILE.write_text(
        json.dumps({
            "global_limits": payload["global_limits"],
            "cooldown_seconds": payload["cooldown_seconds"],
            "paused": payload["paused"],
            "premium_add_cd": payload["premium_add_cd"],
            "reload_add_once": payload["reload_add_once"],
            "user_cooldowns": payload["user_cooldowns"],
            "support_report_cd": payload["support_report_cd"],
            "report_mode_cd": payload["report_mode_cd"],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


DATA = load_data()


def _load_consents() -> dict[str, str]:
    if CONSENTS_FILE.exists():
        try:
            return json.loads(CONSENTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_consents(c: dict[str, str]) -> None:
    CONSENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONSENTS_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


CONSENTS = _load_consents()


def has_consent(uid: int) -> bool:
    return str(uid) in CONSENTS


def set_consent(uid: int) -> None:
    CONSENTS[str(uid)] = _to_iso(_now())
    _save_consents(CONSENTS)


def _get_user(uid: int) -> dict[str, Any]:
    key = str(uid)
    if key not in DATA["users"]:
        DATA["users"][key] = _empty_user()
    return DATA["users"][key]


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_premium(uid: int) -> bool:
    if is_admin(uid):
        return True
    return bool(_get_user(uid).get("premium"))


def _is_subscription_active(uid: int) -> bool:
    if is_admin(uid):
        return True
    exp = _parse_iso(_get_user(uid).get("expires_at"))
    if exp is None:
        return False
    return exp > _now()


def has_access(uid: int) -> bool:
    return is_admin(uid) or _is_subscription_active(uid)


def get_expires_at(uid: int) -> datetime | None:
    if is_admin(uid):
        return None
    return _parse_iso(_get_user(uid).get("expires_at"))


def format_expires(uid: int) -> str:
    if is_admin(uid):
        return "бессрочно (администратор)"
    exp = get_expires_at(uid)
    if exp is None:
        return "нет активной подписки"
    local = exp.astimezone()
    remaining = exp - _now()
    if remaining.total_seconds() <= 0:
        return "истекла"
    days = int(remaining.total_seconds() // 86400)
    hours = int((remaining.total_seconds() % 86400) // 3600)
    mins = int((remaining.total_seconds() % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if mins and not days:
        parts.append(f"{mins} мин.")
    left = " ".join(parts) if parts else "менее минуты"
    return f"до {local.strftime('%d.%m.%Y %H:%M')} (осталось {left})"


def parse_duration(raw: str) -> timedelta | None:
    raw = raw.strip().lower()
    m = re.fullmatch(r"(-?\d+)\s*([dh])", raw)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return timedelta(days=n)
    return timedelta(hours=n)


def add_time(uid: int, delta: timedelta) -> datetime:
    u = _get_user(uid)
    now = _now()
    current = _parse_iso(u.get("expires_at"))
    if current is None or current < now:
        base = now
    else:
        base = current
    new_exp = base + delta
    if new_exp <= now:
        u["expires_at"] = None
        save_data(DATA)
        return now
    u["expires_at"] = _to_iso(new_exp)
    save_data(DATA)
    return new_exp


def remove_access(uid: int) -> None:
    u = _get_user(uid)
    u["expires_at"] = None
    save_data(DATA)


def set_premium(uid: int, value: bool) -> None:
    u = _get_user(uid)
    u["premium"] = bool(value)
    save_data(DATA)


def is_paused() -> bool:
    return bool(DATA.get("paused", False))


def toggle_pause() -> bool:
    DATA["paused"] = not bool(DATA.get("paused", False))
    save_data(DATA)
    return DATA["paused"]


def get_cooldown() -> int:
    return int(DATA.get("cooldown_seconds", 0))


def set_cooldown(seconds: int) -> None:
    DATA["cooldown_seconds"] = max(0, int(seconds))
    save_data(DATA)


def set_limit(target: str, mode: str, value: int) -> str:
    mode = mode.lower()
    if mode not in ("fast", "standard"):
        raise ValueError("mode must be fast or standard")
    value = max(0, int(value))

    if target.lower() == "all":
        DATA.setdefault("global_limits", {})[mode] = value
        save_data(DATA)
        return f"Глобальный лимит {mode} = {value}"

    uid = int(target)
    u = _get_user(uid)
    u.setdefault("limits", {})[mode] = value
    save_data(DATA)
    return f"Лимит {mode} для {uid} = {value}"


def _effective_limits(uid: int) -> dict[str, int]:
    gl = DATA.get("global_limits", {})
    u = _get_user(uid)
    ul = u.get("limits") or {}
    return {
        "standard": int(ul.get("standard", gl.get("standard", DEFAULT_LIMIT_STANDARD))),
        "fast": int(ul.get("fast", gl.get("fast", DEFAULT_LIMIT_FAST))),
    }


def _refresh_usage_window(uid: int) -> dict[str, Any]:
    u = _get_user(uid)
    used = u.setdefault("used", {"standard": 0, "fast": 0, "window": None})
    ws = _window_start()
    stored = _parse_iso(used.get("window"))
    if stored is None or stored < ws:
        used["standard"] = 0
        used["fast"] = 0
        used["window"] = _to_iso(ws)
        save_data(DATA)
    return used


def get_usage(uid: int) -> tuple[dict[str, int], dict[str, int]]:
    used = _refresh_usage_window(uid)
    limits = _effective_limits(uid)
    return (
        {"standard": int(used.get("standard", 0)), "fast": int(used.get("fast", 0))},
        limits,
    )


def check_and_consume_limit(uid: int, mode: str) -> tuple[bool, str]:
    if is_admin(uid) or is_premium(uid):
        return True, ""

    if is_paused():
        return False, (
            "⏸ Бот временно на паузе.\n"
            "Обращения к помощнику недоступны. Попробуйте позже."
        )

    mode = "fast" if mode == "fast" else "standard"
    cd = get_cooldown()
    if cd > 0:
        last = _parse_iso(DATA.get("user_cooldowns", {}).get(str(uid)))
        if last is not None:
            elapsed = (_now() - last).total_seconds()
            if elapsed < cd:
                left = int(cd - elapsed)
                return False, f"⏳ Подождите ещё {left} сек. перед следующим запросом."

    used = _refresh_usage_window(uid)
    limits = _effective_limits(uid)
    if int(used.get(mode, 0)) >= limits[mode]:
        next_w = _window_start() + timedelta(hours=3)
        local = next_w.astimezone().strftime("%H:%M")
        mode_ru = "фаст" if mode == "fast" else "стандарт"
        return (
            False,
            f"🚫 Лимит режима «{mode_ru}» исчерпан ({limits[mode]} за 3 часа).\n"
            f"Обновится около {local}.",
        )

    used[mode] = int(used.get(mode, 0)) + 1
    DATA.setdefault("user_cooldowns", {})[str(uid)] = _to_iso(_now())
    save_data(DATA)
    return True, ""


def get_premium_add_settings(uid: int) -> tuple[int, int]:
    """
    Возвращает (cd_seconds, max_hours) для /add премиума.
    По умолчанию: PREMIUM_ADD_CD_SEC и 24 часа.
    """
    u = _get_user(uid)
    s = u.get("add_settings") or {}
    cd_min = s.get("cd_minutes")
    max_h = s.get("max_hours")
    if cd_min is None:
        cd_sec = int(PREMIUM_ADD_CD_SEC)
    else:
        cd_sec = max(0, int(cd_min) * 60)
    if max_h is None:
        max_h = 24
    else:
        max_h = max(1, int(max_h))
    return cd_sec, max_h


def set_premium_add_settings(uid: int, cd_minutes: int, max_hours: int) -> None:
    u = _get_user(uid)
    u["add_settings"] = {
        "cd_minutes": max(0, int(cd_minutes)),
        "max_hours": max(1, int(max_hours)),
    }
    save_data(DATA)


def can_use_premium_add(uid: int) -> tuple[bool, str]:
    if is_admin(uid):
        return True, ""
    if not is_premium(uid):
        return False, "❌ Команда доступна только премиум-пользователям и администраторам."

    if DATA.get("reload_add_once", {}).get(str(uid)):
        return True, ""

    cd_sec, _max_h = get_premium_add_settings(uid)
    if cd_sec <= 0:
        return True, ""

    last = _parse_iso(DATA.get("premium_add_cd", {}).get(str(uid)))
    if last is None:
        return True, ""
    elapsed = (_now() - last).total_seconds()
    if elapsed >= cd_sec:
        return True, ""
    left = int(cd_sec - elapsed)
    mins = left // 60
    secs = left % 60
    return False, f"⏳ КД /add: ещё {mins} мин. {secs} сек."


def mark_premium_add_used(uid: int) -> None:
    if is_admin(uid):
        return
    DATA.get("reload_add_once", {}).pop(str(uid), None)
    DATA.setdefault("premium_add_cd", {})[str(uid)] = _to_iso(_now())
    save_data(DATA)


def grant_reload_add(uid: int) -> None:
    DATA.setdefault("reload_add_once", {})[str(uid)] = True
    save_data(DATA)


def check_support_report_cd(uid: int) -> tuple[bool, str]:
    if is_admin(uid):
        return True, ""
    from config import SUPPORT_REPORT_CD_SEC
    last = _parse_iso(DATA.get("support_report_cd", {}).get(str(uid)))
    if last is None:
        return True, ""
    elapsed = (_now() - last).total_seconds()
    if elapsed >= SUPPORT_REPORT_CD_SEC:
        return True, ""
    left = int(SUPPORT_REPORT_CD_SEC - elapsed)
    return False, f"⏳ Сообщение в поддержку можно раз в 5 минут. Осталось {left} сек."


def mark_support_report(uid: int) -> None:
    DATA.setdefault("support_report_cd", {})[str(uid)] = _to_iso(_now())
    save_data(DATA)


def check_report_mode_cd(uid: int) -> tuple[bool, str]:
    if is_admin(uid) or is_premium(uid):
        return True, ""
    from config import REPORT_COOLDOWN_SEC
    last = _parse_iso(DATA.get("report_mode_cd", {}).get(str(uid)))
    if last is None:
        return True, ""
    elapsed = (_now() - last).total_seconds()
    if elapsed >= REPORT_COOLDOWN_SEC:
        return True, ""
    left = int(REPORT_COOLDOWN_SEC - elapsed)
    return False, f"⏳ Составление отчёта: подождите {left} сек."


def mark_report_mode(uid: int) -> None:
    DATA.setdefault("report_mode_cd", {})[str(uid)] = _to_iso(_now())
    save_data(DATA)


def iter_active_user_ids() -> list[int]:
    result = []
    for uid_str, u in DATA.get("users", {}).items():
        uid = int(uid_str)
        if uid in ADMIN_IDS:
            continue
        exp = _parse_iso(u.get("expires_at"))
        if exp and exp > _now():
            result.append(uid)
    return sorted(result)


def iter_expired_user_ids() -> list[int]:
    result = []
    for uid_str, u in DATA.get("users", {}).items():
        uid = int(uid_str)
        if uid in ADMIN_IDS:
            continue
        exp = _parse_iso(u.get("expires_at"))
        if exp is None or exp <= _now():
            result.append(uid)
    return sorted(set(result))


def list_users_summary_data(active_only: bool = True) -> list[dict]:
    rows = []
    if active_only:
        ids = sorted(set(list(ADMIN_IDS) + iter_active_user_ids()))
    else:
        ids = iter_expired_user_ids()

    for uid in ids:
        used, limits = get_usage(uid)
        rows.append({
            "uid": uid,
            "admin": uid in ADMIN_IDS,
            "premium": bool(_get_user(uid).get("premium")) and uid not in ADMIN_IDS,
            "expires": format_expires(uid),
            "used": used,
            "limits": limits,
        })
    return rows


WHITELIST: set[int] = set()


def refresh_compat_set() -> None:
    global WHITELIST
    WHITELIST = set()
    for uid_str, u in DATA.get("users", {}).items():
        exp = _parse_iso(u.get("expires_at"))
        if exp and exp > _now():
            WHITELIST.add(int(uid_str))
    WHITELIST |= ADMIN_IDS


refresh_compat_set()


def save_whitelist(_ids=None) -> None:
    save_data(DATA)
    refresh_compat_set()


def check_photo_limit(uid: int, add_count: int = 0) -> tuple[bool, str, int]:
    """
    Лимит фотографий для нейросети: PHOTO_LIMIT_COUNT за PHOTO_LIMIT_WINDOW_SEC.
    Возвращает (ok, message, remaining_slots).
    """
    from config import PHOTO_LIMIT_COUNT, PHOTO_LIMIT_WINDOW_SEC
    if is_admin(uid) or is_premium(uid):
        return True, "", PHOTO_LIMIT_COUNT

    u = _get_user(uid)
    now = _now()
    hist = u.get("photo_ts") or []
    # hist: list of ISO timestamps
    cutoff = now - __import__("datetime").timedelta(seconds=PHOTO_LIMIT_WINDOW_SEC)
    kept = []
    for s in hist:
        ts = _parse_iso(s)
        if ts and ts >= cutoff:
            kept.append(s)
    used = len(kept)
    if used + add_count > PHOTO_LIMIT_COUNT:
        left_sec = PHOTO_LIMIT_WINDOW_SEC
        if kept:
            oldest = _parse_iso(kept[0])
            if oldest:
                left_sec = max(0, int(PHOTO_LIMIT_WINDOW_SEC - (now - oldest).total_seconds()))
        mins = left_sec // 60
        return (
            False,
            f"📷 Лимит фото: {PHOTO_LIMIT_COUNT} за 15 мин. Подождите ~{mins} мин.",
            max(0, PHOTO_LIMIT_COUNT - used),
        )
    return True, "", max(0, PHOTO_LIMIT_COUNT - used)


def consume_photo_slots(uid: int, count: int) -> None:
    if count <= 0:
        return
    if is_admin(uid) or is_premium(uid):
        return
    u = _get_user(uid)
    hist = u.get("photo_ts") or []
    now_iso = _to_iso(_now())
    from config import PHOTO_LIMIT_WINDOW_SEC
    cutoff = _now() - __import__("datetime").timedelta(seconds=PHOTO_LIMIT_WINDOW_SEC)
    kept = []
    for s in hist:
        ts = _parse_iso(s)
        if ts and ts >= cutoff:
            kept.append(s)
    for _ in range(count):
        kept.append(now_iso)
    u["photo_ts"] = kept
    save_data(DATA)

