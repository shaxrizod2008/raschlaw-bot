import asyncio
import json
import math
import os
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ==========================================
# 1. ASOSIY SOZLAMALAR
# ==========================================
BOT_TOKEN = "8477917413:AAFz0osuVTsi0OpvqlzvMtRtsPpGv-f5IkI"
ADMIN_IDS = [5360312409]

REQUIRED_CHANNELS = [
    {
        "title": "1-kanal: Qonun Eksperti",
        "username": "@Qonun_Eksperti",
        "url": "https://t.me/Qonun_Eksperti"
    },
    {
        "title": "2-kanal: Huquq Sertifikati",
        "username": "@Huquq_sertifikati_uz",
        "url": "https://t.me/Huquq_sertifikati_uz"
    }
]

DB_NAME = "quiz_bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==========================================
# MAJBURIY OBUNA TEKSHIRISH
# ==========================================
async def check_subscriptions(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    for ch in REQUIRED_CHANNELS:
        try:
            m = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            if m.status in ["left", "kicked"]:
                return False
        except Exception:
            return False
    return True

def get_subscription_keyboard(cb: str = "check_sub_start") -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"➕ {ch['title']}", url=ch["url"])]
        for ch in REQUIRED_CHANNELS
    ]
    btns.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data=cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)

# ==========================================
# 2. MA'LUMOTLAR BAZASI (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "user_id INTEGER PRIMARY KEY, full_name TEXT, "
        "joined_at TEXT, active_test_code TEXT DEFAULT '')"
    )
    try:
        c.execute("ALTER TABLE users ADD COLUMN active_test_code TEXT DEFAULT ''")
    except Exception:
        pass

    c.execute(
        "CREATE TABLE IF NOT EXISTS tests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, "
        "subject TEXT, scoring_type TEXT, is_timed INTEGER, "
        "start_time TEXT, end_time TEXT, structure_json TEXT, "
        "answers_json TEXT, status TEXT DEFAULT 'active')"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS submissions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, test_code TEXT, "
        "user_id INTEGER, full_name TEXT, user_answers_json TEXT, "
        "raw_score REAL, max_score REAL, rasch_score REAL, "
        "grade TEXT, submitted_at TEXT, UNIQUE(test_code, user_id))"
    )
    conn.commit()
    conn.close()

# ==========================================
# 3. JAVOB TEKSHIRISH VA RASCH MODELI
# ==========================================
UNIT_SUFFIX_RE = re.compile(
    r"^(\d+(?:[.,/]\d+)?)"
    r"(?:yil|oyda|oy|kun|soat|hafta|foiz|baravar|ta|yosh|nafar|marta|qismi|qism|%)+$",
    re.IGNORECASE
)

def normalize_text(val: str) -> str:
    if not val:
        return ""
    val = str(val).lower().strip()
    for ch in ["ʻ", "ʼ", "’", "‘", "`", "´"]:
        val = val.replace(ch, "'")
    return re.sub(r"\s+", "", val.replace(",", "."))

def get_answer_variants(correct_val: str) -> list:
    variants = []
    for raw_part in str(correct_val).split("|"):
        norm = normalize_text(raw_part)
        if norm and norm not in variants:
            variants.append(norm)
            m = UNIT_SUFFIX_RE.match(norm)
            if m and m.group(1) not in variants:
                variants.append(m.group(1))
    return variants or [""]

def is_answer_correct(user_val: str, correct_val: str) -> bool:
    u_norm = normalize_text(user_val)
    if not u_norm:
        return False
    u_variants = [u_norm]
    m = UNIT_SUFFIX_RE.match(u_norm)
    if m:
        u_variants.append(m.group(1))
    corr_variants = get_answer_variants(correct_val)
    return any(uv in corr_variants for uv in u_variants)

def get_certificate_grade(score_val: float) -> str:
    """
    Milliy sertifikat darajalarini 5% qadam bilan hisoblash:
    C: 46% - 49% | C+: 50% - 54% | B: 55% - 59%
    B+: 60% - 64% | A: 65% - 69% | A+: 70% va undan yuqori
    """
    if score_val >= 70:
        return "A+"
    elif score_val >= 65:
        return "A"
    elif score_val >= 60:
        return "B+"
    elif score_val >= 55:
        return "B"
    elif score_val >= 50:
        return "C+"
    elif score_val >= 46:
        return "C"
    return "—"

def get_question_keys_split(structure: list):
    mc_keys, written_keys = [], []
    for item in structure:
        q_num, q_type = str(item["q"]), item["type"]
        if q_type in ("abcd", "abcdef"):
            mc_keys.append(q_num)
        elif q_type == "written_ab":
            written_keys.extend([f"{q_num}_a", f"{q_num}_b"])
        else:
            written_keys.append(q_num)
    return mc_keys, written_keys

