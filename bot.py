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
import hashlib
import subprocess
import shlex
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

PORT = int(os.environ.get('PORT', 8443))

DB_POOL = None

def init_db_pool():
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
    if DB_POOL is None:
        init_db_pool()
    return DB_POOL.getconn()

def put_conn_back(conn):
    if DB_POOL and conn:
        DB_POOL.putconn(conn)

def _sync_fetchall(query, params=()):
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
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
    except Exception as e:
        logging.error(f"Async DB query hatası: {e}")
        raise

async def async_fetchall(query, params=()):
    return await async_db_query(_sync_fetchall, query, params)

async def async_execute(query, params=()):
    return await async_db_query(_sync_execute, query, params)

async def async_fetchone(query, params=()):
    return await async_db_query(_sync_fetchone, query, params)

import google.cloud.storage
from google.oauth2 import service_account

def create_google_client():
    try:
        google_key_base64 = os.getenv("GOOGLE_KEY_BASE64")
        if not google_key_base64:
            logging.warning("⚠️ GOOGLE_KEY_BASE64 bulunamadı")
            return None
            
        key_json = base64.b64decode(google_key_base64).decode('utf-8')
        credentials_info = json.loads(key_json)
        
        credentials = service_account.Credentials.from_service_account_info(credentials_info)
        storage_client = google.cloud.storage.Client(
            credentials=credentials,
            project=os.getenv("GOOGLE_PROJECT_ID")
        )
        
        logging.info("✅ Google Cloud Storage client başarıyla oluşturuldu")
        return storage_client
    except Exception as e:
        logging.error(f"❌ Google Cloud Storage client oluşturma hatası: {e}")
        return None

def upload_backup_to_google(filename, remote_path=None):
    try:
        client = create_google_client()
        if not client:
            return False
            
        bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
        if not bucket_name:
            logging.error("❌ GOOGLE_BUCKET_NAME bulunamadı")
            return False
            
        bucket = client.bucket(bucket_name)
        
        if remote_path is None:
            remote_path = f"backups/{os.path.basename(filename)}"
            
        blob = bucket.blob(remote_path)
        
        with open(filename, 'rb') as f:
            blob.upload_from_file(f)
            
        logging.info(f"✅ Dosya Google Cloud Storage'a yüklendi: {remote_path}")
        return True
        
    except Exception as e:
        logging.error(f"❌ Google Cloud Storage yükleme hatası: {e}")
        return False

def download_last_backup(remote_path, local_filename):
    try:
        client = create_google_client()
        if not client:
            return False
            
        bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
        if not bucket_name:
            return False
            
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(remote_path)
        
        blob.download_to_filename(local_filename)
        logging.info(f"✅ Dosya Google Cloud Storage'dan indirildi: {remote_path}")
        return True
        
    except Exception as e:
        logging.error(f"❌ Google Cloud Storage indirme hatası: {e}")
        return False

def list_backups(prefix="backups/"):
    try:
        client = create_google_client()
        if not client:
            return []
            
        bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
        if not bucket_name:
            return []
            
        bucket = client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=prefix)
        
        backup_list = []
        for blob in blobs:
            backup_list.append({
                'name': blob.name,
                'size': blob.size,
                'updated': blob.updated
            })
            
        return sorted(backup_list, key=lambda x: x['updated'], reverse=True)
        
    except Exception as e:
        logging.error(f"❌ Google Cloud Storage liste hatası: {e}")
        return []

async def async_upload_to_google(filename, remote_path=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, upload_backup_to_google, filename, remote_path)

async def yedekleme_gorevi(context: ContextTypes.DEFAULT_TYPE):
    try:
        logging.info("💾 Yedekleme işlemi başlatılıyor...")
        
        success_count = 0
        total_count = 0
        
        backup_files = [
            ("Kullanicilar.xlsx", "backups/Kullanicilar.xlsx"),
            ("bot.log", "backups/bot.log")
        ]
        
        for local_file, remote_path in backup_files:
            if os.path.exists(local_file):
                total_count += 1
                if await async_upload_to_google(local_file, remote_path):
                    success_count += 1
            else:
                logging.warning(f"⚠️ Yedeklenecek dosya bulunamadı: {local_file}")
        
        status_msg = f"💾 Gece Yedekleme Raporu\n\n"
        status_msg += f"📅 Tarih: {dt.datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}\n"
        status_msg += f"📁 Dosya: {success_count}/{total_count} başarılı\n"
        
        if success_count == total_count:
            status_msg += "🎉 Tüm yedeklemeler başarılı!"
            logging.info("💾 Gece yedeklemesi tamamlandı: Tüm dosyalar başarıyla yedeklendi")
        else:
            status_msg += f"⚠️ {total_count - success_count} dosya yedeklenemedi"
            logging.warning(f"💾 Gece yedeklemesi kısmen başarılı: {success_count}/{total_count}")
        
        if success_count > 0:
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
        logging.error(f"💾 Yedekleme hatası: {e}")

def yedekle_postgres():
    try:
        timestamp = dt.datetime.now(TZ).strftime("%Y-%m-%d_%H-%M")
        dump_file = f"postgres_backup_{timestamp}.dump"
        dump_path = f"/tmp/{dump_file}"

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            logging.error("❌ DATABASE_URL bulunamadı")
            return False

        # Use subprocess.run with safe argument handling
        try:
            with open(dump_path, 'wb') as f:
                result = subprocess.run(
                    ['pg_dump', '-Fc', db_url],
                    stdout=f,
                    stderr=subprocess.PIPE,
                    check=False
                )
            
            if result.returncode != 0:
                logging.error(f"❌ pg_dump başarısız (code {result.returncode}): {result.stderr.decode()}")
                if os.path.exists(dump_path):
                    os.unlink(dump_path)
                return False
                
        except Exception as e:
            logging.error(f"❌ pg_dump çalıştırma hatası: {e}")
            if os.path.exists(dump_path):
                os.unlink(dump_path)
            return False

        gcs_path = f"backups/sql/{dump_file}"
        success = upload_backup_to_google(dump_path, gcs_path)
        
        # Clean up temp file
        if os.path.exists(dump_path):
            os.unlink(dump_path)

        if success:
            logging.info(f"💾 PostgreSQL yedeği alındı ve GCS'ye yüklendi: {dump_file}")
            return True
        else:
            logging.error("❌ PostgreSQL yedeği GCS'ye yüklenemedi")
            return False

    except Exception as e:
        logging.error(f"❌ PostgreSQL yedeği sırasında hata: {e}")
        # Clean up on any exception
        if 'dump_path' in locals() and os.path.exists(dump_path):
            os.unlink(dump_path)
        return False

async def yedekle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    await update.message.reply_text("💾 Yedekleme işlemi başlatılıyor...")
    
    try:
        success_count = 0
        backup_files = [
            ("Kullanicilar.xlsx", "backups/Kullanicilar.xlsx"),
            ("bot.log", "backups/bot.log")
        ]
        
        for local_file, remote_path in backup_files:
            if os.path.exists(local_file):
                if await async_upload_to_google(local_file, remote_path):
                    success_count += 1
        
        if success_count == len(backup_files):
            await update.message.reply_text("✅ Tüm yedeklemeler başarıyla tamamlandı!")
        else:
            await update.message.reply_text(f"⚠️ Yedekleme kısmen başarılı: {success_count}/{len(backup_files)} dosya")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Yedekleme hatası: {e}")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GROUP_ID = int(CHAT_ID) if CHAT_ID else None
TZ = ZoneInfo("Asia/Tashkent")

SUPER_ADMIN_ID = 1000157326

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
excel_file_hash = None
excel_last_modified = 0

user_role_cache = {}
user_role_cache_time = 0

async def get_user_role(user_id):
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

