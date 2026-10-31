import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote, parse_qs, urlsplit, urlunsplit
import os
import argparse
import requests
from dotenv import load_dotenv

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import random

# ============================================================
# الإعدادات
# ============================================================

BASE_URL = "https://sa.aqar.fm"
SOURCE_NAME = "aqar"

DB_PATH = os.getenv("REAL_ESTATE_DB_PATH", "real_estate.db")

HEADLESS = os.getenv("SCRAPER_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "y"}

# مهم:
# في الدورة الأولى لا نوقف التصنيف عند المكرر.
# للروابط العامة لا نتوقف عند أول إعلان قديم؛ نمسح الصفحات كلها ونضيف غير الموجود فقط.
STOP_CATEGORY_ON_FIRST_OLD_AFTER_INITIAL_CYCLE = False

WAIT_AFTER_PAGE_LOAD_MS = 2000
DELAY_BETWEEN_LISTINGS_MIN = 5
DELAY_BETWEEN_LISTINGS_MAX = 10

DELAY_BETWEEN_PAGES_MIN = 15
DELAY_BETWEEN_PAGES_MAX = 25

DELAY_AFTER_EVERY_N_LISTINGS = 20
LONG_BREAK_MIN = 10
LONG_BREAK_MAX = 20
# عند فشل صفحة أو ظهور تحقق بشري
RETRY_SLEEP_SECONDS = 20
MAX_PAGE_RETRIES = 20
MAX_DETAIL_RETRIES = 3


# ============================================================
# الروابط
# عدلها أنت فقط
# ============================================================

AQAR_LINKS = [
    {
        "url": "https://sa.aqar.fm/شاليه-للإيجار",
        "property_type": "شاليه",
        "purpose": "إيجار",
    },
    {
        "url": "https://sa.aqar.fm/استراحة-للإيجار",
        "property_type": "استراحة",
        "purpose": "إيجار",
    },
    {"url": "https://sa.aqar.fm/استوديوهات-للإيجار", "property_type": "استوديو", "purpose": "إيجار"},
    {"url": "https://sa.aqar.fm/شقق-للإيجار", "property_type": "شقة", "purpose": "إيجار"},
]


# ============================================================
# قاعدة البيانات
# ============================================================

def random_sleep(min_seconds, max_seconds, reason=""):
    delay = random.uniform(min_seconds, max_seconds)

    if reason:
        print(f"⏳ انتظار {delay:.1f} ثانية - {reason}")

    time.sleep(delay)
def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db_connect()
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
        INSERT OR IGNORE INTO scraper_state (
            source,
            initial_collection_done,
            last_cycle_started_at,
            last_cycle_finished_at
        )
        VALUES (?, 0, NULL, NULL)
        """,
        (SOURCE_NAME,),
    )

    columns_to_add = {
        "price_text": "TEXT",
        "rent_period": "TEXT",
        "area_text": "TEXT",
        "region": "TEXT",
        "sub_region": "TEXT",
        "postal_code": "TEXT",
        "additional_number": "TEXT",
        "location_link": "TEXT",
        "videos_json": "TEXT",
        "advertiser_json": "TEXT",
    }

    cur.execute("PRAGMA table_info(listings)")
    existing_columns = {row[1] for row in cur.fetchall()}

    for col_name, col_type in columns_to_add.items():
        if col_name not in existing_columns:
            cur.execute(f"ALTER TABLE listings ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()


def is_initial_collection_done():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT initial_collection_done
        FROM scraper_state
        WHERE source = ?
        LIMIT 1
        """,
        (SOURCE_NAME,),
    )

    row = cur.fetchone()
    conn.close()

    return bool(row and row[0] == 1)


def mark_cycle_started():
    now = datetime.now().isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE scraper_state
        SET last_cycle_started_at = ?
        WHERE source = ?
        """,
        (now, SOURCE_NAME),
    )

    conn.commit()
    conn.close()


def mark_initial_collection_done():
    now = datetime.now().isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE scraper_state
        SET initial_collection_done = 1,
            last_cycle_finished_at = ?
        WHERE source = ?
        """,
        (now, SOURCE_NAME),
    )

    conn.commit()
    conn.close()


def mark_cycle_finished():
    now = datetime.now().isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE scraper_state
        SET last_cycle_finished_at = ?
        WHERE source = ?
        """,
        (now, SOURCE_NAME),
    )

    conn.commit()
    conn.close()


def load_existing_ad_ids_before_cycle():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT source_listing_id
        FROM listings
        WHERE source = ?
        AND source_listing_id IS NOT NULL
        """,
        (SOURCE_NAME,),
    )

    ids = {str(row[0]) for row in cur.fetchall() if row[0]}
    conn.close()

    return ids


def listing_exists(ad_id=None, listing_url=None):
    conn = db_connect()
    cur = conn.cursor()

    if ad_id:
        cur.execute(
            """
            SELECT id FROM listings
            WHERE source = ?
            AND source_listing_id = ?
            LIMIT 1
            """,
            (SOURCE_NAME, str(ad_id)),
        )
        row = cur.fetchone()
        if row:
            conn.close()
            return True

    if listing_url:
        cur.execute(
            """
            SELECT id FROM listings
            WHERE source = ?
            AND listing_url = ?
            LIMIT 1
            """,
            (SOURCE_NAME, listing_url),
        )
        row = cur.fetchone()
        conn.close()
        return row is not None

    conn.close()
    return False


def save_listing(data):
    now = datetime.now().isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO listings (
            source,
            source_listing_id,

            property_type,
            purpose,

            listing_url,

            title,
            price,
            price_text,
            rent_period,

            area,
            area_text,

            region,
            city,
            sub_region,
            district,
            street,
            building_number,
            postal_code,
            additional_number,
            location_link,

            phone,
            whatsapp_url,

            description,

            details_json,
            features_json,
            images_json,
            videos_json,
            advertiser_json,

            first_seen_at,
            last_seen_at,
            is_active
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("source"),
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

            now,
            now,
            1,
        ),
    )

    inserted = cur.rowcount == 1
    conn.commit()
    conn.close()
    return inserted


def touch_listing_seen(ad_id=None, listing_url=None):
    """تحديث وقت آخر ظهور لإعلان موجود بدون إعادة فتح صفحة التفاصيل."""
    now = datetime.now().isoformat(timespec="seconds")

    conn = db_connect()
    cur = conn.cursor()

    if ad_id:
        cur.execute(
            """
            UPDATE listings
            SET last_seen_at = ?, is_active = 1
            WHERE source = ? AND source_listing_id = ?
            """,
            (now, SOURCE_NAME, str(ad_id)),
        )
    elif listing_url:
        cur.execute(
            """
            UPDATE listings
            SET last_seen_at = ?, is_active = 1
            WHERE source = ? AND listing_url = ?
            """,
            (now, SOURCE_NAME, listing_url),
        )

    conn.commit()
    conn.close()


# ============================================================
# أدوات تنظيف واستخراج
# ============================================================

def clean_text(text):
    if text is None:
        return None

    text = str(text)
    text = text.replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)

    return text.strip()


def normalize_url(href):
    if not href:
        return None

    href = href.strip()

    if href.startswith("http"):
        return href

    return urljoin(BASE_URL, href)


def extract_ad_id(url):
    if not url:
        return None

    url = unquote(url)

    match = re.search(r"-(\d{5,})/?$", url)
    if match:
        return match.group(1)

    nums = re.findall(r"\d{5,}", url)
    if nums:
        return nums[-1]

    return None


def parse_price(price_text):
    if not price_text:
        return None

    text = str(price_text)
    text = text.replace(",", "")
    text = text.replace("§", "")
    text = text.replace("ر.س", "")
    text = text.replace("ريال", "")
    text = text.replace("SAR", "")

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        return int(float(match.group()))
    except ValueError:
        return None


def parse_area(area_text):
    if not area_text:
        return None

    text = str(area_text).replace(",", "")

    match = re.search(r"(\d+(?:\.\d+)?)\s*(م²|م2|متر|م)", text)
    if not match:
        match = re.search(r"^\s*(\d+(?:\.\d+)?)\s*$", text)

    if not match:
        return None

    try:
        return int(float(match.group(1)))
    except ValueError:
        return None


def extract_rent_period(price_text):
    if not price_text:
        return None

    if "سنوي" in price_text:
        return "سنوي"
    if "شهري" in price_text:
        return "شهري"
    if "يومي" in price_text:
        return "يومي"

    return None


def build_page_url(base_url, page_number):
    """يبني رابط الصفحة مع الحفاظ على query string إن وجد."""
    parts = urlsplit(base_url.strip())
    path = parts.path.rstrip("/")

    if page_number > 1:
        path = f"{path}/{page_number}"

    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def wait_before_retry(reason):
    print("=" * 90)
    print(f"⚠️ سيتم إغلاق المتصفح والانتظار {RETRY_SLEEP_SECONDS} ثانية ثم المحاولة من نفس المكان.")
    print(f"السبب: {reason}")
    print("=" * 90)
    time.sleep(RETRY_SLEEP_SECONDS)