def parse_admin_answers(raw_text: str, structure: list):
    raw_text = raw_text.strip()
    mc_keys, written_keys = get_question_keys_split(structure)
    answers_dict = {}

    if ":" in raw_text:
        for line in raw_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                answers_dict[k.strip()] = v.strip()
        return answers_dict, structure

    if not written_keys:
        clean_letters = re.sub(r"[^A-Za-z]", "", raw_text).upper()
        if len(clean_letters) != len(mc_keys):
            return None, (
                f"❗️ Testda {len(mc_keys)} ta variantli savol bor, "
                f"siz {len(clean_letters)} ta harf kiritdingiz! Qaytadan yuboring:"
            )
        for k, letter in zip(mc_keys, clean_letters):
            answers_dict[k] = letter
        return answers_dict, structure

    n_mc = len(mc_keys)
    mc_collected, end_mc_idx = [], 0
    for idx, ch in enumerate(raw_text):
        if len(mc_collected) < n_mc:
            if ch.isalpha():
                mc_collected.append(ch.upper())
                end_mc_idx = idx + 1
            elif ch.isspace():
                continue
            else:
                break
        else:
            break

    if len(mc_collected) < n_mc:
        return None, (
            f"❗️ Boshida {n_mc} ta variant harfi bo'lishi kerak, "
            f"siz {len(mc_collected)} ta harf kiritdingiz!"
        )

    for k, letter in zip(mc_keys, mc_collected):
        answers_dict[k] = letter

    rem = raw_text[end_mc_idx:].strip()
    if not rem:
        return None, "❗️ Variantlardan keyin yozma savollar javobini ham kiriting!"

    spaced_tokens = [t for t in re.split(r"[\s,;]+", rem) if t]
    if len(spaced_tokens) > 1:
        tokens = spaced_tokens
    else:
        rem_clean = re.sub(r"\s+", "", rem)
        token_pat = (
            r"\d+(?:[.,/]\d+)?"
            r"(?:yil|oyda|oy|kun|soat|hafta|foiz|baravar|ta|yosh|nafar|marta|qismi|qism|%)*"
            r"|[a-zA-Zʻʼ’‘']+"
        )
        tokens = re.findall(token_pat, rem_clean, flags=re.IGNORECASE)

    if len(tokens) == len(written_keys):
        for k, tok in zip(written_keys, tokens):
            answers_dict[k] = tok
        return answers_dict, structure

    if len(tokens) > 1:
        new_struct = [it for it in structure if it["type"] in ("abcd", "abcdef")]
        written_keys = []
        if len(tokens) == 20 and len(mc_keys) == 35:
            for q in range(36, 46):
                new_struct.append({"q": str(q), "type": "written_ab"})
                written_keys.extend([f"{q}_a", f"{q}_b"])
        else:
            for i in range(len(tokens)):
                q_str = str(len(mc_keys) + 1 + i)
                new_struct.append({"q": q_str, "type": "written_single"})
                written_keys.append(q_str)
        for k, tok in zip(written_keys, tokens):
            answers_dict[k] = tok
        return answers_dict, new_struct

    rem_clean = re.sub(r"\s+", "", rem)
    w_count = len(written_keys)
    if len(rem_clean) < w_count:
        return None, (
            f"❗️ Yozma javoblar qismi juda qisqa. "
            f"Kamida {w_count} ta javob kiriting:"
        )
    base_len, extra, pos = len(rem_clean) // w_count, len(rem_clean) % w_count, 0
    for i, k in enumerate(written_keys):
        step = base_len + (1 if i < extra else 0)
        answers_dict[k] = rem_clean[pos:pos + step]
        pos += step
    return answers_dict, structure

