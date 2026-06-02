"""
Агент-бот для Сырника — Claude (Anthropic) с prompt caching, верификацией,
автосозданием GitHub Issues и web-search для UX/конкурентов.

Команды: /audit, /tech, /ux, /security, /marketing, /database, /performance,
         /competitor, /reload, /selftest
"""

import asyncio
import base64
import time
import io
import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime
from dataclasses import dataclass, field

import anthropic
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Конфиг ───────────────────────────────────────────────────────────────────

AGENT_BOT_TOKEN    = os.environ["AGENT_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")
GITHUB_REPO        = os.environ.get("SIRNIKE_REPO", os.environ.get("GITHUB_REPO", "gorbdan/sirnike"))
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
ADMIN_IDS_RAW      = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS          = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()] if ADMIN_IDS_RAW else []
AUTO_QA_INTERVAL_H = int(os.environ.get("AUTO_QA_INTERVAL_H", "0"))

GITHUB_FILES = ["SirNike.py", "config.py", "db.py", "requirements.txt", "AGENT_NOTES.md"]

logger.info("Repo: %r | Model: %s | Issues: %s",
            GITHUB_REPO, CLAUDE_MODEL, "ON" if GITHUB_TOKEN else "OFF (no token)")

# ── GitHub: получение файлов ─────────────────────────────────────────────────

def fetch_github_file(repo: str, filepath: str, token: str = "") -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/main/{filepath}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    req.add_header("User-Agent", "SirnikeAgentBot/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            logger.info("Loaded %s (%d chars)", filepath, len(content))
            return content
    except urllib.error.HTTPError as e:
        logger.warning("GitHub %s: HTTP %s", filepath, e.code)
        return None
    except Exception as e:
        logger.warning("GitHub %s: %s", filepath, e)
        return None


def load_code_from_github() -> tuple[dict[str, str], list[str]]:
    files, failed = {}, []
    for fname in GITHUB_FILES:
        content = fetch_github_file(GITHUB_REPO, fname, GITHUB_TOKEN)
        if content is None:
            failed.append(fname)
        else:
            files[fname] = content
    return files, failed


def build_full_code_block(files: dict[str, str]) -> str:
    """Собирает ВСЕ файлы в один блок — для prompt caching общий префикс."""
    parts = []
    for fname in GITHUB_FILES:
        if fname not in files:
            continue
        lang = "python" if fname.endswith(".py") else "text"
        parts.append(f"### {fname}\n```{lang}\n{files[fname]}\n```")
    return "\n\n".join(parts)


CODE_FILES: dict[str, str] = {}
CODE_LOAD_ERRORS: list[str] = []
RELOAD_LOCK = asyncio.Lock()

CODE_FILES, CODE_LOAD_ERRORS = load_code_from_github()
if CODE_LOAD_ERRORS:
    logger.warning("Failed to load: %s", CODE_LOAD_ERRORS)
else:
    total = sum(len(v) for v in CODE_FILES.values())
    logger.info("Loaded %d files, %d chars total", len(CODE_FILES), total)

# ── GitHub Issues: создание и дедуп ──────────────────────────────────────────

def _gh_request(method: str, path: str, body: dict | None = None) -> dict | list | None:
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "SirnikeAgentBot/1.0")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning("GitHub API %s %s: HTTP %s — %s",
                       method, path, e.code, e.read().decode("utf-8", errors="ignore")[:200])
        return None
    except Exception as e:
        logger.warning("GitHub API %s %s: %s", method, path, e)
        return None


