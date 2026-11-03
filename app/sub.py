"""HTTP subscription server: /sub/<token> returns links for Happ / v2RayTun / Hiddify / v2rayN, and a page with
instructions when opened in a browser."""
import base64
import datetime
import html
import io
import time
from string import Template

import qrcode
from aiohttp import web

from .service import Service, fmt_bytes, uses

HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_APK = "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"
HAPP_WINDOWS = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"
AWG_IOS = "https://apps.apple.com/app/amneziawg/id6478942365"
AWG_ANDROID = "https://play.google.com/store/apps/details?id=org.amnezia.awg"

PAGE = Template("""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>VPN · $name</title>
<style>
:root{--bg:#f2f4f8;--card:#fff;--text:#14161a;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--accent2:#1d4ed8;
--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f14;--card:#171a21;--text:#eef0f4;--muted:#9aa3b2;
--line:#262a33;--chip:#1e2433}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:20px 16px 40px}
.card{background:var(--card);border-radius:18px;padding:18px;margin:14px 0;box-shadow:0 1px 2px #0000000d}
.head{display:flex;align-items:center;gap:14px}.logo{width:52px;height:52px;border-radius:14px;
background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:grid;place-items:center;font-size:26px;flex:none}
h1{font-size:21px;margin:0}h2{font-size:17px;margin:0 0 12px}.muted{color:var(--muted);font-size:14px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:600;color:#fff}
.ok{background:var(--ok)}.bad{background:var(--bad)}.warn{background:var(--warn)}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.stat{background:var(--chip);border-radius:12px;padding:10px 12px}.stat b{display:block;font-size:17px}
.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;background:var(--accent);border-radius:99px}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:14px;border:0;
border-radius:14px;background:var(--accent);color:#fff;font-size:17px;font-weight:600;text-decoration:none;
cursor:pointer;margin-top:10px}.btn:active{background:var(--accent2)}
.btn.alt{background:var(--chip);color:var(--text)}
.apps{display:grid;grid-template-columns:1fr 1fr;gap:8px}.apps a{padding:11px;font-size:15px;margin:0}
ol{margin:0;padding-left:22px}ol li{margin:6px 0}
.qr{text-align:center}.qr img{width:200px;height:200px;border-radius:12px;background:#fff;padding:8px}
code{display:block;word-break:break-all;font-size:12px;background:var(--chip);padding:10px;border-radius:10px;
margin-top:8px}details{margin-top:10px}summary{cursor:pointer;color:var(--accent);font-weight:600}
.faq b{display:block;margin-top:10px}.foot{text-align:center;color:var(--muted);font-size:13px;margin-top:20px}
</style></head><body><div class="wrap">

<div class="card"><div class="head"><div class="logo">🛡</div><div>
<h1>$name</h1><div><span class="badge $status_class">$status</span></div></div></div>
<div class="stats"><div class="stat"><span class="muted">Действует</span><b>$expire</b></div>
<div class="stat"><span class="muted">Трафик</span><b>$used</b><span class="muted">$limit</span>$bar</div></div>
</div>
$vless_block
$awg_block
<div class="foot">Ссылка личная — не пересылайте её другим людям.</div>
</div>
<script>
function copy(id,btn){navigator.clipboard.writeText(document.getElementById(id).textContent).then(function(){
var t=btn.textContent;btn.textContent='✅ Скопировано';setTimeout(function(){btn.textContent=t},1800)})}
</script></body></html>""")

VLESS_BLOCK = Template("""
<div class="card"><h2>📱 Подключение за 2 шага</h2>
<ol><li><b>Установите Happ</b>:</li></ol>
<div class="apps"><a class="btn alt" href="$happ_ios">🍏 iPhone / iPad</a>
<a class="btn alt" href="$happ_android">🤖 Android</a>
<a class="btn alt" href="$happ_apk">📦 Android (APK)</a>
<a class="btn alt" href="$happ_windows">💻 Windows</a></div>
<p class="muted">Нет Happ в App Store? В российском App Store его может не быть — нужен Apple ID другой страны.</p>
<ol start="2"><li><b>Нажмите кнопку</b> — подписка добавится в Happ сама:</li></ol>
<a class="btn" href="$deeplink">➕ Добавить в Happ</a>
<button class="btn alt" onclick="copy('sub',this)">📋 Скопировать ссылку</button>
<code id="sub">$sub</code>
<p class="muted">Затем в Happ выберите сервер и нажмите большую кнопку включения.</p>
<details><summary>Показать QR-код (для другого устройства)</summary>
<div class="qr"><img src="$sub?qr=1" alt="QR-код подписки"></div>
<p class="muted">В Happ: «+» → «Сканировать QR-код».</p></details>
</div>
<div class="card faq"><h2>❓ Если не работает</h2>
<b>Не подключается</b><span class="muted">В Happ потяните список вниз, чтобы обновить подписку, и выберите
другой сервер: XHTTP вместо Reality или наоборот. На мобильном интернете обычно лучше работает XHTTP.</span>
<b>Банк или сайт пишет про VPN</b><span class="muted">Некоторые российские сервисы не работают с зарубежного
IP — на время выключите VPN.</span>
<b>Медленно</b><span class="muted">Попробуйте другой сервер из списка и Wi-Fi вместо мобильной сети.</span>
</div>""")

