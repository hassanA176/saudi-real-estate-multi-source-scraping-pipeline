import json
import re
import sqlite3
import time
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# الإعدادات
# ============================================================

BASE_URL = "https://www.propertyfinder.sa"
SOURCE_NAME = "property_finder"

DB_PATH = os.getenv("REAL_ESTATE_DB_PATH", "real_estate.db")

HEADLESS = os.getenv("SCRAPER_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "y"}

STOP_CATEGORY_ON_FIRST_OLD_AFTER_INITIAL_CYCLE = False

WAIT_AFTER_PAGE_LOAD_MS = 4000

DELAY_BETWEEN_LISTINGS_MIN = 2.5
DELAY_BETWEEN_LISTINGS_MAX = 5


DELAY_BETWEEN_PAGES_MIN = 5

DELAY_BETWEEN_PAGES_MAX = 10

DELAY_AFTER_EVERY_N_LISTINGS = 20

LONG_BREAK_MIN = 10
LONG_BREAK_MAX = 20

RETRY_SLEEP_SECONDS = 60
MAX_PAGE_RETRIES = 20
MAX_DETAIL_RETRIES = 3


# ============================================================
# الروابط
# عدلها أنت فقط
# ============================================================

PROPERTY_FINDER_LINKS = [
    {
        "url": "https://www.propertyfinder.sa/ar/search?c=2&t=1&fu=0&rp=y&ob=mr",
        "property_type": "شقة",
        "purpose": "إيجار",
    },
    {
        "url": "https://www.propertyfinder.sa/ar/search?c=2&t=20&fu=0&rp=y&ob=mr",
        "property_type": "استراحة",
        "purpose": "إيجار",
    },
]


# ============================================================
# أدوات عامة
# ============================================================

def random_sleep(min_seconds, max_seconds, reason=""):
    delay = random.uniform(min_seconds, max_seconds)

    if reason:
        print(f"⏳ انتظار {delay:.1f} ثانية - {reason}")

    time.sleep(delay)


def db_connect():
    return sqlite3.connect(DB_PATH)


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


def arabic_digits_to_english(text):
    if text is None:
        return None

    mapping = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789"
    )

    return str(text).translate(mapping)


def parse_price(price_text):
    if not price_text:
        return None

    text = arabic_digits_to_english(price_text)
    text = text.replace(",", "")
    text = text.replace("٬", "")
    text = text.replace("ريال", "")
    text = text.replace("SAR", "")
    text = text.replace("ر.س", "")

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

    text = arabic_digits_to_english(area_text)
    text = text.replace(",", "")
    text = text.replace("٬", "")

    match = re.search(r"(\d+(?:\.\d+)?)\s*(متر|م²|م2|م)", text)
    if not match:
        match = re.search(r"^\s*(\d+(?:\.\d+)?)\s*$", text)

    if not match:
        return None

    try:
        return int(float(match.group(1)))
    except ValueError:
        return None


def parse_int(text):
    if not text:
        return None

    text = arabic_digits_to_english(text)
    text = text.replace(",", "").replace("٬", "")

    match = re.search(r"\d+", text)
    if not match:
        return None

    try:
        return int(match.group())
    except ValueError:
        return None


def extract_ad_id(url_or_text):
    if not url_or_text:
        return None

    text = str(url_or_text)

    # من الرابط:
    # ...-51504424.html
    match = re.search(r"-(\d{5,})\.html", text)
    if match:
        return match.group(1)

    # من data-testid/list-item
    match = re.search(r"list-item-(\d{5,})", text)
    if match:
        return match.group(1)

    nums = re.findall(r"\d{5,}", text)
    if nums:
        return nums[-1]

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


def split_location_text(location_text):
    result = {
        "district": None,
        "city": None,
        "region": None,
    }

    if not location_text:
        return result

    parts = [clean_text(p) for p in location_text.split(",") if clean_text(p)]

    if len(parts) >= 1:
        result["district"] = parts[0]

    if len(parts) >= 2:
        result["city"] = parts[1]

    if len(parts) >= 3:
        result["region"] = parts[2]

    return result


