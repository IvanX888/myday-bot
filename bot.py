# -*- coding: utf-8 -*-
"""
Мой день — Telegram-бот напоминаний (версия без aiogram — работает на Termux без компиляции).
Установка: pip install -r requirements.txt   (нужен ffmpeg для голосовых)
Запуск: BOT_TOKEN=токен python bot.py
"""
import os, json, re, time, logging, tempfile, subprocess, threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "myday_data.json")
SEND_VOICE = True
CHECK_SEC = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("myday")

# ================== ХРАНИЛИЩЕ ==================
STORE_LOCK = threading.Lock()
def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "links": {}}

def save_data(d):
    with STORE_LOCK:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)

def new_profile(name):
    return {"name": name, "username": None, "active": None, "tasks": [],
            "snooze": 15, "repeat": 30, "shared": False,
            "helpers": [], "created": datetime.now().isoformat()}

# ---- активный задачник ----
def active_owner(d, uid):
    u = d["users"].get(uid, {})
    a = u.get("active")
    if a and a in d["users"] and (a == uid or uid in d["users"][a].get("helpers", [])):
        return a
    return uid

def accessible(d, uid):
    acc = [(uid, d["users"].get(uid, new_profile("Я"))["name"])]
    for k, v in d["users"].items():
        if uid in v.get("helpers", []):
            acc.append((k, v["name"]))
    return acc

# ================== TELEGRAM API (через requests) ==================
def tg(method, **params):
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/{method}", data=params, timeout=30)
            return r.json()
        except Exception as e:
            log.warning(f"{method} попытка {attempt+1}: {e}")
            time.sleep(3)
    return {}

def tg_file(method, file_field, file_bytes, filename, **params):
    try:
        r = requests.post(f"{API}/{method}",
                          data=params, files={file_field: (filename, file_bytes)},
                          timeout=60)
        return r.json()
    except Exception as e:
        log.warning(f"{method}: {e}")
        return {}

