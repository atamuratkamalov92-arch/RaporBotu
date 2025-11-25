"""
📋 CHANGELOG - bot.py v4.6.8

✅ GÜNCELLEMELER:
- "Yerel Ekipbaşı" kategorisi staff olarak tanınacak şekilde SYSTEM_PROMPT güncellendi
- BWC raporlarındaki "Toplam Yerel Ekipbaşı" değeri artık staff kategorisine eklenecek
- Diğer tüm fonksiyonlar korundu
"""

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

# Çevre değişkenlerini en başta yükle
load_dotenv()

# Loglama ayarı - Railway için konsol çıktısı (EN ÜSTE)
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)

# Railway için PORT ayarı
PORT = int(os.environ.get('PORT', 8443))
logging.info(f"🚀 Railway PORT: {PORT}")

try:
    from telegram import Update, BotCommand, BotCommandScopeAllPrivateChats
    HAS_PRIVATE_SCOPE = True
except Exception as e:
    HAS_PRIVATE_SCOPE = False
    logging.warning(f"BotCommandScopeAllPrivateChats yüklenemedi: {e}")

from telegram.ext import (
    Application, MessageHandler, CommandHandler, ContextTypes, filters
)
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from psycopg2 import pool
from bs4 import BeautifulSoup
from openai import OpenAI

# Çevre değişkeni doğrulama
def validate_environment():
    """Gerekli tüm çevre değişkenlerini doğrula"""
    required_vars = {
        'BOT_TOKEN': 'Telegram Bot Token',
        'DATABASE_URL': 'PostgreSQL Veritabanı URL',
        'OPENAI_API_KEY': 'OpenAI API Anahtarı'
    }
    
    missing_vars = []
    for var, description in required_vars.items():
        if not os.getenv(var):
            missing_vars.append(f"{var} ({description})")
    
    if missing_vars:
        error_msg = f"❌ Eksik çevre değişkenleri: {', '.join(missing_vars)}"
        logging.error(error_msg)
        raise RuntimeError(error_msg)
    
    logging.info("✅ Tüm gerekli çevre değişkenleri ayarlanmış")

# İçe aktarımda çevre değişkenlerini doğrula
validate_environment()

# Veritabanı bağlantı havuzu
DB_POOL = None

def init_db_pool():
    """Hata yönetimi ile veritabanı bağlantı havuzunu başlat"""
    global DB_POOL
    try:
        if DB_POOL is None:
            DB_POOL = pool.ThreadedConnectionPool(
                minconn=1, 
                maxconn=10, 
                dsn=os.environ['DATABASE_URL'], 
                sslmode='require'
            )
            logging.info("✅ Veritabanı bağlantı havuzu başlatıldı")
    except Exception as e:
        logging.error(f"❌ Veritabanı havuzu başlatma hatası: {e}")
        raise

def get_conn_from_pool():
    """Doğrulama ile havuzdan bağlantı al"""
    if DB_POOL is None:
        init_db_pool()
    
    try:
        conn = DB_POOL.getconn()
        if conn.closed:
            logging.warning("⚠️ Bağlantı kapalıydı, yeni oluşturuluyor")
            DB_POOL.putconn(conn)
            conn = DB_POOL.getconn()
        return conn
    except Exception as e:
        logging.error(f"❌ Havuzdan bağlantı alma hatası: {e}")
        raise

def put_conn_back(conn):
    """Bağlantıyı havuza güvenli şekilde geri ver"""
    try:
        if DB_POOL and conn and not conn.closed:
            DB_POOL.putconn(conn)
    except Exception as e:
        logging.error(f"❌ Bağlantıyı havuz iade etme hatası: {e}")

# Güvenli veritabanı yardımcı fonksiyonları
def _sync_fetchall_safe(query, params=()):
    """Güvenli sorgu çalıştır ve tuple index koruması ile tüm sonuçları döndür"""
    conn = get_conn_from_pool()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return rows if rows else []
    except Exception as e:
        logging.error(f"Veritabanı fetchall hatası: {e}")
        return []
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

def _sync_execute_safe(query, params=()):
    """Güvenli sorgu çalıştır ve satır sayısını döndür"""
    conn = get_conn_from_pool()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        return cur.rowcount
    except Exception as e:
        conn.rollback()
        logging.error(f"Veritabanı execute hatası: {e}")
        return 0
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

def _sync_fetchone_safe(query, params=()):
    """Güvenli sorgu çalıştır ve tuple index koruması ile tek sonuç döndür"""
    conn = get_conn_from_pool()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return row if row else None
    except Exception as e:
        logging.error(f"Veritabanı fetchone hatası: {e}")
        return None
    finally:
        if cur:
            cur.close()
        put_conn_back(conn)

# Async veritabanı operasyonları
async def async_db_query(func, *args, **kwargs):
    """Executor içinde veritabanı sorgusu çalıştır"""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
    except Exception as e:
        logging.error(f"Async DB sorgu hatası: {e}")
        raise

async def async_fetchall(query, params=()):
    """Güvenli tuple işleme ile async fetchall"""
    try:
        result = await async_db_query(_sync_fetchall_safe, query, params)
        return result if result else []
    except Exception as e:
        logging.error(f"Async fetchall hatası - Sorgu: {query}, Parametreler: {params}, Hata: {e}")
        return []

async def async_execute(query, params=()):
    """Güvenli işleme ile async execute"""
    return await async_db_query(_sync_execute_safe, query, params)

async def async_fetchone(query, params=()):
    """Güvenli tuple işleme ile async fetchone"""
    try:
        result = await async_db_query(_sync_fetchone_safe, query, params)
        return result
    except Exception as e:
        logging.error(f"Async fetchone hatası - Sorgu: {query}, Parametreler: {params}, Hata: {e}")
        return None

def safe_get_tuple_value(tuple_data, index, default=None):
    """Index sınır kontrolü ile tuple'dan güvenli değer alma"""
    if tuple_data is None:
        return default
    
    if isinstance(tuple_data, (tuple, list)) and len(tuple_data) > index:
        value = tuple_data[index]
        return value if value is not None else default
    
    return default

# Gelişmiş JSON parsing ile doğrulama
def safe_json_loads(json_string, default=None):
    """Kapsamlı hata yönetimi ile güvenli JSON string parsing"""
    if json_string is None:
        return default
    
    # Eğer zaten dict ise, doğrudan döndür
    if isinstance(json_string, dict):
        return json_string
    
    try:
        return json.loads(json_string)
    except json.JSONDecodeError as e:
        logging.error(f"JSON decode hatası: {e}, Girdi: {json_string[:100]}...")
        return default
    except Exception as e:
        logging.error(f"Beklenmeyen JSON parsing hatası: {e}")
        return default

# GELİŞMİŞ EXCEL OKUMA - YENİ FORMAT DESTEĞİ
def safe_read_excel(file_path, required_columns=None):
    """
    GELİŞTİRİLDİ: Yeni Excel formatını destekler
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Excel dosyası bulunamadı: {file_path}")
    
    try:
        df = pd.read_excel(file_path)
        
        # Gerekli kolonları doğrula (esnek)
        if required_columns:
            missing_columns = [col for col in required_columns if col not in df.columns]
            if missing_columns:
                logging.warning(f"⚠️ Eksik kolonlar: {missing_columns}. Mevcut kolonlar: {list(df.columns)}")
                
                # YENİ FORMAT EŞLEŞTİRME - GÜNCELLENDİ
                column_mapping = {
                    'Rol': 'Botdaki Statusu / Rol',
                    'Botdaki Statusu': 'Botdaki Statusu / Rol',
                    'Kullanici Adi Soyadi': 'Kullanici Adi Soyadi',
                    'Telegram ID': 'Telegram ID', 
                    'Proje / Şantiye': 'Proje / Şantiye',
                    'Aktif / Pasif': 'Aktif / Pasif',
                    # YENİ EŞLEŞMELER
                    'Username': 'Username',
                    'Telefon Numarası': 'Telefon Numarası',
                    'Pozisyon Kodu': 'Pozisyon Kodu',
                    'Özel Rapor': 'Özel Rapor'
                }
                
                for required_col in missing_columns:
                    if required_col in column_mapping and column_mapping[required_col] in df.columns:
                        # Mevcut kolonu kullan
                        df[required_col] = df[column_mapping[required_col]]
                        logging.info(f"✅ {required_col} için {column_mapping[required_col]} kolonu kullanıldı")
                    else:
                        # Varsayılan değerlerle ekle
                        if required_col == "Rol":
                            df[required_col] = "KULLANICI"
                        elif required_col == "Botdaki Statusu":
                            df[required_col] = "Aktif"
                        elif required_col == "Aktif / Pasif":
                            df[required_col] = "E"
                        else:
                            df[required_col] = ""
        
        return df
    except Exception as e:
        logging.error(f"Excel okuma hatası: {e}")
        raise

# Timeout ile gelişmiş HTTP istekleri
def safe_http_request(url, method='GET', timeout=30, **kwargs):
    """Timeout ve hata yönetimi ile HTTP isteği yap"""
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response
    except requests.exceptions.Timeout:
        logging.error(f"HTTP istek timeout: {url}")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"HTTP istek hatası: {e}")
        return None

# Helper function for integer conversion - YENİ TELEGRAM ID PARSING
def _to_int_or_none(x):
    """Güvenli şekilde integer'a çevir veya None döndür - YENİ: 8-10 digit Telegram ID"""
    if x is None or pd.isna(x):
        return None
    
    s = str(x).strip()
    if not s:
        return None
    
    # Bilimsel gösterim kontrolü
    if "e+" in s.lower():
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None
    
    # Sadece rakamları al
    s_clean = re.sub(r'[^\d]', '', s)
    
    if not s_clean:
        return None
    
    # YENİ: 8-10 digit Telegram ID kontrolü
    if len(s_clean) < 8 or len(s_clean) > 10:
        # Özel durum: 10 digit ID'ler kabul edilir
        if len(s_clean) == 10:
            pass
        else:
            return None
    
    try:
        return int(s_clean)
    except (ValueError, TypeError):
        return None

