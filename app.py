import os
import random
import re
import fitz  # PyMuPDF
import shutil
import hashlib
import uuid
import gc     # RAM ফ্রি করার জন্য
import time   # ব্যাকগ্রাউন্ড টাস্ক ডিলে করার জন্য
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, UploadFile, File, Form, Request, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from supabase import create_client, Client

# --- Supabase Configuration ---
SUPABASE_URL = "https://xfjurdncpizltrierozw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhmanVyZG5jcGl6bHRyaWVyb3p3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4Mjk5MjE0OCwiZXhwIjoyMDk4NTY4MTQ4fQ.xP1ZRgLIJLi7OQE5l0RoovJ6wZvFQ_Z5EVrM__Ujdtg" 
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="toolscraft-hub-super-secret-key-xyz-2026")

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
TEMPLATE_FILE = "template.pdf"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Helper Functions ---
def get_bdt_date():
    return (datetime.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d")

def get_bdt_time():
    return (datetime.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d %I:%M %p")

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def db_execute(builder, default=None, retries=3):
    for attempt in range(retries):
        try:
            return builder.execute()
        except Exception as e:
            if attempt == retries - 1:
                print("DB Query Error:", e)
                return default
            time.sleep(0.3)

@app.on_event("startup")
async def setup_default_accounts():
    accounts = [
        {"username": "admin", "password": "admin786", "role": "admin", "credits": 999999},
        {"username": "user200", "password": "user123", "role": "user", "credits": 200}
    ]
    for acc in accounts:
        try:
            res = db_execute(supabase.table("users").select("username").eq("username", acc["username"]))
            if res and not res.data:
                db_execute(supabase.table("users").insert({
                    "username": acc["username"], "password": hash_password(acc["password"]),
                    "role": acc["role"], "daily_credits": acc["credits"],
                    "credit_limit": acc["credits"], "used_credits": 0, "last_reset_date": get_bdt_date()
                }))
            elif res:
                db_execute(supabase.table("users").update({
                    "password": hash_password(acc["password"]), "role": acc["role"]
                }).eq("username", acc["username"]))
        except Exception as e:
            print("Setup error:", e)

def check_active_session(request: Request):
    username = request.session.get("username")
    token = request.session.get("session_token")
    if not username or not token: return False
    res = db_execute(supabase.table("users").select("session_token").eq("username", username))
    if res and res.data and res.data[0].get("session_token") == token: return True
    return False

def check_and_reset_credits(username):
    res = db_execute(supabase.table("users").select("daily_credits").eq("username", username))
    if res and res.data:
        return res.data[0].get("daily_credits", 0)
    return 0

# --- PDF Processing Logic ---
LEXEND_FONT_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "LexendDeca-Medium.ttf"))
if not os.path.exists(LEXEND_FONT_PATH):
    LEXEND_FONT_PATH = os.path.abspath("static/LexendDeca-Medium.ttf")

def insert_pdf_text(page, point, text, fontsize=7.0, font_style="normal", color=(0, 0, 0)):
    if font_style in ["title", "author"] and os.path.exists(LEXEND_FONT_PATH):
        try:
            page.insert_text(point, text, fontsize=fontsize, fontname="lexend", fontfile=LEXEND_FONT_PATH, color=color)
            return
        except Exception:
            pass
    page.insert_text(point, text, fontsize=fontsize, fontname="hebo" if font_style == "bold" else "helv", color=color)