def _normalize_title(s: str) -> str:
    """Нормализация для дедупа: lowercase, убираем спецсимволы и эмодзи."""
    s = re.sub(r"[^\w\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def push_audit_to_github(content: str, filename: str) -> str | None:
    """Сохраняет аудит как файл в audits/ репозитория sirnike. Возвращает URL или None."""
    if not GITHUB_TOKEN:
        return None
    path = f"/repos/{GITHUB_REPO}/contents/audits/{filename}"
    # Проверяем существующий файл (нужен sha для обновления)
    existing = _gh_request("GET", path)
    sha = existing.get("sha") if isinstance(existing, dict) else None
    body = {
        "message": f"audit: {filename}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    result = _gh_request("PUT", path, body)
    if result and isinstance(result, dict):
        return result.get("content", {}).get("html_url")
    return None


def fetch_open_audit_issues() -> list[dict]:
    """Все открытые issues с лейблом auto-audit (для дедупа)."""
    result = _gh_request("GET", f"/repos/{GITHUB_REPO}/issues?state=open&labels=auto-audit&per_page=100")
    if not result:
        return []
    return [i for i in result if "pull_request" not in i]


def create_github_issue(title: str, body: str, labels: list[str]) -> str | None:
    """Создаёт issue, возвращает URL. None если выключено или ошибка."""
    result = _gh_request("POST", f"/repos/{GITHUB_REPO}/issues",
                         {"title": title, "body": body, "labels": labels})
    if result and "html_url" in result:
        return result["html_url"]
    return None


# ── Anthropic клиент ──────────────────────────────────────────────────────────

def make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Агенты: конфиг ────────────────────────────────────────────────────────────

BOT_CONTEXT = """Telegram-бот "Сырник" — AI-генератор изображений и видео.
Стек: Python 3.12, python-telegram-bot, SQLite, aiohttp.
Провайдеры: Zveno (Gemini), MashaGPT, YesAPI, Seedance (видео).
Деплой: BotHost + Docker. Валюта: изюминки.

АРХИТЕКТУРНЫЕ ФАКТЫ — обязательно учитывай при анализе:
- asyncio — ОДНОПОТОЧНЫЙ event loop. Любые две строки кода без await между ними выполняются атомарно. Race condition между синхронными операциями ФИЗИЧЕСКИ НЕВОЗМОЖЕН.
- Очередь генерации обслуживается ОДНИМ воркером (queue_worker). В processing_user_ids одновременно максимум 1 пользователь.
- processing_user_ids хранит только user_id, не job-объекты. Данные о job (cost, was_free) хранятся только в _worker_current_job.
- get_balance() и spend_izyminki() вызываются без await между ними — это атомарная пара, race condition невозможен.
- Между generation_queue.get() и следующим try: нет await и нет падающих операций (только set.discard, set.add, присвоение) — краш там невозможен."""

AGENTS_CONFIG = {
    "tech": {
        "role": "Технический аналитик Python/asyncio",
        "focus": "необработанные исключения, потери изюминок при крашах, проблемы очереди генерации, утечки памяти",
        "keywords": ["async def", "try:", "except", "asyncio", "Queue", "create_task",
                     "gather", "wait_for", "raise", "TimeoutError", "CancelledError"],
        "files": ["SirNike.py", "requirements.txt", "AGENT_NOTES.md"],
    },
    "ux": {
        "role": "UX-аналитик Telegram-ботов",
        "focus": "тексты сообщений, онбординг новых пользователей, кнопки, сценарии, ошибки которые видит юзер",
        "keywords": ["reply_text", "send_message", "InlineKeyboard", "ReplyKeyboard",
                     "callback_query", "ParseMode", "edit_text", "answer", "BotCommand",
                     "/start", "приветств", "ошибк", "извин"],
        "files": ["SirNike.py", "AGENT_NOTES.md"],
    },
    "security": {
        "role": "Security-аналитик платёжных систем",
        "focus": "валидация платёжных payload, SQL-запросы, защита от накрутки, безопасность API",
        "keywords": ["pre_checkout", "successful_payment", "execute", "INSERT", "UPDATE",
                     "DELETE", "SELECT", "payload", "invoice", "token", "secret", "validate"],
        "files": ["SirNike.py", "config.py", "AGENT_NOTES.md"],
    },
    "marketing": {
        "role": "Growth-менеджер Telegram-ботов",
        "focus": "реферальная система, пакеты изюминок, бесплатные генерации, конверсия в покупку, retention",
        "keywords": ["referral", "referrer", "ref_", "izyuminki", "изюм", "bonus", "free",
                     "package", "пакет", "промо", "promo", "discount", "skidka"],
        "files": ["SirNike.py", "AGENT_NOTES.md"],
    },
    "database": {
        "role": "Database-инженер SQLite",
        "focus": "транзакции, INSERT OR IGNORE, потеря данных при сбоях, схема БД, дублирование",
        "keywords": ["execute", "executemany", "commit", "rollback", "BEGIN", "INSERT",
                     "UPDATE", "DELETE", "SELECT", "CREATE TABLE", "sqlite", "cursor", "fetchone"],
        "files": ["db.py", "SirNike.py", "AGENT_NOTES.md"],
    },
    "performance": {
        "role": "Performance-инженер Python",
        "focus": "очередь генерации, кэш изображений, медленные DB-запросы, блокирующий I/O в async-контексте",
        "keywords": ["queue", "cache", "sleep", "wait", "lock", "executor", "run_in_executor",
                     "open(", "read(", "write(", "requests.", "urllib", "blocking"],
        "files": ["SirNike.py", "AGENT_NOTES.md"],
    },
}

FINDING_INSTRUCTIONS = """СТРОГИЕ ПРАВИЛА:
1. Пиши ТОЛЬКО о реально найденных проблемах в коде.
2. Каждая находка обязана содержать точную цитату кода и номер строки.
3. НЕ повторяй находки из AGENT_NOTES.md.
4. Если проблем нет — напиши только: "Проблем не обнаружено."
5. НЕ репортить race condition если между двумя операциями НЕТ await — в asyncio это атомарно.
6. НЕ репортить проблемы с processing_user_ids как "несколько пользователей одновременно" — воркер один, там всегда максимум 1 user_id.
7. НЕ репортить теоретические сценарии ("если вдруг", "в теории может") — только реально воспроизводимые баги.
8. Перед каждой находкой мысленно проверь: "Это действительно может случиться с учётом архитектуры бота?"

ФОРМАТ КАЖДОЙ НАХОДКИ (строго, без отклонений):

## {emoji} [файл.py:номер_строки] Короткое название

**Код:**
```python
точная_строка_кода_из_файла
```

**Проблема:** что именно не так

**Воспроизведение:** как баг проявится у пользователя

**Фикс:** конкретное предложение

---

Где {emoji} = 🔴 если критично, 🟡 если важно, 🟢 если незначительно.

Отвечай на русском."""

ANALYZER_USER_MSG = "Проанализируй код Сырника и найди проблемы по своей специализации. Соблюдай формат строго."

# Максимум символов кода в одном запросе (помещается в Tier 1: 50K ITPM)
MAX_CODE_CHARS_PER_REQUEST = 120_000


def _split_python_into_blocks(code: str) -> list[tuple[int, str]]:
    """Делит Python-код на top-level блоки (def/class/etc).
    Возвращает [(start_line_number, block_text)].
    """
    lines = code.splitlines(keepends=True)
    block_starts = [0]
    for i, line in enumerate(lines):
        if i == 0:
            continue
        # Начало нового top-level блока: строка без отступа, начинается с def/async/class/@
        if line and line[0] not in (" ", "\t", "\n", "#"):
            stripped = line.lstrip()
            if stripped.startswith(("def ", "async def ", "class ", "@")):
                block_starts.append(i)
    block_starts.append(len(lines))
    blocks = []
    for j in range(len(block_starts) - 1):
        start, end = block_starts[j], block_starts[j + 1]
        blocks.append((start + 1, "".join(lines[start:end])))
    return blocks


def slice_code_for_agent(file_content: str, keywords: list[str], max_chars: int) -> str:
    """Извлекает функции/классы где есть keywords. Если ничего — голова файла."""
    blocks = _split_python_into_blocks(file_content)
    if not blocks:
        return file_content[:max_chars]

    keywords_lower = [k.lower() for k in keywords]
    relevant = []
    total = 0
    for start_line, block in blocks:
        text_lower = block.lower()
        if any(kw in text_lower for kw in keywords_lower):
            tagged = f"# ──── строка ~{start_line} ────\n{block}"
            if total + len(tagged) > max_chars:
                break
            relevant.append(tagged)
            total += len(tagged)

    if not relevant:
        # Fallback: первые N символов файла
        return file_content[:max_chars]
    return "\n".join(relevant)


def build_code_for_agent(agent_key: str) -> str:
    """Готовит блок кода для агента: режет SirNike.py по keywords, остальные файлы целиком."""
    cfg = AGENTS_CONFIG[agent_key]
    keywords = cfg.get("keywords", [])
    files_to_send = cfg.get("files", GITHUB_FILES)

    parts = []
    # Маленькие файлы — целиком
    small_budget = 0
    for fname in files_to_send:
        if fname not in CODE_FILES or fname == "SirNike.py":
            continue
        content = CODE_FILES[fname]
        lang = "python" if fname.endswith(".py") else "text"
        parts.append(f"### {fname}\n```{lang}\n{content}\n```")
        small_budget += len(content)

    # SirNike.py — урезаем по keywords
    if "SirNike.py" in files_to_send and "SirNike.py" in CODE_FILES:
        budget = MAX_CODE_CHARS_PER_REQUEST - small_budget
        sliced = slice_code_for_agent(CODE_FILES["SirNike.py"], keywords, max_chars=budget)
        parts.insert(0, f"### SirNike.py (релевантные функции)\n```python\n{sliced}\n```")

    return "\n\n".join(parts)


def call_agent(agent_key: str) -> str:
    """Запускает агента-анализатора. Шлёт только релевантный код (без caching)."""
    cfg = AGENTS_CONFIG[agent_key]
    client = make_client()
    code = build_code_for_agent(agent_key)

    system = (
        f"Ты {cfg['role']}.\n\n"
        f"Контекст бота: {BOT_CONTEXT}\n\n"
        f"Фокус анализа: {cfg['focus']}\n\n"
        f"{FINDING_INSTRUCTIONS}"
    )

    logger.info("[%s] отправляю %d chars кода", agent_key, len(code))
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": f"{ANALYZER_USER_MSG}\n\n{code}"}],
    )
    return _extract_text(response)


def _extract_text(response) -> str:
    """Достаёт текст из ответа (может быть несколько блоков если был tool_use)."""
    parts = []
    for block in response.content:
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
    return "\n\n".join(parts) if parts else ""


# ── Парсер находок ────────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str       # "critical" / "important" / "minor"
    emoji: str          # 🔴 / 🟡 / 🟢
    file: str
    line: str
    title: str
    raw: str            # полный блок находки
    code: str = ""
    problem: str = ""
    fix: str = ""
    confirmed: bool = True
    rejection_reason: str = ""
    issue_url: str = ""


SEVERITY_BY_EMOJI = {"🔴": "critical", "🟡": "important", "🟢": "minor"}

FINDING_HEADER_RE = re.compile(
    r"^##\s*(🔴|🟡|🟢)\s*\[([^\]:]+):(\d+|N/A|\?)\]\s*(.+?)$",
    re.MULTILINE,
)


def parse_findings(text: str) -> list[Finding]:
    """Извлекает структурированные находки из markdown-ответа агента."""
    if not text or "проблем не обнаружено" in text.lower():
        return []

    # Находим все заголовки и режем текст между ними
    matches = list(FINDING_HEADER_RE.finditer(text))
    findings = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        emoji, fname, line, title = m.group(1), m.group(2), m.group(3), m.group(4).strip()

        # Достаём код, проблему, фикс из блока
        code_m = re.search(r"\*\*Код:\*\*\s*```\w*\n(.*?)\n```", block, re.DOTALL)
        prob_m = re.search(r"\*\*Проблема:\*\*\s*(.+?)(?=\n\n\*\*|\n---|\Z)", block, re.DOTALL)
        fix_m  = re.search(r"\*\*Фикс:\*\*\s*(.+?)(?=\n\n\*\*|\n---|\Z)", block, re.DOTALL)

        findings.append(Finding(
            severity=SEVERITY_BY_EMOJI.get(emoji, "important"),
            emoji=emoji,
            file=fname.strip(),
            line=line.strip(),
            title=title,
            raw=block,
            code=code_m.group(1).strip() if code_m else "",
            problem=prob_m.group(1).strip() if prob_m else "",
            fix=fix_m.group(1).strip() if fix_m else "",
        ))
    return findings


# ── Верификатор ───────────────────────────────────────────────────────────────

VERIFIER_INSTRUCTIONS = """Ты — строгий верификатор багов. Получаешь список находок другого агента.
Для КАЖДОЙ находки проверь по реальному коду:
- цитата кода точная (есть в файле на указанной строке)?
- проблема реальная (а не выдумка/устаревшая инфа)?
- баг не из AGENT_NOTES.md (там уже известные)?

Для каждой находки выведи СТРОГО одну строку:
FINDING #N: CONFIRMED
или
FINDING #N: REJECTED — короткая причина

Нумерация с 1. Без других пояснений. Будь строгим — лучше отбросить настоящее, чем оставить ложное."""


def verify_findings(agent_key: str, findings: list[Finding]) -> list[Finding]:
    """Запускает верификатора, помечает каждую находку confirmed=True/False.
    Шлёт только релевантный код (тот же что у анализатора), чтобы укладываться в лимит."""
    if not findings:
        return findings
    client = make_client()
    findings_block = "\n\n".join(
        f"FINDING #{i+1}:\n{f.raw}" for i, f in enumerate(findings)
    )
    code = build_code_for_agent(agent_key)
    system = f"{VERIFIER_INSTRUCTIONS}\n\nКонтекст бота: {BOT_CONTEXT}"
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": f"Код:\n\n{code}\n\n---\n\nПроверь эти находки:\n\n{findings_block}"}],
    )
    text = _extract_text(response)

    verdicts = re.findall(r"FINDING #(\d+):\s*(CONFIRMED|REJECTED)(?:\s*[—\-:]\s*(.+))?",
                          text, re.IGNORECASE)
    verdict_map = {int(n): (v.upper(), (r or "").strip()) for n, v, r in verdicts}

    for i, f in enumerate(findings, start=1):
        verdict, reason = verdict_map.get(i, ("CONFIRMED", ""))
        f.confirmed = verdict == "CONFIRMED"
        f.rejection_reason = reason
    return findings


