"""
Агент-бот для Сырника.
Загружает код с GitHub, анализирует через Claude.
Команды: /qa, /analyze, /fix, /reload, /help
"""

import asyncio
import io
import logging
import os
import urllib.request
import urllib.error

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфиг ───────────────────────────────────────────────────────────────────

AGENT_BOT_TOKEN = os.environ["AGENT_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_REPO = os.environ.get("SIRNIKE_REPO", os.environ.get("GITHUB_REPO", "gorbdan/sirnike"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

logger.info(
    "Config: SIRNIKE_REPO=%r GITHUB_REPO=%r → using %r",
    os.environ.get("SIRNIKE_REPO"),
    os.environ.get("GITHUB_REPO"),
    GITHUB_REPO,
)
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()] if ADMIN_IDS_RAW else []

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
AUTO_QA_INTERVAL_H = int(os.environ.get("AUTO_QA_INTERVAL_H", "0"))  # 0 = выключено

GITHUB_FILES = ["SirNike.py", "config.py", "db.py", "requirements.txt", "AGENT_NOTES.md"]

# ─── Загрузка кода с GitHub ────────────────────────────────────────────────────

def fetch_github_file(repo: str, filepath: str, token: str = "") -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/main/{filepath}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    req.add_header("User-Agent", "SirnikeAgentBot/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            logger.info("Loaded %s from GitHub (%d chars)", filepath, len(content))
            return content
    except urllib.error.HTTPError as e:
        logger.warning("GitHub fetch failed for %s: HTTP %s", filepath, e.code)
        return None
    except Exception as e:
        logger.warning("GitHub fetch error for %s: %s", filepath, e)
        return None


def load_code_from_github() -> tuple[str, list[str]]:
    parts = []
    failed = []
    for fname in GITHUB_FILES:
        content = fetch_github_file(GITHUB_REPO, fname, GITHUB_TOKEN)
        if content is None:
            failed.append(fname)
            continue
        ext = fname.rsplit(".", 1)[-1]
        lang = "python" if ext == "py" else "text"
        parts.append(f"### {fname}\n```{lang}\n{content}\n```")
    return "\n\n".join(parts), failed


# Загружаем при старте
SIRNIKE_CODE, CODE_LOAD_ERRORS = load_code_from_github()
LAST_ANALYSIS_FILE = "last_analysis.md"

def _load_last_analysis() -> str:
    try:
        with open(LAST_ANALYSIS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""

def _save_last_analysis(text: str) -> None:
    try:
        with open(LAST_ANALYSIS_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        logger.warning("Failed to save last analysis to disk")

LAST_ANALYSIS: str = _load_last_analysis()
RELOAD_LOCK = asyncio.Lock()
if CODE_LOAD_ERRORS:
    logger.warning("Failed to load from GitHub: %s", CODE_LOAD_ERRORS)
else:
    logger.info("Total code loaded: %d chars", len(SIRNIKE_CODE))

# ─── Системные промпты ─────────────────────────────────────────────────────────

def make_base_context() -> str:
    return f"""Ты работаешь с кодом Telegram-бота "Сырник" — AI-генератор изображений и видео.
Стек: Python, python-telegram-bot, SQLite, aiohttp.
Провайдеры генерации: Zveno (Gemini), MashaGPT, YesAPI, Seedance (видео).
Деплой: BotHost + Docker. Внутренняя валюта: "изюминки".
GitHub: https://github.com/{GITHUB_REPO}

Код проекта:
{SIRNIKE_CODE}
"""


QA_SUFFIX = """
Ты QA-агент. Тестируешь пользовательские сценарии.

Без уточнения — проходись по всем основным флоу:
1. Новый пользователь (/start, онбординг, первая генерация)
2. Генерация с промптом и без фото
3. Генерация с фото-референсом
4. Покупка изюминок (/buy → оплата → начисление)
5. Реферальная система
6. Seedance видео
7. Аватары (загрузка, генерация, удаление)

Для каждого сценария: шаги → ожидаемое поведение → потенциальные баги.
Серьёзность: 🔴 критично / 🟡 важно / 🟢 незначительно.
Отвечай чётко, по пунктам, на русском.
"""

ANALYZE_SUFFIX = """
Ты Analyst-агент. Ищешь баги и проблемы в коде.

ВАЖНЫЕ ПРАВИЛА:
- Бот использует asyncio (однопоточный event loop). Операции между двумя await-точками атомарны — race condition между ними физически невозможен. НЕ репортить как баг паттерн "проверка → add/set" если между ними нет await.
- Если в коде есть файл AGENT_NOTES.md — он содержит список уже исправленных багов и подтверждённых ложных срабатываний. Не репортить то что там отмечено как fixed или false_positive.
- Репортить только реальные, воспроизводимые проблемы.

Смотри на:
1. Race conditions (только через await-границы)
2. Утечки памяти и ресурсов
3. Непойманные исключения и потери данных
4. Безопасность (валидация, инъекции)
5. Edge cases в бизнес-логике (изюминки, рефералы, оплата)
6. Персистентность (__img__ кэш, аватары)
7. Узкие места производительности

Для каждой проблемы: файл + строка → описание → сценарий воспроизведения → серьёзность → предложение по фиксу.
Серьёзность: 🔴 критично / 🟡 важно / 🟢 незначительно.

{previous_analysis_section}

Отвечай чётко, на русском.
"""

FIX_SUFFIX = """
Ты Fix-агент. Пишешь конкретные патчи.

Для каждого фикса:
1. Кратко объясни проблему (1-2 предложения)
2. Покажи что было и что стало

Формат:
```python
# было:
старый код

# стало:
новый код
```

Патчи минимальные — меняй только нужное.
Отвечай на русском.
"""


def get_system_prompt(agent_type: str) -> str:
    base = make_base_context()
    if agent_type == "analyze" and LAST_ANALYSIS:
        prev = (
            "## Предыдущий анализ\n"
            "Ниже баги из прошлого отчёта. Проверь каждый по текущему коду:\n"
            "- Если баг **исправлен** — напиши '✅ Исправлен: [название]' и не включай в основной список\n"
            "- Если баг **ещё есть** — включи в отчёт как обычно\n"
            "- Новые баги которых раньше не было — добавляй в конец\n\n"
            f"Предыдущий отчёт:\n{LAST_ANALYSIS[:6000]}"
        )
        suffix = ANALYZE_SUFFIX.replace("{previous_analysis_section}", prev)
    else:
        suffix = ANALYZE_SUFFIX.replace("{previous_analysis_section}", "")
    suffixes = {"qa": QA_SUFFIX, "analyze": suffix, "fix": FIX_SUFFIX}
    return base + suffixes[agent_type]


# ─── Anthropic клиент ──────────────────────────────────────────────────────────

anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


async def call_agent(agent_type: str, user_message: str) -> str:
    try:
        response = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=get_system_prompt(agent_type),
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except anthropic.APIStatusError as e:
        logger.exception("Anthropic API error")
        return f"Ошибка API: {e.status_code} — {e.message}"
    except Exception as e:
        logger.exception("Agent call failed")
        return f"Ошибка агента: {e}"


# ─── Хелпер: отправить длинный текст ──────────────────────────────────────────

def split_text(text: str, max_len: int = 4000) -> list[str]:
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def send_chunk(update: Update, text: str, status_msg=None):
    try:
        if status_msg:
            await status_msg.edit_text(text, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        if status_msg:
            await status_msg.edit_text(text)
        else:
            await update.message.reply_text(text)


async def send_long(update: Update, text: str, status_msg=None):
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        suffix = f"\n\n({i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_chunk(update, chunk + suffix, status_msg if i == 0 else None)


# ─── Telegram хендлеры ────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Агенты Сырника 🧀\n\n"
        "/qa — тест всех сценариев\n"
        "/qa <тема> — тест конкретного флоу\n\n"
        "/analyze — найти баги в коде\n"
        "/analyze <тема> — анализ конкретной части\n\n"
        "/fix — патчи для топ-проблем\n"
        "/fix <проблема> — фикс конкретной проблемы\n\n"
        "/reload — перезагрузить код с GitHub\n"
        "/reset_analysis — сбросить историю анализа\n"
        "/selftest — проверить что все агенты работают\n\n"
        "Примеры:\n"
        "/qa онбординг нового пользователя\n"
        "/analyze race conditions в очереди\n"
        "/fix потеря изюминок при краше воркера"
    )


async def cmd_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    msg = await update.message.reply_text("🔍 Запускаю самодиагностику...")
    results = []

    # 1. Проверка загрузки кода с GitHub
    _, failed = load_code_from_github()
    if failed:
        results.append(f"❌ GitHub: не загружены {', '.join(failed)}")
    else:
        results.append(f"✅ GitHub: все файлы загружены ({len(GITHUB_FILES)} шт.)")

    # 2. Проверка Claude API (минимальный запрос)
    try:
        resp = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=20,
            messages=[{"role": "user", "content": "Ответь одним словом: работаю"}],
        )
        answer = resp.content[0].text.strip()[:50]
        results.append(f"✅ Claude API: отвечает ({answer!r})")
    except Exception as e:
        results.append(f"❌ Claude API: ошибка ({e})")

    # 3. Проверка каждого агента (минимальный промпт)
    for agent_type, label in [("qa", "QA"), ("analyze", "Analyst"), ("fix", "Fix")]:
        try:
            resp = await anthropic_client.messages.create(
                model=MODEL,
                max_tokens=30,
                system=get_system_prompt(agent_type),
                messages=[{"role": "user", "content": "Ответь одним словом: готов"}],
            )
            answer = resp.content[0].text.strip()[:50]
            results.append(f"✅ Агент {label}: работает ({answer!r})")
        except Exception as e:
            results.append(f"❌ Агент {label}: ошибка ({e})")

    # 4. Проверка LAST_ANALYSIS
    if LAST_ANALYSIS:
        results.append(f"✅ Память анализа: есть ({len(LAST_ANALYSIS):,} символов)")
    else:
        results.append("⚠️ Память анализа: пуста (первый /analyze создаст)")

    report = "Результаты самодиагностики:\n\n" + "\n".join(results)
    await msg.edit_text(report)


async def cmd_reset_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global LAST_ANALYSIS
    LAST_ANALYSIS = ""
    _save_last_analysis("")
    await update.message.reply_text("История анализа сброшена ✅\nСледующий /analyze будет как первый.")


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global SIRNIKE_CODE, CODE_LOAD_ERRORS, LAST_ANALYSIS
    msg = await update.message.reply_text("Загружаю код с GitHub...")
    async with RELOAD_LOCK:
        SIRNIKE_CODE, CODE_LOAD_ERRORS = await asyncio.get_event_loop().run_in_executor(
            None, load_code_from_github
        )
    LAST_ANALYSIS = ""
    _save_last_analysis("")  # сбрасываем историю — код обновился
    if CODE_LOAD_ERRORS:
        await msg.edit_text(
            f"⚠️ Не удалось загрузить: {', '.join(CODE_LOAD_ERRORS)}\n"
            f"Агенты заблокированы. Проверь репо и повтори /reload."
        )
    else:
        await msg.edit_text(
            f"Готово ✅\nЗагружено {len(SIRNIKE_CODE):,} символов из {GITHUB_REPO}"
        )


async def run_agent_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    agent_type: str,
):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return

    if CODE_LOAD_ERRORS:
        await update.message.reply_text(
            f"Код не загружен ({', '.join(CODE_LOAD_ERRORS)}).\nСделай /reload и попробуй снова."
        )
        return

    user_input = " ".join(context.args).strip() if context.args else ""

    default_tasks = {
        "qa": "Проведи полное тестирование всех основных сценариев бота.",
        "analyze": "Найди все критические и важные баги в коде.",
        "fix": "Предложи патчи для топ-5 самых важных проблем.",
    }
    agent_labels = {"qa": "QA", "analyze": "Analyst", "fix": "Fix"}

    task = user_input or default_tasks[agent_type]
    label = agent_labels[agent_type]

    preview = task[:80] + ("..." if len(task) > 80 else "")
    status_msg = await update.message.reply_text(
        f"{label}-агент работает... ⏳\n_{preview}_",
        parse_mode="Markdown",
    )

    result = await call_agent(agent_type, task)

    if agent_type == "analyze":
        global LAST_ANALYSIS
        LAST_ANALYSIS = result
        _save_last_analysis(result)

    filenames = {"qa": "qa_report.md", "analyze": "analyze_report.md", "fix": "fix_patches.md"}
    fname = filenames[agent_type]
    try:
        await status_msg.delete()
    except Exception:
        pass
    try:
        doc = InputFile(io.BytesIO(result.encode("utf-8")), filename=fname)
        await update.message.reply_document(document=doc)
    except Exception:
        logger.exception("Failed to send document, falling back to text")
        await send_long(update, result)


async def cmd_qa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, context, "qa")

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, context, "analyze")