def strip_old_cover_pages(doc):
    strong_cover_body_keywords = [
        "detected as ai",
        "score interpretation",
        "highlight legend",
        "important notice",
        "quick submit",
        "turnitin originality report",
        "ai writing overview",
        "integrity overview",
        "submission declaration"
    ]
    for _ in range(2):
        if len(doc) <= 1:
            break
        page = doc[0]
        rect = page.rect
        body_text_list = []
        for block in page.get_text("blocks"):
            # block format: (x0, y0, x1, y1, text, block_no, block_type)
            if len(block) >= 5 and block[1] >= 40 and block[3] <= rect.height - 40:
                body_text_list.append(str(block[4]))
        
        body_txt = " ".join(body_text_list).lower()
        full_txt = page.get_text().lower()

        is_cover = any(k in body_txt for k in strong_cover_body_keywords) or \
                   ("cover page" in full_txt and "page 1 of" in full_txt)
        
        if is_cover:
            doc.delete_page(0)
        else:
            break

def process_master_pdf(user_pdf_path, output_path, original_filename, ai_percentage, shared_id):
    user_doc = fitz.open(user_pdf_path)

    # Smart cover page stripping: Remove old cover/overview pages if uploaded file is a previously generated report
    strip_old_cover_pages(user_doc)

    # Words and Characters count from stripped user document pages
    actual_words, actual_chars = 0, 0
    for p in user_doc:
        text = p.get_text("text")
        actual_words += len(text.split())
        actual_chars += len(text)

    actual_pages_count = len(user_doc)
    
    new_size = f"{os.path.getsize(user_pdf_path) / 1024:.1f} KB"
    new_id = shared_id
    base_name = re.sub(r'(?i)ai\s*report', '', os.path.splitext(original_filename)[0].replace("_", " ")).strip()
    new_title = " ".join(base_name.split()[:5]) if base_name.split() else "Document"

    now = datetime.now(timezone.utc) + timedelta(hours=6)
    sub_time = now - timedelta(minutes=2) 
    
    def fmt_gmt6_date(dt):
        day_str = str(dt.day)
        hour_str = dt.strftime("%I").lstrip("0")
        return dt.strftime(f"%b {day_str}. %Y.  {hour_str}:%M %p GMT+6")

    sub_date_str = fmt_gmt6_date(sub_time)
    down_date_str = fmt_gmt6_date(now)

    template_doc = fitz.open(TEMPLATE_FILE)
    page1_text = template_doc[0].get_text()
    
    old_id_match = re.search(r"trn:oid:::\d:\d+", page1_text)
    old_id = old_id_match.group(0) if old_id_match else None
    old_title_match = re.search(r"([^\n]+)\s+Aa Aa", page1_text)
    old_title = old_title_match.group(1).strip() if old_title_match else "Fresh Template"
    old_fname_match = re.search(r"File Name\s+(.*?)\s+File Size", page1_text, re.DOTALL)
    old_fname_in_details = old_fname_match.group(1).strip() if old_fname_match else None
    old_pages_match = re.search(r"(\d+)\s+Pages", page1_text)
    old_pages_text = old_pages_match.group(0) if old_pages_match else None
    old_words_match = re.search(r"([\d,]+)\s+Words", page1_text)
    old_words_text = old_words_match.group(0) if old_words_match else None
    old_chars_match = re.search(r"([\d,]+)\s+Characters", page1_text)
    old_chars_text = old_chars_match.group(0) if old_chars_match else None
    old_sub_date_match = re.search(r"Submission Date\s+(.*?)\s+Download Date", page1_text, re.DOTALL)
    old_sub_date = old_sub_date_match.group(1).strip() if old_sub_date_match else None
    old_down_date_match = re.search(r"Download Date\s+(.*?)\s+File Name", page1_text, re.DOTALL)
    old_down_date = old_down_date_match.group(1).strip() if old_down_date_match else None

    page1 = template_doc[0]

    # 1. Main Title Replacement
    for inst in page1.search_for(old_title):
        page1.add_redact_annot(fitz.Rect(inst.x0 - 2, inst.y0 - 2, inst.x1 + 10, inst.y1 + 2), fill=(1, 1, 1))
        page1.apply_redactions()
        insert_pdf_text(page1, (inst.x0, inst.y1 - 4.0), str(new_title), fontsize=17.0, font_style="title", color=(0, 0, 0))

    # 2. Author Replacement ("Aa Aa")
    aa_matches = page1.search_for("Aa Aa")
    if aa_matches:
        for aa_inst in aa_matches:
            page1.add_redact_annot(fitz.Rect(aa_inst.x0 - 2, aa_inst.y0 - 2, aa_inst.x1 + 10, aa_inst.y1 + 2), fill=(1, 1, 1))
            page1.apply_redactions()
            insert_pdf_text(page1, (aa_inst.x0, aa_inst.y1 - 4.0), "*1 *73 *0 *4 *", fontsize=20.0, font_style="author", color=(0, 0, 0))

    # 3. Document Details Replacements
    replacements = {
        old_id: new_id, old_fname_in_details: original_filename, 
        old_pages_text: f"{actual_pages_count + 2} Pages", old_words_text: f"{actual_words:,} Words",
        old_chars_text: f"{actual_chars:,} Characters", "23.5 KB": new_size
    }
    if old_sub_date: replacements[old_sub_date] = sub_date_str
    if old_down_date: replacements[old_down_date] = down_date_str

    for old_txt, new_txt in replacements.items():
        if not old_txt: continue
        for inst in page1.search_for(old_txt):
            is_right_box = old_txt in [old_pages_text, old_words_text, old_chars_text]
            fill_color = (0.97265625, 0.97265625, 0.97265625) if is_right_box else (1, 1, 1)
            rect_to_clear = fitz.Rect(405.0, inst.y0 - 1, inst.x1 + 10, inst.y1 + 1) if is_right_box else inst
            page1.add_redact_annot(rect_to_clear, fill=fill_color)
            page1.apply_redactions()
            x_pos = 415.0 if is_right_box else inst.x0
            insert_pdf_text(page1, (x_pos, inst.y1 - 1.5), str(new_txt), fontsize=7.0, font_style="bold", color=(0, 0, 0))

    if len(template_doc) > 1:
        page2 = template_doc[1]
        ai_headers = page2.search_for("58% detected as AI")
        if ai_headers:
            inst = ai_headers[0]
            page2.add_redact_annot(fitz.Rect(inst.x0, inst.y0 - 2, inst.x1 + 5, inst.y1 - 4), fill=(1, 1, 1))
            page2.apply_redactions()
            insert_pdf_text(page2, (inst.x0, inst.y1 - 4.0), f"{ai_percentage}% detected as AI", fontsize=17.0, font_style="title", color=(0, 0, 0))

        group_inst = page2.search_for("AI-generated only") or page2.search_for("AI generated only") or page2.search_for("Al-generated only")
        if group_inst:
            try:
                ai_val = int(str(ai_percentage).replace('*', '').strip())
                if ai_val == 0: random_detection_num = 0
                elif ai_val <= 15: random_detection_num = random.randint(1, 4)
                elif ai_val <= 40: random_detection_num = random.randint(5, 15)
                elif ai_val <= 70: random_detection_num = random.randint(16, 35)
                else: random_detection_num = random.randint(36, 77)
            except: random_detection_num = random.randint(1, 77)

            page2.add_redact_annot(fitz.Rect(50.0, 180.0, 320.0, 250.0), fill=(1, 1, 1))
            page2.apply_redactions()

            num_str = str(random_detection_num)
            insert_pdf_text(page2, (52.0, 196.5), num_str, fontsize=7.0, font_style="bold", color=(0, 0, 0))
            text_x = 60.0 if len(num_str) == 1 else 63.0
            insert_pdf_text(page2, (text_x, 196.5), "AI generated only", fontsize=7.0, font_style="normal", color=(0, 0, 0))
            pct_x = text_x + 59.0
            insert_pdf_text(page2, (pct_x, 196.5), f"{ai_percentage}%", fontsize=7.0, font_style="normal", color=(0, 0, 0))

            insert_pdf_text(page2, (52.0, 206.5), "Likely AI generated text from a large language model.", fontsize=7.0, font_style="normal", color=(0.35, 0.35, 0.35))

            insert_pdf_text(page2, (52.0, 224.5), "0", fontsize=7.0, font_style="bold", color=(0, 0, 0))
            insert_pdf_text(page2, (60.0, 224.5), "AI generated text that was AI paraphrased", fontsize=7.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page2, (198.0, 224.5), "0%", fontsize=7.0, font_style="normal", color=(0, 0, 0))

            insert_pdf_text(page2, (52.0, 234.5), "Likely AI generated text that was likely revised using an AI paraphrase tool", fontsize=7.0, font_style="normal", color=(0.35, 0.35, 0.35))
            insert_pdf_text(page2, (52.0, 244.5), "or word spinner.", fontsize=7.0, font_style="normal", color=(0.35, 0.35, 0.35))

    template_doc.insert_pdf(user_doc)
    
    logo_path = "static/logo.png"
    if not os.path.exists(logo_path):
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo.png")

    for i, page in enumerate(template_doc):
        rect = page.rect
        header_height, footer_height = (42, 42) if i < 2 else (18, 18)
        header_title = "Cover Page" if i == 0 else "AI Writing Overview" if i == 1 else "AI Writing Submission"
        header_text = f"Page {i + 1} of {len(template_doc)} - {header_title}"
        
        if i < 2:
            header_rect = fitz.Rect(0, 0, rect.width, header_height)
            footer_rect = fitz.Rect(0, rect.height - footer_height, rect.width, rect.height)

            page.clean_contents() 
            page.draw_rect(header_rect, fill=(1, 1, 1), color=None, overlay=True)
            page.draw_rect(footer_rect, fill=(1, 1, 1), color=None, overlay=True)
            page.add_redact_annot(header_rect, fill=(1, 1, 1))
            page.add_redact_annot(footer_rect, fill=(1, 1, 1))
            page.apply_redactions()

            if os.path.exists(logo_path):
                page.insert_image(fitz.Rect(36, 24, 91, 40.5), filename=logo_path)
                page.insert_image(fitz.Rect(36, 748, 91, 764.5), filename=logo_path)
                
            insert_pdf_text(page, fitz.Point(107, 34), header_text, fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(467.5, 34), "Submission ID", fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(513.0, 34), new_id, fontsize=6.0, font_style="normal", color=(0, 0, 0))

            insert_pdf_text(page, fitz.Point(107, 758), header_text, fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(467.5, 758), "Submission ID", fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(513.0, 758), new_id, fontsize=6.0, font_style="normal", color=(0, 0, 0))
        else:
            top_overlay_rect = fitz.Rect(0, 0, rect.width, 38)
            bot_overlay_rect = fitz.Rect(0, rect.height - 36, rect.width, rect.height)

            page.add_redact_annot(top_overlay_rect, fill=(1, 1, 1))
            page.add_redact_annot(bot_overlay_rect, fill=(1, 1, 1))
            page.apply_redactions()

            page.draw_rect(top_overlay_rect, fill=(1, 1, 1), color=None, overlay=True)
            page.draw_rect(bot_overlay_rect, fill=(1, 1, 1), color=None, overlay=True)

            if os.path.exists(logo_path):
                page.insert_image(fitz.Rect(36, 10, 91, 24.5), filename=logo_path)
                page.insert_image(fitz.Rect(36, rect.height - 22, 91, rect.height - 7.5), filename=logo_path)
                
            insert_pdf_text(page, fitz.Point(107, 21), header_text, fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(467.5, 21), "Submission ID", fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(513.0, 21), new_id, fontsize=6.0, font_style="normal", color=(0, 0, 0))

            insert_pdf_text(page, fitz.Point(107, rect.height - 9), header_text, fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(467.5, rect.height - 9), "Submission ID", fontsize=6.0, font_style="normal", color=(0, 0, 0))
            insert_pdf_text(page, fitz.Point(513.0, rect.height - 9), new_id, fontsize=6.0, font_style="normal", color=(0, 0, 0))

        # Convert ALL colored AI highlights into 1 SINGLE unified Hex #C5E9F8 Cyan color
        cyan_color = (0.7725490196078432, 0.9137254901960784, 0.9725490196078431)
        if i >= 2:
            # 1. Recolor native PDF annotations
            for a in list(page.annots()):
                if a.colors and ("fill" in a.colors or "stroke" in a.colors):
                    a.set_colors(stroke=cyan_color, fill=cyan_color)
                    a.update()

            # 2. Recolor vector drawings behind text layer (overlay=False) for 100% smooth mouse selection
            highlight_rects = []
            for d in page.get_drawings():
                fill = d.get("fill")
                if fill:
                    r_val, g_val, b_val = fill[0], fill[1], fill[2]
                    is_white = (r_val > 0.98 and g_val > 0.98 and b_val > 0.98)
                    is_neutral_gray = (abs(r_val - g_val) < 0.02 and abs(g_val - b_val) < 0.02 and abs(r_val - b_val) < 0.02)
                    if not is_white and not is_neutral_gray and (b_val > r_val or g_val > r_val):
                        highlight_rects.append(d["rect"])
            
            if highlight_rects:
                for r in highlight_rects:
                    erase_rect = fitz.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 2.5)
                    page.draw_rect(erase_rect, fill=(1, 1, 1), color=None, fill_opacity=1.0, overlay=False)
                    page.draw_rect(r, fill=cyan_color, color=None, fill_opacity=1.0, overlay=False)

    template_doc.set_metadata({"producer": "pdf-lib (https://github.com/Hopding/pdf-lib)"})
    template_doc.save(output_path, deflate=True, garbage=4)
    template_doc.close()
    user_doc.close()
    
    del template_doc
    del user_doc
    gc.collect() 

