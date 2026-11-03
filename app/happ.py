"""What Happ gets with the subscription besides the keys: routing rules, an announcement, a support button."""
import base64
import datetime
import json
import time

# Russian sites and IP ranges go directly, everything else through the VPN. Lists: roscomvpn (small files,
# rebuilt daily, made for Happ) — github.com/hydraponique/roscomvpn-geosite and roscomvpn-geoip.
# Bump ROUTING_REV when the profile changes: Happ re-applies a profile only when LastUpdated grows.
ROUTING_REV = 1793664000  # 03.11.2026
ROUTING_PROFILE = {
    "Name": "VPN: РФ напрямую",
    "GlobalProxy": "true",
    "UseChunkFiles": "true",
    "RemoteDns": "8.8.8.8",
    "DomesticDns": "77.88.8.8",
    "RemoteDNSType": "DoH",
    "RemoteDNSDomain": "https://8.8.8.8/dns-query",
    "RemoteDNSIP": "8.8.8.8",
    "DomesticDNSType": "DoH",
    "DomesticDNSDomain": "https://77.88.8.8/dns-query",
    "DomesticDNSIP": "77.88.8.8",
    "Geoipurl": "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geoip/release/geoip.dat",
    "Geositeurl": "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geosite/release/geosite.dat",
    "LastUpdated": str(ROUTING_REV),
    "DnsHosts": {},
    "RouteOrder": "block-proxy-direct",
    # Russian sites, banks, government services, marketplaces — directly, from a Russian IP
    "DirectSites": ["geosite:private", "geosite:category-ru", "geosite:whitelist"],
    "DirectIp": ["geoip:private", "geoip:direct"],
    # these may sit on Russian addresses but are slowed down or blocked in Russia — always through the VPN
    "ProxySites": ["geosite:youtube", "geosite:telegram", "geosite:google-play", "geosite:github"],
    "ProxyIp": [],
    "BlockSites": [],
    "BlockIp": [],
    "DomainStrategy": "IPIfNonMatch",
    "FakeDNS": "false",
}

ANNOUNCE_MAX = 200


def routing_link() -> str:
    data = json.dumps(ROUTING_PROFILE, ensure_ascii=False, separators=(",", ":")).encode()
    return "happ://routing/onadd/" + base64.b64encode(data).decode()


def announce(user, text: str = "", now: float | None = None) -> str:
    """The line Happ shows above the servers: the user's own problems first, then the admin's announcement."""
    now = now or time.time()
    if not user["enabled"]:
        line = "⛔ Доступ отключён — напишите администратору"
    elif user["expire_at"] and user["expire_at"] - now < 3 * 86400:
        days = days_left(user["expire_at"], now)
        when = time.strftime("%d.%m", time.localtime(user["expire_at"]))
        left = "истёк" if days < 0 else "сегодня последний день" if days == 0 else f"осталось дней: {days}"
        line = f"⏳ Доступ до {when} ({left}) — продлите у администратора"
    elif user["limit_bytes"] and user["up"] + user["down"] >= 0.9 * user["limit_bytes"]:
        line = "📶 Трафик почти закончился — напишите администратору"
    else:
        line = text.strip()
    return line[:ANNOUNCE_MAX]


def days_left(ts: float, now: float) -> int:
    """Calendar days from today to the date of ts: 0 — today, 1 — tomorrow."""
    return datetime.date.fromtimestamp(ts).toordinal() - datetime.date.fromtimestamp(now).toordinal()


def b64(text: str) -> str:
    return "base64:" + base64.b64encode(text.encode()).decode()
