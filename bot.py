import os, json, asyncio, httpx, asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler

GENAPI_KEY = os.getenv("GENAPI_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
FREE_ANALYSES = 3
MAX_QUESTIONS = 4
WHITELIST_IDS = {7759092298, 888441622}
db_pool = None

async def get_db():
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await db_pool.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            phone TEXT,
            phone_verified BOOLEAN DEFAULT FALSE,
            paid BOOLEAN DEFAULT FALSE,
            analyses_used INTEGER DEFAULT 0,
            news TEXT,
            questions INTEGER DEFAULT 0
        )""")
    return db_pool

async def get_user(uid):
    pool = await get_db()
    row = await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    if not row:
        await pool.execute("INSERT INTO users (user_id) VALUES ($1)", uid)
        row = await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    return dict(row)

async def upd(uid, **kw):
    pool = await get_db()
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kw))
    await pool.execute(f"UPDATE users SET {sets} WHERE user_id=$1", uid, *kw.values())

def rkb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Статус"), KeyboardButton("Новый запрос")]],
        resize_keyboard=True
    )

def pkb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

async def call_genapi(sys_p, usr_p):
    msgs = [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://api.gen-api.ru/api/v1/networks/kimi-k2-5",
                headers={"Authorization": f"Bearer {GENAPI_KEY}", "Content-Type": "application/json"},
                json={"messages": msgs, "is_sync": False, "max_tokens": 2000}
            )
            if r.status_code != 200:
                return f"Ошибка API: {r.status_code}"
            rid = r.json().get("request_id")
            if not rid:
                return f"Ошибка: {r.json().get('message', 'нет request_id')}"
        for _ in range(40):
            await asyncio.sleep(4)
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(
                        f"https://api.gen-api.ru/api/v1/request/{rid}",
                        headers={"Authorization": f"Bearer {GENAPI_KEY}"}
                    )
                    if not r.text.strip():
                        continue
                    d = r.json()
                    if d.get("status") == "success":
                        ct = d.get("response", {})
                        if isinstance(ct, dict):
                            return ct.get("content") or ct.get("text") or str(ct)
                        return str(ct)
                    elif d.get("status") == "error":
                        return f"Ошибка: {d.get('message', '')}"
            except:
                continue
        return "Превышено время. Попробуй ещё раз."
    except Exception as e:
        return f"Ошибка: {e}"

SA = """Ты система анализа новостей. Отвечай на русском. Строго:
Вероятность реальности: [X%]
Верификация: [подтверждена/частично/не подтверждена] - фраза
Контекст: 2-3 предложения
Противоречия: по пунктам
3 модели развития:
- Модель 1 [название] (X%): цепочка - итог
- Модель 2 [название] (X%): цепочка - итог
- Модель 3 [название] (X%): цепочка - итог
Итог: рекомендация
Без вступлений."""
SF = "Система анализа новостей. Русский. Макс 4 предложения. Только факты. Без вступлений."
SE = "Система анализа новостей. Русский. Макс 3 предложения. В конце — мысль про информационную усталость. Без упоминания лимитов."

async def analyze(news, q=None, qc=0):
    if q is None:
        return await call_genapi(SA, f"Новость:\n{news}")
    return await call_genapi(SE if qc >= MAX_QUESTIONS else SF, f"Контекст:\n{news}\n\nВопрос:\n{q}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    if uid in WHITELIST_IDS:
        if not user["phone_verified"]:
            await upd(uid, phone_verified=True, phone="whitelist")
        await update.message.reply_text(
            f"News Intelligence\n\nБезлимитный доступ (тест)\nИспользовано анализов: {user['analyses_used']}",
            reply_markup=rkb()
        )
        return
    if not user["phone_verified"]:
        await update.message.reply_text(
            "Добро пожаловать в News Intelligence.\n\nДля доступа поделитесь номером — один номер = один аккаунт.",
            reply_markup=pkb()
        )
        return
    left = max(0, FREE_ANALYSES - user["analyses_used"])
    st = "Подписка активна" if user["paid"] else f"Бесплатных анализов: {left}/3"
    await update.message.reply_text(f"News Intelligence\n\n{st}", reply_markup=rkb())
    if not user["paid"]:
        await update.message.reply_text(
            "Оформить подписку:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Подписка 200 Stars", callback_data="subscribe")]])
        )

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ct = update.message.contact
    if ct.user_id != uid:
        await update.message.reply_text("Только свой номер.", reply_markup=pkb())
        return
    phone = ct.phone_number
    pool = await get_db()
    ex = await pool.fetchrow(
        "SELECT user_id FROM users WHERE phone=$1 AND user_id!=$2 AND phone_verified=TRUE", phone, uid
    )
    if ex:
        await update.message.reply_text("Этот номер уже привязан к другому аккаунту.")
        return
    await upd(uid, phone=phone, phone_verified=True)
    user = await get_user(uid)
    left = max(0, FREE_ANALYSES - user["analyses_used"])
    await update.message.reply_text(
        f"Номер подтверждён.\n\nДоступно анализов: {left}/3\n\nПришли любую новость.",
        reply_markup=rkb()
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "subscribe":
        await context.bot.send_invoice(
            chat_id=uid, title="News Intelligence",
            description="30 дней безлимитного анализа",
            payload="sub30", currency="XTR",
            prices=[{"label": "Подписка", "amount": 200}]
        )
    elif q.data == "cancel_sub":
        await upd(uid, paid=False)
        await q.message.reply_text("Подписка отменена.", reply_markup=rkb())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    msg = update.message
    if uid not in WHITELIST_IDS and not user["phone_verified"]:
        await msg.reply_text("Поделитесь номером телефона.", reply_markup=pkb())
        return
    fwd = bool(msg.forward_origin or msg.forward_from or msg.forward_from_chat)
    text = (msg.text or msg.caption or "").strip()
    if not text:
        await msg.reply_text("Пришли текст новости.", reply_markup=rkb())
        return
    if text == "Статус":
        wl = uid in WHITELIST_IDS
        st = "Подписка активна" if user["paid"] else ("Безлимит (тест)" if wl else f"Анализов: {max(0,FREE_ANALYSES-user['analyses_used'])}/3")
        has = f"есть ({user['questions']}/{MAX_QUESTIONS})" if user["news"] else "нет"
        btns = []
        if user["paid"]:
            btns.append([InlineKeyboardButton("Отменить подписку", callback_data="cancel_sub")])
        elif not wl:
            btns.append([InlineKeyboardButton("Подписка 200 Stars", callback_data="subscribe")])
        await msg.reply_text(
            f"Статус\n\n{st}\nНовость: {has}",
            reply_markup=InlineKeyboardMarkup(btns) if btns else rkb()
        )
        return
    if text == "Новый запрос":
        await upd(uid, news=None, questions=0)
        await msg.reply_text("Сброшено. Пришли новую новость.", reply_markup=rkb())
        return
    can = user["paid"] or uid in WHITELIST_IDS or user["analyses_used"] < FREE_ANALYSES
    new_news = (
        user["news"] is None or fwd or len(text) > 150 or
        not any(w in text.lower() for w in ["почему","как","что","кто","когда","зачем","а если","расскажи","объясни","?"])
    )
    if new_news:
        if not can:
            await msg.reply_text(
                "Разборы закончились.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Подписка 200 Stars", callback_data="subscribe")]])
            )
            return
        await upd(uid, news=text, questions=0)
        if not user["paid"] and uid not in WHITELIST_IDS:
            await upd(uid, analyses_used=user["analyses_used"] + 1)
        await msg.reply_chat_action("typing")
        sent = await msg.reply_text("Анализирую... (30-60 сек)")
        result = await analyze(text)
        user = await get_user(uid)
        footer = "" if (user["paid"] or uid in WHITELIST_IDS) else f"\n\nОсталось: {max(0,FREE_ANALYSES-user['analyses_used'])}/3"
        btns = [] if (user["paid"] or uid in WHITELIST_IDS) else [[InlineKeyboardButton("Подписка 200 Stars", callback_data="subscribe")]]
        await sent.edit_text(result + footer, reply_markup=InlineKeyboardMarkup(btns) if btns else None)
    else:
        if not user["news"]:
            await msg.reply_text("Пришли новость.", reply_markup=rkb())
            return
        qc = user["questions"]
        if qc >= MAX_QUESTIONS + 1:
            return
        await upd(uid, questions=qc + 1)
        await msg.reply_chat_action("typing")
        sent = await msg.reply_text("...")
        await sent.edit_text(await analyze(user["news"], text, qc))

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await upd(uid, paid=True)
    await update.message.reply_text("Подписка активна!", reply_markup=rkb())
    await update.message.reply_text(
        "Управление:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отменить подписку", callback_data="cancel_sub")]])
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler((filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
