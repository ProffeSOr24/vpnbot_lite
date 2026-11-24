"""Bounded, opt-in throughput measurements between this VPS and Cloudflare."""
import asyncio
import datetime
import json
import statistics
import time

import aiohttp

BASE = "https://speed.cloudflare.com"
SIZE = 25_000_000
INTERVAL = 6 * 3600
CONFIRM = 600


async def measure():
    timeout = aiohttp.ClientTimeout(total=120)
    headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, auto_decompress=False) as session:
        start = time.monotonic()
        async with session.get(BASE + "/__down", params={"bytes": SIZE}, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Cloudflare HTTP {response.status}")
            received = 0
            async for chunk in response.content.iter_chunked(65536):
                received += len(chunk)
                if received > SIZE:
                    break
            if received != SIZE:
                raise ValueError("неполная или неверная загрузка")
        down = SIZE * 8 / (time.monotonic() - start) / 1e6
        payload = bytes(SIZE)
        start = time.monotonic()
        async with session.post(BASE + "/__up", data=payload, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Cloudflare HTTP {response.status}")
            await response.content.read(4096)
        up = SIZE * 8 / (time.monotonic() - start) / 1e6
    return up, down


class SpeedMonitor:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    @property
    def enabled(self):
        return self.db.get("speed_enabled") == "1"

    def toggle(self):
        enabled = not self.enabled
        self.db.set("speed_enabled", int(enabled))
        self.db.set("speed_schedule", json.dumps({"start": int(time.time())}))

    def describe(self):
        last = json.loads(self.db.get("speed_last") or "{}")
        if not last:
            return "⚡ Cloudflare: замеров ещё нет"
        stamp = datetime.datetime.fromtimestamp(last["time"], datetime.timezone(datetime.timedelta(hours=3)))
        head = f"⚡ Cloudflare · {stamp:%d.%m %H:%M} МСК"
        if "error" in last:
            return f"{head}\nЗамер не удался ({last['error']})"
        return f"{head}\n↑ VPS → Cloudflare: {last['up']:.1f} Мбит/с\n↓ Cloudflare → VPS: {last['down']:.1f} Мбит/с"

    async def sample(self):
        result = {"time": int(time.time())}
        try:
            result["up"], result["down"] = await measure()
        except ValueError as exc:
            result["error"] = str(exc)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            result["error"] = type(exc).__name__
        self.db.set("speed_last", json.dumps(result))
        return result

    async def tick(self):
        if not self.enabled or self.lock.locked():
            return
        async with self.lock:
            state = json.loads(self.db.get("speed_schedule") or "{}")
            now = int(time.time())
            if now < state.get("next", 0):
                return
            state.setdefault("start", now)
            state["next"] = now + INTERVAL
            self.db.set("speed_schedule", json.dumps(state))
            result = await self.sample()
            note = self.evaluate(state, result, now)
            self.db.set("speed_schedule", json.dumps(state))
            return note

    @staticmethod
    def evaluate(state, result, now):
        if "error" in result:
            state.pop("pending", None)
            return
        up = result["up"]
        history = state.setdefault("history", [])
        note = None
        if now - state["start"] >= 2 * 86400 and len(history) >= 8:
            baseline = statistics.median(history)
            if up < baseline / 2:
                if state.get("alarm"):
                    return
                if state.get("pending"):
                    state.pop("pending")
                    state["alarm"] = True
                    return f"⚠️ Cloudflare: просадка отдачи подтверждена. {up:.1f} Мбит/с, обычно {baseline:.1f}."
                state["pending"] = True
                state["next"] = now + CONFIRM
                return
            state.pop("pending", None)
            if state.pop("alarm", False):
                note = f"✅ Отдача VPS → Cloudflare восстановилась: {up:.1f} Мбит/с."
        state["history"] = (history + [up])[-28:]
        return note