def get_file_hash(filename):
    try:
        if os.path.exists(filename):
            with open(filename, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        return None
    except:
        return None

def load_excel_intelligent():
    global df, rapor_sorumlulari, id_to_name, id_to_projects, id_to_status, id_to_rol, ADMINS, IZLEYICILER, TUM_KULLANICILAR
    global santiye_sorumlulari, santiye_rapor_durumu, last_excel_update, excel_file_hash, excel_last_modified
    
    try:
        current_hash = get_file_hash(USERS_FILE)
        current_mtime = os.path.getmtime(USERS_FILE) if os.path.exists(USERS_FILE) else 0
        
        if (current_hash == excel_file_hash and 
            current_mtime == excel_last_modified and 
            df is not None):
            logging.info("✅ Excel önbellekte - Yeniden yüklemeye gerek yok")
            return
        
        try:
            df = pd.read_excel(USERS_FILE)
            logging.info("✅ Excel dosyası başarıyla yüklendi")
            
            excel_file_hash = current_hash
            excel_last_modified = current_mtime
            
        except Exception as e:
            logging.error(f"❌ Excel okuma hatası: {e}. Fallback kullanıcı listesi kullanılıyor.")
            df = pd.DataFrame(FALLBACK_USERS)
    
    except Exception as e:
        logging.error(f"❌ Excel yükleme hatası: {e}. Fallback kullanıcı listesi kullanılıyor.")
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
            if tid == 10001573260:
                tid = 1000157326
            if tid == 7097468990:
                tid = 709746899
                
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
    logging.info(f"✅ Excel yüklendi: {len(rapor_sorumlulari)} takip edilen kullanıcı, {len(ADMINS)} admin, {len(IZLEYICILER)} izleyici, {len(TUM_KULLANICILAR)} toplam kullanıcı, {len(santiye_sorumlulari)} şantiye")

load_excel_intelligent()

def is_media_message(message) -> bool:
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

    if (message.caption and not message.text):
        return True

    return False

SYSTEM_PROMPT = """
SEN BİR İNŞAAT RAPORU UZMANISIN. AŞAĞIDAKİ TÜM KURALLAR KESİNLİKLE UYGULANACAK:

==================================================
🎯 SİSTEM MİMARİSİ - DEĞİŞMEYECEK!
==================================================
• Tüm komutlar ve rapor, ozet, cikti formatları AYNI KALACAK
• Grup/DM davranışları KORUNACAK
• Zamanlanmış görevler AYNI çalışacak

==================================================
🚀 GERÇEK RAPOR ANALİZİNE DAYALI PERSONEL HESAPLAMA
==================================================
KRİTİK KURALLAR - GERÇEK ÖRNEKLERDEN TÜRETİLDİ:

1. ÖNCELİK SIRASI:
   - "GENEL ÖZET" bölümündeki "Genel toplam: X" veya "Toplam: X" DEĞERLERİNİ KULLAN
   - "PERSONEL DURUMU" tablosundaki değerleri ikincil kaynak olarak kullan

2. MOBİLİZASYON ve DIŞ GÖREV:
   - "Mobilizasyon: X" → present_workers'a EKLE
   - "Dış görev: X" → present_workers'a EKLE ve issues'a ekle
   - "Lot 71 dış görev X" → present_workers'a EKLE, issues'a ekle
   - "Fap dış görev X" → present_workers'a EKLE, issues'a ekle
   - "Stadyum dış görev X" → present_workers'a EKLE, issues'a ekle

3. İZİNLİ/hasta HESAPLAMA:
   - "İzinli: X" → absent_workers = X
   - "Hastalık İzini: X" → absent_workers += X
   - "İzinli / İşe çıkmayan: X" → absent_workers += X

4. STAFF/İMALAT/MOBİLİZASYON AYRIMI:
   - "Toplam staff: X" → present_workers += X
   - "Toplam imalat: X" → present_workers += X
   - "Toplam mobilizasyon: X" → present_workers += X
   - "Ambarcı: X" → present_workers += X

5. GERÇEK ÖRNEKLERE GÖRE HESAPLAMA:

ÖRNEK 1 - BWC (14.11.2025):
"GENEL ÖZET: Staff:9 Otel:57 Villa:24 ... Mobilizasyon:8 Toplam:166"
→ present_workers = 166 (Toplam doğrudan alınır)

ÖRNEK 2 - LOT13 (15.11.2025):
"GENEL ÖZET: Toplam staff:1 Toplam imalat:0 Toplam mobilizasyon:2 İzinli:1 Genel toplam:10 kişi Lot 71 dış görev 6 Fap dış görev 2"
→ present_workers = 10 (Genel toplam)
→ absent_workers = 1 (İzinli)
→ issues = ["Lot 71 dış görev: 6 kişi", "Fap dış görev: 2 kişi"]

ÖRNEK 3 - SKP (15.11.2025):
"GENEL ÖZET: Toplam staff:1 Toplam imalat:16 Toplam mobilizasyon:2 Ambarcı:1 İzinli:3 Hastalık İzini:2 Genel toplam:25 kişi"
→ present_workers = 25 (Genel toplam)
→ absent_workers = 5 (3+2)

==================================================
🏗️ ŞANTİYE BAZLI AYRIM - PROJE TANIMLARI
==================================================
BWC ŞANTİYESİ:
• OTEL, VILLA, SPA, Restoran, Katlı otopark, VIP Lojman, Güvenlik binası, Spor binası, Peyzaj, Gece Kulübü

LOT13/LOT71 ŞANTİYELERİ:
• Ofis, Kamp, Trafo, Kazan dairesi, Jeneratör, Dış görevler

SKP ŞANTİYESİ:
• Genel Mobilizasyon, Elçi Evi, Beldersoy, Ambarcı

PİRAMİT TOWER:
• Çevre aydınlatma, AVM, Kat çalışmaları

==================================================
💬 CHAT TYPE DAVRANIŞLARI - KESİN KURALLAR
==================================================
GRUP/SÜPERGRUP MESAJLARI:
• Rapor YOKSA → [] döndür (SESSİZ ÇIKIŞ)
• Rapor VARSA → JSON array döndür
• Medya mesajları → SESSİZCE GEÇ (analiz yapma)

ÖZEL MESAJLAR (DM):
• Rapor YOKSA → {"dm_info": "no_report_detected"} döndür
• Rapor VARSA → JSON array döndür
• Kullanıcıya geri bildirim ver

MEDYA FİLTRELEME:
• Foto, video, ses, belge, caption-only → ANALİZ YAPMA
• Sadece saf metin mesajlarını analiz et

==================================================
🤖 GPT ANALİZ ÇIKTISI - KESİN FORMAT
==================================================
SADECE JSON array döndür. Başka hiçbir şey YOK.

[
  {
    "report_id": null,
    "site": "ŞANTIYE_ADI",
    "reported_at": "YYYY-MM-DD",
    "reported_time": "HH:MM",
    "reporter": null,
    "report_type": "RAPOR" | "IZIN/ISYOK",
    "status_summary": "Özet metin",
    "present_workers": integer,
    "absent_workers": integer,
    "issues": ["Dış görev: X kişi", "Mobilizasyon: Y kişi"],
    "actions_requested": [],
    "attachments_ref": [],
    "raw_text": "Orijinal metin parçası",
    "confidence": 0.9
  }
]

==================================================
🎯 KESİN ÇIKTI KURALLARI
==================================================
• SADECE JSON array döndür
• Hiçbir açıklama, yorum, not EKLEME
• Gelecek tarihli raporları AT (reported_at > bugün)
• Eski raporları (365 günden eski) confidence ≤ 0.40 ile işaretle
• Birden fazla rapor varsa AYRI JSON objeleri olarak döndür
• Rapor sırasını KORU (orijinal mesajdaki sırayla)

==================================================
🚨 MUTLAKA UYULACAK SON KURALLAR
==================================================
1. GRUP MESAJLARI:
   - Rapor yoksa → [] (SESSİZ)
   - Rapor varsa → JSON array

2. DM MESAJLARI:
   - Rapor yoksa → {"dm_info": "no_report_detected"}
   - Rapor varsa → JSON array

3. MEDYA MESAJLARI:
   - Hiçbir analiz YAPMA → Sessizce geç

4. PERSONEL HESAPLAMA:
   - "GENEL ÖZET" öncelikli
   - Mobilizasyon ve dış görevleri EKLE
   - İzinli/hastalığı absent_workers'a EKLE

5. TARİH KONTROLLERİ:
   - Gelecek tarih → AT
   - Eski tarih → confidence düşük
   - Bugün/dün → otomatik tanı

BU KURALLARIN DIŞINA ASLA ÇIKMA. HER DAVRANIŞ BU KURALLARA GÖRE OLMALI.
"""

def get_chat_type_behavior(is_group):
    if is_group:
        return (
            "GRUP MODU - KESİN DAVRANIŞ:\n"
            "• Rapor YOKSA → [] döndür (SESSİZ ÇIKIŞ)\n" 
            "• Rapor VARSA → JSON array döndür\n"
            "• Medya mesajları → ANALİZ YAPMA"
        )
    else:
        return (
            "DM MODU - KESİN DAVRANIŞ:\n"
            "• Rapor YOKSA → {\"dm_info\": \"no_report_detected\"} döndür\n"
            "• Rapor VARSA → JSON array döndür\n"
            "• Kullanıcıya geri bildirim verilecek"
        )

USER_PROMPT_TEMPLATE = """
chat_type: "<<<CHAT_TYPE>>>"

🧠 AKILLI SİSTEM AKTİF - GERÇEK RAPOR ANALİZİ:

📊 PERSONEL HESAPLAMA ÖNCELİKLERİ:
1. "GENEL ÖZET" → "Genel toplam" veya "Toplam" değerini kullan
2. MOBİLİZASYON → present_workers'a ekle
3. DIŞ GÖREVLER → present_workers'a ekle + issues'a not et
4. İZİNLİ/hasta → absent_workers'a ekle

🏗️ ŞANTİYE TANIMLARI:
• BWC: OTEL, VILLA, SPA, Restoran, Katlı otopark, VIP Lojman
• LOT13/LOT71: Ofis, Kamp, Trafo, Dış görevler  
• SKP: Genel Mobilizasyon, Elçi Evi, Ambarcı

💬 CHAT TYPE DAVRANIŞI:
<<<CHAT_TYPE_BEHAVIOR>>>

ANALİZ EDİLECEK RAPOR:
<<<RAW_MESSAGE>>>

🔐 KRİTİK KURALLAR:
- ÖNCELİKLE "GENEL ÖZET" bölümünü ara
- "Toplam: X" veya "Genel toplam: X" → present_workers = X
- "Mobilizasyon: X" → present_workers'a EKLE
- "Dış görev X" → present_workers'a EKLE + issues'a ekle
- "İzinli: X" → absent_workers = X
- "Hastalık: X" → absent_workers += X

SADECE JSON array döndür. Başka hiçbir şey YOK.
"""

client = OpenAI(api_key=OPENAI_API_KEY)

def gpt_analyze(system_prompt, user_prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0,
            max_tokens=2000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"GPT hatası: {e}")
        return ""

def process_incoming_message(raw_text: str, is_group: bool = False):
    today = dt.date.today()
    
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            chat_type = "group" if is_group else "private"
            
            chat_type_behavior = get_chat_type_behavior(is_group)
            
            user_prompt = USER_PROMPT_TEMPLATE.replace("<<<CHAT_TYPE>>>", chat_type)
            user_prompt = user_prompt.replace("<<<CHAT_TYPE_BEHAVIOR>>>", chat_type_behavior)
            user_prompt = user_prompt.replace("<<<RAW_MESSAGE>>>", raw_text)

            content = gpt_analyze(SYSTEM_PROMPT, user_prompt)
            
            if not content:
                if attempt < max_retries - 1:
                    time_module.sleep(retry_delay)
                    continue
                return [] if is_group else {"dm_info": "no_report_detected"}
            
            try:
                data = json.loads(content)
                
                if isinstance(data, dict):
                    data = [data]
                
                if isinstance(data, list):
                    if is_group:
                        if len(data) == 0:
                            return []
                        if len(data) == 1 and data[0].get("dm_info"):
                            return []
                    
                    if not is_group:
                        if len(data) == 1 and data[0].get("dm_info") == "no_report_detected":
                            return {"dm_info": "no_report_detected"}
                        if len(data) == 0:
                            return {"dm_info": "no_report_detected"}
                
                filtered_reports = []
                for report in data:
                    if report.get('dm_info'):
                        continue

                    reported_at = report.get('reported_at')
                    if reported_at:
                        try:
                            report_date = dt.datetime.strptime(reported_at, '%Y-%m-%d').date()
                            if report_date > today:
                                continue
                        except ValueError:
                            pass

                    confidence = report.get('confidence', 0.9)
                    if reported_at:
                        try:
                            report_date = dt.datetime.strptime(reported_at, '%Y-%m-%d').date()
                            days_ago = (today - report_date).days
                            if days_ago > 365:
                                confidence = min(confidence, 0.4)
                        except ValueError:
                            pass
                    
                    report['confidence'] = confidence
                    filtered_reports.append(report)
                
                return filtered_reports
            
            except json.JSONDecodeError:
                logging.error(f"GPT JSON parse hatası: {content}")
                if attempt < max_retries - 1:
                    time_module.sleep(retry_delay)
                    continue
                return [] if is_group else {"dm_info": "no_report_detected"}
                
        except Exception as e:
            logging.error(f"GPT analiz hatası (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time_module.sleep(retry_delay)
                continue
            return [] if is_group else {"dm_info": "no_report_detected"}

async def yeni_gpt_rapor_isleme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg:
        return

    user_id = msg.from_user.id
    chat_type = msg.chat.type
    
    is_group = chat_type in ["group", "supergroup"]
    is_dm = chat_type == "private"

    if is_media_message(msg):
        logging.info(f"⛔ Medya mesajı tespit edildi → AI analizi yapılmayacak. User: {user_id}, Chat Type: {chat_type}")
        return

    metin = msg.text or msg.caption
    if not metin:
        return

    if metin.startswith(('/', '.', '!', '\\')):
        return

    try:
        raporlar = process_incoming_message(metin, is_group)
        
        if is_dm and isinstance(raporlar, dict) and raporlar.get('dm_info') == 'no_report_detected':
            await msg.reply_text(
                "❌ Bu mesaj bir rapor olarak algılanmadı.\n\n"
                "Lütfen şantiye, tarih ve iş bilgilerini içeren bir rapor gönderin.\n"
                "Örnek: \"01.11.2024 LOT13 2.kat kablo çekimi 5 kişi\""
            )
            return
        
        if not raporlar or (isinstance(raporlar, list) and len(raporlar) == 0):
            logging.info(f"🤖 GPT: Rapor bulunamadı - {user_id} (Chat Type: {chat_type})")
            
            if is_dm:
                await msg.reply_text(
                    "❌ Rapor bulunamadı.\n\n"
                    "Lütfen şantiye raporunuzu aşağıdaki formatta gönderin:\n"
                    "• Tarih (01.01.2025)\n" 
                    "• Şantiye adı (LOT13, BWC, SKP vb.)\n"
                    "• Yapılan işler\n"
                    "• Personel bilgisi\n\n"
                    "Örnek: \"01.11.2024 LOT13 2.kat kablo çekimi 5 kişi\""
                )
            return

        logging.info(f"🤖 GPT: {len(raporlar)} rapor çıkarıldı - {user_id} (Chat Type: {chat_type})")
        
        kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
        
        basarili_kayitlar = 0
        for i, rapor in enumerate(raporlar):
            try:
                await raporu_gpt_formatinda_kaydet(user_id, kullanici_adi, metin, rapor, msg, i+1)
                basarili_kayitlar += 1
            except Exception as e:
                logging.error(f"❌ Rapor {i+1} kaydetme hatası: {e}")
        
        if is_dm:
            if basarili_kayitlar == len(raporlar):
                if len(raporlar) == 1:
                    await msg.reply_text("✅ Raporunuz başarıyla işlendi!")
                else:
                    await msg.reply_text(f"✅ {len(raporlar)} rapor başarıyla işlendi!")
            else:
                await msg.reply_text(f"⚠️ {basarili_kayitlar}/{len(raporlar)} rapor işlendi. Bazı raporlar kaydedilemedi.")
        
        logging.info(f"📊 Grup raporu işlendi: {basarili_kayitlar}/{len(raporlar)} başarılı")
            
    except Exception as e:
        logging.error(f"❌ GPT rapor işleme hatası: {e}")
        if is_dm:
            await msg.reply_text("❌ Rapor işlenirken bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

async def raporu_gpt_formatinda_kaydet(user_id, kullanici_adi, orijinal_metin, gpt_rapor, msg, rapor_no=1):
    try:
        site = gpt_rapor.get('site')
        if site is None:
            site = "Bilinmeyen"
        else:
            site = str(site).strip() if site else "Bilinmeyen"

        rapor_tarihi = None
        reported_at = gpt_rapor.get('reported_at')
        if reported_at:
            try:
                rapor_tarihi = dt.datetime.strptime(reported_at, '%Y-%m-%d').date()
            except ValueError:
                pass
        
        if not rapor_tarihi:
            rapor_tarihi = parse_rapor_tarihi(orijinal_metin) or dt.datetime.now(TZ).date()
        
        project_name = site
        if not project_name or project_name == 'BELİRSİZ' or project_name == 'Bilinmeyen':
            user_projects = id_to_projects.get(user_id, [])
            if user_projects:
                project_name = user_projects[0]
            else:
                project_name = 'BELİRSİZ'
        
        existing_report = await async_fetchone("""
            SELECT id FROM reports 
            WHERE user_id = %s AND project_name = %s AND report_date = %s
        """, (user_id, project_name, rapor_tarihi))
        
        # Safe check for existing report
        if existing_report is not None and len(existing_report) > 0 and existing_report[0] is not None:
            logging.warning(f"⚠️ Zaten rapor var: {user_id} - {project_name} - {rapor_tarihi}")
            raise Exception(f"Bu şantiye için bugün zaten rapor gönderdiniz: {project_name}")
        
        rapor_tipi = gpt_rapor.get('report_type') or "RAPOR"
        if rapor_tipi is None:
            rapor_tipi = "RAPOR"

        present_workers = gpt_rapor.get('present_workers')
        if present_workers is None:
            present_workers = 0
        else:
            try:
                present_workers = int(present_workers) if present_workers else 0
            except (ValueError, TypeError):
                present_workers = 0

        absent_workers = gpt_rapor.get('absent_workers')
        if absent_workers is None:
            absent_workers = 0
        else:
            try:
                absent_workers = int(absent_workers) if absent_workers else 0
            except (ValueError, TypeError):
                absent_workers = 0

        person_count = max(present_workers, 1)
        
        status_summary = gpt_rapor.get('status_summary') or ""
        if status_summary is None:
            status_summary = ""
            
        issues = gpt_rapor.get('issues') or []
        if not isinstance(issues, list):
            issues = []
        
        work_description = status_summary
        if issues:
            work_description += f" | İşler: {', '.join(issues[:3])}"
        
        if not work_description.strip():
            work_description = orijinal_metin[:200] if orijinal_metin else ""
        
        raw_text = gpt_rapor.get('raw_text')
        if raw_text is None:
            raw_text = orijinal_metin
        else:
            raw_text = str(raw_text).strip() if raw_text else orijinal_metin

        confidence = gpt_rapor.get('confidence', 0.9)
        try:
            confidence = float(confidence) if confidence else 0.9
        except (ValueError, TypeError):
            confidence = 0.9
        
        ai_analysis = {
            "gpt_analysis": gpt_rapor,
            "confidence": confidence,
            "extraction_method": "gpt-4o-mini",
            "original_text_snippet": orijinal_metin[:100] if orijinal_metin else "",
            "raw_text": raw_text[:500] if raw_text else ""
        }
        
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
        
        maliyet_analiz.kayit_ekle('gpt')
            
    except Exception as e:
        logging.error(f"❌ GPT rapor kaydetme hatası: {e}")
        raise e

async def excel_durum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    try:
        mesaj = "📊 EXCEL SİSTEM DURUMU\n\n"
        
        if os.path.exists(USERS_FILE):
            file_size = os.path.getsize(USERS_FILE)
            file_mtime = dt.datetime.fromtimestamp(os.path.getmtime(USERS_FILE))
            mesaj += f"✅ Dosya Mevcut: {USERS_FILE}\n"
            mesaj += f"📏 Boyut: {file_size} bytes\n"
            mesaj += f"🕒 Son Değişiklik: {file_mtime.strftime('%d.%m.%Y %H:%M')}\n"
            
            current_hash = get_file_hash(USERS_FILE)
            mesaj += f"🔐 Hash: {current_hash[:8] if current_hash else 'Hesaplanamadı'}\n\n"
        else:
            mesaj += f"❌ Dosya Bulunamadı: {USERS_FILE}\n\n"
            mesaj += "🔄 Fallback sistem aktif\n\n"
        
        mesaj += "💾 ÖNBELLEK DURUMU:\n"
        mesaj += f"• Excel Hash: {excel_file_hash[:8] if excel_file_hash else 'Yok'}\n"
        mesaj += f"• Son Yükleme: {dt.datetime.fromtimestamp(excel_last_modified).strftime('%d.%m.%Y %H:%M') if excel_last_modified else 'Yok'}\n"
        mesaj += f"• DataFrame: {'Mevcut' if df is not None else 'Yok'}\n\n"
        
        mesaj += "📈 SİSTEM İSTATİSTİKLERİ:\n"
        mesaj += f"• Takip Edilen Kullanıcı: {len(rapor_sorumlulari)}\n"
        mesaj += f"• Adminler: {len(ADMINS)}\n"
        mesaj += f"• İzleyiciler: {len(IZLEYICILER)}\n"
        mesaj += f"• Toplam Kullanıcı: {len(TUM_KULLANICILAR)}\n"
        mesaj += f"• Şantiyeler: {len(santiye_sorumlulari)}\n\n"
        
        mesaj += "🛡️ GÜVENLİK SİSTEMİ:\n"
        mesaj += f"• Fallback Aktif: {'Evet' if df is not None and any(df['Telegram ID'] == 1000157326) else 'Hayır'}\n"
        mesaj += f"• Super Admin: {SUPER_ADMIN_ID} ({'Aktif' if SUPER_ADMIN_ID in ADMINS else 'Pasif'})\n"
        
        await update.message.reply_text(mesaj)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Durum kontrol hatası: {e}")

async def yeni_uye_karşilama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for member in update.message.new_chat_members:
            if member.id == context.bot.id:
                await update.message.reply_text(
                    "🤖 Rapor Botu Aktif!\n\n"
                    "Ben şantiye raporlarınızı otomatik olarak işleyen bir botum.\n"
                    "Günlük çalışma raporlarınızı gönderebilirsiniz.\n\n"
                    "📋 Özellikler:\n"
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

def update_database_schema():
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

def init_database():
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

async def get_santiye_rapor_durumu(bugun):
    try:
        rows = await async_fetchall("""
            SELECT DISTINCT project_name FROM reports 
            WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (bugun,))
        
        return set(row[0] for row in rows if row and len(row) > 0 and row[0])
    except Exception as e:
        logging.error(f"Şantiye rapor durumu hatası: {e}")
        return set()

async def get_eksik_santiyeler(bugun):
    try:
        tum_santiyeler = set(santiye_sorumlulari.keys())
        rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
        eksik_santiyeler = tum_santiyeler - rapor_veren_santiyeler
        
        return {santiye: santiye_sorumlulari.get(santiye, []) for santiye in eksik_santiyeler}
    except Exception as e:
        logging.error(f"Eksik şantiye sorgu hatası: {e}")
        return {}

async def get_santiye_bazli_rapor_durumu(bugun):
    try:
        tum_santiyeler = set(santiye_sorumlulari.keys())
        rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
        
        rows = await async_fetchall("""
            SELECT project_name, user_id FROM reports 
            WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (bugun,))
        
        santiye_rapor_verenler = {}
        for row in rows:
            if row and len(row) >= 2:
                project_name, user_id = row[0], row[1]
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
            f"📊 MALİYET ANALİZİ\n\n"
            f"🤖 GPT İşlemleri: {self.gpt_count} (%{gpt_orani:.1f})\n"
            f"🔄 Fallback: {self.fallback_count}\n"
            f"💰 Tahmini Maliyet: ${maliyet:.4f}\n"
            f"🎯 Başarı Oranı: %{gpt_orani:.1f}"
        )
    
    def detayli_ai_raporu(self):
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
            
            if not result or len(result) < 5 or result[0] is None or result[0] == 0:
                return "🤖 AI Raporu: Henüz AI kullanımı yok"
            
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
            
            rapor = "🤖 DETAYLI AI RAPORU\n\n"
            rapor += f"📈 Genel İstatistikler:\n"
            rapor += f"• Toplam İşlem: {toplam}\n"
            rapor += f"• Başarılı: {basarili} (%{(basarili/toplam*100):.1f})\n"
            rapor += f"• Başarısız: {basarisiz}\n"
            rapor += f"• İlk Kullanım: {ilk_tarih[:10] if ilk_tarih else 'Yok'}\n"
            rapor += f"• Son Kullanım: {son_tarih[:10] if son_tarih else 'Yok'}\n\n"
            
            rapor += f"📅 Son 7 Gün:\n"
            for row in rows:
                if row and len(row) >= 3:
                    gun, toplam_gun, basarili_gun = row
                    oran = (basarili_gun/toplam_gun*100) if toplam_gun > 0 else 0
                    rapor += f"• {gun}: {basarili_gun}/{toplam_gun} (%{oran:.1f})\n"
            
            return rapor
            
        except Exception as e:
            return f"❌ AI raporu oluşturulurken hata: {e}"

maliyet_analiz = MaliyetAnaliz()

def parse_rapor_tarihi(metin):
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
            r'(\d{1,2})\s*[/\.\-]\s*(\d{1,2})\s*[/\.\-]\s*(\d{4})',
            r'(\d{1,2})\s*[/\.\-]\s*(\d{1,2})\s*[/\.\-]\s*(\d{2})',
        ]
        
        for pattern in date_patterns:
            matches = re.finditer(pattern, metin)
            for match in matches:
                groups = match.groups()
                if len(groups) == 3:
                    try:
                        if len(groups[2]) == 4:
                            day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
                        elif len(groups[0]) == 4:
                            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                        else:
                            day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
                            year += 2000
                        
                        parsed_date = dt.datetime(year, month, day).date()
                        if parsed_date <= bugun:
                            return parsed_date
                    except ValueError:
                        continue
        
        return None
    except Exception:
        return None

def izin_mi(metin):
    metin_lower = metin.lower()
    izin_kelimeler = ['izin', 'rapor yok', 'iş yok', 'çalışma yok', 'tatil', 'hasta', 'izindeyim']
    return any(kelime in metin_lower for kelime in izin_kelimeler)

async def tarih_kontrol_et(rapor_tarihi, user_id):
    bugun = dt.datetime.now(TZ).date()
    
    if not rapor_tarihi:
        return False, "❌ Tarih bulunamadı. Lütfen raporunuzda tarih belirtiniz."
    
    if rapor_tarihi > bugun:
        return False, "❌ Gelecek tarihli rapor. Lütfen bugün veya geçmiş tarih kullanınız."
    
    iki_ay_once = bugun - dt.timedelta(days=60)
    if rapor_tarihi < iki_ay_once:
        return False, "❌ Çok eski tarihli rapor. Lütfen son 2 ay içinde bir tarih kullanınız."
    
    result = await async_fetchone("SELECT EXISTS(SELECT 1 FROM reports WHERE user_id = %s AND report_date = %s)", 
                  (user_id, rapor_tarihi))
    
    if result and len(result) > 0 and result[0]:
        return False, "❌ Bu tarih için zaten rapor gönderdiniz."
    
    return True, ""

def parse_tr_date(date_str):
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
    end_date = dt.datetime.now(TZ).date()
    start_date = end_date - dt.timedelta(days=6)
    return start_date, end_date

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
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ Sistem Hatası: {hata_mesaji}"
            )
            await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Hata bildirimi {admin_id} adminine gönderilemedi: {e}")

async def generate_gelismis_personel_ozeti(target_date):
    try:
        rows = await async_fetchall("""
            SELECT user_id, report_type, project_name, person_count, work_description, ai_analysis
            FROM reports WHERE report_date = %s
        """, (target_date,))
        
        if not rows:
            return f"📭 {target_date.strftime('%d.%m.%Y')} tarihinde rapor bulunamadı."
        
        proje_analizleri = {}
        tum_projeler = set()
        
        for row in rows:
            if len(row) < 6:
                continue
            user_id, rapor_tipi, proje_adi, kisi_sayisi, yapilan_is, ai_analysis = row
            
            if not proje_adi:
                proje_adi = 'BELİRSİZ'
                
            if proje_adi not in proje_analizleri:
                proje_analizleri[proje_adi] = {
                    'staff': 0, 'calisan': 0, 'izinli': 0, 'hasta': 0, 'mobilizasyon': 0, 'dis_gorev': 0
                }
            
            yapilan_is_lower = (yapilan_is or '').lower()
            ai_data = json.loads(ai_analysis) if ai_analysis else {}
            gpt_analysis = ai_data.get('gpt_analysis', {})
            issues = gpt_analysis.get('issues', [])
            
            dis_gorev_sayisi = 0
            for issue in issues:
                if 'dış görev' in issue.lower() or 'dis gorev' in issue.lower():
                    sayilar = re.findall(r'\d+', issue)
                    if sayilar:
                        dis_gorev_sayisi += int(sayilar[0])
            
            if 'staff' in yapilan_is_lower:
                proje_analizleri[proje_adi]['staff'] += kisi_sayisi
            elif 'mobilizasyon' in yapilan_is_lower:
                proje_analizleri[proje_adi]['mobilizasyon'] += kisi_sayisi
            elif dis_gorev_sayisi > 0:
                proje_analizleri[proje_adi]['dis_gorev'] += dis_gorev_sayisi
            elif rapor_tipi == "IZIN/ISYOK":
                if 'hasta' in yapilan_is_lower:
                    proje_analizleri[proje_adi]['hasta'] += kisi_sayisi
                else:
                    proje_analizleri[proje_adi]['izinli'] += kisi_sayisi
            else:
                proje_analizleri[proje_adi]['calisan'] += kisi_sayisi
            
            tum_projeler.add(proje_adi)
        
        mesaj = f"📊 {target_date.strftime('%d.%m.%Y')} GÜNLÜK PERSONEL ÖZETİ\n\n"
        
        genel_toplam = 0
        genel_staff = 0
        genel_calisan = 0
        genel_izinli = 0
        genel_hasta = 0
        genel_mobilizasyon = 0
        genel_dis_gorev = 0
        
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: sum(x[1].values()), reverse=True):
            proje_toplam = analiz['staff'] + analiz['calisan'] + analiz['mobilizasyon']
            if proje_toplam > 0:
                genel_toplam += proje_toplam
                genel_staff += analiz['staff']
                genel_calisan += analiz['calisan']
                genel_izinli += analiz['izinli']
                genel_hasta += analiz['hasta']
                genel_mobilizasyon += analiz['mobilizasyon']
                genel_dis_gorev += analiz['dis_gorev']
                
                emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                mesaj += f"{emoji} {proje_adi}: {proje_toplam} kişi\n"
                
                durum_detay = []
                if analiz['staff'] > 0: 
                    durum_detay.append(f"Staff:{analiz['staff']}")
                if analiz['calisan'] > 0: 
                    durum_detay.append(f"Çalışan:{analiz['calisan']}")
                if analiz['izinli'] > 0: 
                    durum_detay.append(f"İzinli:{analiz['izinli']}")
                if analiz['hasta'] > 0: 
                    durum_detay.append(f"Hastalık:{analiz['hasta']}")
                if analiz['mobilizasyon'] > 0: 
                    durum_detay.append(f"Mobilizasyon:{analiz['mobilizasyon']}")
                
                if durum_detay:
                    mesaj += f"   └─ {', '.join(durum_detay)}\n\n"
        
        mesaj += f"📈 GENEL TOPLAM: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 DAĞILIM:\n"
            if genel_staff > 0:
                mesaj += f"• Staff: {genel_staff} (%{genel_staff/genel_toplam*100:.1f})\n"
            if genel_calisan > 0:
                mesaj += f"• Çalışan: {genel_calisan} (%{genel_calisan/genel_toplam*100:.1f})\n"
            if genel_izinli > 0:
                mesaj += f"• İzinli: {genel_izinli} (%{genel_izinli/genel_toplam*100:.1f})\n"
            if genel_hasta > 0:
                mesaj += f"• Hastalık: {genel_hasta} (%{genel_hasta/genel_toplam*100:.1f})\n"
            if genel_mobilizasyon > 0:
                mesaj += f"• Mobilizasyon: {genel_mobilizasyon} (%{genel_mobilizasyon/genel_toplam*100:.1f})\n"
        
        if genel_dis_gorev > 0:
            mesaj += f"🚀 DIŞ GÖREVLER: {genel_dis_gorev} kişi\n"
        
        aktif_projeler = set(proje_analizleri.keys())
        tum_santiyeler = set(santiye_sorumlulari.keys())
        eksik_projeler = [s for s in (tum_santiyeler - aktif_projeler) if s not in ["Belli değil", "Tümü"]]
        
        if eksik_projeler:
            mesaj += f"❌ EKSİK: {', '.join(sorted(eksik_projeler))}"
        
        return mesaj
    except Exception as e:
        return f"❌ Rapor oluşturulurken hata oluştu: {e}"