def build_page_url(base_url, page_number):
    """
    Property Finder يستخدم query parameter:
    page=2
    """
    if page_number == 1:
        return base_url

    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)

    query["page"] = [str(page_number)]

    new_query = urlencode(query, doseq=True)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def wait_before_retry(reason):
    print("=" * 90)
    print(f"⚠️ سيتم إغلاق المتصفح والانتظار {RETRY_SLEEP_SECONDS} ثانية ثم المحاولة من نفس المكان.")
    print(f"السبب: {reason}")
    print("=" * 90)
    time.sleep(RETRY_SLEEP_SECONDS)


# ============================================================
# قاعدة البيانات
# ============================================================

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


def touch_listing_seen(ad_id=None, listing_url=None):
    """
    يحدث last_seen_at للإعلان الموجود بدون إنشاء سجل جديد.
    يستخدم فقط في دورات المتابعة عندما نرى إعلانًا محفوظًا مسبقًا.
    """
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

    conn.commit()
    conn.close()


# ============================================================
# كشف التحقق أو الحظر
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

        strong_patterns = [
            "verify you are human",
            "checking your browser",
            "access denied",
            "too many requests",
            "أنا لست برنامج روبوت",
            "لست روبوت",
            "تحقق من أنك إنسان",
            "تحقق أنك إنسان",
            "يرجى التحقق من أنك إنسان",
        ]

        for pattern in strong_patterns:
            if pattern.lower() in combined:
                print(f"⚠️ تم اعتبار الصفحة تحقق بشري بسبب النص: {pattern}")
                print("عنوان الصفحة:", title)
                print("أول 500 حرف من الصفحة:")
                print(body_text[:500])
                return True

        # في Property Finder لا نعتبر iframe captcha وحده تحققًا
        # لأن الموقع يضعه أحيانًا داخل الصفحة حتى وهي تعمل طبيعيًا.
        real_block_selectors = [
            "input[name='cf-turnstile-response']",
            ".cf-turnstile",
            "div[id*='challenge']",
            "div[class*='challenge']",
        ]

        for selector in real_block_selectors:
            try:
                count = page.locator(selector).count()
                if count > 0:
                    print(f"⚠️ تم اعتبار الصفحة تحقق بشري بسبب selector قوي: {selector}")
                    print(f"عدد العناصر: {count}")
                    print("عنوان الصفحة:", title)
                    return True
            except Exception:
                pass

        return False

    except Exception as e:
        print(f"⚠️ خطأ أثناء فحص التحقق البشري: {e}")
        return False


# ============================================================
# استخراج كروت Property Finder
# ============================================================

def extract_images_from_card(article):
    images = []
    seen = set()

    for img in article.select("img[data-testid='gallery-picture'][src]"):
        src = img.get("src")

        if not src:
            continue

        if "placeholder" in src:
            continue

        if src not in seen:
            seen.add(src)
            images.append(src)

    return images