# ============================================================
# كشف صفحة التحقق أو الحظر
# لا نقوم بتجاوزها، فقط نغلق وننتظر ثم نحاول لاحقًا
# ============================================================

def page_has_human_check(page):
    try:
        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass

        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:
            pass

        combined = f"{title}\n{body_text}".lower()

        # لا تستخدم كلمات عامة مثل human أو تحقق فقط
        strong_patterns = [
            "verify you are human",
            "checking your browser",
            "cloudflare",
            "cf-challenge",
            "turnstile",
            "captcha",
            "أنا لست برنامج روبوت",
            "لست روبوت",
            "تحقق من أنك إنسان",
            "تحقق أنك إنسان",
            "يرجى التحقق من أنك إنسان",
            "access denied",
            "too many requests",
        ]

        for pattern in strong_patterns:
            if pattern.lower() in combined:
                return True

        # فحص iframe أو عناصر كابتشا حقيقية
        captcha_selectors = [
            "iframe[src*='captcha']",
            "iframe[src*='challenges']",
            "iframe[src*='turnstile']",
            "div[class*='captcha']",
            "div[id*='captcha']",
            "input[name='cf-turnstile-response']",
            ".cf-turnstile",
        ]

        for selector in captcha_selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                pass

        return False

    except Exception:
        return False
    


def refresh_page_if_no_cards(page, max_refreshes=2):
    for attempt in range(1, max_refreshes + 1):
        print(f"🔄 لا توجد كروت. محاولة تحديث الصفحة {attempt}/{max_refreshes}")

        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS + 1000)
            close_aqar_promo(page)

            html = page.content()
            cards = extract_cards_from_results_html(html)

            if cards:
                print(f"✅ ظهرت الكروت بعد التحديث. العدد: {len(cards)}")
                return cards

            if page_has_human_check(page):
                print("⚠️ بعد التحديث ظهر تحقق بشري فعلي.")
                return "HUMAN_CHECK"

        except Exception as e:
            print(f"⚠️ فشل تحديث الصفحة: {e}")

        time.sleep(2)

    return []


# ============================================================
# استخراج الموقع من رابط الإعلان
# ============================================================

def normalize_location_value(value):
    """اعتبر قيم الموقع الفارغة أو الشرطات كأنها غير موجودة حتى يعمل الـ fallback."""
    value = clean_text(value)
    if not value:
        return None

    if value.strip().lower() in {
        "-", "--", "—", "–", "غير متوفر", "غير محدد", "لا يوجد",
        "none", "null", "n/a", "na"
    }:
        return None

    return value.strip()


def first_location_value(*values):
    for value in values:
        value = normalize_location_value(value)
        if value:
            return value
    return None


def extract_location_from_listing_url(url):
    """
    استخراج الموقع من بنية روابط عقار العامة.

    مثال:
    /شاليه-للإيجار/الطائف/حي-جبرة/شارع-محمد-بن-المحدث-حي-الرحاب-الطائف-الطائف-6824251

    city     = الطائف
    district = جبرة
    street   = محمد بن المحدث

    بعض الروابط قد تحتوي أيضًا على منطقة/محافظة بين المدينة والحي،
    لذلك نتعامل مع الأجزاء الوسطية بالبادئات بدل الاعتماد على رقم ثابت للجزء.
    """
    result = {
        "region": None,
        "city": None,
        "sub_region": None,
        "district": None,
        "street": None,
    }

    if not url:
        return result

    path = unquote(urlparse(url).path)
    parts = [p.strip() for p in path.strip("/").split("/") if p.strip()]

    # parts[0] = نوع العقار/الغرض، parts[1] = المدينة في روابط عقار العامة
    if len(parts) >= 2:
        result["city"] = normalize_location_value(parts[1].replace("-", " "))

    # آخر جزء في رابط الإعلان هو slug الإعلان وينتهي غالبًا بالـ ID،
    # لذلك الأجزاء بين المدينة والـ slug هي hierarchy الموقع.
    middle_parts = parts[2:-1] if len(parts) >= 4 else []

    for raw in middle_parts:
        text = raw.replace("-", " ").strip()

        if re.match(r"^(?:ال)?منطقة\s+", text):
            value = re.sub(r"^(?:ال)?منطقة\s+", "", text).strip()
            result["region"] = normalize_location_value(value)
            continue

        if re.match(r"^محافظة\s+", text):
            value = re.sub(r"^محافظة\s+", "", text).strip()
            result["sub_region"] = normalize_location_value(value)
            continue

        if re.match(r"^حي\s+", text):
            value = re.sub(r"^حي\s+", "", text).strip()
            result["district"] = normalize_location_value(value)
            continue

        # جزء وسطي غير معروف البادئة: نحتفظ به كـ sub_region بدل فقدانه.
        if not result["sub_region"]:
            result["sub_region"] = normalize_location_value(text)

    # استخراج الشارع من slug الأخير نفسه.
    if len(parts) >= 3:
        last = parts[-1]
        last_without_id = re.sub(r"-\d{5,}$", "", last).strip("-")

        # نأخذ ما قبل أول "حي"/"مدينة"/"منطقة" لأن ما بعدها عادة وصف للموقع.
        street_slug = re.split(
            r"-(?:حي|مدينة|منطقة|المنطقة)-",
            last_without_id,
            maxsplit=1,
        )[0]

        # بعض slugs تبدأ بـ حي-شارع-...؛ نحذف البادئات الوصفية فقط.
        street_slug = re.sub(r"^حي-", "", street_slug)
        street_slug = re.sub(r"^شارع-", "", street_slug)
        street = street_slug.replace("-", " ").strip()

        result["street"] = normalize_location_value(street)

    return result