def get_file_hash(filename):
    """Değişiklik tespiti için dosya hash'ini al"""
    try:
        if os.path.exists(filename):
            with open(filename, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        return None
    except Exception as e:
        logging.error(f"Dosya hash hatası: {e}")
        return None

# Konfigürasyon
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
try:
    GROUP_ID = int(CHAT_ID) if CHAT_ID else None
    logging.info(f"✅ GROUP_ID başarıyla ayarlandı: {GROUP_ID}")
except (ValueError, TypeError) as e:
    GROUP_ID = None
    logging.error(f"❌ GROUP_ID ayarlanamadı: {e}")
TZ = ZoneInfo("Asia/Tashkent")

SUPER_ADMIN_ID = 1000157326

# Fallback kullanıcı veri yapısı
FALLBACK_USERS = [
    {
        "Telegram ID": 1000157326,
        "Kullanici Adi Soyadi": "Atamurat Kamalov", 
        "Aktif / Pasif": "E",
        "Rol": "SÜPER ADMIN",
        "Botdaki Statusu": "Aktif",
        "Proje / Şantiye": "TYM"
    },
    {
        "Telegram ID": 709746899,
        "Kullanici Adi Soyadi": "Eren Boz",
        "Aktif / Pasif": "E", 
        "Rol": "ADMIN",
        "Botdaki Statusu": "Aktif",
        "Proje / Şantiye": "TYM"
    }
]

USERS_FILE = "Kullanicilar.xlsx"

# Global değişkenler başlatma
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

# Sabit şantiye listesi - TÜM raporlarda kullanılacak (FAP kaldırıldı)
SABIT_SANTIYELER = ['BWC', 'DMC', 'STADYUM', 'KÖKSARAY', 'LOT13', 'LOT71', 'OHP', 'SKP', 'YHP', 'TYM', 'MMP', 'RMC', 'PİRAMİT']

# Şantiye bazlı kullanıcı adı (username) eşlemesi - HATIRLATMA MESAJLARI İÇİN
SANTIYE_USERNAME_MAPPING = {
    'BWC': ['YsF1434'],
    'SKP': ['uzyusufmutlu'],
    'DMC': ['uzyusufmutlu'],
    'KÖKSARAY': ['Erdoğan.Karamısır'],
    'STADYUM': ['AdnanKeleş'],
    'LOT13': ['AdnanKeleş'],
    'LOT71': ['AdnanKeleş'],
    'OHP': ['Erdoğan.Karamısır'],
    'YHP': ['Orhan_Ceylan'],
    'MMP': ['Orhan_Ceylan'],
    'RMC': ['Orhan_Ceylan'],
    'TYM': ['Orhan_Ceylan'],
    'PİRAMİT': ['ON5428']
}

# Giriş doğrulama fonksiyonları
def validate_user_input(text, max_length=1000):
    """Kullanıcı giriş metnini doğrula"""
    if not text or not isinstance(text, str):
        return False, "Giriş boş olmayan string olmalı"
    
    if len(text) > max_length:
        return False, f"Giriş çok uzun (maksimum {max_length} karakter)"
    
    # Temizleme
    text = html.escape(text.strip())
    
    return True, text

def validate_date_string(date_str):
    """Tarih string formatını doğrula"""
    try:
        dt.datetime.strptime(date_str, '%Y-%m-%d')
        return True
    except ValueError:
        return False

def normalize_site_name(site_name):
    """Şantiye isimlerini standartlaştır"""
    if not site_name:
        return "BELİRSİZ"
        
    site_name = site_name.upper().strip()
    
    mappings = {
        'LOT 13': 'LOT13',
        'LOT-13': 'LOT13', 
        'LOT13': 'LOT13',
        'LOT 71': 'LOT71',
        'LOT-71': 'LOT71',
        'LOT71': 'LOT71',
        'SKP DAHO': 'SKP',
        'SKP': 'SKP',
        'PİRAMİT TOWER': 'PİRAMİT',
        'PİRAMİT': 'PİRAMİT',
        'PRAMİT': 'PİRAMİT',
        'PRAMIT': 'PİRAMİT',
        'PİRAMİT TOWEr': 'PİRAMİT',
        'PİRAMİT TOWAR': 'PİRAMİT',
        'PIRAMIT': 'PİRAMİT',
        'PIRAMIT TOWER': 'PİRAMİT',
        'PİRAMİD': 'PİRAMİT',
        'PIRAMID': 'PİRAMİT',
        'PYRAMIT': 'PİRAMİT',
        'PYRAMID': 'PİRAMİT',
        'BWC': 'BWC',
        'STADYUM': 'STADYUM',
        'DMC ELLIPSE GARDEN': 'DMC',
        'DMC ELLIPSE': 'DMC',
        'DMC GARDEN': 'DMC',
        'DMC Ellipse Garden Elektrik Grubu': 'DMC',
        'DMC ELLIPSE GARDEN ELEKTRIK GRUBU': 'DMC',
        'DMC ELLIPSE GARDEN ELEKTRIK GRUBU': 'DMC',
        'DMC ELLIPSE ELEKTRIK GRUBU': 'DMC',
        'DMC GARDEN ELEKTRIK GRUBU': 'DMC',
        'DMC ELEKTRIK GRUBU': 'DMC',
        'DMC ELEKTRIK': 'DMC',
        'DMC ELLIPSE GARDEN ELEKTRİK': 'DMC',
        'DMC ELLIPSE ELEKTRİK': 'DMC',
        'DMC GARDEN ELEKTRİK': 'DMC',
        'DMC': 'DMC',
        'KÖKSARAY': 'KÖKSARAY',
        'OHP': 'OHP',
        'TYM': 'TYM',
        'YHP': 'YHP',
        'MMP': 'MMP',
        'RMC': 'RMC'
    }
    
    return mappings.get(site_name, site_name)

# YENİ ŞANTİYE PARSING FONKSİYONU - "TÜMÜ" FİLTRELENDİ
def parse_santiye_list(proje_string):
    """
    YENİ ŞANTİYE PARSING KURALLARI:
    - 'SKP (DAHO) / DMC' → ['SKP', 'DMC']
    - '/' , ',' , '-' , '|' ile ayır
    - Parantez içlerini temizle
    - 'Tümü' → tüm şantiyeler (özel işlem)
    - 'Belli değil' → atla
    - Şantiye isimlerini normalize et
    """
    if not proje_string or pd.isna(proje_string):
        return []
    
    proje_string = str(proje_string).strip()
    
    # Özel durumlar
    if proje_string.upper() == 'TÜMÜ':
        return ['TÜMÜ']
    if proje_string.upper() in ['BELLİ DEĞİL', 'BELİRSİZ', '']:
        return []
    
    # Parantez içlerini temizle: 'SKP (DAHO)' → 'SKP'
    proje_string = re.sub(r'\([^)]*\)', '', proje_string)
    
    # Birden fazla ayırıcı ile böl
    parts = re.split(r'[/,\-\|]', proje_string)
    
    # Temizle, filtrele ve normalize et
    santiyeler = []
    for part in parts:
        part_clean = part.strip()
        if part_clean and part_clean.upper() not in ['BELLİ DEĞİL', 'BELİRSİZ']:
            # Şantiye ismini normalize et
            normalized_site = normalize_site_name(part_clean)
            santiyeler.append(normalized_site)
    
    return santiyeler

# Doğrulama ile gelişmiş Excel yükleme - "TÜMÜ" FİLTRELENDİ
def load_excel_intelligent():
    """Kapsamlı doğrulama ile akıllı Excel dosyası yükleme"""
    global df, rapor_sorumlulari, id_to_name, id_to_projects, id_to_status, id_to_rol
    global ADMINS, IZLEYICILER, TUM_KULLANICILAR, santiye_sorumlulari, santiye_rapor_durumu
    global last_excel_update, excel_file_hash, excel_last_modified
    
    try:
        # Önbellek için dosya hash ve değişiklik zamanını kontrol et
        current_hash = get_file_hash(USERS_FILE)
        current_mtime = os.path.getmtime(USERS_FILE) if os.path.exists(USERS_FILE) else 0
        
        if (current_hash == excel_file_hash and 
            current_mtime == excel_last_modified and 
            df is not None):
            logging.info("✅ Excel önbellekte - Yeniden yüklemeye gerek yok")
            return
        
        # Doğrulama için gerekli kolonları tanımla
        required_columns = ["Telegram ID", "Kullanici Adi Soyadi", "Rol", "Botdaki Statusu", "Proje / Şantiye"]
        
        try:
            # Esnek Excel okuma
            df = safe_read_excel(USERS_FILE, required_columns)
            logging.info("✅ Excel dosyası başarıyla yüklendi")
            
            excel_file_hash = current_hash
            excel_last_modified = current_mtime
            
        except (FileNotFoundError, ValueError) as e:
            logging.error(f"❌ Excel okuma hatası: {e}. Fallback kullanıcı listesi kullanılıyor.")
            df = pd.DataFrame(FALLBACK_USERS)
    
    except Exception as e:
        logging.error(f"❌ Excel yükleme hatası: {e}. Fallback kullanıcı listesi kullanılıyor.")
        df = pd.DataFrame(FALLBACK_USERS)
    
    # ŞANTİYE BAZLI SİSTEM: Güvenli tuple işleme ile Excel verilerini işle
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
        # Telegram ID parsing
        tid = _to_int_or_none(r.get("Telegram ID"))
        fullname = str(r.get("Kullanici Adi Soyadi") or "").strip()
        
        aktif_pasif = str(r.get("Aktif / Pasif") or "E").strip().upper()
        status = str(r.get("Botdaki Statusu") or "Aktif").strip()
        rol = str(r.get("Rol") or "KULLANICI").strip().upper()

        # Sadece aktif kullanıcıları işle
        if not fullname or aktif_pasif != "E":
            continue

        if tid and fullname:
            # Bilinen ID düzeltmelerini işle
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
            
            # ŞANTİYE PARSING - "TÜMÜ" FİLTRELENDİ
            raw_projects = str(r.get("Proje / Şantiye") or "")
            projects = parse_santiye_list(raw_projects)
            
            # "TÜMÜ" şantiyesini filtrele - şantiye listesinde görünmesin
            projects = [proje for proje in projects if proje != "TÜMÜ"]
            temp_id_to_projects[tid] = projects
            
            # Şantiye sorumlularını güncelle - "TÜMÜ" hariç
            for proje in projects:
                if proje and proje != "TÜMÜ":  # "TÜMÜ" şantiyesini ekleme
                    if proje not in temp_santiye_sorumlulari:
                        temp_santiye_sorumlulari[proje] = []
                    if tid not in temp_santiye_sorumlulari[proje]:
                        temp_santiye_sorumlulari[proje].append(tid)
            
            # Tüm aktif kullanıcılar rapor sorumlusu listesene eklenir
            if tid and fullname:
                temp_rapor_sorumlulari.append(tid)
                processed_names.add(fullname)

    # Global değişkenleri güncelle
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
    
    # Super admin'in admin listesinde olduğundan emin ol
    if SUPER_ADMIN_ID not in ADMINS:
        ADMINS.append(SUPER_ADMIN_ID)
    
    last_excel_update = os.path.getmtime(USERS_FILE) if os.path.exists(USERS_FILE) else 0
    
    # "TÜMÜ" şantiyesi olup olmadığını kontrol et
    tumu_sayisi = sum(1 for projects in temp_id_to_projects.values() if "TÜMÜ" in projects)
    logging.info(f"✅ SİSTEM YÜKLENDİ: {len(rapor_sorumlulari)} aktif kullanıcı, {len(ADMINS)} admin, {len(IZLEYICILER)} izleyici, {len(TUM_KULLANICILAR)} toplam kullanıcı, {len(santiye_sorumlulari)} şantiye, {tumu_sayisi} kullanıcıda 'TÜMÜ' şantiyesi (filtrelendi)")

# Excel yüklemeyi başlat
load_excel_intelligent()

# Google Cloud Storage fonksiyonları
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

# YENİ SİSTEM_PROMPT - ÇİFT SAYMA DÜZELTMESİ VE DMC NORMALİZASYONU - YEREL EKİPBAŞI EKLENDİ
SYSTEM_PROMPT = """
Sen bir "Rapor Analiz Asistanısın". Görevin, kullanıcıların Telegram üzerinden gönderdiği serbest formatlı günlük personel raporlarını SABİT BİR JSON formatına dönüştürmektir.

ÖNEMLİ KURALLAR:

1. **SABİT JSON FORMATI**: Her zaman aşağıdaki sabit JSON formatını kullan:

[
  {
    "date": "YYYY-AA-GG",
    "site": "ŞANTİYE_ADI",
    "staff": sayı,
    "calisan": sayı,
    "mobilizasyon": sayı,
    "ambarci": sayı,
    "izinli": sayı,
    "dis_gorev": [
      {"gorev_yeri": "YER_ADI", "sayi": sayı}
    ],
    "dis_gorev_toplam": sayı,
    "genel_toplam": sayı
  }
]

2. **ÖNCELİK KURALI - ÇOK ÖNEMLİ**:
   - ÖNCE mesajda "Genel toplam", "Toplam", "Özet" gibi bölüm ara
   - Eğer özet bölümü varsa (ÖRNEK: "Genel toplam: 25 kişi"):
     → SADECE özet bölümündeki sayıları kullan
     → Detaylı maddeleri TAMAMEN YOK SAY ve parse etme!
   - Özet yoksa veya eksikse, o zaman detaylı maddelerden say

3. **ÇİFT SAYMA KORUMASI**:
   - Asla aynı mesajdan hem özet hem detay sayma!
   - Özet bulduğunda detayları GÖRMEZDEN GEL!
   - ÖRNEK: Mesajda hem detaylı işler hem de "Genel toplam: 25 kişi" varsa, SADECE 25 kullan!

4. **TARİH ALGILAMA**:
   - Format: YYYY-AA-GG
   - Örnek: "13.11.2025" → "2025-11-13"
   - Tarih yoksa bugünün tarihini kullan

5. **ŞANTİYE NORMALİZASYONU**:
   - LOT13, LOT71, SKP, BWC, Piramit, STADYUM, DMC, YHP, TYM, MMP, RMC, PİRAMİT
   - "Lot 13", "lot13", "LOT-13" → "LOT13"
   - "SKP Daho" → "SKP"
   - "Piramit Tower", "PİRAMİT TOWER", "PRAMİT", "PIRAMIT", "PİRAMİD", "PIRAMID", "PYRAMIT", "PYRAMID", "PİRAMİT", "PIRAMIT TOWER" → "PİRAMİT"   # YENİ EKLENDİ
   - "DMC Ellipse Garden", "DMC ELLIPSE GARDEN", "DMC Ellipse", "DMC Garden", "DMC Ellipse Garden Elektrik Grubu", "DMC ELEKTRIK GRUBU" → "DMC"
   - "YHP" → "YHP"
   - "TYM" → "TYM"
   - "MMP" → "MMP"
   - "RMC" → "RMC"

6. **PERSONEL KATEGORİLERİ**:
   - **staff**: mühendis, tekniker, formen, ekipbaşı, şef, Türk mühendis, Türk formen, Yerel formen, Yerel Ekipbaşı, Yerel ekipbaşı
   - **calisan**: usta, işçi, yardımcı, operatör, imalat, çalışan, worker
   - **ambarci**: ambarcı, depo sorumlusu, malzemeci, ambar
   - **mobilizasyon**: genel mobilizasyon, saha kontrol, nöbetçi, mobilizasyon takibi
   - **izinli**: izinli, iş yok, gelmedi, izindeyim, hasta, raporlu, hastalık izni, sıhhat izni
   - **dis_gorev**: başka şantiye görev, dış görev, Lot 71 dış görev

7. **HESAPLAMALAR**:
   genel_toplam = staff + calisan + mobilizasyon + ambarci + izinli + dis_gorev_toplam
   dis_gorev_toplam = tüm dış görevlerin toplamı

8. **DİKKAT EDİLECEK NOKTALAR**:
   - "Çalışan: 10" → calisan: 10
   - "İzinli: 1" → izinli: 1
   - "Ambarcı: 2" → ambarci: 2
   - "Toplam staff: 1" → staff: 1
   - "Toplam mobilizasyon: 2" → mobilizasyon: 2
   - "Toplam Yerel Ekipbaşı: 4" → staff: 4 (Yerel Ekipbaşı staff kategorisine eklenir)
   - "Lot 71 dış görev 8" → dis_gorev: [{"gorev_yeri": "LOT71", "sayi": 8}], dis_gorev_toplam: 8
   - "Beldersoy: 17 kişi" → calisan: 17
   - "Genel toplam: 10 kişi" → genel_toplam: 10 (doğrulama için kullan)

9. **ÖRNEK ÇIKTI FORMATI**:
[
  {
    "date": "2025-11-13",
    "site": "LOT13",
    "staff": 1,
    "calisan": 5,
    "mobilizasyon": 2,
    "ambarci": 1,
    "izinli": 1,
    "dis_gorev": [
      {"gorev_yeri": "LOT71", "sayi": 3},
      {"gorev_yeri": "STADYUM", "sayi": 2}
    ],
    "dis_gorev_toplam": 5,
    "genel_toplam": 15
  }
]

DİKKAT: 
- Sadece JSON döndür, açıklama yapma!
- Tüm sayıları integer olarak döndür
- Eksik alanları 0 olarak doldür
- dis_gorev her zaman bir liste olmalı, boşsa []
- Her zaman bu sabit JSON formatını kullan!
- ÖZET BÖLÜMÜ VARSA DETAYLARI YOK SAY!
"""

# Gelişmiş tarih parser fonksiyonları
def enhanced_date_parser(text):
    """Gelişmiş tarih parser - geçici implementasyon"""
    try:
        # Basit tarih parsing implementasyonu
        patterns = [
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{4})',
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{2})',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                try:
                    if len(match) == 3:
                        day, month, year = int(match[0]), int(match[1]), int(match[2])
                        if len(str(year)) == 2:
                            year += 2000
                        return dt.datetime(year, month, day).date()
                except ValueError:
                    continue
        return None
    except Exception:
        return None

def get_santiye_sorumlusu(santiye_adi):
    """
    Şantiye adına göre sorumlu kişiyi bul
    """
    try:
        santiye_adi = normalize_site_name(santiye_adi)
        
        # Özel durumlar
        if santiye_adi == "BELİRSİZ":
            return None
            
        # Şantiye sorumluları listesinde ara
        if santiye_adi in santiye_sorumlulari:
            sorumlular = santiye_sorumlulari[santiye_adi]
            if sorumlular:
                # Aktif ilk sorumluyu döndür
                for sorumlu_id in sorumlular:
                    if sorumlu_id in rapor_sorumlulari:
                        return sorumlu_id
                # Eğer hiçbiri aktif değilse ilkini döndür
                return sorumlular[0]
        
        logging.warning(f"⚠️ Şantiye sorumlusu bulunamadı: {santiye_adi}")
        return None
        
    except Exception as e:
        logging.error(f"❌ Şantiye sorumlusu bulma hatası: {e}")
        return None


def extract_max_number(text, patterns):
    """Pattern'lere göre maksimum sayıyı çıkar"""
    max_num = 0
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            try:
                num = int(match)
                max_num = max(max_num, num)
            except ValueError:
                continue
    return max_num

# Basitleştirilmiş USER_PROMPT_TEMPLATE
USER_PROMPT_TEMPLATE = "<<<RAW_MESSAGE>>>"

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

