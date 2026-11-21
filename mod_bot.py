import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramNotFound
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")
# Для приватки: адрес её Bot API, например https://bot.example.lol
# Если обычный Telegram - оставь пустым.
API_BASE = os.getenv("API_BASE", "http://botapi-31-77-10-54.nip.io")
WARN_LIMIT = 3          # сколько варнов до авто-фриза
WARN_FREEZE = "24h"     # на сколько авто-фриз за лимит варнов
WARNS_FILE = "warns.json"

router = Router()

# ---------- хранилище варнов ----------
try:
    with open(WARNS_FILE, "r", encoding="utf-8") as f:
        WARNS: dict = json.load(f)
except Exception:
    WARNS = {}


def save_warns():
    with open(WARNS_FILE, "w", encoding="utf-8") as f:
        json.dump(WARNS, f)


def wkey(chat_id, user_id):
    return f"{chat_id}:{user_id}"


# ---------- утилиты ----------
UNITS = {
    "s": 1, "с": 1,
    "m": 60, "м": 60,
    "h": 3600, "ч": 3600,
    "d": 86400, "д": 86400,
    "w": 604800, "н": 604800,
}


def parse_duration(text: str):
    """30m, 2h, 1d, 10с ... -> timedelta или None"""
    m = re.fullmatch(r"(\d+)\s*([a-zа-я])", text.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if unit not in UNITS or n <= 0:
        return None
    return timedelta(seconds=n * UNITS[unit])


def human(td: timedelta) -> str:
    s = int(td.total_seconds())
    parts = []
    for name, sec in (("д", 86400), ("ч", 3600), ("мин", 60), ("сек", 1)):
        if s >= sec:
            parts.append(f"{s // sec} {name}")
            s %= sec
    return " ".join(parts) or "0 сек"


def mention(uid: int, name: str) -> str:
    safe = name.replace("<", "").replace(">", "").replace("&", "")
    return f'<a href="tg://user?id={uid}">{safe}</a>'


# Запасной список админов по ID (через запятую), например: ADMIN_IDS=123,456
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "1780243219,1780243206").replace(" ", "").split(",") if x.lstrip("-").isdigit()
}


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        status = str(getattr(m.status, "value", m.status)).lower()
        return status in ("administrator", "creator", "owner")
    except Exception as e:
        logging.warning("get_chat_member failed for %s: %r", user_id, e)
        return False


async def check_access(message: Message, bot: Bot) -> bool:
    if message.chat.type == "private":
        await message.reply("Эта команда работает только в чате.")
        return False
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.reply(f"Только для админов. Твой ID: {message.from_user.id}")
        return False
    return True


async def get_target(message: Message, command: CommandObject, bot: Bot):
    """Возвращает (user_id, name, остаток_аргументов) или None."""
    args = (command.args or "").split()
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        uid, name, rest = u.id, u.full_name, args
    elif args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
        name = str(uid)
        rest = args[1:]
    else:
        await message.reply("Ответь на сообщение человека или укажи его ID.")
        return None

    me = await bot.get_me()
    if uid == me.id:
        await message.reply("Себя я трогать не буду 🙂")
        return None
    if await is_admin(bot, message.chat.id, uid):
        await message.reply("Нельзя применять к админу.")
        return None
    return uid, name, rest


NO_RIGHTS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

FULL_RIGHTS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


# Программный фриз: если в приватке нет restrictChatMember,
# бот сам удаляет сообщения замороженных (нужен deleteMessage).
FROZEN_FILE = "frozen.json"
try:
    with open(FROZEN_FILE, "r", encoding="utf-8") as f:
        FROZEN: dict = json.load(f)
except Exception:
    FROZEN = {}

STATS = {"seen": 0, "deleted": 0, "delete_errors": 0, "last_error": "-"}


def save_frozen():
    try:
        with open(FROZEN_FILE, "w", encoding="utf-8") as f:
            json.dump(FROZEN, f)
    except Exception as e:
        logging.warning("не удалось сохранить frozen.json: %r", e)


def not_supported(e: Exception) -> bool:
    """Метод в приватке отсутствует (а не просто 'нет прав')."""
    return isinstance(e, TelegramNotFound) or "METHOD_NOT_FOUND" in str(e).upper()


def soft_freeze(chat_id: int, uid: int, until):
    FROZEN[wkey(chat_id, uid)] = until.timestamp() if until else 0
    save_frozen()


async def restrict(bot: Bot, chat_id: int, uid: int, td: timedelta | None) -> str:
    """Возвращает 'native' или 'soft' (программный фриз)."""
    until = datetime.now(timezone.utc) + td if td else None
    try:
        await bot.restrict_chat_member(
            chat_id, uid, permissions=NO_RIGHTS, until_date=until
        )
        return "native"
    except Exception as e:
        if not_supported(e):
            soft_freeze(chat_id, uid, until)
            logging.warning("restrictChatMember не поддерживается, программный фриз для %s", uid)
            return "soft"
        raise


