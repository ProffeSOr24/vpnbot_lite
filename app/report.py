"""Weekly report: server health, users, reachability from Russia. Every Monday at 10:00 Moscow time and on demand."""
import datetime
import html
import time

import psutil

from . import config
from .service import MSK, Service, fmt_bytes, msk_day
from .watch import STATE_TEXT

WEEK = 7 * 86400
SAMPLE_EVERY = 600   # s between server load samples
GAP = 25 * 60        # s without a sample: the bot or the server was not running
SEND_WEEKDAY, SEND_HOUR = 0, 10  # Monday 10:00 MSK
LIST_LIMIT = 10


class Sampler:
    """Server load every 10 minutes: average CPU over the interval, RAM and disk at the moment."""

    def __init__(self) -> None:
        self._cpu = psutil.cpu_times()
        self._last = 0.0

    @staticmethod
    def _busy(prev, cur) -> float:
        def idle(t):
            return t.idle + getattr(t, "iowait", 0.0)
        total = sum(cur) - sum(prev)
        return max(0.0, min(100.0, 100.0 * (1 - (idle(cur) - idle(prev)) / total))) if total > 0 else 0.0

    def tick(self, service: Service, now: float) -> None:
        if now - self._last < SAMPLE_EVERY:
            return
        cur = psutil.cpu_times()
        cpu = self._busy(self._cpu, cur)
        first = not self._last
        self._cpu, self._last = cur, now
        if first:  # the first interval is the bot's own start-up, not representative
            return
        disk = psutil.disk_usage(str(config.DATA_DIR))
        service.db.add_metric(int(now), cpu, psutil.virtual_memory().percent, disk.percent, disk.used)


def week_key(now: float) -> str:
    year, week, _ = datetime.datetime.fromtimestamp(now, MSK).isocalendar()
    return f"{year}-{week:02d}"


def due(service: Service, now: float) -> bool:
    """Monday 10:00 MSK or later in a week that has no report yet. The first run only remembers the week,
    so updating the bot mid-week does not send a report built from a few hours of data."""
    db = service.db
    if db.get("report_enabled", "1") != "1":
        return False
    key = week_key(now)
    sent = db.get("report_week")
    if sent is None:
        db.set("report_week", key)
        return False
    moment = datetime.datetime.fromtimestamp(now, MSK)
    monday = (moment - datetime.timedelta(days=moment.weekday())).replace(hour=SEND_HOUR, minute=0, second=0,
                                                                          microsecond=0)
    return sent != key and moment >= monday


def mark_sent(service: Service, now: float) -> None:
    service.db.set("report_week", week_key(now))
    service.db.prune_history(int(now))


def _date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, MSK).strftime("%d.%m")


def _hours(minutes: int) -> str:
    return f"{minutes // 60} ч" if minutes >= 120 else f"{minutes} мин"


def _change(cur: int, prev: int) -> str:
    if not prev:
        return ""
    pct = round(100 * (cur - prev) / prev)
    return f" ({'+' if pct >= 0 else ''}{pct}% к прошлой)"