# Gelişmiş GPT analizi ile giriş doğrulama
def gpt_analyze_enhanced(system_prompt, user_prompt):
    """Gelişmiş hata yönetimi ile GPT ile metin analizi"""
    is_valid, cleaned_prompt = validate_user_input(user_prompt, 4000)
    if not is_valid:
        logging.error("GPT'ye geçersiz kullanıcı girişi sağlandı")
        return ""
    
    try:
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=30.0)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": cleaned_prompt}
            ],
            temperature=0,
            max_tokens=2000,
            timeout=30.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"GPT analiz hatası: {e}")
        return ""

# YENİ: GELİŞMİŞ FORMAT HATASI BİLDİRİM FONKSİYONU
async def gelismis_format_hatasi_bildirimi(update: Update, kullanici_adi: str, orijinal_mesaj: str, eksik_bilgiler=None):
    """Eksik bilgi durumlarında kullanıcıya detaylı format örneği göster"""
    try:
        # Kullanıcı adını etiketlemek için
        kullanici_etiketi = f"@{kullanici_adi}" if kullanici_adi and not kullanici_adi.startswith('@') else kullanici_adi
        
        # Eksik bilgilere göre özelleştirilmiş mesaj
        if eksik_bilgiler:
            mesaj_basligi = f"Selamun aleyküm {kullanici_etiketi} 👋\n\n"
            
            if "tarih" in eksik_bilgiler:
                mesaj_basligi += "📅 **Tarih belirtilmemiş** - "
            if "santiye" in eksik_bilgiler:
                mesaj_basligi += "📍 **Şantiye adı belirtilmemiş** - "
            if "genel_ozet" in eksik_bilgiler:
                mesaj_basligi += "📝 **Genel özet bulunamadı** - "
            if "coklu_rapor" in eksik_bilgiler:
                mesaj_basligi += "🔄 **Birden fazla tarih/şantiye tespit edildi** - "
            
            mesaj_basligi += "İşlem yapabilmem için raporunuzu aşağıdaki örnek formatta göndermelisiniz:\n\n"
        else:
            mesaj_basligi = f"Selamun aleyküm {kullanici_etiketi} 👋\n\nİşlem yapabilmem için raporunuzu aşağıdaki örnek formatta göndermelisiniz:\n\n"
        
        ornek_format = f"""
{mesaj_basligi}
📍 **ŞANTİYE**: LOT13 
📅 **TARİH**: 25.11.2025

**ÇALIŞMA DETAYLARI:**
B1 bodrum tava konsol montaj 2 kişi
B1 bodrum tava konsol montaj 2 kişi  
3.kat tava montajı 2 kişi
2.kat tava montajı 2 kişi

📝 **GENEL ÖZET:**
• Toplam staff: 2
• Toplam imalat: 12
• Toplam mobilizasyon: 1 kişi
• İzinli: 0
• Genel toplam: 12 kişi
• Dış görev stadyum 2 kişi

---

**Eğer çalışma yok ise:**

📍 **ŞANTİYE**: LOT13 
📅 **TARİH**: 25.11.2025

Çalışma yok


**Not:** Tarihleri mutlaka belirtmelisiniz, her tarihe ve santiyeye ait raporu ayri ayri gondermelisiniz
"""
        
        await update.message.reply_text(ornek_format)
        logging.info(f"📝 Gelişmiş format hatası bildirimi gönderildi: {kullanici_adi}, Eksikler: {eksik_bilgier}")
        
    except Exception as e:
        logging.error(f"❌ Gelişmiş format hatası bildirimi gönderilemedi: {e}")

# YENİ: RAPOR ANALİZ FONKSİYONU - EKSİK BİLGİ TESPİTİ
def analyze_report_for_missing_info(metin, gpt_raporlar):
    """Rapor metnini analiz ederek eksik bilgileri tespit et"""
    eksik_bilgiler = []
    
    try:
        # Tarih kontrolü
        tarih_patterns = [
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{4})',
            r'(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{2})',
            r'(\d{4})[\.\/\-](\d{1,2})[\.\/\-](\d{1,2})',
            r'\b(bugün|bugun|dün|dun)\b'
        ]
        
        has_date = False
        for pattern in tarih_patterns:
            if re.search(pattern, metin, re.IGNORECASE):
                has_date = True
                break
        
        if not has_date:
            eksik_bilgiler.append("tarih")
        
        # Şantiye kontrolü
        santiye_patterns = [
            r'\b(LOT13|LOT71|SKP|BWC|PİRAMİT|STADYUM|DMC|YHP|TYM|MMP|RMC|KÖKSARAY|OHP)\b',
            r'\b(LOT\s*13|LOT\s*71)\b',
            r'\b(Piramit|Piramit Tower)\b'
        ]
        
        has_santiye = False
        for pattern in santiye_patterns:
            if re.search(pattern, metin, re.IGNORECASE):
                has_santiye = True
                break
        
        if not has_santiye:
            eksik_bilgiler.append("santiye")
        
        # Genel özet kontrolü
        genel_ozet_patterns = [
            r'\b(genel\s+toplam|toplam\s+personel|toplam\s+kişi|özet|summary)\b',
            r'\b(staff|çalışan|mobilizasyon|ambarci|izinli)\s*:?\s*\d+\b'
        ]
        
        has_genel_ozet = False
        for pattern in genel_ozet_patterns:
            if re.search(pattern, metin, re.IGNORECASE):
                has_genel_ozet = True
                break
        
        if not has_genel_ozet:
            eksik_bilgier.append("genel_ozet")
        
        # Çoklu rapor kontrolü (birden fazla tarih veya şantiye)
        tarih_sayisi = len(re.findall(r'\d{1,2}[\.\/\-]\d{1,2}[\.\/\-]\d{2,4}', metin))
        santiye_sayisi = len(re.findall(r'\b(LOT13|LOT71|SKP|BWC|PİRAMİT|STADYUM|DMC|YHP|TYM|MMP|RMC)\b', metin, re.IGNORECASE))
        
        if tarih_sayisi > 1 or santiye_sayisi > 1:
            eksik_bilgiler.append("coklu_rapor")
        
        # GPT raporlarını kontrol et
        if gpt_raporlar and isinstance(gpt_raporlar, list):
            for rapor in gpt_raporlar:
                if isinstance(rapor, dict):
                    if not rapor.get('date') or rapor.get('date') == '':
                        if "tarih" not in eksik_bilgiler:
                            eksik_bilgiler.append("tarih")
                    if not rapor.get('site') or rapor.get('site') in ['BELİRSİZ', '']:
                        if "santiye" not in eksik_bilgiler:
                            eksik_bilgiler.append("santiye")
                    if rapor.get('genel_toplam', 0) == 0 and sum([
                        rapor.get('staff', 0), 
                        rapor.get('calisan', 0), 
                        rapor.get('mobilizasyon', 0),
                        rapor.get('ambarci', 0),
                        rapor.get('izinli', 0),
                        rapor.get('dis_gorev_toplam', 0)
                    ]) == 0:
                        if "genel_ozet" not in eksik_bilgiler:
                            eksik_bilgiler.append("genel_ozet")
        
        return eksik_bilgiler
        
    except Exception as e:
        logging.error(f"Rapor analiz hatası: {e}")
        return ["analiz_hatasi"]

