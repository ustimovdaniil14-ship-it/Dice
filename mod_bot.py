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
from aiogram.exceptions import ClientDecodeError, TelegramNotFound
from aiogram.methods import GetUpdates
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message, Update, User

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN", "1780243245:OgkJlwaJOao3C1gi41Jt2rfO8jpvFM26u1V")
# Для приватки: адрес её Bot API, например https://bot.example.lol
# Если обычный Telegram - оставь пустым.
API_BASE = os.getenv("API_BASE", "http://31.77.10.54:18081").strip().rstrip("/")
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


RULES_FILE = "rules.json"
try:
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        RULES: dict = json.load(f)
except Exception:
    RULES = {}

# Имена людей, которым выдавали варны (для /warnsall)
NAMES_FILE = "warn_names.json"
try:
    with open(NAMES_FILE, "r", encoding="utf-8") as f:
        WARN_NAMES: dict = json.load(f)
except Exception:
    WARN_NAMES = {}


def save_names():
    try:
        with open(NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(WARN_NAMES, f, ensure_ascii=False)
    except Exception as e:
        logging.warning("не удалось сохранить warn_names.json: %r", e)


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


# Кому слать ошибки и отладку в личку (он должен один раз написать боту /start в ЛС)
OWNER_ID = int(os.getenv("OWNER_ID", "1780243219"))
# Показывать подробности ошибок в самом чате (1) или только в ЛС владельцу (0)
REPORTS_IN_CHAT = os.getenv("REPORTS_IN_CHAT", "1") == "1"


async def report(bot: Bot, text: str):
    """Отправляет техническую информацию владельцу в ЛС, а не в чат."""
    logging.warning("REPORT: %s", text)
    if not OWNER_ID:
        return
    try:
        await bot.send_message(OWNER_ID, text[:4000])
    except Exception as e:
        logging.warning("не удалось отправить отчёт в ЛС: %r", e)


async def fail(message: Message, bot: Bot, e: Exception):
    if REPORTS_IN_CHAT:
        await message.reply(f"Не вышло: {e}")
    else:
        await message.reply("Не вышло, подробности отправил админу в ЛС.")
    await report(bot, f"Ошибка в чате {message.chat.id}, команда {message.text!r}:\n{e!r}")


# Админы бота по чатам: {"<chat_id>": [user_id, ...]}. Меняются командами /addadmin, /deladmin.
ADMINS_FILE = "admins.json"
try:
    with open(ADMINS_FILE, "r", encoding="utf-8") as f:
        CHAT_ADMINS: dict = json.load(f)
except Exception:
    CHAT_ADMINS = {}


def save_admins():
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(CHAT_ADMINS, f)
    except Exception as e:
        logging.warning("не удалось сохранить admins.json: %r", e)


def is_super(user_id: int) -> bool:
    """Главные админы (ADMIN_IDS / OWNER_ID): могут назначать админов в любом чате."""
    return user_id in ADMIN_IDS or user_id == OWNER_ID


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS or user_id in CHAT_ADMINS.get(str(chat_id), []):
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


# Кэш авторов сообщений: приватка может не передавать from_user в reply_to_message
from collections import OrderedDict, deque

MSG_AUTHORS: "OrderedDict[str, tuple]" = OrderedDict()


CHAT_MSGS: dict = {}  # chat_id -> последние message_id (для /purge)
LOCKED: set = set()   # чаты в режиме /lock


def remember_author(message: Message):
    CHAT_MSGS.setdefault(message.chat.id, deque(maxlen=500)).append(message.message_id)
    if message.from_user:
        MSG_AUTHORS[f"{message.chat.id}:{message.message_id}"] = (
            message.from_user.id, message.from_user.full_name,
        )
        while len(MSG_AUTHORS) > 20000:
            MSG_AUTHORS.popitem(last=False)


def reply_author(message: Message):
    """(user_id, name) автора сообщения, на которое ответили, или None."""
    r = message.reply_to_message
    rid = None
    if r is not None:
        if r.from_user:
            return r.from_user.id, r.from_user.full_name
        rid = r.message_id
    else:
        try:
            rid = message.model_dump(exclude_none=True).get("reply_to_message_id")
        except Exception:
            rid = None
    if rid:
        return MSG_AUTHORS.get(f"{message.chat.id}:{rid}")
    return None


def reply_debug(message: Message) -> str:
    try:
        return str(message.model_dump(exclude_none=True))[:700]
    except Exception as e:
        return repr(e)


async def get_target(message: Message, command: CommandObject, bot: Bot):
    """Возвращает (user_id, name, остаток_аргументов) или None."""
    args = (command.args or "").split()
    ra = reply_author(message)
    if ra:
        uid, name = ra
        rest = args
    elif args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
        name = str(uid)
        rest = args[1:]
    else:
        text = "Ответь на сообщение человека или укажи его ID."
        if message.reply_to_message is not None:
            text = ("Не вижу автора этого сообщения (бот его не запомнил). "
                    "Ответь на более свежее сообщение человека или укажи его ID.")
            if REPORTS_IN_CHAT:
                text += "\n\nОтладка:\n" + reply_debug(message)
            await report(bot, "Нет автора в reply:\n" + reply_debug(message))
        await message.reply(text)
        return None

    me = await bot.me()
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
        remember_author(event)
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
        if event.chat.id in LOCKED and u:
            bot = data.get("bot")
            if bot is not None and not await is_admin(bot, event.chat.id, u.id):
                try:
                    await event.delete()
                except Exception as e:
                    logging.warning("lock: не удалось удалить: %r", e)
                return
        return await handler(event, data)


router.message.outer_middleware(FreezeMiddleware())


UPDATES = {}  # тип обновления -> сколько пришло


class UpdateLogger(BaseMiddleware):
    async def __call__(self, handler, event, data):
        t = event.event_type if hasattr(event, "event_type") else type(event).__name__
        UPDATES[t] = UPDATES.get(t, 0) + 1
        logging.info("UPDATE type=%s raw=%s", t, str(event.model_dump(exclude_none=True))[:300])
        return await handler(event, data)


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
        "/warnsall - у кого сколько предупреждений в чате\n"
        "/frozenlist - кто сейчас заморожен\n"
        "/clearwarns - сбросить варны человеку\n"
        "/warnsreset - сбросить варны у всех (главные админы)\n"
        "/info - сводка по человеку\n"
        "/del - удалить сообщение (ответом)\n"
        "/purge N - удалить последние N сообщений\n"
        "/lock, /unlock - закрыть/открыть чат для не-админов\n"
        "/say текст - написать от имени бота\n"
        "/rules, /setrules - правила чата\n"
        "/report - пожаловаться на сообщение (для всех)\n"
        "/id - узнать ID\n"
        "/addadmin - назначить админа бота в этом чате (только главные админы)\n"
        "/deladmin - снять админа бота\n"
        "/admins - список админов",
        parse_mode="HTML",
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    ra = reply_author(message)
    if ra:
        await message.reply(f"ID: <code>{ra[0]}</code>", parse_mode="HTML")
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
        await fail(message, bot, e)
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
            await fail(message, bot, e)
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
        await fail(message, bot, e)
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
            await fail(message, bot, e)
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
    ra = reply_author(message)
    if ra:
        uid = ra[0]
    elif args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
    else:
        await message.reply("Укажи ID: /unban 123456789")
        return
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
    except Exception as e:
        await fail(message, bot, e)
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
            await fail(message, bot, e)
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
    if name and name != str(uid):
        WARN_NAMES[k] = name
        save_names()
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
            text += (f"\n\nНе удалось заморозить: {e}" if REPORTS_IN_CHAT
                     else "\n\nНе удалось заморозить (подробности отправил админу)")
            await report(bot, f"Автофриз после варнов не удался:\n{e!r}")
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
    ra = reply_author(message)
    if ra:
        uid, name = ra
    else:
        uid, name = message.from_user.id, message.from_user.full_name
    count = WARNS.get(wkey(message.chat.id, uid), 0)
    await message.reply(
        f"У {mention(uid, name)}: {count}/{WARN_LIMIT} предупреждений",
        parse_mode="HTML",
    )


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject, bot: Bot):
    """Назначить админа бота в ЭТОМ чате (ответом на сообщение или по ID). Только для главных админов."""
    if message.chat.type == "private":
        await message.reply("Эта команда работает только в чате.")
        return
    if not message.from_user or not is_super(message.from_user.id):
        await message.reply("Назначать админов могут только главные админы бота.")
        return
    args = (command.args or "").split()
    ra = reply_author(message)
    if ra:
        uid, name = ra
    elif args and args[0].lstrip("-").isdigit():
        uid, name = int(args[0]), args[0]
    else:
        await message.reply("Ответь на сообщение человека или укажи ID: /addadmin 123456789")
        return
    k = str(message.chat.id)
    lst = CHAT_ADMINS.setdefault(k, [])
    if uid in lst or uid in ADMIN_IDS:
        await message.reply("Он уже админ бота.")
        return
    lst.append(uid)
    save_admins()
    await message.reply(f"✅ {mention(uid, name)} теперь админ бота в этом чате", parse_mode="HTML")