def parse_student_answers(raw_text: str, structure: list, correct_answers: dict):
    raw_text = raw_text.strip()
    mc_set = {str(it["q"]) for it in structure if it["type"] in ("abcd", "abcdef")}
    mc_keys = [k for k in correct_answers.keys() if k in mc_set]
    written_keys = [k for k in correct_answers.keys() if k not in mc_set]
    user_answers = {}

    if not written_keys:
        clean_letters = re.sub(r"[^A-Za-z]", "", raw_text).upper()
        if len(clean_letters) != len(mc_keys):
            return None, (
                f"❗️ Testda <b>{len(mc_keys)} ta</b> savol bor, "
                f"siz <b>{len(clean_letters)} ta</b> harf yubordingiz.\n"
                f"✍️ To'liq ketma-ket yuboring (masalan: <code>adbcdbd...</code>):"
            )
        for k, letter in zip(mc_keys, clean_letters):
            user_answers[k] = letter
        return user_answers, None

    n_mc = len(mc_keys)
    mc_collected, end_mc_idx = [], 0
    for idx, ch in enumerate(raw_text):
        if len(mc_collected) < n_mc:
            if ch.isalpha():
                mc_collected.append(ch.upper())
                end_mc_idx = idx + 1
            elif ch.isspace():
                continue
            else:
                break
        else:
            break

    if len(mc_collected) < n_mc:
        return None, (
            f"❗️ Dastlabki <b>{n_mc} ta</b> savol variantli (harfli) "
            f"bo'lishi kerak. Siz {len(mc_collected)} ta harf yozdingiz.\n"
            f"Masalan: <code>adbcdbd26189287senatordeputat</code>"
        )

    for k, letter in zip(mc_keys, mc_collected):
        user_answers[k] = letter

    rem = raw_text[end_mc_idx:].strip()
    if not rem:
        return None, (
            f"❗️ Variantlar ketidan <b>{len(written_keys)} ta yozma savol</b> "
            f"javobini ham joy tashlamasdan yozib yuboring!"
        )

    spaced_tokens = [t for t in re.split(r"[\s,;]+", rem) if t]
    if len(spaced_tokens) == len(written_keys):
        for k, tok in zip(written_keys, spaced_tokens):
            user_answers[k] = tok
        return user_answers, None

    rem_norm = normalize_text(rem)
    pos, m = 0, len(written_keys)
    for i, k in enumerate(written_keys):
        corr_alts = get_answer_variants(correct_answers[k])
        matched = next(
            (alt for alt in corr_alts if alt and rem_norm[pos:pos + len(alt)] == alt),
            None
        )
        if matched is not None:
            user_answers[k] = matched
            pos += len(matched)
        else:
            exp_len = len(corr_alts[0]) if corr_alts[0] else 1
            next_sync = None
            if i + 1 < m:
                next_alts = get_answer_variants(correct_answers[written_keys[i + 1]])
                for n_alt in next_alts:
                    if len(n_alt) >= 1:
                        idx_f = rem_norm.find(n_alt, pos + 1, pos + exp_len + 12)
                        if idx_f != -1:
                            next_sync = idx_f
                            break
            if i == m - 1:
                user_answers[k] = rem_norm[pos:] if pos < len(rem_norm) else "—"
                pos = len(rem_norm)
            elif next_sync is not None:
                user_answers[k] = rem_norm[pos:next_sync]
                pos = next_sync
            else:
                chunk = rem_norm[pos:pos + exp_len]
                user_answers[k] = chunk if chunk else "—"
                pos += exp_len
    return user_answers, None

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

    c.execute(
        "SELECT id, user_id, full_name, user_answers_json "
        "FROM submissions WHERE test_code = ?",
        (test_code,)
    )
    subs = c.fetchall()
    if not subs:
        conn.close()
        return

    n_users = len(subs)
    q_corr = {k: 0 for k in q_keys}
    u_mat = {}
    for sid, uid, fname, u_json in subs:
        u_ans = json.loads(u_json)
        u_mat[sid] = {}
        for k in q_keys:
            ok = is_answer_correct(u_ans.get(k, ""), correct_answers[k])
            u_mat[sid][k] = 1 if ok else 0
            if ok:
                q_corr[k] += 1

    q_weights = {}
    for k in q_keys:
        p = (q_corr[k] + 0.5) / (n_users + 1.0)
        logit = math.log((1.0 - p) / p)
        q_weights[k] = 1.5 + max(-1.0, min(2.0, logit * 0.5))

    max_w = sum(q_weights.values())
    for sid, _, _, _ in subs:
        w_sum = sum(u_mat[sid][k] * q_weights[k] for k in q_keys)
        raw_c = sum(u_mat[sid].values())
        r_score = round((w_sum / max_w) * 95.0, 2) if max_w > 0 else 0.0
        if raw_c == len(q_keys):
            r_score = 93.0
        grade = get_certificate_grade(r_score)
        c.execute(
            "UPDATE submissions "
            "SET raw_score=?, max_score=?, rasch_score=?, grade=? WHERE id=?",
            (raw_c, len(q_keys), r_score, grade, sid)
        )
    conn.commit()
    conn.close()