# Doğrulama ile gelişmiş process_incoming_message
def process_incoming_message(raw_text: str, is_group: bool = False):
    """Kapsamlı doğrulama ile gelen mesajı işle"""
    is_valid, cleaned_text = validate_user_input(raw_text)
    if not is_valid:
        return [] if is_group else {"error": "geçersiz_giriş"}
    
    today = dt.date.today()
    max_retries = 2
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            user_prompt = cleaned_text
            content = gpt_analyze_enhanced(SYSTEM_PROMPT, user_prompt)
            
            if not content:
                if attempt < max_retries - 1:
                    time_module.sleep(retry_delay)
                    continue
                return [] if is_group else {"dm_info": "no_report_detected"}
            
            data = safe_json_loads(content)
            if data is None:
                if attempt < max_retries - 1:
                    time_module.sleep(retry_delay)
                    continue
                return [] if is_group else {"dm_info": "no_report_detected"}
            
            if isinstance(data, dict):
                data = [data]
            
            if not isinstance(data, list):
                if attempt < max_retries - 1:
                    time_module.sleep(retry_delay)
                    continue
                return [] if is_group else {"dm_info": "no_report_detected"}
            
            filtered_reports = []
            for report in data:
                if not isinstance(report, dict):
                    continue
                    
                date_str = report.get('date')
                if date_str:
                    try:
                        report_date = dt.datetime.strptime(date_str, '%Y-%m-%d').date()
                        if report_date > today:
                            report['date'] = today.strftime('%Y-%m-%d')
                    except ValueError:
                        report['date'] = today.strftime('%Y-%m-%d')
                else:
                    report['date'] = today.strftime('%Y-%m-%d')
                
                site = report.get('site', 'BELİRSİZ')
                # GPT'DEN GELEN ŞANTİYE İSMİNİ NORMALİZE ET - EKLENDİ
                report['site'] = normalize_site_name(site)
                
                for key in ['staff', 'calisan', 'mobilizasyon', 'ambarci', 'izinli', 'dis_gorev_toplam', 'genel_toplam']:
                    value = report.get(key, 0)
                    if not isinstance(value, int):
                        try:
                            report[key] = int(value) if value else 0
                        except (ValueError, TypeError):
                            report[key] = 0
                
                if report.get('genel_toplam', 0) == 0:
                    staff = report.get('staff', 0)
                    calisan = report.get('calisan', 0)
                    mobilizasyon = report.get('mobilizasyon', 0)
                    ambarci = report.get('ambarci', 0)
                    izinli = report.get('izinli', 0)
                    dis_gorev_toplam = report.get('dis_gorev_toplam', 0)
                    report['genel_toplam'] = staff + calisan + mobilizasyon + ambarci + izinli + dis_gorev_toplam
                
                if report['genel_toplam'] > 0 or report['staff'] > 0:
                    filtered_reports.append(report)
            
            return filtered_reports
                
        except Exception as e:
            logging.error(f"Mesaj işleme hatası (deneme {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time_module.sleep(retry_delay)
    
    return [] if is_group else {"dm_info": "no_report_detected"}

# RAPOR KAYIT FONKSİYONU - ŞANTİYE BAZLI SİSTEM
async def raporu_gpt_formatinda_kaydet(user_id, kullanici_adi, orijinal_metin, gpt_rapor, msg, rapor_no=1):
    try:
        site = gpt_rapor.get('site', 'BELİRSİZ')
        date_str = gpt_rapor.get('date')
        
        # GPT'DEN GELEN ŞANTİYE İSMİNİ NORMALİZE ET - EKLENDİ
        site = normalize_site_name(site)
        
        rapor_tarihi = None
        if date_str:
            try:
                rapor_tarihi = dt.datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        
        if not rapor_tarihi:
            rapor_tarihi = parse_rapor_tarihi(orijinal_metin) or dt.datetime.now(TZ).date()
        
        santiye_sorumlusu_id = get_santiye_sorumlusu(site)
        
        kaydedilecek_user_id = santiye_sorumlusu_id if santiye_sorumlusu_id else user_id
        kaydedilecek_kullanici_adi = id_to_name.get(santiye_sorumlusu_id, kullanici_adi) if santiye_sorumlusu_id else kullanici_adi
        
        staff = gpt_rapor.get('staff', 0)
        calisan = gpt_rapor.get('calisan', 0)
        mobilizasyon = gpt_rapor.get('mobilizasyon', 0)
        ambarci = gpt_rapor.get('ambarci', 0)
        izinli = gpt_rapor.get('izinli', 0)
        dis_gorev_toplam = gpt_rapor.get('dis_gorev_toplam', 0)
        genel_toplam = gpt_rapor.get('genel_toplam', 0)
        
        if genel_toplam == 0:
            genel_toplam = staff + calisan + mobilizasyon + ambarci + izinli + dis_gorev_toplam
        
        project_name = site
        if not project_name or project_name == 'BELİRSİZ':
            if santiye_sorumlusu_id:
                user_projects = id_to_projects.get(santiye_sorumlusu_id, [])
            else:
                user_projects = id_to_projects.get(user_id, [])
                
            if user_projects:
                project_name = user_projects[0]
            else:
                project_name = 'BELİRSİZ'
        
        existing_report = await async_fetchone("""
            SELECT id FROM reports 
            WHERE project_name = %s AND report_date = %s
        """, (project_name, rapor_tarihi))
        
        has_existing_report = False
        if existing_report is not None:
            existing_id = safe_get_tuple_value(existing_report, 0)
            if existing_id is not None:
                has_existing_report = True
        
        if has_existing_report:
            logging.warning(f"⚠️ Zaten rapor var: {project_name} - {rapor_tarihi}")
            raise Exception(f"Bu şantiye için bugün zaten rapor gönderilmiş: {project_name}")
        
        if izinli > 0:
            rapor_tipi = "IZIN/ISYOK"
        else:
            rapor_tipi = "RAPOR"
        
        work_description = f"Staff:{staff} Çalışan:{calisan} Mobilizasyon:{mobilizasyon} Ambarcı:{ambarci} İzinli:{izinli}"
        if dis_gorev_toplam > 0:
            work_description += f" DışGörevToplam:{dis_gorev_toplam}"
        
        if santiye_sorumlusu_id and santiye_sorumlusu_id != user_id:
            work_description += f" [Raporu ileten: {kullanici_adi}]"
        
        ai_analysis = {
            "yeni_sabit_format": gpt_rapor,
            "extraction_method": "yeni-sabit-json-format",
            "original_text": orijinal_metin[:500],
            "calculated_total": genel_toplam,
            "personel_dagilimi": {
                "staff": staff,
                "calisan": calisan,
                "mobilizasyon": mobilizasyon,
                "ambarci": ambarci,
                "izinli": izinli
            },
            "rapor_gonderen": {
                "user_id": user_id,
                "kullanici_adi": kullanici_adi
            },
            "santiye_sorumlusu": {
                "user_id": santiye_sorumlusu_id,
                "kullanici_adi": id_to_name.get(santiye_sorumlusu_id, "Belirsiz") if santiye_sorumlusu_id else "Belirsiz"
            } if santiye_sorumlusu_id else None
        }
        
        await async_execute("""
            INSERT INTO reports 
            (user_id, project_name, report_date, report_type, person_count, work_description, 
             work_category, personnel_type, delivered_date, is_edited, ai_analysis)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            kaydedilecek_user_id, project_name, rapor_tarihi, rapor_tipi, genel_toplam, 
            work_description[:400], 'detaylı', 'imalat', dt.datetime.now(TZ).date(),
            False, json.dumps(ai_analysis, ensure_ascii=False)
        ))
        
        if santiye_sorumlusu_id and santiye_sorumlusu_id != user_id:
            logging.info(f"✅ ŞANTİYE BAZLI Rapor #{rapor_no} ŞANTİYE SORUMLUSU adına kaydedildi: {kaydedilecek_kullanici_adi} (Raporu ileten: {kullanici_adi}) - {project_name} - {rapor_tarihi}")
        else:
            logging.info(f"✅ ŞANTİYE BAZLI Rapor #{rapor_no} kaydedildi: {user_id} - {project_name} - {rapor_tarihi}")
            
        logging.info(f"📊 Personel Dağılımı: Staff:{staff}, Çalışan:{calisan}, Mobilizasyon:{mobilizasyon}, Ambarcı:{ambarci}, İzinli:{izinli}, DışGörevToplam:{dis_gorev_toplam}, GenelToplam:{genel_toplam}")
        
        maliyet_analiz.kayit_ekle('gpt')
            
    except Exception as e:
        logging.error(f"❌ Şantiye bazlı rapor kaydetme hatası: {e}")
        raise e

# GÜNCELLENMİŞ: YENİ GPT RAPOR İŞLEME FONKSİYONU
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
        
        # DM için özel işlem
        if is_dm and isinstance(raporlar, dict) and raporlar.get('dm_info') == 'no_report_detected':
            await msg.reply_text(
                "❌ Bu mesaj bir rapor olarak algılanmadı.\n\n"
                "Lütfen şantiye, tarih ve iş bilgilerini içeren bir rapor gönderin.\n"
                "Örnek: \"01.11.2024 LOT13 2.kat kablo çekimi 5 kişi\""
            )
            return
        
        # GPT rapor algıladı ama işleyemedi veya eksik bilgi var
        if not raporlar or (isinstance(raporlar, list) and len(raporlar) == 0):
            logging.info(f"🤖 GPT: Rapor algılandı ancak işlenemedi - {user_id} (Chat Type: {chat_type})")
            
            # Eksik bilgileri analiz et
            eksik_bilgiler = analyze_report_for_missing_info(metin, raporlar)
            
            # Grup mesajı ise gelişmiş format hatası bildirimi gönder
            if is_group:
                kullanici_adi = msg.from_user.username or msg.from_user.first_name
                await gelismis_format_hatasi_bildirimi(update, kullanici_adi, metin, eksik_bilgiler)
            elif is_dm:
                await msg.reply_text(
                    "❌ Raporunuz algılandı ancak işlenemedi.\n\n"
                    "Lütfen şantiye raporunuzu aşağıdaki formatta gönderin:\n"
                    "• Tarih (01.01.2025)\n" 
                    "• Şantiye adı (LOT13, BWC, SKP vb.)\n"
                    "• Yapılan işler\n"
                    "• Personel bilgisi\n\n"
                    "Örnek: \"01.11.2024 LOT13 2.kat kablo çekimi 5 kişi\""
                )
            return

        logging.info(f"🤖 GPT: {len(raporlar)} rapor çıkarıldı - {user_id} (Chat Type: {chat_type})")
        
        # GPT raporları çıkarıldı ama eksik bilgi kontrolü yap
        eksik_bilgiler = analyze_report_for_missing_info(metin, raporlar)
        
        # Eksik bilgi varsa ve grup mesajı ise bildirim gönder
        if eksik_bilgiler and is_group:
            kullanici_adi = msg.from_user.username or msg.from_user.first_name
            await gelismis_format_hatasi_bildirimi(update, kullanici_adi, metin, eksik_bilgiler)
            return
        
        kullanici_adi = id_to_name.get(user_id, "Kullanıcı")
        
        basarili_kayitlar = 0
        for i, rapor in enumerate(raporlar):
            try:
                await raporu_gpt_formatinda_kaydet(user_id, kullanici_adi, metin, rapor, msg, i+1)
                basarili_kayitlar += 1
            except Exception as e:
                logging.error(f"❌ Rapor {i+1} kaydetme hatası: {e}")
                
                # Grup mesajı ise ve kayıt hatası olursa format hatası bildirimi gönder
                if is_group and "zaten rapor" not in str(e).lower():
                    kullanici_adi = msg.from_user.username or msg.from_user.first_name
                    eksik_bilgiler = ["kayit_hatasi"]
                    await gelismis_format_hatasi_bildirimi(update, kullanici_adi, metin, eksik_bilgiler)
                    
                if is_dm:
                    await msg.reply_text(f"❌ Rapor {i+1} kaydedilemedi: {str(e)}")
        
        # Başarılı kayıt bildirimi - SADECE DM'DE
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
        
        # Grup mesajı ise genel hata durumunda da format hatası bildirimi gönder
        if is_group:
            try:
                kullanici_adi = msg.from_user.username or msg.from_user.first_name
                eksik_bilgiler = ["sistem_hatasi"]
                await gelismis_format_hatasi_bildirimi(update, kullanici_adi, metin, eksik_bilgiler)
            except Exception as format_error:
                logging.error(f"❌ Format hatası bildirimi gönderilemedi: {format_error}")
                
        if is_dm:
            await msg.reply_text("❌ Rapor işlenirken bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

async def excel_durum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    try:
        mesaj = "📊 EXCEL SİSTEM DURUMU - ŞANTİYE BAZLI SİSTEM\n\n"
        
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
        
        mesaj += "📈 ŞANTİYE İSTATİSTİKLERİ:\n"
        mesaj += f"• Aktif Kullanıcı: {len(rapor_sorumlulari)} \n"
        mesaj += f"• Adminler: {len(ADMINS)}\n"
        mesaj += f"• İzleyiciler: {len(IZLEYICILER)}\n"
        mesaj += f"• Toplam Kullanıcı: {len(TUM_KULLANICILAR)}\n"
        mesaj += f"• Şantiyeler: {len(santiye_sorumlulari)} \n\n"
        
        mesaj += "🏗️ AKTİF ŞANTİYELER :\n"
        for santiye in sorted(santiye_sorumlulari.keys())[:10]:
            sorumlu_sayisi = len(santiye_sorumlulari[santiye])
            mesaj += f"• {santiye}: {sorumlu_sayisi} sorumlu\n"
        
        if len(santiye_sorumlulari) > 10:
            mesaj += f"• ... ve {len(santiye_sorumlulari) - 10} diğer şantiye\n"
        
        mesaj += "\n🛡️ GÜVENLİK SİSTEMİ:\n"
        mesaj += f"• Fallback Aktif: {'Evet' if df is not None and any(df['Telegram ID'] == 1000157326) else 'Hayır'}\n"
        mesaj += f"• Super Admin: {SUPER_ADMIN_ID} ({'Aktif' if SUPER_ADMIN_ID in ADMINS else 'Pasif'})\n"
        mesaj += f"• Telegram ID Format: 8-10 digit\n"
        mesaj += f"• Aktif/Pasif Kontrolü: 'E'/'H'\n"
        
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

# Gelişmiş hata yönetimi ile veritabanı başlatma
def init_database():
    """Kapsamlı hata yönetimi ile veritabanını başlat"""
    try:
        _sync_execute_safe("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id=1), 
                version INTEGER NOT NULL
            )
        """)
        
        _sync_execute_safe("""
            INSERT INTO schema_version (id, version) 
            SELECT 1, 2
            WHERE NOT EXISTS(SELECT 1 FROM schema_version WHERE id=1)
        """)
        
        _sync_execute_safe("""
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
        
        _sync_execute_safe("""
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
        
        index_queries = [
            "CREATE INDEX IF NOT EXISTS idx_reports_date_user ON reports(report_date, user_id)",
            "CREATE INDEX IF NOT EXISTS idx_reports_project_date ON reports(project_name, report_date)",
            "CREATE INDEX IF NOT EXISTS idx_reports_type_date ON reports(report_type, report_date)",
            "CREATE INDEX IF NOT EXISTS idx_reports_user_date ON reports(user_id, report_date)"
        ]
        
        for query in index_queries:
            try:
                _sync_execute_safe(query)
            except Exception as e:
                logging.warning(f"İndeks oluşturma uyarısı: {e}")
        
        logging.info("✅ Veritabanı şeması başarıyla başlatıldı")
        
    except Exception as e:
        logging.error(f"❌ Veritabanı başlatma hatası: {e}")
        raise

init_database()
init_db_pool()

async def get_santiye_rapor_durumu(bugun):
    """Güvenli tuple işleme ile şantiye rapor durumunu al - TÜMÜ FİLTRELENDİ"""
    try:
        rows = await async_fetchall("""
            SELECT DISTINCT project_name FROM reports 
            WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (bugun,))
        
        if not rows:
            return set()
            
        return set(normalize_site_name(safe_get_tuple_value(row, 0, '')) for row in rows 
                  if safe_get_tuple_value(row, 0, '') and safe_get_tuple_value(row, 0, '') != "TÜMÜ")
    except Exception as e:
        logging.error(f"Şantiye rapor durumu hatası: {e}")
        return set()

async def get_eksik_santiyeler(bugun):
    try:
        # TÜMÜ şantiyesini filtrele ve sabit şantiyeleri ekle
        tum_santiyeler = set(santiye for santiye in santiye_sorumlulari.keys() if santiye != "TÜMÜ")
        # SABİT ŞANTİYELERİ EKLE (FAP kaldırıldı)
        tum_santiyeler = tum_santiyeler.union(set(SABIT_SANTIYELER))
        rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
        eksik_santiyeler = tum_santiyeler - rapor_veren_santiyeler
        
        return {santiye: santiye_sorumlulari.get(santiye, []) for santiye in eksik_santiyeler}
    except Exception as e:
        logging.error(f"Eksik şantiye sorgu hatası: {e}")
        return {}

async def get_santiye_bazli_rapor_durumu(bugun):
    try:
        # TÜMÜ şantiyesini filtrele ve SABİT ŞANTİYELERİ EKLE (FAP kaldırıldı)
        tum_santiyeler = set(santiye for santiye in santiye_sorumlulari.keys() if santiye != "TÜMÜ")
        tum_santiyeler = tum_santiyeler.union(set(SABIT_SANTIYELER))
        rapor_veren_santiyeler = await get_santiye_rapor_durumu(bugun)
        
        rows = await async_fetchall("""
            SELECT project_name, user_id FROM reports 
            WHERE report_date = %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (bugun,))
        
        santiye_rapor_verenler = {}
        for row in rows:
            if row and len(row) >= 2:
                project_name = safe_get_tuple_value(row, 0, '')
                # PROJE ADINI NORMALİZE ET - EKLENDİ
                project_name = normalize_site_name(project_name)
                user_id = safe_get_tuple_value(row, 1, 0)
                if project_name and project_name != "TÜMÜ" and user_id:  # TÜMÜ filtrele
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
            result = _sync_fetchone_safe("""
                SELECT 
                    COUNT(*) as toplam,
                    SUM(CASE WHEN basarili = 1 THEN 1 ELSE 0 END) as basarili,
                    SUM(CASE WHEN basarili = 0 THEN 1 ELSE 0 END) as basarisiz,
                    MIN(timestamp) as ilk_tarih,
                    MAX(timestamp) as son_tarih
                FROM ai_logs
            """)
            
            if not result or len(result) < 5 or safe_get_tuple_value(result, 0, 0) is None or safe_get_tuple_value(result, 0, 0) == 0:
                return "🤖 AI Raporu: Henüz AI kullanımı yok"
            
            toplam = safe_get_tuple_value(result, 0, 0)
            basarili = safe_get_tuple_value(result, 1, 0)
            basarisiz = safe_get_tuple_value(result, 2, 0)
            ilk_tarih = safe_get_tuple_value(result, 3, '')
            son_tarih = safe_get_tuple_value(result, 4, '')
            
            rows = _sync_fetchall_safe("""
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
            rapor += f"• Başarısız: {basarilis}\n"
            rapor += f"• İlk Kullanım: {ilk_tarih[:10] if ilk_tarih else 'Yok'}\n"
            rapor += f"• Son Kullanım: {son_tarih[:10] if son_tarih else 'Yok'}\n\n"
            
            rapor += f"📅 Son 7 Gün:\n"
            for row in rows:
                if row and len(row) >= 3:
                    gun = safe_get_tuple_value(row, 0, '')
                    toplam_gun = safe_get_tuple_value(row, 1, 0)
                    basarili_gun = safe_get_tuple_value(row, 2, 0)
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
    
    result = await async_fetchone("SELECT EXISTS(SELECT 1 FROM reports WHERE project_name = %s AND report_date = %s)", 
                  (user_id, rapor_tarihi))
    
    exists = safe_get_tuple_value(result, 0, False) if result else False
    if exists:
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

# Personel özeti fonksiyonu - ŞANTİYE BAZLI - TÜMÜ FİLTRELENDİ
async def generate_gelismis_personel_ozeti(target_date):
    """Güvenli tuple işleme ile gelişmiş personel özeti oluştur"""
    try:
        rows = await async_fetchall("""
            SELECT user_id, report_type, project_name, person_count, work_description, ai_analysis
            FROM reports WHERE report_date = %s
        """, (target_date,))
        
        if not rows:
            return f"📭 {target_date.strftime('%d.%m.%Y')} tarihinde rapor bulunamadı."
        
        proje_analizleri = {}
        tum_projeler = set()
        
        genel_staff = 0
        genel_calisan = 0
        genel_mobilizasyon = 0
        genel_ambarci = 0
        genel_izinli = 0
        genel_dis_gorev_toplam = 0
        genel_toplam = 0
        
        for row in rows:
            if len(row) < 6:
                continue
            user_id = safe_get_tuple_value(row, 0, 0)
            rapor_tipi = safe_get_tuple_value(row, 1, '')
            proje_adi = safe_get_tuple_value(row, 2, 'BELİRSİZ')
            kisi_sayisi = safe_get_tuple_value(row, 3, 0)
            yapilan_is = safe_get_tuple_value(row, 4, '')
            ai_analysis = safe_get_tuple_value(row, 5, '{}')
            
            # PROJE ADINI NORMALİZE ET - EKLENDİ
            proje_adi = normalize_site_name(proje_adi)
            
            if not proje_adi or proje_adi == "TÜMÜ":
                continue
                
            if proje_adi not in proje_analizleri:
                proje_analizleri[proje_adi] = {
                    'toplam': 0,
                    'staff': 0, 'calisan': 0, 'mobilizasyon': 0, 'ambarci': 0, 'izinli': 0, 'dis_gorev_toplam': 0
                }
            
            try:
                ai_data = safe_json_loads(ai_analysis)
                yeni_format = ai_data.get('yeni_sabit_format', {})
                personel_dagilimi = ai_data.get('personel_dagilimi', {})
                
                if yeni_format:
                    staff_count = yeni_format.get('staff', 0)
                    calisan_count = yeni_format.get('calisan', 0)
                    mobilizasyon_count = yeni_format.get('mobilizasyon', 0)
                    ambarci_count = yeni_format.get('ambarci', 0)
                    izinli_count = yeni_format.get('izinli', 0)
                    dis_gorev_toplam_count = yeni_format.get('dis_gorev_toplam', 0)
                    
                    proje_analizleri[proje_adi]['staff'] += staff_count
                    proje_analizleri[proje_adi]['calisan'] += calisan_count
                    proje_analizleri[proje_adi]['mobilizasyon'] += mobilizasyon_count
                    proje_analizleri[proje_adi]['ambarci'] += ambarci_count
                    proje_analizleri[proje_adi]['izinli'] += izinli_count
                    proje_analizleri[proje_adi]['dis_gorev_toplam'] += dis_gorev_toplam_count
                    proje_analizleri[proje_adi]['toplam'] = (
                        proje_analizleri[proje_adi]['staff'] + 
                        proje_analizleri[proje_adi]['calisan'] + 
                        proje_analizleri[proje_adi]['mobilizasyon'] + 
                        proje_analizleri[proje_adi]['ambarci'] + 
                        proje_analizleri[proje_adi]['izinli'] + 
                        proje_analizleri[proje_adi]['dis_gorev_toplam']
                    )
                    
                elif personel_dagilimi:
                    staff_count = personel_dagilimi.get('staff', 0)
                    calisan_count = personel_dagilimi.get('calisan', 0)
                    mobilizasyon_count = personel_dagilimi.get('mobilizasyon', 0)
                    ambarci_count = personel_dagilimi.get('ambarci', 0)
                    izinli_count = personel_dagilimi.get('izinli', 0)
                    dis_gorev_toplam_count = personel_dagilimi.get('dis_gorev_toplam', 0)
                    
                    proje_analizleri[proje_adi]['staff'] += staff_count
                    proje_analizleri[proje_adi]['calisan'] += calisan_count
                    proje_analizleri[proje_adi]['mobilizasyon'] += mobilizasyon_count
                    proje_analizleri[proje_adi]['ambarci'] += ambarci_count
                    proje_analizleri[proje_adi]['izinli'] += izinli_count
                    proje_analizleri[proje_adi]['dis_gorev_toplam'] += dis_gorev_toplam_count
                    proje_analizleri[proje_adi]['toplam'] = (
                        proje_analizleri[proje_adi]['staff'] + 
                        proje_analizleri[proje_adi]['calisan'] + 
                        proje_analizleri[proje_adi]['mobilizasyon'] + 
                        proje_analizleri[proje_adi]['ambarci'] + 
                        proje_analizleri[proje_adi]['izinli'] + 
                        proje_analizleri[proje_adi]['dis_gorev_toplam']
                    )
                else:
                    yapilan_is_lower = (yapilan_is or '').lower()
                    
                    if 'staff' in yapilan_is_lower:
                        proje_analizleri[proje_adi]['staff'] += kisi_sayisi
                    elif 'mobilizasyon' in yapilan_is_lower:
                        proje_analizleri[proje_adi]['mobilizasyon'] += kisi_sayisi
                    elif 'ambarci' in yapilan_is_lower or 'ambarcı' in yapilan_is_lower:
                        proje_analizleri[proje_adi]['ambarci'] += kisi_sayisi
                    elif rapor_tipi == "IZIN/ISYOK":
                        proje_analizleri[proje_adi]['izinli'] += kisi_sayisi
                    else:
                        proje_analizleri[proje_adi]['calisan'] += kisi_sayisi
                    
                    proje_analizleri[proje_adi]['toplam'] += kisi_sayisi
                        
            except Exception as e:
                logging.error(f"Personel analiz hatası: {e}")
                yapilan_is_lower = (yapilan_is or '').lower()
                
                if 'staff' in yapilan_is_lower:
                    proje_analizleri[proje_adi]['staff'] += kisi_sayisi
                elif 'mobilizasyon' in yapilan_is_lower:
                    proje_analizleri[proje_adi]['mobilizasyon'] += kisi_sayisi
                elif 'ambarci' in yapilan_is_lower or 'ambarcı' in yapilan_is_lower:
                    proje_analizleri[proje_adi]['ambarci'] += kisi_sayisi
                elif rapor_tipi == "IZIN/ISYOK":
                    proje_analizleri[proje_adi]['izinli'] += kisi_sayisi
                else:
                    proje_analizleri[proje_adi]['calisan'] += kisi_sayisi
                
                proje_analizleri[proje_adi]['toplam'] += kisi_sayisi
            
            tum_projeler.add(proje_adi)
        
        for proje_adi, analiz in proje_analizleri.items():
            genel_staff += analiz['staff']
            genel_calisan += analiz['calisan']
            genel_mobilizasyon += analiz['mobilizasyon']
            genel_ambarci += analiz['ambarci']
            genel_izinli += analiz['izinli']
            genel_dis_gorev_toplam += analiz['dis_gorev_toplam']
            genel_toplam += analiz['toplam']
        
        mesaj = f"📊 {target_date.strftime('%d.%m.%Y')} GÜNLÜK PERSONEL ÖZETİ\n\n"
        
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: x[1]['toplam'], reverse=True):
            proje_toplam = analiz['toplam']
            if proje_toplam > 0:
                emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                mesaj += f"{emoji} {proje_adi}: {proje_toplam} kişi\n"
                
                durum_detay = []
                if analiz['staff'] > 0: 
                    durum_detay.append(f"Staff:{analiz['staff']}")
                if analiz['calisan'] > 0: 
                    durum_detay.append(f"Çalışan:{analiz['calisan']}")
                if analiz['mobilizasyon'] > 0: 
                    durum_detay.append(f"Mobilizasyon:{analiz['mobilizasyon']}")
                if analiz['ambarci'] > 0: 
                    durum_detay.append(f"Ambarcı:{analiz['ambarci']}")
                if analiz['izinli'] > 0: 
                    durum_detay.append(f"İzinli:{analiz['izinli']}")
                if analiz['dis_gorev_toplam'] > 0:
                    durum_detay.append(f"DışGörev:{analiz['dis_gorev_toplam']}")
                
                if durum_detay:
                    mesaj += f"   └─ {', '.join(durum_detay)}\n\n"
        
        mesaj += f"📈 GENEL TOPLAM: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 DAĞILIM:\n"
            if genel_staff > 0:
                mesaj += f"• Staff: {genel_staff} (%{genel_staff/genel_toplam*100:.1f})\n"
            if genel_calisan > 0:
                mesaj += f"• Çalışan: {genel_calisan} (%{genel_calisan/genel_toplam*100:.1f})\n"
            if genel_mobilizasyon > 0:
                mesaj += f"• Mobilizasyon: {genel_mobilizasyon} (%{genel_mobilizasyon/genel_toplam*100:.1f})\n"
            if genel_ambarci > 0:
                mesaj += f"• Ambarcı: {genel_ambarci} (%{genel_ambarci/genel_toplam*100:.1f})\n"
            if genel_izinli > 0:
                mesaj += f"• İzinli: {genel_izinli} (%{genel_izinli/genel_toplam*100:.1f})\n"
            if genel_dis_gorev_toplam > 0:
                mesaj += f"• Dış Görev: {genel_dis_gorev_toplam} (%{genel_dis_gorev_toplam/genel_toplam*100:.1f})\n"
        
        # TÜM SABİT ŞANTİYELERİ DAHİL ET (FAP kaldırıldı)
        tum_santiyeler = set(SABIT_SANTIYELER).union(set(santiye for santiye in santiye_sorumlulari.keys() if santiye != "TÜMÜ"))
        aktif_projeler = set(proje_analizleri.keys())
        eksik_projeler = [s for s in (tum_santiyeler - aktif_projeler) if s not in ["Belli değil", "Tümü"]]
        
        if eksik_projeler:
            mesaj += f"❌ EKSİK ŞANTİYELER: {', '.join(sorted(eksik_projeler))}"
        
        return mesaj
    except Exception as e:
        return f"❌ Rapor oluşturulurken hata oluştu: {e}"

# Haftalık rapor fonksiyonu - TÜMÜ FİLTRELENDİ
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
        
        toplam_rapor = sum([safe_get_tuple_value(x, 1, 0) for x in rows])
        gun_sayisi = (end_date - start_date).days + 1
        beklenen_rapor = len(rapor_sorumlulari) * gun_sayisi
        verimlilik = (toplam_rapor / beklenen_rapor * 100) if beklenen_rapor > 0 else 0
        
        proje_detay_rows = await async_fetchall("""
            SELECT project_name, ai_analysis
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (start_date, end_date))
        
        proje_analizleri = {}
        
        for row in proje_detay_rows:
            if len(row) < 2:
                continue
                
            proje_adi = safe_get_tuple_value(row, 0, '')
            ai_analysis = safe_get_tuple_value(row, 1, '{}')
            
            # PROJE ADINI NORMALİZE ET - EKLENDİ
            proje_adi = normalize_site_name(proje_adi)
            
            # TÜMÜ şantiyesini filtrele
            if not proje_adi or proje_adi == "TÜMÜ":
                continue
                
            if proje_adi not in proje_analizleri:
                proje_analizleri[proje_adi] = {
                    'staff': 0, 'calisan': 0, 'mobilizasyon': 0, 'ambarci': 0, 'izinli': 0, 'dis_gorev_toplam': 0, 'toplam': 0
                }
            
            try:
                ai_data = safe_json_loads(ai_analysis)
                yeni_format = ai_data.get('yeni_sabit_format', {})
                personel_dagilimi = ai_data.get('personel_dagilimi', {})
                
                if yeni_format:
                    proje_analizleri[proje_adi]['staff'] += yeni_format.get('staff', 0)
                    proje_analizleri[proje_adi]['calisan'] += yeni_format.get('calisan', 0)
                    proje_analizleri[proje_adi]['mobilizasyon'] += yeni_format.get('mobilizasyon', 0)
                    proje_analizleri[proje_adi]['ambarci'] += yeni_format.get('ambarci', 0)
                    proje_analizleri[proje_adi]['izinli'] += yeni_format.get('izinli', 0)
                    proje_analizleri[proje_adi]['dis_gorev_toplam'] += yeni_format.get('dis_gorev_toplam', 0)
                    proje_analizleri[proje_adi]['toplam'] = (
                        proje_analizleri[proje_adi]['staff'] + 
                        proje_analizleri[proje_adi]['calisan'] + 
                        proje_analizleri[proje_adi]['mobilizasyon'] + 
                        proje_analizleri[proje_adi]['ambarci'] + 
                        proje_analizleri[proje_adi]['izinli'] + 
                        proje_analizleri[proje_adi]['dis_gorev_toplam']
                    )
                    
                elif personel_dagilimi:
                    proje_analizleri[proje_adi]['staff'] += personel_dagilimi.get('staff', 0)
                    proje_analizleri[proje_adi]['calisan'] += personel_dagilimi.get('calisan', 0)
                    proje_analizleri[proje_adi]['mobilizasyon'] += personel_dagilimi.get('mobilizasyon', 0)
                    proje_analizleri[proje_adi]['ambarci'] += personel_dagilimi.get('ambarci', 0)
                    proje_analizleri[proje_adi]['izinli'] += personel_dagilimi.get('izinli', 0)
                    proje_analizleri[proje_adi]['dis_gorev_toplam'] += personel_dagilimi.get('dis_gorev_toplam', 0)
                    proje_analizleri[proje_adi]['toplam'] = (
                        proje_analizleri[proje_adi]['staff'] + 
                        proje_analizleri[proje_adi]['calisan'] + 
                        proje_analizleri[proje_adi]['mobilizasyon'] + 
                        proje_analizleri[proje_adi]['ambarci'] + 
                        proje_analizleri[proje_adi]['izinli'] + 
                        proje_analizleri[proje_adi]['dis_gorev_toplam']
                    )
                    
            except Exception as e:
                logging.error(f"Proje analiz hatası: {e}")
                continue
        
        genel_toplam = 0
        genel_staff = 0
        genel_calisan = 0
        genel_mobilizasyon = 0
        genel_ambarci = 0
        genel_izinli = 0
        genel_dis_gorev_toplam = 0
        
        for proje in proje_analizleri.values():
            genel_toplam += proje['toplam']
            genel_staff += proje['staff']
            genel_calisan += proje['calisan']
            genel_mobilizasyon += proje['mobilizasyon']
            genel_ambarci += proje['ambarci']
            genel_izinli += proje['izinli']
            genel_dis_gorev_toplam += proje['dis_gorev_toplam']
        
        # TÜM SABİT ŞANTİYELERİ DAHİL ET (FAP kaldırıldı)
        tum_santiyeler = set(SABIT_SANTIYELER).union(set(santiye for santiye in santiye_sorumlulari.keys() if santiye != "TÜMÜ"))
        rapor_veren_santiyeler = set(proje_analizleri.keys())
        eksik_santiyeler = [s for s in (tum_santiyeler - rapor_veren_santiyeler) if s not in ["Belli değil", "Tümü"]]
        
        mesaj = f"📈 HAFTALIK ÖZET RAPOR\n"
        mesaj += f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        
        mesaj += f"📊 GENEL İSTATİSTİKLER:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor}\n"
        mesaj += f"• Rapor Gönderen: {len(rows)} kişi\n"
        mesaj += f"• İş Günü: {gun_sayisi} gün\n"
        mesaj += f"• Verimlilik: %{verimlilik:.1f}\n"
        mesaj += f"• Toplam Personel: {genel_toplam} kişi\n\n"
        
        mesaj += f"🏗️ PROJE BAZLI PERSONEL:\n\n"
        
        onemli_projeler = ["SKP", "LOT13", "LOT71", "STADYUM", "BWC", "DMC", "YHP", "TYM", "MMP", "RMC", "PİRAMİT"]
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: x[1]['toplam'], reverse=True):
            if proje_adi in onemli_projeler and analiz['toplam'] > 0:
                mesaj += f"🏗️ {proje_adi}: {analiz['toplam']} kişi\n"
                mesaj += f"   └─ Staff:{analiz['staff']}, Çalışan:{analiz['calisan']}, Mobilizasyon:{analiz['mobilizasyon']}, Ambarcı:{analiz['ambarci']}, İzinli:{analiz['izinli']}, DışGörev:{analiz['dis_gorev_toplam']}\n\n"
        
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: x[1]['toplam'], reverse=True):
            if proje_adi not in onemli_projeler and analiz['toplam'] > 0:
                emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                mesaj += f"{emoji} {proje_adi}: {analiz['toplam']} kişi\n"
                
                detay = []
                if analiz['staff'] > 0: detay.append(f"Staff:{analiz['staff']}")
                if analiz['calisan'] > 0: detay.append(f"Çalışan:{analiz['calisan']}")
                if analiz['mobilizasyon'] > 0: detay.append(f"Mobilizasyon:{analiz['mobilizasyon']}")
                if analiz['ambarci'] > 0: detay.append(f"Ambarcı:{analiz['ambarci']}")
                if analiz['izinli'] > 0: detay.append(f"İzinli:{analiz['izinli']}")
                if analiz['dis_gorev_toplam'] > 0: detay.append(f"DışGörev:{analiz['dis_gorev_toplam']}")
                
                if detay:
                    mesaj += f"   └─ {', '.join(detay)}\n"
        
        mesaj += f"\n📈 GENEL TOPLAM: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 DAĞILIM:\n"
            if genel_staff > 0:
                mesaj += f"• Staff: {genel_staff} (%{genel_staff/genel_toplam*100:.1f})\n"
            if genel_calisan > 0:
                mesaj += f"• Çalışan: {genel_calisan} (%{genel_calisan/genel_toplam*100:.1f})\n"
            if genel_mobilizasyon > 0:
                mesaj += f"• Mobilizasyon: {genel_mobilizasyon} (%{genel_mobilizasyon/genel_toplam*100:.1f})\n"
            if genel_ambarci > 0:
                mesaj += f"• Ambarcı: {genel_ambarci} (%{genel_ambarci/genel_toplam*100:.1f})\n"
            if genel_izinli > 0:
                mesaj += f"• İzinli: {genel_izinli} (%{genel_izinli/genel_toplam*100:.1f})\n"
            if genel_dis_gorev_toplam > 0:
                mesaj += f"• Dış Görev: {genel_dis_gorev_toplam} (%{genel_dis_gorev_toplam/genel_toplam*100:.1f})\n"
        
        if eksik_santiyeler:
            mesaj += f"\n❌ EKSİK ŞANTİYELER: {', '.join(sorted(eksik_santiyeler))}"
        
        mesaj += "\n\n📝 Lütfen eksiksiz rapor paylaşımına devam edelim. Teşekkürler."
        
        return mesaj
    except Exception as e:
        return f"❌ Haftalık rapor oluşturulurken hata: {e}"

