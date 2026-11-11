import asyncio
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
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
AUTHOR_NAME = "Abdulvohidov Shahrizod"

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

# O'zbekiston vaqti uchun TZ (UTC+5)
UZ_TZ = timezone(timedelta(hours=5))

def get_uz_now() -> datetime:
    """Server qayerda joylashganidan qat'i nazar, aniq O'zbekiston vaqtini qaytaradi"""
    return datetime.now(UZ_TZ).replace(tzinfo=None)

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
        [InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["url"])]
        for ch in REQUIRED_CHANNELS
    ]
    btns.append([InlineKeyboardButton(text="✅ Obunani tekshirish 🔄", callback_data=cb)])
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
# 3. JAVOB TEKSHIRISH VA BALL HISOBLASH
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

def format_score_num(val: float) -> str:
    if val is None:
        return "0"
    f_val = round(float(val), 1)
    return str(int(f_val)) if f_val.is_integer() else f"{f_val:.1f}"

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

def split_concatenated_written(rem_str: str, expected_count: int) -> list:
    """
    Bo'shliqsiz ulab yozilgan yozma javoblarni (masalan: 1010552421/226.520462436103152161)
    aniq expected_count (20 ta) bo'lakka xatosiz ajratib beradi.
    """
    s = re.sub(r"\s+", "", rem_str)
    atoms = re.findall(r"\d+/\d+|\d+[.,]\d+|\d|[a-zA-Zʻʼ’‘']+", s, flags=re.IGNORECASE)
    m = len(atoms)
    if m < expected_count:
        return None
    if m == expected_count:
        return atoms

    memo = {}

    def score_tok(tok: str) -> int:
        if "/" in tok or "." in tok or "," in tok:
            return 35
        if tok.isalpha():
            return 25
        if tok.isdigit():
            if len(tok) > 1 and tok.startswith("0"):
                return -1000
            val = int(tok)
            if 1 <= val <= 30:
                return 25
            elif 31 <= val <= 100:
                return 12
            elif 101 <= val <= 999:
                return 2
            elif 1000 <= val <= 2030:
                return 5
            return -50
        return -20

    def dp_solve(idx: int, k_left: int):
        if idx == m and k_left == 0:
            return 0, []
        if idx >= m or k_left <= 0 or (m - idx) < k_left:
            return -10**9, None
        state_key = (idx, k_left)
        if state_key in memo:
            return memo[state_key]

        best_sc = -10**9
        best_res = None
        max_take = min(4, m - idx - (k_left - 1))

        for take in range(1, max_take + 1):
            parts = atoms[idx:idx + take]
            if take > 1 and not all(p.isdigit() and len(p) == 1 for p in parts):
                break
            cand = "".join(parts)
            sc = score_tok(cand)
            if sc < -500:
                continue
            sub_sc, sub_res = dp_solve(idx + take, k_left - 1)
            if sub_res is not None and (sc + sub_sc) > best_sc:
                best_sc = sc + sub_sc
                best_res = [cand] + sub_res

        memo[state_key] = (best_sc, best_res)
        return memo[state_key]

    _, res = dp_solve(0, expected_count)
    return res

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
                f"⚠️ Testda {len(mc_keys)} ta variantli savol bor, "
                f"siz esa {len(clean_letters)} ta harf kiritdingiz! 🔄 Qaytadan yuboring:"
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
            f"⚠️ Boshida {n_mc} ta variant harfi bo'lishi kerak, "
            f"siz {len(mc_collected)} ta harf kiritdingiz! 🔄"
        )

    for k, letter in zip(mc_keys, mc_collected):
        answers_dict[k] = letter

    rem = raw_text[end_mc_idx:].strip()
    if not rem:
        return None, "⚠️ Variantlardan keyin yozma savollar javobini ham kiriting! ✍️"

    spaced_tokens = [t for t in re.split(r"[\s;]+", rem) if t]
    if len(spaced_tokens) == len(written_keys):
        for k, tok in zip(written_keys, spaced_tokens):
            answers_dict[k] = tok
        return answers_dict, structure

    smart_tokens = split_concatenated_written(rem, len(written_keys))
    if smart_tokens and len(smart_tokens) == len(written_keys):
        for k, tok in zip(written_keys, smart_tokens):
            answers_dict[k] = tok
        return answers_dict, structure

    rem_clean = re.sub(r"\s+", "", rem)
    w_count = len(written_keys)
    if len(rem_clean) < w_count:
        return None, (
            f"⚠️ Yozma javoblar qismi juda qisqa. "
            f"Kamida {w_count} ta javob kiriting: ✍️"
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
                f"⚠️ Testda <b>{len(mc_keys)} ta</b> savol bor, "
                f"siz esa <b>{len(clean_letters)} ta</b> harf yubordingiz! 🧐\n"
                f"✍️ Iltimos, to'liq ketma-ketlikda yuboring (masalan: <code>adbcdbd...</code>):"
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
            f"⚠️ Dastlabki <b>{n_mc} ta</b> savol variantli (harfli) "
            f"bo'lishi kerak! Siz {len(mc_collected)} ta harf yozdingiz. 🧐\n"
            f"✍️ Masalan: <code>adbcdbd26189287senatordeputat</code>"
        )

    for k, letter in zip(mc_keys, mc_collected):
        user_answers[k] = letter

    rem = raw_text[end_mc_idx:].strip()
    if not rem:
        return None, (
            f"⚠️ Variantlar ketidan <b>{len(written_keys)} ta yozma savol</b> "
            f"javobini ham joy tashlamasdan yozib yuboring! ✍️"
        )

    spaced_tokens = [t for t in re.split(r"[\s;]+", rem) if t]
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

