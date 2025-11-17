```python
import os
import re
import psycopg2
import pandas as pd
import json
from datetime import datetime, time, timedelta
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
import logging
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import tempfile
import requests
import asyncio
import functools
from psycopg2 import pool
import html
from bs4 import BeautifulSoup
import base64

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
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return rows
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

def _sync_execute(query, params=()):
    """Sync execute fonksiyonu"""
    conn = get_conn_from_pool()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        return cur.rowcount
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

def _sync_fetchone(query, params=()):
    """Sync fetchone fonksiyonu"""
    conn = get_conn_from_pool()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return row
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

async def async_db_query(func, *args, **kwargs):
    """Async database sorgusu"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

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
    try:
        if not YANDEX_DISK_TOKEN:
            logging.error("❌ Yandex.Disk token bulunamadı!")
            return False
            
        if not os.path.exists(file_path):
            logging.error(f"❌ Yedeklenecek dosya bulunamadı: {file_path}")
            return False
            
        headers = {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}
        upload_url = "https://cloud-api.yandex.net/v1/disk/resources/upload"
        params = {"path": yandex_path, "overwrite": "true"}
        
        resp = requests.get(upload_url, headers=headers, params=params, timeout=30)
        
        if resp.status_code != 200:
            logging.error(f"❌ Yandex API hatası ({resp.status_code}): {resp.text}")
            return False
            
        href = resp.json().get("href")
        
        if href:
            with open(file_path, "rb") as f:
                upload_resp = requests.put(href, data=f, timeout=60)
                if upload_resp.status_code == 201:
                    file_size = os.path.getsize(file_path) / (1024 * 1024)
                    logging.info(f"✅ Yandex.Disk'e yüklendi: {yandex_path} ({file_size:.2f} MB)")
                    return True
                else:
                    logging.error(f"❌ Yükleme hatası ({upload_resp.status_code}): {upload_resp.text}")
                    return False
        else:
            logging.error(f"❌ Upload linki alınamadı: {resp.text}")
            return False
            
    except Exception as e:
        logging.error(f"❌ Yandex yedekleme hatası: {e}")
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
        status_msg += f"📅 Tarih: {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}\n"
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

# ----------------------------- OPENAI -----------------------------
try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    logging.warning("OpenAI paketi yüklü değil. AI özellikleri devre dışı.")

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
    
    if time.time() - user_role_cache_time > 300:
        user_role_cache = {}
        user_role_cache_time = time.time()
    
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
        except:
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
                logging.info(f"Admin eklendi: {fullname} (ID: {tid}, Rol: {rol}")
            
            if rol == "İZLEYİCİ":
                temp_izleyiciler.append(tid)
                logging.info(f"İzleyici eklendi: {fullname} (ID: {tid})")
            
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
        logging.info(f"Super Admin eklendi: {SUPER_ADMIN_ID}")
    
    last_excel_update = os.path.getmtime(USERS_FILE) if os.path.exists(USERS_FILE) else 0
    logging.info(f"Excel yüklendi: {len(rapor_sorumlulari)} takip edilen kullanıcı, {len(ADMINS)} admin, {len(IZLEYICILER)} izleyici, {len(TUM_KULLANICILAR)} toplam kullanıcı, {len(santiye_sorumlulari)} şantiye")

load_excel()

# PostgreSQL bağlantısı
def get_db_connection():
    """PostgreSQL bağlantısını döndür"""
    return psycopg2.connect(os.environ['DATABASE_URL'], sslmode='require')

# ----------------------------- ŞANTİYE-MERKEZLİ HTML IMPORT SİSTEMİ -----------------------------
class SantiyeMerkezliHTMLImporter:
    def __init__(self):
        self.santiye_esleme_cache = {}
        
    def parse_html_file(self, html_file_path):
        """HTML dosyasını parse eder - Şantiye merkezli versiyon"""
        try:
            with open(html_file_path, 'r', encoding='utf-8') as file:
                soup = BeautifulSoup(file, 'html.parser')
                return self.extract_santiye_bazli_mesajlar(soup)
        except Exception as e:
            logging.error(f"HTML dosyası okuma hatası: {e}")
            return []
    
    def extract_santiye_bazli_mesajlar(self, soup):
        """Şantiye bazlı mesaj çıkarma"""
        messages = []
        
        message_containers = soup.find_all('div', class_=lambda x: x and 'message' in x)
        
        current_date = None
        
        for container in message_containers:
            if 'service' in container.get('class', []):
                date_text = container.get_text().strip()
                try:
                    current_date = datetime.strptime(date_text, '%d %B %Y').date()
                    logging.info(f"📅 Tarih bulundu: {current_date}")
                except ValueError:
                    continue
            
            elif 'default' in container.get('class', []):
                if current_date is None:
                    continue
                    
                message_data = self.parse_message_for_santiye(container, current_date)
                if message_data and self.is_valid_rapor_message(message_data):
                    messages.append(message_data)
        
        return messages
    
    def parse_message_for_santiye(self, element, current_date):
        """Mesajı şantiye bazlı parse et"""
        try:
            message_id = element.get('id', '').replace('message-', '')
            if not message_id or not message_id.isdigit():
                return None
            
            from_name_elem = element.find('div', class_='from_name')
            if not from_name_elem:
                return None
                
            from_name = from_name_elem.get_text().strip()
            
            text_elem = element.find('div', class_='text')
            if not text_elem:
                return None
                
            message_text = text_elem.get_text().strip()
            
            return {
                'message_id': int(message_id),
                'from_name': from_name,
                'message_text': message_text,
                'message_date': current_date,
                'is_edited': False,
                'delivered_date': current_date
            }
            
        except Exception as e:
            logging.error(f"Mesaj parse hatası: {e}")
            return None
    
    def is_valid_rapor_message(self, message_data):
        """Rapor mesajı olup olmadığını kontrol et - Şantiye bazlı"""
        text = message_data['message_text'].lower()
        
        rapor_indicator = any([
            'mobilizasyon' in text,
            'kişi' in text,
            'personel' in text,
            'toplam' in text and any(char.isdigit() for char in text),
            re.search(r'\d{1,2}\.\d{1,2}\.\d{4}', text),
            any(santiye.lower() in text for santiye in santiye_sorumlulari.keys())
        ])
        
        spam_indicators = [
            'kolay gelsin',
            'teşekkür',
            'merhaba',
            'selam',
            'hakkında',
            'komut',
            'yedekleme',
            'yedekle',
            'chatid'
        ]
        
        is_spam = any(indicator in text for indicator in spam_indicators)
        
        return rapor_indicator and not is_spam and len(text) > 20

class SantiyeAIAnaliz:
    def __init__(self, api_key):
        if HAS_OPENAI and api_key:
            self.client = openai.OpenAI(api_key=api_key)
            self.aktif = True
            self.model = "gpt-4o-mini"
            self.cache = {}
            logging.info(f"🤖 Şantiye AI Analiz sistemi aktif! Model: {self.model}")
        else:
            self.aktif = False
            logging.warning("OpenAI devre dışı.")
    
    def santiye_ve_kullanici_analiz_et(self, mesaj_metni, gonderici_adi):
        """Şantiye ve kullanıcı analizi"""
        if not self.aktif:
            return self._fallback_santiye_analiz()
            
        try:
            cache_key = f"santiye_{hash(mesaj_metni[:100])}"
            if cache_key in self.cache:
                return self.cache[cache_key]
            
            santiyeler_listesi = list(santiye_sorumlulari.keys())
            kullanici_listesi = [f"{id_to_name.get(uid, 'Bilinmeyen')} (ID:{uid})" for uid in rapor_sorumlulari]
            
            sistem_promtu = f"""
SEN BİR ŞANTİYE RAPOR ANALİZ ASİSTANISIN. SADECE JSON VER.

**KRİTİK KURAL:** Raporun kimden geldiği DEĞİL, hangi şantiye için olduğu önemli!

**MEVCUT SANTİYELER:** {santiyeler_listesi}
**MEVCUT KULLANICILAR:** {kullanici_listesi}

**ANALİZ KURALLARI:**
1. Önce mesajdaki ŞANTİYE adını bul (%95 emin değilsen "BELİRSİZ" yaz)
2. Şantiye bulunduktan sonra, o şantiyenin SORUMLUSUNU bul
3. Gönderen kişi önemsiz, önemli olan şantiye
4. Eğer mesajda birden fazla şantiye varsa, her biri için ayrı kayıt oluştur

**ÇIKTI formatı:**
{{
 "santiyeler": [
   {{
     "santiye_adi": "BWC",
     "eminlik_orani": 0.98,
     "rapor_metni": "BWC için kısaltılmış rapor",
     "sorumlu_kullanici_id": 123456789
   }}
 ],
 "aciklama": "Analiz detayı"
}}
"""
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": sistem_promtu},
                    {"role": "user", "content": f"GÖNDEREN: {gonderici_adi}\nMESAJ: {mesaj_metni}"}
                ],
                temperature=0.1,
                max_tokens=500,
                response_format={ "type": "json_object" }
            )
            
            cevap = response.choices[0].message.content.strip()
            sonuc = json.loads(cevap)
            sonuc["kaynak"] = "gpt"
            
            for santiye_data in sonuc.get("santiyeler", []):
                santiye_adi = santiye_data.get("santiye_adi")
                if santiye_adi and santiye_adi != "BELİRSİZ":
                    sorumlular = santiye_sorumlulari.get(santiye_adi, [])
                    if sorumlular:
                        santiye_data["sorumlu_kullanici_id"] = sorumlular[0]
                    else:
                        santiye_data["sorumlu_kullanici_id"] = None
            
            self.cache[cache_key] = sonuc
            logging.info(f"🤖 Şantiye analizi: {len(sonuc.get('santiyeler', []))} şantiye bulundu")
            
            return sonuc
            
        except Exception as e:
            logging.error(f"Şantiye AI analiz hatası: {e}")
            sonuc = self._fallback_santiye_analiz()
            return sonuc
    
    def _fallback_santiye_analiz(self):
        """Fallback şantiye analizi"""
        return {
            "santiyeler": [],
            "aciklama": "Fallback analiz",
            "kaynak": "fallback"
        }

class SantiyeImportManager:
    def __init__(self):
        self.processed_ids = set()
        self.santiye_ai = SantiyeAIAnaliz(OPENAI_API_KEY)
        self.load_existing_ids()
    
    def load_existing_ids(self):
        """Mevcut mesaj ID'lerini yükle"""
        try:
            rows = _sync_fetchall("SELECT message_id FROM reports WHERE message_id IS NOT NULL")
            self.processed_ids = set([row[0] for row in rows])
            logging.info(f"📊 Mevcut {len(self.processed_ids)} mesaj ID'si yüklendi")
        except Exception as e:
            logging.error(f"Mevcut ID yükleme hatası: {e}")
    
    async def get_rapor_alan_santiyeler(self, tarih):
        """Belirli bir tarihte rapor alan şantiyeleri getir"""
        try:
            rows = await async_fetchall("""
                SELECT DISTINCT project_name FROM reports 
                WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
            """, (tarih,))
            
            return set([row[0] for row in rows])
        except Exception as e:
            logging.error(f"Rapor alan şantiyeler sorgu hatası: {e}")
            return set()
    
    async def import_santiye_mesajlari(self, messages, batch_size=30):
        """Şantiye bazlı mesaj importu"""
        imported_count = 0
        skipped_count = 0
        error_count = 0
        
        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]
            
            for message_data in batch:
                if await self.should_import_message(message_data):
                    try:
                        santiye_kayit_sayisi = await self.import_single_santiye_message(message_data)
                        imported_count += santiye_kayit_sayisi
                        
                        if imported_count % 10 == 0:
                            logging.info(f"📥 {imported_count} şantiye kaydı import edildi...")
                            
                    except Exception as e:
                        logging.error(f"Şantiye import hatası: {e}")
                        error_count += 1
                else:
                    skipped_count += 1
            
            await asyncio.sleep(0.1)
        
        await self.rapor_eksik_santiyeler(messages)
        
        return imported_count, skipped_count, error_count, {}
    
    async def should_import_message(self, message_data):
        """Mesajın import edilip edilmeyeceğini kontrol et"""
        message_id = message_data.get('message_id')
        
        if message_id in self.processed_ids:
            return False
        
        message_date = message_data.get('message_date')
        if message_date and message_date < datetime(2025, 11, 1).date():
            return False
        
        return True
    
    async def import_single_santiye_message(self, message_data):
        """Tekil mesajı şantiye bazlı import et"""
        message_text = message_data['message_text']
        gonderici_adi = message_data['from_name']
        message_date = message_data['message_date']
        
        ai_sonuc = self.santiye_ai.santiye_ve_kullanici_analiz_et(message_text, gonderici_adi)
        
        kayit_sayisi = 0
        
        for santiye_data in ai_sonuc.get("santiyeler", []):
            santiye_adi = santiye_data.get("santiye_adi")
            sorumlu_kullanici_id = santiye_data.get("sorumlu_kullanici_id")
            eminlik_orani = santiye_data.get("eminlik_orani", 0)
            
            if eminlik_orani < 0.95 or not santiye_adi or santiye_adi == "BELİRSİZ":
                continue
            
            if not sorumlu_kullanici_id:
                logging.warning(f"⚠️ {santiye_adi} şantiyesi için sorumlu bulunamadı")
                continue
            
            try:
                await self.kaydet_santiye_raporu(
                    sorumlu_kullanici_id,
                    santiye_adi,
                    message_text,
                    message_date,
                    message_data,
                    ai_sonuc
                )
                kayit_sayisi += 1
                
            except Exception as e:
                logging.error(f"Şantiye rapor kaydetme hatası: {e}")
        
        self.processed_ids.add(message_data['message_id'])
        
        return kayit_sayisi
    
    async def kaydet_santiye_raporu(self, user_id, santiye_adi, message_text, message_date, message_data, ai_sonuc):
        """Şantiye raporunu veritabanına kaydet"""
        rapor_tipi = 'IZIN/ISYOK' if izin_mi(message_text) else 'RAPOR'
        
        kisi_sayisi = 1
        kisi_match = re.search(r'(\d+)\s*(kişi|personel|çalışan)', message_text.lower())
        if kisi_match:
            kisi_sayisi = int(kisi_match.group(1))
        
        await async_execute("""
            INSERT INTO reports 
            (user_id, project_name, report_date, report_type, person_count, work_description, 
             work_category, personnel_type, delivered_date, is_edited, ai_analysis, message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id, santiye_adi, message_date, rapor_tipi, kisi_sayisi, 
            message_text[:500], 'diğer', 'imalat', message_date,
            False,
            json.dumps(ai_sonuc, ensure_ascii=False) if ai_sonuc else None,
            message_data['message_id']
        ))
        
        if ai_sonuc and 'kaynak' in ai_sonuc:
            maliyet_analiz.kayit_ekle(ai_sonuc['kaynak'])
        
        logging.info(f"✅ Şantiye raporu kaydedildi: {santiye_adi} -> {id_to_name.get(user_id, 'Kullanıcı')}")
    
    async def rapor_eksik_santiyeler(self, tum_mesajlar):
        """Hiç rapor gelmeyen şantiyeleri tespit et ve raporla"""
        try:
            tum_tarihler = set()
            for msg in tum_mesajlar:
                tum_tarihler.add(msg['message_date'])
            
            for tarih in tum_tarihler:
                rapor_alan_santiyeler = await self.get_rapor_alan_santiyeler(tarih)
                tum_santiyeler = set(santiye_sorumlulari.keys())
                eksik_santiyeler = tum_santiyeler - rapor_alan_santiyeler
                
                if eksik_santiyeler:
                    logging.warning(f"📅 {tarih}: {len(eksik_santiyeler)} şantiye raporu eksik: {eksik_santiyeler}")
                    
        except Exception as e:
            logging.error(f"Eksik şantiye analiz hatası: {e}")

# Global şantiye import manager
santiye_import_manager = SantiyeImportManager()

# ----------------------------- MANUEL RAPOR IMPORT SİSTEMİ -----------------------------
async def import_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manuel rapor import komutu - Sadece Super Admin"""
    if not await super_admin_kontrol(update, context):
        return
    
    await update.message.reply_text(
        "📁 **Manuel Rapor Import Sistemi**\n\n"
        "1. **HTML dosyası yükleyin** (Telegram export) VEYA\n"
        "2. **Direkt mesaj içeriklerini** gönderin\n\n"
        "Bot otomatik olarak:\n"
        "• Rapor içeriklerini tespit edecek\n"
        "• Şantiyeleri belirleyecek\n"
        "• Sorumluları atayacak\n"
        "• Veritabanına kaydedecek\n\n"
        "⏳ Lütfen HTML dosyasını yükleyin veya mesaj içeriklerini gönderin..."
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """HTML dosyası işleme"""
    if not await super_admin_kontrol(update, context):
        return
    
    document = update.message.document
    if document.mime_type != 'text/html':
        await update.message.reply_text("❌ Sadece HTML dosyaları destekleniyor.")
        return
    
    file = await context.bot.get_file(document.file_id)
    file_path = f"temp_import_{document.file_id}.html"
    
    await update.message.reply_text("📥 HTML dosyası indiriliyor...")
    
    try:
        await file.download_to_drive(file_path)
        await update.message.reply_text(f"✅ Dosya indirildi: {document.file_name}")
        
        await process_import_file(update, context, file_path)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Dosya işleme hatası: {e}")
    finally:
        if os.path.exists(file_path):
            os.unlink(file_path)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Metin mesajlarını rapor olarak işleme"""
    user_id = update.message.from_user.id
    if user_id != SUPER_ADMIN_ID:
        return
    
    message_text = update.message.text
    
    if message_text.startswith('/'):
        return
    
    await update.message.reply_text("📝 Metin içeriği rapor olarak işleniyor...")
    
    try:
        importer = SantiyeMerkezliHTMLImporter()
        
        fake_message = {
            'message_id': int(datetime.now().timestamp()),
            'from_name': 'Manuel Import',
            'message_text': message_text,
            'message_date': datetime.now(TZ).date(),
            'is_edited': False,
            'delivered_date': datetime.now(TZ).date()
        }
        
        messages = [fake_message]
        
        imported, skipped, errors, _ = await santiye_import_manager.import_santiye_mesajlari(messages)
        
        result_msg = (
            f"✅ **Manuel Rapor Import Tamamlandı!**\n\n"
            f"📊 **Sonuçlar:**\n"
            f"• 📥 İşlenen: {imported} rapor\n"
            f"• ⏭️ Atlanan: {skipped} mesaj\n"
            f"• ❌ Hatalı: {errors} kayıt\n\n"
            f"🎯 Rapor başarıyla veritabanına kaydedildi."
        )
        
        await update.message.reply_text(result_msg)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Manuel import hatası: {e}")

async def process_import_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str):
    """Import dosyasını işleme"""
    try:
        await update.message.reply_text("🔄 Rapor içerikleri analiz ediliyor...")
        
        importer = SantiyeMerkezliHTMLImporter()
        messages = importer.parse_html_file(file_path)
        
        if not messages:
            await update.message.reply_text("❌ İşlenecek rapor bulunamadı.")
            return
        
        total_messages = len(messages)
        await update.message.reply_text(f"📊 {total_messages} mesaj bulundu. Şantiye analizi başlıyor...")
        
        imported, skipped, errors, _ = await santiye_import_manager.import_santiye_mesajlari(messages)
        
        result_msg = (
            f"✅ **Rapor Import Tamamlandı!**\n\n"
            f"📈 **Detaylı Sonuçlar:**\n"
            f"• 📋 Toplam Mesaj: {total_messages}\n"
            f"• 📥 İşlenen Rapor: {imported}\n"
            f"• ⏭️ Atlanan: {skipped}\n"
            f"• ❌ Hatalı: {errors}\n\n"
            f"🎯 Raporlar şantiye bazlı işlendi ve veritabanına kaydedildi."
        )
        
        await update.message.reply_text(result_msg)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Import işlemi hatası: {e}")

# ----------------------------- VERİTABANI ŞEMA GÜNCELLEMESİ -----------------------------
def update_database_schema():
    """Gerekli veritabanı şema güncellemelerini yap"""
    try:
        _sync_execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                              WHERE table_name='reports' AND column_name='message_id') THEN
                    ALTER TABLE reports ADD COLUMN message_id BIGINT;
                    CREATE INDEX IF NOT EXISTS idx_reports_message_id ON reports(message_id);
                END IF;
            END $$;
        """)
        
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
        
        _sync_execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date)")
        _sync_execute("CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id)")
        _sync_execute("CREATE INDEX IF NOT EXISTS idx_reports_project ON reports(project_name)")
        _sync_execute("CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type)")
        _sync_execute("CREATE INDEX IF NOT EXISTS idx_reports_message_id ON reports(message_id)")
        
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
            WHERE report_date = %s AND project_name IS NOT NULL
        """, (bugun,))
        
        rapor_veren_santiyeler = set()
        
        for (project_name,) in rows:
            if project_name and project_name != 'BELİRSİZ':
                rapor_veren_santiyeler.add(project_name)
        
        return rapor_veren_santiyeler
    except Exception as e:
        logging.error(f"Şantiye rapor durumu hatası: {e}")
        return set()

async def get_eksik_santiyeler(bugun):
    """Raporu eksik olan şantiyeleri ve sorumlularını getir"""
    tum_santiyeler = set(santiye_sorumlulari.keys())
    rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
    eksik_santiyeler = tum_santiyeler - rapor_veren_santiyeler
    
    eksik_santiye_sorumlulari = {}
    for santiye in eksik_santiyeler:
        sorumlular = santiye_sorumlulari.get(santiye, [])
        eksik_santiye_sorumlulari[santiye] = sorumlular
    
    return eksik_santiye_sorumlulari

async def get_santiye_bazli_rapor_durumu(bugun):
    """Şantiye bazlı detaylı rapor durumu"""
    tum_santiyeler = set(santiye_sorumlulari.keys())
    rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
    
    santiye_rapor_verenler = {}
    rows = await async_fetchall("""
        SELECT user_id, project_name FROM reports 
        WHERE report_date = %s AND project_name IS NOT NULL
    """, (bugun,))
    
    for user_id, project_name in rows:
        if project_name and project_name != 'BELİRSİZ':
            if project_name not in santiye_rapor_verenler:
                santiye_rapor_verenler[project_name] = []
            santiye_rapor_verenler[project_name].append(user_id)
    
    return {
        'tum_santiyeler': tum_santiyeler,
        'rapor_veren_santiyeler': rapor_veren_santiyeler,
        'eksik_santiyeler': tum_santiyeler - rapor_veren_santiyeler,
        'santiye_rapor_verenler': santiye_rapor_verenler
    }

# ----------------------------- OPTİMİZE AI SİSTEMİ -----------------------------
class OptimizeAkilliRaporAnalizAI:
    def __init__(self, api_key):
        if HAS_OPENAI and api_key:
            self.client = openai.OpenAI(api_key=api_key)
            self.aktif = True
            self.model = "gpt-4o-mini"
            self.cache = {}
            logging.info(f"OPTİMİZE AI sistemi aktif! Model: {self.model}")
        else:
            self.aktif = False
            logging.warning("OpenAI devre dışı.")
    
    def gelismis_analiz_et(self, rapor_metni, kullanici_adi, kullanici_projeleri=None):
        """Yeni veritabanı yapısına uygun analiz"""
        if not self.aktif:
            sonuc = self._fallback_analiz()
            self._log_ai_kullanimi(rapor_metni, sonuc, False, "OpenAI devre dışı")
            return sonuc
            
        try:
            cache_key = f"gpt_{hash(rapor_metni[:100])}"
            if cache_key in self.cache:
                return self.cache[cache_key]
            
            proje_bilgisi = ""
            if kullanici_projeleri:
                proje_bilgisi = f"Kullanıcının sorumlu olduğu projeler: {', '.join(kullanici_projeleri)}"
            
            sistem_promtu = f"""
SEN BİR ŞANTİYE RAPOR ANALİZ ASİSTANISIN. SADECE JSON VER.
Aşağıdaki kurallara %100 UY:

1) Sadece geçerli bir JSON döndür.
2) Tarihi mutlaka GG.AA.YYYY formatına çevir.
3) Eğer raporda tarih yoksa bugünü kullanma → mantıklı tahmin yap.
4) Rapor tipi:
   - 'izin', 'rapor yok', 'iş yok' → IS_YOK
   - 'izinliyim', 'hastayım' → IZIN
   - Diğer tüm durumlar → RAPOR
5) Kişi sayısı:
   - Raporda sayı geçiyorsa onu kullan.
   - Geçmiyorsa 1 kişi varsay.
6) Yapılan iş metnini mümkün olduğunca kısa ama öz yaz.
7) Proje adını rapordaki kelimelerden mantıklı şekilde bul.
8) Eksik bilgileri tahmin et ama GERÇEKÇİ OL.

ÇIKTI formatı:

{{
 "rapor_tarihi": "GG.AA.YYYY",
 "kisi_sayisi": 1,
 "yapilan_is": "kısa açıklama",
 "proje_adi": "adı",
 "rapor_tipi": "RAPOR / IZIN / IS_YOK",
 "aciklama": "detaylı analiz"
}}
"""
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": sistem_promtu},
                    {"role": "user", "content": f"KULLANICI: {kullanici_adi}\nRAPOR METNİ: {rapor_metni}"}
                ],
                temperature=0.1,
                max_tokens=400,
                response_format={ "type": "json_object" }
            )
            
            cevap = response.choices[0].message.content.strip()
            sonuc = json.loads(cevap)
            sonuc["kaynak"] = "gpt"
            
            self.cache[cache_key] = sonuc
            logging.info(f"🤖 GPT ile analiz edildi: {sonuc.get('proje_adi', 'BELİRSİZ')}")
            
            self._log_ai_kullanimi(rapor_metni, sonuc, True)
            
            return sonuc
            
        except Exception as e:
            logging.error(f"GPT analiz hatası: {e}")
            sonuc = self._fallback_analiz()
            self._log_ai_kullanimi(rapor_metni, sonuc, False, str(e))
            return sonuc
    
    def _fallback_analiz(self):
        """GPT başarısız olursa kullanılacak fallback analiz"""
        return {
            "rapor_tarihi": datetime.now(TZ).strftime('%d.%m.%Y'),
            "kisi_sayisi": 1,
            "yapilan_is": "Analiz edilemedi",
            "proje_adi": "BELİRSİZ", 
            "rapor_tipi": "RAPOR",
            "aciklama": "Fallback analiz",
            "kaynak": "fallback"
        }
    
    def _log_ai_kullanimi(self, rapor_metni, ai_sonuc, basarili, hata_mesaji=None):
        """AI kullanımını database'e logla"""
        try:
            _sync_execute("""
                INSERT INTO ai_logs (timestamp, user_id, rapor_metni, ai_cevap, basarili, hata_mesaji)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                datetime.now(TZ).isoformat(),
                0,
                rapor_metni[:500],
                json.dumps(ai_sonuc, ensure_ascii=False)[:1000],
                1 if basarili else 0,
                hata_mesaji
            ))
        except Exception as e:
            logging.error(f"AI log kaydetme hatası: {e}")

ai_analiz = OptimizeAkilliRaporAnalizAI(OPENAI_API_KEY)

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
            istatistik = result
            
            rows = _sync_fetchall("""
                SELECT DATE(timestamp) as gun, 
                       COUNT(*) as toplam,
                       SUM(CASE WHEN basarili = 1 THEN 1 ELSE 0 END) as basarili
                FROM ai_logs 
                GROUP BY DATE(timestamp) 
                ORDER BY gun DESC 
                LIMIT 7
            """)
            gunluk_istatistik = rows
            
            rapor = "🤖 **DETAYLI AI RAPORU**\n\n"
            rapor += f"📈 **Genel İstatistikler:**\n"
            rapor += f"• Toplam İşlem: {istatistik[0]}\n"
            rapor += f"• Başarılı: {istatistik[1]} (%{(istatistik[1]/istatistik[0]*100) if istatistik[0] > 0 else 0:.1f})\n"
            rapor += f"• Başarısız: {istatistik[2]}\n"
            rapor += f"• İlk Kullanım: {istatistik[3][:10] if istatistik[3] else 'Yok'}\n"
            rapor += f"• Son Kullanım: {istatistik[4][:10] if istatistik[4] else 'Yok'}\n\n"
            
            rapor += f"📅 **Son 7 Gün:**\n"
            for gun, toplam, basarili in gunluk_istatistik:
                rapor += f"• {gun}: {basarili}/{toplam} (%{(basarili/toplam*100) if toplam > 0 else 0:.1f})\n"
            
            return rapor
            
        except Exception as e:
            return f"❌ AI raporu oluşturulurken hata: {e}"

maliyet_analiz = MaliyetAnaliz()

# ----------------------------- TARİH FONKSİYONLARI -----------------------------
def parse_rapor_tarihi(metin):
    try:
        bugun = datetime.now(TZ).date()
        metin_lower = metin.lower()
        
        if 'bugün' in metin_lower or 'bugun' in metin_lower:
            return bugun
        if 'dün' in metin_lower or 'dun' in metin_lower:
            return bugun - timedelta(days=1)
        
        patterns = [
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{4})',
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{2})',
            r'(\d{4})[\.\/\-](\d{1,2})[\.\/\-](\d{1,2})'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, metin)
            for match in matches:
                if len(match) == 3:
                    if match[2].isdigit():
                        year = int(match[2])
                        if year < 100:
                            year += 2000
                        month = int(match[1])
                        day = int(match[0])
                        try:
                            return datetime(year, month, day).date()
                        except:
                            continue
                    elif match[0].isdigit() and len(match[0]) == 4:
                        year = int(match[0])
                        month = int(match[1])
                        day = int(match[2])
                        try:
                            return datetime(year, month, day).date()
                        except:
                            continue
        return None
    except:
        return None

def izin_mi(metin):
    """Basit izin kontrolü"""
    metin_lower = metin.lower()
    izin_kelimeler = ['izin', 'rapor yok', 'iş yok', 'çalışma yok', 'tatil', 'hasta', 'izindeyim']
    return any(kelime in metin_lower for kelime in izin_kelimeler)

async def tarih_kontrol_et(rapor_tarihi, user_id):
    bugun = datetime.now(TZ).date()
    
    if not rapor_tarihi:
        return False, "❌ **Tarih bulunamadı.** Lütfen raporunuzda tarih belirtiniz."
    
    if rapor_tarihi > bugun:
        return False, "❌ **Gelecek tarihli rapor.** Lütfen bugün veya geçmiş tarih kullanınız."
    
    iki_ay_once = bugun - timedelta(days=60)
    if rapor_tarihi < iki_ay_once:
        return False, "❌ **Çok eski tarihli rapor.** Lütfen son 2 ay içinde bir tarih kullanınız."
    
    result = await async_fetchone("SELECT COUNT(*) FROM reports WHERE user_id = %s AND report_date = %s", 
                  (user_id, rapor_tarihi))
    ayni_tarihli_rapor_sayisi = result[0]
    
    if ayni_tarihli_rapor_sayisi > 0:
        return False, "❌ **Bu tarih için zaten rapor gönderdiniz.**"
    
    return True, ""

def parse_tr_date(date_str):
    """Tüm tarih formatlarını destekle"""
    try:
        normalized_date = date_str.replace('/', '.').replace('-', '.')
        parts = normalized_date.split('.')
        if len(parts) == 3:
            if len(parts[2]) == 4:
                return datetime.strptime(normalized_date, '%d.%m.%Y').date()
            elif len(parts[0]) == 4:
                return datetime.strptime(normalized_date, '%Y.%m.%d').date()
        raise ValueError("Geçersiz tarih formatı")
    except:
        raise ValueError("Geçersiz tarih formatı")

def week_window_to_today():
    """Bugünden geriye doğru 7 günlük pencere"""
    end_date = datetime.now(TZ).date()
    start_date = end_date - timedelta(days=6)
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
                if analiz['calisan'] > 0: durum_detay.append(f"Çalışan:{analiz['calisan']}")
                if analiz['izinli'] > 0: durum_detay.append(f"İzinli:{analiz['izinli']}")
                if analiz['hastalik'] > 0: durum_detay.append(f"Hastalık:{analiz['hastalik']}")
                
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
            f"`/chatid` - Chat ID göster\n"
            f"`/import_rapor` - Manuel rapor import\n\n"
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
        "• Gerçek zamanlı takip\n"
        "• Manuel rapor import\n\n"
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
    
    target_date = datetime.now(TZ).date()
    await update.message.chat.send_action(action="typing")
    rapor_mesaji = await generate_gelismis_personel_ozeti(target_date)
    await update.message.reply_text(rapor_mesaji)

async def haftalik_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Haftalık rapor komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = datetime.now(TZ).date()
    start_date = today - timedelta(days=today.weekday())
    end_date = start_date + timedelta(days=6)
    
    mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def aylik_rapor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aylık rapor komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = datetime.now(TZ).date()
    start_date = today.replace(day=1)
    end_date = today
    
    mesaj = await generate_aylik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def haftalik_istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Haftalık istatistik komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = datetime.now(TZ).date()
    start_date = today - timedelta(days=today.weekday())
    end_date = start_date + timedelta(days=6)
    
    mesaj = await generate_haftalik_rapor_mesaji(start_date, end_date)
    await update.message.reply_text(mesaj, parse_mode='Markdown')

async def aylik_istatistik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aylık istatistik komutu"""
    if not await admin_kontrol(update, context):
        return
    
    await update.message.chat.send_action(action="typing")
    
    today = datetime.now(TZ).date()
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
    
    bugun = datetime.now(TZ).date()
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
                rapor_tarihi = tarih.strftime('%d.%m.%Y') if isinstance(tarih, datetime) else tarih
                gonderme_tarihi = delivered_date.strftime('%d.%m.%Y') if delivered_date and isinstance(delivered_date, datetime) else delivered_date
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
            ['🕒 Oluşturulma', datetime.now(TZ).strftime('%d.%m.%Y %H:%M')]
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

# ----------------------------- OPTİMİZE RAPOR İŞLEME -----------------------------
async def optimize_rapor_kontrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg:
        return

    user_id = msg.from_user.id
    
    if msg.document or msg.photo:
        return

    metin = msg.text or msg.caption
    if not metin:
        return

    if metin.startswith(('/', '.', '!', '\\')):
        return

    is_edited = bool(update.edited_message)
    delivered_dt = msg.date or datetime.utcnow()
    kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
    kullanici_projeleri = id_to_projects.get(user_id, [])

    ai_sonuc = ai_analiz.gelismis_analiz_et(metin, kullanici_adi, kullanici_projeleri)
    
    if ai_sonuc and 'rapor_tarihi' in ai_sonuc:
        try:
            tarih_str = ai_sonuc['rapor_tarihi']
            if re.match(r'\d{2}\.\d{2}\.\d{4}', tarih_str):
                rapor_tarihi = datetime.strptime(tarih_str, '%d.%m.%Y').date()
            else:
                rapor_tarihi = parse_rapor_tarihi(metin)
        except:
            rapor_tarihi = parse_rapor_tarihi(metin)
    else:
        rapor_tarihi = parse_rapor_tarihi(metin)

    if not rapor_tarihi:
        await msg.reply_text("❌ **Tarih bulunamadı.** Lütfen raporunuzda tarih belirtiniz.")
        return

    tarih_gecerli, hata_mesaji = await tarih_kontrol_et(rapor_tarihi, user_id)
    if not tarih_gecerli:
        await msg.reply_text(hata_mesaji)
        return

    rapor_tipi = ai_sonuc.get('rapor_tipi', 'IZIN/ISYOK' if izin_mi(metin) else 'RAPOR')
    
    await rapor_kaydet_async(user_id, rapor_tipi, metin, rapor_tarihi, delivered_dt, is_edited, ai_sonuc)
    
    kaynak = ai_sonuc.get('kaynak', 'unknown')
    emoji = "🤖" if kaynak == 'gpt' else "⚠️"
    
    await msg.reply_text(
        f"{emoji} **Rapor Kaydedildi** - {kullanici_adi}\n"
        f"**Tarih:** {rapor_tarihi.strftime('%d.%m.%Y')}\n"
        f"**Tip:** {rapor_tipi}\n"
        f"**Proje:** {ai_sonuc.get('proje_adi', 'Belirsiz')}\n"
        f"**Kişi:** {ai_sonuc.get('kisi_sayisi', 'Belirsiz')}"
    )

async def rapor_kaydet_async(user_id: int, rapor_type: str, content_summary: str,
                 rapor_tarihi, delivered_dt: datetime, is_edited: bool, ai_analiz_data=None):
    """Async rapor kaydetme"""
    delivered_date = delivered_dt.astimezone(TZ).date() if delivered_dt else datetime.now(TZ).date()
    
    project_name = ai_analiz_data.get('proje_adi', 'BELİRSİZ') if ai_analiz_data else 'BELİRSİZ'
    person_count = ai_analiz_data.get('kisi_sayisi', 1) if ai_analiz_data else 1
    work_description = ai_analiz_data.get('yapilan_is', content_summary) if ai_analiz_data else content_summary
    work_category = ai_analiz_data.get('is_kategorisi', 'diğer') if ai_analiz_data else 'diğer'
    personnel_type = ai_analiz_data.get('personel_tipi', 'imalat') if ai_analiz_data else 'imalat'
    
    await async_execute("""
        INSERT INTO reports 
        (user_id, project_name, report_date, report_type, person_count, work_description, 
         work_category, personnel_type, delivered_date, is_edited, ai_analysis)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        user_id, project_name, rapor_tarihi, rapor_type, person_count, 
        work_description[:500], work_category, personnel_type, delivered_date,
        1 if is_edited else 0,
        json.dumps(ai_analiz_data, ensure_ascii=False) if ai_analiz_data else None
    ))
    
    if ai_analiz_data and 'kaynak' in ai_analiz_data:
        maliyet_analiz.kayit_ekle(ai_analiz_data['kaynak'])

# ----------------------------- ZAMANLAMA -----------------------------
def schedule_jobs(app):
    jq = app.job_queue
    
    jq.run_repeating(auto_watch_excel, interval=60, first=10)
    jq.run_daily(gunluk_rapor_ozeti, time=time(9, 0, tzinfo=TZ))
    
    jq.run_daily(hatirlatma_mesaji, time=time(12, 30, tzinfo=TZ))
    jq.run_daily(ilk_rapor_kontrol, time=time(15, 0, tzinfo=TZ))
    jq.run_daily(son_rapor_kontrol, time=time(17, 30, tzinfo=TZ))
    
    jq.run_daily(yandex_yedekleme_gorevi, time=time(23, 0, tzinfo=TZ))
    
    jq.run_daily(haftalik_grup_raporu, time=time(17, 40, tzinfo=TZ), days=(4,))
    
    jq.run_monthly(aylik_grup_raporu, when=time(17, 45, tzinfo=TZ), day=28)
    
    logging.info("⏰ Tüm zamanlamalar ayarlandı")

async def auto_watch_excel(context: ContextTypes.DEFAULT_TYPE):
    global last_excel_update
    if os.path.exists(USERS_FILE):
        current_mtime = os.path.getmtime(USERS_FILE)
        if current_mtime > last_excel_update:
            load_excel()
            logging.info("Excel dosyası otomatik yenilendi")

async def gunluk_rapor_ozeti(context: ContextTypes.DEFAULT_TYPE):
    """🕘 09:00 - Sadece Eren ve Atamurat'a DM gönder"""
    try:
        dun = (datetime.now(TZ) - timedelta(days=1)).date()
        rapor_mesaji = await generate_gelismis_personel_ozeti(dun)
        
        hedef_kullanicilar = [709746899, 1000157326]
        
        for user_id in hedef_kullanicilar:
            try:
                await context.bot.send_message(chat_id=user_id, text=rapor_mesaji)
                logging.info(f"🕘 09:00 özeti {user_id} kullanıcısına gönderildi")
            except Exception as e:
                logging.error(f"🕘 {user_id} kullanıcısına özet gönderilemedi: {e}")
                
    except Exception as e:
        logging.error(f"🕘 09:00 rapor hatası: {e}")
        await hata_bildirimi(context, f"09:00 rapor hatası: {e}")

async def hatirlatma_mesaji(context: ContextTypes.DEFAULT_TYPE):
    """🟡 12:30 - Gün ortası şantiye bazlı hatırlatma mesajı"""
    try:
        bugun = datetime.now(TZ).date()
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
            except Exception as e:
                logging.error(f"🟡 {user_id} kullanıcısına şantiye hatırlatma gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🟡 Şantiye hatırlatma mesajı hatası: {e}")
        await hata_bildirimi(context, f"Şantiye hatırlatma mesajı hatası: {e}")

async def ilk_rapor_kontrol(context: ContextTypes.DEFAULT_TYPE):
    """🟠 15:00 - İlk rapor kontrolü (şantiye bazlı)"""
    try:
        bugun = datetime.now(TZ).date()
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
            except Exception as e:
                logging.error(f"🟠 {user_id} kullanıcısına şantiye kontrol mesajı gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🟠 Şantiye rapor kontrol hatası: {e}")
        await hata_bildirimi(context, f"Şantiye rapor kontrol hatası: {e}")

async def son_rapor_kontrol(context: ContextTypes.DEFAULT_TYPE):
    """🔴 17:30 - Gün sonu şantiye bazlı rapor analizi"""
    try:
        bugun = datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        result = await async_fetchone("SELECT COUNT(*) FROM reports WHERE report_date = %s", (bugun,))
        toplam_rapor = result[0]
        
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
            except Exception as e:
                logging.error(f"🔴 {admin_id} adminine şantiye gün sonu özeti gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🔴 Şantiye son rapor kontrol hatası: {e}")
        await hata_bildirimi(context, f"Şantiye son rapor kontrol hatası: {e}")

async def haftalik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(TZ).date()
        start_date = today - timedelta(days=today.weekday() + 7)
        end_date = start_date + timedelta(days=6)
        
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
            except Exception as e:
                logging.error(f"📊 {admin_id} adminine haftalık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"📊 Haftalık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Haftalık grup raporu hatası: {e}")

async def aylik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.now(TZ).date()
        start_date = today.replace(day=1) - timedelta(days=1)
        start_date = start_date.replace(day=1)
        end_date = today.replace(day=1) - timedelta(days=1)
        
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
            except Exception as e:
                logging.error(f"🗓️ {admin_id} adminine aylık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🗓️ Aylık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Aylık grup raporu hatası: {e}")

async def bot_baslatici_mesaji(context: ContextTypes.DEFAULT_TYPE):
    try:
        mesaj = "🤖 **Rapor Kontrol Botu Aktif!**\n\nKontrol bende ⚡️\nKolay gelsin 👷‍♂️"
        
        for admin_id in ADMINS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=mesaj)
                logging.info(f"Başlangıç mesajı {admin_id} adminine gönderildi")
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
        BotCommand("import_rapor", "Manuel rapor import (Super Admin)"),
    ]
    await application.bot.set_my_commands(commands)
    
    await bot_baslatici_mesaji(application)

# ----------------------------- MAIN -----------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Temel komutlar
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("hakkinda", hakkinda_cmd))
    
    # Admin komutları
    app.add_handler(CommandHandler("bugun", bugun_cmd))
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
    app.add_handler(CommandHandler("import_rapor", import_rapor_cmd))
    
    # Manuel import handler'ları
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Rapor işleme
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, optimize_rapor_kontrol))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, optimize_rapor_kontrol))
    
    schedule_jobs(app)
    logging.info("🚀 GÜNCELLENMİŞ Rapor Botu başlatılıyor...")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
```