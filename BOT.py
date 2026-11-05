import asyncio
import json
import math
import os
import re
import sqlite3
from datetime import datetime
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, FSInputFile
)

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ==========================================
# 1. ASOSIY SOZLAMALAR (SIZNING MA'LUMOTLARINGIZ)
# ==========================================
BOT_TOKEN = "8477917413:AAFz0osuVTsi0OpvqlzvMtRtsPpGv-f5IkI"
ADMIN_IDS = [5360312409]  # Sizning ID raqamingiz
WEBAPP_BASE_URL = "https://pro-guarantee-maritime-ranch.trycloudflare.com"  # Hozir o'zingizda chiqqan yoki quyida aytiladigan trycloudflare linki
REQUIRED_CHANNEL = None

DB_NAME = "quiz_bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==========================================
# 2. MA'LUMOTLAR BAZASI (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            joined_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            subject TEXT,
            scoring_type TEXT,
            is_timed INTEGER,
            start_time TEXT,
            end_time TEXT,
            structure_json TEXT,
            answers_json TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_code TEXT,
            user_id INTEGER,
            full_name TEXT,
            user_answers_json TEXT,
            raw_score REAL,
            max_score REAL,
            rasch_score REAL,
            grade TEXT,
            submitted_at TEXT,
            UNIQUE(test_code, user_id)
        )
    """)
    conn.commit()
    conn.close()

# ==========================================
# 3. JAVOB TEKSHIRISH VA RASCH ALGORITMI
# ==========================================
def normalize_text(val: str) -> str:
    if not val:
        return ""
    val = val.lower().strip()
    for ch in ["ʻ", "ʼ", "’", "‘", "`", "´"]:
        val = val.replace(ch, "'")
    val = re.sub(r"\s+", " ", val)
    return val

def is_answer_correct(user_val: str, correct_val: str) -> bool:
    u_norm = normalize_text(user_val)
    if not u_norm:
        return False
    alternatives = [normalize_text(x) for x in correct_val.split("|")]
    return u_norm in alternatives

def calculate_rasch_scores(test_code: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT answers_json FROM tests WHERE code = ?", (test_code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    correct_answers = json.loads(row[0])
    q_keys = list(correct_answers.keys())

    c.execute("SELECT id, user_id, full_name, user_answers_json FROM submissions WHERE test_code = ?", (test_code,))
    subs = c.fetchall()
    if not subs:
        conn.close()
        return

    n_users = len(subs)
    q_CorrectCount = {k: 0 for k in q_keys}
    user_matrix = {}

    for sub_id, uid, fname, u_json in subs:
        u_ans = json.loads(u_json)
        user_matrix[sub_id] = {}
        for k in q_keys:
            ok = is_answer_correct(u_ans.get(k, ""), correct_answers[k])
            user_matrix[sub_id][k] = 1 if ok else 0
            if ok:
                q_CorrectCount[k] += 1

    q_weights = {}
    for k in q_keys:
        p_correct = (q_CorrectCount[k] + 0.5) / (n_users + 1.0)
        difficulty_logit = math.log((1.0 - p_correct) / p_correct)
        weight = 1.5 + max(-1.0, min(2.0, difficulty_logit * 0.5))
        q_weights[k] = weight

    max_possible_weighted = sum(q_weights.values())

    for sub_id, uid, fname, _ in subs:
        weighted_sum = sum(user_matrix[sub_id][k] * q_weights[k] for k in q_keys)
        raw_correct = sum(user_matrix[sub_id].values())

        rasch_score = round((weighted_sum / max_possible_weighted) * 95.0, 2) if max_possible_weighted > 0 else 0.0
        if raw_correct == len(q_keys):
            rasch_score = 93.0

        if rasch_score >= 70: grade = "A+"
        elif rasch_score >= 65: grade = "A"
        elif rasch_score >= 60: grade = "B+"
        elif rasch_score >= 55: grade = "B"
        elif rasch_score >= 50: grade = "C+"
        elif rasch_score >= 46: grade = "C"
        else: grade = "—"

        c.execute("""
            UPDATE submissions
            SET raw_score = ?, max_score = ?, rasch_score = ?, grade = ?
            WHERE id = ?
        """, (raw_correct, len(q_keys), rasch_score, grade, sub_id))

    conn.commit()
    conn.close()

def generate_results_pdf(test_code: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT subject, scoring_type FROM tests WHERE code = ?", (test_code,))
    t_row = c.fetchone()
    subject, scoring_type = t_row if t_row else ("Huquq", "standard")

    if scoring_type == "rasch":
        c.execute("""
            SELECT full_name, raw_score, max_score, rasch_score, grade
            FROM submissions WHERE test_code = ?
            ORDER BY rasch_score DESC, raw_score DESC
        """, (test_code,))
    else:
        c.execute("""
            SELECT full_name, raw_score, max_score, rasch_score, grade
            FROM submissions WHERE test_code = ?
            ORDER BY raw_score DESC
        """, (test_code,))
    rows = c.fetchall()
    conn.close()

    filename = f"Natija_Test_{test_code}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4, rightMargin=25, leftMargin=25, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=16, alignment=1, spaceAfter=12)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontSize=11, alignment=1, spaceAfter=20)

    elements.append(Paragraph(f"TEST №{test_code} — YAKUNIY NATIJALAR JADVALI", title_style))
    mode_text = "Rash modeli (Milliy sertifikat)" if scoring_type == "rasch" else "Standart baholash"
    elements.append(Paragraph(f"Fan: {subject} | Baholash tizimi: {mode_text} | Sana: {datetime.now().strftime('%d.%m.%Y %H:%M')}", sub_style))

    if scoring_type == "rasch":
        data = [["#", "F.I.SH.", "To'g'ri javob", "Rash bali", "Daraja"]]
        for idx, (fname, raw, mx, r_score, grd) in enumerate(rows, 1):
            clean_name = re.sub(r'[^\x00-\x7Fʻʼ’‘\w\s\.\-]', '', fname) or f"Ishtirokchi #{idx}"
            data.append([str(idx), clean_name[:32], f"{int(raw)} / {int(mx)}", f"{r_score}", grd])
        col_widths = [35, 230, 90, 90, 75]
    else:
        data = [["#", "F.I.SH.", "To'g'ri javob", "Foiz (%)"]]
        for idx, (fname, raw, mx, _, _) in enumerate(rows, 1):
            clean_name = re.sub(r'[^\x00-\x7Fʻʼ’‘\w\s\.\-]', '', fname) or f"Ishtirokchi #{idx}"
            pct = round((raw / mx) * 100, 1) if mx else 0
            data.append([str(idx), clean_name[:35], f"{int(raw)} / {int(mx)}", f"{pct}%"])
        col_widths = [40, 260, 110, 110]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0284c7")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 9),
        ('TOPPADDING', (0, 0), (-1, 0), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
    ]))
    elements.append(table)
    doc.build(elements)
    return filename

def build_personal_report(test_code: str, user_id: int) -> str:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT subject, scoring_type, answers_json FROM tests WHERE code = ?", (test_code,))
    t_row = c.fetchone()
    c.execute("SELECT user_answers_json, raw_score, max_score, rasch_score, grade FROM submissions WHERE test_code = ? AND user_id = ?", (test_code, user_id))
    s_row = c.fetchone()
    conn.close()

    if not t_row or not s_row:
        return "Natija topilmadi."

    subject, scoring_type, ans_json = t_row
    u_ans_json, raw_score, max_score, rasch_score, grade = s_row
    correct_answers = json.loads(ans_json)
    user_answers = json.loads(u_ans_json)

    pct = round((raw_score / max_score) * 100, 1) if max_score else 0
    lines = [
        f"📊 <b>Test №{test_code} bo'yicha natijangiz:</b>\n",
        f"📚 <b>Fan:</b> {subject}",
        f"✅ <b>To'g'ri javoblar:</b> {int(raw_score)} / {int(max_score)} ta ({pct}%)"
    ]
    if scoring_type == "rasch":
        lines.append(f"📈 <b>Rash bali:</b> {rasch_score} ball")
        lines.append(f"🏅 <b>Daraja:</b> {grade}")

    lines.append("\n🔍 <b>Savollar tahlili:</b>")
    detail_items = []
    for k, corr_val in correct_answers.items():
        u_val = user_answers.get(k, "—")
        ok = is_answer_correct(u_val, corr_val)
        icon = "✅" if ok else "❌"
        detail_items.append(f"<b>{k}:</b> {u_val or '—'} {icon}")

    for i in range(0, len(detail_items), 3):
        lines.append("  |  ".join(detail_items[i:i+3]))

    return "\n".join(lines)

# ==========================================
# 4. ADMIN PANEL (FSM)
# ==========================================
class CreateTestState(StatesGroup):
    code = State()
    subject = State()
    scoring_type = State()
    structure_choice = State()
    custom_structure = State()
    time_limit = State()
    answers = State()

@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi test yaratish", callback_data="admin_new_test")],
        [InlineKeyboardButton(text="🏁 Aktiv testni yakunlash (PDF chiqarish)", callback_data="admin_finish_list")]
    ])
    await message.answer("👨‍💻 <b>Admin panelga xush kelibsiz!</b>\nKerakli bo'limni tanlang:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_new_test")
async def start_new_test(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tests")
    next_num = c.fetchone()[0] + 1
    conn.close()

    await state.set_state(CreateTestState.code)
    await call.message.answer(f"1️⃣ <b>Test kodini kiriting</b> (avtomatik <code>{next_num}</code> uchun <b>auto</b> yozing):", parse_mode="HTML")

@dp.message(CreateTestState.code)
async def process_test_code(message: Message, state: FSMContext):
    code_input = message.text.strip()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if code_input.lower() == "auto":
        c.execute("SELECT COUNT(*) FROM tests")
        code_input = str(c.fetchone()[0] + 1)
    c.execute("SELECT id FROM tests WHERE code = ?", (code_input,))
    if c.fetchone():
        conn.close()
        await message.answer("❗️ Bu kod bazada band! Boshqa kod yozing:")
        return
    conn.close()

    await state.update_data(code=code_input)
    await state.set_state(CreateTestState.subject)
    await message.answer("2️⃣ <b>Fan yoki test nomini yozing</b> (masalan: <i>Huquq Milliy Sertifikat</i>):", parse_mode="HTML")

@dp.message(CreateTestState.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(CreateTestState.scoring_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Oddiy test", callback_data="stype_standard")],
        [InlineKeyboardButton(text="📈 Rash modeli (Milliy sertifikat)", callback_data="stype_rasch")]
    ])
    await message.answer("3️⃣ <b>Tekshirish usulini tanlang:</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("stype_"))
async def process_scoring_type(call: CallbackQuery, state: FSMContext):
    stype = call.data.replace("stype_", "")
    await state.update_data(scoring_type=stype)
    await state.set_state(CreateTestState.structure_choice)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 Milliy sertifikat (1-32 A-D, 33-35 A-F, 36-45 a/b yozma)", callback_data="struct_ms45")],
        [InlineKeyboardButton(text="📗 Faqat A, B, C, D variantli", callback_data="struct_abcd")],
        [InlineKeyboardButton(text="📙 Aralash (Variantli + Yozma)", callback_data="struct_mixed")]
    ])
    await call.message.answer("4️⃣ <b>Test tuzilishini tanlang:</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("struct_"))
async def process_structure_choice(call: CallbackQuery, state: FSMContext):
    choice = call.data.replace("struct_", "")
    if choice == "ms45":
        structure = []
        for q in range(1, 33): structure.append({"q": str(q), "type": "abcd"})
        for q in range(33, 36): structure.append({"q": str(q), "type": "abcdef"})
        for q in range(36, 46): structure.append({"q": str(q), "type": "written_ab"})
        await state.update_data(structure=structure, total_q=45)
        await ask_time_limit(call.message, state)
    elif choice == "abcd":
        await state.update_data(struct_mode="abcd")
        await state.set_state(CreateTestState.custom_structure)
        await call.message.answer("Nechta savol? Faqat raqam yozing (masalan: <code>30</code>):", parse_mode="HTML")
    elif choice == "mixed":
        await state.update_data(struct_mode="mixed")
        await state.set_state(CreateTestState.custom_structure)
        await call.message.answer("Variantli va yozma savollar sonini yozing (masalan: <code>25 5</code>):", parse_mode="HTML")

@dp.message(CreateTestState.custom_structure)
async def process_custom_structure(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("struct_mode")
    structure = []
    try:
        if mode == "abcd":
            n = int(message.text.strip())
            for q in range(1, n + 1): structure.append({"q": str(q), "type": "abcd"})
            total_q = n
        else:
            parts = message.text.strip().split()
            n_abcd = int(parts[0])
            n_written = int(parts[1])
            for q in range(1, n_abcd + 1): structure.append({"q": str(q), "type": "abcd"})
            for q in range(n_abcd + 1, n_abcd + n_written + 1): structure.append({"q": str(q), "type": "written_single"})
            total_q = n_abcd + n_written
    except Exception:
        await message.answer("❗️ Xato format! Qaytadan raqam kiriting:")
        return

    await state.update_data(structure=structure, total_q=total_q)
    await ask_time_limit(message, state)

async def ask_time_limit(message: Message, state: FSMContext):
    await state.set_state(CreateTestState.time_limit)
    await message.answer(
        "5️⃣ <b>Test vaqtini belgilang:</b>\n\n"
        "• Vaqtsiz (darhol natija) uchun: <code>0</code> yozing.\n"
        "• Vaqtli uchun namuna: <code>05.11.2026 08:00 - 05.11.2026 22:00</code>",
        parse_mode="HTML"
    )

@dp.message(CreateTestState.time_limit)
async def process_time_limit(message: Message, state: FSMContext):
    txt = message.text.strip()
    if txt == "0":
        await state.update_data(is_timed=0, start_time="", end_time="")
    else:
        try:
            s_str, e_str = [x.strip() for x in txt.split("-")]
            datetime.strptime(s_str, "%d.%m.%Y %H:%M")
            datetime.strptime(e_str, "%d.%m.%Y %H:%M")
            await state.update_data(is_timed=1, start_time=s_str, end_time=e_str)
        except Exception:
            await message.answer("❗️ Vaqt formati xato! Namuna: <code>05.11.2026 08:00 - 05.11.2026 22:00</code> yoki <code>0</code>", parse_mode="HTML")
            return

    await state.set_state(CreateTestState.answers)
    await message.answer("6️⃣ <b>To'g'ri javoblar kalitini yuboring</b> (masalan: <code>ABCDABDC...</code> yoki qatorlab):", parse_mode="HTML")

@dp.message(CreateTestState.answers)
async def process_answers_input(message: Message, state: FSMContext):
    data = await state.get_data()
    structure = data["structure"]
    raw_text = message.text.strip()

    expected_keys = []
    for item in structure:
        if item["type"] == "written_ab":
            expected_keys.append(f"{item['q']}_a")
            expected_keys.append(f"{item['q']}_b")
        else:
            expected_keys.append(str(item["q"]))

    answers_dict = {}
    if ":" in raw_text:
        for line in raw_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                answers_dict[k.strip()] = v.strip()
    else:
        clean_letters = re.sub(r"[^A-Za-z]", "", raw_text).upper()
        if len(clean_letters) != len(expected_keys):
            await message.answer(f"❗️ Siz {len(clean_letters)} ta javob kiritdingiz, lekin testda {len(expected_keys)} ta band bor! Qaytadan yuboring:")
            return
        for k, letter in zip(expected_keys, clean_letters):
            answers_dict[k] = letter

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT INTO tests (code, subject, scoring_type, is_timed, start_time, end_time, structure_json, answers_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["code"], data["subject"], data["scoring_type"],
        data["is_timed"], data["start_time"], data["end_time"],
        json.dumps(structure), json.dumps(answers_dict)
    ))
    conn.commit()
    conn.close()
    await state.clear()

    await message.answer(f"✅ <b>Test muvaffaqiyatli yaratildi!</b>\n🔢 <b>Test kodi:</b> <code>{data['code']}</code>", parse_mode="HTML")

async def finalize_test(test_code: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT scoring_type, status FROM tests WHERE code = ?", (test_code,))
    row = c.fetchone()
    if not row or row[1] == "finished":
        conn.close()
        return

    scoring_type = row[0]
    c.execute("UPDATE tests SET status = 'finished' WHERE code = ?", (test_code,))
    conn.commit()
    conn.close()

    if scoring_type == "rasch":
        calculate_rasch_scores(test_code)

    pdf_path = generate_results_pdf(test_code)
    pdf_file = FSInputFile(pdf_path)

    for adm in ADMIN_IDS:
        try:
            await bot.send_document(adm, pdf_file, caption=f"📊 <b>Test №{test_code} yakunlandi!</b>", parse_mode="HTML")
        except Exception:
            pass

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM submissions WHERE test_code = ?", (test_code,))
    participants = [r[0] for r in c.fetchall()]
    conn.close()

    for uid in participants:
        try:
            report_txt = build_personal_report(test_code, uid)
            await bot.send_message(uid, report_txt, parse_mode="HTML")
            await bot.send_document(uid, FSInputFile(pdf_path), caption=f"📄 Test №{test_code} reytingi")
        except Exception:
            pass

@dp.callback_query(F.data == "admin_finish_list")
async def admin_finish_list(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code, subject FROM tests WHERE status = 'active'")
    active_tests = c.fetchall()
    conn.close()

    if not active_tests:
        await call.message.answer("Hozirda aktiv testlar yo'q.")
        return

    buttons = [[InlineKeyboardButton(text=f"🏁 Yakunlash: №{code} ({subj})", callback_data=f"finish_{code}")] for code, subj in active_tests]
    await call.message.answer("Yakunlamoqchi bo'lgan testingizni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("finish_"))
async def admin_finish_confirm(call: CallbackQuery):
    code = call.data.replace("finish_", "")
    await call.message.answer(f"⏳ Test №{code} yakunlanmoqda...")
    await finalize_test(code)
    await call.message.answer(f"✅ Test №{code} yakunlandi!")

# ==========================================
# 5. FOYDALANUVCHI QISMI
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, full_name, joined_at) VALUES (?, ?, ?)",
              (message.from_user.id, message.from_user.full_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    await message.answer(
        f"👤 Hurmatli <b>{message.from_user.full_name}</b>\n\n"
        f"❗️ Testga javob berish uchun <b>test kodini</b> yuboring.",
        parse_mode="HTML"
    )

@dp.message(F.text)
async def handle_test_code(message: Message):
    code = message.text.strip()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code, subject, is_timed, start_time, end_time, status FROM tests WHERE code = ?", (code,))
    t_row = c.fetchone()
    conn.close()

    if not t_row:
        await message.answer("❗️ Bunday kodli test topilmadi.")
        return

    code, subject, is_timed, start_time, end_time, status = t_row
    if status == "finished":
        await message.answer(f"🏁 <b>{code}-sonli test yakunlangan!</b>", parse_mode="HTML")
        return

    if is_timed:
        now = datetime.now()
        st = datetime.strptime(start_time, "%d.%m.%Y %H:%M")
        et = datetime.strptime(end_time, "%d.%m.%Y %H:%M")
        if now < st:
            await message.answer(f"⏳ Test hali boshlanmadi! Boshlanish: <code>{start_time}</code>", parse_mode="HTML")
            return
        if now > et:
            await finalize_test(code)
            await message.answer(f"⌛️ Test vaqti tugagan!", parse_mode="HTML")
            return

    webapp_url = f"{WEBAPP_BASE_URL}/webapp?test_id={code}&user_id={message.from_user.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ Testga javob berish ⚡️", web_app=WebAppInfo(url=webapp_url))]
    ])
    await message.answer(f"<b>{code}-sonli</b> ({subject}) testga javob berish uchun tugmani bosing:", reply_markup=kb, parse_mode="HTML")

# ==========================================
# 6. WEB SERVER (WEBAPP & API)
# ==========================================
async def serve_webapp(request: web.Request):
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return web.Response(text=html_content, content_type="text/html")

async def api_get_test(request: web.Request):
    code = request.match_info.get("code")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code, subject, structure_json FROM tests WHERE code = ?", (code,))
    row = c.fetchone()
    conn.close()
    if not row: return web.json_response({"error": "Not found"}, status=404)
    structure = json.loads(row[2])
    return web.json_response({"code": row[0], "subject": row[1], "total_q": len(structure), "structure": structure})

async def api_submit_test(request: web.Request):
    body = await request.json()
    test_code = str(body.get("test_code"))
    user_id = int(body.get("user_id"))
    user_answers = body.get("answers", {})

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT subject, scoring_type, is_timed, answers_json, status FROM tests WHERE code = ?", (test_code,))
    t_row = c.fetchone()
    if not t_row:
        conn.close()
        return web.json_response({"ok": False, "error": "Test topilmadi!"})

    subject, scoring_type, is_timed, ans_json, status = t_row
    if status == "finished":
        conn.close()
        return web.json_response({"ok": False, "error": "Test yakunlangan!"})

    c.execute("SELECT id FROM submissions WHERE test_code = ? AND user_id = ?", (test_code, user_id))
    if c.fetchone():
        conn.close()
        return web.json_response({"ok": False, "error": "Siz bu testga allaqachon javob bergansiz!"})

    c.execute("SELECT full_name FROM users WHERE user_id = ?", (user_id,))
    u_row = c.fetchone()
    full_name = u_row[0] if u_row else f"User {user_id}"

    correct_answers = json.loads(ans_json)
    raw_correct = sum(1 for k, c_val in correct_answers.items() if is_answer_correct(user_answers.get(k, ""), c_val))
    max_score = len(correct_answers)

    c.execute("""
        INSERT INTO submissions (test_code, user_id, full_name, user_answers_json, raw_score, max_score, rasch_score, grade, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, '-', ?)
    """, (test_code, user_id, full_name, json.dumps(user_answers), raw_correct, max_score, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    if not is_timed and scoring_type == "standard":
        report_txt = build_personal_report(test_code, user_id)
        await bot.send_message(user_id, report_txt, parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Natijani ko'rish", callback_data=f"view_res_{test_code}")]
        ])
        await bot.send_message(user_id, f"Javobingiz qabul qilindi!", reply_markup=kb, parse_mode="HTML")

    return web.json_response({"ok": True})

async def auto_timer_loop():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT code, end_time FROM tests WHERE is_timed = 1 AND status = 'active'")
            active_timed = c.fetchall()
            conn.close()
            now = datetime.now()
            for code, end_time in active_timed:
                et = datetime.strptime(end_time, "%d.%m.%Y %H:%M")
                if now >= et: await finalize_test(code)
        except Exception:
            pass
        await asyncio.sleep(30)

async def main():
    init_db()
    app = web.Application()
    app.router.add_get("/webapp", serve_webapp)
    app.router.add_get("/api/test/{code}", api_get_test)
    app.router.add_post("/api/submit", api_submit_test)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    asyncio.create_task(auto_timer_loop())
    print("✅ Bot va WebApp server (8080-portda) ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())