def calculate_custom_score(structure_json: str, answers_json: str, user_answers_json: str):
    """
    1-35 savollar: har biri 1 balldan.
    36-45 yozma savollar (written_ab): a va b qismlar.
      - Ikkalasini ham topsa: 1 ball.
      - Faqat bittasini (a yoki b) topsa: 0.5 ball.
    """
    struct = json.loads(structure_json)
    corr = json.loads(answers_json)
    u_ans = json.loads(user_answers_json)

    score = 0.0
    max_possible = 0.0

    for item in struct:
        q_num = str(item["q"])
        q_type = item["type"]
        if q_type in ("abcd", "abcdef", "written_single"):
            max_possible += 1.0
            if is_answer_correct(u_ans.get(q_num, ""), corr.get(q_num, "")):
                score += 1.0
        elif q_type == "written_ab":
            max_possible += 1.0
            k_a, k_b = f"{q_num}_a", f"{q_num}_b"
            ok_a = is_answer_correct(u_ans.get(k_a, ""), corr.get(k_a, ""))
            ok_b = is_answer_correct(u_ans.get(k_b, ""), corr.get(k_b, ""))
            if ok_a and ok_b:
                score += 1.0
            elif ok_a or ok_b:
                score += 0.5

    return round(score, 2), round(max_possible, 2)