def apply_header_and_footer(input_pdf_path, output_path, shared_id):
    doc = fitz.open(input_pdf_path)
    
    logo_path = "static/logo.png"
    if not os.path.exists(logo_path):
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo.png")

    strip_old_cover_pages(doc)

    cyan_color = (0.7725490196078432, 0.9137254901960784, 0.9725490196078431)
    for i, page in enumerate(doc):
        rect = page.rect
        header_title = "Integrity Submission"
        header_text = f"Page {i + 1} of {len(doc)} - {header_title}"

        top_overlay_rect = fitz.Rect(0, 0, rect.width, 38)
        bot_overlay_rect = fitz.Rect(0, rect.height - 30, rect.width, rect.height)

        page.add_redact_annot(top_overlay_rect, fill=(1, 1, 1))
        page.add_redact_annot(bot_overlay_rect, fill=(1, 1, 1))
        page.apply_redactions()

        page.draw_rect(top_overlay_rect, fill=(1, 1, 1), color=None, overlay=True)
        page.draw_rect(bot_overlay_rect, fill=(1, 1, 1), color=None, overlay=True)

        if os.path.exists(logo_path):
            page.insert_image(fitz.Rect(36, 10, 91, 24.5), filename=logo_path)
            page.insert_image(fitz.Rect(36, rect.height - 22, 91, rect.height - 7.5), filename=logo_path)

        insert_pdf_text(page, fitz.Point(107, 21), header_text, fontsize=6.0, font_style="normal", color=(0, 0, 0))
        insert_pdf_text(page, fitz.Point(467.5, 21), "Submission ID", fontsize=6.0, font_style="normal", color=(0, 0, 0))
        insert_pdf_text(page, fitz.Point(513.0, 21), shared_id, fontsize=6.0, font_style="normal", color=(0, 0, 0))

        insert_pdf_text(page, fitz.Point(107, rect.height - 9), header_text, fontsize=6.0, font_style="normal", color=(0, 0, 0))
        insert_pdf_text(page, fitz.Point(467.5, rect.height - 9), "Submission ID", fontsize=6.0, font_style="normal", color=(0, 0, 0))
        insert_pdf_text(page, fitz.Point(513.0, rect.height - 9), shared_id, fontsize=6.0, font_style="normal", color=(0, 0, 0))

        for a in list(page.annots()):
            if a.colors and ("fill" in a.colors or "stroke" in a.colors):
                a.set_colors(stroke=cyan_color, fill=cyan_color)
                a.update()

        highlight_rects = []
        for d in page.get_drawings():
            fill = d.get("fill")
            if fill:
                r_val, g_val, b_val = fill[0], fill[1], fill[2]
                is_white = (r_val > 0.98 and g_val > 0.98 and b_val > 0.98)
                is_neutral_gray = (abs(r_val - g_val) < 0.02 and abs(g_val - b_val) < 0.02 and abs(r_val - b_val) < 0.02)
                if not is_white and not is_neutral_gray and (b_val > r_val or g_val > r_val):
                    highlight_rects.append(d["rect"])
        
        if highlight_rects:
            for r in highlight_rects:
                erase_rect = fitz.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 2.5)
                page.draw_rect(erase_rect, fill=(1, 1, 1), color=None, fill_opacity=1.0, overlay=False)
                page.draw_rect(r, fill=cyan_color, color=None, fill_opacity=1.0, overlay=False)
    doc.set_metadata({"producer": "pdf-lib (https://github.com/Hopding/pdf-lib)"})
    doc.save(output_path)
    doc.close()
    
    del doc
    gc.collect()

