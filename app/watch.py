"""Keeping the server reachable: DuckDNS follows the server's IP, check-host.net shows whether Russia can reach it."""
import asyncio
import logging
import time

import aiohttp

from . import config

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


CONTROL = ("ya.ru", "www.google.com")  # asked from a failed node: does it reach anything at all?


class Reachability:
    """Is the server reachable from Russia: check-host.net (2 datacenter nodes + a foreign control) and Globalping
    (home internet providers). Both run in parallel. A node that fails is then asked about ya.ru and google.com
    (no internet there, or no foreign sites — not our problem), and a real failure is diagnosed: whole IP, one port,
    the name (SNI) or DNS."""

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

    # ---------- check-host.net ----------
    async def _ch_run(self, session: aiohttp.ClientSession, kinds: dict[str, tuple[str, str]],
                      nodes: list[str]) -> dict:
        """{label: (kind, target)} from the given nodes → {label: {node: raw result}}."""
        picked = [("node", n) for n in nodes]
        ids = {}
        for label, (kind, target) in kinds.items():
            async with session.get(f"{CHECK_HOST}/check-{kind}", params=[("host", target), *picked],
                                   headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
                ids[label] = (await r.json(content_type=None))["request_id"]
        results = {}
        for _ in range(30):  # nodes usually report within 5–20 s, but check-host can take over a minute
            await asyncio.sleep(3)
            results = {k: await self._get(session, f"/check-result/{rid}") for k, rid in ids.items()}
            if all(v is not None for res in results.values() for v in res.values()):
                break
        return results

    @staticmethod
    def _ch_ok(results: dict, label: str, node: str) -> bool | None:
        """True/False, or None when the node has not answered (check-host is slow or the node is busy)."""
        res = (results.get(label) or {}).get(node)
        if res is None:
            return None
        if not res or not res[0]:
            return False
        if label == "ping":
            return any(x and x[0] == "OK" for x in res[0])
        return isinstance(res[0], dict) and "time" in res[0]

    async def _check_host(self, session: aiohttp.ClientSession, host: str, port: int) -> tuple[list, list]:
        """check-host.net: Russian nodes (ping + TCP) and up to 4 foreign ones as the control."""
        nodes = await self.nodes(session)
        ru = [n for n, info in nodes.items() if info.get("location", [""])[0] == "ru"]
        abroad = [n for n, info in nodes.items() if info.get("location", [""])[0] in ABROAD][:4]
        res = await self._ch_run(session, {"ping": ("ping", host), "tcp": ("tcp", f"{host}:{port}")}, ru + abroad)
        rows = [{"name": nodes[n].get("asn", "?"), "city": nodes[n].get("location", ["", "", "?"])[2],
                 "ping": self._ch_ok(res, "ping", n), "tcp": self._ch_ok(res, "tcp", n), "src": "ch", "ref": n}
                for n in ru]
        return rows, [r for r in (self._ch_ok(res, "tcp", n) for n in abroad) if r is not None]

    async def _ch_tcp(self, session: aiohttp.ClientSession, targets: list[str], nodes: list[str]) -> dict:
        """{target: {node: ok}} — TCP connects from the given check-host nodes."""
        res = await self._ch_run(session, {t: ("tcp", t) for t in targets}, nodes)
        return {t: {n: self._ch_ok(res, t, n) for n in nodes} for t in targets}

    # ---------- Globalping ----------
    async def _gp(self, session: aiohttp.ClientSession, body: dict) -> tuple[str, list]:
        """Run a measurement; (id, results). locations may be an earlier measurement's id: the same probes."""
        async with session.post(f"{GLOBALPING}/measurements", json=body, headers=HEADERS,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}")
            mid = (await r.json(content_type=None))["id"]
        data = {}
        for _ in range(20):
            await asyncio.sleep(3)
            async with session.get(f"{GLOBALPING}/measurements/{mid}", headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
            if data.get("status") != "in-progress":
                break
        return mid, data.get("results", [])

    @staticmethod
    def _gp_tcp_ok(item: dict) -> bool | None:
        res = item.get("result", {})
        loss = (res.get("stats") or {}).get("loss")
        return None if res.get("status") != "finished" or loss is None else loss < 100

    async def _globalping(self, session: aiohttp.ClientSession, host: str, port: int) -> tuple[str, list]:
        """Globalping: TCP connects from home internet providers in Russia (MTS, Beeline, Rostelecom, Dom.ru…) —
        where the users are, unlike check-host's two datacenter nodes. Rows have no ping ("-")."""
        mid, results = await self._gp(session, {
            "type": "ping", "target": host,
            "locations": [{"magic": "RU+eyeball-network", "limit": GLOBALPING_PROBES}],
            "measurementOptions": {"packets": 2, "protocol": "TCP", "port": port}})
        rows = []
        for i, item in enumerate(results):
            probe = item.get("probe", {})
            rows.append({"name": provider_name(probe.get("network", "?")), "city": probe.get("city", "?"),
                         "ping": "-", "tcp": self._gp_tcp_ok(item), "src": "gp", "ref": i,
                         "probe": (probe.get("asn"), probe.get("city"))})
        return mid, rows

    async def _gp_same(self, session: aiohttp.ClientSession, mid: str, rows: list, body: dict, judge) -> dict:
        """The same probes as measurement mid: {row index: judge(result item)}. Matched by position, checked by
        ASN and city — a probe that went offline in between shifts nothing."""
        _, results = await self._gp(session, {**body, "locations": mid})
        out, pool = {}, list(results)
        for row in rows:
            for item in pool:
                probe = item.get("probe", {})
                if (probe.get("asn"), probe.get("city")) == row["probe"]:
                    out[row["ref"]] = judge(item)
                    pool.remove(item)
                    break
        return out

    async def _gp_tcp(self, session, mid, rows, target: str, port: int) -> dict:
        return await self._gp_same(session, mid, rows, {
            "type": "ping", "target": target,
            "measurementOptions": {"packets": 2, "protocol": "TCP", "port": port}}, self._gp_tcp_ok)

    async def _gp_tls(self, session, mid, rows, host: str, port: int) -> dict:
        """HTTPS to host:port with its name — with REALITY on our own domain the probe gets our site and its
        certificate. TCP up but TLS broken means the name (SNI) is filtered."""
        def judge(item):
            res = item.get("result", {})
            if res.get("status") == "finished":
                return True
            raw = (res.get("rawOutput") or "").lower()
            return None if "tcp connection" in raw or not raw else False
        return await self._gp_same(session, mid, rows, {
            "type": "http", "target": host,
            "measurementOptions": {"protocol": "HTTPS", "port": port, "request": {"method": "HEAD", "path": "/"}}},
            judge)

    async def _gp_dns(self, session, mid, rows, host: str) -> dict:
        def judge(item):
            res = item.get("result", {})
            if res.get("status") != "finished":
                return None
            return [a.get("value") for a in res.get("answers", []) if a.get("type") == "A"]
        return await self._gp_same(session, mid, rows, {
            "type": "dns", "target": host, "measurementOptions": {"query": {"type": "A"}}}, judge)

    # ---------- the check ----------
    async def check(self, session: aiohttp.ClientSession, host: str, port: int, *, alt_port: int = 0,
                    sni: bool = False, entry_ip: str = "", exit_ip: str = "") -> dict:
        """{'state': ok|partial|blocked|down|error, 'ru': [[provider, city, ping_ok|'-', tcp_ok, note]],
        'abroad': n_ok/n, 'diagnosis': [...], 'exit': {...}}. Sources run in parallel; any may fail alone."""
        ch, gp = await asyncio.gather(self._check_host(session, host, port), self._globalping(session, host, port),
                                      return_exceptions=True)
        errors = []
        if isinstance(ch, BaseException):
            errors.append(f"check-host.net: {ch or type(ch).__name__}")
            ch = ([], [])
        mid = ""
        if isinstance(gp, BaseException):
            errors.append(f"Globalping: {gp or type(gp).__name__}")
            gp = []
        else:
            mid, gp = gp
        rows, abroad_res = ch[0] + gp, ch[1]
        gp_rows = [r for r in rows if r["src"] == "gp"]
        ch_failed = [r["ref"] for r in rows if r["src"] == "ch" and r["tcp"] is False]
        gp_failed = [r for r in gp_rows if r["tcp"] is False]

        async def nothing():
            return {}

        def maybe(cond, coro):
            if cond:
                return coro
            coro.close()
            return nothing()

        # 1. failed nodes: can they reach anything? 2. the name (SNI) from every probe. The exit IP is never probed:
        # the same probe touching the entry and the exit would tie the two addresses together
        step = await asyncio.gather(
            maybe(ch_failed, self._ch_tcp(session, [f"{c}:443" for c in CONTROL], ch_failed)),
            maybe(mid and gp_failed, self._gp_tcp(session, mid, gp_failed, CONTROL[0], 443)),
            maybe(mid and gp_failed, self._gp_tcp(session, mid, gp_failed, CONTROL[1], 443)),
            maybe(mid and sni, self._gp_tls(session, mid, gp_rows, host, port)),
            return_exceptions=True)
        step = [{} if isinstance(x, BaseException) else x for x in step]
        ch_ctl, gp_ya, gp_google, tls = step
        for r in rows:
            r["note"] = None
            if r["tcp"] is False:
                if r["src"] == "ch":
                    ya, google = (ch_ctl.get(f"{c}:443", {}).get(r["ref"]) for c in CONTROL)
                else:
                    ya, google = gp_ya.get(r["ref"]), gp_google.get(r["ref"])
                if ya is False and google is False:
                    r["note"] = "offline"
                elif ya and google is False:
                    r["note"] = "no_foreign"
            elif r["tcp"] and tls.get(r["ref"]) is False and r["src"] == "gp":
                r["note"] = "sni"

        diagnosis = []
        sni_n = sum(r["note"] == "sni" for r in rows)
        if sni_n:
            diagnosis.append(f"🟠 Блокировка по имени (SNI): у {sni_n} узлов порт открыт, но соединение с именем "
                             f"{host} обрывается → поможет другой домен")

        counted = [r for r in rows if r["note"] not in ("offline", "no_foreign")]
        known = [(r["tcp"] and r["note"] != "sni") for r in counted if r["tcp"] is not None]
        ru_ok, failed = sum(known), len(known) - sum(known)
        skipped = len(rows) - len(counted)
        out_rows = [[r["name"], r["city"], r["ping"], r["tcp"], r["note"]] for r in rows]
        exit_info = {}
        if exit_ip:
            exit_info = {"ip": exit_ip, "entry": entry_ip, "same": bool(entry_ip) and exit_ip == entry_ip}
        now = int(time.time())
        base = {"ru": out_rows, "time": now, "host": host, "port": port, "diagnosis": diagnosis, "exit": exit_info,
                "skipped": skipped}
        abroad_ok = sum(abroad_res)
        if not known:
            return {**base, "state": "error", "error": "; ".join(errors) or "проверочные узлы не ответили вовремя"}
        if abroad_res and not abroad_ok and not ru_ok:
            state = "down"
        elif failed == 0 or (len(known) >= 5 and failed == 1 and not sni_n):  # one flaky probe out of many
            state = "ok"
        elif ru_ok:
            state = "partial"
        elif abroad_ok or not abroad_res:
            state = "blocked"  # Russia fails everywhere; no control is fine when Globalping alone saw it
        else:
            return {**base, "state": "error", "error": "узлы за рубежом не ответили — нечем сравнить"}

        # 3. what exactly is blocked, from the nodes that really failed
        real = [r for r in rows if r["tcp"] is False and not r["note"]]
        if real and state in ("partial", "blocked"):
            alt_target = f"{host}:{alt_port}"
            gp_real = [r for r in real if r["src"] == "gp"]
            ch_real = [r["ref"] for r in real if r["src"] == "ch"]
            alt_ch, alt_gp, dns = await asyncio.gather(
                maybe(alt_port and ch_real, self._ch_tcp(session, [alt_target], ch_real)),
                maybe(mid and alt_port and gp_real, self._gp_tcp(session, mid, gp_real, host, alt_port)),
                maybe(mid and gp_real and entry_ip and entry_ip != host, self._gp_dns(session, mid, gp_real, host)),
                return_exceptions=True)
            alt_ch, alt_gp, dns = ({} if isinstance(x, BaseException) else x for x in (alt_ch, alt_gp, dns))
            kinds = {"ip": 0, "port": 0, "dns": 0}
            for r in real:
                answers = dns.get(r["ref"]) if r["src"] == "gp" else None
                alt = (alt_ch.get(alt_target, {}).get(r["ref"]) if r["src"] == "ch" else alt_gp.get(r["ref"]))
                if answers is not None and entry_ip not in answers:
                    kinds["dns"] += 1
                elif alt is False:
                    kinds["ip"] += 1
                elif alt:
                    kinds["port"] += 1
            if kinds["ip"]:
                diagnosis.append(f"🔴 IP заблокирован целиком: с {kinds['ip']} из {len(real)} узлов с отказом не "
                                 f"открывается и порт {alt_port} → нужен новый IP")
            if kinds["port"]:
                diagnosis.append(f"🟠 Закрыт только порт {port}: с {kinds['port']} из {len(real)} узлов с отказом "
                                 f"порт {alt_port} открывается → поможет смена порта")
            if kinds["dns"]:
                diagnosis.append(f"🟠 Подмена DNS: с {kinds['dns']} из {len(real)} узлов имя {host} указывает не на "
                                 f"{entry_ip}")
        return {**base, "state": state, "ru_summary": f"{ru_ok}/{len(known)}",
                "abroad": f"{abroad_ok}/{len(abroad_res)}" if abroad_res else ""}


STATE_TEXT = {
    "ok": "🟢 доступен из России",
    "partial": "🟡 из России доступен не у всех провайдеров — похоже на начало блокировки",
    "blocked": "🔴 недоступен из России",
    "down": "⚫️ не отвечает ниоткуда — сервер или сеть хостера недоступны",
    "error": "⚪️ проверить не удалось",
}
NOTES = {
    "offline": "⚠️ {city}, {name}: у самого узла нет интернета (не открывается даже ya.ru) — не учитывается",
    "no_foreign": "⚠️ {city}, {name}: провайдер закрыл зарубежные сайты (google.com тоже не открывается) — "
                  "не учитывается",
    "sni": "❌ {city}, {name}: порт {port} открыт, но соединение с именем сервера обрывается",
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
    host, port = result.get("host", host), result.get("port", port)
    lines = [f"<b>Доступность {host}</b>: {STATE_TEXT.get(result['state'], result['state'])}"]
    if result.get("error"):
        lines.append(result["error"])
    ex = result.get("exit") or {}
    if ex.get("same") and config.DDNS_IP:
        lines.append(f"⚠️ Сервер выходит в интернет через входной IP {ex['ip']} — сайты видят адрес, к которому "
                     "подключаются люди. Проверьте у хостера, какой IP основной.")
    elif ex.get("same"):
        lines.append(f"ℹ️ Вход и выход через один IP {ex['ip']} — российские сайты видят адрес подключения")
    elif ex.get("ip") and ex.get("entry"):
        lines.append(f"Вход: {ex['entry']} · выход: {ex['ip']} (сайты видят выход, к нему никто не подключается)")
    if result.get("ru_summary"):
        skipped = result.get("skipped")
        lines.append(f"Из России: подключение проходит в {result['ru_summary']} проверок"
                     + (f" (не учтено узлов с проблемами у провайдера: {skipped})" if skipped else ""))
    for row in result.get("ru", []):
        name, city, ping_ok, tcp_ok = row[:4]
        note = row[4] if len(row) > 4 else None
        if note in NOTES:
            lines.append(NOTES[note].format(city=city, name=name, port=port))
            continue
        ping = "" if ping_ok == "-" else f"ping {_mark(ping_ok)}, "
        lines.append(f"{_mark(tcp_ok)} {city}, {name}: {ping}порт {port}")
    if result.get("diagnosis"):
        lines.append("<b>Что блокируют:</b>")
        lines += result["diagnosis"]
    if result.get("abroad"):
        lines.append(f"Из-за рубежа (контроль): {result['abroad']} узлов отвечают")
    return "\n".join(lines)