def generate_results_pdf(test_code: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT subject, scoring_type FROM tests WHERE code = ?", (test_code,))
    t_row = c.fetchone()
    subject, scoring_type = t_row if t_row else ("Huquq", "standard")
    if scoring_type == "rasch":
        conn.close()
        calculate_rasch_scores(test_code)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
    c.execute(
        "SELECT full_name, raw_score, max_score, rasch_score, grade "
        "FROM submissions WHERE test_code = ?",
        (test_code,)
    )
    rows = c.fetchall()
    conn.close()

    rows = sorted(
        rows,
        key=lambda x: (
            (float(x[3] or 0), float(x[1] or 0))
            if scoring_type == "rasch"
            else float(x[1] or 0)
        ),
        reverse=True
    )
    filename = f"Natija_Test_{test_code}.pdf"
    doc = SimpleDocTemplate(
        filename, pagesize=A4,
        rightMargin=25, leftMargin=25, topMargin=30, bottomMargin=30
    )
    styles = getSampleStyleSheet()
    t_style = ParagraphStyle(
        "T", parent=styles["Heading1"], fontSize=16, alignment=1, spaceAfter=12
    )
    s_style = ParagraphStyle(
        "S", parent=styles["Normal"], fontSize=11, alignment=1, spaceAfter=20
    )
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    elements = [
        Paragraph(f"TEST №{test_code} — YAKUNIY NATIJALAR JADVALI", t_style),
        Paragraph(f"Fan: {subject} | Sana: {now_str}", s_style)
    ]
    if scoring_type == "rasch":
        data = [["#", "F.I.SH.", "To'g'ri javob", "Rash bali", "Daraja"]]
        if not rows:
            data.append(["1", "Ishtirokchilar yo'q", "0 / 0", "0", "—"])
        for idx, (fn, raw, mx, rs, grd) in enumerate(rows, 1):
            cn = re.sub(r"[^\x00-\x7Fʻʼ’‘\w\s\.\-]", "", fn) or f"Ishtirokchi #{idx}"
            data.append([str(idx), cn[:32], f"{int(raw)} / {int(mx)}", f"{rs}", grd])
        col_w = [35, 230, 90, 90, 75]
    else:
        data = [["#", "F.I.SH.", "To'g'ri javob", "Foiz (%)"]]
        if not rows:
            data.append(["1", "Ishtirokchilar yo'q", "0 / 0", "0%"])
        for idx, (fn, raw, mx, _, _) in enumerate(rows, 1):
            cn = re.sub(r"[^\x00-\x7Fʻʼ’‘\w\s\.\-]", "", fn) or f"Ishtirokchi #{idx}"
            pct = round((raw / mx) * 100, 1) if mx else 0
            data.append([str(idx), cn[:35], f"{int(raw)} / {int(mx)}", f"{pct}%"])
        col_w = [40, 260, 110, 110]

    tbl = Table(data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0284c7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ALIGN", (1, 1), (1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
    ]))
    elements.append(tbl)
    doc.build(elements)
    return filename

def build_personal_report(test_code: str, user_id: int) -> str:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT subject, scoring_type, answers_json FROM tests WHERE code = ?",
        (test_code,)
    )
    t_row = c.fetchone()
    if t_row and t_row[1] == "rasch":
        conn.close()
        calculate_rasch_scores(test_code)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
    c.execute(
        "SELECT user_answers_json, raw_score, max_score, rasch_score, grade "
        "FROM submissions WHERE test_code = ? AND user_id = ?",
        (test_code, user_id)
    )
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
    else:
        lines.append(f"🏅 <b>Daraja:</b> {get_certificate_grade(pct)}")

    lines.append("\n🔍 <b>Savollar tahlili:</b>")
    items = [
        f"<b>{k}:</b> {user_answers.get(k, '—')} "
        f"{'✅' if is_answer_correct(user_answers.get(k, ''), cv) else '❌'}"
        for k, cv in correct_answers.items()
    ]
    for i in range(0, len(items), 3):
        lines.append("  |  ".join(items[i:i + 3]))
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
async def admin_menu(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi test yaratish", callback_data="admin_new_test")],
        [InlineKeyboardButton(
            text="🏁 Aktiv testni yakunlash (PDF chiqarish)",
            callback_data="admin_finish_list"
        )]
    ])
    await message.answer(
        "👨‍💻 <b>Admin panelga xush kelibsiz!</b>",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_new_test")
async def start_new_test(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tests")
    nxt = c.fetchone()[0] + 1
    conn.close()
    await state.set_state(CreateTestState.code)
    await call.message.answer(
        f"1️⃣ <b>Test kodini kiriting</b> "
        f"(avtomatik <code>{nxt}</code> uchun <b>auto</b> yozing):",
        parse_mode="HTML"
    )

@dp.message(CreateTestState.code)
async def process_test_code(message: Message, state: FSMContext):
    code_in = message.text.strip()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if code_in.lower() == "auto":
        c.execute("SELECT COUNT(*) FROM tests")
        code_in = str(c.fetchone()[0] + 1)
    c.execute("SELECT id FROM tests WHERE code = ?", (code_in,))
    if c.fetchone():
        conn.close()
        await message.answer("❗️ Bu kod bazada band! Boshqa kod yozing:")
        return
    conn.close()
    await state.update_data(code=code_in)
    await state.set_state(CreateTestState.subject)
    await message.answer("2️⃣ <b>Fan yoki test nomini yozing:</b>", parse_mode="HTML")

@dp.message(CreateTestState.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(CreateTestState.scoring_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Oddiy test", callback_data="stype_standard")],
        [InlineKeyboardButton(
            text="📈 Rash modeli (Milliy sertifikat)",
            callback_data="stype_rasch"
        )]
    ])
    await message.answer(
        "3️⃣ <b>Tekshirish usulini tanlang:</b>",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("stype_"))
async def process_scoring_type(call: CallbackQuery, state: FSMContext):
    await state.update_data(scoring_type=call.data.replace("stype_", ""))
    await state.set_state(CreateTestState.structure_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📘 Milliy sertifikat (1-32 A-D, 33-35 A-F, 36-45 yozma)",
            callback_data="struct_ms45"
        )],
        [InlineKeyboardButton(
            text="📗 Faqat A, B, C, D variantli",
            callback_data="struct_abcd"
        )],
        [InlineKeyboardButton(
            text="📙 Aralash (Variantli + Yozma)",
            callback_data="struct_mixed"
        )]
    ])
    await call.message.answer(
        "4️⃣ <b>Test tuzilishini tanlang:</b>",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("struct_"))