@router.message(Command("deladmin"))
async def cmd_deladmin(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type == "private":
        await message.reply("Эта команда работает только в чате.")
        return
    if not message.from_user or not is_super(message.from_user.id):
        await message.reply("Снимать админов могут только главные админы бота.")
        return
    args = (command.args or "").split()
    ra = reply_author(message)
    if ra:
        uid, name = ra
    elif args and args[0].lstrip("-").isdigit():
        uid, name = int(args[0]), args[0]
    else:
        await message.reply("Ответь на сообщение человека или укажи ID: /deladmin 123456789")
        return
    lst = CHAT_ADMINS.get(str(message.chat.id), [])
    if uid not in lst:
        await message.reply("Его нет в списке админов этого чата (главных админов отсюда не снять).")
        return
    lst.remove(uid)
    save_admins()
    await message.reply(f"✅ {mention(uid, name)} больше не админ бота", parse_mode="HTML")


@router.message(Command("admins"))
async def cmd_admins(message: Message, bot: Bot):
    if not message.from_user or not (
        is_super(message.from_user.id) or await is_admin(bot, message.chat.id, message.from_user.id)
    ):
        await message.reply("Только для админов.")
        return
    chat_list = CHAT_ADMINS.get(str(message.chat.id), [])
    text = "Главные админы: " + (", ".join(f"<code>{i}</code>" for i in sorted(ADMIN_IDS)) or "нет")
    text += "\nАдмины этого чата: " + (", ".join(f"<code>{i}</code>" for i in chat_list) or "нет")
    await message.reply(text, parse_mode="HTML")


@router.message(Command("warnsall"))
async def cmd_warnsall(message: Message, bot: Bot):
    """Список всех, у кого есть предупреждения в этом чате."""
    if not await check_access(message, bot):
        return
    prefix = f"{message.chat.id}:"
    rows = []
    for k, n in WARNS.items():
        if k.startswith(prefix) and n > 0:
            uid = int(k[len(prefix):])
            rows.append((n, uid, WARN_NAMES.get(k) or str(uid)))
    if not rows:
        await message.reply("В этом чате ни у кого нет предупреждений.")
        return
    rows.sort(reverse=True)
    lines = [f"{i}. {mention(uid, name)} (<code>{uid}</code>): <b>{n}/{WARN_LIMIT}</b>"
             for i, (n, uid, name) in enumerate(rows[:50], 1)]
    text = f"⚠️ Предупреждения в этом чате ({len(rows)} чел.):\n\n" + "\n".join(lines)
    if len(rows) > 50:
        text += f"\n\n...и ещё {len(rows) - 50}"
    await message.reply(text[:4000], parse_mode="HTML")


@router.message(Command("frozenlist", "frozen"))
async def cmd_frozenlist(message: Message, bot: Bot):
    """Кто сейчас заморожен/в муте программно в этом чате."""
    if not await check_access(message, bot):
        return
    prefix = f"{message.chat.id}:"
    now = time.time()
    lines = []
    for k, until in list(FROZEN.items()):
        if not k.startswith(prefix):
            continue
        if until and until < now:
            continue
        uid = int(k[len(prefix):])
        left = "навсегда" if not until else human(timedelta(seconds=until - now))
        lines.append(f"• {mention(uid, WARN_NAMES.get(k) or str(uid))} (<code>{uid}</code>): {left}")
    if not lines:
        await message.reply("Сейчас никто не заморожен.")
        return
    await message.reply("🧊 Заморожены (программно):\n\n" + "\n".join(lines)[:4000], parse_mode="HTML")


@router.message(Command("clearwarns"))
async def cmd_clearwarns(message: Message, command: CommandObject, bot: Bot):
    """Сбросить все предупреждения человеку."""
    if not await check_access(message, bot):
        return
    args = (command.args or "").split()
    ra = reply_author(message)
    if ra:
        uid, name = ra
    elif args and args[0].lstrip("-").isdigit():
        uid, name = int(args[0]), args[0]
    else:
        await message.reply("Ответь на сообщение человека или укажи ID.")
        return
    WARNS[wkey(message.chat.id, uid)] = 0
    save_warns()
    await message.reply(f"✅ У {mention(uid, name)} сброшены все предупреждения", parse_mode="HTML")


@router.message(Command("warnsreset"))
async def cmd_warnsreset(message: Message, bot: Bot):
    """Обнулить предупреждения у ВСЕХ в этом чате (только главные админы)."""
    if message.chat.type == "private" or not message.from_user or not is_super(message.from_user.id):
        await message.reply("Только для главных админов, в чате.")
        return
    prefix = f"{message.chat.id}:"
    n = 0
    for k in list(WARNS):
        if k.startswith(prefix) and WARNS[k] > 0:
            WARNS[k] = 0
            n += 1
    save_warns()
    await message.reply(f"✅ Предупреждения сброшены у {n} чел.")


@router.message(Command("info"))
async def cmd_info(message: Message, command: CommandObject, bot: Bot):
    """Сводка по человеку: ID, варны, фриз, статус админа."""
    if not await check_access(message, bot):
        return
    args = (command.args or "").split()
    ra = reply_author(message)
    if ra:
        uid, name = ra
    elif args and args[0].lstrip("-").isdigit():
        uid, name = int(args[0]), args[0]
    else:
        uid, name = message.from_user.id, message.from_user.full_name
    k = wkey(message.chat.id, uid)
    until = FROZEN.get(k)
    if k in FROZEN and (not until or until > time.time()):
        fr = "навсегда" if not until else "ещё " + human(timedelta(seconds=until - time.time()))
    else:
        fr = "нет"
    adm = "да" if await is_admin(bot, message.chat.id, uid) else "нет"
    await message.reply(
        f"👤 {mention(uid, name)}\nID: <code>{uid}</code>\n"
        f"Предупреждения: {WARNS.get(k, 0)}/{WARN_LIMIT}\nЗаморожен: {fr}\nАдмин бота: {adm}",
        parse_mode="HTML",
    )


@router.message(Command("del"))
async def cmd_del(message: Message, bot: Bot):
    """Удалить сообщение, на которое ответили (и саму команду)."""
    if not await check_access(message, bot):
        return
    r = message.reply_to_message
    if not r:
        await message.reply("Ответь командой /del на сообщение, которое нужно удалить.")
        return
    try:
        await bot.delete_message(message.chat.id, r.message_id)
    except Exception as e:
        await fail(message, bot, e)
        return
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("purge"))
async def cmd_purge(message: Message, command: CommandObject, bot: Bot):
    """Удалить последние N сообщений (из тех, что бот видел). /purge 20"""
    if not await check_access(message, bot):
        return
    a = (command.args or "").split()
    n = int(a[0]) if a and a[0].isdigit() else 0
    if not 1 <= n <= 100:
        await message.reply("Укажи число от 1 до 100: /purge 20")
        return
    ids = list(CHAT_MSGS.get(message.chat.id, []))[-(n + 1):]  # + сама команда
    ok = bad = 0
    last_err = None
    for mid in reversed(ids):
        try:
            await bot.delete_message(message.chat.id, mid)
            ok += 1
        except Exception as e:
            bad += 1
            last_err = e
    if ok == 0 and last_err is not None:
        await fail(message, bot, last_err)
        return
    note = f"🧹 Удалено: {ok}" + (f", не удалось: {bad}" if bad else "")
    await bot.send_message(message.chat.id, note)


