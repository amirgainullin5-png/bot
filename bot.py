import asyncio
import logging
import json
import re

from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, KeyboardButtonColor, Text

from config import (
    VK_TOKEN,
    ADMIN_IDS,
    USER_MODES_FILE,
    MAX_USER_TEXT_LEN,
    MAX_ATTACHMENTS,
)
from services.whitelist import (
    has_access,
    is_admin,
    is_premium,
    is_paused,
    toggle_pause,
    get_cooldown,
    set_cooldown,
    set_limit,
    add_time,
    remove_access,
    set_premium,
    parse_duration,
    format_expires,
    get_usage,
    check_and_consume_limit,
    can_use_premium_add,
    mark_premium_add_used,
    grant_reload_add,
    get_premium_add_settings,
    set_premium_add_settings,
    check_support_report_cd,
    mark_support_report,
    list_users_summary_data,
    has_consent,
    set_consent,
)
from handlers.report import handle_report
from handlers.complaint import handle_complaint
from handlers.lawyer import handle_lawyer, ensure_laws

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

if not VK_TOKEN:
    raise RuntimeError("VK_TOKEN / VK_TOKEN_TEST не задан в .env")

bot = Bot(token=VK_TOKEN)


# ========== РЕЖИМЫ ==========
def load_user_modes() -> dict:
    if USER_MODES_FILE.exists():
        try:
            with open(USER_MODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_user_modes(modes: dict):
    USER_MODES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(USER_MODES_FILE, "w", encoding="utf-8") as f:
        json.dump(modes, f, ensure_ascii=False, indent=2)


USER_MODES = load_user_modes()


def get_user_mode(uid: int) -> str:
    return USER_MODES.get(str(uid), "standard")


def set_user_mode(uid: int, mode: str):
    USER_MODES[str(uid)] = mode
    save_user_modes(USER_MODES)


def mode_ru(mode: str) -> str:
    return "фаст" if mode == "fast" else "стандарт"


# ========== КЛАВИАТУРЫ ==========
def main_kb():
    kb = Keyboard(one_time=False)
    kb.add(Text("📋 Составление отчета"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("⚖️ Жалоба / иск"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("⚙️ Режим"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("👤 Профиль"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def mode_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("📘 Стандарт"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("⚡ Фаст"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("« Назад"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def consent_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("✅ Продолжить"), color=KeyboardButtonColor.POSITIVE)
    return kb.get_json()


# ========== ТЕКСТЫ ==========
CONSENT_TEXT = (
    "👋 Добро пожаловать в бота-юриста!\n\n"
    "Перед началом работы необходимо ознакомиться с условиями "
    "обработки персональных данных (в соответствии с Федеральным законом "
    "№ 152-ФЗ «О персональных данных»).\n\n"
    "📌 Оператор обрабатывает следующие данные:\n"
    "• идентификатор пользователя ВКонтакте (VK ID);\n"
    "• имя и фамилия, отображаемые в профиле VK (при обращении к API);\n"
    "• текст сообщений, которые вы отправляете боту;\n"
    "• изображения (скриншоты), которые вы прикладываете к запросам;\n"
    "• сведения о подписке, лимитах и выбранном режиме работы;\n"
    "• технические данные обращений (время запросов, факт согласия).\n\n"
    "🎯 Цели обработки:\n"
    "• предоставление функций бота (юридические разборы, отчёты, жалобы/иски);\n"
    "• учёт доступа и лимитов;\n"
    "• связь с технической поддержкой по вашей инициативе.\n\n"
    "⏱ Срок хранения — на период использования бота и до отзыва согласия / "
    "отзыва доступа. Данные не передаются третьим лицам, за исключением "
    "сервисов, необходимых для работы (API ВКонтакте, сервис генерации ответов).\n\n"
    "Вы вправе отозвать согласие, прекратив использование бота и обратившись "
    "к администратору.\n\n"
    "Нажимая «✅ Продолжить», вы подтверждаете согласие на обработку "
    "указанных персональных данных."
)

GUIDE_TEXT = (
    "📖 Краткий гайд по боту\n\n"
    "🔹 Просто напишите вопрос или пришлите скриншот — бот разберёт ситуацию "
    "по базе норм проекта.\n"
    "🔹 «фаст …» в начале сообщения — быстрый краткий ответ (только текст, фото не разбираются).\n\n"
    "📋 Составление отчета — пошаговый рапорт (BB-код).\n"
    "⚖️ Жалоба / иск — подготовка текста жалобы или развёрнутого иска.\n"
    "⚙️ Режим — стандарт (текст+фото) или фаст (только текст, кратко).\n"
    "👤 Профиль — срок подписки и лимиты.\n\n"
    "Команды:\n"
    "/help — справка\n"
    "/mysub или кнопка «Профиль» — подписка\n"
    "/report текст — сообщение в поддержку (раз в 5 мин)\n"
    "/mode стандарт|фаст — сменить режим\n\n"
    "Удачной работы! ⚖️"
)


def help_text_for(uid: int) -> str:
    mode = mode_ru(get_user_mode(uid))
    lines = [
        "⚖️ Юридический помощник",
        "",
        f"Ваш режим по умолчанию: {mode}",
        "",
        "📌 Возможности кнопок:",
        "• 📋 Составление отчета — пошаговый рапорт (BB-код)",
        "• ⚖️ Жалоба / иск — текст жалобы или иска по вашим материалам",
        "• ⚙️ Режим — «стандарт» (текст+фото) или «фаст» (только текст)",
        "• 👤 Профиль — подписка и лимиты",
        "",
        "💬 Можно просто написать ситуацию или прислать скриншот.",
        "Префикс «фаст …» — быстрый ответ на один запрос.",
        "",
        "📎 Ваши команды:",
        "• /help — эта справка",
        "• /status — статус",
        "• /mysub — подписка",
        "• /mode стандарт|фаст — режим по умолчанию",
        "• /report <текст> — написать в поддержку",
    ]
    if is_premium(uid) or is_admin(uid):
        lines += [
            "",
            "⭐ Премиум:",
            "• /list — активные подписки",
            "• /listoff — истекшие подписки",
            "• /add id/@user ±Nh — выдать часы (лимит задаёт админ через /setadd)",
        ]
    if is_admin(uid):
        lines += [
            "",
            "🛡 Админ:",
            "• /add id/@user 5d|12h|-1d — любое время",
            "• /remove id/@user — снять доступ",
            "• /premium id 1|0 — премиум",
            "• /pause — пауза нейросети для обычных",
            "• /setcd N — кулдаун (сек), 0 = снять",
            "• /setlimit all|id standard|fast N — лимиты",
            "• /setadd id КД_мин макс_часов — рамки /add для премиума\n• /reload_add id — сброс КД /add премиуму (1 раз)",
            "• /listoff — список без подписки",
        ]
    return "\n".join(lines)


def profile_text(uid: int) -> str:
    used, limits = get_usage(uid)
    mode = mode_ru(get_user_mode(uid))
    parts = [
        "👤 Ваш профиль",
        "",
        f"📅 Подписка: {format_expires(uid)}",
        f"⚙️ Режим: {mode}",
        f"📊 Лимиты (окно 3 ч): стандарт {used['standard']}/{limits['standard']}, "
        f"фаст {used['fast']}/{limits['fast']}",
    ]
    if is_admin(uid):
        parts.append("🛡 Роль: администратор")
    elif is_premium(uid):
        parts.append("⭐ Статус: премиум")
    # обычным не пишем «не премиум»
    if is_paused() and not is_premium(uid) and not is_admin(uid):
        parts.append("⏸ Сейчас бот на паузе для обычных запросов.")
    cd = get_cooldown()
    if cd > 0 and not is_premium(uid) and not is_admin(uid):
        parts.append(f"⏱ Интервал между запросами: {cd} сек.")
    return "\n".join(parts)


# ========== УТИЛИТЫ ==========
async def resolve_vk_id(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"\[id(\d+)\|", raw)
    if m:
        return int(m.group(1))
    raw = raw.lstrip("@")
    if raw.isdigit():
        return int(raw)
    if raw.lower().startswith("id") and raw[2:].isdigit():
        return int(raw[2:])
    if len(raw) > 64 or not re.fullmatch(r"[A-Za-z0-9_.]+", raw):
        return None
    try:
        users = await bot.api.users.get(user_ids=[raw])
        if users:
            return users[0].id
    except Exception as e:
        logger.warning(f"resolve_vk_id({raw}): {e}")
    return None


async def fetch_names(uids: list[int]) -> dict[int, str]:
    """uid -> 'Имя Фамилия' для [id|Имя Фамилия]."""
    result: dict[int, str] = {}
    if not uids:
        return result
    # VK API: до 1000 за раз, но дробим по 100
    for i in range(0, len(uids), 100):
        chunk = uids[i : i + 100]
        try:
            users = await bot.api.users.get(user_ids=[str(u) for u in chunk])
            for u in users:
                name = f"{u.first_name} {u.last_name}".strip()
                result[u.id] = name or str(u.id)
        except Exception as e:
            logger.warning(f"users.get: {e}")
            for u in chunk:
                result.setdefault(u, str(u))
    return result


def vk_mention(uid: int, name: str) -> str:
    safe = (name or str(uid)).replace("|", " ")
    return f"[id{uid}|{safe}]"


async def notify_user(uid: int, text: str):
    try:
        await bot.api.messages.send(user_id=uid, message=text, random_id=0)
    except Exception as e:
        logger.warning(f"notify_user({uid}): {e}")


def _safe_text(text: str) -> str:
    if not text:
        return ""
    if len(text) > MAX_USER_TEXT_LEN:
        return text[:MAX_USER_TEXT_LEN]
    return text


async def format_list_message(active: bool) -> str:
    rows = list_users_summary_data(active_only=active)
    if not rows:
        return "Список пуст." if active else "Нет пользователей с истекшей подпиской."
    uids = [r["uid"] for r in rows]
    names = await fetch_names(uids)
    title = "📋 Активные подписки" if active else "📭 Истёкшие / без подписки"
    lines = [title, ""]
    for r in rows:
        uid = r["uid"]
        mention = vk_mention(uid, names.get(uid, str(uid)))
        flags = []
        if r["admin"]:
            flags.append("🛡 admin")
        if r["premium"]:
            flags.append("⭐ premium")
        flag_s = (" " + " ".join(flags)) if flags else ""
        if active:
            lines.append(
                f"• {mention}{flag_s}\n"
                f"  {r['expires']}\n"
                f"  лимиты: стд {r['used']['standard']}/{r['limits']['standard']}, "
                f"фаст {r['used']['fast']}/{r['limits']['fast']}"
            )
        else:
            lines.append(f"• {mention}{flag_s}\n  {r['expires']}")
    return "\n\n".join(lines) if lines else title


async def send_long(message: Message, text: str, keyboard=None):
    if len(text) <= 3900:
        await message.answer(text, keyboard=keyboard)
        return
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)]
    for i, ch in enumerate(chunks):
        kb = keyboard if i == len(chunks) - 1 else None
        await message.answer(ch, keyboard=kb)


# ========== ОБРАБОТЧИК ==========
@bot.on.message()
async def universal_handler(message: Message):
    uid = message.from_id
    text = _safe_text((message.text or "").strip())

    # Согласие (до доступа к функциям)
    if not has_consent(uid):
        if text == "✅ Продолжить":
            set_consent(uid)
            if has_access(uid):
                await message.answer(GUIDE_TEXT, keyboard=main_kb())
            else:
                await message.answer(
                    GUIDE_TEXT + "\n\n⚠️ Сейчас у вас нет активной подписки. "
                    "Обратитесь к администратору для доступа."
                )
            return
        # любое первое сообщение — показываем согласие
        await message.answer(CONSENT_TEXT, keyboard=consent_kb())
        return

    if message.attachments and len(message.attachments) > MAX_ATTACHMENTS:
        await message.answer(
            f"❌ Слишком много вложений (макс. {MAX_ATTACHMENTS}).",
            keyboard=main_kb() if has_access(uid) else None,
        )
        return

    # 1. Отчёт
    handled = await handle_report(message, has_access)
    if handled:
        return

    # 2. Жалоба / иск
    handled = await handle_complaint(message, has_access)
    if handled:
        return

    # 3. Кнопки UI
    if text in ("⚙️ Режим", "Установить режим"):
        if not has_access(uid):
            return
        await message.answer(
            f"Текущий режим: {mode_ru(get_user_mode(uid))}\nВыберите новый:",
            keyboard=mode_kb(),
        )
        return

    if text in ("📘 Стандарт", "📘 Стандартный режим"):
        if not has_access(uid):
            return
        set_user_mode(uid, "standard")
        await message.answer("✅ Режим: стандарт", keyboard=main_kb())
        return

    if text in ("⚡ Фаст", "⚡ Быстрый режим"):
        if not has_access(uid):
            return
        set_user_mode(uid, "fast")
        await message.answer("✅ Режим: фаст", keyboard=main_kb())
        return

    if text in ("« Назад", "Отмена"):
        if has_access(uid):
            await message.answer("Главное меню.", keyboard=main_kb())
        return

    if text in ("👤 Профиль", "Моя подписка", "/mysub"):
        if not has_access(uid) and not is_admin(uid):
            await message.answer("❌ Нет активной подписки.")
            return
        await message.answer(profile_text(uid), keyboard=main_kb())
        return

    # 4. Команды
    if text.startswith(("/", "!")):
        parts = text[1:].split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("start", "help", "помощь", "начать"):
            if not has_access(uid) and not is_admin(uid):
                await message.answer("❌ Нет активной подписки.")
                return
            await message.answer(help_text_for(uid), keyboard=main_kb())
            return

        if cmd == "status":
            if not has_access(uid) and not is_admin(uid):
                return
            await message.answer(profile_text(uid), keyboard=main_kb())
            return

        if cmd == "mode":
            if not has_access(uid):
                return
            if not args or args[0].lower() not in (
                "standard", "fast", "стандарт", "быстрый", "фаст"
            ):
                await message.answer(
                    "Использование:\n/mode стандарт\n/mode фаст",
                    keyboard=main_kb(),
                )
                return
            new_mode = (
                "fast"
                if args[0].lower() in ("fast", "быстрый", "фаст")
                else "standard"
            )
            set_user_mode(uid, new_mode)
            await message.answer(
                f"✅ Режим по умолчанию: {mode_ru(new_mode)}",
                keyboard=main_kb(),
            )
            return

        # /report ТОЛЬКО с текстом — техподдержка
        if cmd == "report":
            if not has_access(uid):
                return
            if not args:
                await message.answer(
                    "📩 Напишите: /report ваш текст\n"
                    "Сообщение уйдёт администраторам.\n"
                    "КД: 5 минут.",
                    keyboard=main_kb(),
                )
                return
            ok, reason = check_support_report_cd(uid)
            if not ok:
                await message.answer(reason, keyboard=main_kb())
                return
            report_text = " ".join(args)[:2000]
            mark_support_report(uid)
            admin_msg = (
                f"📩 Обращение в поддержку\n"
                f"От: [id{uid}|пользователь]\n\n"
                f"{report_text}"
            )
            for aid in ADMIN_IDS:
                try:
                    await bot.api.messages.send(
                        user_id=aid, message=admin_msg, random_id=0
                    )
                except Exception as e:
                    logger.warning(f"report admin {aid}: {e}")
            await message.answer(
                "✅ Сообщение отправлено в поддержку.",
                keyboard=main_kb(),
            )
            return

        if cmd == "list":
            if not (is_admin(uid) or is_premium(uid)):
                return
            msg = await format_list_message(active=True)
            await send_long(message, msg)
            return

        if cmd == "listoff":
            if not (is_admin(uid) or is_premium(uid)):
                return
            msg = await format_list_message(active=False)
            await send_long(message, msg)
            return

        if cmd == "add":
            if not (is_admin(uid) or is_premium(uid)):
                return
            if len(args) < 2:
                await message.answer(
                    "Использование:\n"
                    "/add @user 5d\n"
                    "/add 123456 12h\n"
                    "/add @user -1d\n\n"
                    "Премиум: только часы (h); лимит задаёт админ (/setadd)."
                )
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Пользователь не найден: {args[0]}")
                return
            duration = parse_duration(args[1])
            if duration is None:
                await message.answer("❌ Время: 5d, -1d, 12h, -3h")
                return
            if not is_admin(uid):
                ok, reason = can_use_premium_add(uid)
                if not ok:
                    await message.answer(reason)
                    return
                _cd, max_h = get_premium_add_settings(uid)
                total_hours = duration.total_seconds() / 3600
                # премиум — только часы (h), в рамках max_h
                if args[1].lower().endswith("d"):
                    await message.answer(
                        f"❌ Премиум: только часы (h), диапазон −{max_h}h…{max_h}h."
                    )
                    return
                if abs(total_hours) > max_h:
                    await message.answer(
                        f"❌ Премиум: доступно от −{max_h}h до {max_h}h."
                    )
                    return
                mark_premium_add_used(uid)

            add_time(target, duration)
            sign = "+" if duration.total_seconds() >= 0 else ""
            exp_str = format_expires(target)
            await message.answer(
                f"✅ Доступ изменён: [id{target}|id{target}]\n"
                f"Изменение: {sign}{args[1]}\n"
                f"Подписка: {exp_str}"
            )
            await notify_user(
                target,
                f"🔔 Изменение доступа к боту-юристу\n"
                f"Изменение: {sign}{args[1]}\n"
                f"Срок: {exp_str}",
            )
            # если /add от премиума (не админа) — уведомить администраторов
            if not is_admin(uid) and is_premium(uid):
                admin_msg = (
                    f"📢 Премиум выдал /add\n\n"
                    f"Кто: [id{uid}|id{uid}]\n"
                    f"Кому: [id{target}|id{target}]\n"
                    f"Изменение: {sign}{args[1]}\n"
                    f"Подписка получателя: {exp_str}"
                )
                for aid in ADMIN_IDS:
                    if aid == uid:
                        continue
                    try:
                        await bot.api.messages.send(
                            user_id=aid, message=admin_msg, random_id=0
                        )
                    except Exception as e:
                        logger.warning(f"add notify admin {aid}: {e}")
            return

        if cmd in ("remove", "del", "del_id", "remove_id"):
            if not is_admin(uid):
                return
            if not args:
                await message.answer("Использование: /remove @user")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Не найден: {args[0]}")
                return
            remove_access(target)
            await message.answer(f"✅ Доступ обнулён: {target}")
            await notify_user(
                target,
                "❌ Ваш доступ к боту-юристу был отозван.",
            )
            return

        if cmd == "premium":
            if not is_admin(uid):
                return
            if len(args) < 2 or args[1] not in ("0", "1"):
                await message.answer("/premium @user 1  или  /premium id 0")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Не найден: {args[0]}")
                return
            val = args[1] == "1"
            set_premium(target, val)
            if val:
                await message.answer(f"✅ Премиум выдан: {target}")
                await notify_user(
                    target,
                    "⭐ Вам выдан статус премиум.\n"
                    "Безлимит к нейросети, /add и /list.",
                )
            else:
                await message.answer(f"✅ Премиум снят: {target}")
                await notify_user(target, "Премиум-статус снят.")
            return

        if cmd == "pause":
            if not is_admin(uid):
                return
            state = toggle_pause()
            await message.answer(
                "⏸ Пауза включена." if state else "▶️ Пауза снята."
            )
            return

        if cmd == "setcd":
            if not is_admin(uid):
                return
            if not args:
                await message.answer(
                    f"Текущий КД: {get_cooldown()} сек\n/setcd <сек>  (0 — снять)"
                )
                return
            try:
                sec = int(args[0])
            except ValueError:
                await message.answer("❌ Нужно целое число")
                return
            if sec < 0 or sec > 86400:
                await message.answer("❌ 0…86400")
                return
            set_cooldown(sec)
            await message.answer(f"✅ Кулдаун: {sec} сек.")
            return

        if cmd == "setlimit":
            if not is_admin(uid):
                return
            if len(args) < 3:
                await message.answer(
                    "/setlimit all standard 5\n"
                    "/setlimit all fast 10\n"
                    "/setlimit @user fast 20"
                )
                return
            target_raw, mode_raw, num_raw = args[0], args[1], args[2]
            mode = mode_raw.lower()
            if mode not in ("fast", "standard"):
                await message.answer("Режим: fast или standard")
                return
            try:
                num = int(num_raw)
            except ValueError:
                await message.answer("❌ Число лимита")
                return
            if num < 0 or num > 10000:
                await message.answer("❌ 0…10000")
                return

            if target_raw.lower() == "all":
                msg = set_limit("all", mode, num)
                await message.answer(f"✅ {msg}")
            else:
                target = await resolve_vk_id(target_raw)
                if target is None:
                    await message.answer(f"❌ Не найден: {target_raw}")
                    return
                msg = set_limit(str(target), mode, num)
                await message.answer(f"✅ {msg}")
                mode_h = "фаст" if mode == "fast" else "стандарт"
                await notify_user(
                    target,
                    f"🔔 Ваши лимиты обновлены.\n"
                    f"Режим «{mode_h}»: {num} запросов за 3 часа.",
                )
            return

        if cmd == "setadd":
            if not is_admin(uid):
                return
            if len(args) < 3:
                await message.answer(
                    "Настройка /add для премиума:\n"
                    "/setadd @user <кд_мин> <макс_часов>\n\n"
                    "Пример: /setadd @user 5 120\n"
                    "→ КД 5 мин, выдача от −120h до 120h"
                )
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Не найден: {args[0]}")
                return
            try:
                cd_min = int(args[1])
                max_h = int(args[2])
            except ValueError:
                await message.answer("❌ КД и часы должны быть целыми числами")
                return
            if cd_min < 0 or cd_min > 10080:
                await message.answer("❌ КД: 0…10080 минут (0 = без КД)")
                return
            if max_h < 1 or max_h > 8760:
                await message.answer("❌ Часы: 1…8760")
                return
            set_premium_add_settings(target, cd_min, max_h)
            # если ещё не премиум — можно только напомнить
            prem_note = ""
            if not is_premium(target):
                prem_note = "\n⚠️ У пользователя нет премиума — настройки применятся после /premium … 1"
            await message.answer(
                f"✅ /add для [id{target}|id{target}]:\n"
                f"• КД: {cd_min} мин.\n"
                f"• Диапазон: −{max_h}h…{max_h}h"
                f"{prem_note}"
            )
            cd_line = "без кулдауна" if cd_min == 0 else f"{cd_min} мин."
            await notify_user(
                target,
                f"🔔 Вам изменён доступ к команде /add\n\n"
                f"• Кулдаун между использованиями: {cd_line}\n"
                f"• Можно выдавать подписку: от −{max_h}h до +{max_h}h\n\n"
                f"Пример: /add @user 12h",
            )
            return

        if cmd == "reload_add":
            if not is_admin(uid):
                return
            if not args:
                await message.answer("/reload_add @user")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Не найден: {args[0]}")
                return
            if not is_premium(target):
                await message.answer("❌ Пользователь не премиум.")
                return
            grant_reload_add(target)
            await message.answer(f"✅ КД /add сброшен на 1 раз для {target}")
            await notify_user(
                target,
                "🔄 Администратор снял кулдаун /add (на один раз).",
            )
            return

        if is_admin(uid) or has_access(uid):
            await message.answer(
                f"Неизвестная команда: /{cmd}\nНапишите /help",
                keyboard=main_kb() if has_access(uid) else None,
            )
        return

    # 5. Обычный текст → юрист
    if not has_access(uid):
        return

    mode = get_user_mode(uid)
    lower = text.lower()
    if lower.startswith(("фаст ", "fast ")):
        mode = "fast"
    elif lower.startswith(("стандарт ", "standard ")):
        mode = "standard"

    ok, reason = check_and_consume_limit(uid, mode)
    if not ok:
        await message.answer(reason, keyboard=main_kb())
        return

    await handle_lawyer(message, mode=mode)


async def main():
    logger.info("Бот запускается...")
    try:
        await ensure_laws()
        logger.info("laws.txt готов")
    except Exception as e:
        logger.error(f"laws.txt: {e}")
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