# Aylık rapor fonksiyonu - TÜMÜ FİLTRELENDİ
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
        
        toplam_rapor = sum([safe_get_tuple_value(x, 1, 0) for x in rows])
        gun_sayisi = (end_date - start_date).days + 1
        
        beklenen_rapor = len(rapor_sorumlulari) * gun_sayisi
        eksik_rapor = max(0, beklenen_rapor - toplam_rapor)
        
        proje_detay_rows = await async_fetchall("""
            SELECT project_name, ai_analysis
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND project_name IS NOT NULL AND project_name != 'BELİRSİZ'
        """, (start_date, end_date))
        
        proje_analizleri = {}
        
        for row in proje_detay_rows:
            if len(row) < 2:
                continue
                
            proje_adi = safe_get_tuple_value(row, 0, '')
            ai_analysis = safe_get_tuple_value(row, 1, '{}')
            
            # PROJE ADINI NORMALİZE ET - EKLENDİ
            proje_adi = normalize_site_name(proje_adi)
            
            # TÜMÜ şantiyesini filtrele
            if not proje_adi or proje_adi == "TÜMÜ":
                continue
                
            if proje_adi not in proje_analizleri:
                proje_analizleri[proje_adi] = {
                    'staff': 0, 'calisan': 0, 'mobilizasyon': 0, 'ambarci': 0, 'izinli': 0, 'dis_gorev_toplam': 0, 'toplam': 0
                }
            
            try:
                ai_data = safe_json_loads(ai_analysis)
                yeni_format = ai_data.get('yeni_sabit_format', {})
                personel_dagilimi = ai_data.get('personel_dagilimi', {})
                
                if yeni_format:
                    proje_analizleri[proje_adi]['staff'] += yeni_format.get('staff', 0)
                    proje_analizleri[proje_adi]['calisan'] += yeni_format.get('calisan', 0)
                    proje_analizleri[proje_adi]['mobilizasyon'] += yeni_format.get('mobilizasyon', 0)
                    proje_analizleri[proje_adi]['ambarci'] += yeni_format.get('ambarci', 0)
                    proje_analizleri[proje_adi]['izinli'] += yeni_format.get('izinli', 0)
                    proje_analizleri[proje_adi]['dis_gorev_toplam'] += yeni_format.get('dis_gorev_toplam', 0)
                    proje_analizleri[proje_adi]['toplam'] = (
                        proje_analizleri[proje_adi]['staff'] + 
                        proje_analizleri[proje_adi]['calisan'] + 
                        proje_analizleri[proje_adi]['mobilizasyon'] + 
                        proje_analizleri[proje_adi]['ambarci'] + 
                        proje_analizleri[proje_adi]['izinli'] + 
                        proje_analizleri[proje_adi]['dis_gorev_toplam']
                    )
                    
                elif personel_dagilimi:
                    proje_analizleri[proje_adi]['staff'] += personel_dagilimi.get('staff', 0)
                    proje_analizleri[proje_adi]['calisan'] += personel_dagilimi.get('calisan', 0)
                    proje_analizleri[proje_adi]['mobilizasyon'] += personel_dagilimi.get('mobilizasyon', 0)
                    proje_analizleri[proje_adi]['ambarci'] += personel_dagilimi.get('ambarci', 0)
                    proje_analizleri[proje_adi]['izinli'] += personel_dagilimi.get('izinli', 0)
                    proje_analizleri[proje_adi]['dis_gorev_toplam'] += personel_dagilimi.get('dis_gorev_toplam', 0)
                    proje_analizleri[proje_adi]['toplam'] = (
                        proje_analizleri[proje_adi]['staff'] + 
                        proje_analizleri[proje_adi]['calisan'] + 
                        proje_analizleri[proje_adi]['mobilizasyon'] + 
                        proje_analizleri[proje_adi]['ambarci'] + 
                        proje_analizleri[proje_adi]['izinli'] + 
                        proje_analizleri[proje_adi]['dis_gorev_toplam']
                    )
                    
            except Exception as e:
                logging.error(f"Proje analiz hatası: {e}")
                continue
        
        genel_toplam = 0
        genel_staff = 0
        genel_calisan = 0
        genel_mobilizasyon = 0
        genel_ambarci = 0
        genel_izinli = 0
        genel_dis_gorev_toplam = 0
        
        for proje in proje_analizleri.values():
            genel_toplam += proje['toplam']
            genel_staff += proje['staff']
            genel_calisan += proje['calisan']
            genel_mobilizasyon += proje['mobilizasyon']
            genel_ambarci += proje['ambarci']
            genel_izinli += proje['izinli']
            genel_dis_gorev_toplam += proje['dis_gorev_toplam']
        
        # TÜM SABİT ŞANTİYELERİ DAHİL ET (FAP kaldırıldı)
        tum_santiyeler = set(SABIT_SANTIYELER).union(set(santiye for santiye in santiye_sorumlulari.keys() if santiye != "TÜMÜ"))
        rapor_veren_santiyeler = set(proje_analizleri.keys())
        eksik_santiyeler = [s for s in (tum_santiyeler - rapor_veren_santiyeler) if s not in ["Belli değil", "Tümü"]]
        
        mesaj = f"🗓️ AYLIK ÖZET RAPOR\n"
        mesaj += f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        
        mesaj += f"📈 PERFORMANS ANALİZİ:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor}\n"
        mesaj += f"• Toplam EKSIK Rapor: {eksik_rapor}\n"
        mesaj += f"• İş Günü: {gun_sayisi} gün\n"
        mesaj += f"• Günlük Ort.: {toplam_rapor/gun_sayisi:.1f} rapor\n"
        mesaj += f"• Toplam Personel: {genel_toplam} kişi\n\n"
        
        mesaj += f"🏗️ PROJE BAZLI PERSONEL:\n\n"
        
        onemli_projeler = ["SKP", "LOT13", "LOT71", "BWC", "DMC", "YHP", "TYM", "MMP", "RMC", "PİRAMİT"]
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: x[1]['toplam'], reverse=True):
            if proje_adi in onemli_projeler and analiz['toplam'] > 0:
                mesaj += f"🏗️ {proje_adi}: {analiz['toplam']} kişi\n"
                mesaj += f"   └─ Staff:{analiz['staff']}, Çalışan:{analiz['calisan']}, Mobilizasyon:{analiz['mobilizasyon']}, Ambarcı:{analiz['ambarci']}, İzinli:{analiz['izinli']}, DışGörev:{analiz['dis_gorev_toplam']}\n\n"
        
        for proje_adi, analiz in sorted(proje_analizleri.items(), key=lambda x: x[1]['toplam'], reverse=True):
            if proje_adi not in onemli_projeler and analiz['toplam'] > 0:
                emoji = "🏢" if proje_adi == "TYM" else "🏗️"
                mesaj += f"{emoji} {proje_adi}: {analiz['toplam']} kişi\n"
                
                detay = []
                if analiz['staff'] > 0: detay.append(f"Staff:{analiz['staff']}")
                if analiz['calisan'] > 0: detay.append(f"Çalışan:{analiz['calisan']}")
                if analiz['mobilizasyon'] > 0: detay.append(f"Mobilizasyon:{analiz['mobilizasyon']}")
                if analiz['ambarci'] > 0: detay.append(f"Ambarcı:{analiz['ambarci']}")
                if analiz['izinli'] > 0: detay.append(f"İzinli:{analiz['izinli']}")
                if analiz['dis_gorev_toplam'] > 0: detay.append(f"DışGörev:{analiz['dis_gorev_toplam']}")
                
                if detay:
                    mesaj += f"   └─ {', '.join(detay)}\n"
        
        mesaj += f"\n📈 GENEL TOPLAM: {genel_toplam} kişi\n"
        
        if genel_toplam > 0:
            mesaj += f"🎯 DAĞILIM:\n"
            if genel_staff > 0:
                mesaj += f"• Staff: {genel_staff} (%{genel_staff/genel_toplam*100:.1f})\n"
            if genel_calisan > 0:
                mesaj += f"• Çalışan: {genel_calisan} (%{genel_calisan/genel_toplam*100:.1f})\n"
            if genel_mobilizasyon > 0:
                mesaj += f"• Mobilizasyon: {genel_mobilizasyon} (%{genel_mobilizasyon/genel_toplam*100:.1f})\n"
            if genel_ambarci > 0:
                mesaj += f"• Ambarcı: {genel_ambarci} (%{genel_ambarci/genel_toplam*100:.1f})\n"
            if genel_izinli > 0:
                mesaj += f"• İzinli: {genel_izinli} (%{genel_izinli/genel_toplam*100:.1f})\n"
            if genel_dis_gorev_toplam > 0:
                mesaj += f"• Dış Görev: {genel_dis_gorev_toplam} (%{genel_dis_gorev_toplam/genel_toplam*100:.1f})\n"
        
        if eksik_santiyeler:
            mesaj += f"\n❌ EKSİK ŞANTİYELER: {', '.join(sorted(eksik_santiyeler))}"
        
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
        
        toplam_rapor = sum([safe_get_tuple_value(x, 1, 0) for x in rows])
        gun_sayisi = (end_date - start_date).days + 1
        
        personel_result = await async_fetchone("""
            SELECT COALESCE(SUM(person_count), 0) as toplam_kisi
            FROM reports 
            WHERE report_date BETWEEN %s AND %s AND report_type = 'RAPOR'
        """, (start_date, end_date))
        
        toplam_personel = safe_get_tuple_value(personel_result, 0, 0)
        
        mesaj = f"📅 TARİH ARALIĞI RAPORU\n"
        mesaj += f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        
        mesaj += f"📊 GENEL İSTATİSTİKLER:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor}\n"
        mesaj += f"• Rapor Gönderen: {len(rows)} kişi\n"
        mesaj += f"• Gün Sayısı: {gun_sayisi} gün\n"
        mesaj += f"• Günlük Ort.: {toplam_rapor/gun_sayisi:.1f} rapor\n"
        mesaj += f"• Toplam Personel: {toplam_personel} kişi\n\n"
        
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
                mesaj += f"🏗️ {santiye} ({len(sorumlular)} sorumlu)\n\n"
        
        if durum['rapor_veren_santiyeler']:
            mesaj += f"✅ Rapor İleten Şantiyeler ({len(durum['rapor_veren_santiyeler'])}):\n"
            for santiye in sorted(durum['rapor_veren_santiyeler']):
                mesaj += f"• {santiye}\n"
        
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
        bugun_rapor_sayisi = safe_get_tuple_value(bugun_rapor, 0, 0)
        
        hafta_rapor = await async_fetchone("""
            SELECT COUNT(*) FROM reports WHERE report_date BETWEEN %s AND %s
        """, (bu_hafta_baslangic, bugun))
        hafta_rapor_sayisi = safe_get_tuple_value(hafta_rapor, 0, 0)
        
        ay_rapor = await async_fetchone("""
            SELECT COUNT(*) FROM reports WHERE report_date BETWEEN %s AND %s
        """, (bu_ay_baslangic, bugun))
        ay_rapor_sayisi = safe_get_tuple_value(ay_rapor, 0, 0)
        
        toplam_kullanici = await async_fetchone("SELECT COUNT(DISTINCT user_id) FROM reports")
        toplam_kullanici_sayisi = safe_get_tuple_value(toplam_kullanici, 0, 0)
        
        toplam_rapor = await async_fetchone("SELECT COUNT(*) FROM reports")
        toplam_rapor_sayisi = safe_get_tuple_value(toplam_rapor, 0, 0)
        
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        mesaj = "📊 GENEL İSTATİSTİKLER \n\n"
        
        mesaj += "📅 GÜNLÜK İSTATİSTİKLER:\n"
        mesaj += f"• Bugünkü Rapor: {bugun_rapor_sayisi}\n"
        mesaj += f"• Bu Hafta: {hafta_rapor_sayisi}\n"
        mesaj += f"• Bu Ay: {ay_rapor_sayisi}\n\n"
        
        mesaj += "👥 KULLANICI İSTATİSTİKLERİ:\n"
        mesaj += f"• Toplam Kullanıcı: {toplam_kullanici_sayisi}\n"
        mesaj += f"• Aktif Kullanıcı: {len(rapor_sorumlulari)} \n"
        mesaj += f"• Admin: {len(ADMINS)}\n"
        mesaj += f"• Şantiye: {len(santiye_sorumlulari)} \n\n"
        
        mesaj += "🎯 PERFORMANS İSTATİSTİKLERİ:\n"
        mesaj += f"• Toplam Rapor: {toplam_rapor_sayisi}\n"
        
        if toplam_kullanici_sayisi > 0:
            ortalama_rapor = toplam_rapor_sayisi / toplam_kullanici_sayisi
            mesaj += f"• Kullanıcı Başı Ortalama: {ortalama_rapor:.1f} rapor\n"
        
        mesaj += f"\n🏗️ BUGÜNKÜ ŞANTİYE DURUMU :\n"
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
        "🤖 Rapor Botu Aktif! \n\n"
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
            f"🤖 Yapay Zeka Destekli Rapor Botu - ŞANTİYE BAZLI SİSTEM\n\n"
            f"👋 Hoş geldiniz {user_name}!\n\n"
            f"📋 Tüm Kullanıcılar İçin:\n"
            f"• Rapor göndermek için direkt mesaj yazın\n"
            f"`/start` - Botu başlat\n"
            f"`/info` - Komut bilgisi\n"
            f"`/hakkinda` - Bot hakkında\n\n"
            f"🛡️ Admin Komutları:\n"
            f"`/bugun` - Bugünün özeti (Admin)\n"
            f"`/dun` - Dünün özeti (Admin)\n"
            f"`/eksikraporlar` - Eksik raporları listele (Admin)\n"
            f"`/istatistik` - Genel istatistikler (Admin)\n"
            f"`/haftalik_rapor` - Haftalık rapor (Admin)\n"
            f"`/aylik_rapor` - Aylık rapor (Admin)\n"
            f"`/tariharaligi` - Tarih aralığı raporu (Admin)\n"
            f"`/haftalik_istatistik` - Haftalık istatistik (Admin)\n"
            f"`/aylik_istatistik` - Aylık istatistik (Admin)\n"
            f"`/excel_tariharaligi` - Excel raporu (Admin)\n"
            f"`/maliyet` - Maliyet analizi (Admin)\n"
            f"`/ai_rapor` - Detaylı AI raporu (Admin)\n"
            f"`/kullanicilar` - Tüm kullanıcı listesi (Admin)\n"
            f"`/santiyeler` - Şantiye listesi (Admin)\n"
            f"`/santiye_durum` - Şantiye rapor durumu (Admin)\n\n"
            f"⚡ Super Admin Komutları:\n"
            f"`/reload` - Excel dosyasını yenile (Super Admin)\n"
            f"`/yedekle` - Manuel yedekleme (Super Admin)\n"
            f"`/chatid` - Chat ID göster (Super Admin)\n"
            f"`/excel_durum` - Excel sistem durumu (Super Admin)\n"
            f"`/reset_database` - Veritabanını sıfırla (Super Admin)\n"
            f"`/fix_sequences` - Sequence'leri düzelt (Super Admin)\n\n"
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
        "🤖 Rapor Botu Hakkında \n\n"
        "Geliştirici: Atamurat Kamalov\n"
        "Versiyon: 4.6.8 \n"
        "Özellikler:\n"
        "• Akıllı Rapor Analizi: GPT-4 ile otomatik rapor parsing ve analiz\n"
        "• Çoklu şantiye desteği\n"
        "• Gerçek Zamanlı İşleme: Anında rapor işleme ve kaydetme\n"
        "• Günlük / Haftalık / Aylık icmal rapor ve istatistik oluşturur\n"
        "• Her sabah 09:00'da dünkü personel icmalini Eren Boz'a gönderir\n"
        "• Gün içinde gruba otomatik hatırlatma mesajları gönderir\n"
        "• Çift sayma koruması ile doğru toplamlar\n"
        "• Şantiye bazlı rapor sistemi\n"
        "• Haftalık rapor Cumartesi 17:35'te gönderilir\n"
        "• Aylık rapor her ayın 1'inde 09:30'da gönderilir\n"
        "• Format hatası bildirimi ile kullanıcıları yönlendirir\n"
        "• Eksik bilgi tespiti ve düzeltme isteği\n"
        "• 'Yerel Ekipbaşı' kategorisi staff olarak tanınır\n"
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
    
    mesaj = "👥 TÜM KULLANICI LİSTESİ - ŞANTİYE BAZLI\n\n"
    
    mesaj += f"📋 Aktif Kullanıcılar ({len(rapor_sorumlulari)} - Aktif/Pasif='E'):\n"
    
    # Proje bazlı kullanıcı sayıları - TÜMÜ hariç
    proje_kullanici_sayilari = {}
    for tid in rapor_sorumlulari:
        projeler = id_to_projects.get(tid, [])
        # TÜMÜ şantiyesini filtrele
        projeler = [proje for proje in projeler if proje != "TÜMÜ"]
        for proje in projeler:
            if proje not in proje_kullanici_sayilari:
                proje_kullanici_sayilari[proje] = 0
            proje_kullanici_sayilari[proje] += 1
    
    for proje, sayi in sorted(proje_kullanici_sayilari.items()):
        mesaj += f"• {proje}: {sayi} kullanıcı\n"
    
    mesaj += f"\n🛡️ Adminler: {len(ADMINS)}\n"
    mesaj += f"👀 İzleyiciler: {len(IZLEYICILER)}\n"
    mesaj += f"🏗️ Toplam Şantiye: {len(santiye_sorumlulari)} \n"
    
    await update.message.reply_text(mesaj)

