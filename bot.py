import os, json, asyncio, httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler

GENAPI_KEY = os.getenv("GENAPI_KEY", "YOUR_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN")
FREE_ANALYSES = 3
MAX_QUESTIONS = 4
DB_FILE = "users.json"

def load_db():
    try:
        with open(DB_FILE) as f: return json.load(f)
    except: return {}

def save_db(db):
    with open(DB_FILE, "w") as f: json.dump(db, f)

def get_user(uid):
    db = load_db()
    uid = str(uid)
    if uid not in db:
        db[uid] = {"paid": False, "analyses_used": 0, "session": {"news": None, "questions": 0}}
        save_db(db)
    return db[uid], db

def save_user(uid, user, db):
    db[str(uid)] = user
    save_db(db)

def reset_session(user, news_text):
    user["session"] = {"news": news_text, "questions": 0}

def reply_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Статус"), KeyboardButton("Новый запрос")]],
        resize_keyboard=True,
        persistent=True
    )

def inline_keyboard(paid=False):
    buttons = []
    if not paid:
        buttons.append([InlineKeyboardButton("Подписка 200 Stars", callback_data="subscribe")])
    return InlineKeyboardMarkup(buttons) if buttons else None

async def call_genapi(system_prompt, user_message):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.gen-api.ru/api/v1/networks/kimi-k2-5",
                headers={"Authorization": f"Bearer {GENAPI_KEY}", "Content-Type": "application/json"},
                json={"messages": messages, "is_sync": False, "max_tokens": 2000}
            )
            if r.status_code != 200:
                return f"Ошибка API: статус {r.status_code}"
            data = r.json()
            request_id = data.get("request_id")
            if not request_id:
                return f"Ошибка: {data.get('message', 'нет request_id')}"

        for attempt in range(40):
            await asyncio.sleep(4)
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        f"https://api.gen-api.ru/api/v1/request/{request_id}",
                        headers={"Authorization": f"Bearer {GENAPI_KEY}"}
                    )
                    if r.status_code != 200:
                        continue
                    text = r.text.strip()
                    if not text:
                        continue
                    data = r.json()
                    status = data.get("status")
                    if status == "success":
                        content = data.get("response", {})
                        if isinstance(content, dict):
                            return content.get("content") or content.get("text") or str(content)
                        return str(content)
                    elif status == "error":
                        return f"Ошибка генерации: {data.get('message', '')}"
            except Exception:
                continue

        return "Превышено время ожидания. Попробуй ещё раз."
    except Exception as e:
        return f"Ошибка соединения: {str(e)}"

SYSTEM_ANALYSIS = """Ты система анализа новостей. Отвечай на русском. Структурируй ответ строго так:

Вероятность реальности: [X%]
Верификация: [подтверждена / частично / не подтверждена] - одна фраза
Контекст: кто, где, когда - 2-3 предложения
Противоречия: альтернативные позиции - кратко по пунктам
3 модели развития:
- Модель 1 [название] (вероятность X%): цепочка - итог
- Модель 2 [название] (вероятность X%): цепочка - итог
- Модель 3 [название] (вероятность X%): цепочка - итог
Итог: одна рекомендация

Никаких вступлений."""

SYSTEM_FOLLOWUP = """Ты система анализа новостей. Отвечай на русском. Максимум 3-4 предложения. Только факты. Без вступлений."""

SYSTEM_SOFT_EXIT = """Ты система анализа новостей. Отвечай на русском. Максимум 3 предложения.
В конце органично одной фразой вплети мысль про информационную усталость. Не упоминай лимиты."""

async def analyze(news_text, question=None, q_count=0):
    if question is None:
        return await call_genapi(SYSTEM_ANALYSIS, f"Новость:\n{news_text}")
    system = SYSTEM_SOFT_EXIT if q_count >= MAX_QUESTIONS else SYSTEM_FOLLOWUP
    return await call_genapi(system, f"Контекст:\n{news_text}\n\nВопрос:\n{question}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user, db = get_user(uid)
    left = max(0, FREE_ANALYSES - user["analyses_used"])
    status = "Подписка активна" if user["paid"] else f"Бесплатных анализов: {left}/3"
    kb = inline_keyboard(user["paid"])
    await update.message.reply_text(
        f"News Intelligence\n\nПришли новость - получи разбор.\n\n{status}",
        reply_markup=reply_keyboard()
    )
    if kb:
        await update.message.reply_text("Оформить подписку:", reply_markup=kb)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    user, db = get_user(uid)
    if query.data == "subscribe":
        await context.bot.send_invoice(
            chat_id=uid,
            title="News Intelligence подписка",
            description="Безлимитный анализ новостей на 30 дней",
            payload="subscription_30d",
            currency="XTR",
            prices=[{"label": "Подписка", "amount": 200}]
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user, db = get_user(uid)
    msg = update.message
    is_forward = bool(msg.forward_origin or msg.forward_from or msg.forward_from_chat)
    text = (msg.text or msg.caption or "").strip()

    if not text:
        await msg.reply_text("Пришли текст новости.", reply_markup=reply_keyboard())
        return

    if text == "Статус":
        left = max(0, FREE_ANALYSES - user["analyses_used"])
        news = user["session"]["news"]
        q = user["session"]["questions"]
        status_text = "Подписка активна" if user["paid"] else f"Бесплатных анализов: {left}/3"
        session_text = f"Текущая новость: есть ({q}/{MAX_QUESTIONS} вопросов)" if news else "Текущей новости нет"
        kb = inline_keyboard(user["paid"])
        await msg.reply_text(f"Статус\n\n{status_text}\n{session_text}", reply_markup=kb or reply_keyboard())
        return

    if text == "Новый запрос":
        reset_session(user, None)
        save_user(uid, user, db)
        await msg.reply_text("Контекст сброшен. Пришли новую новость.", reply_markup=reply_keyboard())
        return

    can_use = user["paid"] or user["analyses_used"] < FREE_ANALYSES
    session = user["session"]

    is_new_news = (
        session["news"] is None or
        is_forward or
        len(text) > 150 or
        not any(w in text.lower() for w in ["почему","как","что","кто","когда","зачем","а если","расскажи","объясни","?"])
    )

    if is_new_news:
        if not can_use:
            kb = inline_keyboard(user["paid"])
            await msg.reply_text("Бесплатные разборы закончились.", reply_markup=kb or reply_keyboard())
            return
        reset_session(user, text)
        if not user["paid"]: user["analyses_used"] += 1
        save_user(uid, user, db)
        await msg.reply_chat_action("typing")
        sent = await msg.reply_text("Анализирую... (30-60 сек)")
        result = await analyze(text)
        left = max(0, FREE_ANALYSES - user["analyses_used"])
        footer = "" if user["paid"] else f"\n\nОсталось анализов: {left}/3"
        kb = inline_keyboard(user["paid"])
        await sent.edit_text(result + footer, reply_markup=kb)
    else:
        if session["news"] is None:
            await msg.reply_text("Сначала пришли новость.", reply_markup=reply_keyboard())
            return
        q_count = session["questions"]
        if q_count >= MAX_QUESTIONS + 1:
            return
        session["questions"] += 1
        save_user(uid, user, db)
        await msg.reply_chat_action("typing")
        sent = await msg.reply_text("...")
        result = await analyze(session["news"], text, q_count)
        await sent.edit_text(result)

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user, db = get_user(uid)
    user["paid"] = True
    save_user(uid, user, db)
    await update.message.reply_text("Подписка активна\n\nПрисылай любые новости.", reply_markup=reply_keyboard())

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler((filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
