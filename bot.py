# -*- coding: utf-8 -*-
"""
Мой день — Telegram-бот напоминаний.
Установка: pip install aiogram==3.* gtts speechrecognition soundfile
Также нужен ffmpeg (для распознавания голосовых): apt install ffmpeg / pkg install ffmpeg
Запуск: BOT_TOKEN=токен python bot.py
"""
import os, json, re, asyncio, logging, tempfile, subprocess
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
DATA_FILE = "myday_data.json"
SEND_VOICE = True   # озвучивать напоминания голосом (gTTS)
REPEAT_MIN = 30     # повтор напоминания, если не подтвердил
CHECK_SEC = 30      # как часто проверять дедлайны

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("myday")

# ================== ХРАНИЛИЩЕ ==================
DATA_LOCK = asyncio.Lock()
def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "links": {}}  # users: uid -> profile; links: code -> uid

def save_data(d):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

def new_profile(name):
    return {"name": name, "tasks": [], "snooze": 15, "notify": True,
            "helpers": [], "created": datetime.now().isoformat()}

# ================== ПАРСЕР «ЧТО И КОГДА» ==================
# корни слов — ловим все падежи: среда/среду/среде, пятница/пятницу...
WD_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен"]
NUMW = {"одиннадцать": 11, "двенадцать": 12, "один": 1, "одного": 1, "два": 2, "двух": 2,
        "три": 3, "трёх": 3, "трех": 3, "четыре": 4, "четырёх": 4, "четырех": 4,
        "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7,
        "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10}
DAYPARTS = {"утром": None, "утра": None, "днём": 12, "днем": 12, "дня": 12,
            "вечером": 18, "вечера": 18, "ночью": 0, "ночи": 0}

def parse_task(raw):
    """Возвращает (текст, дедлайн datetime, подпись дня). День не указан — сегодня,
    время не указано — через 30 минут."""
    text = raw.lower().strip()
    now = datetime.now()
    target = now.replace(second=0, microsecond=0)
    day_label = "сегодня"

    for i, stem in enumerate(WD_STEMS):
        mt = re.search(rf"(в[о]?\s*)?{stem}\w*", text)
        if mt:
            off = (i - now.weekday()) % 7
            if off == 0:
                off = 7
            target += timedelta(days=off)
            day_label = f"в {mt.group(0).split()[-1]}"
            text = text.replace(mt.group(0), "")
            break
    if day_label == "сегодня":
        if "послезавтра" in text:
            target += timedelta(days=2); day_label = "послезавтра"; text = text.replace("послезавтра", "")
        elif "завтра" in text:
            target += timedelta(days=1); day_label = "завтра"; text = text.replace("завтра", "")

    h = m = None
    hm = re.search(r"в\s+(\d{1,2})[:.](\d{2})", text)
    honly = re.search(r"\bв\s+(\d{1,2})\s*(часа?|часов)?\b", text)
    numw = next((w for w in NUMW if re.search(rf"\bв\s+{w}\b", text)), None)
    daypart = re.search(r"(утром|утра|днём|днем|дня|вечером|вечера|ночью|ночи)", text)
    if hm:
        h, m = int(hm.group(1)), int(hm.group(2)); text = text.replace(hm.group(0), "")
    elif honly:
        h, m = int(honly.group(1)), 0; text = text.replace(honly.group(0), "")
    elif numw:
        h, m = NUMW[numw], 0; text = re.sub(rf"\bв\s+{numw}\b", "", text)
    if h is not None and daypart:
        base = DAYPARTS.get(daypart.group(1))
        if base == 18 and h < 12: h += 12
        if base == 0 and h < 12: h += 12
        if base == 12 and h <= 6: h += 12
        text = text.replace(daypart.group(0), "")
    if h is None:
        mp = re.search(r"в\s+полдень|полдень", text)
        if mp:
            h, m = 12, 0; text = re.sub(r"в\s+полдень|полдень", "", text)
    if h is None:
        target = now + timedelta(minutes=30); h, m = target.hour, target.minute
    else:
        target = target.replace(hour=h, minute=m)
        if target < now:
            target += timedelta(days=1)
            if day_label == "сегодня": day_label = "завтра"
    text = re.sub(r"\s+", " ", text).strip(" .,;—-")
    if not text:
        text = "Напоминание"
    return text, target, day_label

# ================== ГОЛОС (STT + TTS) ==================
async def transcribe_voice(bot: Bot, file_id: str) -> str:
    """Голосовое -> текст (Google Speech Recognition, бесплатно, ru-RU)."""
    import soundfile as sf
    import speech_recognition as sr
    with tempfile.TemporaryDirectory() as td:
        ogg = os.path.join(td, "v.ogg")
        await (await bot.get_file(file_id)).download(destination=ogg)
        wav = os.path.join(td, "v.wav")
        subprocess.run(["ffmpeg", "-y", "-i", ogg, "-ar", "16000", "-ac", "1", wav],
                       capture_output=True)
        rec = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio = rec.record(src)
        return rec.recognize_google(audio, language="ru-RU")

async def voice_reply(text: str) -> bytes:
    from gtts import gTTS
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        tts = gTTS(text=text, lang="ru")
        tts.save(f.name)
        with open(f.name, "rb") as g:
            data = g.read()
        os.unlink(f.name)
    return data

# ================== КЛАВИАТУРЫ ==================
def kb_task(tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сделал", callback_data=f"done:{tid}"),
        InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snz:{tid}:15"),
        InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snz:{tid}:60"),
    ]])

