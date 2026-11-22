import asyncio
import html
import json
import logging
import os
import re
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from collections import OrderedDict

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatPermissions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

# ======================= НАСТРОЙКИ =======================
TOKEN = os.getenv("BOT_TOKEN", "1780243245:OgkJlwaJOao3C1gi41Jt2rfO8jpvFM26u1V")
API_BASE = os.getenv("API_BASE", "http://botapi-31-77-10-54.nip.io")
DATABASE_URL = os.getenv("DATABASE_URL", "")  # Neon: чтобы настройки не пропадали


def parse_ids(s: str) -> set:
    return {int(x) for x in s.replace(" ", "").split(",") if x.lstrip("-").isdigit()}


# Владелец: видит панель в личке бота, имеет все права везде
OWNER_IDS = parse_ids(os.getenv("OWNER_IDS", "1780243219"))
# Модераторы во ВСЕХ чатах (задано в коде). Остальных добавляй через панель.
ADMIN_IDS = parse_ids(os.getenv("ADMIN_IDS", "1780243206"))
ALLOW_CHAT_ADMINS = True      # админы чата тоже могут пользоваться ботом
WARN_LIMIT = 3                # варнов до авто-фриза
WARN_FREEZE = "24h"           # на сколько авто-фриз
REPORT_COOLDOWN = 30          # сек между жалобами от одного человека
DATA_FILE = "data.json"       # если DATABASE_URL не задан

router = Router()
esc = html.escape


# ======================= ХРАНИЛИЩЕ =======================
class Store:
    def __init__(self):
        self.data = {"mods": {}, "global": [], "warns": {}, "expiry": {}}
        self.db_ok = True

    async def _get(self, key):
        if DATABASE_URL:
            import asyncpg

            conn = await asyncpg.connect(DATABASE_URL.split("?")[0], ssl="require")
            try:
                await conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
                return await conn.fetchval("SELECT v FROM kv WHERE k=$1", key)
            finally:
                await conn.close()
        path = f"{key}.json"
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        return None

    async def _put(self, key, raw):
        if DATABASE_URL:
            import asyncpg

            conn = await asyncpg.connect(DATABASE_URL.split("?")[0], ssl="require")
            try:
                await conn.execute(
                    "INSERT INTO kv (k, v) VALUES ($1, $2) "
                    "ON CONFLICT (k) DO UPDATE SET v = $2",
                    key, raw,
                )
            finally:
                await conn.close()
        else:
            with open(f"{key}.json", "w", encoding="utf-8") as f:
                f.write(raw)

    async def load(self):
        try:
            raw = await self._get("data")
        except Exception as e:
            self.db_ok = False
            logging.error("Не удалось загрузить настройки: %r", e)
            return
        if raw:
            try:
                self.data.update(json.loads(raw))
            except Exception as e:
                logging.error("Битые данные: %r", e)

    async def save(self):
        if not self.db_ok:
            return
        try:
            await self._put("data", json.dumps(self.data, ensure_ascii=False))
        except Exception as e:
            logging.error("Не удалось сохранить настройки: %r", e)


store = Store()


# ======================= УТИЛИТЫ =======================
UNITS = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600,
         "d": 86400, "д": 86400, "w": 604800, "н": 604800}


_TOKEN = re.compile(r"(\d+)\s*([a-zа-я]+)")


def parse_duration(text: str):
    """45m, 3h, 2d, 1d12h, 1ч30м, 2нед, 1мес -> timedelta или None."""
    t = text.strip().lower()
    parts = _TOKEN.findall(t)
    if not parts or _TOKEN.sub("", t).strip():
        return None
    total = 0
    for n, u in parts:
        if u.startswith(("мес", "mo")):
            sec = 30 * 86400
        elif u[0] in UNITS:
            sec = UNITS[u[0]]
        else:
            return None
        total += int(n) * sec
    return timedelta(seconds=total) if total > 0 else None


def human(td) -> str:
    if not td:
        return "навсегда"
    s = int(td.total_seconds())
    parts = []
    for name, sec in (("д", 86400), ("ч", 3600), ("мин", 60), ("сек", 1)):
        if s >= sec:
            parts.append(f"{s // sec} {name}")
            s %= sec
    return " ".join(parts) or "0 сек"


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


def wkey(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"


NO_RIGHTS = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False,
    can_send_other_messages=False, can_add_web_page_previews=False,
)
FULL_RIGHTS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
)