async def santiyeler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    mesaj = "🏗️ ŞANTİYE LİSTESİ\n\n"
    
    # TÜMÜ şantiyesini filtrele
    filtered_santiyeler = {santiye: sorumlular for santiye, sorumlular in santiye_sorumlulari.items() if santiye != "TÜMÜ"}
    
    # Sabit şantiyeleri ekle (FAP kaldırıldı)
    for santiye in SABIT_SANTIYELER:
        if santiye not in filtered_santiyeler:
            filtered_santiyeler[santiye] = []
    
    for santiye in sorted(filtered_santiyeler.keys()):
        # Sadece şantiye ismini göster, sorumlu sayısını gösterme
        mesaj += f"• {santiye}\n"
    
    mesaj += f"\n📊 Toplam {len(filtered_santiyeler)} şantiye"
    
    await update.message.reply_text(mesaj)

async def santiye_durum_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_kontrol(update, context):
        return
    
    bugun = dt.datetime.now(TZ).date()
    durum = await get_santiye_bazli_rapor_durumu(bugun)
    
    # Sabit şantiyeleri ekle - artık get_santiye_bazli_rapor_durumu içinde zaten ekleniyor (FAP kaldırıldı)
    tum_santiyeler_with_sabit = durum['tum_santiyeler']
    eksik_santiyeler_with_sabit = tum_santiyeler_with_sabit - durum['rapor_veren_santiyeler']
    
    mesaj = f"📊 Şantiye Rapor Durumu - {bugun.strftime('%d.%m.%Y')} \n\n"
    
    mesaj += f"✅ Rapor İleten Şantiyeler ({len(durum['rapor_veren_santiyeler'])}):\n"
    for santiye in sorted(durum['rapor_veren_santiyeler']):
        mesaj += f"• {santiye}\n"
    
    mesaj += f"\n❌ Rapor İletilmeyen Şantiyeler ({len(eksik_santiyeler_with_sabit)}):\n"
    for santiye in sorted(eksik_santiyeler_with_sabit):
        if santiye in ["Belli değil", "Tümü"]:
            continue
        mesaj += f"• {santiye}\n"
    
    mesaj += f"\n📈 Özet: {len(durum['rapor_veren_santiyeler'])}/{len(tum_santiyeler_with_sabit)} şantiye rapor iletmiş"
    
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