def extract_cards_from_results_html(html):
    soup = BeautifulSoup(html, "html.parser")

    cards = []
    seen = set()

    articles = soup.select("article[data-testid='property-card']")

    for article in articles:
        link_tag = article.select_one("a[data-testid='property-card-link'][href]")

        if not link_tag:
            link_tag = article.select_one("a[href*='.html'][href]")

        if not link_tag:
            continue

        listing_url = normalize_url(link_tag.get("href"))
        ad_id = extract_ad_id(listing_url)

        if not listing_url or not ad_id:
            continue

        if ad_id in seen:
            continue

        seen.add(ad_id)

        title = None
        title_tag = article.select_one("h3[id^='plp-']")
        if title_tag:
            title = clean_text(title_tag.get_text(" ", strip=True))

        card_property_type = None
        type_tag = article.select_one("[data-testid='property-card-type']")
        if type_tag:
            card_property_type = clean_text(type_tag.get_text(" ", strip=True))

        price_text = None
        price_tag = article.select_one("[data-testid='property-card-price']")
        if price_tag:
            price_text = clean_text(price_tag.get_text(" ", strip=True))

        location_text = None
        location_tag = article.select_one("[data-testid='property-card-location']")
        if location_tag:
            location_text = clean_text(location_tag.get_text(" ", strip=True))

        location_parts = split_location_text(location_text)

        bedrooms_text = None
        bedrooms_tag = article.select_one("[data-testid='property-card-spec-bedroom']")
        if bedrooms_tag:
            bedrooms_text = clean_text(bedrooms_tag.get_text(" ", strip=True))

        bathrooms_text = None
        bathrooms_tag = article.select_one("[data-testid='property-card-spec-bathroom']")
        if bathrooms_tag:
            bathrooms_text = clean_text(bathrooms_tag.get_text(" ", strip=True))

        area_text = None
        area_tag = article.select_one("[data-testid='property-card-spec-area']")
        if area_tag:
            area_text = clean_text(area_tag.get_text(" ", strip=True))

        price_per_area_text = None
        ppa_tag = article.select_one("[data-testid='property-card-spec-price-per-area']")
        if ppa_tag:
            price_per_area_text = clean_text(ppa_tag.get_text(" ", strip=True))

        published_info = None
        published_tag = article.select_one("p[class*='publish-info']")
        if published_tag:
            published_info = clean_text(published_tag.get_text(" ", strip=True))

        image_count = None
        image_count_tag = article.select_one("span[class*='image-count']")
        if image_count_tag:
            image_count = parse_int(image_count_tag.get_text(" ", strip=True))

        broker_logo = None
        broker_logo_tag = article.select_one("[data-testid='property-card-broker-logo'] img[src]")
        if broker_logo_tag:
            broker_logo = broker_logo_tag.get("src")

        images = extract_images_from_card(article)

        card_details = {
            "card_property_type": card_property_type,
            "location_text": location_text,
            "bedrooms": parse_int(bedrooms_text),
            "bedrooms_text": bedrooms_text,
            "bathrooms": parse_int(bathrooms_text),
            "bathrooms_text": bathrooms_text,
            "price_per_area_text": price_per_area_text,
            "published_info": published_info,
            "image_count": image_count,
            "broker_logo": broker_logo,
        }

        cards.append(
            {
                "source_listing_id": ad_id,
                "listing_url": listing_url,
                "title": title,

                "price_text": price_text,
                "price": parse_price(price_text),
                "rent_period": extract_rent_period(price_text),

                "area_text": area_text,
                "area": parse_area(area_text),

                "region": location_parts.get("region"),
                "city": location_parts.get("city"),
                "district": location_parts.get("district"),
                "street": None,

                "description": None,
                "images": images,

                "details": card_details,
            }
        )

    return cards


def refresh_page_if_no_cards(page, max_refreshes=2):
    for attempt in range(1, max_refreshes + 1):
        print(f"🔄 لا توجد كروت. محاولة تحديث الصفحة {attempt}/{max_refreshes}")

        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS + 2000)

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

        time.sleep(3)

    return []


# ============================================================
# استخراج رابط الموقع من JSON-LD
# ============================================================

def normalize_location_link(url):
    if not url:
        return None

    url = str(url).strip()

    # لا نريد الشرطة المائلة في النهاية حتى لا يصير الرابط:
    # https://www.google.com/maps?q=lat,lng/
    while url.endswith("/"):
        url = url[:-1]

    return url or None


def make_google_maps_link(latitude, longitude):
    if latitude is None or longitude is None:
        return None

    lat = str(latitude).strip()
    lng = str(longitude).strip()

    if not lat or not lng:
        return None

    return normalize_location_link(f"https://www.google.com/maps?q={lat},{lng}")


def find_geo_coordinates(obj):
    """
    يبحث داخل JSON-LD كاملًا عن أول geo يحتوي latitude/longitude.
    Property Finder يضعها غالبًا داخل:
    mainEntity -> mainEntity -> geo
    لكن هذه الدالة أوسع حتى لو تغير مكانها.
    """
    if isinstance(obj, dict):
        lat = obj.get("latitude")
        lng = obj.get("longitude")

        if lat is not None and lng is not None:
            return lat, lng

        geo = obj.get("geo")
        if isinstance(geo, dict):
            lat = geo.get("latitude")
            lng = geo.get("longitude")
            if lat is not None and lng is not None:
                return lat, lng

        for value in obj.values():
            found = find_geo_coordinates(value)
            if found:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = find_geo_coordinates(item)
            if found:
                return found

    return None


def extract_location_link_from_soup(soup):
    """
    لا نحفظ صورة placeholder الخاصة بالخريطة.
    الرابط الصحيح في Property Finder يتم توليده من geo داخل JSON-LD.
    """
    scripts = []

    schema_script = soup.select_one("script#plp-schema[type='application/ld+json']")
    if schema_script:
        scripts.append(schema_script)

    for script in soup.select("script[type='application/ld+json']"):
        if script not in scripts:
            scripts.append(script)

    for script in scripts:
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        found = find_geo_coordinates(data)
        if not found:
            continue

        lat, lng = found
        location_link = make_google_maps_link(lat, lng)

        if location_link:
            return location_link

    return None


