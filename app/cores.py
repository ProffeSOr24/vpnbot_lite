"""Xray and Hysteria2 cores: config generation, process supervision and updates."""
import asyncio
import collections
import io
import json
import logging
import os
import platform
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path

import aiohttp

from . import config

log = logging.getLogger(__name__)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
ARCH = {"x86_64": ("64", "amd64"), "amd64": ("64", "amd64"),
        "aarch64": ("arm64-v8a", "arm64"), "arm64": ("arm64-v8a", "arm64")}


def _arch() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine not in ARCH:
        raise RuntimeError(f"Неподдерживаемая архитектура: {machine}")
    return ARCH[machine]


async def latest_tag(session: aiohttp.ClientSession, repo: str) -> str:
    """Latest release tag. Uses the API first and the HTML redirect as a fallback (no rate limit)."""
    try:
        async with session.get(f"https://api.github.com/repos/{repo}/releases/latest",
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return (await r.json())["tag_name"]
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
        pass
    async with session.get(f"https://github.com/{repo}/releases/latest", allow_redirects=False,
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        location = r.headers.get("Location", "")
    if "/releases/tag/" not in location:
        raise RuntimeError(f"Не удалось узнать последнюю версию {repo}")
    return location.split("/releases/tag/", 1)[1]


def plain_version(tag: str | None) -> str | None:
    if not tag:
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", tag)
    return m.group(1) if m else tag


class Core:
    name = ""
    repo = ""
    version_re = r"(\d+\.\d+\.\d+)"

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.logs: collections.deque[str] = collections.deque(maxlen=300)
        self.wanted = False          # should the process be running
        self.started_at = 0.0
        self._lock = asyncio.Lock()
        self._watch: asyncio.Task | None = None

    # --- paths ---
    @property
    def config_path(self) -> Path:
        return config.DATA_DIR / f"{self.name}.json"

    @property
    def binary(self) -> Path:
        installed = config.BIN_DIR / self.name
        return installed if installed.exists() else config.BUILTIN_BIN_DIR / self.name

    def args(self) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return dict(os.environ)

    # --- version ---
    async def version(self, binary: Path | None = None) -> str | None:
        binary = binary or self.binary
        if not binary.exists():
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary), "version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except (OSError, asyncio.TimeoutError):
            return None
        m = re.search(self.version_re, out.decode(errors="replace"))
        return m.group(1) if m else None

    # --- process ---
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        async with self._lock:
            self.wanted = True
            await self._spawn()
        if not self._watch or self._watch.done():
            self._watch = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        async with self._lock:
            self.wanted = False
            await self._kill()

    async def restart(self) -> None:
        async with self._lock:
            self.wanted = True
            await self._kill()
            await self._spawn()
        if not self._watch or self._watch.done():
            self._watch = asyncio.create_task(self._watchdog())

    async def _spawn(self) -> None:
        self.logs.append(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        self.proc = await asyncio.create_subprocess_exec(
            *self.args(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=self.env())
        self.started_at = time.time()
        asyncio.create_task(self._read_output(self.proc))

    async def _kill(self) -> None:
        if not self.running:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        async for line in proc.stdout:
            text = ANSI_RE.sub("", line.decode(errors="replace")).rstrip()
            if text:
                self.logs.append(text)

    async def _watchdog(self) -> None:
        """Restart the process if it crashes; back off when it keeps crashing."""
        delay = 2
        while True:
            await asyncio.sleep(delay)
            if not self.wanted or self.running or self._lock.locked():
                delay = 2
                continue
            self.logs.append(f"!!! {self.name} упал (код {self.proc.returncode if self.proc else '?'}), перезапуск")
            log.warning("%s exited, restarting", self.name)
            try:
                async with self._lock:
                    if self.wanted and not self.running:
                        await self._spawn()
            except OSError as e:
                self.logs.append(f"!!! не удалось запустить: {e}")
            delay = min(delay * 2, 60)

    async def check_config(self) -> str | None:
        """Return an error text if the config is invalid, None if it is fine."""
        return None

    # --- updates ---
    def asset_url(self, tag: str) -> str:
        raise NotImplementedError

    def unpack(self, payload: bytes, dest: Path) -> None:
        raise NotImplementedError

    async def install(self, session: aiohttp.ClientSession, tag: str) -> str:
        """Download release `tag`, verify it runs, swap binaries (keeping a backup) and restart."""
        async with session.get(self.asset_url(tag), timeout=aiohttp.ClientTimeout(total=300)) as r:
            r.raise_for_status()
            payload = await r.read()
        config.BIN_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
            tmp = Path(tmp)
            self.unpack(payload, tmp)
            new_bin = tmp / self.name
            new_bin.chmod(new_bin.stat().st_mode | stat.S_IEXEC)
            new_version = await self.version(new_bin)
            if not new_version:
                raise RuntimeError("Скачанный файл не запускается")
            backup = config.BIN_DIR / "backup" / self.name
            if backup.exists():
                shutil.rmtree(backup)
            backup.mkdir(parents=True)
            for f in [self.binary.parent / self.name, *self.extra_files(self.binary.parent)]:
                if f.exists():
                    shutil.copy2(f, backup / f.name)
            for f in tmp.iterdir():
                os.replace(f, config.BIN_DIR / f.name)
        await self._restart_checked()
        return new_version

    def extra_files(self, directory: Path) -> list[Path]:
        return []

    def has_backup(self) -> bool:
        return (config.BIN_DIR / "backup" / self.name / self.name).exists()

    async def rollback(self) -> str | None:
        backup = config.BIN_DIR / "backup" / self.name
        if not (backup / self.name).exists():
            raise RuntimeError("Нет сохранённой предыдущей версии")
        config.BIN_DIR.mkdir(parents=True, exist_ok=True)
        for f in backup.iterdir():
            os.replace(f, config.BIN_DIR / f.name)  # rename works even while the old binary is running
        shutil.rmtree(backup)
        await self._restart_checked()
        return await self.version()

    async def _restart_checked(self) -> None:
        if self.wanted:
            await self.restart()


class Xray(Core):
    name = "xray"
    repo = "XTLS/Xray-core"
    version_re = r"Xray (\d+\.\d+\.\d+)"

    def args(self) -> list[str]:
        return [str(self.binary), "run", "-c", str(self.config_path)]

    def env(self) -> dict[str, str]:
        return {**os.environ, "XRAY_LOCATION_ASSET": str(self.binary.parent)}

    def extra_files(self, directory: Path) -> list[Path]:
        return [directory / "geoip.dat", directory / "geosite.dat"]

    def asset_url(self, tag: str) -> str:
        return f"https://github.com/{self.repo}/releases/download/{tag}/Xray-linux-{_arch()[0]}.zip"

    def unpack(self, payload: bytes, dest: Path) -> None:
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            for member in ("xray", "geoip.dat", "geosite.dat"):
                if member in z.namelist():
                    z.extract(member, dest)

    async def check_config(self) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            str(self.binary), "run", "-test", "-c", str(self.config_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=self.env())
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        return None if proc.returncode == 0 else out.decode(errors="replace")[-1500:]

    def write_config(self, users, s: dict) -> None:
        clients = [{"id": u["uuid"], "email": u["name"], "flow": "xtls-rprx-vision"} for u in users]
        cfg = {
            "log": {"loglevel": "warning", "access": "none"},
            "api": {"tag": "api", "listen": config.XRAY_API, "services": ["StatsService", "HandlerService"]},
            "stats": {},
            "policy": {
                "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
                "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
            },
            "inbounds": [{
                "tag": "vless-reality",
                "listen": "0.0.0.0",
                "port": config.XRAY_PORT,
                "protocol": "vless",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": f"{s['reality_sni']}:443",
                        "xver": 0,
                        "serverNames": [s["reality_sni"]],
                        "privateKey": s["reality_private"],
                        "shortIds": [s["reality_sid"]],
                    },
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True},
            }],
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "block", "protocol": "blackhole"},
            ],
            "routing": {
                "domainStrategy": "IPIfNonMatch",
                "rules": [
                    {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
                    {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
                ],
            },
        }
        if s.get("block_ads") == "1":
            cfg["routing"]["rules"].append(
                {"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "block"})
        self.config_path.write_text(json.dumps(cfg, indent=2))

    async def pop_stats(self) -> dict[str, list[int]]:
        """Read and reset per-user traffic counters: {name: [up, down]}."""
        if not self.running:
            return {}
        proc = await asyncio.create_subprocess_exec(
            str(self.binary), "api", "statsquery", f"--server={config.XRAY_API}",
            "-pattern", "user>>>", "-reset",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
            data = json.loads(out or b"{}")
        except (asyncio.TimeoutError, json.JSONDecodeError):
            return {}
        result: dict[str, list[int]] = {}
        for item in data.get("stat", []) or []:
            parts = item.get("name", "").split(">>>")  # user>>>name>>>traffic>>>uplink
            if len(parts) != 4:
                continue
            value = int(item.get("value", 0))
            pair = result.setdefault(parts[1], [0, 0])
            pair[0 if parts[3] == "uplink" else 1] += value
        return result


PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
                "100.64.0.0/10", "fc00::/7", "fe80::/10", "::1/128"]


class Hysteria(Core):
    name = "hysteria"
    repo = "apernet/hysteria"
    version_re = r"Version:\s*v?(\d+\.\d+\.\d+)"

    def args(self) -> list[str]:
        return [str(self.binary), "server", "-c", str(self.config_path), "--disable-update-check"]

    def asset_url(self, tag: str) -> str:
        return f"https://github.com/{self.repo}/releases/download/{tag}/hysteria-linux-{_arch()[1]}"

    def unpack(self, payload: bytes, dest: Path) -> None:
        (dest / self.name).write_bytes(payload)

    def write_config(self, users, s: dict) -> None:
        tls_dir = config.DATA_DIR / "hysteria-tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        (tls_dir / "cert.pem").write_text(s["hy_cert"])
        key = tls_dir / "key.pem"
        key.write_text(s["hy_key"])
        key.chmod(0o600)
        cfg = {
            "listen": f":{config.HY2_PORT}",
            "tls": {"cert": str(tls_dir / "cert.pem"), "key": str(key)},
            "auth": {"type": "userpass", "userpass": {u["name"]: u["hy_pass"] for u in users}},
            "masquerade": {"type": "proxy", "proxy": {"url": f"https://{s['hy_sni']}/", "rewriteHost": True}},
            "trafficStats": {"listen": config.HY2_STATS, "secret": s["hy_stats_secret"]},
            "acl": {"inline": [*(f"reject({net})" for net in PRIVATE_NETS), "direct(all)"]},
        }
        if s.get("hy_obfs") == "1":
            cfg["obfs"] = {"type": "salamander", "salamander": {"password": s["hy_obfs_password"]}}
        self.config_path.write_text(json.dumps(cfg, indent=2))

    async def pop_stats(self, secret: str) -> dict[str, list[int]]:
        if not self.running:
            return {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{config.HY2_STATS}/traffic?clear=1",
                                       headers={"Authorization": secret},
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {}
        # hysteria: tx = sent to client (download), rx = received from client (upload)
        return {name: [int(v.get("rx", 0)), int(v.get("tx", 0))] for name, v in (data or {}).items()}
