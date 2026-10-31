#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


# =============================================================================
# Configuration
# =============================================================================

load_dotenv()

SOURCE_NAME = "haraj"
BASE_URL = "https://haraj.com.sa"
DB_PATH = os.getenv("REAL_ESTATE_DB_PATH", "real_estate.db")

# Persistent Chromium profile: preserves cookies/session without exporting scraped
# data to JSON files. This is browser state only.
PROFILE_DIR = Path(os.getenv("HARAJ_PROFILE_DIR", ".haraj_browser_profile"))

HARAJ_PHONE = os.getenv("HARAJ_PHONE", "").strip()
HARAJ_PASSWORD = os.getenv("HARAJ_PASSWORD", "").strip()

HEADLESS = os.getenv("HARAJ_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "y"}
CYCLE_SLEEP_SECONDS = int(os.getenv("HARAJ_CYCLE_SLEEP_SECONDS", "3600"))

WAIT_AFTER_NAV_MS = int(os.getenv("HARAJ_WAIT_AFTER_NAV_MS", "1800"))
DETAIL_WAIT_MS = int(os.getenv("HARAJ_DETAIL_WAIT_MS", "1200"))
FILTER_WAIT_MS = int(os.getenv("HARAJ_FILTER_WAIT_MS", "1500"))
CONTACT_WAIT_MS = int(os.getenv("HARAJ_CONTACT_WAIT_MS", "5000"))

# EXACT legacy scroll behavior requested by the user.
SCROLL_STEP_PX = int(os.getenv("HARAJ_SCROLL_STEP_PX", "400"))
SCROLL_WAIT_SECONDS = float(os.getenv("HARAJ_SCROLL_WAIT_SECONDS", "0.5"))
MAX_SCROLL_ATTEMPTS = int(os.getenv("HARAJ_MAX_SCROLL_ATTEMPTS", "1500"))
MAX_LOAD_MORE_CLICKS = int(os.getenv("HARAJ_MAX_LOAD_MORE_CLICKS", "300"))

END_OF_RESULTS_TEXTS = [
    "لا يوجد المزيد من العروض",
    "لا يوجد عروض",
]

# Add/edit links here exactly like Aqar: URL + fixed type + fixed purpose.
# The internal Haraj article is NOT allowed to override these two values.
HARAJ_LINKS: List[Dict[str, str]] = [

    {
        "url": "https://haraj.com.sa/tags/%D8%B4%D8%A7%D9%84%D9%8A%D9%87%D8%A7%D8%AA%20%D9%84%D9%84%D8%A8%D9%8A%D8%B9",
        "property_type": "شاليه",
        "purpose": "بيع",
    },
    {
        "url": "https://haraj.com.sa/tags/%D8%A7%D8%B3%D8%AA%D8%B1%D8%A7%D8%AD%D8%A7%D8%AA%20%D9%84%D9%84%D8%A7%D9%8A%D8%AC%D8%A7%D8%B1",
        "property_type": "استراحة",
        "purpose": "إيجار",
    },
    {
        "url": "https://haraj.com.sa/tags/%D8%B4%D9%82%D9%82%20%D9%84%D9%84%D8%A7%D9%8A%D8%AC%D8%A7%D8%B1",
        "property_type": "شقة",
        "purpose": "إيجار",
    },
    {
        "url": "https://haraj.com.sa/tags/%D9%85%D8%AE%D9%8A%D9%85%D8%A7%D8%AA%20%D9%84%D9%84%D8%A7%D9%8A%D8%AC%D8%A7%D8%B1",
        "property_type": "مخيم",
        "purpose": "إيجار",
    }
    # Examples to add later:
    # {"url": "https://haraj.com.sa/tags/...", "property_type": "شاليه", "purpose": "إيجار"},
]

# Haraj filter labels supplied from the current UI. `region` is what gets saved
# to listings.region. Keep them identical unless you intentionally want a DB name
# different from the UI label.
HARAJ_REGIONS: List[Dict[str, str]] = [
    {"label": "الرياض", "region": "الرياض"},
    {"label": "الشرقيه", "region": "الشرقيه"},
    {"label": "جده", "region": "جده"},
    {"label": "مكه", "region": "مكه"},
    {"label": "ينبع", "region": "ينبع"},
    {"label": "حفر الباطن", "region": "حفر الباطن"},
    {"label": "المدينة", "region": "المدينة"},
    {"label": "الطايف", "region": "الطايف"},
    {"label": "تبوك", "region": "تبوك"},
    {"label": "القصيم", "region": "القصيم"},
    {"label": "حائل", "region": "حائل"},
    {"label": "أبها", "region": "أبها"},
    {"label": "عسير", "region": "عسير"},
    {"label": "الباحة", "region": "الباحة"},
    {"label": "جيزان", "region": "جيزان"},
    {"label": "نجران", "region": "نجران"},
    {"label": "الجوف", "region": "الجوف"},
    {"label": "عرعر", "region": "عرعر"},
]

# Outer-card cleaning rules. These are applied BEFORE detail-page visits.
# We intentionally use token/phrase matching rather than a raw substring "بيع"
# so names such as "الربيع" are not accidentally excluded.
SALE_PATTERNS: Sequence[re.Pattern] = (
    re.compile(r"(?<![\u0600-\u06FF])للبيع(?![\u0600-\u06FF])"),
    re.compile(r"(?<![\u0600-\u06FF])بيع(?![\u0600-\u06FF])"),
    re.compile(r"(?<![\u0600-\u06FF])للـبيع(?![\u0600-\u06FF])"),
)

REQUEST_PHRASES = {
    "طلب", "طلبات", "طالب", "طالبة", "اطلب", "أطلب", "نطلب",
    "مطلوب", "مطلوبه", "مطلوبة", "مطلوبين",
    "ابغى", "ابغي", "أبغى", "أبغي", "نبغى", "نبغي",
    "ابي", "أبي", "نبى", "نبي",
    "اريد", "أريد", "نريد",
    "احتاج", "أحتاج", "محتاج", "محتاجه", "محتاجة", "محتاجين",
    "ابحث", "أبحث", "نبحث", "باحث عن", "باحثه عن", "باحثة عن",
    "ادور", "أدور", "ندور",
    "ارغب", "أرغب", "نرغب",
    "ودي", "ودنا",
    "من لديه", "من عنده", "اللي عنده", "الي عنده",
    "احتاج استئجار", "احتاج ايجار", "أحتاج استئجار", "أحتاج ايجار",
    "مطلوب استئجار", "مطلوب ايجار", "مطلوب للإيجار",
    "طلب استئجار", "طلب ايجار",
    "شراء", "للشراء", "لشراء", "اشتري", "أشتري", "نشتري",
    "مطلوب شراء", "ابغى اشتري", "أبغى أشتري", "ابي اشتري", "أبي أشتري",
    "ابي استاجر", "أبي أستأجر", "ابغى استاجر", "أبغى أستأجر",
    "اريد استاجر", "أريد أستأجر", "احتاج استاجر", "أحتاج أستأجر",
    "ودي استاجر", "ودي أستأجر", "نبي نستاجر", "نبغى نستاجر",
    "مطلوب للاستئجار", "مطلوب للايجار", "مطلوب للإيجار",
    "هل يوجد", "هل فيه", "من يوفر", "من يقدر يوفر",
}

ARABIC_DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
ARABIC_LETTER_RE = r"\u0600-\u06FF"

DIGIT_TRANSLATION = str.maketrans({
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})


# =============================================================================
# Small helpers
# =============================================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_space(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"[\t\r\f\v]+", " ", str(value))
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text or None


def ascii_digits(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return str(value).translate(DIGIT_TRANSLATION)


def normalize_arabic_for_match(value: Optional[str]) -> str:
    text = ascii_digits(value) or ""
    text = ARABIC_DIACRITICS_RE.sub("", text)
    text = text.replace("ـ", "")
    # Normalize common alef/yaa variants only for matching, never for saved text.
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    s = ascii_digits(value) or ""
    s = s.replace(",", "").replace("٬", "").replace(" ", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(float(m.group(0)))
    except (ValueError, TypeError):
        return None


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        if not value:
            continue
        v = value.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def absolute_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    return urljoin(BASE_URL, href)


def normalize_saudi_mobile(raw: Optional[str]) -> Optional[str]:
    """Return Saudi mobile as local 05XXXXXXXX when possible."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", ascii_digits(raw) or "")
    if digits.startswith("00966"):
        digits = digits[5:]
    elif digits.startswith("966"):
        digits = digits[3:]
    if digits.startswith("5") and len(digits) == 9:
        return "0" + digits
    if digits.startswith("05") and len(digits) == 10:
        return digits
    return None


def extract_phone_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    normalized = ascii_digits(text) or ""

    # First prioritize phone numbers immediately following contact labels.
    label_patterns = [
        r"رقم\s*جوال\s*مسؤول\s*الإعلان",
        r"رقم\s*جوال\s*مسؤول\s*الاعلان",
        r"رقم\s*الجوال",
        r"رقم\s*جوال",
        r"الجوال",
        r"جوال",
        r"رقم\s*التواصل",
        r"للتواصل",
        r"هاتف",
    ]
    for label in label_patterns:
        m = re.search(
            rf"{label}\s*[:：\-]?\s*(\+?966\s*5(?:[\s\-]?\d){{8}}|05(?:[\s\-]?\d){{8}})",
            normalized,
            flags=re.I,
        )
        if m:
            phone = normalize_saudi_mobile(m.group(1))
            if phone:
                return phone

    # Safe generic fallback: only Saudi mobile formats, not arbitrary 10-digit IDs.
    for m in re.finditer(r"(?:\+?966\s*5(?:[\s\-]?\d){8}|05(?:[\s\-]?\d){8})", normalized):
        phone = normalize_saudi_mobile(m.group(0))
        if phone:
            return phone
    return None


def extract_maps_link(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    patterns = [
        r"https?://(?:www\.)?maps\.google\.[^\s<>\"']+",
        r"https?://maps\.app\.goo\.gl/[^\s<>\"']+",
        r"https?://goo\.gl/maps/[^\s<>\"']+",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(0).rstrip(".,،؛;)")
    return None


def canonical_label(value: str) -> str:
    text = normalize_arabic_for_match(value)
    text = re.sub(r"[^0-9a-z\u0600-\u06FF]+", "", text)
    return text


def first_detail_value(details: Dict[str, Any], aliases: Sequence[str]) -> Optional[str]:
    wanted = {canonical_label(a) for a in aliases}
    for key, value in details.items():
        if canonical_label(key) not in wanted:
            continue
        if isinstance(value, list):
            for item in value:
                if item not in (None, ""):
                    return str(item)
        elif value not in (None, ""):
            return str(value)
    return None


def add_detail(details: Dict[str, Any], key: str, value: str) -> None:
    key = (clean_space(key) or "").strip(" :-–—")
    value = clean_space(value) or ""
    if not key or not value:
        return
    if key not in details:
        details[key] = value
        return
    existing = details[key]
    if isinstance(existing, list):
        if value not in existing:
            existing.append(value)
    elif existing != value:
        details[key] = [existing, value]


# =============================================================================
# Database
# =============================================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_listing_id TEXT,
                property_type TEXT,
                purpose TEXT,
                listing_url TEXT NOT NULL,
                title TEXT,
                price INTEGER,
                price_text TEXT,
                rent_period TEXT,
                area INTEGER,
                area_text TEXT,
                region TEXT,
                city TEXT,
                sub_region TEXT,
                district TEXT,
                street TEXT,
                building_number TEXT,
                postal_code TEXT,
                additional_number TEXT,
                location_link TEXT,
                phone TEXT,
                whatsapp_url TEXT,
                description TEXT,
                details_json TEXT,
                features_json TEXT,
                images_json TEXT,
                videos_json TEXT,
                advertiser_json TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                is_active INTEGER DEFAULT 1,
                user_id TEXT DEFAULT NULL,
                UNIQUE(source, source_listing_id),
                UNIQUE(source, listing_url)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scraper_state (
                source TEXT PRIMARY KEY,
                initial_collection_done INTEGER DEFAULT 0,
                last_cycle_started_at TEXT,
                last_cycle_finished_at TEXT
            )
            """
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO scraper_state
                (source, initial_collection_done, last_cycle_started_at, last_cycle_finished_at)
            VALUES (?, 0, NULL, NULL)
            """,
            (SOURCE_NAME,),
        )

        # Migration for an existing shared database. Old rows automatically get NULL.
        cur.execute("PRAGMA table_info(listings)")
        existing_columns = {row[1] for row in cur.fetchall()}
        columns_to_add = {
            "source_listing_id": "TEXT",
            "property_type": "TEXT",
            "purpose": "TEXT",
            "title": "TEXT",
            "price": "INTEGER",
            "price_text": "TEXT",
            "rent_period": "TEXT",
            "area": "INTEGER",
            "area_text": "TEXT",
            "region": "TEXT",
            "city": "TEXT",
            "sub_region": "TEXT",
            "district": "TEXT",
            "street": "TEXT",
            "building_number": "TEXT",
            "postal_code": "TEXT",
            "additional_number": "TEXT",
            "location_link": "TEXT",
            "phone": "TEXT",
            "whatsapp_url": "TEXT",
            "description": "TEXT",
            "details_json": "TEXT",
            "features_json": "TEXT",
            "images_json": "TEXT",
            "videos_json": "TEXT",
            "advertiser_json": "TEXT",
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "is_active": "INTEGER DEFAULT 1",
            "user_id": "TEXT DEFAULT NULL",
        }
        added = []
        for name, col_type in columns_to_add.items():
            if name not in existing_columns:
                cur.execute(f'ALTER TABLE listings ADD COLUMN "{name}" {col_type}')
                added.append(name)
        conn.commit()
        if "user_id" in added:
            print("✅ تمت إضافة عمود user_id إلى قاعدة البيانات القديمة؛ القيم القديمة NULL.")
    finally:
        conn.close()


def mark_cycle_started() -> None:
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE scraper_state SET last_cycle_started_at = ? WHERE source = ?",
            (now_iso(), SOURCE_NAME),
        )
        conn.commit()
    finally:
        conn.close()


def mark_cycle_finished() -> None:
    conn = db_connect()
    try:
        conn.execute(
            """
            UPDATE scraper_state
               SET initial_collection_done = 1,
                   last_cycle_finished_at = ?
             WHERE source = ?
            """,
            (now_iso(), SOURCE_NAME),
        )
        conn.commit()
    finally:
        conn.close()


def listing_exists(source_listing_id: Optional[str], listing_url: Optional[str]) -> bool:
    conn = db_connect()
    try:
        if source_listing_id:
            row = conn.execute(
                "SELECT 1 FROM listings WHERE source = ? AND source_listing_id = ? LIMIT 1",
                (SOURCE_NAME, str(source_listing_id)),
            ).fetchone()
            if row:
                return True
        if listing_url:
            row = conn.execute(
                "SELECT 1 FROM listings WHERE source = ? AND listing_url = ? LIMIT 1",
                (SOURCE_NAME, listing_url),
            ).fetchone()
            return row is not None
        return False
    finally:
        conn.close()


def touch_listing_seen(source_listing_id: Optional[str], listing_url: Optional[str]) -> None:
    if not source_listing_id and not listing_url:
        return
    conn = db_connect()
    try:
        if source_listing_id:
            conn.execute(
                """
                UPDATE listings
                   SET last_seen_at = ?, is_active = 1
                 WHERE source = ? AND source_listing_id = ?
                """,
                (now_iso(), SOURCE_NAME, str(source_listing_id)),
            )
        elif listing_url:
            conn.execute(
                """
                UPDATE listings
                   SET last_seen_at = ?, is_active = 1
                 WHERE source = ? AND listing_url = ?
                """,
                (now_iso(), SOURCE_NAME, listing_url),
            )
        conn.commit()
    finally:
        conn.close()


def save_listing(data: Dict[str, Any]) -> bool:
    timestamp = now_iso()
    columns = [
        "source", "source_listing_id", "property_type", "purpose", "listing_url",
        "title", "price", "price_text", "rent_period",
        "area", "area_text",
        "region", "city", "sub_region", "district", "street", "building_number",
        "postal_code", "additional_number", "location_link",
        "phone", "whatsapp_url", "description",
        "details_json", "features_json", "images_json", "videos_json", "advertiser_json",
        "first_seen_at", "last_seen_at", "is_active", "user_id",
    ]
    values = [
        SOURCE_NAME,
        data.get("source_listing_id"),
        data.get("property_type"),
        data.get("purpose"),
        data.get("listing_url"),
        data.get("title"),
        data.get("price"),
        data.get("price_text"),
        data.get("rent_period"),
        data.get("area"),
        data.get("area_text"),
        data.get("region"),
        data.get("city"),
        data.get("sub_region"),
        data.get("district"),
        data.get("street"),
        data.get("building_number"),
        data.get("postal_code"),
        data.get("additional_number"),
        data.get("location_link"),
        data.get("phone"),
        data.get("whatsapp_url"),
        data.get("description"),
        json.dumps(data.get("details", {}), ensure_ascii=False),
        json.dumps(data.get("features", []), ensure_ascii=False),
        json.dumps(data.get("images", []), ensure_ascii=False),
        json.dumps(data.get("videos", []), ensure_ascii=False),
        json.dumps(data.get("advertiser", {}), ensure_ascii=False),
        timestamp,
        timestamp,
        1,
        None,  # user_id intentionally NULL for Haraj and for existing sources.
    ]
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT OR IGNORE INTO listings ({','.join(columns)}) VALUES ({placeholders})"

    conn = db_connect()
    try:
        cur = conn.execute(sql, values)
        inserted = cur.rowcount == 1
        conn.commit()
        return inserted
    finally:
        conn.close()


# =============================================================================
# Browser/session/login
# =============================================================================

def create_persistent_context(playwright, headless: bool) -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        locale="ar-SA",
        timezone_id="Asia/Riyadh",
        viewport={"width": 1366, "height": 800},
        args=["--start-maximized"],
    )


def login_button_visible(page: Page) -> bool:
    try:
        loc = page.locator('[data-testid="login-link"]')
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def password_input_visible(page: Page) -> bool:
    try:
        loc = page.locator('[data-testid="auth_password"]')
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def wait_for_password_input(page: Page, timeout_ms: int = 5000) -> bool:
    try:
        page.locator('[data-testid="auth_password"]').wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def perform_login(page: Page) -> bool:
    if not HARAJ_PHONE or not HARAJ_PASSWORD:
        print("❌ ضع HARAJ_PHONE و HARAJ_PASSWORD داخل ملف .env أولًا.")
        return False

    try:
        if not login_button_visible(page):
            return True

        print("🔐 تسجيل الدخول إلى حراج...")
        page.locator('[data-testid="login-link"]').first.click(timeout=10000)

        username = page.locator('[data-testid="auth_username"]')
        username.wait_for(state="visible", timeout=10000)
        username.fill(HARAJ_PHONE)

        # Haraj can show password automatically after entering the phone.
        page.wait_for_timeout(700)
        if not password_input_visible(page):
            next_btn = page.locator('[data-testid="auth_submit_username"]')
            if next_btn.count() and next_btn.first.is_visible():
                next_btn.first.click(timeout=10000)
            if not wait_for_password_input(page, timeout_ms=10000):
                print("❌ لم يظهر حقل كلمة المرور بعد إدخال رقم الهاتف.")
                return False

        password = page.locator('[data-testid="auth_password"]').first
        password.fill(HARAJ_PASSWORD)

        # Try a dedicated password submit if Haraj exposes one, otherwise the
        # visible submit button in the auth form, finally Enter as a fallback.
        submitted = False
        for selector in (
            '[data-testid="auth_submit_password"]',
            'form button[type="submit"]',
        ):
            try:
                candidate = page.locator(selector)
                for i in range(min(candidate.count(), 4)):
                    button = candidate.nth(i)
                    if button.is_visible() and button.is_enabled():
                        button.click(timeout=5000)
                        submitted = True
                        break
                if submitted:
                    break
            except Exception:
                pass
        if not submitted:
            password.press("Enter")

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            if not login_button_visible(page) and not password_input_visible(page):
                print("✅ تم تسجيل الدخول إلى حراج، والجلسة محفوظة في ملف المتصفح المحلي.")
                return True

        print("❌ لم نستطع تأكيد نجاح تسجيل الدخول.")
        return False
    except Exception as exc:
        print(f"❌ خطأ أثناء تسجيل الدخول إلى حراج: {exc}")
        return False


def ensure_logged_in(page: Page) -> bool:
    if not login_button_visible(page):
        return True
    return perform_login(page)


# =============================================================================
# Region filter
# =============================================================================

def region_filter_button(page: Page, current_label: Optional[str] = None) -> Optional[Locator]:
    # The button may keep saying "المنطقة" or may display the currently selected
    # region after selection, so support both states.
    labels = ["المنطقة"]
    if current_label:
        labels.append(current_label)

    for label in labels:
        try:
            exact = re.compile(rf"^\s*{re.escape(label)}\s*$")
            candidates = page.get_by_role("button").filter(has_text=exact)
            for i in range(min(candidates.count(), 8)):
                btn = candidates.nth(i)
                if btn.is_visible():
                    return btn
        except Exception:
            pass

    # Strong fallback for the filter control: it contains a chevron-down icon.
    try:
        candidates = page.locator('button:has(svg[data-icon="chevron-down"])')
        for i in range(min(candidates.count(), 20)):
            btn = candidates.nth(i)
            if not btn.is_visible():
                continue
            text = clean_space(btn.inner_text(timeout=800)) or ""
            if "المنطقة" in text or (current_label and current_label in text):
                return btn
        # If only one visible chevron filter exists, it is a safe last fallback.
        visible = []
        for i in range(min(candidates.count(), 20)):
            btn = candidates.nth(i)
            if btn.is_visible():
                visible.append(btn)
        if len(visible) == 1:
            return visible[0]
    except Exception:
        pass
    return None


def open_region_filter(page: Page, current_label: Optional[str] = None) -> bool:
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    btn = region_filter_button(page, current_label=current_label)
    if btn is None:
        print("❌ لم يتم العثور على زر فلتر المنطقة.")
        return False
    try:
        btn.click(timeout=7000)
        page.wait_for_timeout(350)
        return True
    except Exception as exc:
        print(f"❌ تعذر فتح فلتر المنطقة: {exc}")
        return False


def find_region_option(page: Page, label: str, selected: bool = False) -> Optional[Locator]:
    """Return one region row in the filter.

    Haraj exposes the selection state directly in aria-label:
      - aria-label="select"   -> region is currently NOT selected
      - aria-label="deselect" -> region is currently selected

    Using this state is much safer than inferring selection from CSS classes.
    """
    wanted = re.compile(rf"^\s*{re.escape(label)}\s*$")
    aria_state = "deselect" if selected else "select"
    options = page.locator(f'button[aria-label="{aria_state}"]').filter(has_text=wanted)
    try:
        for i in range(min(options.count(), 8)):
            option = options.nth(i)
            if option.is_visible():
                return option
    except Exception:
        pass
    return None


def region_state_matches(page: Page, label: str, should_be_selected: bool) -> bool:
    """Verify the row's aria state, even if Haraj auto-closes the panel."""
    wanted = re.compile(rf"^\s*{re.escape(label)}\s*$")
    aria_state = "deselect" if should_be_selected else "select"

    # First inspect the DOM regardless of visibility. Some Haraj transitions keep
    # the row mounted for a moment after the panel closes.
    try:
        if page.locator(f'button[aria-label="{aria_state}"]').filter(has_text=wanted).count() > 0:
            return True
    except Exception:
        pass

    # If the panel was auto-closed/unmounted after the click, reopen it only for
    # verification and inspect the visible row.
    if not open_region_filter(page, current_label=label if should_be_selected else None):
        return False
    try:
        return find_region_option(page, label, selected=should_be_selected) is not None
    finally:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            pass


def toggle_region(
    page: Page,
    label: str,
    current_filter_label: Optional[str] = None,
    *,
    deselect: bool = False,
) -> bool:
    """Select or deselect exactly one Haraj region and verify the new state."""
    if not open_region_filter(page, current_label=current_filter_label):
        return False

    # When removing the previous region we MUST target aria-label="deselect".
    # When adding a new region we MUST target aria-label="select".
    option = find_region_option(page, label, selected=deselect)
    expected_aria = "deselect" if deselect else "select"
    if option is None:
        print(f"❌ لم أجد المنطقة داخل الفلتر بالحالة {expected_aria}: {label}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    try:
        option.click(timeout=7000)
        page.wait_for_timeout(FILTER_WAIT_MS)

        # After the click Haraj flips the aria-label:
        # select -> deselect when selected, and deselect -> select when removed.
        should_be_selected = not deselect
        verified = region_state_matches(page, label, should_be_selected=should_be_selected)

        # Always close the filter panel before the next action. This avoids the
        # next call accidentally clicking the filter control while its panel is
        # already open.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass

        if verified:
            action = "تحديد" if should_be_selected else "إلغاء تحديد"
            print(f"✅ تم تأكيد {action} المنطقة: {label}")
            return True

        action = "التحديد" if should_be_selected else "إلغاء التحديد"
        print(f"⚠️ تم الضغط على {label} لكن لم أستطع تأكيد {action} من aria-label.")
        return False
    except Exception as exc:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        print(f"❌ تعذر تغيير تحديد المنطقة {label}: {exc}")
        return False


def select_next_region(page: Page, previous_label: Optional[str], next_label: str) -> bool:
    """Explicitly unselect previous region, then select the next one."""
    if previous_label:
        print(f"◻️ إلغاء تحديد المنطقة السابقة: {previous_label}")
        if not toggle_region(page, previous_label, current_filter_label=previous_label, deselect=True):
            print("⚠️ لم نتأكد من إلغاء التحديد؛ سنعيد تحميل رابط التصنيف لمنع جمع منطقتين معًا.")
            return False
        page.wait_for_timeout(FILTER_WAIT_MS)

    print(f"📍 تحديد المنطقة: {next_label}")
    return toggle_region(page, next_label, current_filter_label=None, deselect=False)


# =============================================================================
# Scrolling / Load More
# =============================================================================

def get_end_of_results_text(page: Page) -> Optional[str]:
    """Return the visible Haraj end-of-results message, if any."""
    for text in END_OF_RESULTS_TEXTS:
        try:
            loc = page.get_by_text(text, exact=True)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return text
        except Exception:
            continue
    return None


def has_end_of_results(page: Page) -> bool:
    return get_end_of_results_text(page) is not None


def visible_load_more_button(page: Page) -> Optional[Locator]:
    try:
        loc = page.locator('button[data-testid="posts-load-more"]')
        for i in range(min(loc.count(), 5)):
            btn = loc.nth(i)
            if btn.is_visible() and btn.is_enabled():
                return btn
    except Exception:
        pass
    return None


def post_card_count(page: Page) -> int:
    try:
        return page.locator('[data-testid="post-item"]').count()
    except Exception:
        return 0


def wait_for_more_posts(page: Page, before_count: int, timeout_seconds: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if has_end_of_results(page):
            return True
        if post_card_count(page) > before_count:
            return True
        time.sleep(0.35)
    return False


def scroll_and_load_all(page: Page) -> Dict[str, Any]:
    """Legacy speed/count + immediate break when Haraj says results are exhausted."""
    print(
        f"⬇️ بدء التمرير: step={SCROLL_STEP_PX}px | wait={SCROLL_WAIT_SECONDS}s | "
        f"max={MAX_SCROLL_ATTEMPTS}"
    )
    load_more_clicks = 0
    reason = "max_scroll_attempts"

    for move_no in range(1, MAX_SCROLL_ATTEMPTS + 1):
        end_text = get_end_of_results_text(page)
        if end_text:
            reason = "end_message"
            print(f"✅ ظهرت «{end_text}» عند خطوة {move_no}; إيقاف التمرير فورًا.")
            break

        page.evaluate("step => window.scrollBy(0, step)", SCROLL_STEP_PX)
        time.sleep(SCROLL_WAIT_SECONDS)

        end_text = get_end_of_results_text(page)
        if end_text:
            reason = "end_message"
            print(f"✅ ظهرت «{end_text}» عند خطوة {move_no}; إيقاف التمرير فورًا.")
            break

        button = visible_load_more_button(page)
        if button is not None and load_more_clicks < MAX_LOAD_MORE_CLICKS:
            before = post_card_count(page)
            try:
                button.scroll_into_view_if_needed(timeout=3000)
                button.click(timeout=5000)
                load_more_clicks += 1
                print(f"➕ مشاهدة المزيد #{load_more_clicks} | الكروت قبل الضغط: {before}")
                wait_for_more_posts(page, before, timeout_seconds=12)
            except Exception as exc:
                print(f"⚠️ تعذر الضغط على مشاهدة المزيد هذه المرة: {exc}")

    print(
        f"✅ انتهاء التحميل | السبب={reason} | "
        f"الكروت={post_card_count(page)} | ضغطات مشاهدة المزيد={load_more_clicks}"
    )
    return {
        "reason": reason,
        "cards": post_card_count(page),
        "load_more_clicks": load_more_clicks,
    }


# =============================================================================
# Outer cards + rejection cleaning
# =============================================================================

def rejection_reason(card_text: Optional[str], title: Optional[str]) -> Optional[str]:
    normalized = normalize_arabic_for_match(f"{title or ''} {card_text or ''}")
    if not normalized:
        return None

    for pattern in SALE_PATTERNS:
        if pattern.search(normalized):
            return "بيع/للبيع"

    # Match request words/phrases after the same Arabic normalization.
    for phrase in REQUEST_PHRASES:
        p = normalize_arabic_for_match(phrase)
        if " " in p:
            if p in normalized:
                return f"طلب:{phrase}"
        else:
            if re.search(rf"(?<![{ARABIC_LETTER_RE}]){re.escape(p)}(?![{ARABIC_LETTER_RE}])", normalized):
                return f"طلب:{phrase}"
    return None


def extract_source_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    path = urlparse(url).path
    m = re.search(r"/(\d{6,})/", path)
    return m.group(1) if m else None


def read_outer_cards(page: Page) -> List[Dict[str, Any]]:
    cards = page.locator('[data-testid="post-item"]')
    results: List[Dict[str, Any]] = []
    seen = set()

    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            card_text = clean_space(card.inner_text(timeout=3000)) or ""
            title_link = card.locator('a[data-testid="post-title-link"]').first
            if title_link.count() == 0:
                continue
            href = title_link.get_attribute("href")
            listing_url = absolute_url(href)
            if not listing_url:
                continue

            title = None
            try:
                title = clean_space(title_link.locator("h3").first.inner_text(timeout=1500))
            except Exception:
                try:
                    title = clean_space(title_link.inner_text(timeout=1500))
                except Exception:
                    pass

            rejected = rejection_reason(card_text, title)
            if rejected:
                print(f"🚫 تجاهل من الكارت الخارجي [{rejected}]: {title or listing_url}")
                continue

            source_listing_id = card.get_attribute("data-test-postid") or extract_source_id_from_url(listing_url)
            author = card.get_attribute("data-test-author") or title_link.get_attribute("data-author")

            card_city = None
            try:
                city_link = card.locator('a[href^="/city/"]').first
                if city_link.count():
                    card_city = clean_space(city_link.inner_text(timeout=1000))
            except Exception:
                pass

            card_image = None
            try:
                img = card.locator("img").first
                if img.count():
                    card_image = img.get_attribute("src")
            except Exception:
                pass

            key = source_listing_id or listing_url
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "source_listing_id": str(source_listing_id) if source_listing_id else None,
                    "listing_url": listing_url,
                    "title": title,
                    "card_city": card_city,
                    "card_author": author,
                    "card_image": card_image,
                    "card_text": card_text,
                }
            )
        except Exception as exc:
            print(f"⚠️ تعذر قراءة كارت رقم {i}: {exc}")

    return results


# =============================================================================
# Detail article parsing / cleaning
# =============================================================================

def parse_article_details(article_text: str) -> Dict[str, Any]:
    """Parse flexible key/value lines; duplicate labels become arrays."""
    details: Dict[str, Any] = {}
    text = ascii_digits(article_text) or ""
    lines = [clean_space(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    # Generic colon-based extraction.
    for line in lines:
        m = re.match(r"^(.{2,80}?)\s*[:：]\s*(.+?)\s*$", line)
        if m:
            add_detail(details, m.group(1), m.group(2))

    # Important Haraj fields can also appear without a colon.
    no_colon_patterns = [
        ("رقم العارض", r"^\s*رقم\s+العارض\s+([0-9]+)\s*$"),
        ("رقم الترخيص", r"^\s*رقم\s+الترخيص\s+([0-9]+)\s*$"),
        ("رقم جوال مسؤول الإعلان", r"^\s*رقم\s+جوال\s+مسؤول\s+(?:الإعلان|الاعلان)\s+(\+?[0-9\s-]+)\s*$"),
    ]
    for line in lines:
        for label, pattern in no_colon_patterns:
            m = re.match(pattern, line, flags=re.I)
            if m:
                add_detail(details, label, m.group(1))

    return details


def extract_labeled_multiline(text: str, aliases: Sequence[str]) -> Optional[str]:
    """Extract text after a label until the next obvious key/value line."""
    if not text:
        return None
    escaped = "|".join(re.escape(a) for a in aliases)
    pattern = re.compile(
        rf"(?:^|\n)\s*(?:{escaped})\s*[:：]\s*(.+?)(?=\n\s*[^\n:]{{2,55}}\s*[:：]|\n\s*رقم\s+(?:العارض|الترخيص)\b|\Z)",
        flags=re.S | re.I,
    )
    m = pattern.search(text)
    return clean_space(m.group(1)) if m else None


def extract_features(details: Dict[str, Any], article_text: str) -> List[str]:
    value = first_detail_value(details, ["خدمات العقار", "الخدمات", "مميزات العقار", "المميزات"])
    if not value:
        m = re.search(r"(?:خدمات\s*العقار|الخدمات|مميزات\s*العقار|المميزات)\s*[:：]\s*([^\n]+)", article_text)
        value = m.group(1).strip() if m else None
    if not value:
        return []
    if normalize_arabic_for_match(value) in {"لا يوجد", "لايوجد", "غير متوفر"}:
        return []
    parts = re.split(r"[,،|؛;]+", value)
    return dedupe_keep_order(clean_space(p) or "" for p in parts)


def infer_rent_period(title: Optional[str], article_text: Optional[str], purpose: str) -> Optional[str]:
    if "ايجار" not in normalize_arabic_for_match(purpose):
        return None
    text = normalize_arabic_for_match(f"{title or ''} {article_text or ''}")
    for label, variants in (
        ("يومي", ("يومي", "باليوم", "ليله", "ليلة")),
        ("شهري", ("شهري", "بالشهر")),
        ("سنوي", ("سنوي", "بالسنه", "بالسنة")),
    ):
        if any(normalize_arabic_for_match(v) in text for v in variants):
            return label
    return None


def extract_images(page: Page) -> List[str]:
    image_entries: List[Tuple[int, str]] = []
    spans = page.locator('span[data-testid^="post-image-"]')
    for i in range(spans.count()):
        span = spans.nth(i)
        testid = span.get_attribute("data-testid") or ""
        m = re.search(r"post-image-(\d+)", testid)
        order = int(m.group(1)) if m else i
        try:
            img = span.locator("img").first
            src = img.get_attribute("src") if img.count() else None
            if src:
                image_entries.append((order, src))
        except Exception:
            pass
    image_entries.sort(key=lambda x: x[0])
    return dedupe_keep_order(url for _, url in image_entries)


def extract_videos(page: Page) -> List[str]:
    urls: List[str] = []
    for selector, attr in (("video[src]", "src"), ("video source[src]", "src"), ("iframe[src]", "src")):
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                url = loc.nth(i).get_attribute(attr)
                if url:
                    urls.append(url)
        except Exception:
            pass
    return dedupe_keep_order(urls)


def close_contact_modal(page: Page) -> None:
    # First try the safest generic dialog close buttons; Escape is the fallback.
    selectors = [
        '[role="dialog"] button[aria-label="إغلاق"]',
        '[role="dialog"] button[aria-label="Close"]',
        '[role="dialog"] button:has-text("إغلاق")',
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                return
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def phone_from_contact_modal(page: Page) -> Optional[str]:
    try:
        button = page.locator('button[data-testid="post-contact"]')
        if not button.count() or not button.first.is_visible():
            return None
        button.first.click(timeout=5000)

        mobile = page.locator('a[data-testid="contact_mobile"]')
        try:
            mobile.wait_for(state="visible", timeout=CONTACT_WAIT_MS)
        except PlaywrightTimeoutError:
            close_contact_modal(page)
            return None

        href = mobile.first.get_attribute("href") or ""
        text = clean_space(mobile.first.inner_text(timeout=1500)) or ""
        phone = normalize_saudi_mobile(href) or normalize_saudi_mobile(text)
        close_contact_modal(page)
        return phone
    except Exception:
        close_contact_modal(page)
        return None


def scrape_detail(context: BrowserContext, card: Dict[str, Any], category: Dict[str, str], region: str) -> Optional[Dict[str, Any]]:
    page = context.new_page()
    try:
        page.goto(card["listing_url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(DETAIL_WAIT_MS)

        # If the persistent session expired during a long run, log in again here.
        if login_button_visible(page):
            if not perform_login(page):
                print("⚠️ الجلسة غير صالحة ولم ينجح تسجيل الدخول؛ سنكمل البيانات المتاحة بدون نافذة التواصل.")
            page.wait_for_timeout(700)

        title = card.get("title")
        try:
            title_loc = page.locator('[data-testid="post_title"]')
            if title_loc.count():
                title = clean_space(title_loc.first.inner_text(timeout=3000)) or title
        except Exception:
            pass

        district_from_header = None
        try:
            neigh = page.locator('[data-testid="post-neighborhood"]')
            if neigh.count():
                district_from_header = clean_space(neigh.first.inner_text(timeout=2000))
        except Exception:
            pass

        author = card.get("card_author")
        author_url = None
        try:
            a = page.locator('[data-testid="post-author"]')
            if a.count():
                author = clean_space(a.first.inner_text(timeout=2000)) or author
                author_url = absolute_url(a.first.get_attribute("href"))
        except Exception:
            pass

        article_text = ""
        article = page.locator('article[data-testid="post-article"]')
        if article.count():
            try:
                article_text = clean_space(article.first.inner_text(timeout=5000)) or ""
            except Exception:
                article_text = ""

        details = parse_article_details(article_text)

        # Region is NEVER taken from article; it is the active filter value.
        city = first_detail_value(details, ["المدينة", "مدينة", "المحافظة", "محافظة"])
        if not city:
            city = card.get("card_city")

        district = first_detail_value(details, ["الحي", "حي", "المنطقة", "المنطقه"])
        if not district:
            district = district_from_header

        price_text = first_detail_value(
            details,
            ["سعر الوحدة", "سعر الوحده", "السعر", "سعر العقار", "قيمة الإيجار", "قيمة الايجار", "الايجار", "الإيجار"],
        )
        price = safe_int(price_text)

        area_text = first_detail_value(
            details,
            ["مساحة العقار", "مساحه العقار", "المساحة", "المساحه", "مساحة الوحدة", "مساحه الوحده"],
        )
        area = safe_int(area_text)

        location_link = first_detail_value(
            details,
            ["موقع العقار على الخريطة", "موقع العقار علي الخريطة", "رابط الموقع", "الموقع على الخريطة"],
        )
        if location_link and not location_link.lower().startswith("http"):
            location_link = None
        if not location_link:
            location_link = extract_maps_link(article_text)

        phone = extract_phone_from_text(article_text)
        if phone:
            print(f"☎️ الهاتف من نص الإعلان: {phone}")
        else:
            phone = phone_from_contact_modal(page)
            if phone:
                print(f"☎️ الهاتف من نافذة التواصل: {phone}")
            else:
                print("ℹ️ لم يتوفر رقم هاتف لهذا الإعلان.")

        images = extract_images(page)
        if not images and card.get("card_image"):
            images = [card["card_image"]]
        videos = extract_videos(page)
        features = extract_features(details, article_text)

        # Keep article as the full description; also expose the location-description
        # fields inside details_json rather than discarding them.
        description = article_text or None

        advertiser = {
            "username": author,
            "profile_url": author_url,
            "license_owner": first_detail_value(details, ["صاحب الترخيص"]),
            "responsible_employee": first_detail_value(details, ["الموظف المسؤول عن الإعلان", "الموظف المسؤول عن الاعلان"]),
            "advertiser_number": first_detail_value(details, ["رقم العارض"]),
        }
        advertiser = {k: v for k, v in advertiser.items() if v not in (None, "")}

        return {
            "source_listing_id": card.get("source_listing_id") or extract_source_id_from_url(card.get("listing_url")),
            "listing_url": card.get("listing_url"),
            "title": title,
            "property_type": category["property_type"],  # fixed by our link config
            "purpose": category["purpose"],              # fixed by our link config
            "price": price,
            "price_text": price_text,
            "rent_period": infer_rent_period(title, article_text, category["purpose"]),
            "area": area,
            "area_text": area_text,
            "region": region,                            # fixed by active filter
            "city": city,
            "sub_region": None,
            "district": district,
            "street": first_detail_value(details, ["الشارع", "اسم الشارع"]),
            "building_number": first_detail_value(details, ["رقم المبنى", "رقم المبني"]),
            "postal_code": first_detail_value(details, ["الرمز البريدي"]),
            "additional_number": first_detail_value(details, ["الرقم الإضافي", "الرقم الاضافي"]),
            "location_link": location_link,
            "phone": phone,
            "whatsapp_url": None,
            "description": description,
            "details": details,
            "features": features,
            "images": images,
            "videos": videos,
            "advertiser": advertiser,
            "user_id": None,
        }
    except Exception as exc:
        print(f"❌ خطأ داخل الإعلان {card.get('listing_url')}: {exc}")
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


# =============================================================================
# Category/region processing
# =============================================================================

def process_loaded_region(
    context: BrowserContext,
    page: Page,
    category: Dict[str, str],
    region: str,
) -> Dict[str, int]:
    cards = read_outer_cards(page)
    stats = {"cards": len(cards), "rejected_or_invalid": 0, "existing": 0, "inserted": 0, "detail_failed": 0}
    print(f"📦 الكروت المقبولة بعد فلترة البيع/الطلبات: {len(cards)}")

    for index, card in enumerate(cards, start=1):
        ad_id = card.get("source_listing_id")
        url = card.get("listing_url")
        title = card.get("title") or url

        if listing_exists(ad_id, url):
            touch_listing_seen(ad_id, url)
            stats["existing"] += 1
            print(f"⏭️ [{index}/{len(cards)}] موجود مسبقًا: {title}")
            continue

        print(f"🔎 [{index}/{len(cards)}] إعلان جديد: {title}")
        data = scrape_detail(context, card, category, region)
        if not data:
            stats["detail_failed"] += 1
            continue

        if save_listing(data):
            stats["inserted"] += 1
            print("✅ تم الحفظ في real_estate.db")
        else:
            stats["existing"] += 1
            touch_listing_seen(data.get("source_listing_id"), data.get("listing_url"))
            print("ℹ️ الإعلان أصبح موجودًا أثناء الدورة؛ لم نكرر الحفظ.")

    return stats


def load_category(page: Page, category: Dict[str, str]) -> bool:
    print("\n" + "=" * 100)
    print(f"🔗 التصنيف: {category['property_type']} | {category['purpose']}")
    print(category["url"])
    try:
        page.goto(category["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(WAIT_AFTER_NAV_MS)
        if not ensure_logged_in(page):
            return False
        return True
    except Exception as exc:
        print(f"❌ تعذر فتح التصنيف: {exc}")
        return False


def scrape_category(context: BrowserContext, category: Dict[str, str], only_region: Optional[str] = None) -> None:
    page = context.new_page()
    previous_label: Optional[str] = None
    try:
        if not load_category(page, category):
            return

        regions = HARAJ_REGIONS
        if only_region:
            normalized_target = normalize_arabic_for_match(only_region)
            regions = [r for r in HARAJ_REGIONS if normalize_arabic_for_match(r["label"]) == normalized_target or normalize_arabic_for_match(r["region"]) == normalized_target]
            if not regions:
                print(f"❌ المنطقة المطلوبة غير موجودة في HARAJ_REGIONS: {only_region}")
                return

        for region_cfg in regions:
            label = region_cfg["label"]
            region_value = region_cfg["region"]

            # Explicitly remove previous selection before selecting the next.
            ok = select_next_region(page, previous_label, label)
            if not ok:
                # Hard reset to the category URL guarantees no accidental multi-region state.
                print("🔄 إعادة تحميل التصنيف وإعادة محاولة المنطقة منفردة...")
                page.goto(category["url"], wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(WAIT_AFTER_NAV_MS)
                ensure_logged_in(page)
                previous_label = None
                if not select_next_region(page, None, label):
                    print(f"⚠️ تخطي المنطقة لعدم القدرة على تحديدها: {label}")
                    continue

            previous_label = label
            print(f"\n{'-' * 90}\n📍 {category['property_type']} / {category['purpose']} / {region_value}\n{'-' * 90}")

            # Let Haraj reset results after the filter, then load all available cards.
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)
            scroll_and_load_all(page)
            stats = process_loaded_region(context, page, category, region_value)
            print(f"📊 ملخص {region_value}: {stats}")

            # Return top before opening the filter for the next toggle.
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

        # User explicitly requested removing the current selection when done.
        if previous_label:
            print(f"◻️ إلغاء تحديد آخر منطقة قبل مغادرة التصنيف: {previous_label}")
            if not toggle_region(page, previous_label, current_filter_label=previous_label, deselect=True):
                print("⚠️ لم نتأكد من إلغاء آخر منطقة، لكن التصنيف التالي سيفتح برابط جديد مستقل.")
    finally:
        try:
            page.close()
        except Exception:
            pass


# =============================================================================
# Cycle / CLI
# =============================================================================

def login_first(context: BrowserContext) -> bool:
    """Login/session check always happens before category/region scraping."""
    page = context.pages[0] if context.pages else context.new_page()
    try:
        # Use first configured category as a normal Haraj page for login/session check.
        start_url = HARAJ_LINKS[0]["url"] if HARAJ_LINKS else BASE_URL
        page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(WAIT_AFTER_NAV_MS)
        if login_button_visible(page):
            return perform_login(page)
        print("✅ جلسة حراج المحفوظة صالحة؛ لا حاجة لتسجيل الدخول من جديد.")
        return True
    except Exception as exc:
        print(f"❌ فشل فحص/تسجيل الدخول الأولي: {exc}")
        return False


def run_once(headless: bool = HEADLESS, only_region: Optional[str] = None, only_category: Optional[int] = None) -> None:
    init_db()
    mark_cycle_started()

    print("\n" + "#" * 100)
    print(f"بدء دورة حراج: {now_iso()}")
    print(f"قاعدة البيانات: {Path(DB_PATH).resolve()}")
    print(f"ملف جلسة المتصفح: {PROFILE_DIR.resolve()}")
    print("#" * 100)

    with sync_playwright() as playwright:
        context = create_persistent_context(playwright, headless=headless)
        try:
            if not login_first(context):
                print("❌ أوقفت الدورة لأن تسجيل الدخول هو الخطوة الأولى المطلوبة.")
                return

            categories = HARAJ_LINKS
            if only_category is not None:
                if only_category < 1 or only_category > len(HARAJ_LINKS):
                    print(f"❌ --only-category يجب أن يكون بين 1 و {len(HARAJ_LINKS)}")
                    return
                categories = [HARAJ_LINKS[only_category - 1]]

            for category in categories:
                scrape_category(context, category, only_region=only_region)

            mark_cycle_finished()
            print("✅ انتهت دورة حراج.")
        finally:
            try:
                context.close()
            except Exception:
                pass


def self_test() -> None:
    # Cleaning tests.
    assert rejection_reason("مخيم فاخر للايجار", "مخيم روك") is None
    assert rejection_reason("فيلا للبيع", "فيلا") is not None
    assert rejection_reason("حي الربيع مخيم للايجار", "مخيم الربيع") is None  # بيع must not match inside الربيع
    assert rejection_reason("مطلوب شقة للايجار", "مطلوب شقة") is not None
    assert rejection_reason("طلب فيلا شقتين بناء شخصي", "طلب فيلا شقتين") is not None
    assert rejection_reason("أبغى استراحة", "ابغى استراحة") is not None
    assert rejection_reason("أبحث عن مخيم", "مخيم") is not None
    assert rejection_reason("ارغب في شراء شاليه", "شراء شاليه") is not None

    sample = """معلومات العقار حسب الرخصة
غرض الإعلان: إيجار
سعر الوحدة: 50000
نوع العقار: فيلا
مساحة العقار: 236.43
خدمات العقار: ألياف ضوئية, صرف صحي, كهرباء, هاتف, مياه
المدينة: المزاحمية
الحي: الواحة
رقم جوال مسؤول الإعلان: 0599111083
موقع العقار على الخريطة:
https://maps.google.com/?q=24.434532955677,46.260528170226
رقم العارض 1100139568
رقم الترخيص 7100326142
"""
    d = parse_article_details(sample)
    assert first_detail_value(d, ["المدينة"]) == "المزاحمية"
    assert first_detail_value(d, ["الحي"]) == "الواحة"
    assert safe_int(first_detail_value(d, ["سعر الوحدة"])) == 50000
    assert safe_int(first_detail_value(d, ["مساحة العقار"])) == 236
    assert extract_phone_from_text(sample) == "0599111083"
    assert extract_maps_link(sample).startswith("https://maps.google.com/")
    feats = extract_features(d, sample)
    assert "كهرباء" in feats and "مياه" in feats
    assert first_detail_value(d, ["رقم العارض"]) == "1100139568"
    assert normalize_saudi_mobile("tel:+966564027444") == "0564027444"

    print("✅ SELF-TEST PASSED")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Haraj -> real_estate.db")
    parser.add_argument("--once", action="store_true", help="تشغيل دورة واحدة فقط")
    parser.add_argument("--headless", action="store_true", help="تشغيل Chromium بدون نافذة")
    parser.add_argument("--only-region", help="اختبار منطقة واحدة فقط، مثال: الرياض")
    parser.add_argument("--only-category", type=int, help="اختبار رابط واحد فقط برقم ترتيبه داخل HARAJ_LINKS (يبدأ من 1)")
    parser.add_argument("--self-test", action="store_true", help="اختبار التنظيف والـRegex بدون فتح الموقع")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_test:
        self_test()
        return

    effective_headless = HEADLESS or args.headless

    if args.once:
        run_once(headless=effective_headless, only_region=args.only_region, only_category=args.only_category)
        return

    while True:
        try:
            run_once(headless=effective_headless, only_region=args.only_region, only_category=args.only_category)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"❌ خطأ غير متوقع في دورة حراج: {exc}")
        print(f"⏳ انتظار {CYCLE_SLEEP_SECONDS} ثانية قبل الدورة التالية...")
        time.sleep(CYCLE_SLEEP_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nتم الإيقاف بواسطة المستخدم.")
        sys.exit(0)
