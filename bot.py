import os
import re
import psycopg2
import pandas as pd
import json
import datetime as dt
import logging
import asyncio
import functools
import tempfile
import requests
import html
import base64
import time as time_module
from unicodedata import normalize
from dotenv import load_dotenv
from telegram import Update, BotCommand
try:
    from telegram import BotCommandScopeAllPrivateChats
    HAS_PRIVATE_SCOPE = True
except Exception:
    HAS_PRIVATE_SCOPE = False

from telegram.ext import (
    Application, MessageHandler, CommandHandler, ContextTypes, filters
)
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from psycopg2 import pool
from bs4 import BeautifulSoup
from openai import OpenAI

# ----------------------------- PORT AYARI (RAILWAY İÇİN) -----------------------------
PORT = int(os.environ.get('PORT', 8443))

# ----------------------------- DATABASE POOL -----------------------------
DB_POOL = None

def init_db_pool():
    """Database connection pool'u başlat"""
    global DB_POOL
    try:
        if DB_POOL is None:
            DB_POOL = pool.ThreadedConnectionPool(
                minconn=1, 
                maxconn=10, 
                dsn=os.environ['DATABASE_URL'], 
                sslmode='require'
            )
            logging.info("✅ Database connection pool başlatıldı")
    except Exception as e:
        logging.error(f"❌ Database pool başlatma hatası: {e}")
        raise

def get_conn_from_pool():
    """Pool'dan connection al"""
    if DB_POOL is None:
        init_db_pool()
    return DB_POOL.getconn()

def put_conn_back(conn):
    """Connection'ı pool'a geri ver"""
    if DB_POOL and conn:
        DB_POOL.putconn(conn)

# ----------------------------- ASYNC DATABASE HELPERS -----------------------------
def _sync_fetchall(query, params=()):
    """Sync fetchall fonksiyonu"""
    conn = get_conn_from_pool()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return rows
    except Exception as e:
        logging.error(f"Database fetchall hatası: {e}")
        raise
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

def _sync_execute(query, params=()):
    """Sync execute fonksiyonu"""
    conn = get_conn_from_pool()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        return cur.rowcount
    except Exception as e:
        conn.rollback()
        logging.error(f"Database execute hatası: {e}")
        raise e
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

def _sync_fetchone(query, params=()):
    """Sync fetchone fonksiyonu"""
    conn = get_conn_from_pool()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return row
    except Exception as e:
        logging.error(f"Database fetchone hatası: {e}")
        raise
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

async def async_db_query(func, *args, **kwargs):
    """Async database sorgusu"""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
    except Exception as e:
        logging.error(f"Async DB query hatası: {e}")
        raise

async def async_fetchall(query, params=()):
    """Async fetchall"""
    return await async_db_query(_sync_fetchall, query, params)

async def async_execute(query, params=()):
    """Async execute"""
    return await async_db_query(_sync_execute, query, params)

async def async_fetchone(query, params=()):
    """Async fetchone"""
    return await async_db_query(_sync_fetchone, query, params)

# ----------------------------- YANDEX DISK YEDEKLEME -----------------------------
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")

def upload_to_yandex(file_path, yandex_path):
    """Dosyayı Yandex.Disk'e yükler"""
    if not YANDEX_DISK_TOKEN:
        logging.error("❌ Yandex.Disk token bulunamadı!")
        return False
        
    if not os.path.exists(file_path):
        logging.error(f"❌ Yedeklenecek dosya bulunamadı: {file_path}")
        return False
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            headers = {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}
            upload_url = "https://cloud-api.yandex.net/v1/disk/resources/upload"
            params = {"path": yandex_path, "overwrite": "true"}
            
            resp = requests.get(upload_url, headers=headers, params=params, timeout=30)
            
            if resp.status_code != 200:
                logging.error(f"❌ Yandex API hatası ({resp.status_code}): {resp.text}")
                if attempt < max_retries - 1:
                    continue
                return False
                
            href = resp.json().get("href")
            
            if not href:
                logging.error(f"❌ Upload linki alınamadı: {resp.text}")
                if attempt < max_retries - 1:
                    continue
                return False
            
            with open(file_path, "rb") as f:
                upload_resp = requests.put(href, data=f, timeout=60)
                
            if upload_resp.status_code == 201:
                file_size = os.path.getsize(file_path) / (1024 * 1024)
                logging.info(f"✅ Yandex.Disk'e yüklendi: {yandex_path} ({file_size:.2f} MB)")
                return True
            else:
                logging.error(f"❌ Yükleme hatası ({upload_resp.status_code}): {upload_resp.text}")
                if attempt < max_retries - 1:
                    continue
                return False
                    
        except requests.exceptions.Timeout:
            logging.error(f"❌ Yandex timeout hatası (attempt {attempt + 1})")
            if attempt < max_retries - 1:
                continue
            return False
        except Exception as e:
            logging.error(f"❌ Yandex yedekleme hatası (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                continue
            return False
    
    return False

async def async_upload_to_yandex(file_path, yandex_path):
    """Async Yandex upload"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, upload_to_yandex, file_path, yandex_path)

async def yandex_yedekleme_gorevi(context: ContextTypes.DEFAULT_TYPE):
    """Her gün 23:00'de otomatik yedekleme"""
    try:
        logging.info("💾 Yandex.Disk yedekleme işlemi başlatılıyor...")
        
        if not YANDEX_DISK_TOKEN:
            logging.error("❌ Yandex.Disk token bulunamadı!")
            for admin_id in ADMINS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text="❌ **Yedekleme Hatası:** Yandex.Disk token bulunamadı! Lütfen .env dosyasını kontrol edin."
                    )
                except Exception as e:
                    logging.error(f"Hata bildirimi {admin_id} adminine gönderilemedi: {e}")
            return
        
        success_count = 0
        total_count = 0
        
        backup_files = [
            ("Kullanicilar.xlsx", "/RaporBot_Backup/Kullanicilar.xlsx"),
            ("bot.log", "/RaporBot_Backup/bot.log")
        ]
        
        for local_file, yandex_path in backup_files:
            if os.path.exists(local_file):
                total_count += 1
                if await async_upload_to_yandex(local_file, yandex_path):
                    success_count += 1
            else:
                logging.warning(f"⚠️ Yedeklenecek dosya bulunamadı: {local_file}")
        
        status_msg = f"💾 **Gece Yedekleme Raporu**\n\n"
        status_msg += f"📅 Tarih: {dt.datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}\n"
        status_msg += f"📁 Dosya: {success_count}/{total_count} başarılı\n"
        
        if success_count == total_count:
            status_msg += "🎉 Tüm yedeklemeler başarılı!"
            logging.info("💾 Gece yedeklemesi tamamlandı: Tüm dosyalar başarıyla yedeklendi")
        else:
            status_msg += f"⚠️ {total_count - success_count} dosya yedeklenemedi"
            logging.warning(f"💾 Gece yedeklemesi kısmen başarılı: {success_count}/{total_count}")
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=status_msg
                )
                logging.info(f"💾 Yedekleme raporu {admin_id} adminine gönderildi")
            except Exception as e:
                logging.error(f"Yedekleme raporu {admin_id} adminine gönderilemedi: {e}")
                
    except Exception as e:
        logging.error(f"💾 Yandex.Disk yedekleme hatası: {e}")
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"❌ **Yedekleme Hatası:** {str(e)}"
                )
            except Exception as admin_e:
                logging.error(f"Hata bildirimi {admin_id} adminine gönderilemedi: {admin_e}")

# ----------------------------- MANUEL YEDEKLEME KOMUTU -----------------------------
async def yedekle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manuel yedekleme komutu - Sadece Super Admin"""
    if not await super_admin_kontrol(update, context):
        return
    
    await update.message.reply_text("💾 Yedekleme işlemi başlatılıyor...")
    
    try:
        if not YANDEX_DISK_TOKEN:
            await update.message.reply_text("❌ Yandex.Disk token bulunamadı! .env dosyasını kontrol edin.")
            return
        
        success_count = 0
        backup_files = [
            ("Kullanicilar.xlsx", "/RaporBot_Backup/Kullanicilar.xlsx"),
            ("bot.log", "/RaporBot_Backup/bot.log")
        ]
        
        for local_file, yandex_path in backup_files:
            if os.path.exists(local_file):
                if await async_upload_to_yandex(local_file, yandex_path):
                    success_count += 1
        
        if success_count == len(backup_files):
            await update.message.reply_text("✅ Tüm yedeklemeler başarıyla tamamlandı!")
        else:
            await update.message.reply_text(f"⚠️ Yedekleme kısmen başarılı: {success_count}/{len(backup_files)} dosya")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Yedekleme hatası: {e}")

# ----------------------------- LOGGING (RAILWAY İÇİN) -----------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)

# ----------------------------- ENV -----------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
GROUP_ID = int(CHAT_ID) if CHAT_ID else None
TZ = ZoneInfo("Asia/Tashkent")

# ----------------------------- SABİT SUPER ADMIN -----------------------------
SUPER_ADMIN_ID = 1000157326

# ----------------------------- FALLBACK KULLANICI LİSTESİ -----------------------------
FALLBACK_USERS = [
    {
        "Telegram ID": 1000157326,
        "Kullanici Adi Soyadi": "Atamurat Kamalov", 
        "Takip": "E",
        "Rol": "SÜPER ADMIN",
        "Botdaki Statusu": "Aktif",
        "Proje / Şantiye": "TYM"
    },
    {
        "Telegram ID": 709746899,
        "Kullanici Adi Soyadi": "Eren Boz",
        "Takip": "E", 
        "Rol": "ADMIN",
        "Botdaki Statusu": "Aktif",
        "Proje / Şantiye": "TYM"
    }
]

# ----------------------------- EXCEL ve DATABASE -----------------------------
USERS_FILE = "Kullanicilar.xlsx"

df = None
rapor_sorumlulari = []
id_to_name = {}
id_to_projects = {}
id_to_status = {}
id_to_rol = {}
ADMINS = []
IZLEYICILER = []
TUM_KULLANICILAR = []
santiye_sorumlulari = {}
santiye_rapor_durumu = {}
last_excel_update = 0

# ----------------------------- USER ROLE CACHE -----------------------------
user_role_cache = {}
user_role_cache_time = 0

async def get_user_role(user_id):
    """Cache'li user rol kontrolü"""
    global user_role_cache, user_role_cache_time
    
    current_time = time_module.time()
    if current_time - user_role_cache_time > 300:
        user_role_cache = {}
        user_role_cache_time = current_time
    
    if user_id in user_role_cache:
        return user_role_cache[user_id]
    
    role = "USER"
    if user_id in ADMINS:
        role = "ADMIN"
    if user_id == SUPER_ADMIN_ID:
        role = "SUPER_ADMIN"
    
    user_role_cache[user_id] = role
    return role