def send(chat, text, kb=None, rkb=None):
    params = {"chat_id": chat, "text": text}
    markup = kb if kb else rkb
    if markup:
        params["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    r = tg("sendMessage", **params)
    if r and not r.get("ok"):
        log.warning(f"sendMessage fail: {r.get('description')} | {text[:60]!r}")
    return r

# главное меню-подсказка под полем ввода (как в боте-юристе)
def main_kb():
    return {"keyboard": [
        [{"text": "📋 Мои задачи"}, {"text": "📊 Отчёт"}],
        [{"text": "📒 Задачники"}, {"text": "👨‍👧 Код для семьи"}],
        [{"text": "❓ Помощь"}],
    ], "resize_keyboard": True}

# кнопки-ярлыки -> команды
BUTTON_CMDS = {
    "📋 Мои задачи": "/tasks",
    "📊 Отчёт": "/report",
    "👨‍👧 Код для семьи": "/code",
    "📒 Задачники": "/notebooks",
    "❓ Помощь": "/help",
}

# голосовые/текстовые синонимы команд
CMD_SYNONYMS = {
    "/tasks": ["мои задачи", "покажи задачи", "покажи все задачи", "просмотреть задачи",
               "посмотреть задачи", "что у меня", "список задач", "все задачи",
               "что сегодня", "план на сегодня", "что надо сделать", "мой список"],
    "/report": ["отчёт", "отчет", "итоги", "что сделано", "покажи отчет", "покажи отчёт", "статистика"],
    "/help": ["помощь", "команды", "что умеешь", "помоги", "справка"],
    "/code": ["код для семьи", "дай код", "код семье", "подключить семью", "код семьи"],
    "/ideas": ["мои идеи", "идеи", "заметки", "мои заметки", "что записал"],
    "/shop": ["что купить", "покупки", "список покупок", "что в магазин", "магазин"],
    "/notebooks": ["мои задачники", "какие задачники", "список задачников", "какие списки"],
}

def match_cmd(text):
    """Ищет команду по синониму. Работает и для голоса после STT."""
    t = re.sub(r"[.!,?]", "", text.lower().strip())
    for cmd, phrases in CMD_SYNONYMS.items():
        for ph in phrases:
            if t == ph or t.startswith(ph + " ") or (len(ph) > 6 and ph in t):
                return cmd
    return None

# ============ 2) КАТЕГОРИИ ЗАДАЧ ============
CATS = [
    ("🛒", "покупка", ["купить", "заказать", "приобрести", "закупить"]),
    ("📞", "звонок", ["позвонить", "набрать", "перезвонить"]),
    ("💧", "здоровье", ["воду", "воды", "попить"]),
    ("💊", "здоровье", ["лекарств", "таблетк", "пилюл"]),
    ("💡", "идея", ["идея", "придумал", "записать"]),
]

def detect_cat(text):
    for icon, cat, words in CATS:
        if any(w in text for w in words):
            return icon, cat
    return "🗒️", "дело"

def send_voice(chat, text):
    from gtts import gTTS
    with tempfile.TemporaryDirectory() as td:
        mp3 = os.path.join(td, "v.mp3"); ogg = os.path.join(td, "v.ogg")
        gTTS(text=text, lang="ru").save(mp3)
        subprocess.run(["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "32k", ogg],
                       capture_output=True)
        if os.path.exists(ogg):
            with open(ogg, "rb") as f:
                tg_file("sendVoice", "voice", f.read(), "v.ogg", chat_id=chat)

def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}

def kb_task(tid):
    return kb([[("✅ Сделал", f"done:{tid}"), ("⏰ +15 мин", f"snz:{tid}:15"), ("⏰ +1 час", f"snz:{tid}:60")]])

# ================== ПАРСЕР «ЧТО И КОГДА» ==================
WD_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен"]
NUMW = {"одиннадцать": 11, "двенадцать": 12, "один": 1, "одного": 1, "два": 2, "двух": 2,
        "три": 3, "трёх": 3, "трех": 3, "четыре": 4, "четырёх": 4, "четырех": 4,
        "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7,
        "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10}
DAYPARTS = {"утром": None, "утра": None, "днём": 12, "днем": 12, "дня": 12,
            "вечером": 18, "вечера": 18, "ночью": 0, "ночи": 0}

def parse_task(raw):
    """День не указан — сегодня, время не указано — через 30 минут."""
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

# ================== ГОЛОС -> ТЕКСТ ==================
def transcribe_voice(file_id: str) -> str:
    import speech_recognition as sr
    fr = tg("getFile", file_id=file_id)
    path = fr["result"]["file_path"]
    with tempfile.TemporaryDirectory() as td:
        ogg = os.path.join(td, "v.ogg"); wav = os.path.join(td, "v.wav")
        r2 = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=60)
        with open(ogg, "wb") as f:
            f.write(r2.content)
        subprocess.run(["ffmpeg", "-y", "-i", ogg, "-ar", "16000", "-ac", "1", wav],
                       capture_output=True)
        rec = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio = rec.record(src)
        return rec.recognize_google(audio, language="ru-RU")

# ================== ЛОГИКА ЗАДАЧ ==================
def add_task(d, user_id, text, source):
    icon, cat = detect_cat(text)
    ttext, due, day_label = parse_task(text)
    if cat == "идея":
        due, day_label = None, "заметка"   # идеи не напоминаем — просто храним
    u = d["users"].setdefault(user_id, new_profile("Пользователь"))
    tid = int(time.time() * 1000) % 10**10
    u["tasks"].append({"id": tid, "text": ttext, "due": due.isoformat() if due else None,
                       "time": due.strftime("%H:%M") if due else "", "day_label": day_label,
                       "cat": cat, "icon": icon,
                       "done": False, "status": "pending", "last_fire": "",
                       "confirmed_at": "", "from": source})
    return ttext, due, day_label, tid

def repeat_minutes(u):
    try:
        return int(u.get("repeat", 30))
    except Exception:
        return 30

def handle_command(d, uid, chat, text, username=None):
    u = d["users"].get(uid)
    if text == "/start":
        if not u:
            d["users"][uid] = new_profile("Пользователь")
        if username:
            d["users"][uid]["username"] = username
        save_data(d)
        u = d["users"][uid]
        send(chat, "👋 Привет! Я — «Мой день».\n\nПросто напишите или продиктуйте голосом:\n"
                   "«купить молоко завтра в 10»\n«позвонить Ване в шесть вечера»\n«выпить воды» — через 30 минут\n\n"
                   "Кнопки внизу 👇 помогут быстро управлять задачами.", rkb=main_kb())
        return True
    if text == "/help":
        send(chat, "❓ Помощь\n\n"
                   "• Любое сообщение — это новая задача\n"
                   "• «купить...» — в список покупок, «идея...» — в заметки\n"
                   "• Голосом: «мои задачи», «что сегодня», «что купить», «мои идеи»\n"
                   "• /share_on — общий задачник для семьи\n"
                   "• Несколько задачников: «задачник дяди», «мой задачник», /notebooks\n"
                   "• Передать сообщение: «скажи Иван купить молоко»\n"
                   "• Если 3 раза не подтверждено — бот оповещает семью\n"
                   "• «завтра», «в среду», «в 6 вечера» — понимаю дату и время\n"
                   "• Время не назвали — напомню через 30 минут\n"
                   "• К задачам прикреплены кнопки: ✅ Сделал, ⏰ Отложить\n"
                   "• /repeat 20 — как часто повторять напоминания (минуты)\n"
                   "• /code — дать семье доступ к вашим задачам\n"
                   "• /report — отчёт за день\n\n"
                   "Семья получает ваш отчёт каждый день в 21:00.")
        return True
    if not u:
        send(chat, "Сначала /start")
        return True
    if text in ("/tasks", "/ideas", "/shop"):
        u = d["users"][active_owner(d, uid)]
        ts = u["tasks"]
        if text == "/shop":
            ts = [t for t in ts if t.get("cat") == "покупка"]
        notes = [t for t in ts if not t.get("due")]
        timed = sorted([t for t in ts if t.get("due")], key=lambda t: t["due"])
        if text == "/ideas":
            timed = []
        if not timed and not notes:
            send(chat, "Здесь пока пусто. Напишите или продиктуйте — и я запомню!")
            return True
        lines = []
        if timed:
            lines.append("📋 Задачи:" if text != "/shop" else "🛒 Покупки:")
            for t in timed[:15]:
                lines.append(f"{'✅' if t['done'] else '•'} {t['day_label']} в {t['time']} — {t.get('icon','🗒️')} {t['text']}")
        if notes:
            lines.append("")
            lines.append("💡 Идеи и заметки:")
            for t in notes[:15]:
                lines.append(f"{'✅' if t['done'] else '•'} {t.get('icon','💡')} {t['text']}")
        send(chat, "\n".join(lines), rkb=main_kb())
        return True
    if text == "/report":
        u = d["users"][active_owner(d, uid)]
        done = [t for t in u["tasks"] if t["done"]]
        lines = [f"📋 Отчёт за {datetime.now().strftime('%d.%m.%Y')} — {u['name']}:"]
        for t in sorted(u["tasks"], key=lambda x: x["due"]):
            lines.append(f"{'✅' if t['done'] else '⬜'} {t['day_label']} {t['time']} — {t['text']}"
                         + (f" ({t['confirmed_at']})" if t["done"] else ""))
        lines.append(f"Итог: {len(done)}/{len(u['tasks'])}")
        send(chat, "\n".join(lines))
        return True
    if text == "/code":
        import random
        code = str(random.randint(1000, 9999))
        d["links"][code] = {"uid": uid, "exp": (datetime.now() + timedelta(minutes=10)).isoformat()}
        save_data(d)
        send(chat, f"🔑 Код для семьи: {code}\nДействует 10 минут. Родной человек пишет мне: /link {code}")
        return True
    if text.startswith("/link"):
        parts = text.split()
        if len(parts) < 2:
            send(chat, "Напишите: /link 1234")
            return True
        rec = d["links"].get(parts[1])
        if not rec or datetime.fromisoformat(rec["exp"]) < datetime.now():
            send(chat, "Код не найден или истёк. Попросите новый: /code")
            return True
        pu = d["users"].setdefault(rec["uid"], new_profile("Близкий"))
        if uid not in pu["helpers"]:
            pu["helpers"].append(uid)
        save_data(d)
        send(chat, f"🤝 Вы подключены к «{pu['name']}». Пишите мне задачи текстом — я передам. /report — отчёт. /unlink — отключиться.")
        return True
    if text == "/unlink":
        n = 0
        for uu in d["users"].values():
            if uid in uu.get("helpers", []):
                uu["helpers"].remove(uid); n += 1
        save_data(d)
        send(chat, "Отключено" if n else "Вы ни к кому не подключены")
        return True
    if text == "/notebooks":
        acc = accessible(d, uid)
        cur = active_owner(d, uid)
        lines = ["📒 Ваши задачники:"]
        for i, (ou, nm) in enumerate(acc, 1):
            lines.append(f"{'▶️' if ou == cur else '•'} {i}. {nm}{' (общий)' if ou != uid else ''}")
        lines.append("")
        lines.append("Переключиться: «задачник дяди» или /use 2")
        send(chat, "\n".join(lines), rkb=main_kb())
        return True
    if text.startswith("/use"):
        parts = text.split()
        acc = accessible(d, uid)
        try:
            ou, nm = acc[int(parts[1]) - 1]
        except Exception:
            send(chat, "Нет такого номера. Посмотрите: /notebooks")
            return True
        d["users"][uid]["active"] = ou if ou != uid else None
        save_data(d)
        send(chat, f"📒 Переключился на задачник «{nm}»", rkb=main_kb())
        return True
    if text == "/share_on":
        if not u.get("helpers"):
            send(chat, "Пока некому открывать: пусть семья подключится по коду (/code).")
            return True
        u["shared"] = True; save_data(d)
        send(chat, "🤝 Общий задачник ВКЛЮЧЁН.\nСемья видит ваши задачи, может добавлять и нажимать «Сделал». Отключить: /share_off")
        for h in u["helpers"]:
            send(int(h), f"🤝 {u['name']} открыл(а) вам общий задачник. Пишите задачи — они попадут к {u['name']}, «мои задачи» — увидеть список.")
        return True
    if text == "/share_off":
        u["shared"] = False; save_data(d)
        send(chat, "🔒 Общий задачник выключен. Семья снова только добавляет задачи.")
        return True
    if text.startswith("/repeat"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat, "Напишите: /repeat 20")
            return True
        d["users"][active_owner(d, uid)]["repeat"] = int(parts[1]); save_data(d)
        send(chat, f"✔ Буду напоминать каждые {parts[1]} минут, пока не подтвердите.")
        return True
    return False

# ================== ОБРАБОТКА ОБНОВЛЕНИЙ ==================
def render_tasks_for(u, title="📋 Задачи"):
    notes = [t for t in u["tasks"] if not t.get("due")]
    timed = sorted([t for t in u["tasks"] if t.get("due")], key=lambda t: t["due"])
    lines = [f"{title} — {u['name']}:"]
    for t in timed[:15]:
        lines.append(f"{'✅' if t['done'] else '•'} {t['day_label']} в {t['time']} — {t.get('icon','🗒️')} {t['text']}")
    if notes:
        lines.append("")
        lines.append("💡 Идеи и заметки:")
        for t in notes[:15]:
            lines.append(f"{'✅' if t['done'] else '•'} {t.get('icon','💡')} {t['text']}")
    return "\n".join(lines)

def process_text(d, uid, chat, text, username=None):
    if text in BUTTON_CMDS:
        text = BUTTON_CMDS[text]
    low = text.lower().strip()
    # --- передать сообщение от моего имени ---
    if low.startswith(("скажи ", "передай ", "напиши ")):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3:
            name = parts[1].lstrip("@").lower()
            target = next((k for k, v in d["users"].items()
                           if name in v.get("name", "").lower()
                           or name == str(v.get("username") or "").lower()), None)
            if target:
                sender = d["users"].get(uid, {}).get("name", "Кто-то")
                send(int(target), f"💬 {sender} просит передать:\n{parts[2]}")
                send(chat, f"✔ Отправил «{d['users'][target]['name']}»")
            else:
                send(chat, "Такой человек боту ещё не писал. Пусть откроет бота и нажмёт /start.")
        else:
            send(chat, "Формат: скажи Иван купить молоко")
        return
    # --- переключение задачника ---
    if low.startswith("задачник "):
        name = low[9:].strip()
        acc = accessible(d, uid)
        target = uid if name in ("мой", "моё") else None
        if target is None:
            for ou, nm in acc:
                if name in nm.lower():
                    target = ou
                    break
        if target is not None:
            d["users"].setdefault(uid, new_profile("Я"))["active"] = target if target != uid else None
            save_data(d)
            send(chat, f"📒 Теперь работаем с задачником «{d['users'][target]['name']}»", rkb=main_kb())
        else:
            send(chat, "Такой задачник не найден. Смотрите: /notebooks")
        return
    helper_of = next((k for k, v in d["users"].items() if uid in v.get("helpers", [])), None)
    if helper_of:
        cmd = match_cmd(text)
        owner = d["users"][helper_of]
        if cmd in ("/tasks", "/ideas", "/shop"):
            send(chat, render_tasks_for(owner), rkb=main_kb())
            return
        if cmd == "/report":
            done = sum(1 for t in owner["tasks"] if t["done"])
            send(chat, f"📋 {owner['name']}: {done}/{len(owner['tasks'])} выполнено. Не выполнено: {len(owner['tasks'])-done}.")
            return
        if not text.startswith("/"):
            ttext, due, dl, tid = add_task(d, helper_of, text, "helper")
            save_data(d)
            when = f"{dl} в {due.strftime('%H:%M')}" if due else dl
            send(chat, f"✔ Передал: {ttext} ({when})")
            send(helper_of, f"📌 Новая задача от семьи: {ttext}\n{when}", kb=kb_task(tid))
            if SEND_VOICE:
                try:
                    send_voice(helper_of, f"Новая задача от семьи: {ttext}")
                except Exception:
                    pass
            return
    cmd = match_cmd(text)
    if cmd and not text.startswith("/"):
        if handle_command(d, uid, chat, cmd, username):
            return
    if text.startswith("/"):
        if handle_command(d, uid, chat, text.split("@")[0], username):
            return
    # обычная задача -> в активный задачник
    ttext, due, dl, tid = add_task(d, active_owner(d, uid), text, "self")
    save_data(d)
    when = f"{dl} в {due.strftime('%H:%M')}" if due else dl
    send(chat, f"📌 {ttext}\n{when}", kb=kb_task(tid))
    if SEND_VOICE:
        try:
            send_voice(chat, f"Добавлено: {ttext}. {when}.")
        except Exception as e:
            log.warning(f"TTS: {e}")

def handle_message(d, msg):
    uid = str(msg["from"]["id"])
    chat = msg["chat"]["id"]
    username = msg.get("from", {}).get("username")
    text = msg.get("text", "")
    if text:
        process_text(d, uid, chat, text, username)
        return
    if "voice" in msg:
        try:
            text = transcribe_voice(msg["voice"]["file_id"])
        except Exception as e:
            log.warning(f"STT: {e}")
            if "aifc" in str(e) or "audioop" in str(e):
                send(chat, "⚠️ Голос пока не работает: в Termux выполни pip install -U SpeechRecognition и перезапусти бота.")
            else:
                send(chat, "⚠️ Не разобрал голос. Напишите текстом или продиктуйте чётче.")
            return
        send(chat, f"🎤 Распознал: «{text}»")
        process_text(d, uid, chat, text, username)

def find_task(d, uid, tid):
    """Задача у самого юзера, а если он помощник — у хозяина (общий задачник)."""
    u = d["users"].get(uid)
    if u:
        t = next((x for x in u["tasks"] if x["id"] == tid), None)
        if t:
            return u, t
    for uu in d["users"].values():
        if uid in uu.get("helpers", []):
            t = next((x for x in uu["tasks"] if x["id"] == tid), None)
            if t:
                return uu, t
    return None, None

def handle_callback(d, cb):
    uid = str(cb["from"]["id"])
    data = cb["data"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    parts = data.split(":")
    if parts[0] == "done":
        tid = int(parts[1])
        u, t = find_task(d, uid, tid)
        if not t:
            return
        if t:
            t["done"] = True; t["status"] = "done"
            t["fires"] = 0
            t["confirmed_at"] = datetime.now().strftime("%H:%M")
            save_data(d)
            tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
               message_id=cb["message"]["message_id"],
               text=f"✅ {t['text']} — выполнено в {t['confirmed_at']}")
    elif parts[0] == "snz":
        tid, mins = int(parts[1]), int(parts[2])
        u, t = find_task(d, uid, tid)
        if not t:
            return
        if t:
            t["status"] = "snoozed"
            t["snooze_until"] = (datetime.now() + timedelta(minutes=mins)).isoformat()
            save_data(d)
            tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
               message_id=cb["message"]["message_id"],
               text=f"⏰ «{t['text']}» — напомню через {mins} мин")