async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, context, "fix")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    text = (update.message.text or "").strip().lower()
    if not text:
        return

    if any(w in text for w in ["баг", "ошибк", "краш", "проблем", "сломал", "не работ"]):
        await run_agent_command(update, context, "analyze")
    elif any(w in text for w in ["почини", "исправ", "патч", "фикс", "fix"]):
        await run_agent_command(update, context, "fix")
    elif any(w in text for w in ["протест", "проверь", "сценар", "тест"]):
        await run_agent_command(update, context, "qa")
    else:
        await update.message.reply_text(
            "Используй команды:\n"
            "/qa — тест\n"
            "/analyze — анализ\n"
            "/fix — патчи\n"
            "/help — справка"
        )


# ─── Автотест ─────────────────────────────────────────────────────────────────

async def auto_qa_loop(app):
    if not AUTO_QA_INTERVAL_H or not ADMIN_IDS:
        return
    interval = AUTO_QA_INTERVAL_H * 3600
    logger.info("Auto-QA started: every %dh, reporting to %s", AUTO_QA_INTERVAL_H, ADMIN_IDS[0])
    while True:
        await asyncio.sleep(interval)
        try:
            logger.info("Auto-QA: running scheduled test...")
            result = await call_agent("qa", "Проведи полное тестирование всех основных сценариев бота.")
            doc = InputFile(io.BytesIO(result.encode("utf-8")), filename="auto_qa_report.md")
            await app.bot.send_document(
                chat_id=ADMIN_IDS[0],
                document=doc,
                caption=f"🤖 Авто-QA отчёт (каждые {AUTO_QA_INTERVAL_H}ч)",
            )
            logger.info("Auto-QA: report sent")
        except Exception:
            logger.exception("Auto-QA failed")


# ─── Запуск ───────────────────────────────────────────────────────────────────

_auto_qa_task = None

async def post_init(app):
    global _auto_qa_task
    if AUTO_QA_INTERVAL_H and ADMIN_IDS:
        _auto_qa_task = asyncio.create_task(auto_qa_loop(app))
        logger.info("Auto-QA task created")


def main():
    app = Application.builder().token(AGENT_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("qa", cmd_qa))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("reset_analysis", cmd_reset_analysis))
    app.add_handler(CommandHandler("selftest", cmd_selftest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(
        "Agent bot started. Repo: %s, Code: %d chars, Auto-QA: %s",
        GITHUB_REPO,
        len(SIRNIKE_CODE),
        f"every {AUTO_QA_INTERVAL_H}h" if AUTO_QA_INTERVAL_H else "off",
    )
    app.run_polling()


if __name__ == "__main__":
    main()