def _to_int_or_none(x):
    """Excel'den ID okumak için geliştirilmiş fonksiyon"""
    if x is None or pd.isna(x):
        return None
    
    s = str(x).strip()
    if not s:
        return None
    
    if "e+" in s.lower():
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None
    
    s_clean = re.sub(r'[^\d]', '', s)
    
    if len(s_clean) < 8:
        return None
    
    try:
        return int(s_clean)
    except (ValueError, TypeError):
        return None

# ----------------------------- ŞANTİYE BAZLI SORUMLULUK SİSTEMİ -----------------------------
def load_excel():
    """Excel okunamazsa fallback kullanıcı listesini kullan"""
    global df, rapor_sorumlulari, id_to_name, id_to_projects, id_to_status, id_to_rol, ADMINS, IZLEYICILER, TUM_KULLANICILAR, last_excel_update
    global santiye_sorumlulari, santiye_rapor_durumu
    
    try:
        df = pd.read_excel(USERS_FILE)
        logging.info("✅ Excel dosyası başarıyla yüklendi")
    except Exception as e:
        logging.error(f"❌ Excel okuma hatası: {e}. Fallback kullanıcı listesi kullanılıyor.")
        df = pd.DataFrame(FALLBACK_USERS)
    
    temp_rapor_sorumlulari = []
    temp_id_to_name = {}
    temp_id_to_projects = {}
    temp_id_to_status = {}
    temp_id_to_rol = {}
    temp_admins = []
    temp_izleyiciler = []
    temp_tum_kullanicilar = []
    temp_santiye_sorumlulari = {}
    processed_names = set()

    for _, r in df.iterrows():
        tid = _to_int_or_none(r.get("Telegram ID"))
        fullname = str(r.get("Kullanici Adi Soyadi") or "").strip()
        takip = str(r.get("Takip") or "").strip().upper()
        status = str(r.get("Botdaki Statusu") or "").strip()
        rol = str(r.get("Rol") or "").strip().upper()

        if not fullname:
            continue

        if tid and fullname:
            tid = int(tid)
            temp_id_to_name[tid] = fullname
            temp_id_to_status[tid] = status
            temp_id_to_rol[tid] = rol
            
            temp_tum_kullanicilar.append(tid)
            
            if rol in ["ADMIN", "SÜPER ADMIN", "SUPER ADMIN"]:
                temp_admins.append(tid)
            
            if rol == "İZLEYİCİ":
                temp_izleyiciler.append(tid)
            
            raw = str(r.get("Proje / Şantiye") or "")
            parts = [p.strip() for p in re.split(r'[/,\-\|]', raw) if p.strip()]
            temp_id_to_projects[tid] = parts
            
            for proje in parts:
                if proje not in temp_santiye_sorumlulari:
                    temp_santiye_sorumlulari[proje] = []
                if tid not in temp_santiye_sorumlulari[proje]:
                    temp_santiye_sorumlulari[proje].append(tid)
            
            if takip == "E" and tid and fullname:
                temp_rapor_sorumlulari.append(tid)
                processed_names.add(fullname)

    rapor_sorumlulari = temp_rapor_sorumlulari
    id_to_name = temp_id_to_name
    id_to_projects = temp_id_to_projects
    id_to_status = temp_id_to_status
    id_to_rol = temp_id_to_rol
    ADMINS = temp_admins
    IZLEYICILER = temp_izleyiciler
    TUM_KULLANICILAR = temp_tum_kullanicilar
    santiye_sorumlulari = temp_santiye_sorumlulari
    
    santiye_rapor_durumu = {}
    
    if SUPER_ADMIN_ID not in ADMINS:
        ADMINS.append(SUPER_ADMIN_ID)
    
    last_excel_update = os.path.getmtime(USERS_FILE) if os.path.exists(USERS_FILE) else 0
    logging.info(f"Excel yüklendi: {len(rapor_sorumlulari)} takip edilen kullanıcı, {len(ADMINS)} admin, {len(IZLEYICILER)} izleyici, {len(TUM_KULLANICILAR)} toplam kullanıcı, {len(santiye_sorumlulari)} şantiye")

load_excel()

# PostgreSQL bağlantısı
def get_db_connection():
    """PostgreSQL bağlantısını döndür"""
    return psycopg2.connect(os.environ['DATABASE_URL'], sslmode='require')

# ----------------------------- MEDIA FİLTRE BLOĞU -----------------------------
def is_media_message(message) -> bool:
    """
    MEDIA FILTER BLOCK
    Foto, video, ses, belge, caption-only gibi mesajların
    rapor analizine girmesini engeller.
    """
    if message.photo:
        return True
    if message.video:
        return True
    if message.audio:
        return True
    if message.voice:
        return True
    if message.animation:
        return True
    if message.video_note:
        return True
    if message.document:
        return True

    # Caption-only media (örnek: yalnızca foto + kısa açıklama)
    if (message.caption and not message.text):
        return True

    return False

# ----------------------------- GPT-4-MINI SİSTEM PROMPT (FINAL CI/CD SÜRÜMÜ) -----------------------------
SYSTEM_PROMPT = """You are a deterministic construction report extraction engine.
Your behavior strictly depends on the provided chat_type.

==================================================
CHAT TYPE LOGIC (MANDATORY)
==================================================
You will ALWAYS be given `chat_type` inside the user message.

Allowed values:
• "group"
• "supergroup" 
• "private"

Your required behavior:

1) If chat_type = "group" or "supergroup":
   • If NO valid report exists → return []  
     (Bot will stay silent. This is REQUIRED.)
   • If 1 or more reports exist → return a JSON array of report objects.

2) If chat_type = "private":
   • If NO valid report exists → return:
       [
         { "dm_info": "no_report_detected" }
       ]
   • If valid reports exist → return a JSON array of report objects.

You MUST obey this behavior exactly. No exceptions.

==================================================
WHAT COUNTS AS A REPORT?
==================================================
A message counts as a report ONLY if it contains at least ONE of:

• A detectable date  
  (DD.MM.YYYY, D.M.YYYY, DD/MM/YYYY, 1 November 2025, "03.11.2025 Pazartesi")
• A known construction site name  
  (LOT13, LOT71, SKP, BWC, Piramit Tower, Staff, Chalet, Otel, Villa, SPA...)
• Work descriptions  
  (montaj, test, kablo çekimi, reglaj, bağlantı…)
• Personnel distribution  
  (Mühendis, Tekniker, Formen, Gececi, İzinli, Hasta…)
• Section headers  
  (ŞANTİYE:, TARİH:, PERSONEL DURUMU, GENEL ÖZET, OTEL(), VILLA(), A BLOK…)

If NONE of these exist → it is NOT a report.

==================================================
MULTI-REPORT SPLITTING
==================================================
A single message may contain multiple reports.

Start a new report whenever ANY of these appear:
• A new date  
• A new site name  
• Section headers  
• Block headers (OTEL(), VILLA(), SPA(), A/B/C Blok)
• Repeated patterns:
     Date → job list → totals → Date → job list → totals

Each detected block MUST become a separate JSON object.

==================================================
DATE RULES
==================================================
• Accept ANY date format.  
• Convert to "YYYY-MM-DD" when possible.  
• If date cannot be determined → reported_at = null.  
• If date > current_date → EXCLUDE THE REPORT.  
• If date older than 365 days → include but set confidence ≤ 0.40.  

==================================================
OUTPUT FORMAT RULES  (MANDATORY)
==================================================
You MUST output ONLY a JSON array.  
Never return text, comments, code formatting, explanations, warnings.

Each valid report must match this EXACT schema:

{
  "report_id": null,
  "site": string|null,
  "reported_at": "YYYY-MM-DD" | null,
  "reported_time": "HH:MM" | null,
  "reporter": null,
  "report_type": string|null,
  "status_summary": string|null,
  "present_workers": integer|null,
  "absent_workers": integer|null,
  "issues": [string],
  "actions_requested": [string],
  "attachments_ref": [string],
  "raw_text": string,
  "confidence": number
}

==================================================
FIELD EXTRACTION RULES
==================================================
• "Toplam X" → present_workers = X  
• "İzinli X" / "Hasta X" → absent_workers = X  
• For grouped sites (Otel, Villa, SPA, SKP, BWC…) you may sum sub-group values  
• Issues = short problem-type phrases  
• actions_requested = verbs like montaj, test, kontrol, hazırlık, bağlantı  
• reporter = null (always)  
• raw_text = exact text slice belonging to that report

==================================================
ABSOLUTE FINAL RULES
==================================================
• ALWAYS return valid JSON array.
• NEVER hallucinate values. Unknown → null.
• NEVER generate notes or explanations.
• NEVER merge multiple reports.
• ALWAYS keep report order as in the original message.

End of instructions."""

USER_PROMPT_TEMPLATE = """
chat_type: "<<<CHAT_TYPE>>>"
raw_message: "<<<RAW_MESSAGE>>>"

Extract all valid reports according to the system rules.
Return ONLY a JSON array.
"""

# OpenAI istemcisini başlat
client = OpenAI(api_key=OPENAI_API_KEY)