async def process_structure_choice(call: CallbackQuery, state: FSMContext):
    ch = call.data.replace("struct_", "")
    if ch == "ms45":
        struct = (
            [{"q": str(q), "type": "abcd"} for q in range(1, 33)] +
            [{"q": str(q), "type": "abcdef"} for q in range(33, 36)] +
            [{"q": str(q), "type": "written_single"} for q in range(36, 46)]
        )
        await state.update_data(structure=struct, total_q=45)
        await ask_time_limit(call.message, state)
    elif ch == "abcd":
        await state.update_data(struct_mode="abcd")
        await state.set_state(CreateTestState.custom_structure)
        await call.message.answer(
            "Nechta savol? Faqat raqam yozing (masalan: <code>30</code>):",
            parse_mode="HTML"
        )
    else:
        await state.update_data(struct_mode="mixed")
        await state.set_state(CreateTestState.custom_structure)
        await call.message.answer(
            "Variantli va yozma savollar sonini yozing (masalan: <code>35 10</code>):",
            parse_mode="HTML"
        )

@dp.message(CreateTestState.custom_structure)
async def process_custom_structure(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        if data.get("struct_mode") == "abcd":
            n = int(message.text.strip())
            struct = [{"q": str(q), "type": "abcd"} for q in range(1, n + 1)]
        else:
            p = message.text.strip().split()
            na, nw = int(p[0]), int(p[1])
            struct = (
                [{"q": str(q), "type": "abcd"} for q in range(1, na + 1)] +
                [{"q": str(q), "type": "written_single"} for q in range(na + 1, na + nw + 1)]
            )
    except Exception:
        await message.answer("❗️ Xato format! Qaytadan raqam kiriting:")
        return
    await state.update_data(structure=struct, total_q=len(struct))
    await ask_time_limit(message, state)

async def ask_time_limit(message: Message, state: FSMContext):
    await state.set_state(CreateTestState.time_limit)
    await message.answer(
        "5️⃣ <b>Test vaqtini belgilang:</b>\n"
        "• Vaqtsiz uchun: <code>0</code>\n"
        "• Vaqtli uchun: <code>10.11.2026 08:00 - 10.11.2026 22:00</code>",
        parse_mode="HTML"
    )

@dp.message(CreateTestState.time_limit)
async def process_time_limit(message: Message, state: FSMContext):
    txt = message.text.strip()
    if txt == "0":
        await state.update_data(is_timed=0, start_time="", end_time="")
    else:
        try:
            s, e = [x.strip() for x in txt.split("-")]
            datetime.strptime(s, "%d.%m.%Y %H:%M")
            datetime.strptime(e, "%d.%m.%Y %H:%M")
            await state.update_data(is_timed=1, start_time=s, end_time=e)
        except Exception:
            await message.answer(
                "❗️ Vaqt formati xato! Qaytadan kiriting yoki <code>0</code> yozing:",
                parse_mode="HTML"
            )
            return
    await state.set_state(CreateTestState.answers)
    await message.answer(
        "6️⃣ <b>To'g'ri javoblar kalitini yuboring</b>\n"
        "(masalan: <code>adbcdbd...</code> yoki "
        "<code>adbcdbd26189287senatordeputat</code>):",
        parse_mode="HTML"
    )

@dp.message(CreateTestState.answers)
async def process_answers_input(message: Message, state: FSMContext):
    data = await state.get_data()
    ans_dict, struct_or_err = parse_admin_answers(
        message.text.strip(), data["structure"]
    )
    if ans_dict is None:
        await message.answer(struct_or_err, parse_mode="HTML")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    sql_q = (
        "INSERT INTO tests "
        "(code, subject, scoring_type, is_timed, start_time, end_time, "
        "structure_json, answers_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    c.execute(
        sql_q,
        (
            data["code"], data["subject"], data["scoring_type"],
            data["is_timed"], data["start_time"], data["end_time"],
            json.dumps(struct_or_err), json.dumps(ans_dict)
        )
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(
        f"✅ <b>Test muvaffaqiyatli yaratildi!</b>\n"
        f"🔢 <b>Test kodi:</b> <code>{data['code']}</code>\n"
        f"📊 <b>Savollar soni:</b> {len(ans_dict)} ta",
        parse_mode="HTML"
    )

async def finalize_test(test_code: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT scoring_type, status FROM tests WHERE code = ?", (test_code,))
    row = c.fetchone()
    if not row or row[1] == "finished":
        conn.close()
        return
    c.execute("UPDATE tests SET status = 'finished' WHERE code = ?", (test_code,))
    conn.commit()
    conn.close()
    if row[0] == "rasch":
        calculate_rasch_scores(test_code)
    pdf_path = generate_results_pdf(test_code)
    for adm in ADMIN_IDS:
        try:
            await bot.send_document(
                adm, FSInputFile(pdf_path),
                caption=f"📊 <b>Test №{test_code} yakunlandi!</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM submissions WHERE test_code = ?", (test_code,))
    uids = [r[0] for r in c.fetchall()]
    conn.close()
    for uid in uids:
        try:
            await bot.send_message(
                uid, build_personal_report(test_code, uid), parse_mode="HTML"
            )
            await bot.send_document(
                uid, FSInputFile(pdf_path),
                caption=f"📄 Test №{test_code} umumiy reytingi"
            )
        except Exception:
            pass

@dp.callback_query(F.data == "admin_finish_list")
async def admin_finish_list(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code, subject FROM tests WHERE status = 'active'")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await call.message.answer("Hozirda aktiv testlar yo'q.")
        return
    btns = [
        [InlineKeyboardButton(
            text=f"🏁 Yakunlash: №{c_code} ({subj})",
            callback_data=f"finish_{c_code}"
        )]
        for c_code, subj in rows
    ]
    await call.message.answer(
        "Yakunlamoqchi bo'lgan testingizni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )

@dp.callback_query(F.data.startswith("finish_"))
async def admin_finish_confirm(call: CallbackQuery):
    code = call.data.replace("finish_", "")
    await finalize_test(code)
    await call.message.answer(f"✅ Test №{code} yakunlandi!")

# ==========================================
# 5. O'QUVCHI QISMI VA JAVOB QABUL QILISH
# ==========================================
class RegisterState(StatesGroup):
    waiting_for_name = State()

class SolveTestState(StatesGroup):
    waiting_for_answers = State()

async def process_code_for_user(
    user_id: int, full_name: str, code: str, target_message: Message, state: FSMContext
):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT code, subject, scoring_type, is_timed, start_time, end_time, "
        "structure_json, answers_json, status FROM tests WHERE code = ?",
        (code,)
    )
    t_row = c.fetchone()
    if not t_row:
        conn.close()
        await target_message.answer("❗️ Bunday kodli test topilmadi.")
        return
    code, subject, scoring_type, is_timed, st_str, et_str, s_json, a_json, status = t_row
    c.execute(
        "SELECT id FROM submissions WHERE test_code = ? AND user_id = ?",
        (code, user_id)
    )
    submitted = c.fetchone() is not None
    conn.close()

    if status == "finished":
        rep = build_personal_report(code, user_id)
        if rep != "Natija topilmadi.":
            await target_message.answer(rep, parse_mode="HTML")
        pdf_p = f"Natija_Test_{code}.pdf"
        if not os.path.exists(pdf_p):
            pdf_p = generate_results_pdf(code)
        await target_message.answer_document(
            FSInputFile(pdf_p),
            caption=f"🏁 <b>{code}-sonli ({subject}) test yakunlangan!</b>",
            parse_mode="HTML"
        )
        return

    if is_timed:
        now = datetime.now()
        if now < datetime.strptime(st_str, "%d.%m.%Y %H:%M"):
            await target_message.answer(
                f"⏳ Test hali boshlanmadi! Boshlanish vaqti: <code>{st_str}</code>",
                parse_mode="HTML"
            )
            return
        if now > datetime.strptime(et_str, "%d.%m.%Y %H:%M"):
            await finalize_test(code)
            await target_message.answer(
                "⌛️ Bu testning vaqti tugagan!", parse_mode="HTML"
            )
            return

    if submitted:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📊 Natijani ko'rish", callback_data=f"view_res_{code}"
            )]
        ])
        await target_message.answer(
            f"✅ Siz <b>{code}-sonli</b> testga allaqachon javob yuborgansiz!",
            reply_markup=kb, parse_mode="HTML"
        )
        return

    struct, corr = json.loads(s_json), json.loads(a_json)
    mc_set = {str(it["q"]) for it in struct if it["type"] in ("abcd", "abcdef")}
    mc_k = [k for k in corr.keys() if k in mc_set]
    wr_k = [k for k in corr.keys() if k not in mc_set]

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET active_test_code = ? WHERE user_id = ?", (code, user_id))
    conn.commit()
    conn.close()
    await state.set_state(SolveTestState.waiting_for_answers)
    await state.update_data(active_test_code=code)

    if not wr_k:
        msg = (
            f"📘 <b>{code}-sonli test ({subject})</b>\n"
            f"📊 Savollar soni: <b>{len(mc_k)} ta</b>\n\n"
            f"✍️ <b>Endi javoblaringizni joy tashlamasdan ketma-ket yozib yuboring:</b>\n"
            f"Masalan: <code>adbcdbd...</code>"
        )
    else:
        msg = (
            f"📘 <b>{code}-sonli test ({subject})</b>\n"
            f"📊 Savollar: <b>{len(mc_k)} ta variantli + {len(wr_k)} ta yozma</b>\n\n"
            f"✍️ <b>Endi javoblaringizni birorta joy tashlamasdan ulab yozib yuboring:</b>\n"
            f"Masalan: <code>adbcdbd26189287senatordeputat</code>"
        )
    await target_message.answer(msg, parse_mode="HTML")