async def generate_haftalik_rapor_mesaji(start_date, end_date):
    try:
        rows = await async_fetchall("""
            SELECT user_id, COUNT(*) as rapor_sayisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
            GROUP BY user_id
            ORDER BY rapor_sayisi DESC
        """, (start_date, end_date))
        
        if not rows:
            return f"📭 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')} arasında rapor bulunamadı."
        
        toplam_rapor = sum([x[1] for x in rows if len(x) >= 2])
        gun_sayisi = (end_date - start_date).days + 1
        beklenen_rapor = len(rapor_sorumlulari) * gun_sayisi
        verimlilik = (toplam_rapor / beklenen_rapor * 100) if beklenen_rapor > 0 else 0
        
        en_aktif = rows[:3]
        
        en_pasif = [x for x in rows if len(x) >= 2 and x[1] < gun_sayisi * 0.5]
        
        proje_detay_rows = await async_fetchall("""
            SELECT project_name, 
                   SUM(CASE WHEN report_type = 'RAPOR' THEN person_count ELSE 0 END) as calisan,
                   SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND LOWER(work_description) LIKE '%hasta%' THEN person_count ELSE 0 END) as hasta,
                   SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND (LOWER(work_description) NOT LIKE '%hasta%' OR work_description IS NULL) THEN person_count ELSE 0 END) as izinli,
                   SUM(CASE WHEN LOWER(work_description) LIKE '%staff%' OR LOWER(work_description) LIKE '%staf%' THEN person_count ELSE 0 END) as staff,
                   SUM(CASE WHEN LOWER(work_description) LIKE '%mobilizasyon%' THEN person_count ELSE 0 END) as mobilizasyon
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
            GROUP BY project_name
            ORDER BY project_name
        """, (start_date, end_date))
        
        genel_toplam_result = await async_fetchone("""
            SELECT 
                SUM(CASE WHEN report_type = 'RAPOR' THEN person_count ELSE 0 END) as toplam_calisan,
                SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND LOWER(work_description) LIKE '%hasta%' THEN person_count ELSE 0 END) as toplam_hasta,
                SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND (LOWER(work_description) NOT LIKE '%hasta%' OR work_description IS NULL) THEN person_count ELSE 0 END) as toplam_izinli,
                SUM(CASE WHEN LOWER(work_description) LIKE '%staff%' OR LOWER(work_description) LIKE '%staf%' THEN person_count ELSE 0 END) as toplam_staff,
                SUM(CASE WHEN LOWER(work_description) LIKE '%mobilizasyon%' THEN person_count ELSE 0 END) as toplam_mobilizasyon
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
        """, (start_date, end_date))
        
        # Safe extraction with defaults
        toplam_staff = genel_toplam_result[3] if genel_toplam_result and len(genel_toplam_result) > 3 else 0
        toplam_calisan = genel_toplam_result[0] if genel_toplam_result and len(genel_toplam_result) > 0 else 0
        toplam_mobilizasyon = genel_toplam_result[4] if genel_toplam_result and len(genel_toplam_result) > 4 else 0
        toplam_izinli = genel_toplam_result[2] if genel_toplam_result and len(genel_toplam_result) > 2 else 0
        toplam_hasta = genel_toplam_result[1] if genel_toplam_result and len(genel_toplam_result) > 1 else 0
        
        genel_toplam = toplam_staff + toplam_calisan + toplam_mobilizasyon
        
        tum_santiyeler = set(santiye_sorumlulari.keys())
        rapor_veren_santiyeler = set([row[0] for row in proje_detay_rows if row and len(row) > 0 and row[0]])
        eksik_santiyeler = [s for s in (tum_santiyeler - rapor_veren_santiyeler) if s not in ["Belli değil", "Tümü"]]
        
        mesaj = f"📈 HAFTALIK ÖZET RAPOR\n"
        mesaj += f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        
        mesaj += f"📊 GENEL İSTATİSTİKLER:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor}\n"
        mesaj += f"• Rapor Gönderen: {len(rows)} kişi\n"
        mesaj += f"• İş Günü: {gun_sayisi} gün\n"
        mesaj += f"• Verimlilik: %{verimlilik:.1f}\n\n"
        
        mesaj += f"🔝 EN AKTİF 3 KULLANICI:\n"
        for i, row in enumerate(en_aktif, 1):
            if len(row) >= 2:
                user_id, rapor_sayisi = row[0], row[1]
                kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                emoji = "1️⃣" if i == 1 else "2️⃣" if i == 2 else "3️⃣"
                gunluk_ortalama = rapor_sayisi / gun_sayisi
                mesaj += f"{emoji} {kullanici_adi}: {rapor_sayisi} rapor (günlük: {gunluk_ortalama:.1f})\n"
        
        if en_pasif:
            mesaj += f"\n🔴 DÜŞÜK PERFORMANS (< %50 Katılım):\n"
            for i, row in enumerate(en_pasif[:3], 1):
                if len(row) >= 2:
                    user_id, rapor_sayisi = row[0], row[1]
                    kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                    katilim_orani = (rapor_sayisi / gun_sayisi) * 100
                    emoji = "1️⃣" if i == 1 else "2️⃣" if i == 2 else "3️⃣"
                    mesaj += f"{emoji} {kullanici_adi}: {rapor_sayisi} rapor (%{katilim_orani:.1f})\n"
        
        mesaj += f"\n🏗️ PROJE BAZLI PERSONEL:\n"
        
        onemli_projeler = ["SKP", "LOT13", "LOT71"]
        for row in proje_detay_rows:
            if len(row) >= 6:
                proje_adi, calisan, hasta, izinli, staff, mobilizasyon = row
                if proje_adi in onemli_projeler:
                    toplam_proje = (staff or 0) + (calisan or 0) + (mobilizasyon or 0)
                    if toplam_proje > 0:
                        mesaj += f"🏗️ {proje_adi}: {toplam_proje} kişi\n"
                        mesaj += f"   └─ Staff:{staff or 0}, Çalışan:{calisan or 0}, İzinli:{izinli or 0}, Hastalık:{hasta or 0}, Mobilizasyon:{mobilizasyon or 0}\n\n"
        
        for row in proje_detay_rows:
            if len(row) >= 6:
                proje_adi, calisan, hasta, izinli, staff, mobilizasyon = row
                if proje_adi not in onemli_projeler:
                    toplam_proje = (staff or 0) + (calisan or 0) + (mobilizasyon or 0)
                    if toplam_proje > 0:
                        emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                        mesaj += f"{emoji} {proje_adi}: {toplam_proje} kişi\n"
        
        mesaj += f"\n📈 GENEL TOPLAM: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 DAĞILIM:\n"
            if toplam_staff > 0:
                mesaj += f"• Staff: {toplam_staff} (%{toplam_staff/genel_toplam*100:.1f})\n"
            if toplam_calisan > 0:
                mesaj += f"• Çalışan: {toplam_calisan} (%{toplam_calisan/genel_toplam*100:.1f})\n"
            if toplam_mobilizasyon > 0:
                mesaj += f"• Mobilizasyon: {toplam_mobilizasyon} (%{toplam_mobilizasyon/genel_toplam*100:.1f})\n"
            if toplam_izinli > 0:
                mesaj += f"• İzinli: {toplam_izinli}\n"
            if toplam_hasta > 0:
                mesaj += f"• Hasta: {toplam_hasta}\n"
        
        if eksik_santiyeler:
            mesaj += f"\n❌ EKSİK: {', '.join(sorted(eksik_santiyeler))}"
        
        mesaj += "\n\n📝 Lütfen eksiksiz rapor paylaşımına devam edelim. Teşekkürler."
        
        return mesaj
    except Exception as e:
        return f"❌ Haftalık rapor oluşturulurken hata: {e}"