async def reset_database_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    await update.message.reply_text("🔄 Veritabanı sıfırlanıyor... Bu işlem biraz zaman alabilir.")
    
    try:
        _sync_execute_safe("DROP SCHEMA public CASCADE")
        _sync_execute_safe("CREATE SCHEMA public")
        
        init_database()
        init_db_pool()
        
        await update.message.reply_text("✅ Veritabanı başarıyla sıfırlandı! Tüm tablolar yeniden oluşturuldu.")
        
    except Exception as e:
        logging.error(f"❌ Veritabanı sıfırlama hatası: {e}")
        await update.message.reply_text(f"❌ Veritabanı sıfırlama hatası: {e}")

async def fix_sequences_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await super_admin_kontrol(update, context):
        return
    
    await update.message.reply_text("🔄 Sequence'ler düzeltiliyor...")
    
    try:
        reports_result = await async_fetchone("SELECT COALESCE(MAX(id), 0) FROM reports")
        reports_max_id = safe_get_tuple_value(reports_result, 0, 0)
        new_reports_seq = max(reports_max_id + 1, 1)
        
        await async_execute(f"ALTER SEQUENCE reports_id_seq RESTART WITH {new_reports_seq}")
        
        ai_logs_result = await async_fetchone("SELECT COALESCE(MAX(id), 0) FROM ai_logs")
        ai_logs_max_id = safe_get_tuple_value(ai_logs_result, 0, 0)
        new_ai_logs_seq = max(ai_logs_max_id + 1, 1)
        
        await async_execute(f"ALTER SEQUENCE ai_logs_id_seq RESTART WITH {new_ai_logs_seq}")
        
        await update.message.reply_text(
            f"✅ Sequence'ler başarıyla düzeltildi!\n\n"
            f"📊 Reports: {new_reports_seq}\n"
            f"🤖 AI Logs: {new_ai_logs_seq}"
        )
        
    except Exception as e:
        logging.error(f"❌ Sequence düzeltme hatası: {e}")
        await update.message.reply_text(f"❌ Sequence düzeltme hatası: {e}")

