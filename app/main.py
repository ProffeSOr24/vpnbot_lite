import asyncio
import datetime
import html
import json
import logging
import re
import ssl
import time

import aiohttp
import psutil
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import web

from . import changelog, config, report, watch
from .bot import send_backup, setup
from .service import MSK, Service
from .sub import make_app

log = logging.getLogger("vpnbot")
AUTOUPDATE_HOUR = 3          # MSK: night auto-update window 03:00–04:00
AUTOUPDATE_DELAY = 6 * 3600  # a new version must be on GitHub this long before servers take it
BACKUP_AT = (4, 30)          # MSK: nightly backup, after the auto-update window
GUARD_EVERY = 600            # after a night update: health check every 10 minutes …
GUARD_BAD = 4                # … 4 failures in a row (~40 minutes) roll the bot back
GUARD_WINDOW = 90 * 60       # … watched for an hour and a half


async def notify(bot: Bot, text: str, silent: bool = False) -> None:
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text, disable_notification=silent)
        except TelegramAPIError as e:
            log.warning("cannot notify %s: %s", admin, e)


def sub_ssl_context() -> ssl.SSLContext | None:
    if not (config.SUB_CERT and config.SUB_KEY):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(config.SUB_CERT, config.SUB_KEY)
    return ctx


async def keep_reachable(service: Service, bot: Bot) -> None:
    """Every 5 minutes: point DuckDNS at the current IP. Every hour: check reachability from Russia, alert on change."""
    last_check = time.time() - 3600 + 600  # first check 10 minutes after start: not while the server is restarting
    pending = None  # a bad state seen once; reported only if the re-check confirms it
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if config.DDNS_DOMAIN and config.DDNS_TOKEN:
                    ip = config.DDNS_IP or await watch.public_ip(session)
                    if ip and ip != service.db.get("ddns_ip"):
                        if await watch.duckdns_update(session, config.DDNS_DOMAIN, config.DDNS_TOKEN, ip):
                            old = service.db.get("ddns_ip")
                            service.db.set("ddns_ip", ip)
                            if old:
                                await notify(bot, f"🌐 IP сервера сменился: {old} → {ip}\n"
                                                  f"{config.DDNS_DOMAIN} уже указывает на новый адрес — роутер и "
                                                  "AmneziaWG переподключатся сами (DNS обновится за ~5 минут).\n"
                                                  "В Happ нажмите 🔄 у подписки, чтобы сразу получить ключи "
                                                  "с новым IP (иначе обновится сам при автообновлении).")
                        else:
                            log.warning("duckdns update failed")
                if service.db.get("watch_enabled", "1") == "1" and time.time() - last_check > 3600:
                    last_check = time.time()
                    port = config.XHTTP_PORT or config.XRAY_PORT
                    result = await service.reach.check(session, service.host, port)
                    service.db.set("watch_last", json.dumps(result))
                    if result["state"] != "error":
                        service.db.add_reach(int(time.time()), result["state"])
                    previous = service.db.get("watch_state", "ok")
                    state = result["state"]
                    if state == "error" or state == previous:
                        pending = None
                    elif state != "ok" and pending != state:
                        # one bad result can be check-host's own hiccup: re-check in 5 minutes before alarming
                        pending = state
                        last_check = time.time() - 3600 + 300
                    else:
                        pending = None
                        service.db.set("watch_state", state)
                        await notify(bot, watch.describe(result, service.host, port))
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("reachability job failed")
            await asyncio.sleep(300)


async def watch_disk(service: Service, bot: Bot) -> None:
    """Disk at or above the threshold (80% by default): warn once a day, and clean up unless auto-cleanup is off.
    The host reports what was freed in data/cleanup.result."""
    db = service.db
    result = config.DATA_DIR / "cleanup.result"
    if result.exists():
        info = dict(part.split("=", 1) for part in result.read_text().split() if "=" in part)
        result.unlink()
        await notify(bot, f"🧹 Очистка диска: освобождено {int(info.get('freed', 0)) / 2**20:.0f} МБ, "
                          f"диск занят на {info.get('used_pct', '?')}%")
    used = psutil.disk_usage(str(config.DATA_DIR)).percent
    threshold = int(db.get("disk_threshold", "80") or 80)
    if used < threshold:
        return
    now = time.time()
    auto = db.get("disk_autoclean", "1") == "1"
    if now - float(db.get("disk_alert_ts", "0") or 0) > 86400:
        db.set("disk_alert_ts", int(now))
        await notify(bot, f"⚠️ Диск занят на {used:.0f}% (порог {threshold}%)."
                          + (" Запускаю автоочистку." if auto else " Очистить: 📊 Сервер → 🧹 Очистить диск."))
    if auto and now - float(db.get("disk_clean_ts", "0") or 0) > 6 * 3600:
        service.request_cleanup()


