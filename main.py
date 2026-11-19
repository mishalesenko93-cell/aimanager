from dotenv import load_dotenv
load_dotenv()
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, HTTPException
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aurex")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Необязательно: Telegram ID людей, которые всегда могут создавать задачи.
# Админы группы тоже могут создавать задачи автоматически.
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

app = FastAPI(title="Aurex AI Manager")
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---- Хранилище в памяти (без базы данных) ----
# Сбрасывается при перезапуске сервиса.
TASKS: dict[int, list[dict]] = {}   # chat_id -> список задач
KNOWN_CHATS: set[int] = set()       # группы, где бот уже получал сообщения
_next_id = 1


async def tg(method: str, payload: dict):
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(f"{TG_API}/{method}", json=payload)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API error"))
        return data.get("result")


async def send_text(chat_id, text, business_connection_id=None):
    payload = {"chat_id": chat_id, "text": text}
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    return await tg("sendMessage", payload)


async def gemini_reply(user_text: str) -> str:
    if not GEMINI_API_KEY:
        return "Спасибо за сообщение! Я его получил, менеджер свяжется с вами при необходимости."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    system = (
        "Ты Aurex AI Manager — вежливый и профессиональный рабочий ассистент компании AUREX COMPANY. "
        "Отвечай на языке пользователя (по умолчанию на русском), коротко и по делу. "
        "Не выдумывай цены, сроки, правила и выполненные действия. "
        "Если информации не хватает, скажи, что уточнит менеджер. "
        "Не запрашивай пароли, коды входа и данные банковских карт."
    )
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.35, "maxOutputTokens": 450},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, params={"key": GEMINI_API_KEY}, json=body)
        r.raise_for_status()
        data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return "Не удалось подготовить ответ. Обратитесь, пожалуйста, к менеджеру."


async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await tg("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        return member.get("status") in ("creator", "administrator")
    except Exception:
        log.exception("getChatMember failed")
        return False


async def handle_group_message(message: dict):
    global _next_id
    chat = message["chat"]
    chat_id = chat["id"]
    if chat.get("type") in ("group", "supergroup"):
        KNOWN_CHATS.add(chat_id)

    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    first = text.split()[0]
    command = first.split("@")[0].lower()
    args = text[len(first):].strip()

    if command == "/task":
        if not user_id or not await is_admin(chat_id, user_id):
            await send_text(chat_id, "Создавать задачи могут только администраторы.")
            return
        # Формат: /task Название | @username | 2026-11-20 16:00
        parts = [p.strip() for p in args.split("|")]
        if len(parts) < 3:
            await send_text(chat_id, "Формат:\n/task Название | @username | ГГГГ-ММ-ДД ЧЧ:ММ")
            return
        title, assignee, due_text = parts[0], parts[1], parts[2]
        try:
            due = datetime.strptime(due_text, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
        except ValueError:
            await send_text(chat_id, "Дедлайн должен быть в формате ГГГГ-ММ-ДД ЧЧ:ММ (время по Киеву).")
            return
        task_id = _next_id
        _next_id += 1
        TASKS.setdefault(chat_id, []).append(
            {"id": task_id, "title": title, "assignee": assignee, "due": due, "reminded": False}
        )
        await send_text(
            chat_id,
            f"📌 НОВАЯ ЗАДАЧА #{task_id}\n\n"
            f"Задача: {title}\nИсполнитель: {assignee}\n"
            f"Дедлайн: {due.strftime('%d.%m.%Y %H:%M')} (Киев)\n\n"
            f"Когда выполните — напишите /done {task_id}",
        )

    elif command == "/tasks":
        tasks = sorted(TASKS.get(chat_id, []), key=lambda t: t["due"])
        if not tasks:
            await send_text(chat_id, "Открытых задач нет.")
        else:
            lines = ["📋 ОТКРЫТЫЕ ЗАДАЧИ"]
            for t in tasks[:30]:
                lines.append(f"#{t['id']} — {t['title']} | {t['assignee']} | до {t['due'].strftime('%d.%m %H:%M')}")
            await send_text(chat_id, "\n".join(lines))

    elif command == "/done":
        try:
            task_id = int(args.split()[0])
        except (ValueError, IndexError):
            await send_text(chat_id, "Формат: /done НОМЕР_ЗАДАЧИ")
            return
        tasks = TASKS.get(chat_id, [])
        for t in tasks:
            if t["id"] == task_id:
                tasks.remove(t)
                await send_text(chat_id, f"✅ Задача #{task_id} выполнена.")
                return
        await send_text(chat_id, "Задача не найдена или уже выполнена.")


async def handle_business_message(message: dict):
    text = (message.get("text") or "").strip()
    connection_id = message.get("business_connection_id")
    if not text or not connection_id:
        return
    chat_id = message["chat"]["id"]
    if text.startswith("/"):
        await send_text(chat_id, "Я помогаю с рабочими вопросами. Команды менеджера — в рабочей группе.", connection_id)
        return
    await send_text(chat_id, await gemini_reply(text), connection_id)


async def handle_private_message(message: dict):
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        if text.startswith("/start"):
            await send_text(message["chat"]["id"], "Привет! Я AUREX COMPANY AI. Задайте рабочий вопрос.")
        return
    await send_text(message["chat"]["id"], await gemini_reply(text))


async def send_start_message():
    for chat_id in list(KNOWN_CHATS):
        try:
            await send_text(chat_id, "☀️ Доброе утро, команда! Рабочий день начался. Проверьте свои задачи и пишите о прогрессе в группу.")
        except Exception:
            log.exception("start message failed for %s", chat_id)


async def send_end_report():
    for chat_id in list(KNOWN_CHATS):
        try:
            count = len(TASKS.get(chat_id, []))
            await send_text(chat_id, f"🌙 Итоги дня\nОткрытых задач: {count}\nОбновите статусы и сообщите менеджеру, если нужна помощь.")
        except Exception:
            log.exception("end report failed for %s", chat_id)


async def check_overdue():
    now = datetime.now(TIMEZONE)
    for chat_id, tasks in list(TASKS.items()):
        for t in tasks:
            if not t["reminded"] and t["due"] < now:
                t["reminded"] = True
                try:
                    await send_text(
                        chat_id,
                        f"⏰ {t['assignee']}, задача #{t['id']} «{t['title']}» просрочена. "
                        "Обновите статус или сообщите менеджеру, если что-то мешает.",
                    )
                except Exception:
                    log.exception("overdue reminder failed")


@app.on_event("startup")
async def startup():
    scheduler.add_job(send_start_message, CronTrigger(hour=8, minute=0, timezone=TIMEZONE))
    scheduler.add_job(send_end_report, CronTrigger(hour=21, minute=0, timezone=TIMEZONE))
    scheduler.add_job(check_overdue, CronTrigger(minute="*/15", timezone=TIMEZONE))
    scheduler.start()
    log.info("Aurex started")


@app.on_event("shutdown")
async def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
async def health():
    return {"service": "Aurex AI Manager", "status": "online", "timezone": str(TIMEZONE)}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
    update = await request.json()
    try:
        if "message" in update:
            msg = update["message"]
            if msg["chat"].get("type") == "private":
                await handle_private_message(msg)
            else:
                await handle_group_message(msg)
        elif "business_message" in update:
            await handle_business_message(update["business_message"])
        elif "business_connection" in update:
            bc = update["business_connection"]
            log.info("Business connection: enabled=%s", bc.get("is_enabled"))
    except Exception:
        log.exception("Update handling failed")
    return {"ok": True}
