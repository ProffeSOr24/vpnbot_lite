import asyncio
import json
import logging
import ssl
import time

import aiohttp
import psutil
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import web

from . import changelog, config, watch
from .bot import setup
from .service import Service
from .sub import make_app

log = logging.getLogger("vpnbot")


async def notify(bot: Bot, text: str) -> None:
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
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


async def background(service: Service, bot: Bot, ssl_ctx: ssl.SSLContext | None) -> None:
    """Every minute: account traffic and disable expired users, watch the disk. Once a day: reload the cert."""
    last_cert_reload = time.time()
    while True:
        await asyncio.sleep(60)
        try:
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
    if previous and changelog.newer(previous, config.VERSION):
        await notify(bot, f"↩️ Бот возвращён на версию {config.VERSION} (была {previous}). "
                          "Вернуться на новую — ⬆️ Обновления → ⬆️ Обновить бота.")
    elif previous and previous != config.VERSION:
        news = changelog.local_between(previous, config.VERSION)
        await notify(bot, f"✅ Бот обновлён: {previous} → {config.VERSION}"
                          + (f"\n\n<b>Что нового</b>\n\n{news}" if news else ""))
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