async def auto_update(service: Service, now: float) -> None:
    """Night auto-update (03:00–04:00 MSK, if enabled): ask the host to update the bot when GitHub has a newer
    version. GitHub is checked hourly to learn when a version appeared; it is installed only after it has been
    out for 6 hours — so a broken release fixed the same day never reaches the servers, and the image is surely
    built. A version the admin rolled back from is skipped."""
    db = service.db
    if db.get("autoupdate", "0") != "1" or not config.BOT_VERSION_URL:
        return
    moment = datetime.datetime.fromtimestamp(now, MSK)
    night = moment.hour == AUTOUPDATE_HOUR and db.get("autoupdate_day") != moment.strftime("%Y-%m-%d")
    if not night and now - float(db.get("autoupdate_check", "0") or 0) < 3600:
        return
    try:
        async with aiohttp.ClientSession() as session, \
                session.get(config.BOT_VERSION_URL, timeout=aiohttp.ClientTimeout(total=15)) as r:
            remote = (await r.text()).strip() if r.status == 200 else ""
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return  # try again in a minute
    db.set("autoupdate_check", int(now))
    if night:
        db.set("autoupdate_day", moment.strftime("%Y-%m-%d"))  # one decision per night
    if not re.fullmatch(r"\d+(\.\d+)*", remote) or not changelog.newer(remote, config.VERSION):
        return
    seen_ver, _, seen_ts = (db.get("autoupdate_seen") or "").partition(" ")
    if seen_ver != remote:
        db.set("autoupdate_seen", f"{remote} {int(now)}")
        seen_ts = str(int(now))
    if not night:
        return
    skip = db.get("autoupdate_skip")
    if skip and not changelog.newer(remote, skip):
        log.info("auto-update: %s skipped, the admin rolled back from %s", remote, skip)
        return
    if now - int(seen_ts) < AUTOUPDATE_DELAY:
        log.info("auto-update: %s is too fresh, next night", remote)
        return
    log.info("auto-update: %s -> %s", config.VERSION, remote)
    db.set("autoupdate_pending", remote)
    (config.DATA_DIR / "update.request").write_text("auto")


async def health_problems(service: Service, session: aiohttp.ClientSession, baseline: str) -> list[str] | None:
    """What is broken right now; None when it cannot be told (check-host did not answer)."""
    problems = [f"{core.name} не работает" for core in (service.xray, service.awg, service.hysteria)
                if core.wanted and not core.running]
    if service.xray.wanted:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", config.XRAY_PORT), 5)
            writer.close()
        except (OSError, asyncio.TimeoutError):
            problems.append(f"Xray не принимает подключения на порту {config.XRAY_PORT}")
    port = config.XHTTP_PORT or config.XRAY_PORT
    result = await service.reach.check(session, service.host, port)
    if result["state"] == "down" and baseline != "down":
        problems.append("сервер не отвечает ни из России, ни из-за рубежа")
    elif result["state"] == "error" and not problems:
        return None
    return problems


async def night_guard(service: Service, bot: Bot, now: float) -> None:
    """After a night auto-update: if the VPN is broken for ~40 minutes, go back to the previous version."""
    db = service.db
    guard = json.loads(db.get("night_guard") or "{}")
    if not guard or now < guard["next"]:
        return
    if now > guard["until"]:
        db.set("night_guard", "")
        return
    guard["next"] = now + GUARD_EVERY
    db.set("night_guard", json.dumps(guard))
    async with aiohttp.ClientSession() as session:
        problems = await health_problems(service, session, guard["baseline"])
    if problems is None:
        return
    guard["bad"] = guard["bad"] + 1 if problems else 0
    db.set("night_guard", json.dumps(guard))
    log.info("night guard: %s", problems or "ok")
    if guard["bad"] < GUARD_BAD:
        return
    db.set("night_guard", "")
    reason = "; ".join(problems)
    (config.DATA_DIR / "autorollback.reason").write_text(reason)
    (config.DATA_DIR / "update.request").write_text("rollback")
    await notify(bot, f"⚠️ После ночного обновления до {config.VERSION} VPN не работает ~40 минут: "
                      f"{html.escape(reason)}.\nВозвращаю версию {html.escape(guard['from'])}…", silent=True)


JOBS: set[asyncio.Task] = set()  # keep references so running jobs are not garbage-collected


async def auto_backup(service: Service, bot: Bot, now: float) -> None:
    """Every night: send the database to the admins (silently) if users or settings changed since the last one."""
    db = service.db
    moment = datetime.datetime.fromtimestamp(now, MSK)
    today = moment.strftime("%Y-%m-%d")
    if db.get("backup_auto", "1") != "1" or (moment.hour, moment.minute) < BACKUP_AT or db.get("backup_ts") == today:
        return
    db.set("backup_ts", today)
    fp = db.fingerprint()
    if fp == db.get("backup_fp"):
        return
    caption = (f"💾 Автобэкап {moment:%d.%m.%Y} — пользователей: {len(db.users())}\n"
               "Восстановить: переслать этот файл боту.")
    sent = False
    for admin in config.ADMIN_IDS:
        try:
            await send_backup(service, bot, admin, caption, silent=True)
            sent = True
        except TelegramAPIError as e:
            log.warning("cannot send backup to %s: %s", admin, e)
    if sent:
        db.set("backup_fp", fp)