def calculate_rasch_scores(test_code: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT structure_json, answers_json FROM tests WHERE code = ?", (test_code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    s_json, a_json = row[0], row[1]
    struct = json.loads(s_json)
    correct_answers = json.loads(a_json)

    c.execute(
        "SELECT id, user_id, full_name, user_answers_json "
        "FROM submissions WHERE test_code = ?",
        (test_code,)
    )
    subs = c.fetchall()
    if not subs:
        conn.close()
        return

    # 45 ta asosiy savol bo'yicha ball matritsasi (1-35: 0 yoki 1; 36-45: 0, 0.5 yoki 1)
    q_nums = [str(it["q"]) for it in struct]
    n_users = len(subs)
    q_corr_sum = {qn: 0.0 for qn in q_nums}
    u_mat = {}
    u_raw = {}
    max_sc = float(len(q_nums))

    for sid, uid, fname, u_json in subs:
        u_ans = json.loads(u_json)
        u_mat[sid] = {}
        sc_total = 0.0
        for it in struct:
            qn = str(it["q"])
            qt = it["type"]
            if qt == "written_ab":
                ok_a = is_answer_correct(u_ans.get(f"{qn}_a", ""), correct_answers.get(f"{qn}_a", ""))
                ok_b = is_answer_correct(u_ans.get(f"{qn}_b", ""), correct_answers.get(f"{qn}_b", ""))
                val = 1.0 if (ok_a and ok_b) else (0.5 if (ok_a or ok_b) else 0.0)
            else:
                val = 1.0 if is_answer_correct(u_ans.get(qn, ""), correct_answers.get(qn, "")) else 0.0
            u_mat[sid][qn] = val
            q_corr_sum[qn] += val
            sc_total += val
        u_raw[sid] = round(sc_total, 2)

    q_weights = {}
    for qn in q_nums:
        p = (q_corr_sum[qn] + 0.5) / (n_users + 1.0)
        logit = math.log((1.0 - p) / p)
        q_weights[qn] = 1.5 + max(-1.0, min(2.0, logit * 0.5))

    max_w = sum(q_weights.values())
    for sid, _, _, _ in subs:
        w_sum = sum(u_mat[sid][qn] * q_weights[qn] for qn in q_nums)
        raw_c = u_raw[sid]
        r_score = round((w_sum / max_w) * 95.0, 2) if max_w > 0 else 0.0
        if raw_c == max_sc and max_sc > 0:
            r_score = 93.0
        grade = get_certificate_grade(r_score)
        c.execute(
            "UPDATE submissions "
            "SET raw_score=?, max_score=?, rasch_score=?, grade=? WHERE id=?",
            (raw_c, max_sc, r_score, grade, sid)
        )
    conn.commit()
    conn.close()

# ==========================================
# 4. PDF GENERATOR (Namunadagi dizayn asosida)
# ==========================================
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
        "SELECT full_name, raw_score, max_score, rasch_score, grade, submitted_at "
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
        rightMargin=20, leftMargin=20, topMargin=25, bottomMargin=25
    )
    styles = getSampleStyleSheet()
    t_style = ParagraphStyle(
        "T", parent=styles["Heading1"], fontSize=15, alignment=1, spaceAfter=6
    )
    s_style = ParagraphStyle(
        "S", parent=styles["Normal"], fontSize=10, alignment=1, spaceAfter=14
    )

    elements = [
        Paragraph("<b>TEST NATIJALARI</b>", t_style),
        Paragraph(
            f"<b>Test nomi:</b> {subject} | "
            f"<b>Test kodi:</b> {test_code} | "
            f"<b>Tuzuvchi:</b> {AUTHOR_NAME}<br/>"
            f"<b>Kanallar:</b> @Qonun_Eksperti | @Huquq_sertifikati_uz | "
            f"<b>Qatnashuvchilar:</b> {len(rows)} ta",
            s_style
        )
    ]

    # Keraksiz ustunlar (Ability, Majbur, Asosiy 1, Asosiy 2) olib tashlangan toza jadval
    data = [["#", "Ismi familiyasi", "Ball", "Foiz", "Daraja", "To'g'ri", "Sana", "Vaqt"]]
    if not rows:
        data.append(["1", "Ishtirokchilar yo'q", "0", "0.0%", "—", "0", "—", "—"])

    for idx, (fn, raw, mx, rs, grd, sub_at) in enumerate(rows, 1):
        cn = re.sub(r"[^\x00-\x7Fʻʼ’‘\w\s\.\-]", "", fn) or f"Ishtirokchi #{idx}"
        pct = round((float(raw) / float(mx)) * 100, 1) if mx else 0.0
        ball_val = f"{rs:.2f}" if scoring_type == "rasch" else format_score_num(raw)
        grd_val = grd if grd else get_certificate_grade(float(rs) if scoring_type == "rasch" else pct)
        try:
            dt_obj = datetime.fromisoformat(sub_at)
            d_str = dt_obj.strftime("%d.%m.%y")
            t_str = dt_obj.strftime("%H:%M")
        except Exception:
            now_dt = get_uz_now()
            d_str = now_dt.strftime("%d.%m.%y")
            t_str = now_dt.strftime("%H:%M")

        data.append([
            str(idx),
            cn[:32],
            ball_val,
            f"{pct}%",
            grd_val,
            format_score_num(raw),
            d_str,
            t_str
        ])

    col_w = [30, 195, 55, 55, 50, 50, 55, 45]
    tbl = Table(data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0284c7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ALIGN", (1, 1), (1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
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
        "SELECT subject, scoring_type, structure_json, answers_json FROM tests WHERE code = ?",
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
        return "🔍 Natija topilmadi."

    subject, scoring_type, s_json, ans_json = t_row
    u_ans_json, raw_score, max_score, rasch_score, grade = s_row
    struct = json.loads(s_json)
    correct_answers = json.loads(ans_json)
    user_answers = json.loads(u_ans_json)
    pct = round((raw_score / max_score) * 100, 1) if max_score else 0

    lines = [
        f"🎯 <b>Test №{test_code} bo'yicha shaxsiy natijangiz:</b>\n",
        f"📚 <b>Fan:</b> {subject}",
        f"👤 <b>Tuzuvchi:</b> {AUTHOR_NAME}",
        f"📢 <b>Kanallar:</b> @Qonun_Eksperti | @Huquq_sertifikati_uz",
        f"✅ <b>To'g'ri javoblar:</b> {format_score_num(raw_score)} / {format_score_num(max_score)} ta ({pct}%)"
    ]
    if scoring_type == "rasch":
        lines.append(f"📈 <b>Rash bali:</b> {rasch_score} ball")
        lines.append(f"🏆 <b>Daraja:</b> {grade}")
    else:
        lines.append(f"🏆 <b>Daraja:</b> {get_certificate_grade(pct)}")

    lines.append("\n🔎 <b>Savollar tahlili:</b>")
    mc_items = []
    ab_lines = []
    for it in struct:
        qn = str(it["q"])
        qt = it["type"]
        if qt == "written_ab" or (f"{qn}_a" in correct_answers and f"{qn}_b" in correct_answers):
            ka, kb = f"{qn}_a", f"{qn}_b"
            ua, ub = user_answers.get(ka, "—"), user_answers.get(kb, "—")
            ca, cb = correct_answers.get(ka, ""), correct_answers.get(kb, "")
            oka = is_answer_correct(ua, ca)
            okb = is_answer_correct(ub, cb)
            pts = "1 ball" if (oka and okb) else ("0.5 ball" if (oka or okb) else "0 ball")
            ab_lines.append(
                f"<b>{qn}:</b> a) {ua} {'✅' if oka else '❌'} | b) {ub} {'✅' if okb else '❌'} ({pts})"
            )
        else:
            uv = user_answers.get(qn, "—")
            cv = correct_answers.get(qn, "")
            ok = is_answer_correct(uv, cv)
            mc_items.append(f"<b>{qn}:</b> {uv} {'✅' if ok else '❌'}")

    for i in range(0, len(mc_items), 3):
        lines.append("  |  ".join(mc_items[i:i + 3]))
    if ab_lines:
        lines.append("\n✍️ <b>Yozma savollar (a/b):</b>")
        lines.extend(ab_lines)
    return "\n".join(lines)

# ==========================================
# 5. ADMIN PANEL (FSM)
# ==========================================
class CreateTestState(StatesGroup):
    code = State()
    subject = State()
    scoring_type = State()
    structure_choice = State()
    custom_structure = State()
    time_limit = State()
    answers = State()

class RegisterState(StatesGroup):
    waiting_for_name = State()

class SolveTestState(StatesGroup):
    waiting_for_answers = State()

@dp.message(Command("admin"))
async def admin_menu(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi test yaratish 🛠", callback_data="admin_new_test")],
        [InlineKeyboardButton(
            text="🏁 Aktiv testni yakunlash (PDF chiqarish) 📄",
            callback_data="admin_finish_list"
        )]
    ])
    await message.answer(
        "👨‍💻 <b>Admin panelga xush kelibsiz!</b> 🚀\nKerakli bo'limni tanlang:",
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
        f"1️⃣ <b>Test kodini kiriting</b> 🔢\n"
        f"(avtomatik <code>{nxt}</code> kodi uchun <b>auto</b> deb yozing):",
        parse_mode="HTML"
    )
    await call.answer()

