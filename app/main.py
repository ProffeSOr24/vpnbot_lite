import asyncio
import logging
import ssl
import time

import psutil
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import web

from . import config
from .bot import send_backup, setup
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


async def background(service: Service, bot: Bot, ssl_ctx: ssl.SSLContext | None) -> None:
    """Every minute: account traffic and disable expired users. Once a day: send a backup, reload the cert."""
    last_backup = last_cert_reload = time.time()
    while True:
        await asyncio.sleep(60)
        try:
            await service.collect_stats()
            disabled = await service.enforce_limits()
            if disabled:
                await notify(bot, "⏸ Отключены пользователи:\n" + "\n".join(disabled))
            if service.db.get("daily_backup", "1") == "1" and time.time() - last_backup > 86400:
                last_backup = time.time()
                for admin in config.ADMIN_IDS:
                    await send_backup(service, bot, admin)
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
    await web.TCPSite(runner, "0.0.0.0", config.SUB_PORT, ssl_context=ssl_ctx).start()
    log.info("subscription server on %s:%s", "https" if ssl_ctx else "http", config.SUB_PORT)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(setup(service))
    await bot.set_my_commands([BotCommand(command="menu", description="Главное меню"),
                               BotCommand(command="cancel", description="Отменить ввод")])

    previous = service.db.get("bot_version")
    service.db.set("bot_version", config.VERSION)
    if previous and previous != config.VERSION:
        await notify(bot, f"✅ Бот обновлён: {previous} → {config.VERSION}")
    else:
        await notify(bot, f"🚀 VPN Bot Lite v{config.VERSION} запущен. /menu"
                          + (f"\n⚠️ {error}" if error else ""))

    # a webhook left by another bot on the same token (e.g. the old vpnbot) blocks polling
    await bot.delete_webhook(drop_pending_updates=False)
    task = asyncio.create_task(background(service, bot, ssl_ctx))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await service.collect_stats()
        await service.xray.stop()
        await service.hysteria.stop()
        await service.awg.stop()
        await service.mtproto.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