AWG_BLOCK = Template("""
<div class="card"><h2>📡 AmneziaWG</h2>
<ol><li><b>Установите AmneziaWG</b>:</li></ol>
<div class="apps"><a class="btn alt" href="$awg_ios">🍏 iPhone / iPad</a>
<a class="btn alt" href="$awg_android">🤖 Android</a></div>
<ol start="2"><li><b>Скачайте конфиг</b> и откройте его в AmneziaWG (или в приложении: «+» → «Импорт из файла»):</li></ol>
<a class="btn" href="$conf">⬇️ Скачать конфиг</a>
</div>""")


def days_left(ts: float, now: float) -> int:
    """Calendar days from today to the date of ts: 0 — today, 1 — tomorrow."""
    return datetime.date.fromtimestamp(ts).toordinal() - datetime.date.fromtimestamp(now).toordinal()


def qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(data, border=2).save(buf, format="PNG")
    return buf.getvalue()


def page(service: Service, user) -> str:
    now = time.time()
    e = html.escape
    if not user["enabled"]:
        status, status_class = "Отключён", "bad"
    elif user["expire_at"] and user["expire_at"] - now < 3 * 86400:
        status, status_class = "Скоро закончится", "warn"
    else:
        status, status_class = "Активен", "ok"
    if user["expire_at"]:
        days = days_left(user["expire_at"], now)
        expire = time.strftime("до %d.%m.%Y", time.localtime(user["expire_at"]))
        expire += " · сегодня" if days == 0 else f" · {days} дн" if days > 0 else ""
    else:
        expire = "Бессрочно"
    used = user["up"] + user["down"]
    if user["limit_bytes"]:
        pct = min(100, int(100 * used / user["limit_bytes"]))
        limit, bar = f"из {fmt_bytes(user['limit_bytes'])}", f'<div class="bar"><i style="width:{pct}%"></i></div>'
    else:
        limit, bar = "без лимита", ""
    sub = service.sub_url(user)
    vless_block = VLESS_BLOCK.substitute(
        happ_ios=HAPP_IOS, happ_android=HAPP_ANDROID, happ_apk=HAPP_APK, happ_windows=HAPP_WINDOWS,
        deeplink=e("happ://add/" + sub), sub=e(sub)) if uses(user, "vless") and user["enabled"] else ""
    awg_block = AWG_BLOCK.substitute(awg_ios=AWG_IOS, awg_android=AWG_ANDROID, conf=e(sub + "/awg.conf")) \
        if uses(user, "awg") and user["enabled"] and service.awg_conf(user) else ""
    return PAGE.substitute(name=e(user["name"]), status=status, status_class=status_class, expire=e(expire),
                           used=fmt_bytes(used), limit=limit, bar=bar, vless_block=vless_block,
                           awg_block=awg_block)


def make_app(service: Service) -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def sub(request: web.Request) -> web.Response:
        user = service.db.user_by_token(request.match_info["token"])
        if not user:
            raise web.HTTPNotFound()
        links = service.links(user) if user["enabled"] else []
        sub_url = service.sub_url(user)
        if request.query.get("qr"):
            return web.Response(body=qr_png(sub_url), content_type="image/png")
        if "text/html" in request.headers.get("Accept", ""):
            return web.Response(text=page(service, user), content_type="text/html")

        headers = {
            "profile-title": "base64:" + base64.b64encode(f"VPN {user['name']}".encode()).decode(),
            "profile-update-interval": "12",
            "subscription-userinfo": (f"upload={user['up']}; download={user['down']}; "
                                      f"total={user['limit_bytes'] or 0}; expire={user['expire_at'] or 0}"),
            "profile-web-page-url": sub_url,  # Happ: button that opens the instructions page
            "content-disposition": f'attachment; filename="{user["name"]}"',
        }
        payload = base64.b64encode("\n".join(links).encode()).decode()
        return web.Response(text=payload, headers=headers)

    async def awg_conf(request: web.Request) -> web.Response:
        user = service.db.user_by_token(request.match_info["token"])
        conf = service.awg_conf(user) if user and user["enabled"] and uses(user, "awg") else None
        if not conf:
            raise web.HTTPNotFound()
        return web.Response(text=conf, content_type="text/plain", headers={
            "content-disposition": f'attachment; filename="{user["name"][:15]}.conf"'})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/sub/{token}", sub)
    app.router.add_get("/sub/{token}/awg.conf", awg_conf)
    return app