SOFT_NOTE = "\n<i>(программный режим: бот удаляет сообщения)</i>"


class FreezeMiddleware(BaseMiddleware):
    """Работает на КАЖДОЕ сообщение (в т.ч. команды): удаляет сообщения замороженных."""

    async def __call__(self, handler, event: Message, data):
        STATS["seen"] += 1
        u = event.from_user
        logging.info("msg chat=%s from=%s text=%r", event.chat.id, u.id if u else None, (event.text or "")[:40])
        if u:
            k = wkey(event.chat.id, u.id)
            if k in FROZEN:
                until = FROZEN[k]
                if until and until < time.time():
                    FROZEN.pop(k, None)
                    save_frozen()
                else:
                    try:
                        await event.delete()
                        STATS["deleted"] += 1
                    except Exception as e:
                        STATS["delete_errors"] += 1
                        STATS["last_error"] = repr(e)
                        logging.warning("не удалось удалить сообщение: %r", e)
                    return
        return await handler(event, data)


router.message.outer_middleware(FreezeMiddleware())


# ---------- команды ----------
@router.message(Command("start", "help"))
async def cmd_help(message: Message):
    await message.reply(
        "<b>Команды модератора</b> (ответом на сообщение или по ID):\n\n"
        "/freeze 30m [причина] - фриз на время (s/m/h/d/w или с/м/ч/д/н)\n"
        "/unfreeze - снять фриз\n"
        "/mute [время] [причина] - мут (без времени = навсегда)\n"
        "/unmute - снять мут\n"
        "/ban [время] [причина] - бан (без времени = навсегда)\n"
        "/unban - разбанить (по ID)\n"
        "/kick [причина] - выгнать без бана\n"
        "/warn [причина] - предупреждение\n"
        "/unwarn - снять одно предупреждение\n"
        "/warns - сколько предупреждений\n"
        "/id - узнать ID",
        parse_mode="HTML",
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        await message.reply(f"ID: <code>{message.reply_to_message.from_user.id}</code>", parse_mode="HTML")
    else:
        await message.reply(f"Твой ID: <code>{message.from_user.id}</code>\nID чата: <code>{message.chat.id}</code>", parse_mode="HTML")


@router.message(Command("freeze", "fz"))
async def cmd_freeze(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    td = parse_duration(rest[0]) if rest else None
    if not td:
        await message.reply("Укажи время: /freeze 30m, /freeze 2h, /freeze 1d")
        return
    reason = " ".join(rest[1:]) or "не указана"
    try:
        mode = await restrict(bot, message.chat.id, uid, td)
    except Exception as e:
        await message.reply(f"Не вышло: {e}")
        return
    await message.reply(
        f"🧊 {mention(uid, name)} заморожен на <b>{human(td)}</b>\nПричина: {reason}"
        + (SOFT_NOTE if mode == "soft" else ""),
        parse_mode="HTML",
    )


@router.message(Command("unfreeze", "unmute"))
async def cmd_unfreeze(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, _ = t
    FROZEN.pop(wkey(message.chat.id, uid), None)
    save_frozen()
    try:
        await bot.restrict_chat_member(message.chat.id, uid, permissions=FULL_RIGHTS)
    except Exception as e:
        if not not_supported(e):
            await message.reply(f"Не вышло: {e}")
            return
    await message.reply(f"✅ С {mention(uid, name)} сняты ограничения", parse_mode="HTML")


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    td = parse_duration(rest[0]) if rest else None
    reason_parts = rest[1:] if td else rest
    reason = " ".join(reason_parts) or "не указана"
    try:
        mode = await restrict(bot, message.chat.id, uid, td)
    except Exception as e:
        await message.reply(f"Не вышло: {e}")
        return
    срок = human(td) if td else "навсегда"
    await message.reply(
        f"🔇 {mention(uid, name)} в муте: <b>{срок}</b>\nПричина: {reason}"
        + (SOFT_NOTE if mode == "soft" else ""),
        parse_mode="HTML",
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    td = parse_duration(rest[0]) if rest else None
    reason_parts = rest[1:] if td else rest
    reason = " ".join(reason_parts) or "не указана"
    until = datetime.now(timezone.utc) + td if td else None
    try:
        await bot.ban_chat_member(message.chat.id, uid, until_date=until)
        soft = False
    except Exception as e:
        if not_supported(e):
            soft_freeze(message.chat.id, uid, until)
            soft = True
        else:
            await message.reply(f"Не вышло: {e}")
            return
    срок = human(td) if td else "навсегда"
    await message.reply(
        f"⛔ {mention(uid, name)} забанен: <b>{срок}</b>\nПричина: {reason}"
        + ("\n<i>banChatMember нет в приватке: бот просто удаляет все сообщения этого человека</i>" if soft else ""),
        parse_mode="HTML",
    )


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    args = (command.args or "").split()
    if message.reply_to_message and message.reply_to_message.from_user:
        uid = message.reply_to_message.from_user.id
    elif args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
    else:
        await message.reply("Укажи ID: /unban 123456789")
        return
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
    except Exception as e:
        await message.reply(f"Не вышло: {e}")
        return
    await message.reply(f"✅ <code>{uid}</code> разбанен", parse_mode="HTML")


@router.message(Command("kick"))
async def cmd_kick(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    reason = " ".join(rest) or "не указана"
    try:
        await bot.ban_chat_member(message.chat.id, uid)
        await bot.unban_chat_member(message.chat.id, uid)
    except Exception as e:
        if not_supported(e):
            await message.reply("В этой приватке нет banChatMember, кик невозможен.")
        else:
            await message.reply(f"Не вышло: {e}")
        return
    await message.reply(
        f"👢 {mention(uid, name)} выгнан\nПричина: {reason}", parse_mode="HTML"
    )


@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    reason = " ".join(rest) or "не указана"
    k = wkey(message.chat.id, uid)
    WARNS[k] = WARNS.get(k, 0) + 1
    count = WARNS[k]
    text = (
        f"⚠️ {mention(uid, name)} получил предупреждение "
        f"<b>{count}/{WARN_LIMIT}</b>\nПричина: {reason}"
    )
    if count >= WARN_LIMIT:
        WARNS[k] = 0
        td = parse_duration(WARN_FREEZE)
        try:
            await restrict(bot, message.chat.id, uid, td)
            text += f"\n\n🧊 Лимит варнов - фриз на {human(td)}"
        except Exception as e:
            text += f"\n\nНе удалось заморозить: {e}"
    save_warns()
    await message.reply(text, parse_mode="HTML")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, _ = t
    k = wkey(message.chat.id, uid)
    WARNS[k] = max(0, WARNS.get(k, 0) - 1)
    save_warns()
    await message.reply(
        f"✅ У {mention(uid, name)} теперь {WARNS[k]}/{WARN_LIMIT} предупреждений",
        parse_mode="HTML",
    )


@router.message(Command("warns"))
async def cmd_warns(message: Message, command: CommandObject, bot: Bot):
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        uid, name = u.id, u.full_name
    else:
        uid, name = message.from_user.id, message.from_user.full_name
    count = WARNS.get(wkey(message.chat.id, uid), 0)
    await message.reply(
        f"У {mention(uid, name)}: {count}/{WARN_LIMIT} предупреждений",
        parse_mode="HTML",
    )


@router.message(Command("apicheck"))
async def cmd_apicheck(message: Message, bot: Bot):
    """Показывает, какие методы Bot API поддерживает приватка."""
    if not await check_access(message, bot):
        return
    import aiohttp

    base = API_BASE.rstrip("/") if API_BASE else "https://api.telegram.org"
    methods = [
        "getChatMember", "getChatAdministrators", "restrictChatMember",
        "setChatPermissions", "banChatMember", "unbanChatMember",
        "kickChatMember", "deleteMessage", "promoteChatMember",
    ]
    lines = []
    async with aiohttp.ClientSession() as s:
        for m in methods:
            try:
                async with s.post(
                    f"{base}/bot{TOKEN}/{m}", json={},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data = await r.json(content_type=None)
                desc = str(data.get("description", "")).upper()
                ok = r.status != 404 and "METHOD_NOT_FOUND" not in desc
            except Exception:
                ok = False
            lines.append(("✅ " if ok else "❌ ") + m)
    # живой тест: реально ли бот может удалять сообщения
    try:
        probe = await bot.send_message(message.chat.id, "тест удаления...")
        await probe.delete()
        lines.append("\n✅ живой тест: отправил и удалил сообщение")
    except Exception as e:
        lines.append(f"\n❌ живой тест удаления: {e!r}")
    lines.append(
        f"\nвидел сообщений: {STATS['seen']}, удалил: {STATS['deleted']}, "
        f"ошибок удаления: {STATS['delete_errors']}\nпоследняя ошибка: {STATS['last_error']}\n"
        f"заморожено: {len(FROZEN)}"
    )
    await message.reply("\n".join(lines))


# ---------- веб-сервер для Render / UptimeRobot ----------
async def start_web():
    from aiohttp import web

    async def ok(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()


async def keep_alive():
    """Сам себя пингует каждые 10 минут, чтобы бесплатный Render не засыпал."""
    import aiohttp

    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url, timeout=aiohttp.ClientTimeout(total=20))
        except Exception:
            pass


# ---------- запуск ----------
async def main():
    await start_web()
    asyncio.create_task(keep_alive())
    if API_BASE:
        session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
        bot = Bot(TOKEN, session=session)
    else:
        bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
