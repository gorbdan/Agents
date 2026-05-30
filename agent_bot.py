"""
Агент-бот для Сырника — Vertex AI (Gemini) версия.
6 специализированных агентов. Контекст 1M токенов — весь код целиком.
Команды: /audit, /tech, /ux, /security, /marketing, /database, /performance
"""

import asyncio
import io
import logging
import os
import tempfile
import urllib.request
import urllib.error

from google import genai
from google.genai import types
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Конфиг ───────────────────────────────────────────────────────────────────

AGENT_BOT_TOKEN  = os.environ["AGENT_BOT_TOKEN"]
GOOGLE_PROJECT_ID = os.environ["GOOGLE_PROJECT_ID"]
VERTEX_LOCATION  = os.environ.get("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-001")
GITHUB_REPO      = os.environ.get("SIRNIKE_REPO", os.environ.get("GITHUB_REPO", "gorbdan/sirnike"))
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
ADMIN_IDS_RAW    = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS        = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()] if ADMIN_IDS_RAW else []
AUTO_QA_INTERVAL_H = int(os.environ.get("AUTO_QA_INTERVAL_H", "0"))

# Записываем JSON сервисного аккаунта во временный файл для google SDK
_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
if _SA_JSON:
    _sa_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _sa_file.write(_SA_JSON)
    _sa_file.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _sa_file.name
    logger.info("Service account credentials loaded from env")
else:
    logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — will try ADC")

GITHUB_FILES = ["SirNike.py", "config.py", "db.py", "requirements.txt", "AGENT_NOTES.md"]

logger.info("Repo: %r | Project: %s | Location: %s", GITHUB_REPO, GOOGLE_PROJECT_ID, VERTEX_LOCATION)

# ── GitHub ────────────────────────────────────────────────────────────────────

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


def build_code_block(files: dict[str, str], filenames: list[str]) -> str:
    parts = []
    for fname in filenames:
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

# ── Gemini клиент ─────────────────────────────────────────────────────────────

def make_client() -> genai.Client:
    return genai.Client(vertexai=True, project=GOOGLE_PROJECT_ID, location=VERTEX_LOCATION)

# ── Агенты ────────────────────────────────────────────────────────────────────

BOT_CONTEXT = """Telegram-бот "Сырник" — AI-генератор изображений и видео.
Стек: Python 3.12, python-telegram-bot, SQLite, aiohttp.
Провайдеры: Zveno (Gemini), MashaGPT, YesAPI, Seedance (видео).
Деплой: BotHost + Docker. Валюта: изюминки.
asyncio — однопоточный event loop. Операции между await-точками атомарны."""

AGENTS_CONFIG = {
    "tech": {
        "role": "Технический аналитик Python/asyncio",
        "focus": "необработанные исключения, потери изюминок при крашах, проблемы очереди генерации, утечки памяти",
        "files": ["SirNike.py", "requirements.txt", "AGENT_NOTES.md"],
    },
    "ux": {
        "role": "UX-аналитик Telegram-ботов",
        "focus": "тексты сообщений, онбординг новых пользователей, кнопки, сценарии, ошибки которые видит юзер",
        "files": ["SirNike.py", "AGENT_NOTES.md"],
    },
    "security": {
        "role": "Security-аналитик платёжных систем",
        "focus": "валидация платёжных payload, SQL-запросы, защита от накрутки, безопасность API",
        "files": ["SirNike.py", "config.py", "AGENT_NOTES.md"],
    },
    "marketing": {
        "role": "Growth-менеджер Telegram-ботов",
        "focus": "реферальная система, пакеты изюминок, бесплатные генерации, конверсия в покупку, retention",
        "files": ["SirNike.py", "AGENT_NOTES.md"],
    },
    "database": {
        "role": "Database-инженер SQLite",
        "focus": "транзакции, INSERT OR IGNORE, потеря данных при сбоях, схема БД, дублирование",
        "files": ["db.py", "SirNike.py", "AGENT_NOTES.md"],
    },
    "performance": {
        "role": "Performance-инженер Python",
        "focus": "очередь генерации, кэш изображений, медленные DB-запросы, блокирующий I/O в async-контексте",
        "files": ["SirNike.py", "AGENT_NOTES.md"],
    },
}

