import os
import re
from datetime import datetime
from io import BytesIO

from flask import Flask, request
import telebot
from telebot import types
from openpyxl import Workbook

# ================== KONFIG (token/ID seniki bilan) ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8428384522:AAE0bMRykoO3Edyw6R7Y1oGYN36ViM-2Qk0")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6988170724"))

# Render URL: https://APPNAME.onrender.com  (seniki)
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "https://telegram-bott-ejvk.onrender.com")
WEBHOOK_PATH = "/" + BOT_TOKEN
WEBHOOK_URL = WEBHOOK_BASE + WEBHOOK_PATH

# Ruxsat etilgan fayl turlari
ALLOWED_EXT = (".jpg", ".jpeg", ".tif", ".tiff")

# Standart narxlar (so'm / m²) — admin o'zgartiradi
DEFAULT_PRICES = {
    "banner": 45000,
    "qora_banner": 55000,
    "orakal": 60000,
    "matoviy_orakal": 65000,
    "setka": 60000,
    "beklit": 70000,
}

# Ish haqi stavkasi (so'm / m²)
WAGE_RATE = 1500

# =====================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ===== In-memory ma'lumotlar =====
admins = {ADMIN_ID}                      # adminlar to'plami
users = {}                               # user_id -> {phone, tg_name, username}
display_names = {}                       # user_id -> faqat admin ko'radigan nom
prices = DEFAULT_PRICES.copy()           # global narxlar
user_price_overrides = {}                # user_id -> {type: price}
debts = {}                               # user_id -> qarz summasi
orders = []                              # yakuniy buyurtmalar (hisobot uchun)

# Sessiya (buyurtma) konteksti
user_state = {}                          # user_id -> {"section": ..., "sub": ..., "await": ...}
session_items = {}                       # user_id -> [ {record...} ]  (finish bosilgunga qadar)

# ======== YORDAMCHI FUNKSIYALAR ========

def is_image_filename(name: str) -> bool:
    return name.lower().endswith(ALLOWED_EXT)

def fmt_user_name(uid: int) -> str:
    base = users.get(uid, {})
    disp = display_names.get(uid)
    if disp:
        return disp
    if base.get("tg_name"):
        return base["tg_name"]
    return f"user_{uid}"

def ensure_user_exists(msg):
    uid = msg.from_user.id
    if uid not in users:
        users[uid] = {
            "phone": None,
            "tg_name": (msg.from_user.first_name or "").strip(),
            "username": (msg.from_user.username or ""),
        }
    return uid

def get_effective_price(user_id: int, otype: str) -> int:
    if user_id in user_price_overrides and otype in user_price_overrides[user_id]:
        return user_price_overrides[user_id][otype]
    return prices.get(otype, 0)

def parse_qty(text: str) -> int:
    m = re.search(r'(\d+)\s*(ta|dona|x)?\b', text.lower())
    if m:
        try:
            return max(1, int(m.group(1)))
        except:
            return 1
    return 1

def parse_wh(text: str):
    m = re.search(r'(\d+(?:\.\d+)?)\s*[,x\-\*]\s*(\d+(?:\.\d+)?)', text.lower())
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except:
            return None, None
    return None, None

def parse_length(text: str):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(sm|cm)?\b', text.lower())
    if m:
        try:
            return float(m.group(1))
        except:
            return None
    return None

def add_final_order_record(user_id: int, record: dict):
    orders.append({
        "user_id": user_id,
        "type": record["type"],     # ko'rinadigan nom
        "sub": record["sub"],       # None | '1.07'...
        "file": record["file_name"],
        "width_cm": record["width_cm"],
        "height_cm": record["height_cm"],
        "length_cm": record["length_cm"],
        "qty": record["qty"],
        "area": round(record["area_m2"], 4),
        "unit_price": record["unit_price"],   # qo'shdik: hisobotda ko'rsatish uchun
        "price": int(record["price_sum"]),
        "date": record["date"]
    })
    debts[user_id] = debts.get(user_id, 0) + int(record["price_sum"])