def process_incoming_message(raw_text: str, is_group: bool = False):
    """Gelen mesajı işle - DM/Group ayrımı ile - FINAL CI/CD VERSİYONU"""
    today = dt.date.today()
    
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            # Chat type'ı belirle
            chat_type = "group" if is_group else "private"
            
            user_prompt = USER_PROMPT_TEMPLATE.replace("<<<CHAT_TYPE>>>", chat_type)
            user_prompt = user_prompt.replace("<<<RAW_MESSAGE>>>", raw_text)

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0,
                max_tokens=2000
            )

            content = response.choices[0].message.content.strip()

            try:
                data = json.loads(content)
                
                # ---- FINAL CI/CD MANTIĞI ----
                if isinstance(data, list):
                    # Grup modu - rapor yoksa [] döndür
                    if is_group:
                        if len(data) == 0:
                            return []  # Grup + rapor yok = sessiz çıkış
                        # Grup + dm_info varsa bile sessiz çık
                        if len(data) == 1 and data[0].get("dm_info"):
                            return []
                    
                    # DM modu - rapor yoksa dm_info döndür
                    if not is_group:
                        if len(data) == 1 and data[0].get("dm_info") == "no_report_detected":
                            return {"dm_info": "no_report_detected"}
                        # DM'de dm_info dışında boş array gelirse de dm_info'ya çevir
                        if len(data) == 0:
                            return {"dm_info": "no_report_detected"}

                # ---- Rapor filtreleme ----
                filtered_reports = []
                for report in data:
                    # dm_info içerenleri atla
                    if report.get('dm_info'):
                        continue

                    # Gelecek tarih kontrolü
                    reported_at = report.get('reported_at')
                    if reported_at:
                        try:
                            report_date = dt.datetime.strptime(reported_at, '%Y-%m-%d').date()
                            if report_date > today:
                                continue  # Gelecek tarihli raporları atla
                        except ValueError:
                            pass

                    # Confidence değeri ekle
                    if 'confidence' not in report:
                        report['confidence'] = 0.9
                    
                    filtered_reports.append(report)
                
                return filtered_reports
            
            except json.JSONDecodeError:
                logging.error(f"GPT JSON parse hatası: {content}")
                if attempt < max_retries - 1:
                    time_module.sleep(retry_delay)
                    continue
                # JSON hatasında chat type'a göre davran
                return [] if is_group else {"dm_info": "no_report_detected"}
                
        except Exception as e:
            logging.error(f"GPT analiz hatası (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time_module.sleep(retry_delay)
                continue
            # Genel hatada chat type'a göre davran
            return [] if is_group else {"dm_info": "no_report_detected"}

# ----------------------------- YENİ GPT-4-MINI RAPOR İŞLEME (FINAL CI/CD) -----------------------------
async def yeni_gpt_rapor_isleme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yeni GPT-4-mini ile çoklu rapor işleme - FINAL CI/CD VERSİYONU"""
    msg = update.message or update.edited_message
    if not msg:
        return

    user_id = msg.from_user.id
    chat_type = msg.chat.type
    
    # Chat tipini belirle
    is_group = chat_type in ["group", "supergroup"]
    is_dm = chat_type == "private"

    # ✅ MEDIA FILTER BLOCK - Tüm medya mesajlarını sessizce geç
    if is_media_message(msg):
        logging.info(f"⛔ Medya mesajı tespit edildi → AI analizi yapılmayacak. User: {user_id}, Chat Type: {chat_type}")
        return

    metin = msg.text or msg.caption
    if not metin:
        return

    # Komutları atla
    if metin.startswith(('/', '.', '!', '\\')):
        return

    try:
        # GPT-4-mini ile rapor çıkarımı (is_group bilgisini ver)
        raporlar = process_incoming_message(metin, is_group)
        
        # DM_INFO kontrolü - DM'de rapor yoksa kullanıcıyı bilgilendir
        if is_dm and isinstance(raporlar, dict) and raporlar.get('dm_info') == 'no_report_detected':
            await msg.reply_text(
                "❌ **Bu mesaj bir rapor olarak algılanmadı.**\n\n"
                "Lütfen şantiye, tarih ve iş bilgilerini içeren bir rapor gönderin.\n"
                "Örnek: \"01.11.2024 LOT13 2.kat kablo çekimi 5 kişi\""
            )
            return
        
        # Normal rapor listesi kontrolü - Grup için sessiz, DM için bilgi
        if not raporlar or (isinstance(raporlar, list) and len(raporlar) == 0):
            logging.info(f"🤖 GPT: Rapor bulunamadı - {user_id} (Chat Type: {chat_type})")
            
            # Sadece DM'de bilgi ver
            if is_dm:
                await msg.reply_text(
                    "❌ **Rapor bulunamadı.**\n\n"
                    "Lütfen şantiye raporunuzu aşağıdaki formatta gönderin:\n"
                    "• Tarih (01.01.2025)\n" 
                    "• Şantiye adı (LOT13, BWC, SKP vb.)\n"
                    "• Yapılan işler\n"
                    "• Personel bilgisi\n\n"
                    "Örnek: \"01.11.2024 LOT13 2.kat kablo çekimi 5 kişi\""
                )
            # Grup mesajlarında SESSİZ ÇIKIŞ
            return

        logging.info(f"🤖 GPT: {len(raporlar)} rapor çıkarıldı - {user_id} (Chat Type: {chat_type})")
        
        kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
        
        # Her raporu ayrı ayrı işle
        basarili_kayitlar = 0
        for i, rapor in enumerate(raporlar):
            try:
                await raporu_gpt_formatinda_kaydet(user_id, kullanici_adi, metin, rapor, msg, i+1)
                basarili_kayitlar += 1
            except Exception as e:
                logging.error(f"❌ Rapor {i+1} kaydetme hatası: {e}")
        
        # Kullanıcıya geri bildirim (sadece DM'de)
        if is_dm:
            if basarili_kayitlar == len(raporlar):
                if len(raporlar) == 1:
                    await msg.reply_text("✅ Raporunuz başarıyla işlendi!")
                else:
                    await msg.reply_text(f"✅ {len(raporlar)} rapor başarıyla işlendi!")
            else:
                await msg.reply_text(f"⚠️ {basarili_kayitlar}/{len(raporlar)} rapor işlendi. Bazı raporlar kaydedilemedi.")
        
        # Grup mesajlarında sessiz kal, sadece log
        logging.info(f"📊 Grup raporu işlendi: {basarili_kayitlar}/{len(raporlar)} başarılı")
            
    except Exception as e:
        logging.error(f"❌ GPT rapor işleme hatası: {e}")
        # Hata durumunda sadece DM'de bilgi ver
        if is_dm:
            await msg.reply_text("❌ Rapor işlenirken bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

async def raporu_gpt_formatinda_kaydet(user_id, kullanici_adi, orijinal_metin, gpt_rapor, msg, rapor_no=1):
    """GPT formatındaki raporu veritabanına kaydet - Şantiye bazlı"""
    try:
        # Tarih işleme
        rapor_tarihi = None
        reported_at = gpt_rapor.get('reported_at')
        if reported_at:
            try:
                rapor_tarihi = dt.datetime.strptime(reported_at, '%Y-%m-%d').date()
            except ValueError:
                pass
        
        if not rapor_tarihi:
            rapor_tarihi = parse_rapor_tarihi(orijinal_metin) or dt.datetime.now(TZ).date()
        
        # Proje adı - GPT'den geleni kullan, yoksa kullanıcının şantiyelerinden al
        project_name = gpt_rapor.get('site')
        if not project_name or project_name == 'BELİRSİZ':
            user_projects = id_to_projects.get(user_id, [])
            if user_projects:
                project_name = user_projects[0]
            else:
                project_name = 'BELİRSİZ'
        
        # ŞANTİYE BAZLI KONTROL - Aynı gün aynı şantiye için rapor var mı?
        existing_report = await async_fetchone("""
            SELECT id FROM reports 
            WHERE user_id = %s AND project_name = %s AND report_date = %s
        """, (user_id, project_name, rapor_tarihi))
        
        if existing_report:
            logging.warning(f"⚠️ Zaten rapor var: {user_id} - {project_name} - {rapor_tarihi}")
            raise Exception(f"Bu şantiye için bugün zaten rapor gönderdiniz: {project_name}")
        
        # Rapor tipini AI'dan al, değiştirme
        rapor_tipi = gpt_rapor.get('report_type', 'RAPOR')
        
        # Personel sayısı
        present_workers = gpt_rapor.get('present_workers', 0)
        absent_workers = gpt_rapor.get('absent_workers', 0)
        person_count = max(present_workers, 1)
        
        # İş açıklaması
        status_summary = gpt_rapor.get('status_summary', '')
        issues = gpt_rapor.get('issues', [])
        
        work_description = status_summary
        if issues:
            work_description += f" | İşler: {', '.join(issues[:3])}"
        
        if not work_description.strip():
            work_description = orijinal_metin[:200]
        
        # AI analiz verisi
        ai_analysis = {
            "gpt_analysis": gpt_rapor,
            "confidence": gpt_rapor.get('confidence', 0.9),
            "extraction_method": "gpt-4-mini",
            "original_text_snippet": orijinal_metin[:100],
            "raw_text": gpt_rapor.get('raw_text', '')[:500]
        }
        
        # Veritabanına kaydet
        await async_execute("""
            INSERT INTO reports 
            (user_id, project_name, report_date, report_type, person_count, work_description, 
             work_category, personnel_type, delivered_date, is_edited, ai_analysis)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id, project_name, rapor_tarihi, rapor_tipi, person_count, 
            work_description[:400], 'diğer', 'imalat', dt.datetime.now(TZ).date(),
            False, json.dumps(ai_analysis, ensure_ascii=False)
        ))
        
        logging.info(f"✅ GPT Rapor #{rapor_no} kaydedildi: {user_id} - {project_name} - {rapor_tarihi}")
        
        # Maliyet analizine ekle
        maliyet_analiz.kayit_ekle('gpt')
            
    except Exception as e:
        logging.error(f"❌ GPT rapor kaydetme hatası: {e}")
        raise e

# ----------------------------- YENİ ÜYE KARŞILAMA -----------------------------
async def yeni_uye_karşilama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yeni üye gruba katıldığında hoş geldin mesajı"""
    try:
        for member in update.message.new_chat_members:
            if member.id == context.bot.id:
                await update.message.reply_text(
                    "🤖 **Rapor Botu Aktif!**\n\n"
                    "Ben şantiye raporlarınızı otomatik olarak işleyen bir botum.\n"
                    "Günlük çalışma raporlarınızı gönderebilirsiniz.\n\n"
                    "📋 **Özellikler:**\n"
                    "• Otomatik rapor analizi\n"
                    "• Tarih tanıma\n"
                    "• Personel sayımı\n"
                    "• Şantiye takibi\n\n"
                    "Kolay gelsin! 👷‍♂️"
                )
            else:
                await update.message.reply_text(
                    f"👋 Hoş geldin {member.first_name}!\n\n"
                    f"🤖 Ben şantiye raporlarınızı otomatik işleyen bir botum.\n"
                    f"Günlük çalışma raporlarınızı bu gruba gönderebilirsiniz.\n\n"
                    f"Kolay gelsin! 👷‍♂️"
                )
    except Exception as e:
        logging.error(f"Yeni üye karşılama hatası: {e}")

# ----------------------------- VERİTABANI ŞEMA GÜNCELLEMESİ -----------------------------
def update_database_schema():
    """Gerekli veritabanı şema güncellemelerini yap"""
    try:
        index_queries = [
            "CREATE INDEX IF NOT EXISTS idx_reports_date_user ON reports(report_date, user_id)",
            "CREATE INDEX IF NOT EXISTS idx_reports_project_date ON reports(project_name, report_date)",
            "CREATE INDEX IF NOT EXISTS idx_reports_type_date ON reports(report_type, report_date)",
            "CREATE INDEX IF NOT EXISTS idx_reports_user_date ON reports(user_id, report_date)"
        ]
        
        for query in index_queries:
            try:
                _sync_execute(query)
            except Exception as e:
                logging.warning(f"Index oluşturma hatası (muhtemelen zaten var): {e}")
        
        logging.info("✅ Veritabanı şeması güncellendi")
        
    except Exception as e:
        logging.error(f"❌ Şema güncelleme hatası: {e}")

# ----------------------------- YENİ VERİTABANI YAPISI -----------------------------
def init_database():
    """Yeni normalleştirilmiş veritabanı yapısını oluştur"""
    try:
        _sync_execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id=1), 
                version INTEGER NOT NULL
            )
        """)
        
        _sync_execute("""
            INSERT INTO schema_version (id, version) 
            SELECT 1, 2
            WHERE NOT EXISTS(SELECT 1 FROM schema_version WHERE id=1)
        """)
        
        _sync_execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                project_name VARCHAR(200),
                report_date DATE NOT NULL,
                report_type VARCHAR(50) NOT NULL,
                person_count INTEGER DEFAULT 1,
                work_description TEXT,
                work_category VARCHAR(100),
                personnel_type VARCHAR(100),
                delivered_date DATE,
                is_edited BOOLEAN DEFAULT FALSE,
                ai_analysis JSONB,
                message_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        _sync_execute("""
            CREATE TABLE IF NOT EXISTS ai_logs (
                id SERIAL PRIMARY KEY,
                timestamp TEXT,
                user_id INTEGER,
                rapor_metni TEXT,
                ai_cevap TEXT,
                basarili INTEGER,
                hata_mesaji TEXT
            )
        """)
        
        update_database_schema()
        
        logging.info("✅ Yeni veritabanı yapısı başarıyla oluşturuldu")
        
    except Exception as e:
        logging.error(f"❌ Veritabanı başlatma hatası: {e}")
        raise e

