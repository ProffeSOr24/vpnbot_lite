"""Smoke test run by CI before the image is built: the whole bot starts — settings, cores (fake Xray), the
subscription server, background jobs — with Telegram faked. A crash between start and polling fails the build,
so a broken version never reaches the servers (1.27.0 crashed right there)."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
work = Path(tempfile.mkdtemp())
(work / "bin").mkdir()
shutil.copy(ROOT / "tests" / "fake-xray", work / "bin" / "xray")
os.environ.update(DATA_DIR=str(work / "data"), BUILTIN_BIN_DIR=str(work / "bin"), BOT_TOKEN="123:abc",
                  ADMIN_IDS="1,2", SUB_PORT="29096", SERVER_HOST="1.2.3.4", SERVER_COUNTRY="NL")
sys.path.insert(0, str(ROOT))

from app import main  # noqa: E402  (after the environment is set)
from aiohttp import ClientSession  # noqa: E402
from app.db import DB  # noqa: E402

sent: list[str] = []


async def send_message(self, chat_id, text, **kw):
    sent.append(text)


async def ok(self, *a, **kw):
    return True


async def polling(self, *a, **kw):
    """Instead of Telegram: let the background jobs run, then check the subscription server answers."""
    await asyncio.sleep(3)
    db = DB(Path(os.environ["DATA_DIR"]) / "vpnbot.db")
    uid = db.add_user("smoke")
    token = db.user(uid)["token"]
    async with ClientSession() as s:
        async with s.get(f"http://127.0.0.1:29096/sub/{token}") as r:
            assert r.status == 200, r.status
        async with s.get(f"http://127.0.0.1:29096/sub/{token}", headers={"Accept": "text/html"}) as r:
            assert r.status == 200 and "Добавить в Happ" in await r.text(), r.status


with mock.patch("aiogram.Bot.send_message", send_message), mock.patch("aiogram.Bot.set_my_commands", ok), \
        mock.patch("aiogram.Bot.delete_webhook", ok), mock.patch("aiogram.Dispatcher.start_polling", polling):
    asyncio.run(main.main())
assert sent and "запущен" in sent[0], sent
print("startup OK")