@router.message(Command("lock"))
async def cmd_lock(message: Message, bot: Bot):
    """Режим тишины: бот удаляет сообщения всех не-админов."""
    if not await check_access(message, bot):
        return
    LOCKED.add(message.chat.id)
    await message.reply("🔒 Чат закрыт: сообщения не-админов удаляются. Открыть: /unlock")


@router.message(Command("unlock"))
async def cmd_unlock(message: Message, bot: Bot):
    if not await check_access(message, bot):
        return
    LOCKED.discard(message.chat.id)
    await message.reply("🔓 Чат открыт")


@router.message(Command("say"))
async def cmd_say(message: Message, command: CommandObject, bot: Bot):
    """Бот пишет от своего имени. /say текст"""
    if not await check_access(message, bot):
        return
    if not command.args:
        await message.reply("Напиши текст: /say всем привет")
        return
    await bot.send_message(message.chat.id, command.args)
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    text = RULES.get(str(message.chat.id))
    await message.reply(text if text else "Правила для этого чата не заданы. Админ: /setrules текст")


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    if not command.args:
        await message.reply("Напиши текст правил: /setrules 1. Без спама ...")
        return
    RULES[str(message.chat.id)] = command.args
    try:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(RULES, f, ensure_ascii=False)
    except Exception as e:
        logging.warning("не удалось сохранить rules.json: %r", e)
    await message.reply("✅ Правила сохранены. Показать: /rules")