# ============================================================
# استخراج من صفحة التفاصيل
# ============================================================

def extract_detail_images_from_soup(soup):
    images = []
    seen = set()

    selectors = [
        "img[src*='static.shared.propertyfinder.sa/media/images/listing']",
        "img[data-testid='gallery-picture'][src]",
        "img[src*='/media/images/listing/']",
    ]

    for selector in selectors:
        for img in soup.select(selector):
            src = img.get("src")

            if not src:
                continue

            if "placeholder" in src:
                continue

            if src not in seen:
                seen.add(src)
                images.append(src)

    return images


def extract_property_details_from_soup(soup):
    """
    من قسم تفاصيل العقار:
    label + value
    """
    details = {}

    items = soup.select("div[class*='styles_desktop_list__item']")

    for item in items:
        label_tag = item.select_one("span[class*='label-text']")
        value_tag = item.select_one("p[class*='list__value']")

        if not label_tag or not value_tag:
            continue

        key = clean_text(label_tag.get_text(" ", strip=True))
        value = clean_text(value_tag.get_text(" ", strip=True))

        if key and value:
            details[key] = value

    # fallback عبر data-testid
    testid_map = {
        "property-details-type": "نوع العقار",
        "property-details-size": "مساحة العقار",
        "property-details-bedrooms": "عدد غرف النوم",
        "property-details-bathrooms": "الحمامات",
        "property-details-plot-length": "طول قطعة الأرض",
        "property-details-plot-width": "عرض قطعة الأرض",
        "property-details-street-width": "عرض الشارع",
        "property-details-street-direction": "الواجهة",
        "property-details-age": "عمر العقار",
        "property-details-category": "فئة",
        "property-details-location": "المدينة",
    }

    for testid, key in testid_map.items():
        tag = soup.select_one(f"[data-testid='{testid}']")
        if tag:
            value = clean_text(tag.get_text(" ", strip=True))
            if value:
                details[key] = value

    return details


def extract_features_from_soup(soup):
    features = []

    section = soup.select_one("section[data-testid='amenities-section']")

    if not section:
        return features

    for tag in section.select("p[class*='styles_text']"):
        text = clean_text(tag.get_text(" ", strip=True))

        if text and text not in features:
            features.append(text)

    return features


def extract_moj_location_from_soup(soup):
    result = {
        "moj_address": None,
        "city_from_moj": None,
        "district_from_moj": None,
    }

    card = soup.select_one("[data-testid='moj-deed-description']")
    if not card:
        return result

    text_tag = card.select_one("[data-testid='moj-deed-description-text']")
    if not text_tag:
        return result

    text = clean_text(text_tag.get_text(" ", strip=True))
    result["moj_address"] = text

    if not text:
        return result

    # مثال:
    # حي أبحر الجنوبية في مدينة جدة
    match = re.search(r"حي\s+(.+?)\s+في\s+مدينة\s+(.+)", text)
    if match:
        result["district_from_moj"] = clean_text(match.group(1))
        result["city_from_moj"] = clean_text(match.group(2))

    return result


def extract_description_from_soup(soup):
    possible_selectors = [
        "article[data-testid='dynamic-sanitize-html']",
        "[data-testid='dynamic-sanitize-html']",
        "article[class*='description']",
        "div[class*='description']",
        "[data-testid='property-description']",
        "section[id='description']",
    ]

    best = None
    best_len = 0

    for selector in possible_selectors:
        for tag in soup.select(selector):
            # نحافظ على فواصل <br> كسطور
            for br in tag.find_all("br"):
                br.replace_with("\n")

            text = clean_text(tag.get_text("\n", strip=True))

            if not text:
                continue

            # تجاهل النصوص القصيرة جدًا
            if len(text) < 20:
                continue

            if len(text) > best_len:
                best = text
                best_len = len(text)

    return best

def extract_advertiser_from_soup(soup):
    result = {}

    possible_agent_name = soup.select_one("[data-testid='agent-name']")
    if possible_agent_name:
        result["agent_name"] = clean_text(possible_agent_name.get_text(" ", strip=True))

    possible_broker_name = soup.select_one("[data-testid='broker-name']")
    if possible_broker_name:
        result["broker_name"] = clean_text(possible_broker_name.get_text(" ", strip=True))

    for img in soup.select("img[src]"):
        alt = clean_text(img.get("alt"))
        src = img.get("src")

        if alt and ("logo" in alt.lower() or "شركة" in alt):
            result["broker_logo_alt"] = alt
            result["broker_logo"] = src
            break

    return result