def build_main_menu(uid: int):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🛒 Buyurtma", callback_data="m:buyurtma"))
    kb.add(types.InlineKeyboardButton("📊 Hisobot", callback_data="m:hisobot"))
    kb.add(types.InlineKeyboardButton("📩 Aloqa", callback_data="m:aloqa"))
    if uid in admins:
        kb.add(types.InlineKeyboardButton("⚙️ Admin Panel", callback_data="m:admin"))
    return kb

def show_main_menu(chat_id: int):
    bot.send_message(chat_id, "🏠 Asosiy menyu:", reply_markup=build_main_menu(chat_id))

def show_order_menu(chat_id: int):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📌 Banner", callback_data="o:banner"),
        types.InlineKeyboardButton("⬛ Qora Banner", callback_data="o:qora_banner"),
        types.InlineKeyboardButton("💡 Beklit", callback_data="o:beklit"),
        types.InlineKeyboardButton("🟦 Orakal", callback_data="o:orakal"),
        types.InlineKeyboardButton("🟩 Matoviy Orakal", callback_data="o:matoviy_orakal"),
        types.InlineKeyboardButton("🟥 Setka", callback_data="o:setka"),
    )
    kb.add(types.InlineKeyboardButton("📤 Pechatga berish", callback_data="order:finish"))
    bot.send_message(chat_id, "🛒 Buyurtma bo‘limini tanlang:", reply_markup=kb)

def show_sub_menu_for_roll(chat_id: int, section_key: str):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("1.07", callback_data=f"s:{section_key}:1.07"),
        types.InlineKeyboardButton("1.27", callback_data=f"s:{section_key}:1.27"),
        types.InlineKeyboardButton("1.52", callback_data=f"s:{section_key}:1.52"),
        types.InlineKeyboardButton("Kichik", callback_data=f"s:{section_key}:kichik"),
    )
    kb.add(types.InlineKeyboardButton("📤 Pechatga berish", callback_data="order:finish"))
    bot.send_message(chat_id, "Turini tanlang:", reply_markup=kb)

def order_summary_text(items: list):
    total_area = sum(x["area_m2"] for x in items)
    total_sum = sum(x["price_sum"] for x in items)
    lines = ["✅ <b>Buyurtma qabul qilindi</b>"]
    lines.append(f"• Fayllar soni: <b>{len(items)}</b>")
    lines.append(f"• Jami maydon: <b>{total_area:.2f} m²</b>")
    lines.append(f"• Umumiy narx: <b>{total_sum} so‘m</b>")
    return "\n".join(lines), total_area, total_sum

def write_excel_for_items(items: list) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Buyurtma"
    ws.append(["Turi", "Pastki tur", "Fayl", "Eni (cm)", "Boyi (cm)", "Uzunlik (cm)",
               "Soni", "Maydon (m²)", "Birlik narx (so‘m/m²)", "Jami (so‘m)", "Sana"])
    for it in items:
        ws.append([
            it["type"],
            it["sub"] or "",
            it["file_name"],
            it["width_cm"] if it["width_cm"] is not None else "",
            it["height_cm"] if it["height_cm"] is not None else "",
            it["length_cm"] if it["length_cm"] is not None else "",
            it["qty"],
            round(it["area_m2"], 4),
            int(it["unit_price"]),
            int(it["price_sum"]),
            it["date"]
        ])
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio

def write_excel_for_range(rows: list) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Hisobot"
    ws.append(["User ID", "Ko‘rinadigan nom", "Turi", "Pastki tur", "Fayl",
               "Eni (cm)", "Boyi (cm)", "Uzunlik (cm)", "Soni",
               "Maydon (m²)", "Birlik narx (so‘m/m²)", "Jami (so‘m)", "Sana"])
    for o in rows:
        ws.append([
            o["user_id"], fmt_user_name(o["user_id"]), o["type"], o["sub"] or "", o["file"],
            o["width_cm"] if o["width_cm"] is not None else "",
            o["height_cm"] if o["height_cm"] is not None else "",
            o["length_cm"] if o["length_cm"] is not None else "",
            o["qty"], o["area"], o["unit_price"], o["price"], o["date"]
        ])
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio

def parse_date_pair(s: str):
    a, b = s.strip().split()
    d1 = datetime.strptime(a, "%Y-%m-%d")
    d2 = datetime.strptime(b, "%Y-%m-%d")
    if d2 < d1:
        d1, d2 = d2, d1
    return d1, d2