# ======================= ПРАВА =======================
async def is_chat_admin(bot: Bot, chat_id: int, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
        status = str(getattr(m.status, "value", m.status)).lower()
        return status in ("administrator", "creator", "owner")
    except Exception as e:
        logging.warning("get_chat_member failed for %s: %r", uid, e)
        return False


def listed(chat_id: int, uid: int) -> bool:
    return (
        uid in OWNER_IDS
        or uid in ADMIN_IDS
        or uid in store.data["global"]
        or uid in store.data["mods"].get(str(chat_id), [])
    )


async def is_mod(bot: Bot, chat_id: int, uid: int) -> bool:
    if listed(chat_id, uid):
        return True
    return ALLOW_CHAT_ADMINS and await is_chat_admin(bot, chat_id, uid)


async def protected(bot: Bot, chat_id: int, uid: int) -> bool:
    """Тех, на кого нельзя применять наказания."""
    return listed(chat_id, uid) or await is_chat_admin(bot, chat_id, uid)


async def check_access(message: Message, bot: Bot) -> bool:
    if message.chat.type == "private":
        await message.reply("Эта команда работает только в чате.")
        return False
    if not await is_mod(bot, message.chat.id, message.from_user.id):
        await message.reply(f"Только для модераторов. Твой ID: <code>{message.from_user.id}</code>")
        return False
    return True


# Приватка в reply_to_message присылает только заглушку без автора,
# поэтому бот сам запоминает, кто написал каждое сообщение (и хранит это в БД).
SEEN: OrderedDict = OrderedDict()
SEEN_MAX = 8000
SEEN_DIRTY = [False]
RECEIVED = [0, 0]  # [всего сообщений боту, из них не команды]


def remember(chat_id, msg_id, uid, name):
    SEEN[(chat_id, msg_id)] = (uid, name)
    if len(SEEN) > SEEN_MAX:
        SEEN.popitem(last=False)
    SEEN_DIRTY[0] = True


class Recorder(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            RECEIVED[0] += 1
            if not (event.text or "").startswith("/"):
                RECEIVED[1] += 1
            if event.from_user and event.chat:
                remember(event.chat.id, event.message_id, event.from_user.id, event.from_user.full_name)
        except Exception:
            pass
        return await handler(event, data)


router.message.outer_middleware(Recorder())


async def load_seen():
    try:
        raw = await store._get("authors")
        for c, m, u, n in json.loads(raw or "[]"):
            SEEN[(c, m)] = (u, n)
    except Exception as e:
        logging.warning("не удалось загрузить авторов: %r", e)


async def seen_flusher():
    """Раз в 30 секунд сохраняет кэш авторов, чтобы он пережил перезапуск."""
    while True:
        await asyncio.sleep(30)
        if not SEEN_DIRTY[0] or not store.db_ok:
            continue
        SEEN_DIRTY[0] = False
        raw = json.dumps([[c, m, u, n] for (c, m), (u, n) in SEEN.items()], ensure_ascii=False)
        try:
            await store._put("authors", raw)
        except Exception as e:
            SEEN_DIRTY[0] = True
            logging.warning("не удалось сохранить авторов: %r", e)


def _fwd_user(fwd):
    u = getattr(fwd, "forward_from", None)
    if not u:
        fo = getattr(fwd, "forward_origin", None)
        u = getattr(fo, "sender_user", None) if fo else None
    if u:
        return u.id, u.full_name
    d = (getattr(fwd, "model_extra", None) or {}).get("forward_from")
    if isinstance(d, dict) and isinstance(d.get("id"), int):
        name = " ".join(x for x in (d.get("first_name"), d.get("last_name")) if x) or str(d["id"])
        return d["id"], name
    return None


async def author_via_forward(bot: Bot, chat_id: int, msg_id: int):
    """Для старых сообщений: пересылаем владельцу, читаем автора, удаляем пересылку."""
    for dest in OWNER_IDS:
        try:
            fwd = await bot.forward_message(chat_id=dest, from_chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logging.warning("поиск автора через пересылку не удался: %r", e)
            continue
        found = _fwd_user(fwd)
        try:
            await bot.delete_message(dest, fwd.message_id)
        except Exception:
            pass
        return found
    return None


async def reply_user(message, bot=None):
    """Автор сообщения, на которое ответили: (id, имя) или None."""
    rt = message.reply_to_message
    if not rt:
        return None
    if rt.from_user:
        return rt.from_user.id, rt.from_user.full_name
    hit = SEEN.get((message.chat.id, rt.message_id))
    if hit:
        return hit
    extra = getattr(rt, "model_extra", None) or {}
    for k in ("from_id", "sender_id", "user_id"):
        v = extra.get(k)
        if isinstance(v, int):
            return v, str(v)
    if bot is not None:
        found = await author_via_forward(bot, message.chat.id, rt.message_id)
        if found:
            remember(message.chat.id, rt.message_id, *found)
            return found
    return None


@router.message(Command("privacy", "status"))
async def cmd_privacy(message: Message, bot: Bot):
    """Диагностика: видит ли бот обычные сообщения в чате или только команды."""
    if not await check_access(message, bot):
        return
    me = await bot.get_me()
    await message.reply(
        f"can_read_all_group_messages: {me.can_read_all_group_messages}\n"
        f"всего получено сообщений: {RECEIVED[0]}\n"
        f"из них НЕ команд: {RECEIVED[1]}\n"
        f"запомнено авторов: {len(SEEN)}\n"
        f"хранилище: {'Neon БД (всё сохраняется)' if DATABASE_URL else 'ФАЙЛ - пропадёт при деплое! Добавь DATABASE_URL'}\n\n"
        "Если «НЕ команд» не растёт, когда в чате пишут обычные сообщения, "
        "бот их не получает (включён режим приватности)."
    )


@router.message(Command("debugreply"))
async def cmd_debugreply(message: Message, bot: Bot):
    """Показывает, что именно приватка присылает боту в reply_to_message."""
    if not await check_access(message, bot):
        return
    rt = message.reply_to_message
    if not rt:
        await message.reply("Ответь этой командой на любое сообщение.")
        return
    raw = rt.model_dump_json(exclude_none=True)
    logging.info("debugreply: %s", raw)
    await message.reply(f"<pre>{esc(raw[:1500])}</pre>")


async def get_target(message: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split()
    ru = await reply_user(message, bot)
    if ru:
        (uid, name), rest = ru, args
    elif args and args[0].lstrip("-").isdigit():
        uid, name, rest = int(args[0]), args[0], args[1:]
    else:
        await message.reply("Ответь на сообщение человека или укажи его ID.")
        return None
    me = await bot.get_me()
    if uid == me.id:
        await message.reply("Себя я трогать не буду 🙂")
        return None
    if await protected(bot, message.chat.id, uid):
        await message.reply("Нельзя применять к модератору или админу.")
        return None
    return uid, name, rest


def split_duration(rest: list):
    td = parse_duration(rest[0]) if rest else None
    reason = " ".join(rest[1:] if td else rest) or "не указана"
    return td, reason


# ======================= ДЕЙСТВИЯ =======================
def track(chat_id, uid, kind, until):
    """Запоминает, когда снять ограничение (на случай, если приватка не снимает сама)."""
    k = f"{kind}:{chat_id}:{uid}"
    if until:
        store.data["expiry"][k] = {
            "chat": chat_id, "uid": uid, "kind": kind, "until": until.timestamp(),
        }
    else:
        store.data["expiry"].pop(k, None)


async def do_restrict(bot, chat_id, uid, td):
    until = datetime.now(timezone.utc) + td if td else None
    await bot.restrict_chat_member(chat_id, uid, permissions=NO_RIGHTS, until_date=until)
    track(chat_id, uid, "restrict", until)
    await store.save()


async def do_ban(bot, chat_id, uid, td):
    until = datetime.now(timezone.utc) + td if td else None
    await bot.ban_chat_member(chat_id, uid, until_date=until)
    track(chat_id, uid, "ban", until)
    await store.save()


async def expiry_worker(bot: Bot):
    """Каждые 15 секунд снимает истёкшие фризы, муты и баны."""
    while True:
        await asyncio.sleep(15)
        now = time.time()
        due = [(k, v) for k, v in list(store.data["expiry"].items()) if v["until"] <= now]
        if not due:
            continue
        for k, v in due:
            try:
                if v["kind"] == "restrict":
                    await bot.restrict_chat_member(v["chat"], v["uid"], permissions=FULL_RIGHTS)
                else:
                    await bot.unban_chat_member(v["chat"], v["uid"], only_if_banned=True)
                store.data["expiry"].pop(k, None)
            except Exception as e:
                logging.warning("не удалось снять ограничение %s: %r", k, e)
                if now - v["until"] > 3600:
                    store.data["expiry"].pop(k, None)
        await store.save()


async def do_kick(bot, chat_id, uid):
    await bot.ban_chat_member(chat_id, uid)
    await bot.unban_chat_member(chat_id, uid)


async def do_warn(bot, chat_id, uid):
    """Возвращает (кол-во варнов, был ли авто-фриз)."""
    warns = store.data["warns"]
    k = wkey(chat_id, uid)
    warns[k] = warns.get(k, 0) + 1
    count, froze = warns[k], False
    if count >= WARN_LIMIT:
        warns[k] = 0
        try:
            await do_restrict(bot, chat_id, uid, parse_duration(WARN_FREEZE))
            froze = True
        except Exception as e:
            logging.warning("авто-фриз не удался: %r", e)
    await store.save()
    return count, froze


# ======================= КОМАНДЫ В ЧАТЕ =======================
HELP = (
    "<b>Команды модератора</b> (ответом на сообщение или по ID):\n"
    "/freeze 30m [причина] - фриз на время: 45m, 3h, 2d, 1d12h (s/m/h/d/w или с/м/ч/д/н)\n"
    "/unfreeze - снять фриз\n"
    "/mute [время] [причина] - мут (без времени = навсегда)\n"
    "/unmute - снять мут\n"
    "/ban [время] [причина] - бан\n"
    "/unban - разбанить (по ID)\n"
    "/kick [причина] - выгнать без бана\n"
    "/warn [причина], /unwarn, /warns - предупреждения\n"
    "/id - узнать ID\n\n"
    "<b>Для всех:</b> ответь на сообщение нарушителя командой "
    "<code>.реп причина</code> - бот позовёт модераторов."
)


@router.message(Command("start", "panel", "help"))
async def cmd_start(message: Message):
    cmd = (message.text or "").split()[0].lower()
    if message.chat.type == "private" and message.from_user.id in OWNER_IDS and "help" not in cmd:
        await message.answer(PANEL_TEXT, reply_markup=panel_kb())
    else:
        await message.reply(HELP)


@router.message(Command("id"))
async def cmd_id(message: Message, bot: Bot):
    ru = await reply_user(message, bot)
    if ru:
        await message.reply(f"ID: <code>{ru[0]}</code>")
    else:
        await message.reply(
            f"Твой ID: <code>{message.from_user.id}</code>\n"
            f"ID чата: <code>{message.chat.id}</code>"
        )


@router.message(Command("freeze", "fz"))
async def cmd_freeze(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    td, reason = split_duration(rest)
    if not td:
        await message.reply("Укажи время: /freeze 30m, /freeze 2h, /freeze 1d")
        return
    try:
        await do_restrict(bot, message.chat.id, uid, td)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await message.reply(
        f"🧊 {mention(uid, name)} заморожен на <b>{human(td)}</b>\nПричина: {esc(reason)}"
    )


@router.message(Command("unfreeze", "unmute"))
async def cmd_unfreeze(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, _ = t
    track(message.chat.id, uid, "restrict", None)
    try:
        await bot.restrict_chat_member(message.chat.id, uid, permissions=FULL_RIGHTS)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await message.reply(f"✅ С {mention(uid, name)} сняты ограничения")


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    td, reason = split_duration(rest)
    try:
        await do_restrict(bot, message.chat.id, uid, td)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await message.reply(
        f"🔇 {mention(uid, name)} в муте: <b>{human(td)}</b>\nПричина: {esc(reason)}"
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    td, reason = split_duration(rest)
    try:
        await do_ban(bot, message.chat.id, uid, td)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await message.reply(
        f"⛔ {mention(uid, name)} забанен: <b>{human(td)}</b>\nПричина: {esc(reason)}"
    )


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    args = (command.args or "").split()
    ru = await reply_user(message, bot)
    if ru:
        uid = ru[0]
    elif args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
    else:
        await message.reply("Укажи ID: /unban 123456789")
        return
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        track(message.chat.id, uid, "ban", None)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await message.reply(f"✅ <code>{uid}</code> разбанен")


@router.message(Command("kick"))
async def cmd_kick(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    try:
        await do_kick(bot, message.chat.id, uid)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await message.reply(
        f"👢 {mention(uid, name)} выгнан\nПричина: {esc(' '.join(rest) or 'не указана')}"
    )


@router.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, rest = t
    count, froze = await do_warn(bot, message.chat.id, uid)
    text = (
        f"⚠️ {mention(uid, name)} получил предупреждение "
        f"<b>{count}/{WARN_LIMIT}</b>\nПричина: {esc(' '.join(rest) or 'не указана')}"
    )
    if froze:
        text += f"\n\n🧊 Лимит варнов - фриз на {human(parse_duration(WARN_FREEZE))}"
    await message.reply(text)


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject, bot: Bot):
    if not await check_access(message, bot):
        return
    t = await get_target(message, command, bot)
    if not t:
        return
    uid, name, _ = t
    k = wkey(message.chat.id, uid)
    store.data["warns"][k] = max(0, store.data["warns"].get(k, 0) - 1)
    await store.save()
    await message.reply(
        f"✅ У {mention(uid, name)} теперь {store.data['warns'][k]}/{WARN_LIMIT} предупреждений"
    )


@router.message(Command("warns"))
async def cmd_warns(message: Message, bot: Bot):
    ru = await reply_user(message, bot) or (message.from_user.id, message.from_user.full_name)
    count = store.data["warns"].get(wkey(message.chat.id, ru[0]), 0)
    await message.reply(f"У {mention(ru[0], ru[1])}: {count}/{WARN_LIMIT} предупреждений")


@router.message(Command("addmod", "delmod", "mods"))
async def cmd_mods(message: Message, command: CommandObject, bot: Bot):
    """Владелец прямо в чате назначает модераторов (ID чата вводить не надо)."""
    if message.chat.type == "private":
        await message.reply("Команда работает в самом чате.")
        return
    if message.from_user.id not in OWNER_IDS:
        await message.reply("Только для владельца.")
        return
    cmd = (message.text or "").split()[0].lstrip("/").split("@")[0].lower()
    lst = store.data["mods"].setdefault(str(message.chat.id), [])
    if cmd == "mods":
        glob = sorted(set(store.data["global"]) | ADMIN_IDS)
        await message.reply(
            "<b>Модераторы этого чата</b>\n"
            + (", ".join(f"<code>{i}</code>" for i in lst) or "-")
            + "\n<b>Во всех чатах</b>\n"
            + (", ".join(f"<code>{i}</code>" for i in glob) or "-")
            + "\nАдмины чата тоже могут пользоваться ботом."
        )
        return
    ru = await reply_user(message, bot)
    args = (command.args or "").split()
    if ru:
        uid = ru[0]
    elif args and args[0].lstrip("-").isdigit():
        uid = int(args[0])
    else:
        await message.reply("Ответь на сообщение человека или укажи ID: /addmod 123456")
        return
    if cmd == "addmod":
        if uid not in lst:
            lst.append(uid)
        text = f"✅ <code>{uid}</code> теперь модератор этого чата"
    else:
        if uid in lst:
            lst.remove(uid)
        text = f"✅ <code>{uid}</code> больше не модератор этого чата"
    await store.save()
    await message.reply(text)


# ======================= ПАНЕЛЬ ВЛАДЕЛЬЦА (лс бота) =======================
class PanelSt(StatesGroup):
    chat = State()
    user = State()


PANEL_TEXT = (
    "⚙️ <b>Панель модераторов</b>\n\n"
    "Здесь ты решаешь, кто в каком чате может пользоваться ботом.\n"
    "ID чата и человека узнаёшь командой /id в самом чате."
)


def panel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить модератора", callback_data="p:add")
    b.button(text="➖ Убрать модератора", callback_data="p:del")
    b.button(text="📋 Список", callback_data="p:list")
    b.adjust(1)
    return b.as_markup()


def render_list() -> str:
    d = store.data
    owners = ", ".join(f"<code>{i}</code>" for i in sorted(OWNER_IDS)) or "-"
    glob = ", ".join(f"<code>{i}</code>" for i in sorted(set(d["global"]) | ADMIN_IDS)) or "-"
    lines = [
        "<b>Модераторы</b>",
        f"Владельцы: {owners}",
        f"Во всех чатах: {glob}",
    ]
    for chat, ids in d["mods"].items():
        if ids:
            lines.append(f"Чат <code>{chat}</code>: " + ", ".join(f"<code>{i}</code>" for i in ids))
    if ALLOW_CHAT_ADMINS:
        lines.append("\nАдмины самого чата тоже могут пользоваться ботом.")
    return "\n".join(lines)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    PENDING.pop((message.chat.id, message.from_user.id), None)
    await message.answer("Отменено.")


@router.callback_query(F.data.startswith("p:"))
async def on_panel(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in OWNER_IDS:
        await cb.answer("Только для владельца", show_alert=True)
        return
    act = cb.data.split(":")[1]
    if act in ("add", "del"):
        await state.set_state(PanelSt.chat)
        await state.update_data(mode=act)
        await cb.message.answer(
            "Пришли <b>ID чата</b> числом (например -1001234567890).\n"
            "Если человек должен быть модератором во всех чатах, пришли <code>0</code>.\n"
            "Отмена: /cancel"
        )
    elif act == "list":
        await cb.message.answer(render_list())
    await cb.answer()


@router.message(PanelSt.chat, F.chat.type == "private")
async def st_chat(message: Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    t = (message.text or "").strip()
    if not t.lstrip("-").isdigit():
        await message.answer("Нужно число. Пришли ID чата (0 = все чаты) или /cancel")
        return
    await state.update_data(chat=int(t))
    await state.set_state(PanelSt.user)
    await message.answer("Теперь пришли <b>ID пользователя</b>:")


@router.message(PanelSt.user, F.chat.type == "private")
async def st_user(message: Message, state: FSMContext):
    if message.from_user.id not in OWNER_IDS:
        return
    t = (message.text or "").strip()
    if not t.lstrip("-").isdigit():
        await message.answer("Нужно число. Пришли ID пользователя или /cancel")
        return
    data = await state.get_data()
    chat, mode, uid = data["chat"], data["mode"], int(t)
    lst = store.data["global"] if chat == 0 else store.data["mods"].setdefault(str(chat), [])
    where = "во всех чатах" if chat == 0 else f"в чате <code>{chat}</code>"
    if mode == "add":
        if uid not in lst:
            lst.append(uid)
        msg = f"✅ <code>{uid}</code> теперь модератор {where}"
    else:
        if uid in lst:
            lst.remove(uid)
        msg = f"✅ <code>{uid}</code> больше не модератор {where}"
    await store.save()
    await state.clear()
    await message.answer(msg, reply_markup=panel_kb())


# ======================= ЖАЛОБЫ: .реп =======================
REP_RE = re.compile(r"^[.!/](реп|rep|report)(@\w+)?(?:\s+(.*))?$", re.I | re.S)
REPORTS: dict = {}
LAST_REPORT: dict = {}
SEQ = [int(time.time())]


async def collect_pings(bot: Bot, chat_id: int, exclude: set) -> list:
    people = {}
    try:
        for a in await bot.get_chat_administrators(chat_id):
            if not a.user.is_bot:
                people[a.user.id] = a.user.full_name
    except Exception as e:
        logging.warning("get_chat_administrators failed: %r", e)
    ids = (
        set(store.data["global"])
        | set(store.data["mods"].get(str(chat_id), []))
        | ADMIN_IDS
        | OWNER_IDS
    )
    for uid in ids - set(people) - exclude:
        try:
            m = await bot.get_chat_member(chat_id, uid)
            st = str(getattr(m.status, "value", m.status)).lower()
            if st in ("left", "kicked", "banned"):
                continue
            people[uid] = m.user.full_name
        except Exception:
            continue
    return [mention(u, n) for u, n in people.items() if u not in exclude]


def report_kb(rid: str):
    b = InlineKeyboardBuilder()
    b.button(text="⚠️ Варн", callback_data=f"r:{rid}:warn")
    b.button(text="🧊 Фриз", callback_data=f"r:{rid}:freeze")
    b.button(text="🔇 Мут", callback_data=f"r:{rid}:mute")
    b.button(text="⛔ Бан", callback_data=f"r:{rid}:ban")
    b.button(text="👢 Кик", callback_data=f"r:{rid}:kick")
    b.button(text="🗑 Удалить сообщение", callback_data=f"r:{rid}:del")
    b.button(text="❌ Отклонить", callback_data=f"r:{rid}:dismiss")
    b.adjust(3, 2, 1, 1)
    return b.as_markup()


DURATIONS = {
    "freeze": [("10 мин", "10m"), ("1 час", "1h"), ("6 часов", "6h"), ("1 день", "1d"), ("7 дней", "7d")],
    "mute": [("10 мин", "10m"), ("1 час", "1h"), ("1 день", "1d"), ("7 дней", "7d"), ("Навсегда", "0")],
    "ban": [("1 день", "1d"), ("7 дней", "7d"), ("30 дней", "30d"), ("Навсегда", "0")],
}


def duration_kb(rid: str, act: str):
    b = InlineKeyboardBuilder()
    for label, val in DURATIONS[act]:
        b.button(text=label, callback_data=f"r:{rid}:{act}:{val}")
    b.button(text="✏️ Своё время", callback_data=f"r:{rid}:{act}:c")
    b.button(text="◀️ Назад", callback_data=f"r:{rid}:menu")
    b.adjust(3, 3, 1)
    return b.as_markup()


@router.message(lambda m: bool(m.text) and REP_RE.match(m.text) is not None)
async def cmd_report(message: Message, bot: Bot):
    try:
        await _cmd_report(message, bot)
    except Exception as e:
        logging.exception("жалоба не удалась")
        try:
            await message.reply(f"Ошибка жалобы: {esc(str(e))[:300]}")
        except Exception:
            pass


async def _cmd_report(message: Message, bot: Bot):
    if message.chat.type == "private":
        return
    reason = (REP_RE.match(message.text).group(3) or "").strip() or "не указана"
    rt = message.reply_to_message
    ru = await reply_user(message, bot)
    if not ru:
        await message.reply(
            "Не вижу автора этого сообщения (бот помнит только те, что написаны после его запуска). "
            "Ответь на более свежее сообщение нарушителя: <code>.реп причина</code>"
        )
        return
    me = await bot.get_me()
    target, reporter = SimpleNamespace(id=ru[0], full_name=ru[1]), message.from_user
    if target.id in (me.id, reporter.id):
        return
    if await protected(bot, message.chat.id, target.id):
        await message.reply("Нельзя пожаловаться на модератора.")
        return
    now = time.time()
    if now - LAST_REPORT.get(reporter.id, 0) < REPORT_COOLDOWN:
        await message.reply("Подожди немного перед следующей жалобой.")
        return
    LAST_REPORT[reporter.id] = now

    pings = await collect_pings(bot, message.chat.id, {reporter.id, target.id, me.id})
    SEQ[0] += 1
    rid = str(SEQ[0])
    text = (
        f"🚨 <b>Жалоба</b>\n"
        f"От: {mention(reporter.id, reporter.full_name)}\n"
        f"На: {mention(target.id, target.full_name)}\n"
        f"Причина: {esc(reason)}"
    )
    REPORTS[rid] = {
        "chat": message.chat.id, "target": target.id, "tname": target.full_name,
        "msg": rt.message_id, "text": text,
    }
    full = text + ("\n\n🔔 " + ", ".join(pings) if pings else "\n\n(модераторов для вызова не найдено)")
    try:
        await rt.reply(full, reply_markup=report_kb(rid))
    except Exception:
        await message.reply(full, reply_markup=report_kb(rid))
    try:
        await message.delete()
    except Exception:
        pass


PENDING: dict = {}      # (chat_id, mod_id) -> ожидание своего срока
PENDING_TTL = 120


async def close_report(bot: Bot, rep: dict, rid: str, rmsg: int, result: str, who: str):
    REPORTS.pop(rid, None)
    try:
        await bot.edit_message_text(
            f"{rep['text']}\n\n✅ {result}\nРешил: {who}",
            chat_id=rep["chat"], message_id=rmsg, reply_markup=None,
        )
    except Exception as e:
        logging.warning("не удалось обновить жалобу: %r", e)


async def run_timed(bot: Bot, rep: dict, act: str, td):
    chat_id, uid = rep["chat"], rep["target"]
    name = mention(uid, rep["tname"])
    if act == "ban":
        await do_ban(bot, chat_id, uid, td)
        return f"{name} забанен: {human(td)}"
    await do_restrict(bot, chat_id, uid, td)
    if act == "freeze":
        return f"{name} заморожен на {human(td)}"
    return f"{name} в муте: {human(td)}"


@router.callback_query(F.data.startswith("r:"))
async def on_report(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    rid, act = parts[1], parts[2]
    rep = REPORTS.get(rid)
    if not rep:
        await cb.answer("Жалоба устарела", show_alert=True)
        return
    chat_id, uid, tname = rep["chat"], rep["target"], rep["tname"]
    if not await is_mod(bot, chat_id, cb.from_user.id):
        await cb.answer("Только для модераторов", show_alert=True)
        return
    who = mention(cb.from_user.id, cb.from_user.full_name)
    rmsg = cb.message.message_id

    async def finish(result: str):
        await close_report(bot, rep, rid, rmsg, result, who)
        await cb.answer("Готово")

    try:
        if act == "menu":
            await cb.message.edit_reply_markup(reply_markup=report_kb(rid))
            await cb.answer()
        elif act in DURATIONS and len(parts) == 3:
            await cb.message.edit_reply_markup(reply_markup=duration_kb(rid, act))
            await cb.answer("Выбери срок")
        elif act in DURATIONS and parts[3] == "c":
            hint = "" if act == "freeze" else ", <code>0</code> - навсегда"
            prompt = await cb.message.answer(
                f"✏️ {who}, ответным сообщением напиши срок: "
                f"<code>45m</code>, <code>3h</code>, <code>2d</code>, <code>1d12h</code>{hint}.\n"
                f"Жду {PENDING_TTL // 60} мин. Отмена: /cancel"
            )
            PENDING[(chat_id, cb.from_user.id)] = {
                "rid": rid, "act": act, "rmsg": rmsg,
                "until": time.time() + PENDING_TTL, "prompt": prompt.message_id,
            }
            await cb.answer("Напиши срок в чат")
        elif act in DURATIONS:
            td = None if parts[3] == "0" else parse_duration(parts[3])
            await finish(await run_timed(bot, rep, act, td))
        elif act == "warn":
            count, froze = await do_warn(bot, chat_id, uid)
            extra = f", авто-фриз на {human(parse_duration(WARN_FREEZE))}" if froze else ""
            await finish(f"{mention(uid, tname)} получил варн {count}/{WARN_LIMIT}{extra}")
        elif act == "kick":
            await do_kick(bot, chat_id, uid)
            await finish(f"{mention(uid, tname)} выгнан")
        elif act == "del":
            await bot.delete_message(chat_id, rep["msg"])
            await cb.answer("Сообщение удалено")
        elif act == "dismiss":
            await finish("Жалоба отклонена")
        else:
            await cb.answer()
    except Exception as e:
        await cb.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)


def _pending_filter(m: Message) -> bool:
    return (
        bool(m.text)
        and m.from_user is not None
        and (m.chat.id, m.from_user.id) in PENDING
        and not m.text.startswith("/")
        and REP_RE.match(m.text) is None
    )


@router.message(_pending_filter)
async def on_custom_time(message: Message, bot: Bot):
    """Модератор нажал «Своё время» и пишет срок обычным сообщением."""
    key = (message.chat.id, message.from_user.id)
    p = PENDING.get(key)
    if not p or p["until"] < time.time():
        PENDING.pop(key, None)
        return
    t = message.text.strip().lower()
    forever = t in ("0", "навсегда", "forever", "inf")
    td = None if forever else parse_duration(t)
    if not forever and td is None:
        rt = message.reply_to_message
        if rt and rt.message_id == p["prompt"]:
            await message.reply("Не понял срок. Примеры: 45m, 3h, 2d, 1d12h, 1ч30м.")
        return
    act = p["act"]
    if act == "freeze" and forever:
        await message.reply("Для фриза нужен конкретный срок.")
        return
    rep = REPORTS.get(p["rid"])
    PENDING.pop(key, None)
    if not rep:
        await message.reply("Эта жалоба уже закрыта.")
        return
    who = mention(message.from_user.id, message.from_user.full_name)
    try:
        result = await run_timed(bot, rep, act, td)
    except Exception as e:
        await message.reply(f"Не вышло: {esc(str(e))}")
        return
    await close_report(bot, rep, p["rid"], p["rmsg"], result, who)
    await message.reply(f"✅ {result}")


# ======================= ВЕБ-СЕРВЕР / ЗАПУСК =======================
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


async def main():
    await start_web()
    asyncio.create_task(keep_alive())
    await store.load()
    await load_seen()
    asyncio.create_task(seen_flusher())
    props = DefaultBotProperties(parse_mode="HTML")
    if API_BASE:
        session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE))
        bot = Bot(TOKEN, session=session, default=props)
    else:
        bot = Bot(TOKEN, default=props)
    asyncio.create_task(expiry_worker(bot))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