init_database()
init_db_pool()

# ----------------------------- ŞANTİYE BAZLI RAPOR KONTROLÜ -----------------------------
async def get_santiye_rapor_durumu(bugun):
    """Bugünkü şantiye rapor durumu"""
    try:
        rows = await async_fetchall("""
            SELECT DISTINCT project_name FROM reports 
            WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (bugun,))
        
        return set(row[0] for row in rows if row[0])
    except Exception as e:
        logging.error(f"Şantiye rapor durumu hatası: {e}")
        return set()

async def get_eksik_santiyeler(bugun):
    """Raporu eksik olan şantiyeleri ve sorumlularını getir"""
    try:
        tum_santiyeler = set(santiye_sorumlulari.keys())
        rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
        eksik_santiyeler = tum_santiyeler - rapor_veren_santiyeler
        
        return {santiye: santiye_sorumlulari.get(santiye, []) for santiye in eksik_santiyeler}
    except Exception as e:
        logging.error(f"Eksik şantiye sorgu hatası: {e}")
        return {}

async def get_santiye_bazli_rapor_durumu(bugun):
    """Şantiye bazlı detaylı rapor durumu"""
    try:
        tum_santiyeler = set(santiye_sorumlulari.keys())
        rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
        
        rows = await async_fetchall("""
            SELECT project_name, user_id FROM reports 
            WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (bugun,))
        
        santiye_rapor_verenler = {}
        for project_name, user_id in rows:
            if project_name not in santiye_rapor_verenler:
                santiye_rapor_verenler[project_name] = []
            santiye_rapor_verenler[project_name].append(user_id)
    
        return {
            'tum_santiyeler': tum_santiyeler,
            'rapor_veren_santiyeler': rapor_veren_santiyeler,
            'eksik_santiyeler': tum_santiyeler - rapor_veren_santiyeler,
            'santiye_rapor_verenler': santiye_rapor_verenler
        }
    except Exception as e:
        logging.error(f"Şantiye bazlı rapor durumu hatası: {e}")
        return {'tum_santiyeler': set(), 'rapor_veren_santiyeler': set(), 'eksik_santiyeler': set(), 'santiye_rapor_verenler': {}}

# ----------------------------- MALİYET ANALİZİ -----------------------------
class MaliyetAnaliz:
    def __init__(self):
        self.gpt_count = 0
        self.fallback_count = 0
        
    def kayit_ekle(self, kaynak):
        if kaynak == 'gpt':
            self.gpt_count += 1
        else:
            self.fallback_count += 1
    
    def maliyet_raporu(self):
        toplam = self.gpt_count + self.fallback_count
        if toplam == 0:
            return "📊 Henüz işlem yok"
        
        gpt_orani = (self.gpt_count / toplam) * 100
        maliyet = self.gpt_count * 0.0015
        
        return (
            f"📊 **MALİYET ANALİZİ**\n\n"
            f"🤖 **GPT İşlemleri:** {self.gpt_count} (%{gpt_orani:.1f})\n"
            f"🔄 **Fallback:** {self.fallback_count}\n"
            f"💰 **Tahmini Maliyet:** ${maliyet:.4f}\n"
            f"🎯 **Başarı Oranı:** %{gpt_orani:.1f}"
        )
    
    def detayli_ai_raporu(self):
        """Detaylı AI kullanım raporu"""
        try:
            result = _sync_fetchone("""
                SELECT 
                    COUNT(*) as toplam,
                    SUM(CASE WHEN basarili = 1 THEN 1 ELSE 0 END) as basarili,
                    SUM(CASE WHEN basarili = 0 THEN 1 ELSE 0 END) as basarisiz,
                    MIN(timestamp) as ilk_tarih,
                    MAX(timestamp) as son_tarih
                FROM ai_logs
            """)
            
            if not result or result[0] == 0:
                return "🤖 **AI Raporu:** Henüz AI kullanımı yok"
            
            toplam, basarili, basarisiz, ilk_tarih, son_tarih = result
            
            rows = _sync_fetchall("""
                SELECT DATE(timestamp::timestamp) as gun, 
                       COUNT(*) as toplam,
                       SUM(CASE WHEN basarili = 1 THEN 1 ELSE 0 END) as basarili
                FROM ai_logs 
                WHERE timestamp::timestamp >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY DATE(timestamp::timestamp) 
                ORDER BY gun DESC
            """)
            
            rapor = "🤖 **DETAYLI AI RAPORU**\n\n"
            rapor += f"📈 **Genel İstatistikler:**\n"
            rapor += f"• Toplam İşlem: {toplam}\n"
            rapor += f"• Başarılı: {basarili} (%{(basarili/toplam*100):.1f})\n"
            rapor += f"• Başarısız: {basarisiz}\n"
            rapor += f"• İlk Kullanım: {ilk_tarih[:10] if ilk_tarih else 'Yok'}\n"
            rapor += f"• Son Kullanım: {son_tarih[:10] if son_tarih else 'Yok'}\n\n"
            
            rapor += f"📅 **Son 7 Gün:**\n"
            for gun, toplam_gun, basarili_gun in rows:
                oran = (basarili_gun/toplam_gun*100) if toplam_gun > 0 else 0
                rapor += f"• {gun}: {basarili_gun}/{toplam_gun} (%{oran:.1f})\n"
            
            return rapor
            
        except Exception as e:
            return f"❌ AI raporu oluşturulurken hata: {e}"

maliyet_analiz = MaliyetAnaliz()

# ----------------------------- TARİH FONKSİYONLARI -----------------------------
def parse_rapor_tarihi(metin):
    """Tarih parsing fonksiyonu"""
    try:
        bugun = dt.datetime.now(TZ).date()
        metin_lower = metin.lower()
        
        if 'bugün' in metin_lower or 'bugun' in metin_lower:
            return bugun
        if 'dün' in metin_lower or 'dun' in metin_lower:
            return bugun - dt.timedelta(days=1)
        
        date_patterns = [
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{4})',
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{2})',
            r'(\d{4})[\.\/\-](\d{1,2})[\.\/\-](\d{1,2})',
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, metin)
            if match:
                groups = match.groups()
                if len(groups) == 3:
                    if len(groups[2]) == 4:
                        day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
                    elif len(groups[0]) == 4:
                        year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                    else:
                        day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
                        year += 2000
                    
                    try:
                        parsed_date = dt.datetime(year, month, day).date()
                        if parsed_date <= bugun:
                            return parsed_date
                    except ValueError:
                        continue
        
        return None
    except Exception:
        return None

def izin_mi(metin):
    """Basit izin kontrolü"""
    metin_lower = metin.lower()
    izin_kelimeler = ['izin', 'rapor yok', 'iş yok', 'çalışma yok', 'tatil', 'hasta', 'izindeyim']
    return any(kelime in metin_lower for kelime in izin_kelimeler)

async def tarih_kontrol_et(rapor_tarihi, user_id):
    """Tarih kontrolü"""
    bugun = dt.datetime.now(TZ).date()
    
    if not rapor_tarihi:
        return False, "❌ **Tarih bulunamadı.** Lütfen raporunuzda tarih belirtiniz."
    
    if rapor_tarihi > bugun:
        return False, "❌ **Gelecek tarihli rapor.** Lütfen bugün veya geçmiş tarih kullanınız."
    
    iki_ay_once = bugun - dt.timedelta(days=60)
    if rapor_tarihi < iki_ay_once:
        return False, "❌ **Çok eski tarihli rapor.** Lütfen son 2 ay içinde bir tarih kullanınız."
    
    result = await async_fetchone("SELECT EXISTS(SELECT 1 FROM reports WHERE user_id = %s AND report_date = %s)", 
                  (user_id, rapor_tarihi))
    
    if result and result[0]:
        return False, "❌ **Bu tarih için zaten rapor gönderdiniz.**"
    
    return True, ""

def parse_tr_date(date_str):
    """Tüm tarih formatlarını destekle"""
    try:
        normalized_date = date_str.replace('/', '.').replace('-', '.')
        parts = normalized_date.split('.')
        if len(parts) == 3:
            if len(parts[2]) == 4:
                return dt.datetime.strptime(normalized_date, '%d.%m.%Y').date()
            elif len(parts[0]) == 4:
                return dt.datetime.strptime(normalized_date, '%Y.%m.%d').date()
        raise ValueError("Geçersiz tarih formatı")
    except:
        raise ValueError("Geçersiz tarih formatı")

def week_window_to_today():
    """Bugünden geriye doğru 7 günlük pencere"""
    end_date = dt.datetime.now(TZ).date()
    start_date = end_date - dt.timedelta(days=6)
    return start_date, end_date

# ----------------------------- YARDIMCI FONKSİYONLAR -----------------------------
def is_admin(user_id):
    return user_id in ADMINS

def is_super_admin(user_id):
    return user_id == SUPER_ADMIN_ID

def is_izleyici(user_id):
    return user_id in IZLEYICILER

async def admin_kontrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Bu komut sadece yöneticiler içindir.")
        return False
    return True

async def super_admin_kontrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_super_admin(user_id):
        await update.message.reply_text("❌ Bu komut sadece Super Admin içindir.")
        return False
    return True

async def hata_bildirimi(context: ContextTypes.DEFAULT_TYPE, hata_mesaji: str):
    """Hata mesajını adminlere gönder"""
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ **Sistem Hatası**: {hata_mesaji}"
            )
            await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Hata bildirimi {admin_id} adminine gönderilemedi: {e}")