@dp.message(CreateTestState.code, F.text)
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
        await message.answer("⚠️ Bu kod bazada band! 🚫 Boshqa kod yozing:")
        return
    conn.close()
    await state.update_data(code=code_in)
    await state.set_state(CreateTestState.subject)
    await message.answer("2️⃣ <b>Fan yoki test nomini yozing:</b> 📚", parse_mode="HTML")

@dp.message(CreateTestState.subject, F.text)
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
        "3️⃣ <b>Tekshirish usulini tanlang:</b> ⚙️",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("stype_"))
async def process_scoring_type(call: CallbackQuery, state: FSMContext):
    await state.update_data(scoring_type=call.data.replace("stype_", ""))
    await state.set_state(CreateTestState.structure_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📘 Milliy sertifikat (1-32 A-D, 33-35 A-F, 36-45 yozma a/b)",
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
        "4️⃣ <b>Test tuzilishini tanlang:</b> 🧩",
        reply_markup=kb, parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("struct_"))
async def process_structure_choice(call: CallbackQuery, state: FSMContext):
    ch = call.data.replace("struct_", "")
    if ch == "ms45":
        struct = (
            [{"q": str(q), "type": "abcd"} for q in range(1, 33)] +
            [{"q": str(q), "type": "abcdef"} for q in range(33, 36)] +
            [{"q": str(q), "type": "written_ab"} for q in range(36, 46)]
        )
        await state.update_data(structure=struct, total_q=45)
        await ask_time_limit(call.message, state)
    elif ch == "abcd":
        await state.update_data(struct_mode="abcd")
        await state.set_state(CreateTestState.custom_structure)
        await call.message.answer(
            "🔢 Nechta savol? Faqat raqam yozing (masalan: <code>30</code>):",
            parse_mode="HTML"
        )
    else:
        await state.update_data(struct_mode="mixed")
        await state.set_state(CreateTestState.custom_structure)
        await call.message.answer(
            "🔢 Variantli va yozma savollar sonini yozing (masalan: <code>35 10</code>):",
            parse_mode="HTML"
        )
    await call.answer()