# ================= HANDLERS =================

@bot.message_handler(commands=['start'])
def on_start(msg):
    uid = ensure_user_exists(msg)
    note = (
        "📌 <b>Eslatma</b>\n\n"
        "Fayl yuborilganda <b>fayl nomida</b> o‘lcham va son bo‘lishi <b>shart</b>.\n"
        "Format:\n"
        "• Banner/Qora banner/Beklit/Kichik: <code>eni x bo‘yi</code> (sm) va son — masalan: <code>150x200 4ta</code>\n"
        "• Orakal/Matoviy/Setka (1.07/1.27/1.52): <code>uzunlik</code> (sm) va son — masalan: <code>300 2ta</code>\n"
        "‼️ O‘lcham <b>sm</b> da yoziladi, lekin hisob <b>metrga</b> aylantirilib (1.50 × 1.70 m kabi) m² bo‘yicha qilinadi.\n"
    )
    bot.send_message(uid, note)

    if not users[uid]["phone"]:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(types.KeyboardButton("📱 Ro‘yxatdan o‘tish", request_contact=True))
        bot.send_message(uid, "Ro‘yxatdan o‘tish uchun telefon raqamingizni yuboring 👇", reply_markup=kb)
        return

    show_main_menu(uid)

@bot.message_handler(content_types=['contact'])
def on_contact(msg):
    uid = ensure_user_exists(msg)
    if msg.contact and (msg.contact.user_id == uid or True):
        users[uid]["phone"] = msg.contact.phone_number
        users[uid]["tg_name"] = msg.from_user.first_name or users[uid]["tg_name"]
        users[uid]["username"] = msg.from_user.username or users[uid]["username"]
        bot.send_message(uid, "✅ Ro‘yxatdan o‘tdingiz!", reply_markup=types.ReplyKeyboardRemove())
        show_main_menu(uid)
    else:
        bot.send_message(uid, "Noto‘g‘ri kontakt yuborildi.")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("m:"))
def on_main_menu(call):
    uid = call.message.chat.id
    cmd = call.data.split(":", 1)[1]
    user_state[uid] = {"section": None, "sub": None, "await": None}

    if cmd == "buyurtma":
        session_items[uid] = []
        show_order_menu(uid)
    elif cmd == "hisobot":
        bot.send_message(uid, "⏳ Kun oralig‘ini kiriting: <code>YYYY-MM-DD YYYY-MM-DD</code>")
        user_state[uid]["await"] = "report_range_user"
    elif cmd == "aloqa":
        bot.send_message(uid, "✍️ Xabaringizni yozing. (Faqat Aloqa bo‘limida matn qabul qilinadi)")
        user_state[uid]["await"] = "contact_mode"
    elif cmd == "admin":
        if uid not in admins:
            bot.answer_callback_query(call.id, "Faqat adminlar uchun.")
            return
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💵 Narxlarni boshqarish", callback_data="a:prices"))
        kb.add(types.InlineKeyboardButton("🔖 Mijozga xos narx", callback_data="a:userprice"))
        kb.add(types.InlineKeyboardButton("👤 Ismni o‘zgartirish", callback_data="a:rename"))
        kb.add(types.InlineKeyboardButton("💳 Qarzni tuzatish (+/−)", callback_data="a:debt"))
        kb.add(types.InlineKeyboardButton("🧾 Ish haqi (kun oralig‘i)", callback_data="a:wage"))
        kb.add(types.InlineKeyboardButton("📚 Umumiy hisobot (Excel)", callback_data="a:global_report"))
        kb.add(types.InlineKeyboardButton("➕ Admin qo‘shish", callback_data="a:add_admin"))
        bot.send_message(uid, "⚙️ Admin panel:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("o:"))
def on_order_section(call):
    uid = call.message.chat.id
    section = call.data.split(":")[1]  # banner, qora_banner, ...
    user_state[uid] = {"section": section, "sub": None, "await": None}

    if section in ("orakal", "matoviy_orakal", "setka"):
        show_sub_menu_for_roll(uid, section)
    else:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📤 Pechatga berish", callback_data="order:finish"))
        bot.send_message(uid,
            f"📤 <b>{section.replace('_',' ').title()}</b> fayllarini yuboring.\n"
            f"Format: <code>eni x bo‘yi</code> (sm) va son (masalan: 150x200 4ta).",
            reply_markup=kb
        )

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("s:"))
def on_order_sub(call):
    uid = call.message.chat.id
    _, section, sub = call.data.split(":")  # s:orakal:1.27
    user_state[uid] = {"section": section, "sub": sub, "await": None}
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📤 Pechatga berish", callback_data="order:finish"))

    if sub == "kichik":
        bot.send_message(uid,
            f"📤 <b>{section.replace('_',' ').title()} → Kichik</b> fayllarini yuboring.\n"
            f"Format: <code>eni x bo‘yi</code> (sm) va son (masalan: 100x80 3ta).",
            reply_markup=kb
        )
    else:
        bot.send_message(uid,
            f"📤 <b>{section.replace('_',' ').title()} → {sub}</b> fayllarini yuboring.\n"
            f"Format: <code>uzunlik</code> (sm) va son (masalan: 300 2ta). Eni kelsa ham e’tiborsiz qoldiriladi.",
            reply_markup=kb
        )