async def create_excel_report(start_date, end_date, rapor_baslik):
    try:
        rows = await async_fetchall("""
            SELECT r.user_id, r.report_date, r.report_type, r.work_description, 
                   r.person_count, r.project_name, r.work_category, r.personnel_type,
                   r.delivered_date, r.is_edited, r.ai_analysis
            FROM reports r
            WHERE r.report_date BETWEEN %s AND %s
            ORDER BY r.report_date, r.user_id
        """, (start_date, end_date))
        
        if not rows:
            raise Exception("Belirtilen tarih aralığında rapor bulunamadı")
        
        excel_data = []
        for row in rows:
            if len(row) < 11:
                continue
            user_id = safe_get_tuple_value(row, 0, 0)
            tarih = safe_get_tuple_value(row, 1, '')
            rapor_tipi = safe_get_tuple_value(row, 2, '')
            icerik = safe_get_tuple_value(row, 3, '')
            kisi_sayisi = safe_get_tuple_value(row, 4, 0)
            proje_adi = safe_get_tuple_value(row, 5, '')
            is_kategorisi = safe_get_tuple_value(row, 6, '')
            personel_tipi = safe_get_tuple_value(row, 7, '')
            delivered_date = safe_get_tuple_value(row, 8, '')
            is_edited = safe_get_tuple_value(row, 9, False)
            ai_analysis = safe_get_tuple_value(row, 10, '{}')
            
            # PROJE ADINI NORMALİZE ET - EKLENDİ
            proje_adi = normalize_site_name(proje_adi)
            
            # TÜMÜ şantiyesini filtrele
            if proje_adi == "TÜMÜ":
                continue
                
            kullanici_adi = id_to_name.get(user_id, f"Kullanıcı")
            
            try:
                rapor_tarihi = tarih.strftime('%d.%m.%Y') if isinstance(tarih, dt.datetime) else str(tarih)
                gonderme_tarihi = delivered_date.strftime('%d.%m.%Y') if delivered_date and isinstance(delivered_date, dt.datetime) else str(delivered_date) if delivered_date else ""
            except:
                rapor_tarihi = str(tarih)
                gonderme_tarihi = str(delivered_date) if delivered_date else ""
            
            staff_count = 0
            calisan_count = 0
            mobilizasyon_count = 0
            ambarci_count = 0
            izinli_count = 0
            dis_gorev_toplam_count = 0
            
            try:
                ai_data = safe_json_loads(ai_analysis)
                yeni_format = ai_data.get('yeni_sabit_format', {})
                personel_dagilimi = ai_data.get('personel_dagilimi', {})
                
                if yeni_format:
                    staff_count = yeni_format.get('staff', 0)
                    calisan_count = yeni_format.get('calisan', 0)
                    mobilizasyon_count = yeni_format.get('mobilizasyon', 0)
                    ambarci_count = yeni_format.get('ambarci', 0)
                    izinli_count = yeni_format.get('izinli', 0)
                    dis_gorev_toplam_count = yeni_format.get('dis_gorev_toplam', 0)
                elif personel_dagilimi:
                    staff_count = personel_dagilimi.get('staff', 0)
                    calisan_count = personel_dagilimi.get('calisan', 0)
                    mobilizasyon_count = personel_dagilimi.get('mobilizasyon', 0)
                    ambarci_count = personel_dagilimi.get('ambarci', 0)
                    izinli_count = personel_dagilimi.get('izinli', 0)
                    dis_gorev_toplam_count = personel_dagilimi.get('dis_gorev_toplam', 0)
            except:
                pass
            
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
                'Staff': staff_count,
                'Çalışan': calisan_count,
                'Mobilizasyon': mobilizasyon_count,
                'Ambarcı': ambarci_count,
                'İzinli': izinli_count,
                'Dış Görev Toplam': dis_gorev_toplam_count,
                'User ID': user_id
            })
        
        wb = Workbook()
        ws = wb.active
        ws.title = "Raporlar"
        
        headers = ['Tarih', 'Kullanıcı', 'Rapor Tipi', 'Kişi Sayısı', 'Proje', 'İş Kategorisi', 
                  'Personel Tipi', 'Yapılan İş', 'Gönderilme Tarihi', 'Düzenlendi mi?', 
                  'Staff', 'Çalışan', 'Mobilizasyon', 'Ambarcı', 'İzinli', 'Dış Görev Toplam', 'User ID']
        
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
        
        column_widths = {'A': 12, 'B': 20, 'C': 12, 'D': 12, 'E': 20, 'F': 15, 'G': 15, 'H': 40, 'I': 15, 'J': 12, 'K': 8, 'L': 8, 'M': 12, 'N': 8, 'O': 8, 'P': 12, 'Q': 10}
        for col, width in column_widths.items():
            ws.column_dimensions[col].width = width
        
        ws_summary = wb.create_sheet("Özet")
        toplam_rapor = len(excel_data)
        toplam_kullanici = len(set([x['User ID'] for x in excel_data]))
        gun_sayisi = len(set([x['Tarih'] for x in excel_data]))
        
        toplam_staff = sum([x['Staff'] for x in excel_data])
        toplam_calisan = sum([x['Çalışan'] for x in excel_data])
        toplam_mobilizasyon = sum([x['Mobilizasyon'] for x in excel_data])
        toplam_ambarci = sum([x['Ambarcı'] for x in excel_data])
        toplam_izinli = sum([x['İzinli'] for x in excel_data])
        toplam_dis_gorev_toplam = sum([x['Dış Görev Toplam'] for x in excel_data])
        toplam_personel = toplam_staff + toplam_calisan + toplam_mobilizasyon + toplam_ambarci + toplam_izinli + toplam_dis_gorev_toplam
        
        ws_summary.merge_cells('A1:D1')
        ws_summary['A1'] = f"📊 RAPOR ÖZETİ - {rapor_baslik}"
        ws_summary['A1'].font = Font(bold=True, size=14, color="366092")
        ws_summary['A1'].alignment = center_align
        
        summary_data = [
            ['📅 Rapor Periyodu', f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"],
            ['📊 Toplam Rapor', toplam_rapor],
            ['👥 Toplam Kullanıcı', toplam_kullanici],
            ['📅 İş Günü Sayısı', gun_sayisi],
            ['🕒 Oluşturulma', dt.datetime.now(TZ).strftime('%d.%m.%Y %H:%M')],
            ['', ''],
            ['👨‍💼 PERSONEL DAĞILIMI', ''],
            ['• Staff', toplam_staff],
            ['• Çalışan', toplam_calisan],
            ['• Mobilizasyon', toplam_mobilizasyon],
            ['• Ambarcı', toplam_ambarci],
            ['• İzinli', toplam_izinli],
            ['• Dış Görev Toplam', toplam_dis_gorev_toplam],
            ['🎯 TOPLAM PERSONEL', toplam_personel]
        ]
        
        for row_idx, (label, value) in enumerate(summary_data, 3):
            ws_summary[f'A{row_idx}'] = label
            if value != '':
                ws_summary[f'B{row_idx}'] = value
            ws_summary[f'A{row_idx}'].font = Font(bold=True)
            if row_idx >= 9:
                ws_summary[f'A{row_idx}'].font = Font(bold=False)
        
        ws_summary.column_dimensions['A'].width = 25
        ws_summary.column_dimensions['B'].width = 15
        
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        wb.save(temp_file.name)
        return temp_file.name
    except Exception as e:
        raise e

# YENİ: GÜNCELLENMİŞ ZAMANLAMA SİSTEMİ
def schedule_jobs(app):
    jq = app.job_queue
    
    logging.info("⏰ GÜNCELLENMİŞ ZAMANLAMA SİSTEMİ AYARLANIYOR...")
    logging.info(f"🔍 GROUP_ID değeri: {GROUP_ID}")
    
    if not GROUP_ID:
        logging.error("❌ GROUP_ID ayarlanmamış! Hatırlatma mesajları gönderilemeyecek.")
    else:
        logging.info(f"✅ GROUP_ID ayarlandı: {GROUP_ID}")
    
    # Mevcut job'ları ayarla
    jq.run_repeating(auto_watch_excel, interval=60, first=10)
    jq.run_daily(gunluk_rapor_ozeti, time=dt.time(9, 0, tzinfo=TZ))
    
    # YENİ ZAMANLAMALAR
    hatirlatma_job = jq.run_daily(hatirlatma_mesaji, time=dt.time(12, 30, tzinfo=TZ))
    ilk_kontrol_job = jq.run_daily(ilk_rapor_kontrol, time=dt.time(15, 0, tzinfo=TZ))
    son_kontrol_job = jq.run_daily(son_rapor_kontrol, time=dt.time(17, 30, tzinfo=TZ))
    
    # DÜZELTİLDİ: HAFTALIK RAPOR - CUMARTESİ 17:35
    jq.run_daily(haftalik_grup_raporu, time=dt.time(17, 35, tzinfo=TZ), days=(5,))  # 5 = Cumartesi
    
    # YENİ: AYLIK RAPOR - HER AYIN 1'İ 09:30
    jq.run_daily(aylik_grup_raporu_kontrol, time=dt.time(9, 30, tzinfo=TZ))
    
    jq.run_daily(yedekleme_gorevi, time=dt.time(23, 0, tzinfo=TZ))
    jq.run_daily(lambda context: asyncio.create_task(async_yedekle_postgres()), time=dt.time(23, 10, tzinfo=TZ))
    
    logging.info("⏰ Tüm zamanlamalar ayarlandı ✅")

# YENİ: ASYNC POSTGRES YEDEKLEME
async def async_yedekle_postgres():
    """Async Postgres yedekleme"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, yedekle_postgres)

# YENİ: AYLIK RAPOR KONTROL FONKSİYONU
async def aylik_grup_raporu_kontrol(context: ContextTypes.DEFAULT_TYPE):
    """Ayın 1'inde aylık rapor gönder"""
    try:
        today = dt.datetime.now(TZ).date()
        if today.day == 1:  # Ayın 1'inde çalıştır
            # Önceki ayın raporunu oluştur
            start_date = today.replace(day=1) - dt.timedelta(days=1)
            start_date = start_date.replace(day=1)
            end_date = today.replace(day=1) - dt.timedelta(days=1)
            
            await aylik_grup_raporu_tarihli(context, start_date, end_date)
    except Exception as e:
        logging.error(f"🗓️ Aylık rapor kontrol hatası: {e}")

# YENİ: TARİHLİ AYLIK RAPOR
async def aylik_grup_raporu_tarihli(context: ContextTypes.DEFAULT_TYPE, start_date, end_date):
    """Belirli tarih aralığı için aylık rapor gönder"""
    try:
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
                if "Chat not found" not in str(e):
                    logging.error(f"🗓️ {admin_id} adminine aylık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"🗓️ Aylık grup raporu hatası: {e}")

async def auto_watch_excel(context: ContextTypes.DEFAULT_TYPE):
    try:
        load_excel_intelligent()
    except Exception as e:
        logging.error(f"Excel otomatik izleme hatası: {e}")

async def gunluk_rapor_ozeti(context: ContextTypes.DEFAULT_TYPE):
    try:
        dun = (dt.datetime.now(TZ) - dt.timedelta(days=1)).date()
        rapor_mesaji = await generate_gelismis_personel_ozeti(dun)
        
        # DÜZELTİLDİ: Hem Eren Boz'a hem de sana (Super Admin) gönder
        hedef_kullanicilar = [709746899, 1000157326]  # Eren Boz ve Atamurat Kamalov
        
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
        logging.info("12:30 hatırlatma mesajı tetiklendi")
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        if GROUP_ID:
            if not durum['eksik_santiyeler']:
                mesaj = "✅ Bugün için tüm şantiyelerden raporlar alınmış."
            else:
                mesaj = "❌ Eksik raporlar var:\n"
                for santiye in sorted(durum['eksik_santiyeler']):
                    # Şantiye için kullanıcı adlarını al
                    usernames = SANTIYE_USERNAME_MAPPING.get(santiye, [])
                    if usernames:
                        # Kullanıcı adlarını @ ile birleştir
                        username_str = " @" + ", @".join(usernames)
                        mesaj += f"• {santiye} ({username_str} )\n"
                    else:
                        mesaj += f"• {santiye}\n"
            
            # SABİT NOT EKLENİYOR
            mesaj += "\n\n📝 Not: Şantiyenin dili verdiği rapordur; raporu olmayan iş tamamlanmış sayılmaz. ⚠️\nLütfen günlük raporlarınızı zamanında iletiniz."
            
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj)
                logging.info(f"🟡 12:30 hatırlatma mesajı gruba gönderildi: {GROUP_ID}")
            except Exception as e:
                logging.error(f"🟡 Gruba hatırlatma mesajı gönderilemedi: {e}")
        else:
            logging.error("🟡 GROUP_ID ayarlanmamış, hatırlatma mesajı gönderilemedi")
            
    except Exception as e:
        logging.error(f"Hatırlatma mesajı hatası: {e}")

async def ilk_rapor_kontrol(context: ContextTypes.DEFAULT_TYPE):
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        mesaj = "🕒 15:00 Şantiye Rapor Durumu\n\n"
        
        if durum['rapor_veren_santiyeler']:
            mesaj += f"✅ Rapor iletilen şantiyeler ({len(durum['rapor_veren_santiyeler'])}):\n"
            for santiye in sorted(durum['rapor_veren_santiyeler']):
                mesaj += f"• {santiye}\n"
            mesaj += "\n"
        else:
            mesaj += "✅ Rapor iletilen şantiyeler (0):\n\n"
        
        if durum['eksik_santiyeler']:
            mesaj += f"❌ Rapor iletilmeyen şantiyeler ({len(durum['eksik_santiyeler'])}):\n"
            for santiye in sorted(durum['eksik_santiyeler']):
                if santiye in ["Belli değil", "Tümü"]:
                    continue
                # Şantiye için kullanıcı adlarını al ve etiketle
                usernames = SANTIYE_USERNAME_MAPPING.get(santiye, [])
                if usernames:
                    username_str = " @" + ", @".join(usernames)
                    mesaj += f"• {santiye} ({username_str} )\n"
                else:
                    mesaj += f"• {santiye}\n"
        else:
            mesaj += "❌ Rapor iletilmeyen şantiyeler (0):\n"
            mesaj += "🎉 Tüm şantiyeler raporlarını iletti!"
        
        # SABİT NOT EKLENİYOR
        mesaj += "\n\n📝 Not: Yapılan işin raporunu vermek, işi yapmak kadar önemlidir. ⚠️\nEksik olan raporları iletin lütfen."
        
        if GROUP_ID:
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj)
                logging.info(f"🟠 15:00 şantiye kontrol mesajı gruba gönderildi: {GROUP_ID}")
            except Exception as e:
                logging.error(f"🟠 Gruba şantiye kontrol mesajı gönderilemedi: {e}")
        else:
            logging.error("🟠 GROUP_ID ayarlanmamış, şantiye kontrol mesajı gönderilemedi")
        
    except Exception as e:
        logging.error(f"🟠 Şantiye rapor kontrol hatası: {e}")
        await hata_bildirimi(context, f"Şantiye rapor kontrol hatası: {e}")

async def son_rapor_kontrol(context: ContextTypes.DEFAULT_TYPE):
    """🔴 17:30 - Gün sonu şantiye bazlı rapor analizi - GRUBA GÖNDER"""
    try:
        bugun = dt.datetime.now(TZ).date()
        durum = await get_santiye_bazli_rapor_durumu(bugun)
        
        result = await async_fetchone("SELECT COUNT(*) FROM reports WHERE report_date = %s", (bugun,))
        toplam_rapor = result[0] if result else 0
        
        mesaj = "🕠 Gün Sonu Şantiye Rapor Analizi\n\n"
        
        if durum['eksik_santiyeler']:
            mesaj += f"❌ Rapor İletilmeyen Şantiyeler ({len(durum['eksik_santiyeler'])}):\n"
            for santiye in sorted(durum['eksik_santiyeler']):
                # Şantiye için kullanıcı adlarını al ve etiketle
                usernames = SANTIYE_USERNAME_MAPPING.get(santiye, [])
                if usernames:
                    username_str = " @" + ", @".join(usernames)
                    mesaj += f"• {santiye} ({username_str} )\n"
                else:
                    mesaj += f"• {santiye}\n"
        else:
            mesaj += "❌ Rapor İletilmeyen Şantiyeler (0):\n"
            mesaj += "🎉 Tüm şantiyeler raporlarını iletti!\n"
        
        mesaj += f"\n📊 Bugün toplam {toplam_rapor} rapor alındı."
        mesaj += f"\n🏗️ {len(durum['rapor_veren_santiyeler'])}/{len(durum['tum_santiyeler'])} şantiye rapor iletmiş durumda."
        
        # SABİT NOT EKLENİYOR (Kullanıcılar için)
        mesaj += "\n\n📝 Not:\nYapılan işin raporunu vermek, saha yönetiminin en kritik adımıdır. 📊\nBunca çabaya rağmen rapor iletmeyen şantiyeler, lütfen rapor düzenine özen göstersin. 🙏\nUnutmayın: İşi yapmak cesarettir, raporlamak ise disiplindir. ⚠️"
        
        # DÜZELTİLDİ: GRUBA GÖNDER
        if GROUP_ID:
            try:
                await context.bot.send_message(chat_id=GROUP_ID, text=mesaj)
                logging.info(f"🔴 17:30 gün sonu analizi gruba gönderildi: {GROUP_ID}")
            except Exception as e:
                logging.error(f"🔴 Gruba gün sonu analizi gönderilemedi: {e}")
        else:
            logging.error("🔴 GROUP_ID ayarlanmamış, gün sonu analizi gönderilemedi")
        
    except Exception as e:
        logging.error(f"🔴 Şantiye son rapor kontrol hatası: {e}")
        await hata_bildirimi(context, f"Şantiye son rapor kontrol hatası: {e}")

async def haftalik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    try:
        today = dt.datetime.now(TZ).date()
        
        # SON 7 GÜN İÇİN RAPOR HAZIRLA (Bugünden 7 gün geriye)
        end_date = today  # Bugün dahil
        start_date = today - dt.timedelta(days=6)  # 6 gün geri (7 günlük periyot)
        
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
                if "Chat not found" not in str(e):
                    logging.error(f"📊 {admin_id} adminine haftalık rapor gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"📊 Haftalık grup raporu hatası: {e}")
        await hata_bildirimi(context, f"Haftalık grup raporu hatası: {e}")

async def aylik_grup_raporu(context: ContextTypes.DEFAULT_TYPE):
    """Mevcut aylık rapor fonksiyonu - geriye uyumluluk için"""
    try:
        today = dt.datetime.now(TZ).date()
        start_date = today.replace(day=1)
        end_date = today
        
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
                if "Chat not found" not in str(e):
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
                if "Chat not found" not in str(e):
                    logging.error(f"Başlangıç mesajı {admin_id} adminine gönderilemedi: {e}")
        
    except Exception as e:
        logging.error(f"Bot başlatıcı mesaj hatası: {e}")

async def post_init(application: Application):
    commands = [
        BotCommand("start", "Botu başlat"),
        BotCommand("info", "Komut bilgisi"),
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
        BotCommand("reset_database", "Veritabanını sıfırla (Super Admin)"),
        BotCommand("fix_sequences", "Sequence'leri düzelt (Super Admin)"),
    ]
    await application.bot.set_my_commands(commands)
    
    await bot_baslatici_mesaji(application)

def main():
    try:
        logging.info("🚀 Bot başlatılıyor...")
        
        # Önce veritabanı bağlantılarını test et
        init_db_pool()
        init_database()
        
        app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
        
        # Handler'ları ekle
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
        app.add_handler(CommandHandler("reset_database", reset_database_cmd))
        app.add_handler(CommandHandler("fix_sequences", fix_sequences_cmd))
        
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
        logging.info("✅ Tüm handler'lar ve job'lar ayarlandı")
        
        # Railway için webhook yerine polling kullan
        logging.info("🔄 Polling başlatılıyor...")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logging.error(f"❌ Bot başlatma hatası: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    print("🚀 Telegram Bot Başlatılıyor...")
    print("📝 Güncellenmiş Versiyon v4.6.8:")
    print("   - 'Yerel Ekipbaşı' kategorisi staff olarak tanınacak şekilde SYSTEM_PROMPT güncellendi")
    print("   - BWC raporlarındaki 'Toplam Yerel Ekipbaşı' değeri artık staff kategorisine eklenecek")
    print("   - Diğer tüm fonksiyonlar korundu")
    
    main()