# ----------------------------- EKSİK FONKSİYONLARI EKLE -----------------------------
async def generate_gelismis_personel_ozeti(target_date):
    """📊 Günlük personel özeti oluştur"""
    try:
        rows = await async_fetchall("""
            SELECT user_id, report_type, project_name, person_count, work_description
            FROM reports WHERE report_date = %s
        """, (target_date,))
        
        if not rows:
            return f"📭 **{target_date.strftime('%d.%m.%Y')}** tarihinde rapor bulunamadı."
        
        proje_analizleri = {}
        tum_projeler = set()
        
        for user_id, rapor_tipi, proje_adi, kisi_sayisi, yapilan_is in rows:
            if not proje_adi:
                proje_adi = 'BELİRSİZ'
                
            if proje_adi not in proje_analizleri:
                proje_analizleri[proje_adi] = {
                    'toplam_kisi': 0, 'calisan': 0, 'izinli': 0, 'hastalik': 0
                }
            
            if rapor_tipi == "RAPOR":
                proje_analizleri[proje_adi]['calisan'] += kisi_sayisi
            elif rapor_tipi == "IZIN/ISYOK":
                if 'hasta' in (yapilan_is or '').lower():
                    proje_analizleri[proje_adi]['hastalik'] += kisi_sayisi
                else:
                    proje_analizleri[proje_adi]['izinli'] += kisi_sayisi
            
            proje_analizleri[proje_adi]['toplam_kisi'] += kisi_sayisi
            tum_projeler.add(proje_adi)
        
        mesaj = f"📊 {target_date.strftime('%d.%m.%Y')} GÜNLÜK PERSONEL ÖZETİ\n\n"
        
        genel_toplam = 0
        genel_calisan = 0
        genel_izinli = 0
        genel_hastalik = 0
        
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: x[1]['toplam_kisi'], reverse=True):
            if analiz['toplam_kisi'] > 0:
                genel_toplam += analiz['toplam_kisi']
                genel_calisan += analiz['calisan']
                genel_izinli += analiz['izinli']
                genel_hastalik += analiz['hastalik']
                
                emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                mesaj += f"{emoji} **{proje_adi}**: {analiz['toplam_kisi']} kişi\n"
                
                durum_detay = []
                if analiz['calisan'] > 0: 
                    durum_detay.append(f"Çalışan:{analiz['calisan']}")
                if analiz['izinli'] > 0: 
                    durum_detay.append(f"İzinli:{analiz['izinli']}")
                if analiz['hastalik'] > 0: 
                    durum_detay.append(f"Hastalık:{analiz['hastalik']}")
                
                if durum_detay:
                    mesaj += f"   └─ {', '.join(durum_detay)}\n\n"
        
        mesaj += f"📈 **GENEL TOPLAM**: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 **DAĞILIM**: \n"
            mesaj += f"   • Çalışan: {genel_calisan} kişi (%{genel_calisan/genel_toplam*100:.0f})\n"
            if genel_izinli > 0:
                mesaj += f"   • İzinli: {genel_izinli} kişi (%{genel_izinli/genel_toplam*100:.0f})\n"
            if genel_hastalik > 0:
                mesaj += f"   • Hastalık: {genel_hastalik} kişi (%{genel_hastalik/genel_toplam*100:.0f})\n"
        
        eksik_projeler = tum_projeler - set(proje_analizleri.keys())
        if eksik_projeler:
            mesaj += f"\n❌ **EKSİK**: {', '.join(sorted(eksik_projeler))}"
        
        return mesaj
    except Exception as e:
        return f"❌ Rapor oluşturulurken hata oluştu: {e}"

async def generate_haftalik_rapor_mesaji(start_date, end_date):
    """Haftalık rapor mesajı oluştur"""
    try:
        rows = await async_fetchall("""
            SELECT user_id, COUNT(*) as rapor_sayisi,
                   SUM(CASE WHEN report_type = 'RAPOR' THEN 1 ELSE 0 END) as calisma_raporu
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
            GROUP BY user_id
            ORDER BY rapor_sayisi DESC
        """, (start_date, end_date))
        
        if not rows:
            return f"📭 **{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}** arasında rapor bulunamadı."
        
        toplam_rapor = sum([x[1] for x in rows])
        toplam_calisma_raporu = sum([x[2] for x in rows])
        gun_sayisi = (end_date - start_date).days + 1
        beklenen_rapor = len(rapor_sorumlulari) * gun_sayisi
        verimlilik = (toplam_rapor / beklenen_rapor * 100) if beklenen_rapor > 0 else 0
        
        en_aktif = rows[:3]
        
        proje_rows = await async_fetchall("""
            SELECT project_name, SUM(person_count) as toplam_kisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND report_type = 'RAPOR'
            GROUP BY project_name
            ORDER BY toplam_kisi DESC
        """, (start_date, end_date))
        
        mesaj = f"📈 **HAFTALIK ÖZET RAPOR**\n"
        mesaj += f"*{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}*\n\n"
        
        mesaj += f"📊 **GENEL İSTATİSTİKLER**:\n"
        mesaj += f"   • 📨 Toplam Rapor: **{toplam_rapor}**\n"
        mesaj += f"   • ✅ Çalışma Raporu: **{toplam_calisma_raporu}**\n"
        mesaj += f"   • 👥 Rapor Gönderen: **{len(rows)}** kişi\n"
        mesaj += f"   • 📅 İş Günü: **{gun_sayisi}** gün\n"
        mesaj += f"   • 🎯 Verimlilik: **%{verimlilik:.1f}**\n\n"
        
        mesaj += f"🔝 **EN AKTİF 3 KULLANICI**:\n"
        for i, (user_id, rapor_sayisi, calisma_raporu) in enumerate(en_aktif, 1):
            kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉"
            gunluk_ortalama = rapor_sayisi / gun_sayisi
            mesaj += f"   {emoji} **{kullanici_adi}**: {rapor_sayisi} rapor (günlük: {gunluk_ortalama:.1f})\n"
        
        mesaj += f"\n🏗️ **PROJE BAZLI PERSONEL**:\n"
        for proje_adi, toplam_kisi in proje_rows:
            if toplam_kisi > 0:
                emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                mesaj += f"   {emoji} **{proje_adi}**: {toplam_kisi} kişi\n"
        
        return mesaj
    except Exception as e:
        return f"❌ Haftalık rapor oluşturulurken hata: {e}"

async def generate_aylik_rapor_mesaji(start_date, end_date):
    """Aylık rapor mesajı oluştur"""
    try:
        rows = await async_fetchall("""
            SELECT user_id, COUNT(*) as rapor_sayisi,
                   SUM(CASE WHEN report_type = 'RAPOR' THEN 1 ELSE 0 END) as calisma_raporu
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
            GROUP BY user_id
            ORDER BY rapor_sayisi DESC
        """, (start_date, end_date))
        
        if not rows:
            return f"📭 **{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}** arasında rapor bulunamadı."
        
        toplam_rapor = sum([x[1] for x in rows])
        toplam_calisma_raporu = sum([x[2] for x in rows])
        gun_sayisi = (end_date - start_date).days + 1
        
        en_aktif = rows[:3]
        en_pasif = [x for x in rows if x[1] < gun_sayisi * 0.5]
        
        mesaj = f"🗓️ **AYLIK ÖZET RAPOR**\n"
        mesaj += f"*{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}*\n\n"
        
        mesaj += f"📈 **PERFORMANS ANALİZİ**:\n"
        mesaj += f"   • 📊 Toplam Rapor: **{toplam_rapor}**\n"
        mesaj += f"   • ✅ Çalışma Raporu: **{toplam_calisma_raporu}**\n"
        mesaj += f"   • 📉 Pasif Kullanıcı: **{len(en_pasif)}**\n"
        mesaj += f"   • 📅 İş Günü: **{gun_sayisi}** gün\n"
        mesaj += f"   • 📨 Günlük Ort.: **{toplam_rapor/gun_sayisi:.1f}** rapor\n\n"
        
        mesaj += f"🔝 **EN AKTİF 3 KULLANICI**:\n"
        for i, (user_id, rapor_sayisi, calisma_raporu) in enumerate(en_aktif, 1):
            kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉"
            gunluk_ortalama = rapor_sayisi / gun_sayisi
            mesaj += f"   {emoji} **{kullanici_adi}**: {rapor_sayisi} rapor (günlük: {gunluk_ortalama:.1f})\n"
        
        if en_pasif:
            mesaj += f"\n🔴 **DÜŞÜK PERFORMANS** (<%50 katılım):\n"
            for i, (user_id, rapor_sayisi, calisma_raporu) in enumerate(en_pasif[:3], 1):
                kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                katilim_orani = (rapor_sayisi / gun_sayisi) * 100
                emoji = "1️⃣" if i == 1 else "2️⃣" if i == 2 else "3️⃣"
                mesaj += f"   {emoji} **{kullanici_adi}**: {rapor_sayisi} rapor (%{katilim_orani:.1f})\n"
        
        return mesaj
    except Exception as e:
        return f"❌ Aylık rapor oluşturulurken hata: {e}"

async def generate_tarih_araligi_raporu(start_date, end_date):
    """Tarih aralığı raporu oluştur"""
    try:
        rows = await async_fetchall("""
            SELECT user_id, COUNT(*) as rapor_sayisi,
                   SUM(CASE WHEN report_type = 'RAPOR' THEN 1 ELSE 0 END) as calisma_raporu
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
            GROUP BY user_id
            ORDER BY rapor_sayisi DESC
        """, (start_date, end_date))
        
        if not rows:
            return f"📭 **{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}** arasında rapor bulunamadı."
        
        toplam_rapor = sum([x[1] for x in rows])
        toplam_calisma_raporu = sum([x[2] for x in rows])
        gun_sayisi = (end_date - start_date).days + 1
        
        en_aktif = rows[:3]
        
        personel_result = await async_fetchone("""
            SELECT SUM(person_count) as toplam_kisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND report_type = 'RAPOR'
        """, (start_date, end_date))
        
        toplam_personel = personel_result[0] or 0
        
        mesaj = f"📅 **TARİH ARALIĞI RAPORU**\n"
        mesaj += f"*{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}*\n\n"
        
        mesaj += f"📊 **GENEL İSTATİSTİKLER**:\n"
        mesaj += f"   • 📨 Toplam Rapor: **{toplam_rapor}**\n"
        mesaj += f"   • ✅ Çalışma Raporu: **{toplam_calisma_raporu}**\n"
        mesaj += f"   • 👥 Rapor Gönderen: **{len(rows)}** kişi\n"
        mesaj += f"   • 📅 Gün Sayısı: **{gun_sayisi}** gün\n"
        mesaj += f"   • 📨 Günlük Ort.: **{toplam_rapor/gun_sayisi:.1f}** rapor\n"
        mesaj += f"   • 👷 Toplam Personel: **{toplam_personel}** kişi\n\n"
        
        mesaj += f"🔝 **EN AKTİF 3 KULLANICI**:\n"
        for i, (user_id, rapor_sayisi, calisma_raporu) in enumerate(en_aktif, 1):
            kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉"
            gunluk_ortalama = rapor_sayisi / gun_sayisi
            mesaj += f"   {emoji} **{kullanici_adi}**: {rapor_sayisi} rapor (günlük: {gunluk_ortalama:.1f})\n"
        
        return mesaj
    except Exception as e:
        return f"❌ Tarih aralığı raporu oluşturulurken hata: {e}"