# --- Routes ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": None})

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        res = db_execute(supabase.table("users").select("role").eq("username", username).eq("password", hash_password(password)))
        if res and res.data:
            role = res.data[0]['role']
            token = str(uuid.uuid4())
            db_execute(supabase.table("users").update({"session_token": token}).eq("username", username))
            request.session.update({"username": username, "role": role, "session_token": token})
            return RedirectResponse(url="/", status_code=303)
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "ভুল ইউজারনেম বা পাসওয়ার্ড!"})
    except Exception as e: 
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": f"সিস্টেম এরর: {str(e)}"})

@app.get("/logout")
async def logout(request: Request):
    username = request.session.get("username")
    if username:
        try: db_execute(supabase.table("users").update({"session_token": None}).eq("username", username))
        except: pass
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not check_active_session(request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    username = request.session.get("username")
    role = request.session.get("role")
    credits = check_and_reset_credits(username)
    user_files = []
    try:
        res = db_execute(supabase.table("file_history").select("id, filename, processed_date").eq("username", username).order("id", desc=True).limit(10))
        if res and res.data: user_files = [(f['id'], f['filename'], f['processed_date']) for f in res.data]
    except: pass
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "username": username, "role": role, "credits": credits, "user_files": user_files})

@app.post("/upload")
async def upload_file(request: Request, file_ai: UploadFile = File(...), ai_percentage: str = Form(...)):
    if not check_active_session(request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    username = request.session.get("username")
    credits = check_and_reset_credits(username)
    if credits <= 0:
        return HTMLResponse(content="<h3>আপনার আজকের ক্রেডিট শেষ!</h3><br><a href='/'>Back</a>", status_code=403)

    try:
        unique_id = str(uuid.uuid4())[:8]
        saved_ai_filename = f"{unique_id}_AI_{file_ai.filename}"
        input_ai_path = os.path.join(UPLOAD_DIR, saved_ai_filename)
        with open(input_ai_path, "wb") as buffer: shutil.copyfileobj(file_ai.file, buffer)

        output_report_path = os.path.join(OUTPUT_DIR, f"Report_{saved_ai_filename}")
        shared_submission_id = f"trn:oid:::1:{random.randint(1000000000, 9999999999)}"

        process_master_pdf(input_ai_path, output_report_path, file_ai.filename, ai_percentage, shared_submission_id)

        res_u = db_execute(supabase.table("users").select("used_credits").eq("username", username))
        current_used = res_u.data[0].get("used_credits", 0) if res_u and res_u.data and res_u.data[0].get("used_credits") is not None else 0

        if credits < 900000:
            db_execute(supabase.table("users").update({"daily_credits": credits - 1, "used_credits": current_used + 1}).eq("username", username))
        else:
            db_execute(supabase.table("users").update({"used_credits": current_used + 1}).eq("username", username))

        current_time = get_bdt_time()
        db_execute(supabase.table("file_history").insert([
            {"username": username, "filename": f"Report_{saved_ai_filename}", "processed_date": current_time}
        ]))
        
        db_execute(supabase.table("credit_logs").insert({"username": username, "usage_date": get_bdt_date()}))
        return RedirectResponse(url="/", status_code=303)
        
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error: {str(e)}</h3>", status_code=500)

def delete_file_and_history(file_id: int, output_path: str, upload_filename: str):
    time.sleep(30)
    try:
        if os.path.exists(output_path): os.remove(output_path)
        in_path = os.path.join(UPLOAD_DIR, upload_filename)
        if os.path.exists(in_path): os.remove(in_path)
        db_execute(supabase.table("file_history").delete().eq("id", file_id))
    except Exception as e:
        print("Delete error:", e)

@app.get("/download_past_file/{file_id}")
async def download_past_file(request: Request, file_id: int, background_tasks: BackgroundTasks):
    if not check_active_session(request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)
    
    username = request.session.get("username")
    try:
        res = db_execute(supabase.table("file_history").select("filename").eq("id", file_id).eq("username", username))
        if res and res.data:
            saved_filename = res.data[0]['filename']
            output_path = os.path.join(OUTPUT_DIR, saved_filename)
            upload_filename = saved_filename.replace("Report_", "", 1) if saved_filename.startswith("Report_") else saved_filename.replace("Edited_", "", 1)
            
            if os.path.exists(output_path):
                background_tasks.add_task(delete_file_and_history, file_id, output_path, upload_filename)
                return FileResponse(output_path, media_type="application/pdf", filename=saved_filename[9:])
    except Exception as e: 
        print(e)
        
    return HTMLResponse("<h3>ফাইলটি সার্ভারে পাওয়া যায়নি বা ইতিমধ্যে ডিলিট হয়ে গেছে!</h3><br><a href='/'>হোমে ফিরে যান</a>", status_code=404)

@app.post("/delete_my_file")
async def delete_my_file(request: Request, file_id: int = Form(...)):
    if not check_active_session(request): return RedirectResponse(url="/login", status_code=303)
    username = request.session.get("username")
    try:
        res = db_execute(supabase.table("file_history").select("filename").eq("id", file_id).eq("username", username))
        if res and res.data:
            saved_filename = res.data[0]['filename']
            out_path = os.path.join(OUTPUT_DIR, saved_filename)
            upload_filename = saved_filename.replace("Report_", "", 1) if saved_filename.startswith("Report_") else saved_filename.replace("Edited_", "", 1)
            in_path = os.path.join(UPLOAD_DIR, upload_filename)
            if os.path.exists(in_path): os.remove(in_path)
            if os.path.exists(out_path): os.remove(out_path)
            db_execute(supabase.table("file_history").delete().eq("id", file_id))
    except: pass
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_all_files")
async def delete_all_files(request: Request):
    if not check_active_session(request): return RedirectResponse(url="/login", status_code=303)
    try: db_execute(supabase.table("file_history").delete().eq("username", request.session.get("username")))
    except: pass
    return RedirectResponse(url="/", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not check_active_session(request) or request.session.get("role") != "admin": return HTMLResponse("Access Denied", status_code=403)
    users, history, daily_usage_list = [], [], []
    today = get_bdt_date()
    try:
        users_res = supabase.table("users").select("username, role, daily_credits, used_credits").execute()
            
        if users_res.data:
            for u in users_res.data:
                uname = u['username']
                logs_today = supabase.table("credit_logs").select("id").eq("username", uname).eq("usage_date", today).execute()
                used_today = len(logs_today.data) if logs_today.data else 0
                total_used = u.get('used_credits', 0) if u.get('used_credits') is not None else 0
                users.append((uname, u['role'], u['daily_credits'], used_today, total_used))

        hist_res = supabase.table("file_history").select("username, filename, processed_date").order("id", desc=True).limit(50).execute()
        if hist_res.data: history = [(h['username'], h['filename'], h['processed_date']) for h in hist_res.data]
                
        five_days_ago = (datetime.utcnow() + timedelta(hours=6) - timedelta(days=5)).strftime("%Y-%m-%d")
        supabase.table("credit_logs").delete().lt("usage_date", five_days_ago).execute()
        
        usage_res = supabase.table("credit_logs").select("username, usage_date").execute()
        if usage_res.data:
            usage_dict = {}
            for row in usage_res.data:
                key = (row['username'], row['usage_date'])
                usage_dict[key] = usage_dict.get(key, 0) + 1
            daily_usage_list = sorted([{"username": u, "date": d, "used": count} for (u, d), count in usage_dict.items()], key=lambda x: x['date'], reverse=True)
            
    except: pass
    total_files = len(os.listdir(UPLOAD_DIR)) + len(os.listdir(OUTPUT_DIR))
    return templates.TemplateResponse(request=request, name="admin.html", context={"request": request, "users": users, "history": history, "total_files": total_files, "daily_usage": daily_usage_list})

@app.post("/admin/create_user")
async def create_user(request: Request, new_username: str = Form(...), new_password: str = Form(...), initial_credits: int = Form(5)):
    if not check_active_session(request) or request.session.get("role") != "admin": return HTMLResponse("Access Denied", status_code=403)
    try:
        supabase.table("users").insert({"username": new_username, "password": hash_password(new_password), "role": "user", "daily_credits": initial_credits, "credit_limit": initial_credits, "used_credits": 0, "last_reset_date": get_bdt_date()}).execute()
    except: pass
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/update_credits")
async def update_credits(request: Request, up_username: str = Form(...), new_credits: int = Form(...)):
    if not check_active_session(request) or request.session.get("role") != "admin": return HTMLResponse("Access Denied", status_code=403)
    try:
        supabase.table("users").update({"daily_credits": int(new_credits), "credit_limit": int(new_credits)}).eq("username", up_username).execute()
    except: pass
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/reset_used_credits")
async def reset_used_credits(request: Request, rst_username: str = Form(...)):
    if not check_active_session(request) or request.session.get("role") != "admin": return HTMLResponse("Access Denied", status_code=403)
    try: supabase.table("users").update({"used_credits": 0}).eq("username", rst_username).execute()
    except: pass
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/delete_user")
async def delete_user(request: Request, del_username: str = Form(...)):
    if not check_active_session(request) or request.session.get("role") != "admin": return HTMLResponse("Access Denied", status_code=403)
    if del_username == "admin": return HTMLResponse("Admin account cannot be deleted!", status_code=400)
    try: supabase.table("users").delete().eq("username", del_username).execute()
    except: pass
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/clear_all_files")
async def clear_all_files(request: Request):
    if not check_active_session(request) or request.session.get("role") != "admin": return HTMLResponse("Access Denied", status_code=403)
    for folder in [UPLOAD_DIR, OUTPUT_DIR]:
        for f in os.listdir(folder):
            if os.path.isfile(os.path.join(folder, f)): os.remove(os.path.join(folder, f))
    try: supabase.table("file_history").delete().neq("id", 0).execute()
    except: pass
    return RedirectResponse(url="/admin", status_code=303)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
