"""
Агент-бот для Сырника — CrewAI версия.
6 специализированных агентов на Gemini (бесплатно).
Команды: /audit, /tech, /ux, /security, /marketing, /database, /performance
"""

import asyncio
import io
import logging
import os
import urllib.request
import urllib.error

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from crewai import Agent, Task, Crew, Process, LLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Конфиг ───────────────────────────────────────────────────────────────────

AGENT_BOT_TOKEN = os.environ["AGENT_BOT_TOKEN"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
# LiteLLM (движок CrewAI) читает ключ через GOOGLE_API_KEY
os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
GITHUB_REPO     = os.environ.get("SIRNIKE_REPO", os.environ.get("GITHUB_REPO", "gorbdan/sirnike"))
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
ADMIN_IDS_RAW   = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS       = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip()] if ADMIN_IDS_RAW else []
AUTO_QA_INTERVAL_H = int(os.environ.get("AUTO_QA_INTERVAL_H", "0"))

GITHUB_FILES = ["SirNike.py", "config.py", "db.py", "requirements.txt", "AGENT_NOTES.md"]

logger.info("Repo: %r", GITHUB_REPO)

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


def load_code_from_github() -> tuple[str, list[str]]:
    parts, failed = [], []
    for fname in GITHUB_FILES:
        content = fetch_github_file(GITHUB_REPO, fname, GITHUB_TOKEN)
        if content is None:
            failed.append(fname)
            continue
        lang = "python" if fname.endswith(".py") else "text"
        parts.append(f"### {fname}\n```{lang}\n{content}\n```")
    return "\n\n".join(parts), failed


SIRNIKE_CODE, CODE_LOAD_ERRORS = load_code_from_github()
RELOAD_LOCK = asyncio.Lock()
if CODE_LOAD_ERRORS:
    logger.warning("Failed to load: %s", CODE_LOAD_ERRORS)
else:
    logger.info("Total: %d chars", len(SIRNIKE_CODE))

# ── LLM ───────────────────────────────────────────────────────────────────────

def make_llm() -> LLM:
    model = os.environ.get("GEMINI_MODEL", "gemini/gemini-2.0-flash")
    return LLM(model=model, api_key=GEMINI_API_KEY)

# ── Агенты ────────────────────────────────────────────────────────────────────

BOT_CONTEXT = """
Telegram-бот "Сырник" — AI-генератор изображений и видео.
Стек: Python 3.12, python-telegram-bot, SQLite, aiohttp.
Провайдеры: Zveno (Gemini), MashaGPT, YesAPI, Seedance (видео).
Деплой: BotHost + Docker. Валюта: изюминки.
asyncio — однопоточный event loop. Операции между await-точками атомарны.
"""

AGENTS_CONFIG = {
    "tech": {
        "role": "Технический аналитик",
        "goal": "Найти технические баги: необработанные исключения, потери данных, async-проблемы, утечки памяти",
        "backstory": "Опытный Python/asyncio разработчик. Знает что в однопоточном asyncio нет race condition без await между операциями.",
        "focus": "исключения, потери изюминок при крашах, проблемы очереди генерации, утечки памяти",
    },
    "ux": {
        "role": "UX-аналитик",
        "goal": "Найти проблемы с пользовательским опытом: непонятные сообщения, плохой онбординг, запутанные сценарии",
        "backstory": "Специалист по UX Telegram-ботов. Смотрит глазами пользователя.",
        "focus": "тексты сообщений, онбординг новых пользователей, кнопки, сценарии, ошибки которые видит юзер",
    },
    "security": {
        "role": "Security-аналитик",
        "goal": "Найти уязвимости: проблемы с платежами, валидацией, SQL-инъекции, утечки данных",
        "backstory": "Специалист по безопасности с фокусом на платёжные системы.",
        "focus": "валидация платёжных payload, SQL-запросы, защита от накрутки, безопасность API",
    },
    "marketing": {
        "role": "Маркетинг-аналитик",
        "goal": "Оценить реферальную систему, монетизацию, удержание пользователей",
        "backstory": "Growth-менеджер с опытом в Telegram-ботах и freemium-монетизации.",
        "focus": "реферальная система, пакеты изюминок, бесплатные генерации, конверсия в покупку, retention",
    },
    "database": {
        "role": "Database-инженер",
        "goal": "Найти проблемы с данными: неатомарные операции, потери при крашах, дубли записей",
        "backstory": "Специалист по SQLite и целостности данных.",
        "focus": "транзакции, INSERT OR IGNORE, потеря данных при сбоях, схема БД, дублирование",
    },
    "performance": {
        "role": "Performance-инженер",
        "goal": "Найти узкие места: очередь генерации, память, медленные запросы, блокирующий I/O",
        "backstory": "Специалист по оптимизации Python-сервисов под нагрузкой.",
        "focus": "очередь генерации, кэш изображений, медленные DB-запросы, блокирующий I/O в async-контексте",
    },
}

TASK_TEMPLATE = """{bot_context}

Код проекта:
{code}

Проанализируй с точки зрения: {focus}

Для каждой проблемы укажи:
- Файл и строка
- Описание проблемы
- Сценарий воспроизведения
- Серьёзность: 🔴 критично / 🟡 важно / 🟢 незначительно
- Предложение по фиксу

Отвечай на русском, чётко по пунктам."""

FOLLOWUP_TEMPLATE = """На основе предыдущих находок, проанализируй код со своей точки зрения: {focus}

Не повторяй уже найденное. Добавляй только то что в твоей зоне ответственности.
Серьёзность: 🔴 критично / 🟡 важно / 🟢 незначительно. На русском."""