# ----------------------------- KOMUTLAR -----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Rapor Botu Aktif!**\n\n"
        "Komutlar için `/info` yazın.\n\n"
        "📋 **Temel Kullanım:**\n"
        "• Rapor göndermek için direkt mesaj yazın\n"
        "• `/info` - Tüm komutları görüntüle\n"
        "• `/hakkinda` - Bot hakkında bilgi"
    )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tüm kullanıcılar için komut listesi"""
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name
    
    if is_admin(user_id):
        info_text = (
            f"🤖 **Yapay Zeka Destekli Rapor Botu**\n\n"
            f"👋 Hoş geldiniz {user_name}!\n\n"
            f"📋 **Tüm Kullanıcılar İçin:**\n"
            f"• Rapor göndermek için direkt mesaj yazın\n"
            f"`/start` - Botu başlat\n"
            f"`/info` - Komut bilgisi\n"
            f"`/hakkinda` - Bot hakkında\n\n"
            f"🛡️ **Admin Komutları:**\n"
            f"`/bugun` - Bugünün özeti\n"
            f"`/dun` - Dünün özeti\n"
            f"`/haftalik_rapor` - Haftalık rapor\n"
            f"`/aylik_rapor` - Aylık rapor\n"
            f"`/tariharaligi [baslangic] [bitis]` - Tarih aralığı raporu\n"
            f"`/haftalik_istatistik` - Haftalık istatistik\n"
            f"`/aylik_istatistik` - Aylık istatistik\n"
            f"`/excel_tariharaligi [baslangic] [bitis]` - Excel raporu\n"
            f"`/maliyet` - Maliyet analizi\n"
            f"`/ai_rapor` - Detaylı AI raporu\n"
            f"`/kullanicilar` - Tüm kullanıcı listesi\n"
            f"`/santiyeler` - Şantiye listesi\n"
            f"`/santiye_durum` - Şantiye rapor durumu\n\n"
            f"⚡ **Super Admin Komutları:**\n"
            f"`/reload` - Excel dosyasını yenile\n"
            f"`/yedekle` - Manuel yedekleme\n"
            f"`/chatid` - Chat ID göster\n\n"
            f"🔒 **Not:** Komutlar yetkinize göre çalışacaktır."
        )
    else:
        info_text = (
            f"🤖 **Yapay Zeka Destekli Rapor Botu**\n\n"
            f"👋 Hoş geldiniz {user_name}!\n\n"
            f"📋 **Kullanıcı Komutları:**\n"
            f"• Rapor göndermek için direkt mesaj yazın\n"
            f"`/start` - Botu başlat\n"
            f"`/info` - Komut bilgisi\n"
            f"`/hakkinda` - Bot hakkında\n\n"
            f"🔒 **Admin komutları sadece yetkililer içindir.**"
        )
    
    await update.message.reply_text(info_text, parse_mode='Markdown')

async def hakkinda_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot hakkında bilgi"""
    hakkinda_text = (
        "🤖 **Rapor Botu Hakkında**\n\n"
        "**Geliştirici:** Atamurat Kamalov\n"
        "**Versiyon:** 3.0 (Yeni Veritabanı Yapısı)\n"
        "**Özellikler:**\n"
        "• Yapay Zeka destekli rapor analizi\n"
        "• Optimize edilmiş veritabanı\n"
        "• Otomatik hatırlatma sistemi\n"
        "• Excel raporları\n"
        "• Yandex.Disk yedekleme\n"
        "• Gerçek zamanlı takip\n\n"
        "💡 **Teknoloji:** Python, PostgreSQL, OpenAI GPT-4\n"
        "⚡ **Performans:** Optimize edilmiş sorgular"
    )
    await update.message.reply_text(hakkinda_text, parse_mode='Markdown')

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Chat ID göster - Sadece Super Admin"""
    if not await super_admin_kontrol(update, context):
        return
    
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    await update.message.reply_text(
        f"📋 **Chat ID Bilgileri:**\n\n"
        f"👤 **Kullanıcı ID:** `{user_id}`\n"
        f"💬 **Chat ID:** `{chat_id}`\n"
        f"👥 **Grup ID:** `{GROUP_ID}`"
    )

async def bugun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bugünün rapor özeti"""
    if not await admin_kontrol(update, context):
        return
    
    target_date = dt.datetime.now(TZ).date()
    await update.message.chat.send_action(action="typing")
    rapor_mesaji = await generate_gelismis_personel_ozeti(target_date)
    await update.message.reply_text(rapor_mesaji)