def build(service: Service, now: float | None = None) -> str:
    now = now or time.time()
    db = service.db
    start = now - WEEK
    warn, bad = [], []  # verdict reasons

    # ---------- server ----------
    rows = db.conn.execute("SELECT * FROM metrics WHERE ts >= ? ORDER BY ts", (int(start),)).fetchall()
    first_ever = db.conn.execute("SELECT MIN(ts) FROM metrics").fetchone()[0]
    server = []
    boot = psutil.boot_time()
    gaps = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:]) if b["ts"] - a["ts"] > GAP]
    if rows and now - rows[-1]["ts"] > GAP:
        gaps.append(int(now - rows[-1]["ts"]))
    down_min = sum(gaps) // 60
    uptime = f"аптайм {(now - boot) / 86400:.0f} дн" if now - boot >= 86400 else "перезагружался сегодня"
    if not rows:
        server.append("Данные о нагрузке ещё собираются — полная картина будет в следующей сводке.")
    elif gaps:
        server.append(f"⚠️ Перерывы в работе: {len(gaps)}, всего ~{_hours(down_min)} ({uptime})")
        (bad if down_min > 60 else warn).append(f"перерывы в работе ~{_hours(down_min)}")
    else:
        server.append(f"✅ Работал без перерывов ({uptime})")
    if rows:
        cpu = [r["cpu"] for r in rows]
        ram = [r["ram"] for r in rows]
        cpu_avg, ram_avg = sum(cpu) / len(cpu), sum(ram) / len(ram)
        server.append(f"CPU: в среднем {cpu_avg:.0f}%, пик {max(cpu):.0f}% (ядер: {psutil.cpu_count()})")
        server.append(f"RAM: в среднем {ram_avg:.0f}%, пик {max(ram):.0f}% "
                      f"(из {fmt_bytes(psutil.virtual_memory().total)})")
        if cpu_avg >= 70:
            warn.append(f"процессор загружен в среднем на {cpu_avg:.0f}%")
        if ram_avg >= 85 or max(ram) >= 95:
            warn.append("почти кончается оперативная память")
    disk = psutil.disk_usage(str(config.DATA_DIR))
    grew = f", за неделю {'+' if disk.used >= rows[0]['disk_used'] else '−'}" \
           f"{fmt_bytes(abs(disk.used - rows[0]['disk_used']))}" if rows else ""
    server.append(f"Диск: занято {disk.percent:.0f}% ({fmt_bytes(disk.used)} из {fmt_bytes(disk.total)}){grew}")
    if disk.percent >= 90:
        bad.append(f"диск занят на {disk.percent:.0f}%")
    elif disk.percent >= 80:
        warn.append(f"диск занят на {disk.percent:.0f}%")
    cores = []
    for core in (service.xray, service.awg):
        if core.wanted:
            cores.append(f"{core.name} {'🟢' if core.running else '🔴'}")
            if not core.running:
                bad.append(f"{core.name} не работает")
    if cores:
        server.append("Ядра: " + ", ".join(cores))

    # ---------- reachability ----------
    reach = []
    checks = db.conn.execute("SELECT * FROM reach_log WHERE ts >= ? ORDER BY ts", (int(start),)).fetchall()
    if db.get("watch_enabled", "1") != "1":
        reach.append("Сторож выключен (📊 Сервер → 🔔 Сторож).")
    elif not checks:
        reach.append("Проверок за неделю ещё нет — сторож проверяет раз в час.")
    else:
        fails = [c for c in checks if c["state"] != "ok"]
        if not fails:
            reach.append(f"✅ Доступен всю неделю: {len(checks)} проверок из {len(checks)}")
        else:
            pct = 100 * (len(checks) - len(fails)) / len(checks)
            kinds = {}
            for c in fails:
                kinds[c["state"]] = kinds.get(c["state"], 0) + 1
            names = {"partial": "🟡 не у всех провайдеров", "blocked": "🔴 недоступен из РФ",
                     "down": "⚫️ не отвечал ниоткуда"}
            reach.append(f"⚠️ Доступен в {len(checks) - len(fails)} проверках из {len(checks)} ({pct:.0f}%)")
            reach += [f"{names.get(k, k)}: {n} раз" for k, n in kinds.items()]
            reach.append(f"Последний сбой: {_date(fails[-1]['ts'])} "
                         f"{datetime.datetime.fromtimestamp(fails[-1]['ts'], MSK):%H:%M}")
        last = checks[-1]["state"]
        reach.append(f"Сейчас: {STATE_TEXT.get(last, last)}")
        if last in ("blocked", "down"):
            bad.append("сервер сейчас недоступен из России")
        elif last == "partial" or fails:
            warn.append("были проблемы с доступностью из РФ")

    # ---------- users ----------
    people = []
    users = db.users()
    names = {u["name"] for u in users}
    day0, prev0 = msk_day(start + 86400), msk_day(start - WEEK + 86400)  # the last 7 Moscow days, today included
    week = {r["name"]: r for r in db.conn.execute(
        "SELECT name, SUM(bytes) AS bytes, SUM(minutes) AS minutes FROM usage WHERE day >= ? GROUP BY name",
        (day0,))}
    prev_total = db.conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM usage WHERE day >= ? AND day < ?",
                                 (prev0, day0)).fetchone()[0]
    total = sum(r["bytes"] for n, r in week.items() if n in names)
    active = [u for u in users if u["last_seen"] and u["last_seen"] >= start]
    active_ids = {u["id"] for u in active}
    new = [u for u in users if u["created_at"] >= start]
    people.append(f"Всего: {len(users)}, были в сети за неделю: {len(active)}"
                  + (f", новых: {len(new)}" if new else ""))
    people.append(f"Трафик за неделю: {fmt_bytes(total)}{_change(total, prev_total)}")
    top = sorted((r for n, r in week.items() if n in names and r["bytes"]), key=lambda r: -r["bytes"])[:3]
    if top:
        people.append("\n🏆 <b>Самые активные</b>")
        for i, r in enumerate(top, 1):
            people.append(f"{i}. {html.escape(r['name'])} — {fmt_bytes(r['bytes'])}, в сети {_hours(r['minutes'])}")
    idle = [u for u in users if u["enabled"] and u["id"] not in active_ids]
    if idle:
        people.append("\n💤 <b>Не пользовались за неделю</b>")
        for u in idle[:LIST_LIMIT]:
            seen = f"последний раз {_date(u['last_seen'])}" if u["last_seen"] else "ни разу не подключался"
            people.append(f"• {html.escape(u['name'])} — {seen}")
        if len(idle) > LIST_LIMIT:
            people.append(f"…и ещё {len(idle) - LIST_LIMIT}")
    soon = [u for u in users if u["enabled"] and u["expire_at"] and now <= u["expire_at"] < now + WEEK]
    if soon:
        people.append("\n⏳ <b>Срок кончается на этой неделе</b>")
        people += [f"• {html.escape(u['name'])} — {_date(u['expire_at'])}" for u in soon[:LIST_LIMIT]]
    off = [u for u in users if not u["enabled"]]
    if off:
        people.append(f"\n⏸ Отключены: {', '.join(html.escape(u['name']) for u in off[:LIST_LIMIT])}"
                      + (f" и ещё {len(off) - LIST_LIMIT}" if len(off) > LIST_LIMIT else ""))

    # ---------- verdict ----------
    if bad:
        verdict = "🔴 Требует внимания: " + "; ".join(bad + warn)
    elif warn:
        verdict = "🟡 Хорошо, есть замечания: " + "; ".join(warn)
    else:
        verdict = "🟢 Отлично — всё работает"
    head = f"📋 <b>Сводка за неделю</b> {_date(start + 86400)}–{_date(now)}"
    if first_ever and first_ever > start:
        head += f"\n<i>статистика собирается с {_date(first_ever)}</i>"
    parts = [head, f"<b>Общее состояние:</b> {verdict}",
             "🖥 <b>Сервер</b>\n" + "\n".join(server),
             "📡 <b>Доступность из России</b>\n" + "\n".join(reach),
             "👥 <b>Пользователи</b>\n" + "\n".join(people)]
    return "\n\n".join(parts)[:4000]