@bot.callback_query_handler(func=lambda c: c.data == "order:finish")
def on_finish_order(call):
    uid = call.message.chat.id
    items = session_items.get(uid, [])
    if not items:
        bot.answer_callback_query(call.id, "Buyurtma ro‘yxati bo‘sh.")
        return

    # Excel tayyorla
    excel = write_excel_for_items(items)
    caption, _, _ = order_summary_text(items)

    # Yakuniy bazaga yozish
    for rec in items:
        add_final_order_record(uid, rec)

    # Excel yuborish
    bot.send_document(uid, ("buyurtma.xlsx", excel), caption=caption)

    # Sessiyani tozalash
    session_items[uid] = []

@bot.message_handler(content_types=['document', 'photo'])
def on_file(msg):
    uid = ensure_user_exists(msg)
    st = user_state.get(uid, {"section": None, "sub": None, "await": None})
    section = st.get("section")
    sub = st.get("sub")

    if not section:
        bot.reply_to(msg, "Avval buyurtma bo‘limini tanlang: /start → 🛒 Buyurtma")
        return

    if msg.content_type == 'document':
        file_name = msg.document.file_name or "file"
        if not is_image_filename(file_name):
            bot.reply_to(msg, "❌ Faqat JPG/JPEG yoki TIF/TIFF qabul qilinadi.")
            return
    else:
        bot.reply_to(msg, "❌ Foto (nomisiz) qabul qilinmaydi. Faylni <b>document</b> ko‘rinishida yuboring.")
        return

    name_only = file_name.rsplit(".", 1)[0]
    qty = parse_qty(name_only)
    width_cm, height_cm = parse_wh(name_only)
    length_cm = parse_length(name_only)

    try:
        area_m2 = 0.0
        unit_price = get_effective_price(uid, section)
        if section in ("banner", "qora_banner", "beklit"):
            if (width_cm is None) or (height_cm is None):
                bot.reply_to(msg, "❌ O‘lcham topilmadi. Masalan: <code>150x170 2ta</code> tarzida nomlang.")
                return
            area_m2 = (width_cm / 100.0) * (height_cm / 100.0) * max(1, qty)

        elif section in ("orakal", "matoviy_orakal", "setka"):
            if sub == "kichik":
                if (width_cm is None) or (height_cm is None):
                    bot.reply_to(msg, "❌ O‘lcham topilmadi. Masalan: <code>100x80 3ta</code> tarzida nomlang.")
                    return
                area_m2 = (width_cm / 100.0) * (height_cm / 100.0) * max(1, qty)
            else:
                if sub not in ("1.07", "1.27", "1.52"):
                    bot.reply_to(msg, "❌ Ichki holat xatosi: tur aniqlanmadi.")
                    return
                if length_cm is None:
                    bot.reply_to(msg, "❌ Uzunlik topilmadi. Masalan: <code>300 2ta</code> tarzida nomlang.")
                    return
                coef = float(sub)  # eni sifatida 1.07/1.27/1.52 hisoblanadi
                area_m2 = (length_cm / 100.0) * coef * max(1, qty)

        else:
            bot.reply_to(msg, "❌ Ichki holat xatosi: bo‘lim aniqlanmadi.")
            return

        total_price = round(area_m2 * unit_price)

        rec = {
            "type": section.replace("_", " ").title(),
            "sub": (sub if section in ("orakal", "matoviy_orakal", "setka") else None),
            "file_name": file_name,
            "width_cm": round(width_cm, 2) if width_cm is not None else None,
            "height_cm": round(height_cm, 2) if height_cm is not None else None,
            "length_cm": round(length_cm, 2) if (length_cm is not None and sub not in (None, "kichik")) else None,
            "qty": qty,
            "area_m2": round(area_m2, 4),
            "unit_price": unit_price,
            "price_sum": total_price,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        session_items.setdefault(uid, []).append(rec)
        # Hech qanday xabar yo'q — “Pechatga berish” bosilganda xulosa va Excel ketadi.

    except Exception as e:
        bot.reply_to(msg, f"❌ Hisoblashda xatolik: {e}")

@bot.message_handler(content_types=['text'])
def on_text(msg):
    uid = ensure_user_exists(msg)
    st = user_state.get(uid, {"section": None, "sub": None, "await": None})
    await_mode = st.get("await")

    # ---- Admin interaktiv amallar ----
    if uid in admins and await_mode:
        # Global narxlarni o'zgartirish
        if await_mode == "prices_choose_type":
            key = msg.text.strip().lower().replace(" ", "_")
            if key not in prices:
                bot.reply_to(msg, "Noto‘g‘ri tur. (" + ", ".join(prices.keys()) + ")")
                return
            user_state[uid]["await"] = "prices_set_value"
            user_state[uid]["_price_key"] = key
            bot.reply_to(msg, "Yangi narxni yozing (so‘m/m²):")
            return

        if await_mode == "prices_set_value":
            key = user_state[uid].get("_price_key")
            try:
                val = int(msg.text.strip())
                prices[key] = val
                bot.reply_to(msg, f"✅ {key.replace('_',' ').title()} narxi yangilandi: {val} so‘m/m²")
            except:
                bot.reply_to(msg, "Butun son kiriting, masalan: 60000")
            user_state[uid]["await"] = None
            user_state[uid].pop("_price_key", None)
            return

        # Mijoz nomini o'zgartirish
        if await_mode == "rename_ask_userid":
            try:
                target = int(msg.text.strip())
                if target not in users:
                    bot.reply_to(msg, "Bunday foydalanuvchi topilmadi.")
                else:
                    user_state[uid]["await"] = "rename_ask_name"
                    user_state[uid]["_rename_target"] = target
                    bot.reply_to(msg, "Yangi nomni yozing (faqat admin ko‘radi):")
                return
            except:
                bot.reply_to(msg, "Foydalanuvchi ID sini yozing (butun son).")
                return

        if await_mode == "rename_ask_name":
            target = user_state[uid].get("_rename_target")
            display_names[target] = msg.text.strip()
            bot.reply_to(msg, f"✅ O‘zgartirildi. Endi adminlar uchun nom: {display_names[target]}")
            user_state[uid]["await"] = None
            user_state[uid].pop("_rename_target", None)
            return

        # Qarzni tuzatish
        if await_mode == "debt_ask_userid":
            try:
                target = int(msg.text.strip())
                if target not in users:
                    bot.reply_to(msg, "Bunday foydalanuvchi topilmadi.")
                else:
                    user_state[uid]["await"] = "debt_ask_delta"
                    user_state[uid]["_debt_target"] = target
                    curr = debts.get(target, 0)
                    bot.reply_to(msg, f"Joriy qarz: {curr} so‘m. "
                                      f"Qarzga qo‘shish (+) yoki kamaytirish (−) summasini yozing, masalan: -50000")
                return
            except:
                bot.reply_to(msg, "Foydalanuvchi ID sini yozing (butun son).")
                return

        if await_mode == "debt_ask_delta":
            target = user_state[uid].get("_debt_target")
            try:
                delta = int(msg.text.strip())
                debts[target] = debts.get(target, 0) + delta
                bot.reply_to(msg, f"✅ Yangilandi. Yangi qarz: {debts[target]} so‘m")
            except:
                bot.reply_to(msg, "Butun son kiriting, masalan: -30000 yoki 45000")
            user_state[uid]["await"] = None
            user_state[uid].pop("_debt_target", None)
            return

        # Ish haqi (kun oralig'i)
        if await_mode == "wage_dates":
            try:
                d1, d2 = parse_date_pair(msg.text)
                total_area = 0.0
                for o in orders:
                    od = datetime.strptime(o["date"], "%Y-%m-%d %H:%M")
                    if d1 <= od <= d2:
                        total_area += o["area"]
                wage = round(total_area * WAGE_RATE)
                bot.reply_to(msg, f"🧾 Ish haqi:\n"
                                  f"• Oraliq: {d1.date()} — {d2.date()}\n"
                                  f"• Jami maydon: {total_area:.2f} m²\n"
                                  f"• Stavka: {WAGE_RATE} so‘m/m²\n"
                                  f"• Hisob: <b>{wage} so‘m</b>")
            except:
                bot.reply_to(msg, "Format: <code>YYYY-MM-DD YYYY-MM-DD</code>")
            user_state[uid]["await"] = None
            return

        # Mijozga xos narx
        if await_mode == "userprice_ask_userid":
            try:
                target = int(msg.text.strip())
                if target not in users:
                    bot.reply_to(msg, "Bunday foydalanuvchi topilmadi.")
                else:
                    user_state[uid]["await"] = "userprice_ask_type"
                    user_state[uid]["_up_target"] = target
                    bot.reply_to(msg, "Qaysi tur? (" + ", ".join(prices.keys()) + ")")
                return
            except:
                bot.reply_to(msg, "Foydalanuvchi ID sini yozing (butun son).")
                return

        if await_mode == "userprice_ask_type":
            key = msg.text.strip().lower().replace(" ", "_")
            if key not in prices:
                bot.reply_to(msg, "Noto‘g‘ri tur. (" + ", ".join(prices.keys()) + ")")
                return
            user_state[uid]["await"] = "userprice_ask_value"
            user_state[uid]["_up_type"] = key
            bot.reply_to(msg, "Narxni kiriting (0 yozsangiz — o‘chiriladi):")
            return

        if await_mode == "userprice_ask_value":
            target = user_state[uid].get("_up_target")
            key = user_state[uid].get("_up_type")
            try:
                val = int(msg.text.strip())
                if val <= 0:
                    if target in user_price_overrides and key in user_price_overrides[target]:
                        user_price_overrides[target].pop(key, None)
                    bot.reply_to(msg, f"✅ {fmt_user_name(target)} uchun {key} maxsus narx o‘chirildi.")
                else:
                    user_price_overrides.setdefault(target, {})[key] = val
                    bot.reply_to(msg, f"✅ {fmt_user_name(target)} uchun {key} narxi {val} so‘m/m² qilindi.")
            except:
                bot.reply_to(msg, "Butun son kiriting, masalan: 52000 yoki 0")
            user_state[uid]["await"] = None
            user_state[uid].pop("_up_target", None)
            user_state[uid].pop("_up_type", None)
            return

        # Umumiy hisobot (admin)
        if await_mode == "global_report_range":
            try:
                d1, d2 = parse_date_pair(msg.text)
                rows = []
                for o in orders:
                    od = datetime.strptime(o["date"], "%Y-%m-%d %H:%M")
                    if d1 <= od <= d2:
                        rows.append(o)
                if not rows:
                    bot.reply_to(msg, "Ushbu oraliqda buyurtmalar topilmadi.")
                else:
                    excel = write_excel_for_range(rows)
                    bot.send_document(uid, ("hisobot.xlsx", excel), caption=f"📚 {d1.date()} — {d2.date()} umumiy hisobot")
            except:
                bot.reply_to(msg, "Format: <code>YYYY-MM-DD YYYY-MM-DD</code>")
            user_state[uid]["await"] = None
            return

        # ➕ Admin qo'shish
        if await_mode == "add_admin_userid":
            try:
                new_admin_id = int(msg.text.strip())
                admins.add(new_admin_id)
                bot.reply_to(msg, f"✅ Yangi admin qo‘shildi: <code>{new_admin_id}</code>")
            except:
                bot.reply_to(msg, "Butun son kiriting (foydalanuvchi ID).")
            user_state[uid]["await"] = None
            return

    # ---- Foydalanuvchi rejimlari ----
    # Hisobot (foydalanuvchi o'ziga)
    if await_mode == "report_range_user":
        try:
            d1, d2 = parse_date_pair(msg.text)
            rows = []
            for o in orders:
                if o["user_id"] != uid:
                    continue
                od = datetime.strptime(o["date"], "%Y-%m-%d %H:%M")
                if d1 <= od <= d2:
                    rows.append(o)
            if not rows:
                bot.reply_to(msg, "Ushbu oraliqda buyurtmalar topilmadi.")
            else:
                excel = write_excel_for_range(rows)
                bot.send_document(uid, ("hisobot.xlsx", excel),
                                  caption=f"📚 Sizning hisobot: {d1.date()} — {d2.date()}")
        except:
            bot.reply_to(msg, "Format: <code>YYYY-MM-DD YYYY-MM-DD</code>")
        user_state[uid]["await"] = None
        return

    # Aloqa rejimi: faqat shu holatda matn qabul qilinadi
    if await_mode == "contact_mode":
        for aid in admins:
            try:
                if aid != uid:
                    uname = users[uid].get('username', '')
                    un = f"@{uname}" if uname else ""
                    bot.send_message(aid, f"📩 {fmt_user_name(uid)} {un} (ID: {uid}):\n{msg.text}")
            except:
                pass
        return

    # Boshqa holatlarda matnni o'chiramiz (chatlashish faqat Aloqa bo‘limida)
    try:
        bot.delete_message(uid, msg.message_id)
    except:
        pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("a:"))