def backfill_missing_locations_from_urls():
    """
    إصلاح السجلات القديمة في قاعدة البيانات من رابط الإعلان نفسه،
    لكن فقط للحقول المفقودة/الشرطات حتى لا نستبدل قيمة صحيحة موجودة.
    """
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, listing_url, region, city, sub_region, district, street
        FROM listings
        WHERE source = ?
        """,
        (SOURCE_NAME,),
    )

    rows = cur.fetchall()
    updated = 0

    for row in rows:
        row_id, listing_url, region, city, sub_region, district, street = row
        parsed = extract_location_from_listing_url(listing_url)

        new_region = first_location_value(region, parsed.get("region"))
        new_city = first_location_value(city, parsed.get("city"))
        new_sub_region = first_location_value(sub_region, parsed.get("sub_region"))
        new_district = first_location_value(district, parsed.get("district"))
        new_street = first_location_value(street, parsed.get("street"))

        current_clean = (
            normalize_location_value(region),
            normalize_location_value(city),
            normalize_location_value(sub_region),
            normalize_location_value(district),
            normalize_location_value(street),
        )
        new_values = (new_region, new_city, new_sub_region, new_district, new_street)

        if new_values != current_clean:
            cur.execute(
                """
                UPDATE listings
                SET region = ?, city = ?, sub_region = ?, district = ?, street = ?
                WHERE id = ?
                """,
                (*new_values, row_id),
            )
            updated += 1

    conn.commit()
    conn.close()

    if updated:
        print(f"✅ تم إصلاح بيانات الموقع من الروابط لعدد {updated} سجل قديم.")

    return updated


# ============================================================
# استخراج كروت صفحة النتائج
# ============================================================

def extract_cards_from_results_html(html):
    soup = BeautifulSoup(html, "html.parser")

    cards = []
    seen = set()

    anchors = soup.select("a.no-underline[href]")

    for a in anchors:
        href = a.get("href")
        full_url = normalize_url(href)
        ad_id = extract_ad_id(full_url)

        if not full_url or not ad_id:
            continue

        if full_url in seen:
            continue

        seen.add(full_url)

        title = None
        price_text = None
        area_text = None
        description = None
        image_url = None

        title_span = a.select_one("p.font-bold span")
        if title_span:
            title = clean_text(title_span.get_text(" ", strip=True))

        price_p = a.select_one("p.text-brand")
        if price_p:
            price_text = clean_text(price_p.get_text(" ", strip=True))

        area_img = a.select_one("img[src*='area.svg']")
        if area_img:
            li = area_img.find_parent("li")
            if li:
                area_text = clean_text(li.get_text(" ", strip=True))

        desc_p = a.select_one("div.hidden.lg\\:block p")
        if desc_p:
            description = clean_text(desc_p.get_text("\n", strip=True))

        img = a.select_one("img[src*='images.aqar.fm']")
        if img:
            image_url = img.get("src")

        url_location = extract_location_from_listing_url(full_url)

        cards.append(
            {
                "listing_url": full_url,
                "source_listing_id": ad_id,
                "title": title,
                "price_text": price_text,
                "price": parse_price(price_text),
                "rent_period": extract_rent_period(price_text),
                "area_text": area_text,
                "area": parse_area(area_text),
                "description": description,
                "images": [image_url] if image_url else [],
                "city": url_location.get("city"),
                "sub_region": url_location.get("sub_region"),
                "district": url_location.get("district"),
                "street": url_location.get("street"),
            }
        )

    return cards


# ============================================================
# صفحة التفاصيل: صور، تفاصيل، مميزات، موقع، معلن
# ============================================================

def get_best_src_from_img(img):
    if not img:
        return None

    srcset = img.get("srcset")
    if srcset:
        candidates = []
        for part in srcset.split(","):
            url = part.strip().split(" ")[0].strip()
            if url:
                candidates.append(url)

        if candidates:
            return candidates[-1]

    return img.get("src")


def extract_images_from_detail_soup(soup):
    images = []
    seen = set()

    container = soup.select_one("div[class*='listingImages']")

    if container:
        imgs = container.select("img[src], img[srcset]")
    else:
        imgs = soup.select("img[src*='images.aqar.fm'], img[srcset*='images.aqar.fm']")

    for img in imgs:
        src = get_best_src_from_img(img)

        if not src:
            continue

        if "images.aqar.fm" not in src:
            continue

        if src not in seen:
            seen.add(src)
            images.append(src)

    return images


def extract_description_from_detail_soup(soup):
    containers = soup.select("div.surface.border.p-4.rounded-md")

    best = None
    best_len = 0

    for container in containers:
        p = container.select_one("p")
        if not p:
            continue

        text = clean_text(p.get_text("\n", strip=True))
        if not text:
            continue

        if len(text) > best_len:
            best = text
            best_len = len(text)

    return best


def find_section_after_h3(soup, h3_text):
    for h3 in soup.find_all("h3"):
        text = clean_text(h3.get_text(" ", strip=True))
        if text and h3_text in text:
            node = h3.find_next_sibling()
            while node:
                if getattr(node, "name", None) == "div":
                    return node
                node = node.find_next_sibling()

    return None


def extract_ad_details_from_detail_soup(soup):
    section = find_section_after_h3(soup, "تفاصيل الإعلان")
    details = {}

    if not section:
        return details

    items = section.select("div.grid.grid-cols-2")

    for item in items:
        ps = item.find_all("p", recursive=False)

        if len(ps) < 2:
            ps = item.find_all("p")

        if len(ps) >= 2:
            key = clean_text(ps[0].get_text(" ", strip=True))
            value = clean_text(ps[1].get_text(" ", strip=True))

            if key and value:
                details[key] = value

    return details


def extract_features_from_detail_soup(soup):
    section = find_section_after_h3(soup, "المميزات")
    features = []

    if not section:
        return features

    for div in section.select("div.flex"):
        text = clean_text(div.get_text(" ", strip=True))

        if not text:
            continue

        if len(text) <= 60 and text not in features:
            features.append(text)

    return features


def extract_videos_from_detail_soup(soup):
    videos = []
    seen = set()

    video_section = None

    for h3 in soup.find_all("h3"):
        text = clean_text(h3.get_text(" ", strip=True))
        if text and "الفيديوهات" in text:
            video_section = h3.find_next_sibling("div")
            break

    if not video_section:
        return videos

    for img in video_section.select("img[src]"):
        src = img.get("src")

        if not src:
            continue

        if "cdn.aqar.fm/thumbnails" not in src:
            continue

        if src not in seen:
            seen.add(src)
            videos.append(
                {
                    "thumbnail": src,
                    "video_url": None,
                }
            )

    return videos


def extract_location_pairs_from_soup(soup):
    wanted = {
        "المنطقة",
        "المدينة",
        "الحي",
        "الشارع",
        "الرمز البريدي",
        "رقم المبنى",
        "الرقم الإضافي",
    }

    result = {}

    items = soup.select("div[class*='tabs-module'][class*='item']")

    for item in items:
        label = item.select_one("span[class*='label']")
        if not label:
            continue

        key = clean_text(label.get_text(" ", strip=True))

        if key not in wanted:
            continue

        spans = item.find_all("span")
        value = None

        for span in spans:
            txt = clean_text(span.get_text(" ", strip=True))
            if txt and txt != key:
                value = txt
                break

        if key and value:
            result[key] = value

    return result


def normalize_location_link(url):
    if not url:
        return None

    url = str(url).strip()

    # إزالة الشرطة المائلة فقط من نهاية رابط الخرائط حتى لا يصبح q=lat,lng/
    while url.endswith("/"):
        url = url[:-1]

    # توحيد روابط Google Maps التي تأتي بهذا الشكل:
    # https://maps.google.com?q=24.862299,46.655121
    # إلى الشكل الذي تريده بدون / في النهاية:
    # https://www.google.com/maps?q=24.862299,46.655121
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    if "q" in qs and qs["q"]:
        q = qs["q"][0].strip().rstrip("/")

        if re.match(r"^-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?$", q):
            q = re.sub(r"\s+", "", q)

            if "google" in parsed.netloc.lower():
                return f"https://www.google.com/maps?q={q}"

    return url


def extract_location_link_from_soup(soup):
    # 1) الأفضل: رابط Google Maps الموجود داخل popup
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue

        href = href.strip()

        if (
            "google.com/maps" in href
            or "maps.google.com" in href
            or "goo.gl/maps" in href
            or "openstreetmap.org" in href
        ):
            return normalize_location_link(href)

    # 2) fallback: لو لا يوجد رابط خرائط، نحفظ صورة البلاطة كما طلبت في نفس العمود
    img = soup.select_one("img.leaflet-tile[src*='tile.openstreetmap.org']")
    if img:
        src = img.get("src")
        if src:
            return normalize_location_link(src)

    # 3) fallback عام لأي صورة يبدو أنها خريطة
    for img in soup.select("img[src]"):
        src = img.get("src")
        if not src:
            continue

        src = src.strip()
        lower_src = src.lower()

        if "map" in lower_src or "maps" in lower_src or "tile.openstreetmap.org" in lower_src:
            return normalize_location_link(src)

    return None


def extract_location_link_by_click(page):
    # قبل الضغط: ربما يكون popup أو الرابط ظاهرًا بالفعل
    selectors = [
        "a[href*='maps.google.com?q=']",
        "a[href*='google.com/maps?q=']",
        "a[href*='openstreetmap.org']",
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                href = loc.first.get_attribute("href")
                if href:
                    return normalize_location_link(href)
        except Exception:
            pass

    # فتح Popup الخريطة بالضغط على دبوس Leaflet
    marker_selectors = [
        ".leaflet-marker-icon",
        ".leaflet-marker-pane .leaflet-interactive",
        "[class*='leaflet-marker-icon']",
    ]

    for marker_selector in marker_selectors:
        try:
            markers = page.locator(marker_selector)
            if markers.count() == 0:
                continue

            markers.first.click(timeout=3000, force=True)
            page.wait_for_timeout(600)

            for selector in selectors:
                loc = page.locator(selector)
                if loc.count() > 0:
                    href = loc.first.get_attribute("href")
                    if href:
                        return normalize_location_link(href)

        except Exception:
            pass

    return None


def extract_advertiser_from_soup(soup):
    result = {}

    adv = soup.select_one("#advertisor")
    if not adv:
        return result

    company_h2 = adv.select_one(".text-center h2")
    if company_h2:
        result["company_name"] = clean_text(company_h2.get_text(" ", strip=True))

    user_link = adv.select_one("a[href*='/user/'] h2")
    if user_link:
        result["agent_name"] = clean_text(user_link.get_text(" ", strip=True))

    profile_link = adv.select_one("a[href*='/user/']")
    if profile_link:
        result["profile_url"] = normalize_url(profile_link.get("href"))

    rating = adv.select_one(".text-sm span")
    if rating:
        result["rating"] = clean_text(rating.get_text(" ", strip=True))

    return result


# ============================================================
# الواتساب
# ============================================================

# ============================================================
# استخراج الهاتف والواتساب / تسجيل دخول عقار / بوت تليجرام
# ============================================================

# نقرأ إعدادات أسرار الدخول من ملف .env الموجود بجانب السكربت.
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")
AQAR_LOGIN_URL = "https://sa.aqar.fm/login"


def env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (ValueError, TypeError):
        return default


def session_file_path():
    configured = os.getenv("AQAR_SESSION_STATE_FILE", ".secrets/aqar_storage_state.json").strip()
    path = Path(configured).expanduser()
    return path if path.is_absolute() else SCRIPT_DIR / path


def has_saved_session():
    path = session_file_path()
    if not path.is_file():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return False
        return bool(state.get("cookies") or state.get("origins"))
    except (ValueError, OSError):
        print("⚠️ ملف الجلسة المحلية غير صالح؛ سنحتاج إلى تسجيل الدخول مجددًا.")
        return False


def save_session_state(context):
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        # إنشاء الملف بصلاحيات خاصة قبل أن يكتب Playwright بيانات الجلسة.
        fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        try:
            context.storage_state(path=str(temp_path), indexed_db=True)
        except TypeError:  # Playwright الأقدم لا يدعم indexed_db
            context.storage_state(path=str(temp_path))
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()


# حالة مؤقتة داخل تشغيل السكربت؛ لا نحاول عرض الأرقام باستمرار عند فرض حد من الموقع.
CONTACT_STATE = {
    "login_retry_after": 0.0,
    "contact_retry_after": 0.0,
}


def login_configuration():
    phone = os.getenv("AQAR_LOGIN_PHONE", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not re.fullmatch(r"5\d{8}", phone):
        raise ValueError("AQAR_LOGIN_PHONE يجب أن يكون 9 أرقام ويبدأ بـ 5، دون 0 أو +966.")
    if not token or token.startswith("PASTE_") or not re.fullmatch(r"-?\d+", chat_id):
        raise ValueError("اضبط TELEGRAM_BOT_TOKEN و TELEGRAM_ALLOWED_CHAT_ID في ملف .env.")
    return phone, token, int(chat_id)


def telegram_api(token, method, payload, timeout=15):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            data=payload,
            timeout=timeout,
        )
        body = response.json()
        if response.status_code != 200 or not body.get("ok"):
            # لا نطبع الرد أو عنوان الطلب لأنهما قد يتضمنان أسرارًا.
            raise RuntimeError(f"Telegram API: {method} failed (HTTP {response.status_code}).")
        return body.get("result")
    except requests.RequestException:
        raise RuntimeError(f"تعذّر الاتصال ببوت تليجرام أثناء {method}.") from None
    except ValueError:
        raise RuntimeError(f"أرسل تليجرام ردًا غير متوقع أثناء {method}.") from None


def telegram_last_update_id(token):
    # offset=-1 يطلب أحدث تحديث بدل إعادة قراءة رسائل قديمة تحمل رموزًا منتهية.
    updates = telegram_api(token, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0}, timeout=10)
    return max((int(item["update_id"]) for item in updates or []), default=-1)


def receive_otp_from_telegram(token, chat_id, after_id, max_wait_seconds):
    deadline = time.monotonic() + max_wait_seconds
    offset = after_id + 1
    while time.monotonic() < deadline:
        poll_seconds = min(10, max(1, int(deadline - time.monotonic())))
        updates = telegram_api(token, "getUpdates", {
            "offset": offset,
            "timeout": poll_seconds,
            "allowed_updates": json.dumps(["message"]),
        }, timeout=poll_seconds + 10)
        for update in updates or []:
            offset = max(offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            if (message.get("chat") or {}).get("id") != chat_id:
                continue
            candidate = str(message.get("text") or "").strip()
            if re.fullmatch(r"\d{4}", candidate):
                return candidate
            # لا نكرر الإشعارات ولا نقبل رموزًا طويلة أو من محادثة غير مصرّح لها.
    return None


def login_screen_ready(page):
    try:
        return (page.get_by_placeholder("5xxxxxxxx").count() > 0
                or page.locator('input[inputmode="numeric"][maxlength="12"]').count() > 0)
    except Exception:
        return False


def perform_aqar_login(context, reason="initial"):
    """تسجيل الدخول بحساب المستخدم نفسه؛ لا يتجاوز التحقق أو حدود العرض."""
    phone, token, chat_id = login_configuration()
    login_page = context.new_page()
    try:
        print("🔐 فتح صفحة تسجيل الدخول إلى عقار." if reason == "initial"
              else "🔐 انتهت الجلسة؛ سنطلب منك رمز دخول جديدًا عبر تليجرام.")
        login_page.goto(AQAR_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        login_page.wait_for_timeout(1000)
        close_aqar_promo(login_page)
        if page_has_human_check(login_page):
            print("⚠️ يتطلب الموقع تحققًا بشريًا؛ لن يحاول السكربت تجاوزه.")
            return False

        print("🔎 البحث عن حقل رقم الجوال...")
        phone_input = login_page.get_by_placeholder("5xxxxxxxx")
        if phone_input.count() == 0:
            phone_input = login_page.locator('input[inputmode="numeric"][maxlength="12"]')
        if phone_input.count() == 0:
            print("⚠️ لم يتم العثور على حقل رقم الجوال في صفحة تسجيل الدخول.")
            return False

        close_aqar_promo(login_page)
        phone_input.first.fill(phone, timeout=10000)
        print("✅ تم إدخال رقم الجوال.")

        close_aqar_promo(login_page)
        continue_button = login_page.get_by_role("button", name="استمرار", exact=True)
        if continue_button.count() == 0:
            print("⚠️ لم يتم العثور على زر استمرار في صفحة تسجيل الدخول.")
            return False
        continue_button.first.click(timeout=10000)
        print("✅ تم الضغط على استمرار؛ انتظار نموذج رمز التحقق...")

        otp_group = login_page.locator('[role="group"][aria-label="رمز التحقق"]')
        otp_group.wait_for(state="visible", timeout=20000)
        close_aqar_promo(login_page)
        print("✅ ظهر نموذج رمز التحقق.")

        # مهم: لا نتصل بتليجرام قبل إدخال الرقم وطلب رمز التحقق من عقار.
        # بهذه الطريقة لا يمنع عطل تليجرام كتابة رقم الجوال أو طلب OTP.
        try:
            last_update = telegram_last_update_id(token)
        except RuntimeError as exc:
            print(f"⚠️ تعذر تجهيز استقبال OTP من تليجرام: {exc}")
            return False

        telegram_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🔐 عقار: أدخل رمز التحقق المكوّن من 4 أرقام الذي وصلك، بالرد على هذه المحادثة. لن أطبع الرمز في سجل التشغيل.",
        })
        max_attempts = env_int("AQAR_OTP_MAX_ATTEMPTS", 2)
        for attempt in range(1, max_attempts + 1):
            otp = receive_otp_from_telegram(
                token, chat_id, last_update,
                env_int("AQAR_OTP_WAIT_SECONDS", 180),
            )
            if not otp:
                print("⚠️ انتهت مهلة وصول رمز التحقق عبر تليجرام.")
                return False
            # نحدّث المؤشر قبل إمكانية طلب رمز ثانٍ؛ لا نعتمد على رمز قديم.
            last_update = telegram_last_update_id(token)
            fields = login_page.locator('[role="group"][aria-label="رمز التحقق"] input')
            if fields.count() != 4:
                print("⚠️ تغيّر نموذج رمز التحقق في عقار.")
                return False
            close_aqar_promo(login_page)
            for index, digit in enumerate(otp):
                close_aqar_promo(login_page)
                fields.nth(index).fill(digit, timeout=5000)
            otp = None
            close_aqar_promo(login_page)
            login_page.get_by_role("button", name="تحقق", exact=True).first.click(timeout=10000)

            # التحقق من انتقال الموقع من /login؛ بقاء النموذج يعني أن الرمز لم يقبل.
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline:
                if urlparse(login_page.url).path.rstrip("/") != "/login":
                    save_session_state(context)
                    if not has_saved_session():
                        print("⚠️ انتقل الموقع من تسجيل الدخول، لكنه لم يحفظ كوكيز/تخزين الجلسة؛ لم نؤكد الاستمرار.")
                        return False
                    CONTACT_STATE["login_retry_after"] = 0.0
                    print("✅ نجح تسجيل الدخول، وحُفظت الجلسة محليًا.")
                    return True
                login_page.wait_for_timeout(400)
            if attempt < max_attempts:
                telegram_api(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "⚠️ لم يؤكد الموقع الدخول بالرمز السابق. أرسل الرمز الصحيح إذا كان ما زال صالحًا.",
                })
        print("⚠️ لم يتم تأكيد تسجيل الدخول؛ سيستمر السحب دون هاتف مؤقتًا.")
        return False
    except (PlaywrightTimeoutError, RuntimeError, ValueError) as exc:
        # ValueError الخاص بإعدادات .env يجب أن يظهر بوضوح لأول تشغيل.
        if isinstance(exc, ValueError):
            raise
        print("⚠️ تعذّر تسجيل الدخول حاليًا؛ سيتم الحفاظ على بقية بيانات العقارات.")
        return False
    except Exception:
        print("⚠️ حدث خطأ غير متوقع أثناء تسجيل الدخول، دون عرض أي بيانات حساسة.")
        return False
    finally:
        try:
            login_page.close()
        except Exception:
            pass


def login_dialog_visible(page):
    try:
        dialog_heading = page.get_by_role("heading", name="الرجاء تسجيل الدخول", exact=True)
        return dialog_heading.count() > 0 and dialog_heading.first.is_visible()
    except Exception:
        return False


def close_blocking_dialog(page):
    """غلق النافذة التي تمنع استكمال صفحة التفاصيل؛ دون النقر على متابعة أو اشتراك."""
    close_aqar_promo(page)
    try:
        # نافذة الإعلان nd-card ليست نافذة اشتراك ولا تسجيل دخول.
        modal = page.locator("div.drop-shadow-2xl:visible, [role='dialog']:not(.nd-card):visible")
        if modal.count() > 0:
            cancel = modal.first.get_by_role("button", name="إلغاء", exact=True)
            if cancel.count() and cancel.first.is_visible():
                cancel.first.click(timeout=1500)
                return
            close_btn = modal.first.locator("div.border-b button, button[aria-label='Close']")
            if close_btn.count() and close_btn.first.is_visible():
                close_btn.first.click(timeout=1500)
                return
        page.keyboard.press("Escape")
    except Exception:
        pass


def restricted_contact_dialog_visible(page):
    try:
        for modal in page.locator("div.drop-shadow-2xl:visible, [role='dialog']:not(.nd-card):visible").all():
            text = modal.inner_text(timeout=1500)
            if any(term in text for term in (
                "يجب الاشتراك", "الاشتراك", "اشترك", "الحد الأقصى", "الحد اليومي",
                "عدد المحاولات", "تجاوزت الحد", "لا يمكنك عرض", "يرجى الترقية",
            )):
                return True
    except Exception:
        pass
    return False


def normalize_saudi_mobile(raw):
    value = re.sub(r"[\s\-()]", "", str(raw or "")).lstrip("+")
    if re.fullmatch(r"9665\d{8}", value):
        return "0" + value[3:]
    if re.fullmatch(r"05\d{8}", value):
        return value
    if re.fullmatch(r"5\d{8}", value):
        return "0" + value
    return None


def extract_phone_from_whatsapp_url(url):
    """اقرأ رقم الهاتف فقط من رابط واتساب موثوق، وليس من نص الرسالة المشفّر."""
    if not url:
        return None
    parsed = urlparse(str(url).replace("&amp;", "&"))
    host = (parsed.hostname or "").lower()
    if host not in {"api.whatsapp.com", "web.whatsapp.com", "wa.me", "www.wa.me"} and parsed.scheme != "whatsapp":
        return None
    params = parse_qs(parsed.query)
    candidate = (params.get("phone") or [None])[0]
    if not candidate and host in {"wa.me", "www.wa.me"}:
        candidate = parsed.path.strip("/").split("/")[0]
    return normalize_saudi_mobile(candidate)


def read_visible_call_phone(page):
    """أرقام فقط من زر اتصال داخل مجموعة إجراءات الإعلان، تجنبًا لأرقام إعلانات أخرى."""
    try:
        groups = page.locator("div.flex.gap-2.mt-4")
        for group in groups.all():
            if not group.is_visible():
                continue
            for button in group.locator("button").all():
                label = button.inner_text(timeout=700).strip()
                number = normalize_saudi_mobile(label)
                if number:
                    return number
    except Exception:
        pass
    return None


def read_existing_whatsapp_url(page):
    try:
        for group in page.locator("div.flex.gap-2.mt-4").all():
            if not group.is_visible():
                continue
            for element in group.locator("a[href]").all():
                href = element.get_attribute("href")
                if extract_phone_from_whatsapp_url(href):
                    return href
    except Exception:
        pass
    return None


def whatsapp_contact_by_click(page):
    """التقاط رابط واتساب بعد النقر فقط؛ لا نفتح التطبيق ولا نرسل رسالة."""
    close_aqar_promo(page)
    href = read_existing_whatsapp_url(page)
    if href:
        phone = extract_phone_from_whatsapp_url(href)
        return {"phone": phone, "whatsapp_url": f"https://wa.me/966{phone[1:]}"}

    button = page.get_by_role("button", name="واتساب", exact=True)
    if button.count() == 0:
        return {"phone": None, "whatsapp_url": None}
    # نعترض فتح واتساب مؤقتًا لالتقاط الرابط؛ تشغيل واتساب الخارجي غير ضروري.
    # إذا استخدم الموقع زرًا برابط <a> عاديًا، نقرأ href قبل الانتقال أيضًا.
    capture_js = r"""() => {
        window.__aqarWAUrl = null;
        const isWhatsApp = (value) => {
            try {
                const x = new URL(String(value), location.href);
                return ['wa.me','www.wa.me','api.whatsapp.com',
                        'web.whatsapp.com'].includes(x.hostname)
                        || x.protocol === 'whatsapp:';
            } catch (e) { return false; }
        };
        window.__aqarOldOpen = window.open;
        window.open = function(url, ...args) {
            if (isWhatsApp(url)) {
                window.__aqarWAUrl = String(url);
                return {focus(){}, close(){}, closed: false,
                        location: {set href(v) {if(isWhatsApp(v))window.__aqarWAUrl=String(v);}}};
            }
            return window.__aqarOldOpen.apply(this, [url, ...args]);
        };
        window.__aqarWAClickCapture = function(evt) {
            const link = evt.target.closest?.('a[href]');
            if (link && isWhatsApp(link.href)) {
                window.__aqarWAUrl = link.href;
                evt.preventDefault();
                evt.stopImmediatePropagation();
            }
        };
        document.addEventListener('click', window.__aqarWAClickCapture, true);
    }"""
    restore_js = """() => {
        if (window.__aqarOldOpen) window.open=window.__aqarOldOpen;
        if (window.__aqarWAClickCapture)
            document.removeEventListener('click',window.__aqarWAClickCapture,true);
        delete window.__aqarOldOpen;
        delete window.__aqarWAClickCapture;
    }"""
    captured = None
    original_url = page.url
    popup_pages = []
    try:
        page.evaluate(capture_js)
        page.on("popup", lambda popup: popup_pages.append(popup))
        close_aqar_promo(page)
        button.first.click(timeout=4000)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            captured = page.evaluate("() => window.__aqarWAUrl")
            if not captured:
                captured = read_existing_whatsapp_url(page)
            if not captured and extract_phone_from_whatsapp_url(page.url):
                captured = page.url
            if not captured:
                for popup in popup_pages:
                    if extract_phone_from_whatsapp_url(popup.url):
                        captured = popup.url
                        break
            if captured or login_dialog_visible(page) or restricted_contact_dialog_visible(page):
                break
            page.wait_for_timeout(200)
        if login_dialog_visible(page):
            return {"phone": None, "whatsapp_url": None, "status": "login_required"}
        if restricted_contact_dialog_visible(page):
            return {"phone": None, "whatsapp_url": None, "status": "restricted"}
        phone = extract_phone_from_whatsapp_url(captured)
        return {"phone": phone, "whatsapp_url": f"https://wa.me/966{phone[1:]}" if phone else None}
    except Exception:
        return {"phone": None, "whatsapp_url": None}
    finally:
        try:
            page.evaluate(restore_js)
        except Exception:
            pass
        for popup in popup_pages:
            try:
                popup.close()
            except Exception:
                pass
        if page.url != original_url and extract_phone_from_whatsapp_url(page.url):
            try:
                page.go_back(wait_until="domcontentloaded", timeout=8000)
            except Exception:
                pass


def read_contact_once(page):
    """أولوية زر اتصال؛ واتساب بديل في حال عدم ظهور رقم اتصال."""
    close_aqar_promo(page)
    result = {"status": "unavailable", "phone": None, "whatsapp_url": None}
    number = read_visible_call_phone(page)
    if number:
        return {**result, "status": "ok", "phone": number}

    group_was_visible = False
    clicked_call = False
    try:
        group_was_visible = page.locator("div.flex.gap-2.mt-4 button").count() > 0
        button = page.get_by_role("button", name="اتصال", exact=True)
        if button.count() > 0:
            close_aqar_promo(page)
            button.first.click(timeout=4000)
            clicked_call = True
            page.wait_for_timeout(450)
    except Exception:
        pass

    if login_dialog_visible(page):
        close_blocking_dialog(page)
        return {**result, "status": "login_required"}
    if restricted_contact_dialog_visible(page):
        close_blocking_dialog(page)
        return {**result, "status": "restricted"}

    # بعض صفحات عقار تحتاج ضغطة أولى لفتح الإجراءات، ثم ضغطة ثانية على اتصال.
    if not clicked_call:
        wa = whatsapp_contact_by_click(page)
        if wa.get("phone"):
            return {**result, "status": "ok", "phone": wa["phone"], "whatsapp_url": wa.get("whatsapp_url")}
        if wa.get("status") in {"restricted", "login_required"}:
            close_blocking_dialog(page)
            return {**result, "status": wa["status"]}
        return result

    if not group_was_visible:
        try:
            call_in_group = page.locator("div.flex.gap-2.mt-4").get_by_role(
                "button", name="اتصال", exact=True,
            )
            if call_in_group.count() and call_in_group.first.is_visible():
                close_aqar_promo(page)
                call_in_group.first.click(timeout=3000)
        except Exception:
            pass

    deadline = time.monotonic() + env_int("AQAR_PHONE_REVEAL_TIMEOUT_MS", 7000, 500) / 1000
    while time.monotonic() < deadline:
        number = read_visible_call_phone(page)
        if number:
            return {**result, "status": "ok", "phone": number}
        if login_dialog_visible(page):
            close_blocking_dialog(page)
            return {**result, "status": "login_required"}
        if restricted_contact_dialog_visible(page):
            close_blocking_dialog(page)
            return {**result, "status": "restricted"}
        page.wait_for_timeout(350)

    # زر الاتصال لم يعرض رقمًا؛ جرّب واتساب مرة واحدة فقط.
    wa = whatsapp_contact_by_click(page)
    if wa.get("status") == "login_required":
        close_blocking_dialog(page)
        return {**result, "status": "login_required"}
    if wa.get("status") == "restricted":
        close_blocking_dialog(page)
        return {**result, "status": "restricted"}
    if wa.get("phone"):
        return {**result, "status": "ok", "phone": wa["phone"], "whatsapp_url": wa.get("whatsapp_url")}
    close_blocking_dialog(page)
    return result


def read_contact_with_auth(context, page, listing_url):
    if time.monotonic() < CONTACT_STATE["contact_retry_after"]:
        return {"phone": None, "whatsapp_url": None}
    try:
        outcome = read_contact_once(page)
        if outcome["status"] == "login_required":
            if time.monotonic() < CONTACT_STATE["login_retry_after"]:
                return {"phone": None, "whatsapp_url": None}
            if perform_aqar_login(context, reason="expired"):
                page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS)
                close_aqar_promo(page)
                outcome = read_contact_once(page)
            else:
                CONTACT_STATE["login_retry_after"] = (
                    time.monotonic() + env_int("AQAR_LOGIN_RETRY_COOLDOWN_SECONDS", 3600)
                )
                return {"phone": None, "whatsapp_url": None}
        if outcome["status"] == "restricted":
            CONTACT_STATE["contact_retry_after"] = (
                time.monotonic() + env_int("AQAR_CONTACT_COOLDOWN_SECONDS", 3600)
            )
            print("⚠️ عقار قيّد عرض بيانات التواصل؛ سنستكمل السحب دون محاولة تجاوز الحد.")
        elif outcome["status"] == "login_required":
            CONTACT_STATE["login_retry_after"] = (
                time.monotonic() + env_int("AQAR_LOGIN_RETRY_COOLDOWN_SECONDS", 3600)
            )
        return {"phone": outcome.get("phone"), "whatsapp_url": outcome.get("whatsapp_url")}
    except Exception:
        print("⚠️ تعذّر استخراج الهاتف لهذا الإعلان؛ سنستكمل بقية بياناته.")
        return {"phone": None, "whatsapp_url": None}


def first_run_login(playwright):
    """على أول تشغيل بدون ملف جلسة، نسجل الدخول مباشرة قبل سحب أي إعلان."""
    if has_saved_session():
        print("🔐 توجد جلسة عقار محفوظة محليًا؛ ستُستخدم دون إرسال رمز جديد الآن.")
        return True
    # إخفاق تهيئة env يجب أن يظهر بوضوح، بدل تشغيل وهمي برقم هاتف غير مضبوط.
    login_configuration()
    if time.monotonic() < CONTACT_STATE["login_retry_after"]:
        return False
    browser, context = create_context(playwright)
    try:
        ok = perform_aqar_login(context, reason="initial")
        if not ok:
            CONTACT_STATE["login_retry_after"] = (
                time.monotonic() + env_int("AQAR_LOGIN_RETRY_COOLDOWN_SECONDS", 3600)
            )
        return ok
    finally:
        safe_close_context_browser(context, browser)


# ============================================================
# إغلاق نافذة عروض عقار الإعلانية تلقائيًا في أي وقت
# ============================================================

# لا نستخدم selector عامًا للحوارات: نغلق فقط nd-card الخاصة بالإعلان
# حتى لا تتأثر نوافذ تسجيل الدخول، رمز التحقق، الاتصال أو الاشتراك.
AQAR_PROMO_CLOSE_SCRIPT = r"""(() => {
    if (window.__aqarPromoDismissInstalled) return;
    window.__aqarPromoDismissInstalled = true;
    const promoSelector = 'div.nd-card[role="dialog"]';
    const closeSelector = 'button.nd-close[data-close][aria-label="إغلاق"]';
    const lastClick = new WeakMap();
    let pending = false;

    function dismiss() {
        for (const card of document.querySelectorAll(promoSelector)) {
            const button = card.querySelector(closeSelector);
            if (!button || !card.isConnected || !button.isConnected) continue;
            const style = window.getComputedStyle(card);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const now = Date.now();
            // امنع حلقة إعادة النقر إذا بقي نفس العنصر لحظة بعد الضغط.
            const sinceLastClick = now - (lastClick.get(button) || 0);
            if (sinceLastClick < 800) {
                // إذا ظهرت النافذة مجددًا فورًا، أغلقها بعد انتهاء مهلة الحماية.
                window.setTimeout(scheduleDismiss, 810 - sinceLastClick);
                continue;
            }
            lastClick.set(button, now);
            button.click();
        }
    }

    function scheduleDismiss() {
        if (pending) return;
        pending = true;
        window.setTimeout(() => {
            pending = false;
            dismiss();
        }, 0);
    }

    // يعمل منذ بدء تحميل المستند، ومع أي نافذة تظهر لاحقًا أثناء OTP أو السحب.
    const observer = new MutationObserver(scheduleDismiss);
    observer.observe(document, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'style', 'aria-hidden'],
    });
    document.addEventListener('DOMContentLoaded', scheduleDismiss, {once: true});
    scheduleDismiss();
})();"""


def close_aqar_promo(page):
    """فحص احتياطي قبل الضغط/الكتابة؛ المراقب داخل المتصفح هو الحماية الأساسية."""
    try:
        cards = page.locator('div.nd-card[role="dialog"]')
        for card in cards.all():
            if not card.is_visible():
                continue
            close_button = card.locator('button.nd-close[data-close][aria-label="إغلاق"]')
            if close_button.count() and close_button.first.is_visible():
                close_button.first.click(timeout=1200)
                return True
    except Exception:
        # فشل إغلاق إعلان ترويجي لا يجب أن يوقف جمع بيانات العقار.
        pass
    return False


# ============================================================
# المتصفح
# ============================================================

def create_context(playwright):
    browser = playwright.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ],
    )

    options = {
        "viewport": {"width": 1366, "height": 768},
        "locale": "ar-SA",
        "timezone_id": "Asia/Riyadh",
    }
    if has_saved_session():
        options["storage_state"] = str(session_file_path())
    context = browser.new_context(**options)
    # تثبيت المراقب قبل إنشاء أي صفحة؛ يعمل أيضًا في التبويبات الجديدة وبعد التنقل.
    context.add_init_script(script=AQAR_PROMO_CLOSE_SCRIPT)

    return browser, context


def safe_close_context_browser(context=None, browser=None):
    try:
        if context:
            context.close()
    except Exception:
        pass

    try:
        if browser:
            browser.close()
    except Exception:
        pass


# ============================================================
# صفحة التفاصيل مع retry
# ============================================================

def scrape_detail_page_once(context, card_data, property_type, purpose):
    detail_page = context.new_page()
    listing_url = card_data["listing_url"]

    try:
        detail_page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)
        detail_page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS)
        close_aqar_promo(detail_page)
        random_sleep(0.8, 1.8, "بعد فتح صفحة الإعلان")

        if page_has_human_check(detail_page):
            return {
                "status": "human_check",
                "data": None,
            }

        whatsapp_data = read_contact_with_auth(context, detail_page, listing_url)
        if whatsapp_data.get("phone"):
            print("✅ تم استخراج رقم التواصل من اتصال أو واتساب.")
        else:
            print("ℹ️ لم يتوفر رقم التواصل؛ سنكمل بقية البيانات.")

        # الضغط على تفاصيل الموقع
        try:
            close_aqar_promo(detail_page)
            detail_page.get_by_text("تفاصيل الموقع", exact=True).click(timeout=3500)
            detail_page.wait_for_timeout(700)
        except Exception:
            pass

        location_link = extract_location_link_by_click(detail_page)

        html = detail_page.content()
        soup = BeautifulSoup(html, "html.parser")

        if not location_link:
            location_link = extract_location_link_from_soup(soup)

        detail_images = extract_images_from_detail_soup(soup)
        description = extract_description_from_detail_soup(soup)
        details = extract_ad_details_from_detail_soup(soup)
        features = extract_features_from_detail_soup(soup)
        videos = extract_videos_from_detail_soup(soup)
        location_pairs = extract_location_pairs_from_soup(soup)
        advertiser = extract_advertiser_from_soup(soup)

        url_location = extract_location_from_listing_url(listing_url)

        # لا نسمح لقيمة مثل "-" القادمة من صفحة التفاصيل أن تمنع fallback الرابط.
        region = first_location_value(
            location_pairs.get("المنطقة"),
            url_location.get("region"),
        )
        city = first_location_value(
            location_pairs.get("المدينة"),
            card_data.get("city"),
            url_location.get("city"),
        )
        district = first_location_value(
            location_pairs.get("الحي"),
            card_data.get("district"),
            url_location.get("district"),
        )
        street = first_location_value(
            location_pairs.get("الشارع"),
            card_data.get("street"),
            url_location.get("street"),
        )

        building_number = normalize_location_value(location_pairs.get("رقم المبنى"))
        postal_code = normalize_location_value(location_pairs.get("الرمز البريدي"))
        additional_number = normalize_location_value(location_pairs.get("الرقم الإضافي"))

        area_text = details.get("المساحة") or card_data.get("area_text")
        area = parse_area(area_text) or card_data.get("area")

        price_text = card_data.get("price_text")
        price = card_data.get("price")

        images = detail_images if detail_images else card_data.get("images", [])

        full_details = {
            **details,
            "location": location_pairs,
        }

        final_data = {
            "source": SOURCE_NAME,
            "source_listing_id": card_data.get("source_listing_id"),

            "property_type": property_type,
            "purpose": purpose,

            "listing_url": listing_url,

            "title": card_data.get("title"),
            "price": price,
            "price_text": price_text,
            "rent_period": card_data.get("rent_period"),

            "area": area,
            "area_text": area_text,

            "region": region,
            "city": city,
            "sub_region": first_location_value(card_data.get("sub_region"), url_location.get("sub_region")),
            "district": district,
            "street": street,
            "building_number": building_number,
            "postal_code": postal_code,
            "additional_number": additional_number,
            "location_link": location_link,

            "phone": whatsapp_data.get("phone"),
            "whatsapp_url": whatsapp_data.get("whatsapp_url"),

            "description": description or card_data.get("description"),

            "details": full_details,
            "features": features,
            "images": images,
            "videos": videos,
            "advertiser": advertiser,
        }

        return {
            "status": "ok",
            "data": final_data,
        }

    except Exception as e:
        print(f"❌ خطأ في صفحة التفاصيل: {listing_url}")
        print(f"السبب: {e}")

        return {
            "status": "error",
            "data": None,
        }

    finally:
        try:
            detail_page.close()
        except Exception:
            pass


def scrape_detail_page_with_retries(context, card_data, property_type, purpose):
    for attempt in range(1, MAX_DETAIL_RETRIES + 1):
        result = scrape_detail_page_once(
            context=context,
            card_data=card_data,
            property_type=property_type,
            purpose=purpose,
        )

        if result["status"] == "ok":
            return result["data"]

        if result["status"] == "human_check":
            print("⚠️ ظهرت صفحة تحقق بشري داخل صفحة الإعلان.")
            return "RETRY_WHOLE_PAGE"

        print(f"⚠️ محاولة تفاصيل الإعلان فشلت: {attempt}/{MAX_DETAIL_RETRIES}")
        time.sleep(2)

    return None


# ============================================================
# صفحة نتائج واحدة
# ============================================================

def scrape_results_page_once(
    playwright,
    page_url,
    property_type,
    purpose,
    initial_cycle,
    existing_ids_before_cycle,
    current_cycle_new_ids,
):
    browser = None
    context = None

    try:
        browser, context = create_context(playwright)
        page = context.new_page()

        print("=" * 90)
        print(f"فتح صفحة النتائج: {page_url}")
        print(f"النوع: {property_type} | الغرض: {purpose}")
        print(f"وضع الدورة: {'الدورة الأولى' if initial_cycle else 'دورة متابعة'}")
        print("=" * 90)

        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS)
        close_aqar_promo(page)

        if page_has_human_check(page):
            print("⚠️ ظهرت صفحة تحقق بشري في صفحة النتائج.")
            return {
                "status": "retry",
                "stop_category": False,
                "reason": "human_check_on_results_page",
            }

        html = page.content()
        cards = extract_cards_from_results_html(html)

        if not cards:
            cards = refresh_page_if_no_cards(page, max_refreshes=2)

            if cards == "HUMAN_CHECK":
                return {
                    "status": "retry",
                    "stop_category": False,
                    "reason": "human_check_after_refresh",
                }

            if not cards:
                print("⚠️ لا توجد كروت بعد التحديث. سيتم اعتبار هذه نهاية التصنيف في هذه الدورة.")
                return {
                    "status": "ok",
                    "stop_category": True,
                    "reason": "no_cards_after_refresh",
                    "page_ad_ids": [],
                }

        page_ad_ids = [
            str(card.get("source_listing_id"))
            for card in cards
            if card.get("source_listing_id")
        ]

        print(f"✅ عدد الكروت المستخرجة من الصفحة: {len(cards)}")

        for index, card in enumerate(cards, start=1):
            ad_id = str(card.get("source_listing_id") or "")
            listing_url = card.get("listing_url")

            print("-" * 90)
            print(f"إعلان {index}/{len(cards)}")
            print(f"ID: {ad_id}")
            print(f"الرابط: {listing_url}")

            exists = listing_exists(ad_id, listing_url)

            if exists:
                # مهم للروابط العامة: لا نتوقف عند إعلان قديم؛ قد توجد إعلانات جديدة بعده.
                touch_listing_seen(ad_id=ad_id, listing_url=listing_url)

                if ad_id in current_cycle_new_ids:
                    print(f"🟡 الإعلان حُفظ في نفس الدورة الحالية، سيتم تجاهله والمتابعة: {ad_id}")
                else:
                    print(f"🟡 إعلان موجود مسبقًا، تم تحديث last_seen_at والمتابعة: {ad_id}")
                continue

            data = scrape_detail_page_with_retries(
                context=context,
                card_data=card,
                property_type=property_type,
                purpose=purpose,
            )

            if data == "RETRY_WHOLE_PAGE":
                return {
                    "status": "retry",
                    "stop_category": False,
                    "reason": "human_check_on_detail_page",
                }

            if data:
                inserted = save_listing(data)

                if inserted:
                    current_cycle_new_ids.add(ad_id)
                    print("✅ تم حفظ إعلان جديد في قاعدة البيانات")
                    print(f"العنوان: {data.get('title')}")
                    print(f"السعر: {data.get('price_text')}")
                    print(f"المساحة: {data.get('area_text')}")
                    print(f"المدينة: {data.get('city')}")
                    print(f"الحي: {data.get('district')}")
                    print(f"الشارع: {data.get('street')}")
                    print(f"رقم المبنى: {data.get('building_number')}")
                    print(f"رابط الموقع: {data.get('location_link')}")
                    print(f"عدد الصور: {len(data.get('images') or [])}")
                    print(f"عدد المميزات: {len(data.get('features') or [])}")
                else:
                    # حماية إضافية في حالة تضارب UNIQUE نادر بين الفحص والحفظ.
                    touch_listing_seen(ad_id=ad_id, listing_url=listing_url)
                    print(f"🟡 لم تتم إضافة صف جديد لأن الإعلان موجود بالفعل: {ad_id}")
            else:
                print("⚠️ لم يتم حفظ الإعلان بسبب فشل صفحة التفاصيل، وسيُعاد اكتشافه في دورة لاحقة.")

            random_sleep(
                DELAY_BETWEEN_LISTINGS_MIN,
                DELAY_BETWEEN_LISTINGS_MAX,
                "بين الإعلانات"
            )

            if index % DELAY_AFTER_EVERY_N_LISTINGS == 0:
                random_sleep(
                    LONG_BREAK_MIN,
                    LONG_BREAK_MAX,
                    f"استراحة بعد {DELAY_AFTER_EVERY_N_LISTINGS} إعلانات"
                )

        return {
            "status": "ok",
            "stop_category": False,
            "reason": "page_done",
            "page_ad_ids": page_ad_ids,
        }

    except Exception as e:
        print(f"❌ خطأ في صفحة النتائج: {page_url}")
        print(f"السبب: {e}")

        return {
            "status": "retry",
            "stop_category": False,
            "reason": str(e),
        }

    finally:
        safe_close_context_browser(context, browser)

def scrape_results_page_with_retries(
    playwright,
    page_url,
    property_type,
    purpose,
    initial_cycle,
    existing_ids_before_cycle,
    current_cycle_new_ids,
):
    retry_count = 0

    while True:
        result = scrape_results_page_once(
            playwright=playwright,
            page_url=page_url,
            property_type=property_type,
            purpose=purpose,
            initial_cycle=initial_cycle,
            existing_ids_before_cycle=existing_ids_before_cycle,
            current_cycle_new_ids=current_cycle_new_ids,
        )

        if result["status"] == "ok":
            return result

        retry_count += 1

        if retry_count > MAX_PAGE_RETRIES:
            print(f"❌ فشلت الصفحة بعد {MAX_PAGE_RETRIES} محاولات. سيتم تجاوزها.")
            return {
                "status": "ok",
                "stop_category": True,
                "reason": "max_retries_exceeded",
            }

        wait_before_retry(result.get("reason", "unknown"))


# ============================================================
# تصنيف كامل
# ============================================================

def scrape_category(
    playwright,
    category,
    initial_cycle,
    existing_ids_before_cycle,
    current_cycle_new_ids,
):
    base_url = category["url"].rstrip("/")
    property_type = category["property_type"]
    purpose = category["purpose"]

    page_number = 1
    seen_page_ad_ids = set()

    while True:
        page_url = build_page_url(base_url, page_number)

        result = scrape_results_page_with_retries(
            playwright=playwright,
            page_url=page_url,
            property_type=property_type,
            purpose=purpose,
            initial_cycle=initial_cycle,
            existing_ids_before_cycle=existing_ids_before_cycle,
            current_cycle_new_ids=current_cycle_new_ids,
        )

        if result.get("stop_category"):
            print(f"✅ انتهى التصنيف: {property_type} - {purpose}")
            print(f"سبب الإنهاء: {result.get('reason')}")
            break

        page_ids = set(result.get("page_ad_ids") or [])

        # حماية من حالة أن الموقع يعيد آخر صفحة أو يعيد توجيه صفحة غير موجودة
        # إلى صفحة سبق أن قرأناها، وهو ما كان سيجعل while True لا تنتهي.
        if page_ids and page_ids.issubset(seen_page_ad_ids):
            print("✅ انتهى التصنيف لأن الصفحة الحالية لا تحتوي أي ID جديد بالنسبة للصفحات السابقة.")
            print("السبب: repeated_pagination_page")
            break

        seen_page_ad_ids.update(page_ids)

        page_number += 1
        random_sleep(
            DELAY_BETWEEN_PAGES_MIN,
            DELAY_BETWEEN_PAGES_MAX,
            "بين الصفحات"
        )


# ============================================================
# تشغيل دورة واحدة
# ============================================================

def run_aqar_once():

    init_db()
    backfill_missing_locations_from_urls()

    initial_cycle = not is_initial_collection_done()

    existing_ids_before_cycle = load_existing_ad_ids_before_cycle()
    current_cycle_new_ids = set()

    mark_cycle_started()

    print("\n" + "#" * 100)
    print(f"بدء دورة عقار: {datetime.now().isoformat(timespec='seconds')}")
    print(f"هل هذه الدورة الأولى؟ {'نعم' if initial_cycle else 'لا'}")
    print(f"عدد الإعلانات الموجودة قبل بداية الدورة: {len(existing_ids_before_cycle)}")
    print("#" * 100)

    with sync_playwright() as playwright:
        # أول تشغيل: تسجيل الدخول أولًا، دون انتظار ظهور مطالبة به في إعلان.
        logged_in = first_run_login(playwright)
        if not logged_in:
            if os.getenv("AQAR_CONTINUE_WHEN_PHONE_UNAVAILABLE", "true").lower() not in {"1", "true", "yes"}:
                print("⚠️ لم ينجح تسجيل الدخول؛ تم إيقاف الدورة دون مسح أي إعلان.")
                return
            print("⚠️ سنستكمل السحب المتاح دون هاتف حتى تصبح الجلسة جاهزة.")
        for category in AQAR_LINKS:
            scrape_category(
                playwright=playwright,
                category=category,
                initial_cycle=initial_cycle,
                existing_ids_before_cycle=existing_ids_before_cycle,
                current_cycle_new_ids=current_cycle_new_ids,
            )

    if initial_cycle:
        mark_initial_collection_done()
        print("✅ اكتملت الدورة الأولى. دورات المتابعة لا تتوقف عند أي إعلان قديم.")

    else:
        mark_cycle_finished()

    print("✅ انتهت دورة عقار.")


# ============================================================
# تشغيل مستمر كل ساعة
# ============================================================

# ============================================================
# تعبئة اختيارية للهواتف المفقودة في إعلانات عقار القديمة
# ============================================================

def backfill_missing_phones(limit=100):
    """ينفذ بطلب المستخدم فقط: لا يعيد إدخال إعلان ولا يغير أعمدته الأخرى."""
    init_db()
    conn = db_connect()
    try:
        rows = conn.execute(
            """SELECT id, listing_url FROM listings
               WHERE source = ? AND (phone IS NULL OR TRIM(phone) IN ('', '-'))
               ORDER BY id ASC LIMIT ?""",
            (SOURCE_NAME, limit),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("✅ لا توجد أرقام قديمة ناقصة ضمن السجلات المطلوبة.")
        return 0
    print(f"ℹ️ سنراجع أرقام {len(rows)} إعلان قديم من عقار دون إضافة إعلانات مكررة.")
    updated = 0
    with sync_playwright() as playwright:
        logged_in = first_run_login(playwright)
        if not logged_in and os.getenv("AQAR_CONTINUE_WHEN_PHONE_UNAVAILABLE", "true").lower() not in {"1", "true", "yes"}:
            return 0
        browser, context = create_context(playwright)
        try:
            for row_id, listing_url in rows:
                if time.monotonic() < CONTACT_STATE["contact_retry_after"]:
                    print("⚠️ تم إيقاف استكمال الهواتف احترامًا لحد عرض الأرقام في الموقع.")
                    break
                page = context.new_page()
                try:
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS)
                    close_aqar_promo(page)
                    if page_has_human_check(page):
                        print("⚠️ تحقق بشري؛ أوقفنا استكمال الأرقام بدل تكرار الطلبات.")
                        break
                    contact = read_contact_with_auth(context, page, listing_url)
                    if contact.get("phone"):
                        conn = db_connect()
                        try:
                            conn.execute(
                                """UPDATE listings SET phone = ?, whatsapp_url = ?
                                   WHERE id = ? AND source = ?
                                     AND (phone IS NULL OR TRIM(phone) IN ('', '-'))""",
                                (contact["phone"], contact.get("whatsapp_url"), row_id, SOURCE_NAME),
                            )
                            if conn.total_changes:
                                updated += 1
                            conn.commit()
                        finally:
                            conn.close()
                        print(f"✅ تم استكمال رقم إعلان قديم (ID المحلي: {row_id}).")
                    else:
                        print(f"ℹ️ رقم الإعلان المحلي {row_id} غير متاح حاليًا.")
                except Exception:
                    print("⚠️ تعذرت مراجعة هذا الإعلان القديم؛ ننتقل للتالي.")
                finally:
                    page.close()
                random_sleep(DELAY_BETWEEN_LISTINGS_MIN, DELAY_BETWEEN_LISTINGS_MAX, "بين الإعلانات القديمة")
        finally:
            safe_close_context_browser(context, browser)
    print(f"✅ انتهت مراجعة الهواتف القديمة؛ تم تحديث {updated} سجل.")
    return updated


def run_forever():
    while True:
        try:
            run_aqar_once()
        except Exception as e:
            print("❌ خطأ عام في دورة عقار:")
            print(e)

        print(f"انتظار ساعة قبل الدورة التالية: {datetime.now().isoformat(timespec='seconds')}")
        time.sleep(3600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="سحب عقار مع تسجيل دخول محلي واستقبال OTP من بوت تليجرام")
    parser.add_argument("--once", action="store_true", help="تشغيل دورة واحدة")
    parser.add_argument("--backfill-phones", action="store_true", help="استكمال أرقام إعلانات عقار القديمة فقط")
    parser.add_argument("--limit", type=int, default=100, help="أقصى عدد سجلات عند استكمال الهواتف")
    args = parser.parse_args()
    if args.backfill_phones:
        backfill_missing_phones(limit=max(1, args.limit))
    elif args.once:
        run_aqar_once()
    else:
        run_forever()