@dp.callback_query(F.data.startswith("view_res_"))
async def handle_view_result(call: CallbackQuery):
    code = call.data.replace("view_res_", "")
    await call.message.answer(
        build_personal_report(code, call.from_user.id), parse_mode="HTML"
    )
    await call.answer()

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not await check_subscriptions(message.from_user.id):
        await message.answer(
            "❗️ <b>Botdan foydalanish uchun 2 ta kanalga obuna bo'ling:</b>",
            reply_markup=get_subscription_keyboard("check_sub_start"),
            parse_mode="HTML"
        )
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT full_name FROM users WHERE user_id = ?", (message.from_user.id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        await state.set_state(RegisterState.waiting_for_name)
        await message.answer(
            "✍️ Iltimos, ism va familiyangizni kiriting:", parse_mode="HTML"
        )
        return
    await message.answer(
        f"👤 Hurmatli <b>{row[0]}</b>\n"
        f"❗️ Test ishlash uchun <b>test kodini</b> yuboring.",
        parse_mode="HTML"
    )

@dp.message(RegisterState.waiting_for_name)
async def process_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 3:
        await message.answer("❗️ Ism va familiyangizni to'liq kiriting:")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    sql_u = (
        "INSERT INTO users (user_id, full_name, joined_at) "
        "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET full_name = ?"
    )
    c.execute(sql_u, (message.from_user.id, name, datetime.now().isoformat(), name))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(
        f"✅ Rahmat, <b>{name}</b>!\n"
        f"Endi test ishlash uchun <b>test kodini</b> yuboring.",
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("check_sub_"))
async def handle_subscription_check(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not await check_subscriptions(uid):
        await call.answer("❌ Kanalga obuna bo'lmadingiz!", show_alert=True)
        return
    await call.answer("✅ Obuna tasdiqlandi!")
    act = call.data.replace("check_sub_", "")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT full_name FROM users WHERE user_id = ?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        await state.set_state(RegisterState.waiting_for_name)
        await call.message.answer("✍️ Ism va familiyangizni kiriting:")
        return
    if act == "start":
        await call.message.answer(
            f"✅ Hurmatli <b>{row[0]}</b>, test kodini yuboring.",
            parse_mode="HTML"
        )
    elif act.startswith("code_"):
        await process_code_for_user(
            uid, row[0], act.replace("code_", ""), call.message, state
        )

async def save_and_evaluate_student_answer(
    message: Message, state: FSMContext, user_id: int,
    full_name: str, test_code: str, raw_text: str
):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT scoring_type, is_timed, structure_json, answers_json, status "
        "FROM tests WHERE code = ?",
        (test_code,)
    )
    t_row = c.fetchone()
    if not t_row or t_row[4] == "finished":
        conn.close()
        await state.clear()
        await message.answer("⌛️ Test topilmadi yoki yakunlangan!")
        return
    scoring_type, is_timed, s_json, a_json, _ = t_row
    c.execute(
        "SELECT id FROM submissions WHERE test_code = ? AND user_id = ?",
        (test_code, user_id)
    )
    if c.fetchone():
        conn.close()
        await state.clear()
        await message.answer("❗️ Siz bu testga javob yuborgansiz!")
        return

    struct, corr = json.loads(s_json), json.loads(a_json)
    u_ans, err = parse_student_answers(raw_text, struct, corr)
    if u_ans is None:
        conn.close()
        await message.answer(err, parse_mode="HTML")
        return

    raw_c = sum(1 for k, cv in corr.items() if is_answer_correct(u_ans.get(k, ""), cv))
    pct_val = round((raw_c / len(corr)) * 100, 1) if corr else 0.0
    init_grade = get_certificate_grade(pct_val)
    sql_sub = (
        "INSERT INTO submissions "
        "(test_code, user_id, full_name, user_answers_json, "
        "raw_score, max_score, rasch_score, grade, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)"
    )
    c.execute(
        sql_sub,
        (
            test_code, user_id, full_name, json.dumps(u_ans),
            raw_c, len(corr), init_grade, datetime.now().isoformat()
        )
    )
    c.execute("UPDATE users SET active_test_code = '' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await state.clear()

    if scoring_type == "rasch":
        calculate_rasch_scores(test_code)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📊 Natijani ko'rish", callback_data=f"view_res_{test_code}"
        )]
    ])
    await message.answer(
        "✅ <b>Javobingiz qabul qilindi va hisoblandi!</b>",
        reply_markup=kb, parse_mode="HTML"
    )
    await message.answer(build_personal_report(test_code, user_id), parse_mode="HTML")

