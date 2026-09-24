import asyncio
import logging
import json
from handlers.complaint import handle_complaint
import re
from pathlib import Path

from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, KeyboardButtonColor, Text

from config import VK_TOKEN, ADMIN_IDS, USER_MODES_FILE
from services.whitelist import has_access, is_admin, WHITELIST, save_whitelist
from handlers.report import handle_report
from handlers.lawyer import handle_lawyer, ensure_laws

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("main")

bot = Bot(token=VK_TOKEN)

# ========== РЕЖИМЫ ПОЛЬЗОВАТЕЛЕЙ ==========
def load_user_modes() -> dict:
    if USER_MODES_FILE.exists():
        try:
            with open(USER_MODES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
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

# ========== КЛАВИАТУРЫ ==========
def main_kb():
    kb = Keyboard(one_time=False)
    kb.add(Text("Составление отчета"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("Написание жалобы/иска"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("Установить режим"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()

def mode_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("📘 Стандартный режим"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("⚡ Быстрый режим"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

# ========== ОБРАБОТЧИК ==========
@bot.on.message()
async def universal_handler(message: Message):
    uid = message.from_id
    text = (message.text or "").strip()

    # 1. Отчёты
    handled = await handle_report(message, has_access)
    if handled:
        return

    # 2. Жалобы / иски
    handled = await handle_complaint(message, has_access)
    if handled:
        return

    # 3. Кнопки режима ...

    # 2. Кнопки режима
    if text == "Установить режим":
        if not has_access(uid):
            return
        current = get_user_mode(uid)
        await message.answer(
            f"Текущий режим: {current}\nВыберите новый:",
            keyboard=mode_kb()
        )
        return

    if text == "📘 Стандартный режим":
        if not has_access(uid):
            return
        set_user_mode(uid, "standard")
        await message.answer("✅ Установлен стандартный режим", keyboard=main_kb())
        return

    if text == "⚡ Быстрый режим":
        if not has_access(uid):
            return
        set_user_mode(uid, "fast")
        await message.answer("✅ Установлен быстрый режим", keyboard=main_kb())
        return

    if text == "Отмена":
        await message.answer("Отменено.", keyboard=main_kb())
        return

    # 3. Команды
    if text.startswith(("/", "!")):
        parts = text[1:].split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("start", "help", "помощь", "начать"):
            if not has_access(uid):
                return
            mode = get_user_mode(uid)
            await message.answer(
                f"⚖️ Юрист Amazing Online\n\n"
                f"Текущий режим: {mode}\n\n"
                f"• Просто напиши ситуацию или пришли скриншот\n"
                f"• «фаст ...» — быстрый ответ на один раз\n"
                f"• /mode standard|fast — сменить режим по умолчанию\n"
                f"• /report — составление отчёта\n",
                f"• «Написание жалобы/иска» — анализ скринов/видео и текст жалобы",
                keyboard=main_kb()
            )
            return

        if cmd == "mode":
            if not has_access(uid):
                return
            if not args or args[0].lower() not in ("standard", "fast", "стандарт", "быстрый"):
                await message.answer("Использование:\n/mode standard\n/mode fast")
                return
            new_mode = "fast" if args[0].lower() in ("fast", "быстрый") else "standard"
            set_user_mode(uid, new_mode)
            await message.answer(f"✅ Режим по умолчанию изменён на: {new_mode}", keyboard=main_kb())
            return

        # Админские команды
        if not is_admin(uid):
            return

        if cmd == "list":
            ids = "\n".join(map(str, sorted(WHITELIST))) or "пусто"
            await message.answer(f"Белый список ({len(WHITELIST)}):\n{ids}")
            return

        async def resolve_vk_id(raw: str) -> int | None:
            raw = raw.strip()

            # VK-упоминание: [id515495282|@jonnsina] или [id515495282|Имя]
            m = re.search(r"\[id(\d+)\|", raw)
            if m:
                return int(m.group(1))

            # обычный @username или просто username
            raw = raw.lstrip("@")

            if raw.isdigit():
                return int(raw)

            if raw.lower().startswith("id") and raw[2:].isdigit():
                return int(raw[2:])

            try:
                users = await bot.api.users.get(user_ids=[raw])
                if users:
                    return users[0].id
            except Exception as e:
                logger.warning(f"resolve_vk_id({raw}): {e}")
            return None

        if cmd in ("add_id", "add"):
            if not args:
                await message.answer("Использование:\n/add 123456789\n/add @username")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Не удалось найти пользователя: {args[0]}")
                return
            if target in WHITELIST:
                await message.answer(f"ℹ️ Уже в списке: {target}")
            else:
                WHITELIST.add(target)
                save_whitelist(WHITELIST)
                await message.answer(f"✅ Доступ выдан: {target}")
                try:
                    await bot.api.messages.send(
                        user_id=target,
                        message="✅ Вам выдан доступ к боту-юристу Amazing Online.",
                        random_id=0
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить уведомление: {e}")
            return

        if cmd in ("del_id", "remove_id", "del", "remove"):
            if not args:
                await message.answer("Использование:\n/del 123456789\n/del @username")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer(f"❌ Не удалось найти пользователя: {args[0]}")
                return
            if target not in WHITELIST:
                await message.answer(f"ℹ️ Нет в списке: {target}")
            else:
                WHITELIST.discard(target)
                save_whitelist(WHITELIST)
                await message.answer(f"✅ Доступ забран: {target}")
                try:
                    await bot.api.messages.send(
                        user_id=target,
                        message="❌ Ваш доступ к боту-юристу Amazing Online был отозван.",
                        random_id=0
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить уведомление: {e}")
            return

        # Внутри обработчика команд:

        if cmd in ("add_id", "add"):
            if not args:
                await message.answer("Использование:\n/add_id 123456789\n/add_id @username")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer("❌ Пользователь не найден")
                return
            if target in WHITELIST:
                await message.answer(f"ℹ️ {target} уже в списке")
            else:
                WHITELIST.add(target)
                save_whitelist(WHITELIST)
                await message.answer(f"✅ Доступ выдан: {target}")
                try:
                    await bot.api.messages.send(
                        user_id=target,
                        message="✅ Вам выдан доступ к боту-юристу Amazing Online.",
                        random_id=0
                    )
                except Exception:
                    pass
            return

        if cmd in ("del_id", "remove_id", "del"):
            if not args:
                await message.answer("Использование:\n/del_id 123456789\n/del_id @username")
                return
            target = await resolve_vk_id(args[0])
            if target is None:
                await message.answer("❌ Пользователь не найден")
                return
            if target not in WHITELIST:
                await message.answer(f"ℹ️ {target} нет в списке")
            else:
                WHITELIST.discard(target)
                save_whitelist(WHITELIST)
                await message.answer(f"✅ Доступ забран: {target}")
                try:
                    await bot.api.messages.send(
                        user_id=target,
                        message="❌ Ваш доступ к боту-юристу Amazing Online был отозван.",
                        random_id=0
                    )
                except Exception:
                    pass
            return

    # 4. Обычные сообщения → юрист
    if not has_access(uid):
        return

    mode = get_user_mode(uid)
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