@router.message(Command("report"))
async def cmd_report(message: Message, bot: Bot):
    """Любой участник жалуется на сообщение (ответом): уйдёт админу в ЛС."""
    r = message.reply_to_message
    if not r:
        await message.reply("Ответь командой /report на сообщение-нарушение.")
        return
    ra = reply_author(message)
    who = f"{ra[1]} ({ra[0]})" if ra else "неизвестно"
    sender = message.from_user.full_name if message.from_user else "?"
    await report(
        bot,
        f"🚩 Жалоба в чате {message.chat.id} от {sender}\nНа: {who}\n"
        f"Сообщение #{r.message_id}: {(getattr(r, 'text', None) or '')[:300]}",
    )
    await message.reply("✅ Жалоба отправлена админам")


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
    lines.append("\ngetMe: берётся вручную (приватка отдаёт урезанный ответ)")
    lines.append(
        f"\nвидел сообщений: {STATS['seen']}, удалил: {STATS['deleted']}, "
        f"ошибок удаления: {STATS['delete_errors']}\nпоследняя ошибка: {STATS['last_error']}\n"
        f"заморожено: {len(FROZEN)}\nобновления по типам: {UPDATES}"
    )
    await message.reply("\n".join(lines))


@router.message(Command("deltest"))
async def cmd_deltest(message: Message, bot: Bot):
    """Пробует разные форматы deleteMessage и показывает сырой ответ приватки."""
    if not await check_access(message, bot):
        return
    import aiohttp

    base = API_BASE.rstrip("/") if API_BASE else "https://api.telegram.org"
    cid = message.chat.id
    raw_cid = str(cid)
    variants = [
        ("deleteMessage json int", "deleteMessage", lambda m: {"chat_id": cid, "message_id": m}),
        ("deleteMessage json str", "deleteMessage", lambda m: {"chat_id": str(cid), "message_id": str(m)}),
        ("deleteMessages json", "deleteMessages", lambda m: {"chat_id": cid, "message_ids": [m]}),
        ("deleteMessage без -100", "deleteMessage", lambda m: {"chat_id": int(raw_cid.replace("-100", "-", 1)), "message_id": m}),
        ("deleteMessage chat_id>0", "deleteMessage", lambda m: {"chat_id": abs(int(raw_cid[4:] or 0)) if raw_cid.startswith("-100") else abs(cid), "message_id": m}),
    ]
    out = [f"chat_id = {cid}"]
    async with aiohttp.ClientSession() as s:
        for title, method, build in variants:
            probe = await bot.send_message(cid, f"probe: {title}")
            try:
                async with s.post(
                    f"{base}/bot{TOKEN}/{method}", json=build(probe.message_id),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    txt = (await r.text())[:150]
                out.append(f"{r.status} {title}: {txt}")
            except Exception as e:
                out.append(f"ERR {title}: {e!r}")
    out.append("\nКакие probe-сообщения остались в чате - те варианты не сработали.")
    await message.reply("\n".join(out))


@router.message(Command("apiscan"))
async def cmd_apiscan(message: Message, bot: Bot):
    """Перебирает возможные названия методов и показывает существующие + страницы документации."""
    if not await check_access(message, bot):
        return
    import aiohttp

    base = API_BASE.rstrip("/") if API_BASE else "https://api.telegram.org"
    names = """getMe getChat getChatMember getChatMembers getChatMemberCount getChatMembersCount getChatAdministrators
    restrictChatMember restrictMember muteChatMember muteMember mutechatmember setChatMemberPermissions setChatMemberRestrictions
    setChatPermissions banChatMember banMember kickChatMember kickMember removeChatMember removeMember unbanChatMember unbanMember
    banChatSenderChat unbanChatSenderChat promoteChatMember setChatAdministratorCustomTitle
    deleteMessage deleteMessages deleteChatMessages deleteMessagesFrom deleteHistory deleteUserMessages
    leaveChat setChatTitle setChatDescription pinChatMessage unpinChatMessage
    createChatInviteLink exportChatInviteLink approveChatJoinRequest declineChatJoinRequest
    getUpdates setMyCommands getMyCommands getWebhookInfo sendMessage forwardMessage copyMessage
    editMessageText getFile getUserProfilePhotos getChatMenuButton""".split()
    found, missing = [], 0
    async with aiohttp.ClientSession() as s:
        async def probe(n):
            try:
                async with s.post(f"{base}/bot{TOKEN}/{n}", json={}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    t = (await r.text())[:60].replace("\n", " ")
                if r.status == 404 or "METHOD_NOT_FOUND" in t.upper():
                    return None
                return f"✅ {n}: {r.status} {t}"
            except Exception as e:
                return None
        res = await asyncio.gather(*(probe(n) for n in names))
        found = [x for x in res if x]
        docs = []
        for path in ("/", "/docs", "/openapi.json", "/methods", "/help", "/api", "/swagger"):
            try:
                async with s.get(base + path, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        docs.append(f"{path} -> 200 ({r.headers.get('content-type', '?')[:30]})")
            except Exception:
                pass
    text = "Существуют:\n" + "\n".join(found) + "\n\nСтраницы сервера:\n" + ("\n".join(docs) or "нет")
    await message.reply(text[:4000])


@router.error()
async def on_error(event, bot: Bot):
    """Любая необработанная ошибка уходит владельцу в ЛС, в чат ничего не пишем."""
    await report(bot, f"Необработанная ошибка: {event.exception!r}")
    return True


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


async def preload_me(bot: Bot):
    """Новые aiogram требуют от getMe 12 полей, которых приватка не отдаёт.
    Читаем getMe вручную и кладём в кэш бота, чтобы bot.me() не делал свой запрос."""
    import aiohttp

    base = API_BASE or "https://api.telegram.org"
    async with aiohttp.ClientSession() as sess:
        async with sess.post(f"{base}/bot{TOKEN}/getMe", timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json(content_type=None)
    res = data["result"]
    bot._me = User(
        id=res["id"],
        is_bot=res.get("is_bot", True),
        first_name=res.get("first_name", "bot"),
        username=res.get("username"),
    )
    logging.info("getMe ok: @%s (%s)", res.get("username"), res["id"])


# ---------- устойчивая сессия ----------
# Приватка присылает не все поля, которые требует свежий aiogram (например, у опроса
# нет members_only). Из-за одного такого сообщения getUpdates падал в вечный повтор
# и бот переставал отвечать. Теперь "плохие" апдейты упрощаются, а не ломают всю пачку.
SAFE_MSG_KEYS = {
    "message_id", "date", "chat", "from", "sender_chat", "text", "entities",
    "reply_to_message", "edit_date", "caption", "caption_entities", "message_thread_id",
}
MSG_UPDATE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")


def reduce_update(item: dict) -> dict:
    out = {"update_id": item.get("update_id")}
    for k in MSG_UPDATE_KEYS:
        if isinstance(item.get(k), dict):
            m = {kk: vv for kk, vv in item[k].items() if kk in SAFE_MSG_KEYS}
            try:
                Update.model_validate({"update_id": out["update_id"], k: m})
                out[k] = m
            except Exception:
                pass
    return out


class TolerantSession(AiohttpSession):
    def check_response(self, bot, method, status_code, content):
        try:
            return super().check_response(bot=bot, method=method, status_code=status_code, content=content)
        except ClientDecodeError:
            if not isinstance(method, GetUpdates):
                raise
            data = json.loads(content)
            fixed = []
            for item in data.get("result") or []:
                try:
                    Update.model_validate(item)
                    fixed.append(item)
                except Exception as e:
                    logging.warning("апдейт %s упрощён: %s", item.get("update_id"), str(e)[:150])
                    fixed.append(reduce_update(item))
            data["result"] = fixed
            return super().check_response(
                bot=bot, method=method, status_code=status_code, content=json.dumps(data)
            )


# ---------- запуск ----------
async def main():
    await start_web()
    asyncio.create_task(keep_alive())
    if API_BASE:
        session = TolerantSession(api=TelegramAPIServer.from_base(API_BASE))
        bot = Bot(TOKEN, session=session)
    else:
        bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.update.outer_middleware(UpdateLogger())
    await preload_me(bot)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook не удался (продолжаю): %r", e)
    logging.info("API_BASE=%s", API_BASE or "https://api.telegram.org")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "edited_message", "channel_post", "my_chat_member", "chat_member", "callback_query"],
    )


if __name__ == "__main__":
    asyncio.run(main())