async def generate_aylik_rapor_mesaji(start_date, end_date):
    try:
        rows = await async_fetchall("""
            SELECT user_id, COUNT(*) as rapor_sayisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
            GROUP BY user_id
            ORDER BY rapor_sayisi DESC
        """, (start_date, end_date))
        
        if not rows:
            return f"📭 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')} arasında rapor bulunamadı."
        
        toplam_rapor = sum([x[1] for x in rows if len(x) >= 2])
        gun_sayisi = (end_date - start_date).days + 1
        
        beklenen_rapor = len(rapor_sorumlulari) * gun_sayisi
        eksik_rapor = max(0, beklenen_rapor - toplam_rapor)
        
        en_aktif = rows[:3]
        
        en_pasif = [x for x in rows if len(x) >= 2 and x[1] < gun_sayisi * 0.5]
        
        proje_detay_rows = await async_fetchall("""
            SELECT project_name, 
                   SUM(CASE WHEN report_type = 'RAPOR' THEN person_count ELSE 0 END) as calisan,
                   SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND LOWER(work_description) LIKE '%hasta%' THEN person_count ELSE 0 END) as hasta,
                   SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND (LOWER(work_description) NOT LIKE '%hasta%' OR work_description IS NULL) THEN person_count ELSE 0 END) as izinli,
                   SUM(CASE WHEN LOWER(work_description) LIKE '%staff%' OR LOWER(work_description) LIKE '%staf%' THEN person_count ELSE 0 END) as staff,
                   SUM(CASE WHEN LOWER(work_description) LIKE '%mobilizasyon%' THEN person_count ELSE 0 END) as mobilizasyon
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
            GROUP BY project_name
            ORDER BY project_name
        """, (start_date, end_date))
        
        genel_toplam_result = await async_fetchone("""
            SELECT 
                SUM(CASE WHEN report_type = 'RAPOR' THEN person_count ELSE 0 END) as toplam_calisan,
                SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND LOWER(work_description) LIKE '%hasta%' THEN person_count ELSE 0 END) as toplam_hasta,
                SUM(CASE WHEN report_type = 'IZIN/ISYOK' AND (LOWER(work_description) NOT LIKE '%hasta%' OR work_description IS NULL) THEN person_count ELSE 0 END) as toplam_izinli,
                SUM(CASE WHEN LOWER(work_description) LIKE '%staff%' OR LOWER(work_description) LIKE '%staf%' THEN person_count ELSE 0 END) as toplam_staff,
                SUM(CASE WHEN LOWER(work_description) LIKE '%mobilizasyon%' THEN person_count ELSE 0 END) as toplam_mobilizasyon
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
        """, (start_date, end_date))
        
        # Safe extraction with defaults
        toplam_staff = genel_toplam_result[3] if genel_toplam_result and len(genel_toplam_result) > 3 else 0
        toplam_calisan = genel_toplam_result[0] if genel_toplam_result and len(genel_toplam_result) > 0 else 0
        toplam_mobilizasyon = genel_toplam_result[4] if genel_toplam_result and len(genel_toplam_result) > 4 else 0
        toplam_izinli = genel_toplam_result[2] if genel_toplam_result and len(genel_toplam_result) > 2 else 0
        toplam_hasta = genel_toplam_result[1] if genel_toplam_result and len(genel_toplam_result) > 1 else 0
        
        genel_toplam = toplam_staff + toplam_calisan + toplam_mobilizasyon
        
        tum_santiyeler = set(santiye_sorumlulari.keys())
        rapor_veren_santiyeler = set([row[0] for row in proje_detay_rows if row and len(row) > 0 and row[0]])
        eksik_santiyeler = [s for s in (tum_santiyeler - rapor_veren_santiyeler) if s not in ["Belli değil", "Tümü"]]
        
        mesaj = f"🗓️ AYLIK ÖZET RAPOR\n"
        mesaj += f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        
        mesaj += f"📈 PERFORMANS ANALİZİ:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor}\n"
        mesaj += f"• Toplam EKSIK Rapor: {eksik_rapor}\n"
        mesaj += f"• Pasif Kullanıcı: {len(en_pasif)}\n"
        mesaj += f"• İş Günü: {gun_sayisi} gün\n"
        mesaj += f"• Günlük Ort.: {toplam_rapor/gun_sayisi:.1f} rapor\n\n"
        
        mesaj += f"🔝 EN AKTİF 3 KULLANICI:\n"
        for i, row in enumerate(en_aktif, 1):
            if len(row) >= 2:
                user_id, rapor_sayisi = row[0], row[1]
                kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                emoji = "1️⃣" if i == 1 else "2️⃣" if i == 2 else "3️⃣"
                gunluk_ortalama = rapor_sayisi / gun_sayisi
                mesaj += f"{emoji} {kullanici_adi}: {rapor_sayisi} rapor (günlük: {gunluk_ortalama:.1f})\n"
        
        if en_pasif:
            mesaj += f"\n🔴 DÜŞÜK PERFORMANS (< %50 Katılım):\n"
            for i, row in enumerate(en_pasif[:3], 1):
                if len(row) >= 2:
                    user_id, rapor_sayisi = row[0], row[1]
                    kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                    katilim_orani = (rapor_sayisi / gun_sayisi) * 100
                    emoji = "1️⃣" if i == 1 else "2️⃣" if i == 2 else "3️⃣"
                    mesaj += f"{emoji} {kullanici_adi}: {rapor_sayisi} rapor (%{katilim_orani:.1f})\n"
        
        mesaj += f"\n🏗️ PROJE BAZLI PERSONEL:\n"
        
        onemli_projeler = ["SKP", "LOT13", "LOT71"]
        for row in proje_detay_rows:
            if len(row) >= 6:
                proje_adi, calisan, hasta, izinli, staff, mobilizasyon = row
                if proje_adi in onemli_projeler:
                    toplam_proje = (staff or 0) + (calisan or 0) + (mobilizasyon or 0)
                    if toplam_proje > 0:
                        mesaj += f"🏗️ {proje_adi}: {toplam_proje} kişi\n"
                        mesaj += f"   └─ Staff:{staff or 0}, Çalışan:{calisan or 0}, İzinli:{izinli or 0}, Hastalık:{hasta or 0}, Mobilizasyon:{mobilizasyon or 0}\n\n"
        
        for row in proje_detay_rows:
            if len(row) >= 6:
                proje_adi, calisan, hasta, izinli, staff, mobilizasyon = row
                if proje_adi not in onemli_projeler:
                    toplam_proje = (staff or 0) + (calisan or 0) + (mobilizasyon or 0)
                    if toplam_proje > 0:
                        emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                        mesaj += f"{emoji} {proje_adi}: {toplam_proje} kişi\n"
        
        mesaj += f"\n📈 GENEL TOPLAM: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 DAĞILIM:\n"
            if toplam_staff > 0:
                mesaj += f"• Staff: {toplam_staff} (%{toplam_staff/genel_toplam*100:.1f})\n"
            if toplam_calisan > 0:
                mesaj += f"• Çalışan: {toplam_calisan} (%{toplam_calisan/genel_toplam*100:.1f})\n"
            if toplam_mobilizasyon > 0:
                mesaj += f"• Mobilizasyon: {toplam_mobilizasyon} (%{toplam_mobilizasyon/genel_toplam*100:.1f})\n"
            if toplam_izinli > 0:
                mesaj += f"• İzinli: {toplam_izinli}\n"
            if toplam_hasta > 0:
                mesaj += f"• Hasta: {toplam_hasta}\n"
        
        if eksik_santiyeler:
            mesaj += f"\n❌ EKSİK: {', '.join(sorted(eksik_santiyeler))}"
        
        mesaj += "\n\n📝 Lütfen eksiksiz rapor paylaşımına devam edelim. Teşekkürler."
        
        return mesaj
        
    except Exception as e:
        return f"❌ Aylık rapor oluşturulurken hata: {e}"

