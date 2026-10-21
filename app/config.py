import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BIN_DIR = DATA_DIR / "bin"          # binaries installed by the bot (updates)
BUILTIN_BIN_DIR = Path(os.environ.get("BUILTIN_BIN_DIR", "/opt/bin"))  # baked into the image

SERVER_HOST = os.environ.get("SERVER_HOST", "").strip()
XRAY_PORT = _int("XRAY_PORT", 443)
HY2_PORT = _int("HY2_PORT", 443)
SUB_PORT = _int("SUB_PORT", 2096)
# Public base URL for subscriptions, e.g. https://sub.example.com (if behind a reverse proxy)
SUB_URL = os.environ.get("SUB_URL", "").strip().rstrip("/")

XRAY_API = "127.0.0.1:10085"
HY2_STATS = "127.0.0.1:10086"

# URL of the VERSION file of the bot in the git repository, used to check for bot updates
BOT_VERSION_URL = os.environ.get("BOT_VERSION_URL", "").strip()

VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