async def safe(job) -> None:
    try:
        await job
    except Exception:  # noqa: BLE001 - a background job must not die silently
        log.exception("background job failed")


async def background(service: Service, bot: Bot, ssl_ctx: ssl.SSLContext | None) -> None:
    """Every minute: account traffic and disable expired users, watch the disk. Once a day: reload the cert."""
    last_cert_reload = time.time()
    sampler = report.Sampler()
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            (config.DATA_DIR / "heartbeat").write_text(f"{config.VERSION} {int(now)}")  # vpn.sh waits for it
            task = asyncio.create_task(safe(night_guard(service, bot, now)))  # check-host takes ~1–2 min
            JOBS.add(task)
            task.add_done_callback(JOBS.discard)
            sampler.tick(service, now)
            await auto_update(service, now)
            await auto_backup(service, bot, now)
            if report.due(service, now):
                text = report.build(service, now)
                report.mark_sent(service, now)
                await notify(bot, text)
            await watch_disk(service, bot)
            await service.collect_stats()
            disabled = await service.enforce_limits()
            if disabled:
                await notify(bot, "⏸ Отключены пользователи:\n" + "\n".join(disabled))
            if ssl_ctx and time.time() - last_cert_reload > 86400:
                last_cert_reload = time.time()
                ssl_ctx.load_cert_chain(config.SUB_CERT, config.SUB_KEY)  # pick up a renewed certificate
        except Exception:  # noqa: BLE001 - the loop must survive
            log.exception("background job failed")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN or not config.ADMIN_IDS:
        raise SystemExit("Задайте BOT_TOKEN и ADMIN_IDS в .env")

    service = Service()
    service.init_settings()
    try:
        await service.detect_host()
    except RuntimeError as e:
        log.warning("%s", e)
    psutil.cpu_percent()

    error = await service.apply()
    if error:
        log.error("cannot start cores: %s", error)

    runner = web.AppRunner(make_app(service), access_log=None)
    await runner.setup()
    ssl_ctx = sub_ssl_context()
    await web.TCPSite(runner, config.SUB_LISTEN, config.SUB_PORT, ssl_context=ssl_ctx).start()
    log.info("subscription server on %s:%s", "https" if ssl_ctx else "http", config.SUB_PORT)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(setup(service))
    await bot.set_my_commands([BotCommand(command="menu", description="Главное меню"),
                               BotCommand(command="cancel", description="Отменить ввод")])

    previous = service.db.get("bot_version")
    service.db.set("bot_version", config.VERSION)
    auto = service.db.get("autoupdate_pending") == config.VERSION  # this start is the night update we asked for
    service.db.set("autoupdate_pending", "")
    reason_file = config.DATA_DIR / "autorollback.reason"
    reason = reason_file.read_text().strip() if reason_file.exists() else ""
    reason_file.unlink(missing_ok=True)
    if previous != config.VERSION and not auto:
        service.db.set("night_guard", "")  # updated or rolled back by hand: nothing to watch
    if auto:
        service.db.set("night_guard", json.dumps({
            "from": previous or "", "baseline": service.db.get("watch_state", "ok"), "bad": 0,
            "next": time.time() + GUARD_EVERY, "until": time.time() + GUARD_WINDOW}))
    if previous and changelog.newer(previous, config.VERSION):
        service.db.set("autoupdate_skip", previous)  # night auto-update must not bring the bad version back
        service.db.set("night_guard", "")
        if reason:
            await notify(bot, f"↩️ Автоматический откат: {html.escape(reason)}.\n"
                              f"Работает прежняя версия {config.VERSION}, версия {previous} пропущена — "
                              "ночью поставится только следующая.", silent=True)
        else:
            await notify(bot, f"↩️ Бот возвращён на версию {config.VERSION} (была {previous}). "
                              "Вернуться на новую — ⬆️ Обновления → ⬆️ Обновить бота."
                              + ("\nНочное автообновление эту версию пропустит и поставит только следующую."
                                 if service.db.get("autoupdate", "0") == "1" else ""))
    elif previous and previous != config.VERSION:
        news = changelog.local_between(previous, config.VERSION)
        head = "🌙 Ночное автообновление" if auto else "✅ Бот обновлён"
        await notify(bot, f"{head}: {previous} → {config.VERSION}"
                          + (f"\n\n<b>Что нового</b>\n\n{news}" if news else "")
                          + ("\n\nЕсли что-то не так — ⬆️ Обновления → ↩️ Вернуть бота." if auto else ""),
                     silent=auto)
    else:
        await notify(bot, f"🚀 VPN Bot Lite v{config.VERSION} запущен. /menu"
                          + (f"\n⚠️ {error}" if error else ""))

    # a webhook left by another bot on the same token (e.g. the old vpnbot) blocks polling
    await bot.delete_webhook(drop_pending_updates=False)
    task = asyncio.create_task(background(service, bot, ssl_ctx))
    reach_task = asyncio.create_task(keep_reachable(service, bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        reach_task.cancel()
        await service.collect_stats()
        await service.xray.stop()
        await service.hysteria.stop()
        await service.awg.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