SYSTEM_PROMPT = """Ты {role}. Анализируй код Telegram-бота "Сырник".

Контекст: {bot_context}

СТРОГИЕ ПРАВИЛА:
1. Пиши ТОЛЬКО о том что реально видишь в предоставленном коде.
2. Каждая находка ОБЯЗАНА содержать цитату конкретной строки кода.
3. НЕ пиши шаблонные советы без конкретного места в коде.
4. Если проблем нет — напиши: "В коде проблем не обнаружено."
5. Не повторяй находки из AGENT_NOTES.md — они уже известны.

Для каждой находки:
- Цитата: `конкретная строка кода`
- Описание проблемы
- Сценарий воспроизведения
- Серьёзность: 🔴 критично / 🟡 важно / 🟢 незначительно
- Предложение по фиксу

Фокус: {focus}
Отвечай на русском."""


def call_gemini_agent(agent_key: str) -> str:
    cfg = AGENTS_CONFIG[agent_key]
    code = build_code_block(CODE_FILES, cfg["files"])
    client = make_client()

    system = SYSTEM_PROMPT.format(
        role=cfg["role"],
        bot_context=BOT_CONTEXT,
        focus=cfg["focus"],
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"Вот код для анализа:\n\n{code}",
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            max_output_tokens=8000,
        ),
    )
    return response.text


def run_full_audit() -> str:
    results = []
    for key, cfg in AGENTS_CONFIG.items():
        logger.info("Running agent: %s", key)
        try:
            result = call_gemini_agent(key)
            results.append(f"## {cfg['role']}\n\n{result}")
        except Exception as e:
            logger.exception("Agent %s failed", key)
            results.append(f"## {cfg['role']}\n\n❌ Ошибка: {e}")
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
    msg = await update.message.reply_text(f"{label} работает... ⏳\n(пара минут)")
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, runner_fn)
        try:
            await msg.delete()
        except Exception:
            pass
        doc = InputFile(io.BytesIO(result.encode("utf-8")), filename=filename)
        await update.message.reply_document(document=doc)
    except Exception as e:
        logger.exception("Agent failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, run_full_audit, "full_audit.md", "🔍 Полный аудит (6 агентов)")

async def cmd_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: call_gemini_agent("tech"), "tech_report.md", "⚙️ Tech-агент")

async def cmd_ux(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: call_gemini_agent("ux"), "ux_report.md", "👤 UX-агент")

async def cmd_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: call_gemini_agent("security"), "security_report.md", "🔒 Security-агент")

async def cmd_marketing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: call_gemini_agent("marketing"), "marketing_report.md", "📊 Marketing-агент")

async def cmd_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: call_gemini_agent("database"), "database_report.md", "🗄️ Database-агент")

async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_agent_command(update, lambda: call_gemini_agent("performance"), "performance_report.md", "⚡ Performance-агент")


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
    results.append(f"{'✅' if _SA_JSON else '⚠️'} Credentials: {'сервисный аккаунт' if _SA_JSON else 'ADC'}")
    results.append(f"✅ Модель: {GEMINI_MODEL} @ {VERTEX_LOCATION}")
    results.append(f"✅ Агентов: {len(AGENTS_CONFIG)}")
    total = sum(len(v) for v in CODE_FILES.values())
    results.append(f"✅ Код: {total:,} символов в {len(CODE_FILES)} файлах")
    await msg.edit_text("Самодиагностика:\n\n" + "\n".join(results))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Агенты Сырника 🧀\n\n"
        "/audit — полный аудит (все 6 агентов)\n\n"
        "/tech — технические баги\n"
        "/ux — пользовательский опыт\n"
        "/security — безопасность и платежи\n"
        "/marketing — монетизация и рефералы\n"
        "/database — целостность данных\n"
        "/performance — производительность\n\n"
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
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("selftest", cmd_selftest))

    total = sum(len(v) for v in CODE_FILES.values())
    logger.info("Agent bot started (Vertex AI Gemini). Repo: %s, Files: %d, Chars: %d",
                GITHUB_REPO, len(CODE_FILES), total)
    app.run_polling()


if __name__ == "__main__":
    main()