# ── GitHub Issues для подтверждённых находок ─────────────────────────────────

def _issue_dedup_key(agent: str, f: Finding) -> str:
    return _normalize_title(f"{agent} {f.file} {f.line} {f.title}")


def push_findings_to_issues(agent_key: str, findings: list[Finding]) -> int:
    """Создаёт GitHub Issues для подтверждённых, с дедупом. Возвращает кол-во созданных."""
    if not GITHUB_TOKEN:
        logger.info("GITHUB_TOKEN отсутствует — issues не создаются")
        return 0

    open_issues = fetch_open_audit_issues()
    existing_keys = set()
    for issue in open_issues:
        # ключ дедупа берём из title issue
        existing_keys.add(_normalize_title(issue.get("title", "")))

    created = 0
    for f in findings:
        if not f.confirmed:
            continue
        title = f"[{agent_key}] {f.emoji} {f.file}:{f.line} — {f.title}"
        norm = _normalize_title(title)
        # Простой дедуп: либо точное совпадение, либо существенное пересечение
        if norm in existing_keys or any(norm in k or k in norm for k in existing_keys if len(k) > 20):
            logger.info("Issue дубликат, пропускаю: %s", title[:80])
            continue

        body = (
            f"**Файл:** `{f.file}` (строка {f.line})\n\n"
            f"**Код:**\n```python\n{f.code}\n```\n\n"
            f"**Проблема:** {f.problem}\n\n"
            f"**Предложение по фиксу:** {f.fix}\n\n"
            f"---\n*Создано автоматически агентом `{agent_key}` ({f.emoji} {f.severity}).*"
        )
        labels = ["auto-audit", f"agent:{agent_key}", f"severity:{f.severity}"]
        url = create_github_issue(title, body, labels)
        if url:
            f.issue_url = url
            created += 1
            logger.info("Issue создан: %s", url)
    return created


