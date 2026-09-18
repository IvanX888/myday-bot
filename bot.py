# -*- coding: utf-8 -*-
"""
Мой день — бот-напоминалка и семейный задачник (Termux-friendly, без компиляции).
Зависимости: pip install -r requirements.txt; системные: ffmpeg, flac (для голоса).
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
            "repeat": 30, "shared": False,
            "helpers": [], "created": datetime.now().isoformat()}

def active_owner(d, uid):
    """Чей задачник активен. 'self' = свой. None = по умолчанию
    (обычный юзер — свой, помощник — хозяина)."""
    u = d["users"].get(uid, {})
    a = u.get("active")
    if a == "self":
        return uid
    if a and a in d["users"]:
        return a
    h = next((k for k, v in d["users"].items() if uid in v.get("helpers", [])), None)
    return h or uid

def own_mode(d, uid):
    return d["users"].get(uid, {}).get("active") == "self"

def accessible(d, uid):
    acc = [(uid, d["users"].get(uid, new_profile("Я"))["name"])]
    for k, v in d["users"].items():
        if uid in v.get("helpers", []):
            acc.append((k, v["name"]))
    return acc

def name_match(name, real_name):
    """Нечёткое сопоставление: 'дяди' найдёт 'Дядя Валера'."""
    name = name.lower().strip()
    nml = real_name.lower()
    if not name:
        return False
    if name in nml or nml in name:
        return True
    w = name.split()[0]
    for x in nml.split():
        if len(x) >= 3 and len(w) >= 3 and (x.startswith(w[:4]) or w.startswith(x[:4])):
            return True
    return False

# ================== TELEGRAM API ==================
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
        r = requests.post(f"{API}/{method}", data=params,
                          files={file_field: (filename, file_bytes)}, timeout=60)
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

# ---- клавиатуры ----
def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": cd} for t, cd in row] for row in rows]}

def kb_task(tid):
    return kb([
        [("✅ Сделал", f"done:{tid}"), ("⏰ +15 мин", f"snz:{tid}:15"), ("⏰ +1 час", f"snz:{tid}:60")],
        [("🗑 Удалить", f"del:{tid}")],
    ])

def kb_confirm(mode):
    return kb([[("✔ Да, очистить", f"clr:{mode}:yes"), ("✖ Отмена", f"clr:{mode}:no")]])

def main_kb():
    return {"keyboard": [
        [{"text": "📋 Мои задачи"}, {"text": "🛒 Покупки"}],
        [{"text": "💡 Мои идеи"}, {"text": "📊 Отчёт"}],
        [{"text": "📒 Задачники"}, {"text": "❓ Помощь"}],
    ], "resize_keyboard": True}

BUTTON_CMDS = {
    "📋 Мои задачи": "/tasks", "🛒 Покупки": "/shop",
    "💡 Мои идеи": "/ideas", "📊 Отчёт": "/report",
    "📒 Задачники": "/notebooks", "❓ Помощь": "/help",
}

# ================== КОМАНДЫ-СИНОНИМЫ (текст и голос) ==================
CMD_SYNONYMS = {
    "/start": ["старт", "start", "начать", "начнем"],
    "/tasks": ["мои задачи", "покажи задачи", "покажи все задачи", "просмотреть задачи",
               "посмотреть задачи", "что у меня", "список задач", "все задачи",
               "что сегодня", "план на сегодня", "что надо сделать", "мой список"],
    "/report": ["отчёт", "отчет", "итоги", "что сделано", "покажи отчет", "покажи отчёт", "статистика"],
    "/help": ["помощь", "команды", "что умеешь", "помоги", "справка"],
    "/code": ["код для семьи", "дай код", "код семье", "подключить семью", "код семьи"],
    "/ideas": ["мои идеи", "идеи", "заметки", "мои заметки", "что записал",
               "мои записи", "важные записи", "мои номера", "что запомнил", "мои контакты"],
    "/shop": ["что купить", "покупки", "список покупок", "что в магазин", "магазин", "список продуктов"],
    "/notebooks": ["мои задачники", "какие задачники", "список задачников", "какие списки"],
    "/clear_done": ["убрать выполненные", "очистить задачи", "убрать сделанное", "очистить список"],
    "/clear_all": ["удалить все задачи", "удалить всё", "удалить все", "очистить всё",
                   "обнулить задачник", "стереть всё", "очистить всё"],
    "/clear_shop": ["убрать купленное", "очистить покупки", "удалить покупки"],
    "/clear_ideas": ["очистить идеи", "удалить заметки", "удалить идеи", "очистить заметки"],
    "/shop_done": ["куплено всё", "всё купил", "всё куплено", "купил всё"],
}

def match_cmd(text):
    t = re.sub(r"[.!,?]", "", text.lower().strip())
    for cmd, phrases in CMD_SYNONYMS.items():
        for ph in phrases:
            if t == ph or t.startswith(ph + " ") or (len(ph) > 6 and ph in t):
                return cmd
    return None

# ================== КАТЕГОРИИ ==================
CATS = [
    ("🛒", "покупка", ["купить", "заказать", "приобрести", "закупить", "продукты"]),
    ("📞", "звонок", ["позвонить", "набрать", "перезвонить"]),
    ("💧", "здоровье", ["воду", "воды", "попить"]),
    ("💊", "здоровье", ["лекарств", "таблетк", "пилюл"]),
    ("💡", "идея", ["идея", "придумал", "записать", "запомни", "заметка", "запись",
                    "номер", "телефон", "важно", "важное", "важная", "реквизит",
                    "адрес", "контакт", "пин", "пароль", "код от", "секрет", "сохрани"]),
]

def detect_cat(text):
    for icon, cat, words in CATS:
        if any(w in text for w in words):
            return icon, cat
    return "🗒️", "дело"

# ================== ПАРСЕР ВРЕМЕНИ ==================
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

# ================== ЗАДАЧИ ==================
VERBS = ("купить", "заказать", "приобрести", "закупить")

def make_task(u, text, due, day_label, icon, cat, source):
    tid = int(time.time() * 1000) % 10**10 + len(u["tasks"])
    u["tasks"].append({"id": tid, "text": text, "due": due.isoformat() if due else None,
                       "time": due.strftime("%H:%M") if due else "", "day_label": day_label,
                       "cat": cat, "icon": icon,
                       "done": False, "status": "pending", "last_fire": "",
                       "confirmed_at": "", "from": source})
    return tid

def add_tasks(d, owner_uid, text, source):
    """Добавляет задачи. Покупки с запятыми -> отдельные пункты списка."""
    icon, cat = detect_cat(text)
    u = d["users"].setdefault(owner_uid, new_profile("Пользователь"))
    created = []
    if cat == "покупка":
        parts = [p.strip() for p in re.split(r",| и ", text) if p.strip()]
        items = []
        for p in parts:
            for v in VERBS:
                if p.startswith(v + " "):
                    p = p[len(v):].strip()
            if len(p) > 1:
                items.append(p)
        if len(items) >= 2:
            for it in items[:10]:
                ttext, due, dl = parse_task(it)
                tid = make_task(u, it, due, dl, "🛒", "покупка", source)
                created.append((f"🛒 {it}", due, dl, tid))
            return created
    ttext, due, dl = parse_task(text)
    if cat == "идея":
        due, dl = None, "заметка"
    tid = make_task(u, ttext, due, dl, icon, cat, source)
    return [(f"{icon} {ttext}", due, dl, tid)]

def repeat_minutes(u):
    try:
        return int(u.get("repeat", 30))
    except Exception:
        return 30

def find_task(d, uid, tid):
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

def render_lists(u, title="📋 Задачи"):
    notes = [t for t in u["tasks"] if not t.get("due")]
    timed = sorted([t for t in u["tasks"] if t.get("due")], key=lambda t: t["due"])
    lines = [f"{title} — {u['name']}:"]
    for t in timed[:20]:
        lines.append(f"{'✅' if t['done'] else '•'} {t['day_label']} в {t['time']} — {t.get('icon','🗒️')} {t['text']}")
    if notes:
        lines.append("")
        lines.append("💡 Идеи, номера и заметки:")
        for t in notes[:20]:
            lines.append(f"{'✅' if t['done'] else '•'} {t.get('icon','💡')} {t['text']}")
    return "\n".join(lines)

CLEAR_TEXT = {
    "done": "все выполненные задачи",
    "all": "ВСЕ задачи и заметки",
    "shop": "все покупки",
    "ideas": "все идеи и заметки",
}

def do_clear(d, owner_uid, mode):
    u = d["users"].get(owner_uid)
    if not u:
        return 0
    before = len(u["tasks"])
    if mode == "done":
        u["tasks"] = [t for t in u["tasks"] if not t["done"]]
    elif mode == "all":
        u["tasks"] = []
    elif mode == "shop":
        u["tasks"] = [t for t in u["tasks"] if t.get("cat") != "покупка"]
    elif mode == "ideas":
        u["tasks"] = [t for t in u["tasks"] if t.get("due")]
    save_data(d)
    return before - len(u["tasks"])

# ================== КОМАНДЫ ==================
def handle_command(d, uid, chat, text, username=None):
    u = d["users"].get(uid)
    if text == "/start":
        if not u:
            d["users"][uid] = new_profile("Пользователь")
        if username:
            d["users"][uid]["username"] = username
        save_data(d)
        send(chat, "👋 Привет! Я — «Мой день» — напоминалка и семейный задачник.\n\n"
                   "Просто напишите или продиктуйте голосом:\n"
                   "«купить молоко, хлеб и яйца» — список покупок\n"
                   "«позвонить Ване в шесть вечера» — напоминание\n"
                   "«запомни номер Вани 8911...» — тихая заметка\n"
                   "«выпить воды» — напомню через 30 минут\n\n"
                   "Кнопки внизу 👇 — быстрый доступ.", rkb=main_kb())
        return True
    if text == "/help":
        send(chat, "❓ Что я умею\n\n"
                   "📌 ЗАДАЧИ И НАПОМИНАНИЯ\n"
                   "• Любое сообщение — новая задача\n"
                   "• Понимаю: «завтра», «в среду», «в 6 вечера»\n"
                   "• Время не назвали — через 30 минут\n"
                   "• Кнопки под задачей: ✅ Сделал, ⏰ Отложить, 🗑 Удалить\n"
                   "• /repeat 20 — как часто повторять (минут)\n\n"
                   "🛒 ПОКУПКИ\n"
                   "• «купить молоко, хлеб и яйца» — разложу по пунктам\n"
                   "• «что купить» — список; «куплено всё» — закрыть всё\n\n"
                   "💡 ЗАМЕТКИ\n"
                   "• «запомни...», «важно...», «номер...», «адрес...» — без напоминаний\n"
                   "• «мои идеи» — посмотреть записи\n\n"
                   "📒 ЗАДАЧНИКИ\n"
                   "• «задачник Дядя» / «мой задачник» — переключение\n"
                   "• /code → семья пишет /link 1234 → /share_on — общий задачник\n\n"
                   "💬 СООБЩЕНИЯ\n"
                   "• «скажи Дядя позвонить маме» — передам от вашего имени\n\n"
                   "🧹 ОЧИСТКА\n"
                   "• «убрать выполненные», «убрать купленное», «очистить идеи», «удалить всё»\n"
                   "• Если 3 раза не подтвердил задачу — оповещу семью")
        return True
    if not u:
        send(chat, "Сначала /start")
        return True
    if text in ("/tasks", "/ideas", "/shop"):
        u2 = d["users"][active_owner(d, uid)]
        ts = u2["tasks"]
        if text == "/shop":
            ts = [t for t in ts if t.get("cat") == "покупка"]
        if text == "/ideas":
            ts = [t for t in ts if not t.get("due")]
        if not ts:
            send(chat, "Здесь пока пусто ✨", rkb=main_kb())
            return True
        send(chat, render_lists(u2, "🛒 Покупки" if text == "/shop" else ("💡 Идеи и заметки" if text == "/ideas" else "📋 Задачи")), rkb=main_kb())
        return True
    if text == "/report":
        u2 = d["users"][active_owner(d, uid)]
        done = [t for t in u2["tasks"] if t["done"]]
        lines = [f"📋 Отчёт за {datetime.now().strftime('%d.%m.%Y')} — {u2['name']}:"]
        for t in sorted(u2["tasks"], key=lambda x: (x.get("due") or "9999")):
            mark = "✅" if t["done"] else "⬜"
            when = f"{t['day_label']} {t['time']}" if t.get("due") else "заметка"
            lines.append(f"{mark} {when} — {t.get('icon','🗒️')} {t['text']}"
                         + (f" ({t['confirmed_at']})" if t["done"] else ""))
        lines.append(f"Итог: {len(done)}/{len(u2['tasks'])}")
        send(chat, "\n".join(lines), rkb=main_kb())
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
        send(chat, f"🤝 Вы подключены к «{pu['name']}».\nПишите мне задачи — я передам. «мои задачи» — увидеть список. /unlink — отключиться.")
        return True
    if text == "/unlink":
        n = 0
        for uu in d["users"].values():
            if uid in uu.get("helpers", []):
                uu["helpers"].remove(uid); n += 1
        save_data(d)
        send(chat, "Отключено" if n else "Вы ни к кому не подключены")
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
    if text == "/notebooks":
        acc = accessible(d, uid)
        cur = active_owner(d, uid)
        lines = ["📒 Ваши задачники:"]
        for i, (ou, nm) in enumerate(acc, 1):
            lines.append(f"{'▶️' if ou == cur else '•'} {i}. {nm}{' (общий)' if ou != uid else ''}")
        lines.append("")
        lines.append("Переключиться: «задачник Дядя» или /use 2")
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
        d["users"][uid]["active"] = ou if ou != uid else "self"
        save_data(d)
        send(chat, f"📒 Переключился на задачник «{nm}»", rkb=main_kb())
        return True
    if text.startswith("/repeat"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat, "Напишите: /repeat 20")
            return True
        d["users"][active_owner(d, uid)]["repeat"] = int(parts[1]); save_data(d)
        send(chat, f"✔ Буду напоминать каждые {parts[1]} минут, пока не подтвердите.", rkb=main_kb())
        return True
    if text in ("/clear_done", "/clear_all", "/clear_shop", "/clear_ideas"):
        mode = text.split("_", 1)[1]
        send(chat, f"🧹 Удалить {CLEAR_TEXT[mode]}?", kb=kb_confirm(mode))
        return True
    if text == "/shop_done":
        owner = d["users"][active_owner(d, uid)]
        n = 0
        for t in owner["tasks"]:
            if t.get("cat") == "покупка" and not t["done"]:
                t["done"] = True; t["status"] = "done"
                t["confirmed_at"] = datetime.now().strftime("%H:%M"); n += 1
        save_data(d)
        send(chat, f"✅ Отмечено купленным: {n} позиций", rkb=main_kb())
        return True
    return False

# ================== ОБРАБОТКА СООБЩЕНИЙ ==================
def process_text(d, uid, chat, text, username=None):
    if text in BUTTON_CMDS:
        text = BUTTON_CMDS[text]
    low = text.lower().strip()
    # передать сообщение от моего имени
    if low.startswith(("скажи ", "передай ", "напиши ")):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3:
            name = parts[1].lstrip("@").lower()
            target = next((k for k, v in d["users"].items()
                           if name_match(name, v.get("name", ""))
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
    # переключение задачника
    if low in ("мой задачник", "моя записная", "к своему задачнику"):
        d["users"].setdefault(uid, new_profile("Я"))["active"] = "self"
        save_data(d)
        send(chat, "📒 Теперь работаем с вашим задачником", rkb=main_kb())
        return
    if low.startswith("задачник "):
        name = low[9:].strip()
        acc = accessible(d, uid)
        target = uid if name in ("мой", "моё") else None
        if target is None:
            for ou, nm in acc:
                if name_match(name, nm):
                    target = ou
                    break
        if target is not None:
            d["users"].setdefault(uid, new_profile("Я"))["active"] = target if target != uid else "self"
            save_data(d)
            send(chat, f"📒 Теперь работаем с задачником «{d['users'][target]['name']}»", rkb=main_kb())
        else:
            send(chat, "Такой задачник не найден. Смотрите: /notebooks")
        return
    helper_of = next((k for k, v in d["users"].items() if uid in v.get("helpers", [])), None)
    if helper_of and not own_mode(d, uid):
        cmd = match_cmd(text)
        target = active_owner(d, uid)
        owner = d["users"][target]
        if cmd in ("/tasks", "/ideas", "/shop", "/notebooks"):
            send(chat, render_lists(owner, f"📋 Задачи «{owner['name']}»"), rkb=main_kb())
            return
        if cmd == "/report":
            done = sum(1 for t in owner["tasks"] if t["done"])
            send(chat, f"📋 {owner['name']}: {done}/{len(owner['tasks'])} выполнено. Не выполнено: {len(owner['tasks'])-done}.")
            return
        if cmd in ("/clear_done", "/clear_all", "/clear_shop", "/clear_ideas"):
            mode = cmd.split("_", 1)[1]
            send(chat, f"🧹 Удалить {CLEAR_TEXT[mode]} «{owner['name']}»?", kb=kb_confirm(mode))
            return
        if cmd == "/shop_done":
            n = 0
            for t in owner["tasks"]:
                if t.get("cat") == "покупка" and not t["done"]:
                    t["done"] = True; t["status"] = "done"
                    t["confirmed_at"] = datetime.now().strftime("%H:%M"); n += 1
            save_data(d)
            send(chat, f"✅ Отмечено купленным: {n} позиций", rkb=main_kb())
            return
        if not text.startswith("/"):
            created = add_tasks(d, target, text, "helper")
            save_data(d)
            if len(created) > 1:
                lines = [f"✔ Передал список ({len(created)} позиций):"] + [c[0] for c in created]
                send(chat, "\n".join(lines))
                for label, due, dl, tid in created:
                    when = f"{dl} в {due.strftime('%H:%M')}" if due else dl
                    send(helper_of, f"📌 {label}\n{when}", kb=kb_task(tid))
            else:
                label, due, dl, tid = created[0]
                when = f"{dl} в {due.strftime('%H:%M')}" if due else dl
                send(chat, f"✔ Передал: {label} ({when})")
                send(helper_of, f"📌 Новая задача от семьи: {label}\n{when}", kb=kb_task(tid))
                if SEND_VOICE:
                    try:
                        send_voice(helper_of, f"Новая задача от семьи: {label}")
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
    # новая задача (или список покупок)
    created = add_tasks(d, active_owner(d, uid), text, "self")
    save_data(d)
    if len(created) > 1:
        lines = [f"🛒 Добавил {len(created)} позиций:"] + [c[0] for c in created]
        send(chat, "\n".join(lines), rkb=main_kb())
    else:
        label, due, dl, tid = created[0]
        when = f"{dl} в {due.strftime('%H:%M')}" if due else dl
        send(chat, f"📌 {label}\n{when}", kb=kb_task(tid))
        if SEND_VOICE:
            try:
                send_voice(chat, f"Добавлено: {label}. {when}.")
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

# ================== КНОПКИ ==================
def handle_callback(d, cb):
    uid = str(cb["from"]["id"])
    data = cb["data"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    parts = data.split(":")
    if parts[0] == "done":
        u, t = find_task(d, uid, int(parts[1]))
        if not t:
            return
        t["done"] = True; t["status"] = "done"; t["fires"] = 0
        t["confirmed_at"] = datetime.now().strftime("%H:%M")
        save_data(d)
        tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
           message_id=cb["message"]["message_id"],
           text=f"✅ {t['text']} — выполнено в {t['confirmed_at']}")
    elif parts[0] == "snz":
        u, t = find_task(d, uid, int(parts[1]))
        if not t:
            return
        mins = int(parts[2])
        t["status"] = "snoozed"
        t["snooze_until"] = (datetime.now() + timedelta(minutes=mins)).isoformat()
        save_data(d)
        tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
           message_id=cb["message"]["message_id"],
           text=f"⏰ «{t['text']}» — напомню через {mins} мин")
    elif parts[0] == "del":
        u, t = find_task(d, uid, int(parts[1]))
        if not t:
            return
        u["tasks"] = [x for x in u["tasks"] if x["id"] != t["id"]]
        save_data(d)
        tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
           message_id=cb["message"]["message_id"],
           text=f"🗑 «{t['text']}» — удалено")
    elif parts[0] == "clr":
        mode, answer = parts[1], parts[2]
        owner = active_owner(d, uid)
        if answer == "no":
            tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
               message_id=cb["message"]["message_id"], text="✖ Очистка отменена")
            return
        n = do_clear(d, owner, mode)
        save_data(d)
        tg("editMessageText", chat_id=cb["message"]["chat"]["id"],
           message_id=cb["message"]["message_id"],
           text=f"🧹 Удалено позиций: {n}")

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
                continue
            due = datetime.fromisoformat(t["due"])
            if due.date() != now.date():
                continue
            last = datetime.fromisoformat(t["last_fire"]) if t["last_fire"] else None
            if now >= due and (last is None or (now - last).total_seconds() >= repeat_minutes(u) * 60):
                t["status"] = "overdue"; t["last_fire"] = now.isoformat()
                t["fires"] = t.get("fires", 0) + 1
                changed = True
                send(int(k), f"🔔 {t['text']}\nПора! Повторяю каждые {repeat_minutes(u)} мин, пока не подтвердите.",
                     kb=kb_task(t["id"]))
                if SEND_VOICE:
                    try:
                        send_voice(int(k), f"Напоминаю: {t['text']}")
                    except Exception as e:
                        log.warning(f"TTS: {e}")
                if t["fires"] == 3 and u.get("helpers"):
                    for h in u["helpers"]:
                        send(int(h), f"⚠️ ВНИМАНИЕ: «{t['text']}» ({u['name']}) не подтверждается уже 3 раза. Может, позвоните?")
        if now.strftime("%H:%M") == "21:00" and u["tasks"] and not u.get("_rep_" + now.strftime("%d%m")):
            done = sum(1 for t in u["tasks"] if t["done"])
            for h in u.get("helpers", []):
                send(int(h), f"📋 {u['name']}: {done}/{len(u['tasks'])} за сегодня. Не выполнено: {len(u['tasks'])-done}. Подробности: /report")
            u["_rep_" + now.strftime("%d%m")] = True; changed = True
    if changed:
        save_data(d)

# ================== HEALTH / MAIN ==================
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
            if datetime.now().date() != last_save_day:
                last_save_day = datetime.now().date()
                for u in d["users"].values():
                    u["tasks"] = [t for t in u["tasks"]
                                  if not t["done"] or (t.get("due") and t["due"][:10] >= str(last_save_day))]
                    for k in list(u.keys()):
                        if k.startswith("_rep_"):
                            del u[k]
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
