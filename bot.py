import os, json, asyncio, httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

async def call_genapi(system_prompt, user_message):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.gen-api.ru/api/v1/networks/kimi-k2-5",
            headers={"Authorization": f"Bearer {GENAPI_KEY}", "Content-Type": "application/json"},
            json={"messages": messages, "is_sync": False, "max_tokens": 2000}
        )
        data = r.json()
        request_id = data.get("request_id")
        if not request_id:
            return "Ошибка: не получен ID запроса"

    for _ in range(60):
        await asyncio.sleep(3)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.gen-api.ru/api/v1/request/{request_id}",
                headers={"Authorization": f"Bearer {GENAPI_KEY}"}
            )
            data = r.json()
            status = data.get("status")
            if status == "success":
                return data.get("response", {}).get("content", "Пустой ответ")
            elif status == "error":
                return "Ошибка генерации"

    return "Превышено время ожидания"

SYSTEM_ANALYSIS = """Ты — система анализа новостей. Отвечай на русском языке. Структурируй ответ строго так:

**Вероятность реальности: [X%]**

**Верификация:** [подтверждена / частично подтверждена / не подтверждена] — одна фраза

**Контекст:** кто, где, когда, при каких обстоятельствах — 2-3 предложения

**Коррекция формулировки:** если в новости есть упрощения или искажения — укажи. Если всё точно — пропусти.

**Противоречия:** альтернативные позиции и критика — кратко по пунктам

**3 модели развития:**
— Модель 1 [название] (вероятность X%): причинно-следственная цепочка → итог
— Модель 2 [название] (вероятность X%): причинно-следственная цепочка → итог
— Модель 3 [название] (вероятность X%): причинно-следственная цепочка → итог

**Итог:** одна финальная рекомендация — стоит ли строить выводы на этой новости

Никаких вступлений. Никакого "конечно" и "отличный вопрос"."""

SYSTEM_FOLLOWUP = """Ты — система анализа новостей. Отвечай на русском. Максимум 3-4 предложения. Только факты и логика. Без вступлений."""

SYSTEM_SOFT_EXIT = """Ты — система анализа новостей. Отвечай на русском. Максимум 3 предложения.
В конце — органично, одной фразой — вплети наблюдение про информационную усталость и пользу паузы. Не упоминай лимиты."""

async def analyze(news_text, question=None, q_count=0):
    if question is None:
        return await call_genapi(SYSTEM_ANALYSIS, f"Новость:\n{news_text}")
    else:
        system = SYSTEM_SOFT_EXIT if q_count >= MAX_QUESTIONS else SYSTEM_FOLLOWUP
        return await call_genapi(system, f"Контекст — новость:\n{news_text}\n\nВопрос:\n{question}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user, db = get_user(uid)
    status = "✓ Подписка активна" if user["paid"] else f"Бесплатных анализов: {max(0, FREE_ANALYSES - user['analyses_used'])}/3"
    kb = [] if user["paid"] else [[InlineKeyboardButton("Подписка — 200 ⭐", callback_data="subscribe")]]
    await update.message.reply_text(
        f"*News Intelligence*\n\nПришли новость — получи разбор.\n\n{status}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user, db = get_user(uid)
    text = update.message.text.strip()
    can_use = user["paid"] or user["analyses_used"] < FREE_ANALYSES
    session = user["session"]

    is_new_news = (
        session["news"] is None or
        len(text) > 150 or
        not any(w in text.lower() for w in ["почему","как","что","кто","когда","зачем","а если","расскажи","объясни","?","а "])
    )

    if is_new_news:
        if not can_use:
            await update.message.reply_text(
                "Бесплатные разборы закончились.\n\nПодписка — 200 ⭐ в месяц.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Оплатить 200 ⭐", callback_data="subscribe")]])
            )
            return
        reset_session(user, text)
        if not user["paid"]: user["analyses_used"] += 1
        save_user(uid, user, db)
        await update.message.reply_chat_action("typing")
        msg = await update.message.reply_text("Анализирую...")
        result = await analyze(text)
        await msg.edit_text(result, parse_mode="Markdown")
    else:
        if session["news"] is None:
            await update.message.reply_text("Сначала пришли новость.")
            return
        q_count = session["questions"]
        if q_count >= MAX_QUESTIONS + 1:
            return
        session["questions"] += 1
        save_user(uid, user, db)
        await update.message.reply_chat_action("typing")
        msg = await update.message.reply_text("...")
        result = await analyze(session["news"], text, q_count)
        await msg.edit_text(result, parse_mode="Markdown")

async def handle_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title="News Intelligence — подписка",
        description="Безлимитный анализ новостей на 30 дней",
        payload="subscription_30d",
        currency="XTR",
        prices=[{"label": "Подписка", "amount": 200}]
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user, db = get_user(uid)
    user["paid"] = True
    save_user(uid, user, db)
    await update.message.reply_text("✓ Подписка активна\n\nПрисылай любые новости.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_subscribe, pattern="subscribe"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