# ── Competitor агент (без web search — на знаниях Claude + env list) ─────────

COMPETITORS_LIST = os.environ.get("COMPETITORS_LIST", "")  # формат: "name1: descr | name2: descr"

COMPETITOR_PROMPT = """Ты — продуктовый аналитик Telegram-ботов для российского рынка.

КОНТЕКСТ: Сырник — Telegram-бот AI-генерации изображений и видео на сторонних провайдерах (Zveno/Gemini, MashaGPT, YesAPI, Seedance). Валюта: изюминки. Реферальная система. Деплой на BotHost.

НАШИ ПРЯМЫЕ КОНКУРЕНТЫ — это НЕ Midjourney, Kandinsky или Shedevrum (это платформы). Наши конкуренты — такие же Telegram-боты, работающие через сторонние API, ориентированные на русскоязычную аудиторию:
- @syntxaibot
- @ptichkinobot
- @NanoBananoAiPhoto_bot
- Другие похожие боты если знаешь

Для КАЖДОГО конкурента разбери:
- Какие провайдеры/модели используют
- Система монетизации (токены, подписка, цены в рублях)
- UX: онбординг, кнопки, промпты, сценарии
- Реферальная/партнёрская программа
- Видео-генерация: есть ли, какая
- Что делают лучше нас, что хуже

Затем выдай 5–10 КОНКРЕТНЫХ улучшений для Сырника в формате:

## 🎯 [Категория] Название улучшения

**Что есть у конкурента:** @username + конкретная фича

**Что у нас:** текущее состояние

**Предложение:** конкретный шаг реализации

**Ожидаемый эффект:** retention/конверсия/ARPU

---

ВАЖНО: Если не знаешь точно что есть у конкретного бота — так и напиши "не знаю точно, но судя по описанию". Не выдумывай фичи.

Отвечай на русском. Без воды."""


