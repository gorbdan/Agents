"""
Агент-бот для Сырника.
Загружает код с GitHub, анализирует через Claude.
Команды: /qa, /analyze, /fix, /reload, /help
"""

import asyncio
import logging
import os
import urllib.request
import urllib.error
import json

from telegram import Update
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
GITHUB_REPO = os.environ.get("GITHUB_REPO", "gorbdan/sirnike")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()] if ADMIN_IDS_RAW else []

MODEL = "claude-sonnet-4-20250514"

GITHUB_FILES = ["SirNike.py", "config.py", "db.py", "requirements.txt"]

# ─── Загрузка кода с GitHub ────────────────────────────────────────────────────

def fetch_github_file(repo: str, filepath: str, token: str = "") -> str:
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
        return f"# Не удалось загрузить {filepath}: HTTP {e.code}"
    except Exception as e:
        logger.warning("GitHub fetch error for %s: %s", filepath, e)
        return f"# Не удалось загрузить {filepath}: {e}"


def load_code_from_github() -> str:
    parts = []
    for fname in GITHUB_FILES:
        content = fetch_github_file(GITHUB_REPO, fname, GITHUB_TOKEN)
        ext = fname.rsplit(".", 1)[-1]
        lang = "python" if ext == "py" else "text"
        parts.append(f"### {fname}\n```{lang}\n{content}\n```")
    return "\n\n".join(parts)


# Загружаем при старте
SIRNIKE_CODE = load_code_from_github()
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

Смотри на:
1. Race conditions и конкурентность
2. Утечки памяти и ресурсов
3. Непойманные исключения и потери данных
4. Безопасность (валидация, инъекции)
5. Edge cases в бизнес-логике (изюминки, рефералы, оплата)
6. Персистентность (__img__ кэш, аватары)
7. Узкие места производительности

Для каждой проблемы: файл + строка → описание → сценарий воспроизведения → серьёзность → предложение по фиксу.
Серьёзность: 🔴 критично / 🟡 важно / 🟢 незначительно.
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
    suffixes = {"qa": QA_SUFFIX, "analyze": ANALYZE_SUFFIX, "fix": FIX_SUFFIX}
    return base + suffixes[agent_type]


# ─── Anthropic клиент ──────────────────────────────────────────────────────────

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


async def call_agent(agent_type: str, user_message: str) -> str:
    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=4096,
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

async def send_long(update: Update, text: str, status_msg=None):
    max_len = 4000
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]

    for i, chunk in enumerate(chunks):
        suffix = f"\n\n_({i+1}/{len(chunks)})_" if len(chunks) > 1 else ""
        if i == 0 and status_msg:
            await status_msg.edit_text(chunk + suffix, parse_mode="Markdown")
        else:
            await update.message.reply_text(chunk + suffix, parse_mode="Markdown")


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
        "/reload — перезагрузить код с GitHub\n\n"
        "Примеры:\n"
        "/qa онбординг нового пользователя\n"
        "/analyze race conditions в очереди\n"
        "/fix потеря изюминок при краше воркера"
    )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global SIRNIKE_CODE
    msg = await update.message.reply_text("Загружаю код с GitHub...")
    SIRNIKE_CODE = load_code_from_github()
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

    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: asyncio.run(call_agent(agent_type, task))
    )

    await send_long(update, result, status_msg)


# Обёртки для каждой команды
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


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    # Фикс для asyncio внутри run_in_executor
    import nest_asyncio
    nest_asyncio.apply()

    app = Application.builder().token(AGENT_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("qa", cmd_qa))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(
        "Agent bot started. Repo: %s, Code: %d chars",
        GITHUB_REPO,
        len(SIRNIKE_CODE),
    )
    app.run_polling()


if __name__ == "__main__":
    main()
