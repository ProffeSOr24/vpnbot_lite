"""«Что нового» из CHANGELOG.md: разделы между двумя версиями, в разметке Telegram."""
import html
import re
from pathlib import Path

LOCAL = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
HEADING = re.compile(r"^## (\d+(?:\.\d+)*)\b.*$", re.M)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version))


def newer(a: str, b: str) -> bool:
    """Is version a newer than b?"""
    return _key(a) > _key(b)


def sections(text: str) -> list[tuple[str, str]]:
    """[(версия, текст раздела без заголовка)] в порядке файла (новые сверху)."""
    marks = list(HEADING.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        body = body.split("\n## ", 1)[0]  # «## До 1.10» и прочие разделы без номера версии
        out.append((m.group(1), body.strip()))
    return out


def to_telegram(markdown: str) -> str:
    """Markdown из CHANGELOG → HTML для Telegram: **жирный**, `код`, пункты списка."""
    text = html.escape(markdown, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^ {2,}- ", "    ◦ ", text, flags=re.M)  # nested items
    return re.sub(r"^- ", "• ", text, flags=re.M)


def between(text: str, old: str | None, new: str, limit: int = 3500) -> str:
    """Что появилось после версии old до new включительно; пусто, если нечего показать."""
    chunks = []
    for version, body in sections(text):
        if _key(version) <= _key(new) and (old is None or _key(version) > _key(old)) and body:
            chunks.append(f"🆕 <b>Версия {version}</b>\n{to_telegram(body)}")
    out = "\n\n".join(chunks)
    if len(out) > limit:
        out = out[:limit].rsplit("\n", 1)[0] + "\n…полностью — в CHANGELOG.md на GitHub"
    return out


def local_between(old: str | None, new: str) -> str:
    try:
        return between(LOCAL.read_text(encoding="utf-8"), old, new)
    except OSError:
        return ""