def call_competitor_agent() -> str:
    client = make_client()
    user_msg = "Сделай конкурентный анализ."
    if COMPETITORS_LIST:
        user_msg += f"\n\nОсобое внимание этим конкурентам: {COMPETITORS_LIST}"
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=COMPETITOR_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return _extract_text(response)


# ── Оркестрация ───────────────────────────────────────────────────────────────

def run_agent_with_verification(agent_key: str) -> str:
    """Анализатор → парсер → верификатор → issues → итоговый markdown."""
    cfg = AGENTS_CONFIG[agent_key]
    logger.info("[%s] анализирую...", agent_key)
    raw = call_agent(agent_key)

    findings = parse_findings(raw)
    if not findings:
        return f"## {cfg['role']}\n\n{raw}\n\n_Структурированных находок не найдено._"

    logger.info("[%s] найдено %d, верифицирую...", agent_key, len(findings))
    findings = verify_findings(agent_key, findings)
    confirmed = [f for f in findings if f.confirmed]
    rejected = [f for f in findings if not f.confirmed]
    logger.info("[%s] подтверждено %d, отклонено %d", agent_key, len(confirmed), len(rejected))

    issues_created = push_findings_to_issues(agent_key, confirmed)
    logger.info("[%s] создано issues: %d", agent_key, issues_created)

    # Формируем отчёт
    parts = [f"## {cfg['role']}\n"]
    parts.append(f"_Найдено: {len(findings)} | Подтверждено: {len(confirmed)} | "
                 f"Отклонено: {len(rejected)} | GitHub Issues: {issues_created}_\n")

    if confirmed:
        parts.append("### ✅ Подтверждённые находки\n")
        for f in confirmed:
            block = f.raw
            if f.issue_url:
                block += f"\n\n🔗 **Issue:** {f.issue_url}"
            parts.append(block)
            parts.append("---")

    if rejected:
        parts.append("\n### ❌ Отклонено верификатором\n")
        for f in rejected:
            parts.append(f"- **{f.title}** ({f.file}:{f.line}) — _{f.rejection_reason or 'без причины'}_")

    return "\n\n".join(parts)