# ================== ПЛАНИРОВЩИК ==================
def scheduler_tick(d):
    now = datetime.now()
    changed = False
    for k, u in d["users"].items():
        if not u.get("notify", True):
            continue
        for t in u["tasks"]:
            if t["done"]:
                continue
            if t.get("status") == "snoozed":
                if now >= datetime.fromisoformat(t["snooze_until"]):
                    t["status"] = "pending"; t["last_fire"] = ""; changed = True
                else:
                    continue
            if not t.get("due"):
                continue  # идеи/заметки не напоминаем
            due = datetime.fromisoformat(t["due"])
            if due.date() != now.date():
                continue
            last = datetime.fromisoformat(t["last_fire"]) if t["last_fire"] else None
            if now >= due and (last is None or (now - last).total_seconds() >= repeat_minutes(u) * 60):
                t["status"] = "overdue"; t["last_fire"] = now.isoformat(); changed = True
                t["fires"] = t.get("fires", 0) + 1
                send(int(k), f"🔔 {t['text']}\nПора! Повторяю каждые {repeat_minutes(u)} мин, пока не подтвердите.",
                     kb=kb_task(t["id"]))
                if t["fires"] == 3 and u.get("helpers"):
                    for h in u["helpers"]:
                        send(int(h), f"⚠️ ВНИМАНИЕ: «{t['text']}» ({u['name']}) не подтверждается уже 3 раза. Может, позвоните?")
                if SEND_VOICE:
                    try:
                        send_voice(int(k), f"Напоминаю: {t['text']}")
                    except Exception as e:
                        log.warning(f"TTS: {e}")
        # отчёт помощникам в 21:00
        if now.strftime("%H:%M") == "21:00" and u["tasks"] and not u.get("_rep_" + now.strftime("%d%m")):
            done = sum(1 for t in u["tasks"] if t["done"])
            for h in u.get("helpers", []):
                send(int(h), f"📋 {u['name']}: {done}/{len(u['tasks'])} за сегодня. Не выполнено: {len(u['tasks'])-done}. Подробности: /report")
            u["_rep_" + now.strftime("%d%m")] = True; changed = True
    if changed:
        save_data(d)

