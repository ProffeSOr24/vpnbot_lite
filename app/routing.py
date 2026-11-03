"""«РФ напрямую»: routing rules sent with the subscription, so Russian sites (banks, Госуслуги, marketplaces) go
directly from the user's own Russian IP and everything else through the VPN.

The apps take the rules in the same `routing` header but in different formats, so the client is recognised by its
User-Agent: Happ gets its routing profile, v2RayTun an Xray routing object. Other apps get nothing (Hiddify has the
same thing built in: Settings → Region → Russia — the subscription page tells how)."""
import base64
import json

# --- Happ: its own profile format; geo files from roscomvpn (small, rebuilt daily, made for Russia) ---
# Happ re-applies a profile only when LastUpdated grows: bump it whenever the profile changes.
HAPP_REV = 1793750400  # 04.11.2026
HAPP_PROFILE = {
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
    "LastUpdated": str(HAPP_REV),
    "DnsHosts": {},
    "RouteOrder": "block-proxy-direct",
    # Russian sites and IP ranges — directly
    "DirectSites": ["geosite:private", "geosite:category-ru", "geosite:whitelist"],
    "DirectIp": ["geoip:private", "geoip:direct"],
    # may sit on Russian addresses but are slowed down in Russia — always through the VPN
    "ProxySites": ["geosite:youtube", "geosite:telegram", "geosite:google-play", "geosite:github"],
    "ProxyIp": [],
    "BlockSites": [],
    "BlockIp": [],
    "DomainStrategy": "IPIfNonMatch",
    "FakeDNS": "false",
}

# --- v2RayTun: an Xray routing object, geo files bundled with the app (v2fly: category-ru, geoip ru) ---
# Rules apply top-down; in one rule `domain` and `ip` must BOTH match, so domains and IPs are separate rules.
V2RAYTUN_ROUTING = {
    "name": "VPN: РФ напрямую",
    "id": "5F0C2E7A-6B1D-4C8E-9A3F-2D7B1E4C9A01",
    "domainStrategy": "IPIfNonMatch",
    "domainMatcher": "hybrid",
    "balancers": [],
    "rules": [
        {"__name__": "YouTube и Telegram — через VPN", "id": "0B6E4F1A-2C3D-4E5F-8A9B-1C2D3E4F5A60",
         "type": "field", "outboundTag": "proxy",
         "domain": ["geosite:youtube", "geosite:telegram", "geosite:google-play", "geosite:github"]},
        {"__name__": "Сайты РФ — напрямую", "id": "1C7F5A2B-3D4E-4F60-9B0C-2D3E4F5A6B71",
         "type": "field", "outboundTag": "direct",
         "domain": ["geosite:private", "geosite:category-ru", "domain:ru", "domain:su", "domain:xn--p1ai"]},
        {"__name__": "IP РФ — напрямую", "id": "2D806B3C-4E5F-4071-8C1D-3E4F5A6B7C82",
         "type": "field", "outboundTag": "direct", "ip": ["geoip:private", "geoip:ru"]},
    ],
}


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()).decode()


def client(user_agent: str) -> str:
    """happ | v2raytun | '' (an app without routing in the subscription)."""
    ua = user_agent.lower()
    if "happ" in ua:
        return "happ"
    if "v2raytun" in ua:
        return "v2raytun"
    return ""


def header(user_agent: str) -> str | None:
    """Value of the `routing` header for this app, or None."""
    kind = client(user_agent)
    if kind == "happ":
        return "happ://routing/onadd/" + _b64(HAPP_PROFILE)
    if kind == "v2raytun":
        return _b64(V2RAYTUN_ROUTING)
    return None