def on_admin(call):
    uid = call.message.chat.id
    if uid not in admins:
        bot.answer_callback_query(call.id, "Faqat adminlar uchun.")
        return

    cmd = call.data.split(":", 1)[1]
    user_state[uid] = {"section": None, "sub": None, "await": None}

    if cmd == "prices":
        txt = ["💵 Hozirgi narxlar:"]
        for k, v in prices.items():
            txt.append(f"• {k.replace('_',' ').title()}: {v} so‘m/m²")
        txt.append("\nQaysi tur narxini o‘zgartirasiz? (nomini yozing)")
        bot.send_message(uid, "\n".join(txt))
        user_state[uid]["await"] = "prices_choose_type"
        return

    if cmd == "rename":
        bot.send_message(uid, "Qaysi foydalanuvchi ID sini o‘zgartirasiz? (butun son)")
        user_state[uid]["await"] = "rename_ask_userid"
        return

    if cmd == "debt":
        bot.send_message(uid, "Qaysi foydalanuvchi ID sini tanlaysiz? (butun son)")
        user_state[uid]["await"] = "debt_ask_userid"
        return

    if cmd == "wage":
        bot.send_message(uid, "Kun oraliqni kiriting: <code>YYYY-MM-DD YYYY-MM-DD</code>")
        user_state[uid]["await"] = "wage_dates"
        return

    if cmd == "userprice":
        bot.send_message(uid, "Foydalanuvchi ID sini yozing (butun son)")
        user_state[uid]["await"] = "userprice_ask_userid"
        return

    if cmd == "global_report":
        bot.send_message(uid, "Kun oralig‘ini kiriting: <code>YYYY-MM-DD YYYY-MM-DD</code>")
        user_state[uid]["await"] = "global_report_range"
        return

    if cmd == "add_admin":
        bot.send_message(uid, "Yangi adminning foydalanuvchi ID sini kiriting (butun son):")
        user_state[uid]["await"] = "add_admin_userid"
        return

# ================== WEBHOOK ROUTES ==================

@app.route(WEBHOOK_PATH, methods=['POST'])
def telegram_webhook():
    try:
        json_str = request.stream.read().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        return f"ERR {e}", 200
    return "OK", 200

@app.route("/", methods=['GET'])
def index():
    # Webhook ni har GET da yangilaymiz
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    return "Bot webhook set OK", 200

# ================== MAIN ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