@dp.message(CreateTestState.custom_structure, F.text)
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
        await message.answer("⚠️ Xato format! 🔄 Qaytadan raqam kiriting:")
        return
    await state.update_data(structure=struct, total_q=len(struct))
    await ask_time_limit(message, state)

async def ask_time_limit(message: Message, state: FSMContext):
    await state.set_state(CreateTestState.time_limit)
    await message.answer(
        "5️⃣ <b>Test vaqtini belgilang:</b> ⏰\n"
        "• Vaqtsiz uchun: <code>0</code>\n"
        "• Vaqtli uchun faqat soatni yozing (masalan: <code>18:00-20:00</code>):",
        parse_mode="HTML"
    )

@dp.message(CreateTestState.time_limit, F.text)
async def process_time_limit(message: Message, state: FSMContext):
    txt = message.text.strip()
    if txt == "0":
        await state.update_data(is_timed=0, start_time="", end_time="")
    else:
        # 18:00-20:00 yoki 18:00 - 20:00 formatini tekshirish
        m_short = re.match(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$", txt)
        if m_short:
            try:
                now = get_uz_now()
                t1 = datetime.strptime(m_short.group(1), "%H:%M").time()
                t2 = datetime.strptime(m_short.group(2), "%H:%M").time()
                dt_start = datetime.combine(now.date(), t1)
                dt_end = datetime.combine(now.date(), t2)
                if dt_end <= dt_start:
                    dt_end += timedelta(days=1)
                s = dt_start.strftime("%d.%m.%Y %H:%M")
                e = dt_end.strftime("%d.%m.%Y %H:%M")
                await state.update_data(is_timed=1, start_time=s, end_time=e)
            except Exception:
                await message.answer(
                    "⚠️ Vaqt formati xato! ⏳ Masalan: <code>18:00-20:00</code> yoki <code>0</code> deb yozing:",
                    parse_mode="HTML"
                )
                return
        else:
            # Agar admin to'liq sana bilan yozsa ham qabul qiladi
            try:
                s, e = [x.strip() for x in txt.split("-")]
                datetime.strptime(s, "%d.%m.%Y %H:%M")
                datetime.strptime(e, "%d.%m.%Y %H:%M")
                await state.update_data(is_timed=1, start_time=s, end_time=e)
            except Exception:
                await message.answer(
                    "⚠️ Vaqt formati xato! ⏳ Masalan: <code>18:00-20:00</code> yoki <code>0</code> deb yozing:",
                    parse_mode="HTML"
                )
                return

    await state.set_state(CreateTestState.answers)
    await message.answer(
        "6️⃣ <b>To'g'ri javoblar kalitini yuboring:</b> 🔑\n"
        "(masalan: <code>DACACBAABBA...</code>):",
        parse_mode="HTML"
    )

@dp.message(CreateTestState.answers, F.text)
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

    time_info = (
        f"⏰ <b>Vaqt:</b> {data['start_time']} — {data['end_time']}\n"
        if data["is_timed"] else "⏰ <b>Vaqt:</b> Vaqtsiz (Darhol natija)\n"
    )
    await message.answer(
        f"🎉 <b>Test muvaffaqiyatli yaratildi!</b> ✅\n\n"
        f"🔢 <b>Test kodi:</b> <code>{data['code']}</code>\n"
        f"📚 <b>Fan:</b> {data['subject']}\n"
        f"{time_info}"
        f"👨‍💻 <b>Tuzuvchi:</b> {AUTHOR_NAME}\n"
        f"🚀 O'quvchilar endi test ishlashlari mumkin!",
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
                caption=f"🏁 <b>Test №{test_code} yakunlandi!</b> 📊",
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
                uid,
                f"⌛️ <b>Test №{test_code} yakunlandi!</b>\n\n" + build_personal_report(test_code, uid),
                parse_mode="HTML"
            )
            await bot.send_document(
                uid, FSInputFile(pdf_path),
                caption=f"📄 Test №{test_code} umumiy reyting jadvali 🏆"
            )
        except Exception:
            pass
    try:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
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
        await call.message.answer("📭 Hozirda aktiv testlar yo'q.")
        await call.answer()
        return
    btns = [
        [InlineKeyboardButton(
            text=f"🏁 Yakunlash: №{c_code} ({subj})",
            callback_data=f"finish_{c_code}"
        )]
        for c_code, subj in rows
    ]
    await call.message.answer(
        "📋 Yakunlamoqchi bo'lgan testingizni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("finish_"))
async def admin_finish_confirm(call: CallbackQuery):
    code = call.data.replace("finish_", "")
    await finalize_test(code)
    await call.message.answer(f"🏁 Test №{code} muvaffaqiyatli yakunlandi! ✅")
    await call.answer()

# ==========================================
# 6. O'QUVCHI QISMI VA JAVOB QABUL QILISH
# ==========================================
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
        await target_message.answer("❌ Bunday kodli test topilmadi! 🔍 Tekshirib qaytadan yuboring.")
        return
    code, subject, scoring_type, is_timed, st_str, et_str, s_json, a_json, status = t_row
    c.execute(
        "SELECT id FROM submissions WHERE test_code = ? AND user_id = ?",
        (code, user_id)
    )
    submitted = c.fetchone() is not None
    conn.close()

    if status == "finished":
        await target_message.answer(
            f"⌛️ <b>{code}-sonli ({subject}) testning vaqti tugagan!</b>\n"
            f"🔒 Ushbu test yakunlangan, javob yuborish to'xtatilgan.",
            parse_mode="HTML"
        )
        return

    if is_timed:
        now = get_uz_now()
        if now < datetime.strptime(st_str, "%d.%m.%Y %H:%M"):
            await target_message.answer(
                f"⏳ <b>Test hali boshlanmadi!</b>\n🕒 Boshlanish vaqti (Toshkent vaqti): <code>{st_str}</code>",
                parse_mode="HTML"
            )
            return
        if now > datetime.strptime(et_str, "%d.%m.%Y %H:%M"):
            await finalize_test(code)
            await target_message.answer(
                f"⌛️ <b>{code}-sonli ({subject}) testning vaqti tugagan!</b>\n"
                f"🔒 Ushbu test yakunlangan, javob yuborish to'xtatilgan.",
                parse_mode="HTML"
            )
            return

    if submitted:
        if is_timed:
            await target_message.answer(
                f"✅ Siz <b>{code}-sonli</b> testga javob yuborgansiz!\n"
                f"⏳ Natijalar test vaqti tugagach (<code>{et_str}</code>) avtomatik yuboriladi.",
                parse_mode="HTML"
            )
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📊 Natijani ko'rish 🎯", callback_data=f"view_res_{code}"
                )]
            ])
            await target_message.answer(
                f"✅ Siz <b>{code}-sonli</b> testga allaqachon javob yuborgansiz! 🎉",
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
            f"📘 <b>{code}-sonli test ({subject})</b> 🚀\n"
            f"👤 <b>Tuzuvchi:</b> {AUTHOR_NAME}\n"
            f"📊 Savollar soni: <b>{len(mc_k)} ta</b>\n\n"
            f"✍️ <b>Endi javoblaringizni joy tashlamasdan ketma-ket yozib yuboring:</b>\n"
            f"💡 Masalan: <code>adbcdbd...</code>"
        )
    else:
        msg = (
            f"📘 <b>{code}-sonli test ({subject})</b> 🚀\n"
            f"👤 <b>Tuzuvchi:</b> {AUTHOR_NAME}\n"
            f"📊 Savollar: <b>{len(mc_k)} ta variantli + yozma savollar</b>\n\n"
            f"✍️ <b>Endi javoblaringizni birorta joy tashlamasdan ulab yozib yuboring:</b>\n"
            f"💡 Masalan: <code>adbcdbd26189287senatordeputat</code>"
        )
    await target_message.answer(msg, parse_mode="HTML")