async def generate_tarih_araligi_raporu(start_date, end_date):
    try:
        rows = await async_fetchall("""
            SELECT user_id, COUNT(*) as rapor_sayisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s
            GROUP BY user_id
            ORDER BY rapor_sayisi DESC
        """, (start_date, end_date))
        
        if not rows:
            return f"📭 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')} arasında rapor bulunamadı."
        
        toplam_rapor = sum([x[1] for x in rows if len(x) >= 2])
        gun_sayisi = (end_date - start_date).days + 1
        
        en_aktif = rows[:3]
        
        personel_result = await async_fetchone("""
            SELECT SUM(person_count) as toplam_kisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND report_type = 'RAPOR'
        """, (start_date, end_date))
        
        toplam_personel = personel_result[0] if personel_result and len(personel_result) > 0 else 0
        
        mesaj = f"📅 TARİH ARALIĞI RAPORU\n"
        mesaj += f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        
        mesaj += f"📊 GENEL İSTATİSTİKLER:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor}\n"
        mesaj += f"• Rapor Gönderen: {len(rows)} kişi\n"
        mesaj += f"• Gün Sayısı: {gun_sayisi} gün\n"
        mesaj += f"• Günlük Ort.: {toplam_rapor/gun_sayisi:.1f} rapor\n"
        mesaj += f"• Toplam Personel: {toplam_personel} kişi\n\n"
        
        mesaj += f"🔝 EN AKTİF 3 KULLANICI:\n"
        for i, row in enumerate(en_aktif, 1):
            if len(row) >= 2:
                user_id, rapor_sayisi = row[0], row[1]
                kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                emoji = "1️⃣" if i == 1 else "2️⃣" if i == 2 else "3️⃣"
                gunluk_ortalama = rapor_sayisi / gun_sayisi
                mesaj += f"{emoji} {kullanici_adi}: {rapor_sayisi} rapor (günlük: {gunluk_ortalama:.1f})\n"
        
        return mesaj
    except Exception as e:
        return f"❌ Tarih aralığı raporu oluşturulurken hata: {e}"