# ============================================================
# رقم الهاتف من النافذة المنبثقة بعد الضغط على زر اتصال
# ============================================================

def normalize_advertised_phone(raw):
    """تنظيف رقم الهاتف المعروض فقط؛ لا تخمين إذا كان غير متاح."""
    if not raw:
        return None
    value = arabic_digits_to_english(str(raw)).strip()
    if value.lower().startswith("tel:"):
        value = value[4:]
    value = re.sub(r"[\s()\-.]", "", value)
    if re.fullmatch(r"\+?\d{8,15}", value):
        return value
    return None


def extract_phone_from_call_popup(page):
    """
    يعتمد على محددات HTML التي أرسلها المستخدم:
      button[data-testid='bottom-actions-call-button-lead']
      div[class*='call-lead-modal_container']
      button[data-testid='call-number-button'] a[href^='tel:']
    يقرأ href ولا ينقر رابط tel:، ولذلك لا يبدأ مكالمة.
    """
    result = {"phone": None, "phone_url": None}
    try:
        modal = page.locator("div[class*='call-lead-modal_container']").first
        # في بعض الحالات قد تكون النافذة مفتوحة مسبقاً.
        if not modal.is_visible():
            call_button = page.locator("button[data-testid='bottom-actions-call-button-lead']").first
            call_button.wait_for(state="visible", timeout=6000)
            call_button.click(timeout=7500)

        modal.wait_for(state="visible", timeout=8500)
        tel_link = modal.locator(
            "button[data-testid='call-number-button'] a[href^='tel:']"
        ).first
        tel_link.wait_for(state="attached", timeout=6000)

        href = tel_link.get_attribute("href")
        phone = normalize_advertised_phone(href)
        if not phone:
            phone = normalize_advertised_phone(tel_link.inner_text(timeout=2000))
        if phone:
            result["phone"] = phone
            result["phone_url"] = href if href and href.lower().startswith("tel:") else f"tel:{phone}"
            print("✅ تم استخراج رقم الهاتف من نافذة الاتصال.")
        else:
            print("ℹ️ فُتحت نافذة الاتصال، لكن لم يُعرض رقم هاتف صالح.")
    except PlaywrightTimeoutError:
        print("ℹ️ زر الاتصال أو نافذة الرقم غير متاحين لهذا الإعلان.")
    except Exception as exc:
        # خطأ الهاتف وحده لا يحرمنا من باقي بيانات الإعلان.
        print(f"⚠️ تعذر قراءة رقم الهاتف لهذا الإعلان: {type(exc).__name__}: {exc}")
    return result



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

    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        locale="ar-SA",
        timezone_id="Asia/Riyadh",
    )

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
        random_sleep(1.5, 4, "بعد فتح صفحة الإعلان")

        if page_has_human_check(detail_page):
            return {
                "status": "human_check",
                "data": None,
            }

        phone_data = extract_phone_from_call_popup(detail_page)

        html = detail_page.content()
        soup = BeautifulSoup(html, "html.parser")

        location_link = extract_location_link_from_soup(soup)
        if location_link:
            print(f"✅ تم استخراج رابط الموقع: {location_link}")
        else:
            print("⚠️ لم يتم استخراج رابط الموقع من JSON-LD")

        detail_images = extract_detail_images_from_soup(soup)
        details = extract_property_details_from_soup(soup)
        features = extract_features_from_soup(soup)
        moj_location = extract_moj_location_from_soup(soup)
        description = extract_description_from_soup(soup)
        if description:
            print("✅ تم استخراج الوصف")
            print(description[:300])
        else:
            print("⚠️ لم يتم استخراج الوصف")
        advertiser = extract_advertiser_from_soup(soup)

        area_text = (
            details.get("مساحة العقار")
            or card_data.get("area_text")
        )

        area = parse_area(area_text) or card_data.get("area")

        city = (
            details.get("المدينة")
            or moj_location.get("city_from_moj")
            or card_data.get("city")
        )

        district = (
            moj_location.get("district_from_moj")
            or card_data.get("district")
        )

        region = card_data.get("region")

        detail_property_type = details.get("نوع العقار") or card_data.get("details", {}).get("card_property_type")

        final_property_type = property_type or detail_property_type

        images = detail_images if detail_images else card_data.get("images", [])

        full_details = {
            **card_data.get("details", {}),
            **details,
            "moj_location": moj_location,
        }

        final_data = {
            "source": SOURCE_NAME,
            "source_listing_id": card_data.get("source_listing_id"),

            "property_type": final_property_type,
            "purpose": purpose,

            "listing_url": listing_url,

            "title": card_data.get("title"),
            "price": card_data.get("price"),
            "price_text": card_data.get("price_text"),
            "rent_period": card_data.get("rent_period"),

            "area": area,
            "area_text": area_text,

            "region": region,
            "city": city,
            "sub_region": None,
            "district": district,
            "street": None,
            "building_number": None,
            "postal_code": None,
            "additional_number": None,
            "location_link": location_link,

            "phone": phone_data.get("phone"),
            "whatsapp_url": None,  # رابط tel ليس رابط واتساب

            "description": description or card_data.get("description"),

            "details": full_details,
            "features": features,
            "images": images,
            "videos": [],
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
        time.sleep(3)

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
                print("⚠️ لا توجد كروت بعد التحديث. سيتم إنهاء هذا التصنيف فقط.")
                return {
                    "status": "ok",
                    "stop_category": True,
                    "reason": "no_cards_after_refresh",
                }

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
                # الإعلان محفوظ بالفعل: لا نعيد حفظه ولا نوقف التصنيف.
                # نحدث فقط وقت آخر مشاهدة ثم نكمل لباقي الإعلانات والصفحات.
                touch_listing_seen(ad_id=ad_id, listing_url=listing_url)

                if ad_id in current_cycle_new_ids:
                    print(f"🟡 الإعلان محفوظ مسبقًا في نفس الدورة الحالية، تم تحديث last_seen_at والمتابعة: {ad_id}")
                    continue

                if initial_cycle:
                    print(f"🟡 إعلان مكرر أثناء الدورة الأولى، تم تحديث last_seen_at والمتابعة: {ad_id}")
                    continue

                if ad_id in existing_ids_before_cycle:
                    print(f"🟡 إعلان قديم موجود من قبل بداية الدورة، تم تحديث last_seen_at وسيستمر الفحص: {ad_id}")
                    continue

                print(f"🟡 إعلان موجود، تم تحديث last_seen_at وسيتم تجاهله والمتابعة: {ad_id}")
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
                save_listing(data)
                current_cycle_new_ids.add(ad_id)

                print("✅ تم حفظ الإعلان في قاعدة البيانات")
                print(f"العنوان: {data.get('title')}")
                print(f"السعر: {data.get('price_text')}")
                print(f"المساحة: {data.get('area_text')}")
                print(f"المدينة: {data.get('city')}")
                print(f"الحي: {data.get('district')}")
                print(f"رابط الموقع: {data.get('location_link')}")
                print(f"حالة الهاتف: {'متاح' if data.get('phone') else 'غير متاح'}")
                print(f"عدد الصور: {len(data.get('images') or [])}")
                print(f"عدد المميزات: {len(data.get('features') or [])}")

            else:
                print("⚠️ لم يتم حفظ الإعلان بسبب فشل صفحة التفاصيل، وسيكمل لباقي الإعلانات.")

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
    base_url = category["url"]
    property_type = category["property_type"]
    purpose = category["purpose"]

    page_number = 1

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

        page_number += 1

        random_sleep(
            DELAY_BETWEEN_PAGES_MIN,
            DELAY_BETWEEN_PAGES_MAX,
            "بين الصفحات"
        )


# ============================================================
# تشغيل دورة واحدة
# ============================================================

def run_property_finder_once():
    init_db()

    initial_cycle = not is_initial_collection_done()

    existing_ids_before_cycle = load_existing_ad_ids_before_cycle()
    current_cycle_new_ids = set()

    mark_cycle_started()

    print("\n" + "#" * 100)
    print(f"بدء دورة Property Finder: {datetime.now().isoformat(timespec='seconds')}")
    print(f"هل هذه الدورة الأولى؟ {'نعم' if initial_cycle else 'لا'}")
    print(f"عدد الإعلانات الموجودة قبل بداية الدورة: {len(existing_ids_before_cycle)}")
    print("#" * 100)

    with sync_playwright() as playwright:
        for category in PROPERTY_FINDER_LINKS:
            scrape_category(
                playwright=playwright,
                category=category,
                initial_cycle=initial_cycle,
                existing_ids_before_cycle=existing_ids_before_cycle,
                current_cycle_new_ids=current_cycle_new_ids,
            )

    if initial_cycle:
        mark_initial_collection_done()
        print("✅ تم تعليم الدورة الأولى لـ Property Finder كمكتملة. من الدورة القادمة سيستمر الفحص حتى نهاية الصفحات، وسيتم تجاهل الإعلانات القديمة وإضافة الجديدة فقط.")
    else:
        mark_cycle_finished()

    print("✅ انتهت دورة Property Finder.")


# ============================================================
# تشغيل مستمر كل ساعة
# ============================================================

def run_forever():
    while True:
        try:
            run_property_finder_once()
        except Exception as e:
            print("❌ خطأ عام في دورة Property Finder:")
            print(e)

        print(f"انتظار ساعة قبل الدورة التالية: {datetime.now().isoformat(timespec='seconds')}")
        time.sleep(3600)


def backfill_existing_phones(limit=100, after_id=0):
    """وضع اختياري لمرة واحدة: استكمال هواتف السجلات القديمة الناقصة دون تعديل بياناتها الأخرى."""
    init_db()
    with db_connect() as db:
        rows = db.execute(
            """SELECT id, listing_url FROM listings
               WHERE source = ? AND (phone IS NULL OR TRIM(phone) = '') AND id > ?
               ORDER BY id LIMIT ?""",
            (SOURCE_NAME, after_id, limit),
        ).fetchall()

    if not rows:
        print("✅ لا توجد سجلات قديمة مطابقة تحتاج استكمال الهاتف في هذا النطاق.")
        return

    done = 0
    last_id = after_id
    stopped_for_check = False
    with sync_playwright() as playwright:
        browser, context = create_context(playwright)
        try:
            for local_id, listing_url in rows:
                last_id = local_id
                page = context.new_page()
                try:
                    print(f"🔎 استكمال هاتف السجل المحلي #{local_id}")
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS)
                    if page_has_human_check(page):
                        print("⚠️ ظهرت صفحة تحقق؛ توقف الاستكمال دون تجاوزها.")
                        stopped_for_check = True
                        break
                    phone = extract_phone_from_call_popup(page).get("phone")
                    if phone:
                        with db_connect() as db:
                            db.execute(
                                """UPDATE listings SET phone = ?
                                   WHERE id = ? AND source = ?
                                   AND (phone IS NULL OR TRIM(phone) = '')""",
                                (phone, local_id, SOURCE_NAME),
                            )
                        done += 1
                        print("✅ تم استكمال رقم الهاتف دون إنشاء إعلان مكرر.")
                except Exception as exc:
                    print(f"⚠️ تعذر استكمال هاتف السجل #{local_id}: {exc}")
                finally:
                    page.close()
                random_sleep(DELAY_BETWEEN_LISTINGS_MIN, DELAY_BETWEEN_LISTINGS_MAX,
                             "بين عمليات استكمال الهواتف")
        finally:
            safe_close_context_browser(context, browser)

    print(f"انتهت المحاولة: تم استكمال {done} رقمًا، وآخر معرف محلي تمت زيارته: {last_id}")
    if stopped_for_check:
        print("ظهرت صفحة تحقق، ولم يتم الانتقال لما بعدها.")
    else:
        print(f"إذا أردت متابعة الدفعة التالية استخدم: --backfill-phones --after-id {last_id}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Property Finder scraper with call-popup phone extraction")
    parser.add_argument("--backfill-phones", action="store_true",
                        help="استكمال هواتف السجلات القديمة المفقودة مرة واحدة ثم الخروج")
    parser.add_argument("--limit", type=int, default=100,
                        help="أقصى عدد سجلات قديمة يُفحص في دفعة الاستكمال (الافتراضي 100)")
    parser.add_argument("--after-id", type=int, default=0,
                        help="ابدأ بعد آخر ID محلي من دفعة الاستكمال السابقة")
    args = parser.parse_args()
    if args.backfill_phones:
        backfill_existing_phones(limit=max(1, args.limit), after_id=max(0, args.after_id))
    else:
        run_forever()