@dp.callback_query(F.data.startswith("view_res_"))
async def handle_view_result(call: CallbackQuery):
    code = call.data.replace("view_res_", "")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT status, subject, is_timed, end_time FROM tests WHERE code = ?", (code,))
    t_row = c.fetchone()
    conn.close()
    if t_row and t_row[2] == 1 and t_row[0] != "finished":
        await call.message.answer(
            f"⏳ <b>{code}-sonli ({t_row[1]}) test davom etmoqda!</b>\n"
            f"🔒 Javoblar va natijalar test vaqti tugagach (<code>{t_row[3]}</code>) avtomatik tashlanadi.",
            parse_mode="HTML"
        )
        await call.answer()
        return
    await call.message.answer(
        build_personal_report(code, call.from_user.id), parse_mode="HTML"
    )
    await call.answer()

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not await check_subscriptions(message.from_user.id):
        await message.answer(
            "👋 <b>Assalomu alaykum!</b>\n"
            "❗️ <b>Botdan foydalanish uchun quyidagi 2 ta kanalga obuna bo'ling:</b> 👇",
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
            "👋 <b>Xush kelibsiz!</b>\n✍️ Iltimos, ism va familiyangizni to'liq kiriting:",
            parse_mode="HTML"
        )
        return
    await message.answer(
        f"🎓 Hurmatli <b>{row[0]}</b>!\n"
        f"🔢 Test ishlash uchun <b>test kodini</b> yuboring: 👇",
        parse_mode="HTML"
    )