async def eksikraporlar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        mesaj = f"📊 EKSİK RAPORLAR - {bugun.strftime('%d.%m.%Y')}\n\n"
        
        if not durum['eksik_santiyeler']:
            mesaj += "🎉 Tüm şantiyeler raporlarını iletti! Harika iş!"
        else:
            mesaj += f"❌ Rapor İletilmeyen Şantiyeler ({len(durum['eksik_santiyeler'])}):\n\n"
            
            for santiye in sorted(durum['eksik_santiyeler']):
                if santiye in ["Belli değil", "Tümü"]:
                    continue
                sorumlular = santiye_sorumlulari.get(santiye, [])
                sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
                
                mesaj += f"🏗️ {santiye}\n"
                mesaj += f"   👥 Sorumlular: {', '.join(sorumlu_isimler)}\n\n"
        
        rapor_gonderenler = set()
        for santiye in durum['rapor_veren_santiyeler']:
            rapor_gonderenler.update(durum['santiye_rapor_verenler'].get(santiye, []))
        
        rapor_gondermeyenler = set(rapor_sorumlulari) - rapor_gonderenler
        
        if rapor_gondermeyenler:
            mesaj += f"👤 RAPOR GÖNDERMEYEN KULLANICILAR ({len(rapor_gondermeyenler)}):\n"
            for user_id in sorted(rapor_gondermeyenler):
                kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
                mesaj += f"• {kullanici_adi}\n"
        
        await update.message.reply_text(mesaj)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Eksik raporlar kontrol edilirken hata: {e}")