AUDIT_PAUSE_SECONDS = 65  # пауза между агентами чтобы не превысить rate limit


def run_full_audit() -> str:
    results = []
    keys = list(AGENTS_CONFIG.keys())
    for i, key in enumerate(keys):
        try:
            results.append(run_agent_with_verification(key))
        except Exception as e:
            logger.exception("Agent %s failed", key)
            results.append(f"## {AGENTS_CONFIG[key]['role']}\n\n❌ Ошибка: {e}")
        if i < len(keys) - 1:
            logger.info("Пауза %ds перед следующим агентом...", AUDIT_PAUSE_SECONDS)
            time.sleep(AUDIT_PAUSE_SECONDS)
    return "# Полный аудит Сырника\n\n" + "\n\n---\n\n".join(results)


# ── Telegram хендлеры ─────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


async def run_agent_command(update: Update, runner_fn, filename: str, label: str):
    if not is_allowed(update.effective_user.id):
        return
    if not CODE_FILES:
        await update.message.reply_text("Код не загружен. Сделай /reload.")
        return
    msg = await update.message.reply_text(f"{label} работает... ⏳")
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, runner_fn)
        try:
            await msg.delete()
        except Exception:
            pass
        doc = InputFile(io.BytesIO(result.encode("utf-8-sig")), filename=filename)
        await update.message.reply_document(document=doc)
        # Сохраняем в GitHub
        ts = datetime.utcnow().strftime("%Y-%m-%d_%H-%M")
        gh_filename = f"{ts}_{filename}"
        url = await asyncio.get_event_loop().run_in_executor(
            None, lambda: push_audit_to_github(result, gh_filename)
        )
        if url:
            await update.message.reply_text(f"📁 Сохранено в GitHub: {url}")
    except Exception as e:
        logger.exception("Agent failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, run_full_audit, "full_audit.md", "🔍 Полный аудит (6 агентов + верификация + issues)")

async def cmd_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: run_agent_with_verification("tech"), "tech_report.md", "⚙️ Tech")

