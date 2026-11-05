"""Keeping the server reachable: DuckDNS follows the server's IP, check-host.net shows whether Russia can reach it."""
import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

CHECK_HOST = "https://check-host.net"
GLOBALPING = "https://api.globalping.io/v1"
GLOBALPING_PROBES = 10  # home providers in Russia per check (free API: 250 probe results an hour)
PROVIDERS = {"mts": "МТС", "vimpelcom": "Билайн", "pvimpelcom": "Билайн", "beeline": "Билайн",
             "rostelecom": "Ростелеком", "er-telecom": "Дом.ру", "moscow city telephone network": "МГТС",
             "megafon": "МегаФон", "tele2": "Tele2", "t2 mobile": "T2", "ttk": "ТТК"}
ABROAD = ("de", "nl", "fi", "fr", "pl")  # control nodes: if these fail too, the server itself is down
HEADERS = {"Accept": "application/json"}


async def public_ip(session: aiohttp.ClientSession) -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return (await r.text()).strip()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    return None


async def duckdns_update(session: aiohttp.ClientSession, domain: str, token: str, ip: str) -> bool:
    """Point <name>.duckdns.org at ip. DuckDNS answers OK or KO."""
    name = domain.removesuffix(".duckdns.org")
    try:
        async with session.get("https://www.duckdns.org/update", params={"domains": name, "token": token, "ip": ip},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            return (await r.text()).strip() == "OK"
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("duckdns: %s", e)
        return False


class Reachability:
    """Is the server reachable from Russia: check-host.net (2 datacenter nodes + a foreign control) and Globalping
    (home internet providers). Both run in parallel."""

    def __init__(self) -> None:
        self._nodes: tuple[float, dict] = (0.0, {})

    async def _get(self, session: aiohttp.ClientSession, path: str, **params) -> dict:
        async with session.get(CHECK_HOST + path, params=params, headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json(content_type=None)

    async def nodes(self, session: aiohttp.ClientSession) -> dict:
        loaded, nodes = self._nodes
        if not nodes or time.time() - loaded > 86400:
            nodes = (await self._get(session, "/nodes/hosts")).get("nodes", {})
            self._nodes = (time.time(), nodes)
        return nodes

    async def _check_host(self, session: aiohttp.ClientSession, host: str, port: int) -> tuple[list, list]:
        """check-host.net: Russian nodes (ping + TCP) and up to 4 foreign ones as the control."""
        nodes = await self.nodes(session)
        ru = [n for n, info in nodes.items() if info.get("location", [""])[0] == "ru"]
        abroad = [n for n, info in nodes.items() if info.get("location", [""])[0] in ABROAD][:4]
        picked = [("node", n) for n in ru + abroad]
        ids = {}
        for kind, target in (("ping", host), ("tcp", f"{host}:{port}")):
            async with session.get(f"{CHECK_HOST}/check-{kind}", params=[("host", target), *picked],
                                   headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
                ids[kind] = (await r.json(content_type=None))["request_id"]
        results = {}
        for _ in range(30):  # nodes usually report within 5–20 s, but check-host can take over a minute
            await asyncio.sleep(3)
            results = {k: await self._get(session, f"/check-result/{rid}") for k, rid in ids.items()}
            if all(v is not None for res in results.values() for v in res.values()):
                break

        def ok(kind: str, node: str) -> bool | None:
            """True/False, or None when the node has not answered (check-host is slow or the node is busy)."""
            res = results.get(kind, {}).get(node)
            if res is None:
                return None
            if not res or not res[0]:
                return False
            if kind == "ping":
                return any(x and x[0] == "OK" for x in res[0])
            return isinstance(res[0], dict) and "time" in res[0]

        rows = [(nodes[n].get("asn", "?"), nodes[n].get("location", ["", "", "?"])[2], ok("ping", n), ok("tcp", n))
                for n in ru]
        return rows, [r for r in (ok("tcp", n) for n in abroad) if r is not None]

    async def _globalping(self, session: aiohttp.ClientSession, host: str, port: int) -> list:
        """Globalping: TCP connects from home internet providers in Russia (MTS, Beeline, Rostelecom, Dom.ru…) —
        where the users are, unlike check-host's two datacenter nodes. Rows have no ping ("-")."""
        body = {"type": "ping", "target": host, "locations": [{"magic": "RU+eyeball-network", "limit": GLOBALPING_PROBES}],
                "measurementOptions": {"packets": 2, "protocol": "TCP", "port": port}}
        async with session.post(f"{GLOBALPING}/measurements", json=body, headers=HEADERS,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            mid = (await r.json(content_type=None))["id"]
        data = {}
        for _ in range(15):
            await asyncio.sleep(3)
            async with session.get(f"{GLOBALPING}/measurements/{mid}", headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
            if data.get("status") != "in-progress":
                break
        rows = []
        for item in data.get("results", []):
            probe, res = item.get("probe", {}), item.get("result", {})
            loss = (res.get("stats") or {}).get("loss")
            ok = None if res.get("status") != "finished" or loss is None else loss < 100
            rows.append((provider_name(probe.get("network", "?")), probe.get("city", "?"), "-", ok))
        return rows

    async def check(self, session: aiohttp.ClientSession, host: str, port: int) -> dict:
        """{'state': ok|partial|blocked|down|error, 'ru': [(provider, city, ping_ok|'-', tcp_ok)], 'abroad': n_ok/n}.
        Both sources run in parallel; either may fail alone."""
        ch, gp = await asyncio.gather(self._check_host(session, host, port), self._globalping(session, host, port),
                                      return_exceptions=True)
        errors = []
        if isinstance(ch, BaseException):
            errors.append(f"check-host.net: {ch or type(ch).__name__}")
            ch = ([], [])
        if isinstance(gp, BaseException):
            errors.append(f"Globalping: {gp or type(gp).__name__}")
            gp = []
        ru_rows, abroad_res = ch[0] + gp, ch[1]
        abroad_ok = sum(abroad_res)
        ru_known = [t for _, _, _, t in ru_rows if t is not None]
        ru_ok, failed = sum(ru_known), len(ru_known) - sum(ru_known)
        now = int(time.time())
        if not ru_known:
            return {"state": "error", "error": "; ".join(errors) or "проверочные узлы не ответили вовремя",
                    "ru": ru_rows, "time": now}
        if abroad_res and not abroad_ok and not ru_ok:
            state = "down"
        elif failed == 0 or (len(ru_known) >= 5 and failed == 1):  # one flaky probe out of many is noise
            state = "ok"
        elif ru_ok:
            state = "partial"
        elif abroad_ok or not abroad_res:
            state = "blocked"  # Russia fails everywhere; no control is fine when Globalping alone saw it
        else:
            return {"state": "error", "error": "узлы за рубежом не ответили — нечем сравнить", "ru": ru_rows,
                    "time": now}
        return {"state": state, "ru": ru_rows, "ru_summary": f"{ru_ok}/{len(ru_known)}",
                "abroad": f"{abroad_ok}/{len(abroad_res)}" if abroad_res else "", "time": now}


STATE_TEXT = {
    "ok": "🟢 доступен из России",
    "partial": "🟡 из России доступен не у всех провайдеров — похоже на начало блокировки",
    "blocked": "🔴 недоступен из России — IP, видимо, заблокирован",
    "down": "⚫️ не отвечает ниоткуда — сервер или сеть хостера недоступны",
    "error": "⚪️ проверить не удалось",
}


def _mark(value: bool | None) -> str:
    return "❔" if value is None else "✅" if value else "❌"


def provider_name(network: str) -> str:
    """Russian name for the well-known providers: «PVimpelCom» → «Билайн»."""
    low = network.lower()
    for key, name in PROVIDERS.items():
        if key in low:
            return name
    return network[:30]


def describe(result: dict, host: str, port: int) -> str:
    lines = [f"<b>Доступность {host}</b>: {STATE_TEXT.get(result['state'], result['state'])}"]
    if result.get("error"):
        lines.append(result["error"])
    if result.get("ru_summary"):
        lines.append(f"Из России: подключение проходит в {result['ru_summary']} проверок")
    for name, city, ping_ok, tcp_ok in result.get("ru", []):
        ping = "" if ping_ok == "-" else f"ping {_mark(ping_ok)}, "
        lines.append(f"{_mark(tcp_ok)} {city}, {name}: {ping}порт {port}")
    if result.get("abroad"):
        lines.append(f"Из-за рубежа (контроль): {result['abroad']} узлов отвечают")
    return "\n".join(lines)