@dp.message(RegisterState.waiting_for_name, F.text)
async def process_user_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 3:
        await message.answer("⚠️ Iltimos, ism va familiyangizni to'liqroq kiriting: ✍️")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    sql_u = (
        "INSERT INTO users (user_id, full_name, joined_at) "
        "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET full_name = ?"
    )
    c.execute(sql_u, (message.from_user.id, name, get_uz_now().isoformat(), name))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(
        f"🎉 Rahmat, <b>{name}</b>! Ro'yxatdan muvaffaqiyatli o'tdingiz ✅\n"
        f"🔢 Endi test ishlash uchun <b>test kodini</b> yuboring: 👇",
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("check_sub_"))
async def handle_subscription_check(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not await check_subscriptions(uid):
        await call.answer("❌ Siz hali kanallarga to'liq obuna bo'lmadingiz!", show_alert=True)
        return
    await call.answer("✅ Obuna tasdiqlandi! 🎉")
    act = call.data.replace("check_sub_", "")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT full_name FROM users WHERE user_id = ?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        await state.set_state(RegisterState.waiting_for_name)
        await call.message.answer("✍️ Iltimos, ism va familiyangizni to'liq kiriting:")
        return
    if act == "start":
        await call.message.answer(
            f"✅ Hurmatli <b>{row[0]}</b>, test kodini yuborishingiz mumkin: 🔢👇",
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
        "SELECT scoring_type, is_timed, end_time, structure_json, answers_json, status "
        "FROM tests WHERE code = ?",
        (test_code,)
    )
    t_row = c.fetchone()
    if not t_row or t_row[5] == "finished":
        conn.close()
        await state.clear()
        await message.answer("⌛️ Ushbu testning vaqti tugagan va u yakunlangan! 🔒")
        return
    scoring_type, is_timed, et_str, s_json, a_json, _ = t_row
    c.execute(
        "SELECT id FROM submissions WHERE test_code = ? AND user_id = ?",
        (test_code, user_id)
    )
    if c.fetchone():
        conn.close()
        await state.clear()
        await message.answer("⚠️ Siz bu testga allaqachon javob yuborgansiz! ✅")
        return

    struct, corr = json.loads(s_json), json.loads(a_json)
    u_ans, err = parse_student_answers(raw_text, struct, corr)
    if u_ans is None:
        conn.close()
        await message.answer(err, parse_mode="HTML")
        return

    raw_c, max_sc = calculate_custom_score(s_json, a_json, json.dumps(u_ans))
    pct_val = round((raw_c / max_sc) * 100, 1) if max_sc else 0.0
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
            raw_c, max_sc, init_grade, get_uz_now().isoformat()
        )
    )
    c.execute("UPDATE users SET active_test_code = '' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await state.clear()

    if scoring_type == "rasch":
        calculate_rasch_scores(test_code)

    if is_timed == 1:
        await message.answer(
            f"✅ <b>Javobingiz qabul qilindi!</b> 📥\n\n"
            f"⏳ Ushbu test vaqtli rejimda o'tkazilmoqda. Test vaqti tugashi bilan "
            f"(<code>{et_str}</code>) javoblar tahlili va umumiy natijalar avtomatik tarzda tashlanadi!",
            parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📊 Natijani ko'rish 🎯", callback_data=f"view_res_{test_code}"
            )]
        ])
        await message.answer(
            "🎉 <b>Javobingiz qabul qilindi va hisoblandi!</b> ✅",
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

@dp.message(StateFilter(default_state), F.text)
async def handle_test_code(message: Message, state: FSMContext):
    txt, uid = message.text.strip(), message.from_user.id
    if not await check_subscriptions(uid):
        await message.answer(
            "❗️ <b>Avval 2 ta kanalimizga obuna bo'ling:</b> 👇",
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
        await message.answer("✍️ Iltimos, ism va familiyangizni to'liq kiriting:")
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
            now = get_uz_now()
            for code, et in rows:
                if now >= datetime.strptime(et, "%d.%m.%Y %H:%M"):
                    await finalize_test(code)
        except Exception:
            pass
        await asyncio.sleep(30)

async def main():
    init_db()
    asyncio.create_task(auto_timer_loop())
    print("✅ Bot muvaffaqiyatli ishga tushdi (Toshkent vaqti bilan)!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