def kb_task_list(ts, page=0):
    rows = []
    for t in ts[page*8:(page+1)*8]:
        mark = "✅" if t["done"] else ("⏰" if t.get("status") == "snoozed" else "•")
        rows.append([InlineKeyboardButton(text=f"{mark} {t['time']} {t['text'][:30]}",
                                          callback_data=f"view:{t['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ================== РУЧКИ ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def uid(m: Message) -> str:
    return str(m.from_user.id)

@dp.message(Command("start"))
async def cmd_start(m: Message):
    async with DATA_LOCK:
        d = load_data()
        u = d["users"].get(uid(m))
        if not u:
            name = m.from_user.first_name or "Пользователь"
            d["users"][uid(m)] = new_profile(name)
            save_data(d)
            u = d["users"][uid(m)]
    await m.answer(
        f"👋 {u['name']}, привет! Я — «Мой день».\n\n"
        "Просто напишите или продиктуйте голосом, например:\n"
        "«купить молоко завтра в 10»\n"
        "«позвонить Ване в шесть вечера»\n"
        "«выпить воды» (это будет через 30 минут)\n\n"
        "Команды: /tasks — мои задачи, /report — отчёт за день, "
        "/code — код для семьи, /settings — настройки.")

@dp.message(Command("tasks"))
async def cmd_tasks(m: Message):
    async with DATA_LOCK:
        d = load_data(); u = d["users"].get(uid(m))
    if not u or not u["tasks"]:
        await m.answer("Задач пока нет. Напишите или продиктуйте — и я напомню!")
        return
    ts = sorted(u["tasks"], key=lambda t: t["due"])
    lines = [f"{'✅' if t['done'] else '•'} {t['day_label']} в {t['time']} — {t['text']}" for t in ts[:15]]
    await m.answer("📋 Ваши задачи:\n" + "\n".join(lines))

@dp.message(Command("report"))
async def cmd_report(m: Message):
    async with DATA_LOCK:
        d = load_data(); u = d["users"].get(uid(m))
    if not u:
        await m.answer("Сначала /start")
        return
    done = [t for t in u["tasks"] if t["done"]]
    miss = [t for t in u["tasks"] if not t["done"] and t["due"].date() <= datetime.now().date()]
    lines = [f"📋 Отчёт за {datetime.now().strftime('%d.%m.%Y')} — {u['name']}:"]
    for t in sorted(u["tasks"], key=lambda x: x["due"]):
        lines.append(f"{'✅' if t['done'] else '⬜'} {t['day_label']} {t['time']} — {t['text']}"
                     + (f" ({t['confirmed_at']})" if t["done"] else ""))
    lines.append(f"Итог: {len(done)}/{len(u['tasks'])}")
    await m.answer("\n".join(lines))

@dp.message(Command("code"))
async def cmd_code(m: Message):
    import random
    async with DATA_LOCK:
        d = load_data()
        code = str(random.randint(1000, 9999))
        d["links"][code] = {"uid": uid(m), "exp": (datetime.now() + timedelta(minutes=10)).isoformat()}
        save_data(d)
    await m.answer(f"🔑 Код для семьи: <b>{code}</b>\nДействует 10 минут. "
                   f"Пусть родной человек напишет мне: /link {code}", parse_mode="HTML")

@dp.message(Command("link"))
async def cmd_link(m: Message):
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Напишите: /link 1234 (код от вашего близкого)")
        return
    async with DATA_LOCK:
        d = load_data()
        rec = d["links"].get(parts[1])
        if not rec or datetime.fromisoformat(rec["exp"]) < datetime.now():
            await m.answer("Код не найден или истёк. Попросите новый: /code")
            return
        pu = d["users"].setdefault(rec["uid"], new_profile("Близкий"))
        if uid(m) not in pu["helpers"]:
            pu["helpers"].append(uid(m))
        save_data(d)
    await m.answer(f"🤝 Вы подключены к «{pu['name']}». Теперь вы можете:\n"
                   "• писать мне задачи — я передам их (напишите просто текст);\n"
                   "• получать отчёт: /report;\n"
                   "• отключиться: /unlink")

@dp.message(Command("unlink"))
async def cmd_unlink(m: Message):
    async with DATA_LOCK:
        d = load_data(); n = 0
        for u in d["users"].values():
            if uid(m) in u.get("helpers", []):
                u["helpers"].remove(uid(m)); n += 1
        save_data(d)
    await m.answer("Отключено" if n else "Вы ни к кому не были подключены")

@dp.message(Command("settings"))
async def cmd_settings(m: Message):
    async with DATA_LOCK:
        d = load_data(); u = d["users"].get(uid(m))
    if not u:
        await m.answer("Сначала /start"); return
    await m.answer(f"⚙️ Интервал повтора напоминаний: {u.get('repeat', REPEAT_MIN)} мин.\n"
                   f"Изменить: /repeat 20\nГолосовые ответы: {'вкл' if SEND_VOICE else 'выкл'}")

@dp.message(Command("repeat"))
async def cmd_repeat(m: Message):
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await m.answer("Напишите: /repeat 20 (минут)"); return
    async with DATA_LOCK:
        d = load_data(); d["users"][uid(m)]["repeat"] = int(parts[1]); save_data(d)
    await m.answer(f"✔ Буду напоминать каждые {parts[1]} минут, пока не подтвердите.")

# ---------- добавление задачи: текст или голос ----------
async def add_task_for(target_uid: str, text: str, source: str):
    ttext, due, day_label = parse_task(text)
    async with DATA_LOCK:
        d = load_data()
        u = d["users"].setdefault(target_uid, new_profile("Пользователь"))
        tid = int(datetime.now().timestamp() * 1000) % 10**10
        u["tasks"].append({"id": tid, "text": ttext, "due": due.isoformat(),
                           "time": due.strftime("%H:%M"), "day_label": day_label,
                           "done": False, "status": "pending", "last_fire": "",
                           "confirmed_at": "", "from": source})
        save_data(d)
    return ttext, due, day_label, tid

@dp.message(F.voice)
async def on_voice(m: Message):
    wait = await m.answer("🎤 Слушаю…")
    try:
        text = await transcribe_voice(bot, m.voice.file_id)
    except Exception as e:
        await wait.edit_text("⚠️ Не разобрал голос. Напишите текстом или продиктуйте чётче.")
        log.warning(f"STT error: {e}")
        return
    await wait.delete()
    await process_user_text(m, text)

@dp.message(F.text)
async def on_text(m: Message):
    async with DATA_LOCK:
        d = load_data()
        is_helper = any(uid(m) in u.get("helpers", []) for u in d["users"].values())
        has_profile = uid(m) in d["users"]
    if not has_profile and not is_helper:
        await cmd_start(m)
        return
    if is_helper and not m.text.startswith("/"):
        # помощник пишет задачу своему подопечному
        target = next(u for u in load_data()["users"].values() if uid(m) in u.get("helpers", []))
        ttext, due, day_label, tid = await add_task_for(
            next(k for k, v in load_data()["users"].items() if v is target), m.text, "helper")
        await m.answer(f"✔ Передал «{target['name']}»: {ttext} ({day_label} в {due.strftime('%H:%M')})")
        await bot.send_message(next(k for k, v in load_data()["users"].items() if v is target),
                               f"📌 Новая задача от семьи: <b>{ttext}</b>\n{day_label} в {due.strftime('%H:%M')}",
                               parse_mode="HTML")
        return
    await process_user_text(m, m.text)

async def process_user_text(m: Message, text: str):
    ttext, due, day_label, tid = await add_task_for(uid(m), text, "self")
    msg = await m.answer(f"📌 <b>{ttext}</b>\n{day_label} в {due.strftime('%H:%M')}",
                         parse_mode="HTML", reply_markup=kb_task(tid))
    if SEND_VOICE:
        try:
            await bot.send_voice(m.chat.id, await voice_reply(f"Добавлено: {ttext}. {day_label} в {due.strftime('%H:%M')}."))
        except Exception as e:
            log.warning(f"TTS error: {e}")

# ---------- кнопки ----------
@dp.callback_query(F.data.startswith("done:"))
async def cb_done(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    async with DATA_LOCK:
        d = load_data(); u = d["users"][uid(c.message)]
        t = next((x for x in u["tasks"] if x["id"] == tid), None)
        if not t:
            await c.answer("Задача не найдена"); return
        t["done"] = True; t["status"] = "done"
        t["confirmed_at"] = datetime.now().strftime("%H:%M")
        save_data(d)
    await c.message.edit_text(f"✅ {t['text']} — выполнено в {t['confirmed_at']}")
    await c.answer("Отлично!")

@dp.callback_query(F.data.startswith("snz:"))
async def cb_snooze(c: CallbackQuery):
    _, tid_s, mins = c.data.split(":")
    tid, mins = int(tid_s), int(mins)
    async with DATA_LOCK:
        d = load_data(); u = d["users"][uid(c.message)]
        t = next((x for x in u["tasks"] if x["id"] == tid), None)
        if not t:
            await c.answer("Задача не найдена"); return
        t["status"] = "snoozed"
        t["snooze_until"] = (datetime.now() + timedelta(minutes=mins)).isoformat()
        save_data(d)
    await c.message.edit_text(f"⏰ «{t['text']}» — напомню через {mins} мин")
    await c.answer(f"Напомню через {mins} мин")

# ================== ПЛАНИРОВЩИК ==================
async def scheduler():
    while True:
        await asyncio.sleep(CHECK_SEC)
        try:
            async with DATA_LOCK:
                d = load_data(); changed = False
                now = datetime.now()
                for k, u in d["users"].items():
                    if not u.get("notify"):
                        continue
                    for t in u["tasks"]:
                        if t["done"]:
                            continue
                        if t.get("status") == "snoozed":
                            until = datetime.fromisoformat(t["snooze_until"])
                            if now >= until:
                                t["status"] = "pending"; t["last_fire"] = ""; changed = True
                            else:
                                continue
                        due = datetime.fromisoformat(t["due"])
                        if due.date() != now.date():
                            continue  # сегодняшние — сегодня
                        last = datetime.fromisoformat(t["last_fire"]) if t["last_fire"] else None
                        if now >= due and (last is None or (now - last).total_seconds() >= u.get("repeat", REPEAT_MIN) * 60):
                            t["status"] = "overdue"; t["last_fire"] = now.isoformat(); changed = True
                            try:
                                await bot.send_message(int(k),
                                    f"🔔 <b>{t['text']}</b>\nПора! Повторяю каждые {u.get('repeat', REPEAT_MIN)} мин, пока не подтвердите.",
                                    parse_mode="HTML", reply_markup=kb_task(t["id"]))
                                if SEND_VOICE:
                                    await bot.send_voice(int(k), await voice_reply(f"Напоминаю: {t['text']}"))
                            except Exception as e:
                                log.warning(f"send reminder: {e}")
                    # ежедневный отчёт помощникам в 21:00
                    if now.strftime("%H:%M") == "21:00" and u["tasks"]:
                        done = sum(1 for t in u["tasks"] if t["done"])
                        rep = (f"📋 {u['name']}: {done}/{len(u['tasks'])} за сегодня. "
                               f"Не выполнено: {len(u['tasks']) - done}. Подробности: /report")
                        for h in u.get("helpers", []):
                            try:
                                await bot.send_message(int(h), rep)
                            except Exception:
                                pass
                if changed:
                    save_data(d)
        except Exception as e:
            log.error(f"scheduler: {e}")

# ================== ЗАПУСК ==================
# веб-эндпоинт для хостингов (Render и т.п.): показывает, что бот жив,
# и даёт URL, который пингует UptimeRobot, чтобы сервис не засыпал
from aiohttp import web
async def health(request):
    return web.Response(text="myday bot ok")

async def run_health():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await run_health()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
