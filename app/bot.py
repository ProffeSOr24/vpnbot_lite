"""Telegram admin bot."""
import asyncio
import html
import json
import os
import platform
import re
import tempfile
import time
from pathlib import Path

import aiohttp
import psutil
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import changelog, config, report, watch
from .cores import latest_tag, plain_version
from .service import PROTOS, Service, fmt_bytes, uses
from .sub import qr_png

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
GB = 1024 ** 3


class Input(StatesGroup):
    add_user = State()
    days = State()
    limit = State()
    reality_sni = State()
    host = State()


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


BACK = [("⬅️ Меню", "menu")]


def fmt_date(ts: int | None) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "бессрочно"


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}д {h}ч {rem // 60}м" if d else f"{h}ч {rem // 60}м"


def setup(service: Service) -> Router:
    router = Router()
    router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
    router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))
    db = service.db

    async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
        if isinstance(target, CallbackQuery):
            try:
                await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
            await target.answer()
        else:
            await target.answer(text, reply_markup=markup, disable_web_page_preview=True)

    async def apply(target: Message | CallbackQuery) -> bool:
        error = await service.apply()
        if error:
            msg = target.message if isinstance(target, CallbackQuery) else target
            await msg.answer(f"⚠️ Ошибка применения конфигурации:\n<pre>{html.escape(error)}</pre>")
        return error is None

    def dot(core) -> str:
        return "🟢" if core.running else ("⚪️" if not core.wanted else "🔴")

    # ---------- main menu ----------
    def menu_markup() -> InlineKeyboardMarkup:
        return kb([("👥 Пользователи", "users"), ("📊 Сервер", "server")],
                  [("⬆️ Обновления", "updates"), ("⚙️ Настройки", "settings")],
                  [("💾 Бэкап", "backup"), ("📜 Логи", "logs")])

    def menu_text() -> str:
        users = db.users()
        active = sum(1 for u in users if u["enabled"])
        return (f"<b>VPN Bot Lite</b> v{config.VERSION}\n"
                f"Сервер: <code>{html.escape(service.host)}</code>\n"
                f"Пользователи: {active} активных из {len(users)}\n"
                f"Xray: {dot(service.xray)}   AmneziaWG: {dot(service.awg)}")

    @router.message(CommandStart())
    @router.message(Command("menu"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(menu_text(), reply_markup=menu_markup())

    @router.callback_query(F.data == "menu")
    async def menu(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show(cb, menu_text(), menu_markup())

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.", reply_markup=menu_markup())

    # ---------- users ----------
    ICON = {"both": "🔗📡", "vless": "🔗", "awg": "📡"}

    PAGE = 7
    views: dict[int, tuple[str, int]] = {}  # admin -> (filter, page): back from a card returns to the same page

    async def users_list(target: Message | CallbackQuery, only: str | None = None, page: int | None = None) -> None:
        viewer = target.from_user.id if target.from_user else 0
        last_only, last_page = views.get(viewer, ("", 0))
        only = last_only if only is None else only
        page = (last_page if only == last_only else 0) if page is None else page
        everyone = db.users()
        online = {u["id"] for u in everyone if service.presence(u)[0]}
        match = {"": lambda u: True, "online": lambda u: u["id"] in online,
                 "offline": lambda u: u["id"] not in online}
        shown = [u for u in everyone if match.get(only, lambda u: uses(u, only))(u)]
        shown.sort(key=lambda u: (list(ICON).index(u["proto"]), u["name"].lower()))
        pages = max(1, -(-len(shown) // PAGE))
        page = min(max(page, 0), pages - 1)
        views[viewer] = (only, page)
        b = InlineKeyboardBuilder()
        for u in shown[page * PAGE:(page + 1) * PAGE]:
            used = fmt_bytes(u["up"] + u["down"])
            dot = "🟢" if u["id"] in online else "🔴"
            b.button(text=f"{dot}{service.user_status(u)} {ICON[u['proto']]} {u['name']} · {used}",
                     callback_data=f"u:{u['id']}")
        b.adjust(1)
        if pages > 1:
            b.row(*kb([("◀️", f"up:{page - 1}" if page else "noop"), (f"{page + 1} / {pages}", "noop"),
                       ("▶️", f"up:{page + 1}" if page < pages - 1 else "noop")]).inline_keyboard[0])
        mark = lambda key: "• " if only == key else ""  # noqa: E731
        b.row(*kb([(f"{mark('')}Все", "uf:"), (f"{mark('online')}🟢 Онлайн", "uf:online"),
                   (f"{mark('offline')}🔴 Офлайн", "uf:offline")]).inline_keyboard[0])
        b.row(*kb([(f"{mark('vless')}🔗 VLESS", "uf:vless"), (f"{mark('awg')}📡 AWG", "uf:awg")]).inline_keyboard[0])
        b.row(*kb([("➕ Добавить", "add"), *BACK]).inline_keyboard[0])
        if everyone:
            b.row(*kb([("🔁 Доступ для всех", "protoall"), ("🗑 Удалить всех", "delall")]).inline_keyboard[0])
        counts = {k: sum(1 for u in everyone if u["proto"] == k) for k in ICON}
        text = ("<b>Пользователи</b>\n"
                f"🟢 Сейчас онлайн: <b>{len(online)}</b>   🔴 не в сети: {len(everyone) - len(online)}"
                f"   (всего {len(everyone)})\n"
                f"🔗📡 VLESS + AmneziaWG: {counts['both']}   🔗 только VLESS: {counts['vless']}   "
                f"📡 только AmneziaWG: {counts['awg']}\n"
                + (f"Показано: {len(shown)} из {len(everyone)}\n" if only else "") +
                "🟢 в сети  🔴 не в сети · ✅ активен  ⏳ скоро истекает  ⏸ отключён")
        await show(target, text, b.as_markup())

    @router.callback_query(F.data == "users")
    async def users(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await users_list(cb)

    @router.callback_query(F.data.startswith("uf:"))
    async def users_filtered(cb: CallbackQuery) -> None:
        await users_list(cb, cb.data[3:], 0)

    @router.callback_query(F.data.startswith("up:"))
    async def users_page(cb: CallbackQuery) -> None:
        await users_list(cb, page=int(cb.data[3:]))

    @router.callback_query(F.data == "noop")
    async def noop(cb: CallbackQuery) -> None:
        await cb.answer()

    @router.callback_query(F.data == "protoall")
    async def users_proto_all(cb: CallbackQuery) -> None:
        await show(cb, "Какой доступ дать <b>всем</b> пользователям? Потом можно поменять у любого в карточке.\n\n"
                       "У кого доступ пропадёт, у того перестанут работать соответствующие ключи "
                       "(VLESS-подписка или конфиг AmneziaWG).",
                   kb(*[[(title, f"pa:{key}")] for key, title in PROTOS.items()], [("Отмена", "users")]))

    @router.callback_query(F.data.startswith("pa:"))
    async def users_proto_all_ok(cb: CallbackQuery) -> None:
        proto = cb.data[3:]
        if proto not in PROTOS:
            return
        for u in db.users():
            db.update_user(u["id"], proto=proto)
        await cb.answer(f"Всем: {PROTOS[proto]}")
        await apply(cb)
        await users_list(cb)

    def user_card(u) -> tuple[str, InlineKeyboardMarkup]:
        limit = fmt_bytes(u["limit_bytes"]) if u["limit_bytes"] else "без лимита"
        text = (f"<b>{html.escape(u['name'])}</b> {service.user_status(u)}\n"
                f"Статус: {'активен' if u['enabled'] else 'отключён'}\n"
                f"Срок: {fmt_date(u['expire_at'])}\n"
                f"Трафик: ↑{fmt_bytes(u['up'])} ↓{fmt_bytes(u['down'])} = "
                f"<b>{fmt_bytes(u['up'] + u['down'])}</b> / {limit}\n"
                f"Создан: {fmt_date(u['created_at'])}\n"
                f"Доступ: {PROTOS[u['proto']]}\n"
                f"Сейчас: {service.presence(u)[1]}")
        i = u["id"]
        get = [b for b in (("🔗 Ключи и QR", f"links:{i}") if uses(u, "vless") else None,
                           ("📡 AWG приложение", f"awg:{i}") if uses(u, "awg") else None,
                           ("📶 AWG роутер", f"awgr:{i}") if uses(u, "awg") else None) if b]
        markup = kb(get, [("🔁 Доступ: " + {"both": "оба", "vless": "VLESS", "awg": "AmneziaWG"}[u["proto"]],
                           f"proto:{i}")],
                    [("⏸ Отключить" if u["enabled"] else "▶️ Включить", f"toggle:{i}"),
                     ("📅 Срок", f"days:{i}"), ("📦 Лимит", f"limit:{i}")],
                    [("🔄 Сбросить трафик", f"reset:{i}"), ("🗑 Удалить", f"del:{i}")],
                    [("⬅️ Пользователи", "users")])
        return text, markup

    async def get_user(cb: CallbackQuery):
        u = db.user(int(cb.data.split(":")[1]))
        if not u:
            await cb.answer("Пользователь не найден", show_alert=True)
        return u

    @router.callback_query(F.data.startswith("u:"))
    async def user_view(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if u := await get_user(cb):
            await show(cb, *user_card(u))

    @router.callback_query(F.data.startswith("proto:"))
    async def user_proto(cb: CallbackQuery) -> None:
        if u := await get_user(cb):
            order = list(PROTOS)
            db.update_user(u["id"], proto=order[(order.index(u["proto"]) + 1) % len(order)])
            await apply(cb)
            await show(cb, *user_card(db.user(u["id"])))

    @router.callback_query(F.data.startswith("links:"))
    async def user_links(cb: CallbackQuery) -> None:
        u = await get_user(cb)
        if not u:
            return
        sub = service.sub_url(u)
        text = (f"<b>{html.escape(u['name'])}</b>\n\n"
                f"📲 <b>Подписка</b> (рекомендуется — ключи обновляются сами):\n<code>{html.escape(sub)}</code>\n\n"
                + "\n\n".join(f"<code>{html.escape(link)}</code>" for link in service.links(u))
                + "\n\nПриложения: Hiddify, v2RayTun, Happ (iOS/Android), v2rayN (Windows), Streisand (iOS).")
        await cb.message.answer_photo(BufferedInputFile(qr_png(sub), "qr.png"), caption="QR-код подписки")
        await cb.message.answer(text, disable_web_page_preview=True)
        await cb.answer()

    @router.callback_query(F.data.startswith(("awg:", "awgr:")))
    async def user_awg(cb: CallbackQuery) -> None:
        for_router = cb.data.startswith("awgr:")
        u = await get_user(cb)
        if not u:
            return
        if db.get("awg_enabled") != "1":
            await cb.answer("AmneziaWG выключен в настройках", show_alert=True)
            return
        conf = service.awg_conf(u, router=for_router)
        if conf is None:
            await cb.answer("Конфиг перенесён со старого сервера без приватного ключа — "
                            "файл у клиента уже есть, новый выдать нельзя", show_alert=True)
            return
        await cb.message.answer_document(
            BufferedInputFile(conf.encode(), f"{u['name']}{'-router' if for_router else ''}.conf"),
            caption=(f"📶 AmneziaWG для роутера <b>{html.escape(u['name'])}</b>\n"
                     "Keenetic/Netcraze (4.2+): Интернет → Другие подключения → WireGuard → Загрузить из файла. "
                     "OpenWrt (amneziawg) — импорт этого .conf." if for_router else
                     f"📡 AmneziaWG для <b>{html.escape(u['name'])}</b>\n"
                     "Телефон/ПК: приложение AmneziaVPN или AmneziaWG — импорт файла или QR.\n"
                     "Для роутера возьмите «📶 AWG роутер» — прошивки не принимают усиленную маскировку."))
        await cb.message.answer_photo(BufferedInputFile(qr_png(conf), "awg.png"), caption="QR для AmneziaWG")
        await cb.answer()

    @router.callback_query(F.data.startswith("toggle:"))
    async def user_toggle(cb: CallbackQuery) -> None:
        u = await get_user(cb)
        if not u:
            return
        enable = not u["enabled"]
        fields = {"enabled": int(enable)}
        if enable and u["expire_at"] and u["expire_at"] <= time.time():
            fields["expire_at"] = None
        if enable and u["limit_bytes"] and u["up"] + u["down"] >= u["limit_bytes"]:
            fields.update(up=0, down=0)
        db.update_user(u["id"], **fields)
        await apply(cb)
        await show(cb, *user_card(db.user(u["id"])))

    @router.callback_query(F.data.startswith("reset:"))
    async def user_reset(cb: CallbackQuery) -> None:
        if u := await get_user(cb):
            db.update_user(u["id"], up=0, down=0)
            await show(cb, *user_card(db.user(u["id"])))

    @router.callback_query(F.data.startswith("del:"))
    async def user_delete(cb: CallbackQuery) -> None:
        if u := await get_user(cb):
            await show(cb, f"Удалить <b>{html.escape(u['name'])}</b>? Ключи перестанут работать.",
                       kb([("🗑 Да, удалить", f"delok:{u['id']}"), ("Отмена", f"u:{u['id']}")]))

    @router.callback_query(F.data.startswith("delok:"))
    async def user_delete_ok(cb: CallbackQuery) -> None:
        if u := await get_user(cb):
            db.delete_user(u["id"])
            await apply(cb)
            await users_list(cb)

    @router.callback_query(F.data == "delall")
    async def users_delete_all(cb: CallbackQuery) -> None:
        count = len(db.users())
        await show(cb, f"⚠️ Удалить <b>всех пользователей ({count})</b>?\n\n"
                       "У всех перестанут работать ключи VLESS и конфиги AmneziaWG, включая роутеры. "
                       "Перед удалением бот пришлёт файл бэкапа — из него можно всё восстановить.",
                   kb([(f"🗑 Да, удалить всех {count}", "delall2")], [("Отмена", "users")]))

    @router.callback_query(F.data == "delall2")
    async def users_delete_all_confirm(cb: CallbackQuery) -> None:
        await show(cb, "Точно? Это действие нельзя отменить кнопкой — только восстановлением из бэкапа.",
                   kb([("🗑 Удалить окончательно", "delallok")], [("Отмена", "users")]))

    @router.callback_query(F.data == "delallok")
    async def users_delete_all_ok(cb: CallbackQuery) -> None:
        await cb.answer("Делаю бэкап и удаляю…")
        await send_backup(service, cb.bot, cb.from_user.id)  # first a way back
        users = db.users()
        for u in users:
            db.delete_user(u["id"])
        await apply(cb)
        await cb.message.answer(f"🗑 Удалено пользователей: {len(users)}. Восстановить — отправьте боту файл бэкапа выше.")
        await users_list(cb.message)

    @router.callback_query(F.data.startswith("days:"))
    async def user_days(cb: CallbackQuery, state: FSMContext) -> None:
        if u := await get_user(cb):
            await state.set_state(Input.days)
            await state.update_data(user_id=u["id"])
            await show(cb, f"Срок для <b>{html.escape(u['name'])}</b>: сейчас {fmt_date(u['expire_at'])}.\n"
                           "Отправьте число дней от сегодня (0 — бессрочно) или «+30», чтобы продлить.",
                       kb([("+30 дней", f"plus30:{u['id']}"), ("♾ Бессрочно", f"nodays:{u['id']}")],
                          [("Отмена", f"u:{u['id']}")]))

    async def set_expire(target, user_id: int, expire_at: int | None) -> None:
        u = db.user(user_id)
        fields = {"expire_at": expire_at}
        was_expired = u["expire_at"] and u["expire_at"] <= time.time()
        if not u["enabled"] and was_expired and (expire_at is None or expire_at > time.time()):
            fields["enabled"] = 1
        db.update_user(user_id, **fields)
        if "enabled" in fields:
            await apply(target)
        await show(target, *user_card(db.user(user_id)))

    def extend(u, days: int) -> int:
        base = max(u["expire_at"] or 0, int(time.time()))
        return base + days * 86400

    @router.callback_query(F.data.startswith("plus30:"))
    async def user_plus30(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if u := await get_user(cb):
            await set_expire(cb, u["id"], extend(u, 30))

    @router.callback_query(F.data.startswith("nodays:"))
    async def user_nodays(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if u := await get_user(cb):
            await set_expire(cb, u["id"], None)

    @router.message(Input.days, F.text)
    async def user_days_input(message: Message, state: FSMContext) -> None:
        m = re.fullmatch(r"\s*(\+?)(\d{1,5})\s*", message.text)
        if not m:
            await message.answer("Нужно число дней, например 30 или +30. /cancel — отмена.")
            return
        user_id = (await state.get_data())["user_id"]
        await state.clear()
        u = db.user(user_id)
        if not u:
            return
        days = int(m.group(2))
        if m.group(1):
            expire = extend(u, days)
        else:
            expire = int(time.time()) + days * 86400 if days else None
        await set_expire(message, user_id, expire)

    @router.callback_query(F.data.startswith("limit:"))
    async def user_limit(cb: CallbackQuery, state: FSMContext) -> None:
        if u := await get_user(cb):
            await state.set_state(Input.limit)
            await state.update_data(user_id=u["id"])
            await show(cb, f"Лимит трафика для <b>{html.escape(u['name'])}</b> в ГБ (0 — без лимита):",
                       kb([("Отмена", f"u:{u['id']}")]))

    @router.message(Input.limit, F.text)
    async def user_limit_input(message: Message, state: FSMContext) -> None:
        try:
            gb = float(message.text.replace(",", "."))
            assert gb >= 0
        except (ValueError, AssertionError):
            await message.answer("Нужно число гигабайт, например 50. /cancel — отмена.")
            return
        user_id = (await state.get_data())["user_id"]
        await state.clear()
        if db.user(user_id):
            db.update_user(user_id, limit_bytes=int(gb * GB) or None)
            await show(message, *user_card(db.user(user_id)))

    @router.callback_query(F.data == "add")
    async def user_add(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Input.add_user)
        await show(cb, "Отправьте имя пользователя (латиница, цифры, <code>_ . -</code>).\n"
                       "Можно сразу указать срок в днях и лимит в ГБ:\n<code>ivan 30 100</code>\n\n"
                       "Можно несколько — по одному в строке.", kb([("Отмена", "users")]))

    @router.message(Input.add_user, F.text)
    async def user_add_input(message: Message, state: FSMContext) -> None:
        created, errors = [], []
        for line in message.text.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            if not NAME_RE.match(name):
                errors.append(f"{name}: недопустимое имя")
                continue
            if db.user_by_name(name):
                errors.append(f"{name}: уже существует")
                continue
            try:
                days = int(parts[1]) if len(parts) > 1 else 0
                gb = float(parts[2].replace(",", ".")) if len(parts) > 2 else 0
            except ValueError:
                errors.append(f"{name}: срок и лимит должны быть числами")
                continue
            created.append(db.add_user(name, int(time.time()) + days * 86400 if days else None,
                                       int(gb * GB) or None))
        if errors:
            await message.answer("⚠️ " + "\n".join(html.escape(e) for e in errors))
        if not created:
            await message.answer("Никто не добавлен. Попробуйте ещё раз или /cancel.")
            return
        await state.clear()
        await apply(message)
        if len(created) == 1:
            await show(message, *user_card(db.user(created[0])))
        else:
            await users_list(message)

    # ---------- server ----------
    @router.callback_query(F.data == "server")
    async def server(cb: CallbackQuery) -> None:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(str(config.DATA_DIR))
        net = psutil.net_io_counters()
        load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
        users = db.users()
        total = sum(u["up"] + u["down"] for u in users)
        xv = await service.xray.version()

        def core_line(core, ver):
            state = "🟢 работает" if core.running else ("⚪️ выключен" if not core.wanted else "🔴 упал")
            up = f", uptime {fmt_uptime(time.time() - core.started_at)}" if core.running else ""
            return f"{core.name} {ver or '?'}: {state}{up}"

        text = (f"<b>Сервер</b> {html.escape(service.host)}\n"
                f"ОС: {html.escape(platform.platform(terse=True))}\n"
                f"Uptime: {fmt_uptime(time.time() - psutil.boot_time())}\n"
                f"CPU: {psutil.cpu_percent(interval=0.5):.0f}% ({psutil.cpu_count()} ядер), load {load}\n"
                f"RAM: {fmt_bytes(mem.used)} / {fmt_bytes(mem.total)} ({mem.percent:.0f}%)\n"
                f"Диск: {fmt_bytes(disk.used)} / {fmt_bytes(disk.total)} ({disk.percent:.0f}%)\n"
                f"Сеть с загрузки ОС: ↑{fmt_bytes(net.bytes_sent)} ↓{fmt_bytes(net.bytes_recv)}\n\n"
                f"{core_line(service.xray, xv)}\n"
                f"{core_line(service.awg, await service.awg.version())}\n\n"
                f"Трафик пользователей (всего): {fmt_bytes(total)}")
        if config.DDNS_DOMAIN:
            text += f"\n\n🌐 DuckDNS: {html.escape(config.DDNS_DOMAIN)} → {db.get('ddns_ip') or '…'}"
        last = json.loads(db.get("watch_last") or "{}")
        if last.get("state"):
            text += (f"\n📡 {watch.STATE_TEXT.get(last['state'], last['state'])}"
                     f" (проверено {time.strftime('%d.%m %H:%M', time.localtime(last.get('time', 0)))})")
        watch_on = db.get("watch_enabled", "1") == "1"
        await show(cb, text, kb([("🔄 Обновить", "server"), ("♻️ Перезапуск ядер", "restart")],
                                [("📡 Доступность из РФ", "reach"), ("🧹 Очистить диск", "cleanup")],
                                [("📋 Сводка за неделю", "report")],
                                [(f"🧹 Автоочистка при ≥{db.get('disk_threshold', '80')}%: "
                                  f"{'вкл' if db.get('disk_autoclean', '1') == '1' else 'выкл'}", "cleanup_auto")],
                                [(f"🔔 Сторож раз в час: {'вкл' if watch_on else 'выкл'}", "reach_toggle")], BACK))

    @router.callback_query(F.data == "reach")
    async def reach(cb: CallbackQuery) -> None:
        await cb.answer("Проверяю с узлов в России, ~20 секунд…")
        port = config.XHTTP_PORT or config.XRAY_PORT
        async with aiohttp.ClientSession() as session:
            result = await service.reach.check(session, service.host, port)
        if result["state"] != "error":
            db.set("watch_last", json.dumps(result))
            db.set("watch_state", result["state"])
        await cb.message.answer(watch.describe(result, service.host, port), reply_markup=kb([("📊 Сервер", "server")]))

    def report_kb() -> InlineKeyboardMarkup:
        on = db.get("report_enabled", "1") == "1"
        return kb([("🔕 Не присылать по понедельникам" if on else "🔔 Присылать по понедельникам в 10:00",
                    "report_toggle")], [("📊 Сервер", "server")])

    @router.callback_query(F.data == "report")
    async def weekly_report(cb: CallbackQuery) -> None:
        await cb.answer()
        await cb.message.answer(report.build(service), reply_markup=report_kb())

    @router.callback_query(F.data == "report_toggle")
    async def report_toggle(cb: CallbackQuery) -> None:
        on = db.get("report_enabled", "1") == "1"
        db.set("report_enabled", "0" if on else "1")
        if not on:
            db.set("report_week", report.week_key(time.time()))  # next one on Monday, not right now
        await cb.answer("Сводка по понедельникам выключена" if on else "Сводка будет приходить по понедельникам в 10:00")
        await cb.message.edit_reply_markup(reply_markup=report_kb())

    @router.callback_query(F.data == "cleanup")
    async def cleanup(cb: CallbackQuery) -> None:
        service.request_cleanup()
        await cb.answer()
        await cb.message.answer("🧹 Чищу диск: старые образы Docker, системный журнал, кэш apt, старые логи. "
                                "Через минуту-две придёт результат.\n\n"
                                "Если ничего не придёт — на сервере: <code>./vpn.sh cleanup</code>")

    @router.callback_query(F.data == "cleanup_auto")
    async def cleanup_auto(cb: CallbackQuery) -> None:
        db.set("disk_autoclean", "0" if db.get("disk_autoclean", "1") == "1" else "1")
        await server(cb)

    @router.callback_query(F.data == "reach_toggle")
    async def reach_toggle(cb: CallbackQuery) -> None:
        db.set("watch_enabled", "0" if db.get("watch_enabled", "1") == "1" else "1")
        await server(cb)

    @router.callback_query(F.data == "restart")
    async def restart(cb: CallbackQuery) -> None:
        await cb.answer("Перезапускаю…")
        if await apply(cb):
            await cb.message.answer("✅ Ядра перезапущены")

    # ---------- updates ----------
    async def remote_bot_version(session: aiohttp.ClientSession) -> str | None:
        if not config.BOT_VERSION_URL:
            return None
        try:
            async with session.get(config.BOT_VERSION_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return (await r.text()).strip() if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def remote_changelog(current: str, remote: str) -> str:
        """«Что нового» из CHANGELOG.md рядом с файлом VERSION в репозитории — видно до обновления."""
        if not config.BOT_VERSION_URL.endswith("VERSION"):
            return ""
        url = config.BOT_VERSION_URL[:-len("VERSION")] + "CHANGELOG.md"
        try:
            async with aiohttp.ClientSession() as session, \
                    session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return changelog.between(await r.text(), current, remote, limit=2500) if r.status == 200 else ""
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ""

    @router.callback_query(F.data == "updates")
    async def updates(cb: CallbackQuery) -> None:
        await cb.answer("Проверяю…")
        rows, lines = [], ["<b>Обновления</b>\n"]
        async with aiohttp.ClientSession() as session:
            for core in (service.xray,):
                current = await core.version()
                try:
                    tag = await latest_tag(session, core.repo)
                    latest = plain_version(tag)
                except Exception as e:  # noqa: BLE001 - shown to the admin
                    tag, latest = None, None
                    lines.append(f"{core.name}: {current} (ошибка проверки: {html.escape(str(e))})")
                if latest:
                    mark = "✅ актуальная" if latest == current else f"→ доступна <b>{latest}</b>"
                    lines.append(f"{core.name}: {current} {mark}")
                    if latest != current:
                        rows.append([(f"⬆️ {core.name} {latest}", f"upd:{core.name}:{tag}")])
                if core.has_backup():
                    rows.append([(f"↩️ Откатить {core.name}", f"rollback:{core.name}")])
            remote = await remote_bot_version(session)
        lines.append(f"amneziawg: {await service.awg.version() or '?'} (обновляется вместе с ботом)")
        if remote and remote != config.VERSION:
            lines.append(f"бот: {config.VERSION} → доступна <b>{html.escape(remote)}</b>")
            news = await remote_changelog(config.VERSION, remote)
            if news:
                lines.append(f"\n<b>Что будет в новой версии</b>\n\n{news}\n")
        else:
            lines.append(f"бот: {config.VERSION}" + (" ✅ актуальная" if remote else ""))
        rows.append([("⬆️ Обновить бота", "updbot")])
        auto = db.get("autoupdate", "0") == "1"
        rows.append([(f"🌙 Автообновление ночью: {'вкл' if auto else 'выкл'}", "autoupd")])
        lines.append("\n🌙 Автообновление: " + ("<b>вкл</b> — новая версия ставится сама в 3–4 ночи по Москве, "
                                                "если вышла не меньше 6 часов назад" if auto else "выкл"))
        previous = rollback_target()
        if previous:
            rows.append([(f"↩️ Вернуть бота на {previous}", "rollbot")])
        rows.append([("🔄 Проверить снова", "updates"), *BACK])
        await show(cb, "\n".join(lines), kb(*rows))

    def rollback_target() -> str:
        """The version before the last update, if the host saved it (data/rollback.info: "<commit> <version>")."""
        try:
            return (config.DATA_DIR / "rollback.info").read_text().split()[1]
        except (OSError, IndexError):
            return ""

    @router.callback_query(F.data == "rollbot")
    async def rollback_bot(cb: CallbackQuery) -> None:
        previous = rollback_target()
        if not previous:
            await cb.answer("Нет сохранённой предыдущей версии", show_alert=True)
            return
        await show(cb, f"Вернуть бота на версию <b>{previous}</b> (сейчас {config.VERSION})?\n\n"
                       "Пользователи и ключи сохранятся. VPN пропадёт на 10–30 секунд. "
                       "Снова обновиться можно в любой момент кнопкой «⬆️ Обновить бота».",
                   kb([(f"↩️ Да, вернуть {previous}", "rollbotok")], [("Отмена", "updates")]))

    @router.callback_query(F.data == "rollbotok")
    async def rollback_bot_ok(cb: CallbackQuery) -> None:
        (config.DATA_DIR / "update.request").write_text("rollback")
        await cb.answer()
        await cb.message.answer("⏳ Возвращаю предыдущую версию (1–3 минуты), после чего придёт сообщение.\n\n"
                                "Если ничего не произошло — на сервере: <code>./vpn.sh rollback</code>")

    def core_by_name(name: str):
        return {"xray": service.xray, "amneziawg": service.awg}[name]

    @router.callback_query(F.data.startswith("upd:"))
    async def update_core(cb: CallbackQuery) -> None:
        _, name, tag = cb.data.split(":", 2)
        core = core_by_name(name)
        await cb.answer()
        msg = await cb.message.answer(f"⏳ Обновляю {name} до {html.escape(tag)}…")
        try:
            async with aiohttp.ClientSession() as session:
                await service.collect_stats()
                version = await core.install(session, tag)
            error = await service.xray.check_config() if core is service.xray else None
            if error:
                await core.rollback()
                await msg.edit_text(f"⚠️ Новая версия не приняла конфиг, выполнен откат:\n"
                                    f"<pre>{html.escape(error)}</pre>")
                return
            await msg.edit_text(f"✅ {name} обновлён до {version}")
        except Exception as e:  # noqa: BLE001
            await msg.edit_text(f"❌ Не удалось обновить {name}: {html.escape(str(e))}")

    @router.callback_query(F.data.startswith("rollback:"))
    async def rollback_core(cb: CallbackQuery) -> None:
        core = core_by_name(cb.data.split(":")[1])
        await cb.answer()
        try:
            version = await core.rollback()
            await cb.message.answer(f"↩️ {core.name} откатан до {version}")
        except Exception as e:  # noqa: BLE001
            await cb.message.answer(f"❌ {html.escape(str(e))}")

    @router.callback_query(F.data == "autoupd")
    async def autoupdate_toggle(cb: CallbackQuery) -> None:
        on = db.get("autoupdate", "0") != "1"
        db.set("autoupdate", "1" if on else "0")
        await cb.answer("🌙 Автообновление включено: в 3–4 ночи по Москве" if on else "Автообновление выключено",
                        show_alert=on)
        await updates(cb)

    @router.callback_query(F.data == "updbot")
    async def update_bot(cb: CallbackQuery) -> None:
        (config.DATA_DIR / "update.request").write_text(str(cb.from_user.id))
        await cb.answer()
        await cb.message.answer(
            "⏳ Запрос на обновление отправлен. Сервер скачает новую версию и перезапустит бота "
            "(1–3 минуты), после чего придёт сообщение.\n\n"
            "Если ничего не произошло — выполните на сервере: <code>./vpn.sh update</code>")

    # ---------- settings ----------
    def settings_view() -> tuple[str, InlineKeyboardMarkup]:
        s = service.settings()
        on = lambda key: "вкл" if s.get(key) == "1" else "выкл"  # noqa: E731
        text = (f"<b>Настройки</b>\n\n"
                f"Адрес сервера: <code>{html.escape(service.host)}</code>\n"
                f"Названия ключей: <code>{html.escape(service.remark('Reality'))}</code>"
                + ("" if service.country else " (страну не удалось определить)")
                + " — страну можно задать в .env: <code>SERVER_COUNTRY=NL</code>\n"
                f"Порты: Reality {config.XRAY_PORT}/tcp, XHTTP {config.XHTTP_PORT or '—'}/tcp, "
                f"AmneziaWG {config.AWG_PORT}/udp, подписка {config.SUB_PORT}/tcp\n\n"
                + (f"<b>VLESS Reality</b>\nМаскировка: свой сайт <code>{html.escape(s['self_sni'])}</code> "
                   f"(SNI = свой домен, настоящий сертификат; отключить: <code>./vpn.sh stealth off</code>)\n"
                   if s["self_sni"] else
                   f"<b>VLESS Reality</b>\nSNI (маскировка): <code>{html.escape(s['reality_sni'])}</code>, "
                   f"передавать SNI в ссылках: {'нет' if s.get('reality_nosni') == '1' else 'да'}\n") +
                f"XHTTP: {on('xhttp_enabled') if config.XHTTP_PORT else 'выкл (XHTTP_PORT=0)'}\n"
                f"Public key: <code>{s['reality_public']}</code>\nShort ID: <code>{s['reality_sid']}</code>\n\n"
                f"<b>AmneziaWG</b>: {on('awg_enabled')}, подсеть {config.AWG_SUBNET}, порт {config.AWG_PORT}\n"
                f"Маскировка новых конфигов: "
                f"{'QUIC Initial 1200 байт' if s.get('awg_client_profile', 'quic') == 'quic' else 'как у сервера'}\n"
                f"<b>Блокировка рекламы</b>: {on('block_ads')}\n"
                f"<b>РФ напрямую</b> (российские сайты мимо VPN, Happ и v2RayTun): {on('routing_ru')}")
        markup = kb([("🌐 SNI Reality", "set:sni"), ("🩺 Проверить SNI", "set:check")],
                    [(f"SNI в ссылках: {'нет' if s.get('reality_nosni') == '1' else 'да'}", "set:nosni"),
                     (f"XHTTP: {on('xhttp_enabled')}", "set:xhttp")],
                    [("🔑 Новые ключи Reality", "set:rekey")],
                    [(f"AmneziaWG: {on('awg_enabled')}", "set:awg"), ("🔑 Новые ключи AWG", "set:awgkey")],
                    [("Маскировка новых AWG-конфигов: "
                      + ("QUIC" if s.get("awg_client_profile", "quic") == "quic" else "как у сервера"), "set:awgprof")],
                    [(f"Блок рекламы: {on('block_ads')}", "set:ads"), ("📍 Адрес сервера", "set:host")],
                    [(f"🧭 РФ напрямую: {on('routing_ru')}", "set:routing")],
                    BACK)
        return text, markup

    @router.callback_query(F.data == "settings")
    async def settings(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await show(cb, *settings_view())

    @router.callback_query(F.data == "set:routing")
    async def toggle_routing(cb: CallbackQuery) -> None:
        on = db.get("routing_ru", "1") != "1"
        db.set("routing_ru", "1" if on else "0")
        await show(cb, *settings_view())
        await cb.message.answer(
            "🧭 «РФ напрямую» включено: Happ и v2RayTun получат правила при обновлении подписки." if on else
            "🧭 Правила больше не рассылаются. У кого они уже есть, остаются в приложении — выключить можно "
            "в нём самом: Happ — Настройки → Маршрутизация, v2RayTun — Маршрутизация.")

    @router.callback_query(F.data.in_({"set:ads", "set:awg", "set:nosni", "set:xhttp"}))
    async def toggle_setting(cb: CallbackQuery) -> None:
        key = {"set:ads": "block_ads",
               "set:awg": "awg_enabled", "set:nosni": "reality_nosni", "set:xhttp": "xhttp_enabled"}[cb.data]
        db.set(key, "0" if db.get(key) == "1" else "1")
        await apply(cb)
        await show(cb, *settings_view())
        if key in ("reality_nosni", "xhttp_enabled"):
            await cb.message.answer("ℹ️ Ссылки изменились — клиентам с подпиской достаточно обновить её.")

    @router.callback_query(F.data == "set:rekey")
    async def rekey(cb: CallbackQuery) -> None:
        await show(cb, "Сгенерировать новые ключи Reality? Старые ключи у всех клиентов перестанут работать "
                       "(подписки обновятся автоматически).", kb([("Да", "set:rekeyok"), ("Отмена", "settings")]))

    @router.callback_query(F.data == "set:rekeyok")
    async def rekey_ok(cb: CallbackQuery) -> None:
        service.regenerate_reality()
        await apply(cb)
        await show(cb, *settings_view())

    @router.callback_query(F.data == "set:awgkey")
    async def awg_rekey(cb: CallbackQuery) -> None:
        await show(cb, "⚠️ Сгенерировать новый ключ и маскировку AmneziaWG (2.0)?\n\n"
                       "<b>Все</b> выданные .conf перестанут работать, <b>включая роутер</b> — "
                       "их нужно будет выдать и импортировать заново.",
                   kb([("Да", "set:awgkeyok"), ("Отмена", "settings")]))

    @router.callback_query(F.data == "set:awgkeyok")
    async def awg_rekey_ok(cb: CallbackQuery) -> None:
        service.regenerate_awg()
        await service.awg.stop()  # obfuscation parameters can't be changed on a running interface
        await apply(cb)
        await show(cb, *settings_view())

    @router.callback_query(F.data == "set:awgprof")
    async def awg_profile(cb: CallbackQuery) -> None:
        # client-side only: affects .conf files issued from now on; the server and existing devices are unchanged
        db.set("awg_client_profile", "server" if db.get("awg_client_profile", "quic") == "quic" else "quic")
        await show(cb, *settings_view())

    @router.callback_query(F.data == "set:sni")
    async def set_sni(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Input.reality_sni)
        await show(cb, "Отправьте домен для маскировки Reality (сайт с TLS 1.3 и HTTP/2, "
                       "не за Cloudflare), например:\n<code>www.microsoft.com</code>, <code>dl.google.com</code>, "
                       "<code>www.samsung.com</code>", kb([("Отмена", "settings")]))

    @router.message(Input.reality_sni, F.text)
    async def set_sni_input(message: Message, state: FSMContext) -> None:
        domain = message.text.strip().lower()
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
            await message.answer("Это не похоже на домен. /cancel — отмена.")
            return
        await state.clear()
        check = await service.check_sni(domain)
        if "❌" in check and "✅" not in check:
            await message.answer(f"{html.escape(check)}\n\nSNI не изменён.")
            return
        db.set("reality_sni", domain)
        await apply(message)
        await message.answer(html.escape(check) + "\n\nКлиентам с подпиской достаточно обновить её.")
        await show(message, *settings_view())

    @router.callback_query(F.data == "set:check")
    async def check_sni(cb: CallbackQuery) -> None:
        await cb.answer("Проверяю…")
        await cb.message.answer(html.escape(await service.check_sni(db.get("reality_sni"))))

    @router.callback_query(F.data == "set:host")
    async def set_host(cb: CallbackQuery, state: FSMContext) -> None:
        if config.SERVER_HOST:
            await cb.answer("Адрес задан в .env (SERVER_HOST)", show_alert=True)
            return
        await state.set_state(Input.host)
        await show(cb, "Отправьте IP или домен, который будет в ключах. «auto» — определить IP заново.",
                   kb([("Отмена", "settings")]))

    @router.message(Input.host, F.text)
    async def set_host_input(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        await state.clear()
        if value.lower() == "auto":
            db.conn.execute("DELETE FROM settings WHERE key='host'")
            try:
                await service.detect_host()
            except RuntimeError as e:
                await message.answer(str(e))
        elif re.fullmatch(r"[A-Za-z0-9.:\-]+", value):
            db.set("host", value)
        else:
            await message.answer("Некорректный адрес.")
        await show(message, *settings_view())

    # ---------- backup ----------
    def backup_kb() -> InlineKeyboardMarkup:
        on = db.get("backup_auto", "1") == "1"
        return kb([(f"🗓 Автобэкап каждую ночь: {'вкл' if on else 'выкл'}", "backup_auto")], BACK)

    @router.callback_query(F.data == "backup")
    async def backup(cb: CallbackQuery) -> None:
        await cb.answer()
        await send_backup(service, cb.bot, cb.from_user.id)
        await cb.message.answer(
            "Чтобы восстановить — перешлите файл бэкапа боту, он покажет, что в нём, и спросит подтверждение.\n\n"
            "🗓 Автобэкап: каждую ночь (около 4:30 по Москве) бот присылает бэкап без звука, если за день что-то "
            "поменялось — пользователи, сроки, лимиты, ключи, настройки.", reply_markup=backup_kb())

    @router.callback_query(F.data == "backup_auto")
    async def backup_auto(cb: CallbackQuery) -> None:
        db.set("backup_auto", "0" if db.get("backup_auto", "1") == "1" else "1")
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=backup_kb())

    def pending_restore(admin: int) -> Path:
        return config.DATA_DIR / f"restore-{admin}.db"

    @router.message(F.document)
    async def restore(message: Message, state: FSMContext) -> None:
        if not (message.document.file_name or "").endswith(".db"):
            await message.answer("Для восстановления отправьте файл бэкапа <code>.db</code>.")
            return
        path = pending_restore(message.from_user.id)
        await message.bot.download(message.document, destination=path)
        summary = db.backup_summary(path) if db.is_valid_backup(path) else None
        if summary is None:
            path.unlink(missing_ok=True)
            await message.answer("❌ Это не бэкап VPN Bot Lite.")
            return
        await state.clear()
        stamp = re.search(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", message.document.file_name or "")
        when = f"{stamp[3]}.{stamp[2]}.{stamp[1]} {stamp[4]}:{stamp[5]}" if stamp else "неизвестной даты"
        old, new = {u["name"] for u in db.users()}, set(summary["names"])

        def names(items: set[str]) -> str:
            items = sorted(items)
            return ", ".join(html.escape(n) for n in items[:15]) + (f" и ещё {len(items) - 15}" if len(items) > 15 else "")

        lines = [f"💾 <b>Бэкап от {when}</b>",
                 f"Пользователей в бэкапе: <b>{len(new)}</b> (сейчас на сервере: {len(old)})"]
        if new - old:
            lines.append(f"➕ Вернутся: {names(new - old)}")
        if old - new:
            lines.append(f"➖ Пропадут (их нет в бэкапе): {names(old - new)}")
        if new == old:
            lines.append("Список пользователей тот же; восстановятся сроки, лимиты, ключи и настройки из бэкапа.")
        lines.append("\nПеред восстановлением бот пришлёт бэкап текущего состояния — чтобы можно было вернуть.")
        await message.answer("\n".join(lines), reply_markup=kb([("✅ Восстановить", "restore_ok"),
                                                                 ("Отмена", "restore_no")]))

    @router.callback_query(F.data == "restore_no")
    async def restore_cancel(cb: CallbackQuery) -> None:
        pending_restore(cb.from_user.id).unlink(missing_ok=True)
        await cb.answer("Отменено")
        await cb.message.edit_text("Восстановление отменено, ничего не изменилось.")

    @router.callback_query(F.data == "restore_ok")
    async def restore_confirm(cb: CallbackQuery) -> None:
        path = pending_restore(cb.from_user.id)
        if not path.exists() or not db.is_valid_backup(path):
            path.unlink(missing_ok=True)
            await cb.answer("Файл уже использован или устарел — отправьте его ещё раз", show_alert=True)
            return
        await cb.answer("Восстанавливаю…")
        await cb.message.edit_reply_markup(reply_markup=None)
        await send_backup(service, cb.bot, cb.from_user.id, "💾 Бэкап перед восстановлением — вернуть, если что")
        await service.collect_stats()
        db.close()
        for suffix in ("-wal", "-shm"):
            Path(str(db.path) + suffix).unlink(missing_ok=True)
        os.replace(path, db.path)
        db.open()
        service.init_settings()
        await service.awg.stop()  # the restored server key/parameters need a fresh interface
        await apply(cb)
        await cb.message.answer(f"✅ Восстановлено. Пользователей: {len(db.users())}", reply_markup=menu_markup())

    # ---------- logs ----------
    @router.callback_query(F.data == "logs")
    async def logs(cb: CallbackQuery) -> None:
        debug = db.get("debug_logs") == "1"
        await show(cb, "Какие логи показать?\n\nПодробные логи Xray показывают каждое подключение — "
                       "включите, попробуйте подключиться и откройте нужные логи.",
                   kb([("Xray", "log:xray"), ("AmneziaWG", "log:amneziawg")],
                      [(f"🔍 Подробные логи: {'вкл' if debug else 'выкл'}", "debuglogs")], BACK))

    @router.callback_query(F.data == "debuglogs")
    async def toggle_debug_logs(cb: CallbackQuery) -> None:
        db.set("debug_logs", "0" if db.get("debug_logs") == "1" else "1")
        await apply(cb)
        await logs(cb)

    @router.callback_query(F.data.startswith("log:"))
    async def show_logs(cb: CallbackQuery) -> None:
        core = core_by_name(cb.data.split(":")[1])
        text = "\n".join(list(core.logs)[-40:]) or "пусто"
        await cb.message.answer(f"<b>{core.name}</b>\n<pre>{html.escape(text[-3800:])}</pre>")
        await cb.answer()

    return router


async def send_backup(service: Service, bot: Bot, chat_id: int, caption: str = "💾 Бэкап VPN Bot Lite",
                      silent: bool = False) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"vpnbot-{time.strftime('%Y%m%d-%H%M')}.db"
        service.db.backup_to(path)
        await bot.send_document(chat_id, FSInputFile(path), caption=caption, disable_notification=silent)