async def dun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dünün rapor özeti"""
    if not await admin_kontrol(update, context):
        return
    
    target_date = dt.datetime.now(TZ).date() - dt.timedelta(days=1)
    await update.message.chat.send_action(action="typing")
    rapor_mesaji = await generate_gelismis_personel_ozeti(target_date)
    await update.message.reply_text(rapor_mesaji)

async def haftalik_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Haftalık rapor komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today - dt.timedelta(days=today.weekday())
    end_date = start_date + dt.timedelta(days=6)
    
    mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def aylik_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aylık rapor komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today.replace(day=1)
    end_date = today
    
    mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def haftalik_istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Haftalık istatistik komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today - dt.timedelta(days=today.weekday())
    end_date = start_date + dt.timedelta(days=6)
    
    mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def aylik_istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aylık istatistik komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today.replace(day=1)
    end_date = today
    
    mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def tariharaligi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📅 Tarih aralığı özet raporu - Sadece Admin"""
    if not await admin_kontrol(update, context):
        return
    
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "📅 **Tarih Aralığı Kullanımı:**\n\n"
            "`/tariharaligi 01.11.2024 15.11.2024`\n"
            "Belirtilen tarih aralığı için detaylı rapor oluşturur."
        )
        return
    
    await update.message.chat.send_action(action="typing")
    
    try:
        start_date = parse_tr_date(context.args[0])
        end_date = parse_tr_date(context.args[1])
        
        if start_date > end_date:
            await update.message.reply_text("❌ Başlangıç tarihi bitiş tarihinden büyük olamaz.")
            return
        
        mesaj = await generate_tarih_araligi_raporu(start_date, end_date)
        
        await update.message.reply_text(mesaj, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text("❌ Tarih formatı hatalı. GG.AA.YYYY şeklinde girin.")

async def excel_tariharaligi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Excel tarih aralığı raporu"""
    if not await admin_kontrol(update, context):
        return

    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "📅 **Excel Tarih Aralığı Raporu**\n\n"
            "Kullanım: `/excel_tariharaligi 01.11.2024 15.11.2024`\n"
            "Belirtilen tarih aralığı için Excel raporu oluşturur."
        )
        return

    await update.message.reply_text("⌛ Excel raporu hazırlanıyor...")

    try:
        tarih1 = context.args[0].replace('/', '.').replace('-', '.')
        tarih2 = context.args[1].replace('/', '.').replace('-', '.')
        
        start_date = parse_tr_date(tarih1)
        end_date = parse_tr_date(tarih2)
        
        if start_date > end_date:
            await update.message.reply_text("❌ Başlangıç tarihi bitiş tarihinden büyük olamaz.")
            return

        mesaj = await generate_tarih_araligi_raporu(start_date, end_date)
        excel_dosyasi = await create_excel_report(start_date, end_date, 
                                                 f"Tarih_Araligi_{start_date.strftime('%d.%m.%Y')}_{end_date.strftime('%d.%m.%Y')}")

        await update.message.reply_text(mesaj, parse_mode='Markdown')
        
        with open(excel_dosyasi, 'rb') as file:
            await update.message.reply_document(
                document=file,
                filename=f"Rapor_{start_date.strftime('%d.%m.%Y')}_{end_date.strftime('%d.%m.%Y')}.xlsx",
                caption=f"📊 Tarih Aralığı Raporu: {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"
            )
        
        os.unlink(excel_dosyasi)
        
    except Exception as e:
        await update.message.reply_text("❌ Tarih formatı hatalı. GG.AA.YYYY şeklinde girin.")
        logging.error(f"Excel tarih aralığı rapor hatası: {e}")

async def kullanicilar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kullanıcı listesi"""
    if not await admin_kontrol(update, context):
        return
    
    mesaj = "👥 **TÜM KULLANICI LİSTESİ**\n\n"
    
    mesaj += f"📋 **Rapor Sorumluları** ({len(rapor_sorumlulari)}):\n"
    for tid in rapor_sorumlulari:
        ad = id_to_name.get(tid, "Bilinmeyen")
        projeler = ", ".join(id_to_projects.get(tid, []))
        status = id_to_status.get(tid, "Belirsiz")
        rol = id_to_rol.get(tid, "Belirsiz")
        mesaj += f"• **{ad}**\n  📍 Projeler: {projeler}\n  🏷️ Status: {status}\n  👤 Rol: {rol}\n\n"
    
    admin_rapor_olmayanlar = [admin for admin in ADMINS if admin not in rapor_sorumlulari]
    if admin_rapor_olmayanlar:
        mesaj += f"🛡️ **Adminler** ({len(admin_rapor_olmayanlar)}):\n"
        for tid in admin_rapor_olmayanlar:
            ad = id_to_name.get(tid, "Bilinmeyen")
            rol = id_to_rol.get(tid, "Belirsiz")
            mesaj += f"• **{ad}** - {rol}\n"
        mesaj += "\n"
    
    if IZLEYICILER:
        mesaj += f"👀 **İzleyiciler** ({len(IZLEYICILER)}):\n"
        for tid in IZLEYICILER:
            ad = id_to_name.get(tid, "Bilinmeyen")
            mesaj += f"• **{ad}**\n"
    
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def santiyeler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Şantiye listesi ve sorumlularını göster"""
    if not await admin_kontrol(update, context):
        return
    
    mesaj = "🏗️ **ŞANTİYE LİSTESİ ve SORUMLULARI**\n\n"
    
    for santiye, sorumlular in sorted(santiye_sorumlulari.items()):
        sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
        mesaj += f"**{santiye}**\n"
        mesaj += f"  👥 Sorumlular: {', '.join(sorumlu_isimler)}\n\n"
    
    mesaj += f"📊 Toplam {len(santiye_sorumlulari)} şantiye"
    
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def santiye_durum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Günlük şantiye rapor durumu"""
    if not await admin_kontrol(update, context):
        return
    
    bugun = dt.datetime.now(TZ).date()
    durum = await get_santiye_bazli_rapor_durumu(bugun)
    
    mesaj = f"📊 **Şantiye Rapor Durumu - {bugun.strftime('%d.%m.%Y')}**\n\n"
    
    mesaj += f"✅ **Rapor İleten Şantiyeler** ({len(durum['rapor_veren_santiyeler'])}):\n"
    for santiye in sorted(durum['rapor_veren_santiyeler']):
        rapor_verenler = durum['santiye_rapor_verenler'].get(santiye, [])
        rapor_veren_isimler = [id_to_name.get(uid, f"Kullanıcı {uid}") for uid in rapor_verenler]
        
        if rapor_verenler:
            mesaj += f"• **{santiye}** - İleten: {', '.join(rapor_veren_isimler)}\n"
        else:
            mesaj += f"• **{santiye}** - Rapor iletildi\n"
    
    mesaj += f"\n❌ **Rapor İletilmeyen Şantiyeler** ({len(durum['eksik_santiyeler'])}):\n"
    for santiye in sorted(durum['eksik_santiyeler']):
        sorumlular = santiye_sorumlulari.get(santiye, [])
        sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
        mesaj += f"• **{santiye}** - Sorumlular: {', '.join(sorumlu_isimler)}\n"
    
    mesaj += f"\n📈 Özet: {len(durum['rapor_veren_santiyeler'])}/{len(durum['tum_santiyeler'])} şantiye rapor iletmiş"
    
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def maliyet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maliyet analizi"""
    if not await admin_kontrol(update, context):
        return
    
    rapor = maliyet_analiz.maliyet_raporu()
    await update.message.reply_text(rapor, parse_mode='Markdown')

async def ai_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🤖 Detaylı AI kullanım raporu - Sadece Admin"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    rapor = maliyet_analiz.detayli_ai_raporu()
    await update.message.reply_text(rapor, parse_mode='Markdown')

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Excel yenileme"""
    if not await super_admin_kontrol(update, context):
        return
    
    load_excel()
    await update.message.reply_text("✅ Excel dosyası yeniden yüklendi!")

# ----------------------------- RAPOR ÜRETİCİ FONKSİYONLAR -----------------------------
async def create_excel_report(start_date, end_date, rapor_baslik):
    """Excel rapor oluştur"""
    try:
        rows = await async_fetchall("""
            SELECT r.user_id, r.report_date, r.report_type, r.work_description, 
                   r.person_count, r.project_name, r.work_category, r.personnel_type,
                   r.delivered_date, r.is_edited
            FROM reports r
            WHERE r.report_date BETWEEN %s AND %s
            ORDER BY r.report_date, r.user_id
        """, (start_date, end_date))
        
        if not rows:
            raise Exception("Belirtilen tarih aralığında rapor bulunamadı")
        
        excel_data = []
        for user_id, tarih, rapor_tipi, icerik, kisi_sayisi, proje_adi, is_kategorisi, personel_tipi, delivered_date, is_edited in rows:
            kullanici_adi = id_to_name.get(user_id, f"Kullanıcı")
            
            try:
                rapor_tarihi = tarih.strftime('%d.%m.%Y') if isinstance(tarih, dt.datetime) else str(tarih)
                gonderme_tarihi = delivered_date.strftime('%d.%m.%Y') if delivered_date and isinstance(delivered_date, dt.datetime) else str(delivered_date) if delivered_date else ""
            except:
                rapor_tarihi = str(tarih)
                gonderme_tarihi = str(delivered_date) if delivered_date else ""
            
            excel_data.append({
                'Tarih': rapor_tarihi,
                'Kullanıcı': kullanici_adi,
                'Rapor Tipi': rapor_tipi,
                'Kişi Sayısı': kisi_sayisi,
                'Proje': proje_adi or 'BELİRSİZ',
                'İş Kategorisi': is_kategorisi or '',
                'Personel Tipi': personel_tipi or '',
                'Yapılan İş': icerik[:100] + '...' if len(icerik) > 100 else icerik,
                'Gönderilme Tarihi': gonderme_tarihi,
                'Düzenlendi mi?': 'Evet' if is_edited else 'Hayır',
                'User ID': user_id
            })
        
        wb = Workbook()
        ws = wb.active
        ws.title = "Raporlar"
        
        headers = ['Tarih', 'Kullanıcı', 'Rapor Tipi', 'Kişi Sayısı', 'Proje', 'İş Kategorisi', 
                  'Personel Tipi', 'Yapılan İş', 'Gönderilme Tarihi', 'Düzenlendi mi?', 'User ID']
        
        header_font = Font(bold=True, color="FFFFFF", size=12)
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
        center_align = Alignment(horizontal='center', vertical='center')
        
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center_align
            cell.border = border
        
        for row_idx, row_data in enumerate(excel_data, 2):
            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=row_data.get(header, ''))
                cell.border = border
                if header == 'Rapor Tipi':
                    if row_data['Rapor Tipi'] == 'RAPOR':
                        cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                    else:
                        cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        
        column_widths = {'A': 12, 'B': 20, 'C': 12, 'D': 12, 'E': 20, 'F': 15, 'G': 15, 'H': 40, 'I': 15, 'J': 12, 'K': 10}
        for col, width in column_widths.items():
            ws.column_dimensions[col].width = width
        
        ws_summary = wb.create_sheet("Özet")
        toplam_rapor = len(excel_data)
        toplam_kullanici = len(set([x['User ID'] for x in excel_data]))
        gun_sayisi = len(set([x['Tarih'] for x in excel_data]))
        
        ws_summary.merge_cells('A1:D1')
        ws_summary['A1'] = f"📊 RAPOR ÖZETİ - {rapor_baslik}"
        ws_summary['A1'].font = Font(bold=True, size=14, color="366092")
        ws_summary['A1'].alignment = center_align
        
        summary_data = [
            ['📅 Rapor Periyodu', f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"],
            ['📊 Toplam Rapor', toplam_rapor],
            ['👥 Toplam Kullanıcı', toplam_kullanici],
            ['📅 İş Günü Sayısı', gun_sayisi],
            ['🕒 Oluşturulma', dt.datetime.now(TZ).strftime('%d.%m.%Y %H:%M')]
        ]
        
        for row_idx, (label, value) in enumerate(summary_data, 3):
            ws_summary[f'A{row_idx}'] = label
            ws_summary[f'B{row_idx}'] = value
            ws_summary[f'A{row_idx}'].font = Font(bold=True)
            ws_summary[f'B{row_idx}'].border = border
        
        ws_summary.column_dimensions['A'].width = 25
        ws_summary.column_dimensions['B'].width = 15
        
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        wb.save(temp_file.name)
        return temp_file.name
    except Exception as e:
        raise e

# ----------------------------- ZAMANLAMA -----------------------------
def schedule_jobs(app):
    """Zamanlanmış görevleri ayarla"""
    jq = app.job_queue
    
    jq.run_repeating(auto_watch_excel, interval=60, first=10)
    jq.run_daily(gunluk_rapor_ozeti, time=dt.time(9, 0, tzinfo=TZ))
    
    jq.run_daily(hatirlatma_mesaji, time=dt.time(12, 30, tzinfo=TZ))
    jq.run_daily(ilk_rapor_kontrol, time=dt.time(15, 0, tzinfo=TZ))
    jq.run_daily(son_rapor_kontrol, time=dt.time(17, 30, tzinfo=TZ))
    
    jq.run_daily(yandex_yedekleme_gorevi, time=dt.time(23, 0, tzinfo=TZ))
    
    jq.run_daily(haftalik_grup_raporu, time=dt.time(17, 40, tzinfo=TZ), days=(4,))
    
    jq.run_monthly(aylik_grup_raporu, when=dt.time(17, 45, tzinfo=TZ), day=28)
    
    logging.info("⏰ Tüm zamanlamalar ayarlandı")

async def auto_watch_excel(context: ContextTypes.DEFAULT_TYPE):
    """Excel dosyası otomatik izleme"""
    global last_excel_update
    try:
        if os.path.exists(USERS_FILE):
            current_mtime = os.path.getmtime(USERS_FILE)
            if current_mtime > last_excel_update:
                load_excel()
                logging.info("Excel dosyası otomatik yenilendi")
    except Exception as e:
        logging.error(f"Excel otomatik izleme hatası: {e}")

async def gunluk_rapor_ozeti(context: ContextTypes.DEFAULT_TYPE):
    """🕘 09:00 - Sadece Eren ve Atamurat'a DM gönder"""
    try:
        dun = (dt.datetime.now(TZ) - dt.timedelta(days=1)).date()
        rapor_mesaji = await generate_gelismis_personel_ozeti(dun)
        
        hedef_kullanicilar = [709746899, 1000157326]
        
        for user_id in hedef_kullanicilar:
            try:
                await context.bot.send_message(chat_id=user_id, text=rapor_mesaji)
                logging.info(f"🕘 09:00 özeti {user_id} kullanıcısına gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"🕘 {user_id} kullanıcısına özet gönderilemedi: {e}")
                
    except Exception as e:
        logging.error(f"🕘 09:00 rapor hatası: {e}")
        await hata_bildirimi(context, f"09:00 rapor hatası: {e}")

async def hatirlatma_mesaji(context: ContextTypes.DEFAULT_TYPE):
    """🟡 12:30 - Gün ortası şantiye bazlı hatırlatma mesajı"""
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        if not durum['eksik_santiyeler']:
            logging.info("🟡 12:30 - Tüm şantiyeler raporunu göndermiş")
            return
        
        mesaj = "🔔 **Günlük Hatırlatma (Şantiye Bazlı)**\n\n"
        mesaj += "Raporu henüz iletilmeyen şantiyeler:\n"
        
        for santiye in sorted(durum['eksik_santiyeler']):
            sorumlular = santiye_sorumlulari.get(santiye, [])
            sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
            mesaj += f"• **{santiye}** - Sorumlular: {', '.join(sorumlu_isimler)}\n"
        
        mesaj += "\n⏰ Lütfen şantiye raporunuzu en geç 15:00'e kadar iletilmiş olun!"
        
        for user_id in rapor_sorumlulari:
            try:
                await context.bot.send_message(chat_id=user_id, text=mesaj)
                logging.info(f"🟡 Şantiye hatırlatma mesajı {user_id} kullanıcısına gönderildi")
                await asyncio.sleep(0.3)
            except Exception as e:
                logging.error(f"🟡 {user_id} kullanıcısına şantiye hatırlatma gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🟡 Şantiye hatırlatma mesajı hatası: {e}")
        await hata_bildirimi(context, f"Şantiye hatırlatma mesajı hatası: {e}")

async def ilk_rapor_kontrol(context: ContextTypes.DEFAULT_TYPE):
    """🟠 15:00 - İlk rapor kontrolü (şantiye bazlı)"""
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        mesaj = "🕒 **15:00 Şantiye Rapor Durumu**\n\n"
        
        if durum['rapor_veren_santiyeler']:
            mesaj += f"✅ **Rapor iletilen şantiyeler** ({len(durum['rapor_veren_santiyeler'])}):\n"
            for santiye in sorted(durum['rapor_veren_santiyeler']):
                rapor_verenler = durum['santiye_rapor_verenler'].get(santiye, [])
                rapor_veren_isimler = [id_to_name.get(uid, f"Kullanıcı {uid}") for uid in rapor_verenler]
                
                if rapor_verenler:
                    mesaj += f"• **{santiye}** - Rapor ileten: {', '.join(rapor_veren_isimler)}\n"
                else:
                    mesaj += f"• **{santiye}** - Rapor iletildi\n"
            mesaj += "\n"
        else:
            mesaj += "✅ **Rapor iletilen şantiyeler** (0):\n\n"
        
        if durum['eksik_santiyeler']:
            mesaj += f"❌ **Rapor iletilmeyen şantiyeler** ({len(durum['eksik_santiyeler'])}):\n"
            for santiye in sorted(durum['eksik_santiyeler']):
                sorumlular = santiye_sorumlulari.get(santiye, [])
                sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
                mesaj += f"• **{santiye}** - Sorumlular: {', '.join(sorumlu_isimler)}\n"
        else:
            mesaj += "❌ **Rapor iletilmeyen şantiyeler** (0):\n"
            mesaj += "🎉 Tüm şantiyeler raporlarını iletti!"
        
        for user_id in rapor_sorumlulari:
            try:
                await context.bot.send_message(chat_id=user_id, text=mesaj)
                logging.info(f"🟠 Şantiye kontrol mesajı {user_id} kullanıcısına gönderildi")
                await asyncio.sleep(0.3)
            except Exception as e:
                logging.error(f"🟠 {user_id} kullanıcısına şantiye kontrol mesajı gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🟠 Şantiye rapor kontrol hatası: {e}")
        await hata_bildirimi(context, f"Şantiye rapor kontrol hatası: {e}")

async def son_rapor_kontrol(context: ContextTypes.DEFAULT_TYPE):
    """🔴 17:30 - Gün sonu şantiye bazlı rapor analizi"""
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        result = await async_fetchone("SELECT COUNT(*) FROM reports WHERE report_date = %s", (bugun,))
        toplam_rapor = result[0] if result else 0
        
        mesaj = "🕠 **Gün Sonu Şantiye Rapor Analizi**\n\n"
        
        if durum['eksik_santiyeler']:
            mesaj += f"❌ **Rapor İletilmeyen Şantiyeler** ({len(durum['eksik_santiyeler'])}):\n"
            for santiye in sorted(durum['eksik_santiyeler']):
                sorumlular = santiye_sorumlulari.get(santiye, [])
                sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
                mesaj += f"• **{santiye}** - Sorumlular: {', '.join(sorumlu_isimler)}\n"
        else:
            mesaj += "❌ **Rapor İletilmeyen Şantiyeler** (0):\n"
            mesaj += "🎉 Tüm şantiyeler raporlarını iletti!\n"
        
        mesaj += f"\n📊 Bugün toplam **{toplam_rapor}** rapor alındı."
        mesaj += f"\n🏗️ **{len(durum['rapor_veren_santiyeler'])}/{len(durum['tum_santiyeler'])}** şantiye rapor iletmiş durumda."
        
        for user_id in rapor_sorumlulari:
            try:
                await context.bot.send_message(chat_id=user_id, text=mesaj)
                logging.info(f"🔴 Şantiye gün sonu analizi {user_id} kullanıcısına gönderildi")
                await asyncio.sleep(0.3)
            except Exception as e:
                logging.error(f"🔴 {user_id} kullanıcısına şantiye gün sonu analizi gönderilemedi: {e}")
        
        admin_mesaj = f"📋 **Gün Sonu Şantiye Özeti - {bugun.strftime('%d.%m.%Y')}**\n\n"
        
        if durum['rapor_veren_santiyeler']:
            admin_mesaj += f"✅ **Rapor İleten Şantiyeler** ({len(durum['rapor_veren_santiyeler'])}):\n"
            for santiye in sorted(durum['rapor_veren_santiyeler']):
                rapor_verenler = durum['santiye_rapor_verenler'].get(santiye, [])
                rapor_veren_isimler = [id_to_name.get(uid, f"Kullanıcı {uid}") for uid in rapor_verenler]
                
                if rapor_verenler:
                    admin_mesaj += f"• **{santiye}** - İleten: {', '.join(rapor_veren_isimler)}\n"
                else:
                    admin_mesaj += f"• **{santiye}** - Rapor iletildi\n"
            admin_mesaj += "\n"
        
        admin_mesaj += mesaj.split('\n\n', 1)[1]
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_mesaj)
                logging.info(f"🔴 Şantiye gün sonu özeti {admin_id} adminine gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"🔴 {admin_id} adminine şantiye gün sonu özeti gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🔴 Şantiye son rapor kontrol hatası: {e}")
        await hata_bildirimi(context, f"Şantiye son rapor kontrol hatası: {e}")

async def haftalik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    """Haftalık grup raporu"""
    try:
        today = dt.datetime.now(TZ).date()
        start_date = today - dt.timedelta(days=today.weekday() + 7)
        end_date = start_date + dt.timedelta(days=6)
        
        mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
        mesaj += "\n\n📝 **Lütfen eksiksiz rapor paylaşımına devam edelim. Teşekkürler.**"
        
        if GROUP_ID:
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj, parse_mode='Markdown')
                logging.info(f"📊 Haftalık grup raporu gönderildi: {start_date} - {end_date}")
            except Exception as e:
                logging.error(f"📊 Haftalık grup raporu gönderilemedi: {e}")
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=mesaj, parse_mode='Markdown')
                logging.info(f"📊 Haftalık rapor {admin_id} adminine gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"📊 {admin_id} adminine haftalık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"📊 Haftalık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Haftalık grup raporu hatası: {e}")