def run_single_agent(agent_key: str, code: str) -> str:
    cfg = AGENTS_CONFIG[agent_key]
    llm = make_llm()

    agent = Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=cfg["backstory"],
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )
    task = Task(
        description=TASK_TEMPLATE.format(
            bot_context=BOT_CONTEXT, code=code, focus=cfg["focus"]
        ),
        expected_output=f"Список проблем: {cfg['role']}",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
    return str(crew.kickoff())


def run_full_audit(code: str) -> str:
    llm = make_llm()

    agents = {
        key: Agent(
            role=cfg["role"],
            goal=cfg["goal"],
            backstory=cfg["backstory"],
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )
        for key, cfg in AGENTS_CONFIG.items()
    }

    # Первый агент получает полный код
    first_task = Task(
        description=TASK_TEMPLATE.format(
            bot_context=BOT_CONTEXT,
            code=code,
            focus=AGENTS_CONFIG["tech"]["focus"],
        ),
        expected_output="Список технических багов",
        agent=agents["tech"],
    )

    # Остальные агенты видят предыдущие находки через context
    prev_tasks = [first_task]
    followup_tasks = []
    for key in ["ux", "security", "marketing", "database", "performance"]:
        task = Task(
            description=FOLLOWUP_TEMPLATE.format(focus=AGENTS_CONFIG[key]["focus"]),
            expected_output=f"Находки: {AGENTS_CONFIG[key]['role']}",
            agent=agents[key],
            context=prev_tasks.copy(),
        )
        followup_tasks.append(task)
        prev_tasks.append(task)

    # Финальный репортёр собирает всё
    reporter = Agent(
        role="Главный редактор отчёта",
        goal="Собрать все находки в единый структурированный отчёт без дублей",
        backstory="Технический редактор, специалист по audit-отчётам.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )
    final_task = Task(
        description="""Собери все находки в единый отчёт. Убери дубли. Расставь по приоритету.

Структура:
# Полный аудит Сырника

## 🔴 Критические баги
## 🟡 Важные проблемы
## 🟢 Незначительные замечания
## ✅ Что работает хорошо

На русском.""",
        expected_output="Полный структурированный отчёт",
        agent=reporter,
        context=prev_tasks,
    )

    all_tasks = [first_task] + followup_tasks + [final_task]
    all_agents = list(agents.values()) + [reporter]

    crew = Crew(
        agents=all_agents,
        tasks=all_tasks,
        process=Process.sequential,
        verbose=False,
    )
    return str(crew.kickoff())


# ── Telegram хендлеры ─────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


async def run_crew_command(
    update: Update,
    runner_fn,
    filename: str,
    label: str,
):
    if not is_allowed(update.effective_user.id):
        return
    if CODE_LOAD_ERRORS:
        await update.message.reply_text(
            f"Код не загружен ({', '.join(CODE_LOAD_ERRORS)}).\nСделай /reload."
        )
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
        logger.exception("Crew failed")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_full_audit(SIRNIKE_CODE), "full_audit.md", "🔍 Полный аудит (6 агентов)")

async def cmd_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_single_agent("tech", SIRNIKE_CODE), "tech_report.md", "⚙️ Tech-агент")

async def cmd_ux(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_single_agent("ux", SIRNIKE_CODE), "ux_report.md", "👤 UX-агент")

async def cmd_security(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_single_agent("security", SIRNIKE_CODE), "security_report.md", "🔒 Security-агент")

async def cmd_marketing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_single_agent("marketing", SIRNIKE_CODE), "marketing_report.md", "📊 Marketing-агент")

async def cmd_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_single_agent("database", SIRNIKE_CODE), "database_report.md", "🗄️ Database-агент")

async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_crew_command(update, lambda: run_single_agent("performance", SIRNIKE_CODE), "performance_report.md", "⚡ Performance-агент")


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global SIRNIKE_CODE, CODE_LOAD_ERRORS
    msg = await update.message.reply_text("Загружаю код с GitHub...")
    async with RELOAD_LOCK:
        SIRNIKE_CODE, CODE_LOAD_ERRORS = await asyncio.get_event_loop().run_in_executor(
            None, load_code_from_github
        )
    if CODE_LOAD_ERRORS:
        await msg.edit_text(f"⚠️ Не удалось загрузить: {', '.join(CODE_LOAD_ERRORS)}\nПроверь репо и повтори /reload.")
    else:
        await msg.edit_text(f"✅ Загружено {len(SIRNIKE_CODE):,} символов из {GITHUB_REPO}")


async def cmd_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    msg = await update.message.reply_text("🔍 Самодиагностика...")
    results = []

    _, failed = load_code_from_github()
    results.append(f"{'✅' if not failed else '❌'} GitHub: {'все файлы загружены' if not failed else ', '.join(failed)}")
    results.append(f"✅ Gemini API: ключ {'настроен' if GEMINI_API_KEY else '❌ не задан'}")
    results.append(f"✅ Агентов: {len(AGENTS_CONFIG)} ({', '.join(AGENTS_CONFIG.keys())})")
    results.append(f"✅ Код: {len(SIRNIKE_CODE):,} символов")

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
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: run_full_audit(SIRNIKE_CODE)
            )
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

    logger.info("Agent bot started (CrewAI + Gemini). Repo: %s, Code: %d chars", GITHUB_REPO, len(SIRNIKE_CODE))
    app.run_polling()


if __name__ == "__main__":
    main()