@dp.message(SolveTestState.waiting_for_answers, F.text)
async def handle_student_answers(message: Message, state: FSMContext):
    txt, uid = message.text.strip(), message.from_user.id
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code FROM tests WHERE code = ?", (txt,))
    is_code = c.fetchone() is not None
    c.execute(
        "SELECT full_name, active_test_code FROM users WHERE user_id = ?", (uid,)
    )
    u_row = c.fetchone()
    conn.close()
    fname = u_row[0] if u_row and u_row[0] else f"User {uid}"
    data = await state.get_data()
    t_code = data.get("active_test_code") or (
        u_row[1] if u_row and len(u_row) > 1 else ""
    )
    if is_code and len(txt) <= 5:
        await process_code_for_user(uid, fname, txt, message, state)
        return
    await save_and_evaluate_student_answer(message, state, uid, fname, t_code, txt)

@dp.message(F.text)
async def handle_test_code(message: Message, state: FSMContext):
    txt, uid = message.text.strip(), message.from_user.id
    if not await check_subscriptions(uid):
        await message.answer(
            "❗️ <b>Avval 2 ta kanalimizga obuna bo'ling:</b>",
            reply_markup=get_subscription_keyboard(f"check_sub_code_{txt}"),
            parse_mode="HTML"
        )
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT full_name, active_test_code FROM users WHERE user_id = ?", (uid,)
    )
    row = c.fetchone()
    c.execute("SELECT code FROM tests WHERE code = ?", (txt,))
    is_code = c.fetchone() is not None
    conn.close()
    if not row or not row[0]:
        await state.set_state(RegisterState.waiting_for_name)
        await message.answer("✍️ Ism va familiyangizni kiriting:")
        return
    if not is_code and len(row) > 1 and row[1]:
        await save_and_evaluate_student_answer(
            message, state, uid, row[0], row[1], txt
        )
        return
    await process_code_for_user(uid, row[0], txt, message, state)

async def auto_timer_loop():
    while True:
        try:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute(
                "SELECT code, end_time FROM tests "
                "WHERE is_timed = 1 AND status = 'active'"
            )
            rows = c.fetchall()
            conn.close()
            now = datetime.now()
            for code, et in rows:
                if now >= datetime.strptime(et, "%d.%m.%Y %H:%M"):
                    await finalize_test(code)
        except Exception:
            pass
        await asyncio.sleep(30)

async def main():
    init_db()
    asyncio.create_task(auto_timer_loop())
    print("✅ Bot muvaffaqiyatli ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())