async def istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    try:
        bugun = dt.datetime.now(TZ).date()
        bu_hafta_baslangic = bugun - dt.timedelta(days=bugun.weekday())
        bu_ay_baslangic = bugun.replace(day=1)
        
        bugun_rapor = await async_fetchone("SELECT COUNT(*) FROM reports WHERE report_date = %s", (bugun,))
        bugun_rapor_sayisi = bugun_rapor[0] if bugun_rapor and len(bugun_rapor) > 0 else 0
        
        hafta_rapor = await async_fetchone("""
            SELECT COUNT(*) FROM reports WHERE report_date BETWEEN %s AND %s
        """, (bu_hafta_baslangic, bugun))
        hafta_rapor_sayisi = hafta_rapor[0] if hafta_rapor and len(hafta_rapor) > 0 else 0
        
        ay_rapor = await async_fetchone("""
            SELECT COUNT(*) FROM reports WHERE report_date BETWEEN %s AND %s
        """, (bu_ay_baslangic, bugun))
        ay_rapor_sayisi = ay_rapor[0] if ay_rapor and len(ay_rapor) > 0 else 0
        
        toplam_kullanici = await async_fetchone("SELECT COUNT(DISTINCT user_id) FROM reports")
        toplam_kullanici_sayisi = toplam_kullanici[0] if toplam_kullanici and len(toplam_kullanici) > 0 else 0
        
        toplam_rapor = await async_fetchone("SELECT COUNT(*) FROM reports")
        toplam_rapor_sayisi = toplam_rapor[0] if toplam_rapor and len(toplam_rapor) > 0 else 0
        
        en_aktif = await async_fetchone("""
            SELECT user_id, COUNT(*) as rapor_sayisi 
            FROM reports 
            GROUP BY user_id 
            ORDER BY rapor_sayisi DESC 
            LIMIT 1
        """)
        
        if en_aktif and len(en_aktif) >= 2 and en_aktif[0] is not None:
            en_aktif_kullanici = id_to_name.get(en_aktif[0], "Kullanıcı")
            en_aktif_rapor = en_aktif[1]
        else:
            en_aktif_kullanici = "Yok"
            en_aktif_rapor = 0
        
        mesaj = "📊 GENEL İSTATİSTİKLER\n\n"
        
        mesaj += "📅 GÜNLÜK İSTATİSTİKLER:\n"
        mesaj += f"• Bugünkü Rapor: {bugun_rapor_sayisi}\n"
        mesaj += f"• Bu Hafta: {hafta_rapor_sayisi}\n"
        mesaj += f"• Bu Ay: {ay_rapor_sayisi}\n\n"
        
        mesaj += "👥 KULLANICI İSTATİSTİKLERİ:\n"
        mesaj += f"• Toplam Kullanıcı: {toplam_kullanici_sayisi}\n"
        mesaj += f"• Rapor Sorumlusu: {len(rapor_sorumlulari)}\n"
        mesaj += f"• Admin: {len(ADMINS)}\n"
        mesaj += f"• Şantiye: {len(santiye_sorumlulari)}\n\n"
        
        mesaj += "🎯 PERFORMANS İSTATİSTİKLERİ:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor_sayisi}\n"
        mesaj += f"• En Aktif Kullanıcı: {en_aktif_kullanici} ({en_aktif_rapor} rapor)\n"
        
        if toplam_kullanici_sayisi > 0:
            ortalama_rapor = toplam_rapor_sayisi / toplam_kullanici_sayisi
            mesaj += f"• Kullanıcı Başı Ortalama: {ortalama_rapor:.1f} rapor\n"
        
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        mesaj += f"\n🏗️ BUGÜNKÜ ŞANTİYE DURUMU:\n"
        mesaj += f"• Rapor İleten: {len(durum['rapor_veren_santiyeler'])}/{len(durum['tum_santiyeler'])}\n"
        
        toplam_santiye = len(durum['tum_santiyeler'])
        if toplam_santiye > 0:
            basari_orani = (len(durum['rapor_veren_santiyeler']) / toplam_santiye) * 100
            mesaj += f"• Başarı Oranı: %{basari_orani:.1f}\n"
        else:
            mesaj += "• Başarı Oranı: %0.0\n"
        
        await update.message.reply_text(mesaj)
        
    except Exception as e:
        await update.message.reply_text(f"❌ İstatistikler oluşturulurken hata: {e}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Rapor Botu Aktif!\n\n"
        "Komutlar için `/info` yazın.\n\n"
        "📋 Temel Kullanım:\n"
        "• Rapor göndermek için direkt mesaj yazın\n"
        "• `/info` - Tüm komutları görüntüle\n"
        "• `/hakkinda` - Bot hakkında bilgi"
    )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name
    
    if is_admin(user_id):
        info_text = (
            f"🤖 Yapay Zeka Destekli Rapor Botu\n\n"
            f"👋 Hoş geldiniz {user_name}!\n\n"
            f"📋 Tüm Kullanıcılar İçin:\n"
            f"• Rapor göndermek için direkt mesaj yazın\n"
            f"`/start` - Botu başlat\n"
            f"`/info` - Komut bilgisi\n"
            f"`/hakkinda` - Bot hakkında\n\n"
            f"🛡️ Admin Komutları:\n"
            f"`/bugun` - Bugünün özeti\n"
            f"`/dun` - Dünün özeti\n"
            f"`/eksikraporlar` - Eksik raporları listele\n"
            f"`/istatistik` - Genel istatistikler\n"
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
            f"⚡ Super Admin Komutları:\n"
            f"`/reload` - Excel dosyasını yenile\n"
            f"`/yedekle` - Manuel yedekleme\n"
            f"`/chatid` - Chat ID göster\n"
            f"`/excel_durum` - Excel sistem durumu\n\n"
            f"🔒 Not: Komutlar yetkinize göre çalışacaktır."
        )
    else:
        info_text = (
            f"🤖 Yapay Zeka Destekli Rapor Botu\n\n"
            f"👋 Hoş geldiniz {user_name}!\n\n"
            f"📋 Kullanıcı Komutları:\n"
            f"• Rapor göndermek için direkt mesaj yazın\n"
            f"`/start` - Botu başlat\n"
            f"`/info` - Komut bilgisi\n"
            f"`/hakkinda` - Bot hakkında\n\n"
            f"🔒 Admin komutları sadece yetkililer içindir."
        )
    
    await update.message.reply_text(info_text)

async def hakkinda_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hakkinda_text = (
        "🤖 Rapor Botu Hakkında\n\n"
        "Geliştirici: Atamurat Kamalov\n"
        "Versiyon: 4.0 (Yeni OpenAI API + Google Drive Hazır)\n"
        "Özellikler:\n"
        "• Raporları otomatik analiz eder\n"
        "• Günlük / Haftalık / Aylık istatistik oluşturur\n"
        "• Her sabah 09:00'da dünkü personel icmalini Eren Boz'a gönderir\n"
        "• Çoklu rapor parsing yapar\n"
        "• Optimize edilmiş veritabanı kullanır\n"
        "• Gün içinde kullanıcıya otomatik hatırlatma mesajları gönderir\n"
        "• ve daha birçok özelliğe sahiptir\n\n"
        "Daha detaylı bilgi için /info yazın."
    )
    await update.message.reply_text(hakkinda_text)

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    
    await update.message.reply_text(
        f"📋 Chat ID Bilgileri:\n\n"
        f"👤 Kullanıcı ID: `{user_id}`\n"
        f"💬 Chat ID: `{chat_id}`\n"
        f"👥 Grup ID: `{GROUP_ID}`"
    )

async def bugun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    target_date = dt.datetime.now(TZ).date()
    await update.message.chat.send_action(action="typing")
    rapor_mesaji = await generate_gelismis_personel_ozeti(target_date)
    await update.message.reply_text(rapor_mesaji)

async def dun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    target_date = dt.datetime.now(TZ).date() - dt.timedelta(days=1)
    await update.message.chat.send_action(action="typing")
    rapor_mesaji = await generate_gelismis_personel_ozeti(target_date)
    await update.message.reply_text(rapor_mesaji)

async def haftalik_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today - dt.timedelta(days=today.weekday())
    end_date = start_date + dt.timedelta(days=6)
    
    mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj)

async def aylik_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today.replace(day=1)
    end_date = today
    
    mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj)

async def haftalik_istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today - dt.timedelta(days=today.weekday())
    end_date = start_date + dt.timedelta(days=6)
    
    mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj)

async def aylik_istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = dt.datetime.now(TZ).date()
    start_date = today.replace(day=1)
    end_date = today
    
    mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj)

async def tariharaligi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "📅 Tarih Aralığı Kullanımı:\n\n"
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
        
        await update.message.reply_text(mesaj)
        
    except Exception as e:
        await update.message.reply_text("❌ Tarih formatı hatalı. GG.AA.YYYY şeklinde girin.")

async def excel_tariharaligi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return

    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "📅 Excel Tarih Aralığı Raporu\n\n"
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

        await update.message.reply_text(mesaj)
        
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
    if not await admin_kontrol(update, context):
        return
    
    mesaj = "👥 TÜM KULLANICI LİSTESİ\n\n"
    
    mesaj += f"📋 Rapor Sorumluları ({len(rapor_sorumlulari)}):\n"
    for tid in rapor_sorumlulari:
        ad = id_to_name.get(tid, "Bilinmeyen")
        projeler = ", ".join(id_to_projects.get(tid, []))
        status = id_to_status.get(tid, "Belirsiz")
        rol = id_to_rol.get(tid, "Belirsiz")
        mesaj += f"• {ad}\n  📍 Projeler: {projeler}\n  🏷️ Status: {status}\n  👤 Rol: {rol}\n\n"
    
    admin_rapor_olmayanlar = [admin for admin in ADMINS if admin not in rapor_sorumlulari]
    if admin_rapor_olmayanlar:
        mesaj += f"🛡️ Adminler ({len(admin_rapor_olmayanlar)}):\n"
        for tid in admin_rapor_olmayanlar:
            ad = id_to_name.get(tid, "Bilinmeyen")
            rol = id_to_rol.get(tid, "Belirsiz")
            mesaj += f"• {ad} - {rol}\n"
        mesaj += "\n"
    
    if IZLEYICILER:
        mesaj += f"👀 İzleyiciler ({len(IZLEYICILER)}):\n"
        for tid in IZLEYICILER:
            ad = id_to_name.get(tid, "Bilinmeyen")
            mesaj += f"• {ad}\n"
    
    await update.message.reply_text(mesaj)

async def santiyeler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    mesaj = "🏗️ ŞANTİYE LİSTESİ ve SORUMLULARI\n\n"
    
    for santiye, sorumlular in sorted(santiye_sorumlulari.items()):
        sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
        mesaj += f"{santiye}\n"
        mesaj += f"  👥 Sorumlular: {', '.join(sorumlu_isimler)}\n\n"
    
    mesaj += f"📊 Toplam {len(santiye_sorumlulari)} şantiye"
    
    await update.message.reply_text(mesaj)

async def santiye_durum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    bugun = dt.datetime.now(TZ).date()
    durum = await get_santiye_bazli_rapor_durumu(bugun)
    
    mesaj = f"📊 Şantiye Rapor Durumu - {bugun.strftime('%d.%m.%Y')}\n\n"
    
    mesaj += f"✅ Rapor İleten Şantiyeler ({len(durum['rapor_veren_santiyeler'])}):\n"
    for santiye in sorted(durum['rapor_veren_santiyeler']):
        rapor_verenler = durum['santiye_rapor_verenler'].get(santiye, [])
        rapor_veren_isimler = [id_to_name.get(uid, f"Kullanıcı {uid}") for uid in rapor_verenler]
        
        if rapor_verenler:
            mesaj += f"• {santiye} - İleten: {', '.join(rapor_veren_isimler)}\n"
        else:
            mesaj += f"• {santiye} - Rapor iletildi\n"
    
    mesaj += f"\n❌ Rapor İletilmeyen Şantiyeler ({len(durum['eksik_santiyeler'])}):\n"
    for santiye in sorted(durum['eksik_santiyeler']):
        if santiye in ["Belli değil", "Tümü"]:
            continue
        sorumlular = santiye_sorumlulari.get(santiye, [])
        sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
        mesaj += f"• {santiye} - Sorumlular: {', '.join(sorumlu_isimler)}\n"
    
    mesaj += f"\n📈 Özet: {len(durum['rapor_veren_santiyeler'])}/{len(durum['tum_santiyeler'])} şantiye rapor iletmiş"
    
    await update.message.reply_text(mesaj)

async def maliyet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    rapor = maliyet_analiz.maliyet_raporu()
    await update.message.reply_text(rapor)