async def aylik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    """Aylık grup raporu"""
    try:
        today = dt.datetime.now(TZ).date()
        start_date = today.replace(day=1) - dt.timedelta(days=1)
        start_date = start_date.replace(day=1)
        end_date = today.replace(day=1) - dt.timedelta(days=1)
        
        mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
        mesaj += "\n\n📝 **Lütfen eksiksiz rapor paylaşımına devam edelim. Teşekkürler.**"
        
        if GROUP_ID:
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj, parse_mode='Markdown')
                logging.info(f"🗓️ Aylık grup raporu gönderildi: {start_date} - {end_date}")
            except Exception as e:
                logging.error(f"🗓️ Aylık grup raporu gönderilemedi: {e}")
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=mesaj, parse_mode='Markdown')
                logging.info(f"🗓️ Aylık rapor {admin_id} adminine gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"🗓️ {admin_id} adminine aylık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🗓️ Aylık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Aylık grup raporu hatası: {e}")

async def bot_baslatici_mesaji(context: ContextTypes.DEFAULT_TYPE):
    """Bot başlatıcı mesaj"""
    try:
        mesaj = "🤖 **Rapor Kontrol Botu Aktif!**\n\nKontrol bende ⚡️\nKolay gelsin 👷‍♂️"
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=mesaj)
                logging.info(f"Başlangıç mesajı {admin_id} adminine gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"Başlangıç mesajı {admin_id} adminine gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"Bot başlatıcı mesaj hatası: {e}")

async def post_init(application: Application):
    """Bot başlangıç ayarları"""
    commands = [
        BotCommand("start", "Botu başlat"),
        BotCommand("info", "Komut bilgisi (Tüm kullanıcılar)"),
        BotCommand("hakkinda", "Bot hakkında bilgi"),
        
        BotCommand("bugun", "Bugünün özeti (Admin)"),
        BotCommand("dun", "Dünün özeti (Admin)"),
        BotCommand("haftalik_rapor", "Haftalık rapor (Admin)"),
        BotCommand("aylik_rapor", "Aylık rapor (Admin)"),
        BotCommand("tariharaligi", "Tarih aralığı raporu (Admin)"),
        BotCommand("haftalik_istatistik", "Haftalık istatistik (Admin)"),
        BotCommand("aylik_istatistik", "Aylık istatistik (Admin)"),
        BotCommand("excel_tariharaligi", "Excel tarih aralığı raporu (Admin)"),
        BotCommand("maliyet", "Maliyet analizi (Admin)"),
        BotCommand("ai_rapor", "Detaylı AI raporu (Admin)"),
        BotCommand("kullanicilar", "Tüm kullanıcı listesi (Admin)"),
        BotCommand("santiyeler", "Şantiye listesi (Admin)"),
        BotCommand("santiye_durum", "Şantiye rapor durumu (Admin)"),
        
        BotCommand("reload", "Excel yenile (Super Admin)"),
        BotCommand("yedekle", "Manuel yedekleme (Super Admin)"),
        BotCommand("chatid", "Chat ID göster (Super Admin)"),
    ]
    await application.bot.set_my_commands(commands)
    
    await bot_baslatici_mesaji(application)

# ----------------------------- MAIN -----------------------------
def main():
    """Ana fonksiyon - GPT-4-mini entegrasyonlu"""
    try:
        app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
        
        # Temel komutlar
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("info", info_cmd))
        app.add_handler(CommandHandler("hakkinda", hakkinda_cmd))
        
        # Admin komutları
        app.add_handler(CommandHandler("bugun", bugun_cmd))
        app.add_handler(CommandHandler("dun", dun_cmd))
        app.add_handler(CommandHandler("haftalik_rapor", haftalik_rapor_cmd))
        app.add_handler(CommandHandler("aylik_rapor", aylik_rapor_cmd))
        app.add_handler(CommandHandler("tariharaligi", tariharaligi_cmd))
        app.add_handler(CommandHandler("haftalik_istatistik", haftalik_istatistik_cmd))
        app.add_handler(CommandHandler("aylik_istatistik", aylik_istatistik_cmd))
        app.add_handler(CommandHandler("excel_tariharaligi", excel_tariharaligi_cmd))
        app.add_handler(CommandHandler("maliyet", maliyet_cmd))
        app.add_handler(CommandHandler("ai_rapor", ai_rapor_cmd))
        app.add_handler(CommandHandler("kullanicilar", kullanicilar_cmd))
        app.add_handler(CommandHandler("santiyeler", santiyeler_cmd))
        app.add_handler(CommandHandler("santiye_durum", santiye_durum_cmd))
        
        # Super Admin komutları
        app.add_handler(CommandHandler("reload", reload_cmd))
        app.add_handler(CommandHandler("yedekle", yedekle_cmd))
        app.add_handler(CommandHandler("chatid", chatid_cmd))
        
        # Yeni üye karşılama
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, yeni_uye_karşilama))
        
        # YENİ GPT-4-MINI RAPOR İŞLEME SİSTEMİ - Grup ve DM ayrımlı
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), 
            yeni_gpt_rapor_isleme
        ))  # Sadece grup mesajları

        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, 
            yeni_gpt_rapor_isleme
        ))  # Sadece DM mesajları

        # Düzenlenmiş mesajlar için
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & filters.UpdateType.EDITED_MESSAGE, 
            yeni_gpt_rapor_isleme
        ))

        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & filters.UpdateType.EDITED_MESSAGE, 
            yeni_gpt_rapor_isleme
        ))
        
        schedule_jobs(app)
        logging.info("🚀 GPT-4-MINI ENTEGRE Rapor Botu başlatılıyor...")
        
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logging.error(f"❌ Bot başlatma hatası: {e}")
        raise

if __name__ == "__main__":
    main()