async def cmd_ux(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: run_agent_with_verification("ux"), "ux_report.md", "👤 UX")

async def cmd_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: run_agent_with_verification("security"), "security_report.md", "🔒 Security")

async def cmd_marketing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: run_agent_with_verification("marketing"), "marketing_report.md", "📊 Marketing")

async def cmd_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: run_agent_with_verification("database"), "database_report.md", "🗄️ Database")

async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: run_agent_with_verification("performance"), "performance_report.md", "⚡ Performance")

async def cmd_competitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, call_competitor_agent, "competitor_report.md", "🎯 Competitor research (web search)")


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global CODE_FILES, CODE_LOAD_ERRORS
    msg = await update.message.reply_text("Загружаю код с GitHub...")
    async with RELOAD_LOCK:
        CODE_FILES, CODE_LOAD_ERRORS = await asyncio.get_event_loop().run_in_executor(
            None, load_code_from_github
        )
    if CODE_LOAD_ERRORS:
        await msg.edit_text(f"⚠️ Не удалось загрузить: {', '.join(CODE_LOAD_ERRORS)}")
    else:
        total = sum(len(v) for v in CODE_FILES.values())
        await msg.edit_text(f"✅ Загружено {len(CODE_FILES)} файлов, {total:,} символов из {GITHUB_REPO}")