async def ai_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    rapor = maliyet_analiz.detayli_ai_raporu()
    await update.message.reply_text(rapor)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    global excel_file_hash, excel_last_modified
    excel_file_hash = None
    excel_last_modified = 0
    
    load_excel_intelligent()
    await update.message.reply_text("✅ Excel dosyası ZORUNLU yeniden yüklendi! (Önbellek temizlendi)")

async def create_excel_report(start_date, end_date, rapor_baslik):
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
        for row in rows:
            if len(row) < 10:
                continue
            user_id, tarih, rapor_tipi, icerik, kisi_sayisi, proje_adi, is_kategorisi, personel_tipi, delivered_date, is_edited = row
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

def schedule_jobs(app):
    jq = app.job_queue
    
    jq.run_repeating(auto_watch_excel, interval=60, first=10)
    jq.run_daily(gunluk_rapor_ozeti, time=dt.time(9, 0, tzinfo=TZ))
    
    jq.run_daily(hatirlatma_mesaji, time=dt.time(12, 30, tzinfo=TZ))
    jq.run_daily(ilk_rapor_kontrol, time=dt.time(15, 0, tzinfo=TZ))
    jq.run_daily(son_rapor_kontrol, time=dt.time(17, 30, tzinfo=TZ))
    
    jq.run_daily(yedekleme_gorevi, time=dt.time(23, 0, tzinfo=TZ))
    jq.run_daily(lambda context: yedekle_postgres(), time=dt.time(23, 10, tzinfo=TZ))
    
    jq.run_daily(haftalik_grup_raporu, time=dt.time(17, 40, tzinfo=TZ), days=(4,))
    
    jq.run_monthly(aylik_grup_raporu, when=dt.time(17, 45, tzinfo=TZ), day=28)
    
    logging.info("⏰ Tüm zamanlamalar ayarlandı")

async def auto_watch_excel(context: ContextTypes.DEFAULT_TYPE):
    try:
        load_excel_intelligent()
    except Exception as e:
        logging.error(f"Excel otomatik izleme hatası: {e}")

async def gunluk_rapor_ozeti(context: ContextTypes.DEFAULT_TYPE):
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
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        if not durum['eksik_santiyeler']:
            logging.info("🟡 12:30 - Tüm şantiyeler raporunu göndermiş")
            return
        
        mesaj = "🔔 Günlük Hatırlatma (Şantiye Bazlı)\n\n"
        mesaj += "Raporu henüz iletilmeyen şantiyeler:\n"
        
        for santiye in sorted(durum['eksik_santiyeler']):
            if santiye in ["Belli değil", "Tümü"]:
                continue
            sorumlular = santiye_sorumlulari.get(santiye, [])
            sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
            mesaj += f"• {santiye} - Sorumlular: {', '.join(sorumlu_isimler)}\n"
        
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
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        mesaj = "🕒 15:00 Şantiye Rapor Durumu\n\n"
        
        if durum['rapor_veren_santiyeler']:
            mesaj += f"✅ Rapor iletilen şantiyeler ({len(durum['rapor_veren_santiyeler'])}):\n"
            for santiye in sorted(durum['rapor_veren_santiyeler']):
                rapor_verenler = durum['santiye_rapor_verenler'].get(santiye, [])
                rapor_veren_isimler = [id_to_name.get(uid, f"Kullanıcı {uid}") for uid in rapor_verenler]
                
                if rapor_verenler:
                    mesaj += f"• {santiye} - Rapor ileten: {', '.join(rapor_veren_isimler)}\n"
                else:
                    mesaj += f"• {santiye} - Rapor iletildi\n"
            mesaj += "\n"
        else:
            mesaj += "✅ Rapor iletilen şantiyeler (0):\n\n"
        
        if durum['eksik_santiyeler']:
            mesaj += f"❌ Rapor iletilmeyen şantiyeler ({len(durum['eksik_santiyeler'])}):\n"
            for santiye in sorted(durum['eksik_santiyeler']):
                if santiye in ["Belli değil", "Tümü"]:
                    continue
                sorumlular = santiye_sorumlulari.get(santiye, [])
                sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
                mesaj += f"• {santiye} - Sorumlular: {', '.join(sorumlu_isimler)}\n"
        else:
            mesaj += "❌ Rapor iletilmeyen şantiyeler (0):\n"
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
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        result = await async_fetchone("SELECT COUNT(*) FROM reports WHERE report_date = %s", (bugun,))
        toplam_rapor = result[0] if result and len(result) > 0 else 0
        
        mesaj = "🕠 Gün Sonu Şantiye Rapor Analizi\n\n"
        
        if durum['eksik_santiyeler']:
            mesaj += f"❌ Rapor İletilmeyen Şantiyeler ({len(durum['eksik_santiyeler'])}):\n"
            for santiye in sorted(durum['eksik_santiyeler']):
                if santiye in ["Belli değil", "Tümü"]:
                    continue
                sorumlular = santiye_sorumlulari.get(santiye, [])
                sorumlu_isimler = [id_to_name.get(sid, f"Kullanıcı {sid}") for sid in sorumlular]
                mesaj += f"• {santiye} - Sorumlular: {', '.join(sorumlu_isimler)}\n"
        else:
            mesaj += "❌ Rapor İletilmeyen Şantiyeler (0):\n"
            mesaj += "🎉 Tüm şantiyeler raporlarını iletti!\n"
        
        mesaj += f"\n📊 Bugün toplam {toplam_rapor} rapor alındı."
        mesaj += f"\n🏗️ {len(durum['rapor_veren_santiyeler'])}/{len(durum['tum_santiyeler'])} şantiye rapor iletmiş durumda."
        
        for user_id in rapor_sorumlulari:
            try:
                await context.bot.send_message(chat_id=user_id, text=mesaj)
                logging.info(f"🔴 Şantiye gün sonu analizi {user_id} kullanıcısına gönderildi")
                await asyncio.sleep(0.3)
            except Exception as e:
                logging.error(f"🔴 {user_id} kullanıcısına şantiye gün sonu analizi gönderilemedi: {e}")
        
        admin_mesaj = f"📋 Gün Sonu Şantiye Özeti - {bugun.strftime('%d.%m.%Y')}\n\n"
        
        if durum['rapor_veren_santiyeler']:
            admin_mesaj += f"✅ Rapor İleten Şantiyeler ({len(durum['rapor_veren_santiyeler'])}):\n"
            for santiye in sorted(durum['rapor_veren_santiyeler']):
                rapor_verenler = durum['santiye_rapor_verenler'].get(santiye, [])
                rapor_veren_isimler = [id_to_name.get(uid, f"Kullanıcı {uid}") for uid in rapor_verenler]
                
                if rapor_verenler:
                    admin_mesaj += f"• {santiye} - İleten: {', '.join(rapor_veren_isimler)}\n"
                else:
                    admin_mesaj += f"• {santiye} - Rapor iletildi\n"
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
    try:
        today = dt.datetime.now(TZ).date()
        start_date = today - dt.timedelta(days=today.weekday() + 7)
        end_date = start_date + dt.timedelta(days=6)
        
        mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
        
        if GROUP_ID:
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj)
                logging.info(f"📊 Haftalık grup raporu gönderildi: {start_date} - {end_date}")
            except Exception as e:
                logging.error(f"📊 Haftalık grup raporu gönderilemedi: {e}")
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=mesaj)
                logging.info(f"📊 Haftalık rapor {admin_id} adminine gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"📊 {admin_id} adminine haftalık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"📊 Haftalık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Haftalık grup raporu hatası: {e}")

async def aylik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    try:
        today = dt.datetime.now(TZ).date()
        start_date = today.replace(day=1) - dt.timedelta(days=1)
        start_date = start_date.replace(day=1)
        end_date = today.replace(day=1) - dt.timedelta(days=1)
        
        mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
        
        if GROUP_ID:
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj)
                logging.info(f"🗓️ Aylık grup raporu gönderildi: {start_date} - {end_date}")
            except Exception as e:
                logging.error(f"🗓️ Aylık grup raporu gönderilemedi: {e}")
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=mesaj)
                logging.info(f"🗓️ Aylık rapor {admin_id} adminine gönderildi")
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"🗓️ {admin_id} adminine aylık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🗓️ Aylık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Aylık grup raporu hatası: {e}")

async def bot_baslatici_mesaji(context: ContextTypes.DEFAULT_TYPE):
    try:
        mesaj = "🤖 Rapor Kontrol Botu Aktif!\n\nKontrol bende ⚡️\nKolay gelsin 👷‍♂️"
        
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
    commands = [
        BotCommand("start", "Botu başlat"),
        BotCommand("info", "Komut bilgisi (Tüm kullanıcılar)"),
        BotCommand("hakkinda", "Bot hakkında bilgi"),
        
        BotCommand("bugun", "Bugünün özeti (Admin)"),
        BotCommand("dun", "Dünün özeti (Admin)"),
        BotCommand("eksikraporlar", "Eksik raporları listele (Admin)"),
        BotCommand("istatistik", "Genel istatistikler (Admin)"),
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
        BotCommand("excel_durum", "Excel sistem durumu (Super Admin)"),
    ]
    await application.bot.set_my_commands(commands)
    
    await bot_baslatici_mesaji(application)

def main():
    try:
        app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
        
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("info", info_cmd))
        app.add_handler(CommandHandler("hakkinda", hakkinda_cmd))
        
        app.add_handler(CommandHandler("bugun", bugun_cmd))
        app.add_handler(CommandHandler("dun", dun_cmd))
        app.add_handler(CommandHandler("eksikraporlar", eksikraporlar_cmd))
        app.add_handler(CommandHandler("istatistik", istatistik_cmd))
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
        
        app.add_handler(CommandHandler("reload", reload_cmd))
        app.add_handler(CommandHandler("yedekle", yedekle_cmd))
        app.add_handler(CommandHandler("chatid", chatid_cmd))
        app.add_handler(CommandHandler("excel_durum", excel_durum_cmd))
        
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, yeni_uye_karşilama))
        
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), 
            yeni_gpt_rapor_isleme
        ))

        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, 
            yeni_gpt_rapor_isleme
        ))

        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & filters.UpdateType.EDITED_MESSAGE, 
            yeni_gpt_rapor_isleme
        ))

        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & filters.UpdateType.EDITED_MESSAGE, 
            yeni_gpt_rapor_isleme
        ))
        
        schedule_jobs(app)
        logging.info("🚀 TÜM KARARLAR UYGULANDI - Rapor Botu başlatılıyor...")
        
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logging.error(f"❌ Bot başlatma hatası: {e}")
        raise

if __name__ == "__main__":
    main()