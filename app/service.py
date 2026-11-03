"""Glue between the database and the cores: settings, links, applying configs, traffic accounting."""
import asyncio
import base64
import datetime
import ipaddress
import json
import logging
import random
import re
import secrets
import socket
import ssl
import time
from urllib.parse import quote, urlencode

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .watch import Reachability
from . import config
from .cores import AWG_PARAMS, XHTTP_CLIENT, AmneziaWG, Xray, awg_client_lines, awg_param_lines
from .db import DB, wg_keypair

log = logging.getLogger(__name__)

DEFAULT_REALITY_SNI = "www.microsoft.com"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def reality_keys() -> tuple[str, str]:
    key = X25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64url(private), b64url(public)


def fmt_bytes(n: int | None) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.2f} {unit}"
        n /= 1024
    return ""


ONLINE_WINDOW = 180   # s: active within this window counts as online (AWG renews the handshake every 2 minutes)
ACTIVE_BYTES = 2048   # B per minute: below this a VLESS client is just idle keepalives


def fmt_duration(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч"


def fmt_ago(ts: int) -> str:
    """«5 мин назад», «вчера в 23:40», «12.10 в 18:05»"""
    delta = int(time.time()) - ts
    if delta < 3600:
        return f"{max(1, delta // 60)} мин назад"
    if delta < 6 * 3600:
        return f"{delta // 3600} ч назад"
    day = time.strftime("%d.%m", time.localtime(ts))
    hm = time.strftime("%H:%M", time.localtime(ts))
    if day == time.strftime("%d.%m"):
        return f"сегодня в {hm}"
    if day == time.strftime("%d.%m", time.localtime(time.time() - 86400)):
        return f"вчера в {hm}"
    return f"{day} в {hm}"


MSK = datetime.timezone(datetime.timedelta(hours=3))  # schedule and day boundaries of the weekly report


def msk_day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d")


COUNTRIES = {"NL": "Нидерланды", "DE": "Германия", "FI": "Финляндия", "SE": "Швеция", "EE": "Эстония",
             "LV": "Латвия", "LT": "Литва", "PL": "Польша", "FR": "Франция", "GB": "Великобритания",
             "CH": "Швейцария", "AT": "Австрия", "CZ": "Чехия", "BG": "Болгария", "RO": "Румыния",
             "MD": "Молдова", "TR": "Турция", "KZ": "Казахстан", "AM": "Армения", "GE": "Грузия",
             "AE": "ОАЭ", "US": "США", "CA": "Канада", "JP": "Япония", "SG": "Сингапур", "HK": "Гонконг",
             "IT": "Италия", "ES": "Испания", "PT": "Португалия", "NO": "Норвегия", "DK": "Дания",
             "IS": "Исландия", "IE": "Ирландия", "BE": "Бельгия", "LU": "Люксембург", "HU": "Венгрия",
             "RS": "Сербия", "CY": "Кипр", "IL": "Израиль", "RU": "Россия", "BY": "Беларусь", "UA": "Украина"}


def flag(code: str) -> str:
    """🇳🇱 from "NL" (regional indicator letters)."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code) if re.fullmatch(r"[A-Z]{2}", code) else ""


PROTOS = {"both": "🔗📡 VLESS + AmneziaWG", "vless": "🔗 только VLESS", "awg": "📡 только AmneziaWG"}


def uses(user, proto: str) -> bool:
    """Does the user get VLESS ("vless") or AmneziaWG ("awg")?"""
    kind = user["proto"] if "proto" in user.keys() else "both"
    return kind in ("both", proto)


class Service:
    def __init__(self):
        self.db = DB(config.DATA_DIR / "vpnbot.db")
        self.xray = Xray()
        self.awg = AmneziaWG()
        self.reach = Reachability()
        self._apply_lock = asyncio.Lock()

    # --- settings ---
    def init_settings(self) -> None:
        db = self.db
        if not db.get("reality_private"):
            self.regenerate_reality()
        if not db.get("reality_sni"):
            db.set("reality_sni", DEFAULT_REALITY_SNI)
        db.conn.execute("DELETE FROM settings WHERE key LIKE 'hy\\_%' ESCAPE '\\'")  # Hysteria2 was removed in 1.25
        (config.BIN_DIR / "hysteria").unlink(missing_ok=True)
        for k, v in (("block_ads", "0"), ("awg_enabled", "1"),
                     ("reality_nosni", "1"), ("xhttp_enabled", "1"), ("xhttp_path", "/" + secrets.token_hex(6)),
                     ("watch_enabled", "1")):
            if db.get(k) is None:
                db.set(k, v)
        if not db.get("awg_private"):
            self.regenerate_awg()
        db.ensure_awg(config.AWG_SUBNET)

    def regenerate_awg(self) -> None:
        """New server key and obfuscation parameters (all AmneziaWG configs must be re-issued)."""
        private, public = wg_keypair()
        while True:  # AmneziaWG 2.0: padded sizes of the four message types must all differ
            s1, s2, s3, s4 = random.randint(15, 150), random.randint(15, 150), random.randint(15, 64), \
                random.randint(0, 32)
            if len({148 + s1, 92 + s2, 64 + s3, 32 + s4}) == 4:
                break
        h = random.sample(range(5, 2 ** 31 - 1), 4)
        params = {"jc": 4, "jmin": 40, "jmax": 70, "s1": s1, "s2": s2, "s3": s3, "s4": s4,
                  "h1": h[0], "h2": h[1], "h3": h[2], "h4": h[3]}
        self.db.set("awg_private", private)
        self.db.set("awg_public", public)
        for k in AWG_PARAMS:  # drop 2.0 values left from an import, they belong to the old key
            self.db.conn.execute("DELETE FROM settings WHERE key=?", (f"awg_{k.lower()}",))
        for k, v in params.items():
            self.db.set(f"awg_{k}", str(v))

    def regenerate_reality(self) -> None:
        private, public = reality_keys()
        self.db.set("reality_private", private)
        self.db.set("reality_public", public)
        self.db.set("reality_sid", secrets.token_hex(8))

    def settings(self) -> dict:
        s = {r["key"]: r["value"] for r in self.db.conn.execute("SELECT key, value FROM settings")}
        s["self_sni"] = self.self_sni
        return s

    @property
    def self_sni(self) -> str:
        """The server's own domain when REALITY masquerades as our own site (SELF_SNI=1), else ''."""
        if not config.SELF_SNI:
            return ""
        try:
            ipaddress.ip_address(self.host)
            return ""  # an IP has no certificate to show
        except ValueError:
            return self.host

    async def detect_host(self) -> str:
        if config.SERVER_HOST:
            return config.SERVER_HOST
        if self.db.get("host"):
            return self.db.get("host")
        async with aiohttp.ClientSession() as session:
            for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        ip = (await r.text()).strip()
                        ipaddress.ip_address(ip)
                        self.db.set("host", ip)
                        return ip
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue
        raise RuntimeError("Не удалось определить внешний IP, задайте SERVER_HOST в .env")

    async def detect_country(self) -> None:
        """Country of the server's IP for the flag in key names; checked once a week (SERVER_COUNTRY overrides)."""
        if config.SERVER_COUNTRY or time.time() - float(self.db.get("country_ts", "0") or 0) < 7 * 86400:
            return
        async with aiohttp.ClientSession() as session:
            for url, field in (("https://ipwho.is/", "country_code"), ("https://api.country.is/", "country"),
                               ("https://ipinfo.io/json", "country")):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        code = str((await r.json(content_type=None)).get(field) or "").upper()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError):
                    continue
                if re.fullmatch(r"[A-Z]{2}", code):
                    self.db.set("country", code)
                    self.db.set("country_ts", int(time.time()))
                    return

    @property
    def country(self) -> str:
        return config.SERVER_COUNTRY or self.db.get("country") or ""

    def remark(self, kind: str) -> str:
        """Key name in the apps: «🇳🇱 Нидерланды Reality» — Happ shows the flag as the server icon."""
        code = self.country
        if not code:
            return kind
        return f"{flag(code)} {COUNTRIES.get(code, code)} {kind}"

    @property
    def host(self) -> str:
        return config.SERVER_HOST or self.db.get("host") or "127.0.0.1"

    def _host_for_url(self) -> str:
        try:
            if ipaddress.ip_address(self.host).version == 6:
                return f"[{self.host}]"
        except ValueError:
            pass
        return self.host

    async def check_sni(self, domain: str) -> str:
        """Can the server reach the REALITY target over TLS 1.3? Reality connects to it for every client."""
        lines = []
        for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(domain, 443, family=family,
                                                                     type=socket.SOCK_STREAM)
            except socket.gaierror:
                lines.append(f"{label}: нет адреса")
                continue
            ip = infos[0][4][0]
            ctx = ssl.create_default_context()
            started = time.monotonic()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, 443, ssl=ctx, server_hostname=domain), 6)
                version = writer.get_extra_info("ssl_object").version()
                writer.close()
                ms = (time.monotonic() - started) * 1000
                mark = "✅" if version == "TLSv1.3" else "⚠️ нужен TLSv1.3,"
                lines.append(f"{label}: {mark} {version}, {ms:.0f} мс")
            except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
                lines.append(f"{label}: ❌ {type(e).__name__} {e}".strip())
        ok = any("✅" in line for line in lines)
        broken_v6 = any(line.startswith("IPv6: ❌") for line in lines)
        verdict = "годится" if ok else "НЕ годится — выберите другой сайт"
        if ok and broken_v6:
            verdict += " (но IPv6 на сервере не работает — если VLESS не подключается, выберите сайт без IPv6)"
        return f"Проверка {domain}: {verdict}\n" + "\n".join(lines)

    def _reality_host(self) -> str:
        """VLESS Reality links carry an IP even when SERVER_HOST is a domain: clients put a domain from the address
        into SNI when the link has none, and the no-SNI trick (and the server's serverNames) would break.
        The subscription URL stays on the domain, so apps pick up a new IP on their next subscription refresh."""
        try:
            ipaddress.ip_address(self.host)
            return self._host_for_url()
        except ValueError:
            pass
        if self.self_sni or self.db.get("reality_nosni") != "1":
            return self.host
        ip = self.db.get("ddns_ip")
        if not ip:
            try:
                ip = socket.gethostbyname(self.host)
            except OSError:
                return self.host
        return ip

    # --- links ---
    def sub_url(self, user) -> str:
        base = config.SUB_URL or f"http://{self._host_for_url()}:{config.SUB_PORT}"
        return f"{base}/sub/{user['token']}"

    def _reality_params(self, s: dict) -> dict:
        # own domain: the name matches the IP and the site behind it; without SNI the client sends no name at all
        sni = s["self_sni"] or ("" if s.get("reality_nosni") == "1" else s["reality_sni"])
        return {"security": "reality", "encryption": "none", "pbk": s["reality_public"], "sid": s["reality_sid"],
                "sni": sni, "fp": "chrome"}

    def vless_link(self, user) -> str:
        s = self.settings()
        params = {"type": "tcp", "flow": "xtls-rprx-vision", **self._reality_params(s)}
        return (f"vless://{user['uuid']}@{self._reality_host()}:{config.XRAY_PORT}"
                f"?{urlencode(params)}#{quote(self.remark('Reality'))}")

    def xhttp_link(self, user) -> str:
        s = self.settings()
        params = {"type": "xhttp", "path": s["xhttp_path"], "mode": "packet-up",
                  "extra": json.dumps(XHTTP_CLIENT, separators=(",", ":")), **self._reality_params(s)}
        return (f"vless://{user['uuid']}@{self._reality_host()}:{config.XHTTP_PORT}"
                f"?{urlencode(params)}#{quote(self.remark('XHTTP'))}")

    def links(self, user) -> list[str]:
        if not uses(user, "vless"):
            return []
        links = [self.vless_link(user)]
        if config.XHTTP_PORT and self.db.get("xhttp_enabled") == "1":
            links.append(self.xhttp_link(user))
        return links

    def awg_conf(self, user, router: bool = False) -> str | None:
        """Client config; None for clients imported without their private key.
        Routers: Keenetic/Netcraze firmware answers «invalid I1 value» to signature packets, so their config has
        no I1–I5 at all — those are sent by the client only, the server doesn't need them."""
        if not user["awg_private"]:
            return None
        s = self.settings()
        params = [line for line in awg_param_lines(s) if not line.startswith(("I1 ", "I2 ", "I3 ", "I4 ", "I5 "))] \
            if router else awg_client_lines(s)
        lines = ["[Interface]", f"PrivateKey = {user['awg_private']}", f"Address = {user['awg_ip']}/32",
                 f"DNS = {config.AWG_DNS}", *params,
                 "", "[Peer]", f"PublicKey = {s['awg_public']}",
                 *([f"PresharedKey = {user['awg_psk']}"] if user["awg_psk"] else []),
                 f"Endpoint = {self._host_for_url()}:{config.AWG_PORT}",
                 "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25"]
        return "\n".join(lines) + "\n"

    # --- disk ---
    def request_cleanup(self) -> None:
        """Ask the host to free disk space: the systemd unit that runs `vpn.sh update` sees "cleanup" and cleans."""
        (config.DATA_DIR / "update.request").write_text("cleanup")
        self.db.set("disk_clean_ts", int(time.time()))

    # --- applying ---
    async def apply(self) -> str | None:
        """Regenerate configs and restart the cores. Returns an error text or None."""
        async with self._apply_lock:
            await self.collect_stats()
            users = [u for u in self.db.users(only_enabled=True) if uses(u, "vless")]
            s = self.settings()
            self.xray.write_config(users, s)
            error = await self.xray.check_config()
            if error:
                log.error("xray config error: %s", error)
                return f"Xray: {error}"
            await self.xray.restart()
            try:
                self.db.ensure_awg(config.AWG_SUBNET)
                users = [u for u in self.db.users(only_enabled=True) if uses(u, "awg")]
                if s.get("awg_enabled") == "1" and users:
                    self.awg.write_config(users, s)
                    await self.awg.sync()
                else:
                    await self.awg.stop()
            except Exception as e:  # noqa: BLE001 - Xray keeps working even if AWG can't start
                log.exception("amneziawg apply failed")
                return f"AmneziaWG: {e}"
            return None

    async def collect_stats(self) -> None:
        stats: dict[str, list[int]] = {}
        active: dict[str, tuple[str, str]] = {}  # name -> (via, ip): who did something since the last call
        awg_names = {u["awg_public"]: u["name"] for u in self.db.users() if u["awg_public"]}
        for via, part in (("VLESS", await self.xray.pop_stats()),
                          ("AmneziaWG", await self.awg.pop_stats(awg_names))):
            for name, (up, down) in part.items():
                acc = stats.setdefault(name, [0, 0])
                acc[0] += up
                acc[1] += down
                if up + down > ACTIVE_BYTES:
                    active[name] = (via, "")
        now = int(time.time())
        for public, (handshake, endpoint) in (await self.awg.handshakes()).items():
            if public in awg_names and now - handshake < ONLINE_WINDOW:
                active[awg_names[public]] = ("AmneziaWG", endpoint.rsplit(":", 1)[0])
        day = msk_day(now)
        for name, (up, down) in stats.items():
            if up or down:
                self.db.add_traffic(name, up, down)
                self.db.add_usage(day, name, up + down, 0)
        for name in active:
            self.db.add_usage(day, name, 0, 1)  # called once a minute: one minute online
        self.update_presence(active, now)

    def update_presence(self, active: dict[str, tuple[str, str]], now: int) -> None:
        for u in self.db.users():
            if u["name"] not in active:
                continue
            via, ip = active[u["name"]]
            fresh = not u["last_seen"] or now - u["last_seen"] >= ONLINE_WINDOW  # a new session starts
            self.db.update_user(u["id"], last_seen=now, last_via=via,
                                **({"online_since": now} if fresh or not u["online_since"] else {}),
                                **({"last_ip": ip} if ip else {}))

    @staticmethod
    def presence(user) -> tuple[bool, str]:
        """(online now?, text for the card)"""
        seen = user["last_seen"] if "last_seen" in user.keys() else None
        if not seen:
            return False, "ещё не подключался"
        now = int(time.time())
        if now - seen < ONLINE_WINDOW:
            since = fmt_duration(now - (user["online_since"] or seen))
            ip = f", {user['last_ip']}" if user["last_ip"] else ""
            return True, f"🟢 онлайн {since} — {user['last_via']}{ip}"
        return False, f"🔴 не в сети, был {fmt_ago(seen)} ({user['last_via']})"

    async def enforce_limits(self) -> list[str]:
        """Disable users that are expired or out of traffic. Returns the reasons for notifications."""
        now = int(time.time())
        reasons = []
        for u in self.db.users(only_enabled=True):
            reason = None
            if u["expire_at"] and u["expire_at"] <= now:
                reason = "истёк срок"
            elif u["limit_bytes"] and u["up"] + u["down"] >= u["limit_bytes"]:
                reason = "исчерпан лимит трафика"
            if reason:
                self.db.update_user(u["id"], enabled=0)
                reasons.append(f"{u['name']}: {reason}")
        if reasons:
            await self.apply()
        return reasons

    def user_status(self, user) -> str:
        if not user["enabled"]:
            return "⏸"
        if user["expire_at"] and user["expire_at"] - time.time() < 3 * 86400:
            return "⏳"
        return "✅"