async def cmd_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    msg = await update.message.reply_text("🔍 Самодиагностика...")
    results = []
    _, failed = load_code_from_github()
    results.append(f"{'✅' if not failed else '❌'} GitHub: {'OK' if not failed else ', '.join(failed)}")
    results.append(f"✅ Модель: {CLAUDE_MODEL}")
    results.append(f"{'✅' if GITHUB_TOKEN else '⚠️'} GitHub Issues: {'ON' if GITHUB_TOKEN else 'OFF (нет GITHUB_TOKEN)'}")
    results.append(f"✅ Агентов с верификацией: {len(AGENTS_CONFIG)}")
    total = sum(len(v) for v in CODE_FILES.values())
    results.append(f"✅ Код: {total:,} символов в {len(CODE_FILES)} файлах")

    # Проверим Anthropic API ping
    try:
        client = make_client()
        r = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        results.append(f"✅ Anthropic API: OK ({r.usage.input_tokens}in/{r.usage.output_tokens}out)")
    except Exception as e:
        results.append(f"❌ Anthropic API: {e}")

    # Проверим GitHub Issues API
    if GITHUB_TOKEN:
        issues = fetch_open_audit_issues()
        results.append(f"✅ Open auto-audit issues: {len(issues)}")

    await msg.edit_text("Самодиагностика:\n\n" + "\n".join(results))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Агенты Сырника 🧀\n\n"
        "/audit — полный аудит (6 агентов + верификация + GitHub Issues)\n\n"
        "/tech — технические баги\n"
        "/ux — UX (с web search конкурентов)\n"
        "/security — безопасность и платежи\n"
        "/marketing — монетизация и рефералы\n"
        "/database — целостность данных\n"
        "/performance — производительность\n\n"
        "/competitor — глубокое исследование конкурентов\n\n"
        "/reload — обновить код с GitHub\n"
        "/selftest — проверить что всё работает"
    )


# ── Авто-аудит ────────────────────────────────────────────────────────────────

_auto_audit_task = None


async def auto_audit_loop(app):
    if not AUTO_QA_INTERVAL_H or not ADMIN_IDS:
        return
    interval = AUTO_QA_INTERVAL_H * 3600
    logger.info("Auto-audit: every %dh → %d", AUTO_QA_INTERVAL_H, ADMIN_IDS[0])
    while True:
        await asyncio.sleep(interval)
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, run_full_audit)
            doc = InputFile(io.BytesIO(result.encode("utf-8")), filename="auto_audit.md")
            await app.bot.send_document(
                chat_id=ADMIN_IDS[0],
                document=doc,
                caption=f"🤖 Авто-аудит (каждые {AUTO_QA_INTERVAL_H}ч)",
            )
        except Exception:
            logger.exception("Auto-audit failed")


async def post_init(app):
    global _auto_audit_task
    if AUTO_QA_INTERVAL_H and ADMIN_IDS:
        _auto_audit_task = asyncio.create_task(auto_audit_loop(app))
        logger.info("Auto-audit task created")


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(AGENT_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("tech", cmd_tech))
    app.add_handler(CommandHandler("ux", cmd_ux))
    app.add_handler(CommandHandler("security", cmd_security))
    app.add_handler(CommandHandler("marketing", cmd_marketing))
    app.add_handler(CommandHandler("database", cmd_database))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("competitor", cmd_competitor))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("selftest", cmd_selftest))

    total = sum(len(v) for v in CODE_FILES.values())
    logger.info("Agent bot started (Claude). Repo: %s, Model: %s, Files: %d, Chars: %d",
                GITHUB_REPO, CLAUDE_MODEL, len(CODE_FILES), total)
    app.run_polling()


if __name__ == "__main__":
    main()