# ================== HEALTH (для хостингов с пингом) ==================
def run_health():
    port = int(os.environ.get("PORT", 0))
    if not port:
        return
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b"myday bot ok")
        def log_message(self, *a):
            pass
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", port), H).serve_forever(),
                     daemon=True).start()

# ================== ГЛАВНЫЙ ЦИКЛ ==================
def main():
    if BOT_TOKEN.startswith("ВСТАВЬ"):
        raise SystemExit("Укажите BOT_TOKEN: export BOT_TOKEN=...")
    run_health()
    d = load_data()
    offset = 0
    last_check = 0
    last_save_day = datetime.now().date()
    log.info("Бот запущен")
    while True:
        try:
            if time.time() - last_check >= CHECK_SEC:
                last_check = time.time()
                scheduler_tick(d)
            # раз в сутки чистим старые выполненные задачи
            if datetime.now().date() != last_save_day:
                last_save_day = datetime.now().date()
                for u in d["users"].values():
                    u["tasks"] = [t for t in u["tasks"] if not t["done"] or (t.get("due") and t["due"][:10] >= str(last_save_day))]
                    for k in list(u.keys()):
                        if k.startswith("_rep_"): del u[k]
                save_data(d)
            res = tg("getUpdates", offset=offset, timeout=25)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    handle_message(d, upd["message"])
                elif "callback_query" in upd:
                    handle_callback(d, upd["callback_query"])
            time.sleep(1)
        except Exception as e:
            log.error(f"цикл: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
