import json
import re
import sqlite3
import time
import random
import html as html_lib
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse, unquote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# الإعدادات
# ============================================================

BASE_URL = "https://www.bayut.sa"
SOURCE_NAME = "bayut"

DB_PATH = os.getenv("REAL_ESTATE_DB_PATH", "real_estate.db")

STOP_CATEGORY_ON_FIRST_OLD_AFTER_INITIAL_CYCLE = False

REQUEST_TIMEOUT = 60
HEADLESS = os.getenv("SCRAPER_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "y"}
WAIT_AFTER_PAGE_LOAD_MS = 2500

DELAY_BETWEEN_LISTINGS_MIN = 2
DELAY_BETWEEN_LISTINGS_MAX = 4

DELAY_BETWEEN_PAGES_MIN = 4
DELAY_BETWEEN_PAGES_MAX = 8

DELAY_AFTER_EVERY_N_LISTINGS = 25
LONG_BREAK_MIN = 8
LONG_BREAK_MAX = 12

RETRY_SLEEP_SECONDS = 15
MAX_PAGE_RETRIES = 5
MAX_DETAIL_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# ============================================================
# روابط بيوت
# ضع روابط عامة قليلة فقط
# ============================================================

BAYUT_LINKS = [
    {
        "url": "https://www.bayut.sa/ايجار-يومي/العقارات/السعودية/",
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


def clean_text(text):
    if text is None:
        return None

    text = html_lib.unescape(str(text))
    text = text.replace("\u200f", "").replace("\u200e", "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)

    return text.strip()


def has_real_value(value):
    if value is None:
        return False
    text = clean_text(value)
    if not text:
        return False
    return text not in {"-", "--", "—", "غير متوفر", "غير محدد", "لا يوجد"}


def normalize_rent_period(value):
    text = clean_text(value)
    if not text:
        return None
    if "ليلة" in text or "يوم" in text:
        return "يومي"
    if "أسبوع" in text or "اسبوع" in text:
        return "أسبوعي"
    if "شهر" in text:
        return "شهري"
    if "سنة" in text or "سنوي" in text:
        return "سنوي"
    return text


def arabic_digits_to_english(text):
    if text is None:
        return None

    mapping = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789"
    )

    return str(text).translate(mapping)


def normalize_url(href):
    if not href:
        return None

    href = href.strip()

    if href.startswith("http"):
        return href

    return urljoin(BASE_URL, href)


def extract_ad_id(url_or_text):
    if not url_or_text:
        return None

    text = str(url_or_text)

    # مثال:
    # /العقار/تفاصيل-87839720.html
    match = re.search(r"تفاصيل-(\d{5,})\.html", text)
    if match:
        return match.group(1)

    match = re.search(r"-(\d{5,})\.html", text)
    if match:
        return match.group(1)

    nums = re.findall(r"\d{5,}", text)
    if nums:
        return nums[-1]

    return None


def parse_price(price_text):
    if price_text is None:
        return None

    if isinstance(price_text, int):
        return price_text

    if isinstance(price_text, float):
        return int(price_text)

    text = arabic_digits_to_english(price_text)
    text = text.replace(",", "")
    text = text.replace("٬", "")
    text = text.replace("ريال", "")
    text = text.replace("SAR", "")
    text = text.replace("ر.س", "")
    text = text.replace("⃁", "")

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        return int(float(match.group()))
    except ValueError:
        return None


def parse_area(area_text):
    if area_text is None:
        return None

    if isinstance(area_text, int):
        return area_text

    if isinstance(area_text, float):
        return int(area_text)

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
    if text is None:
        return None

    if isinstance(text, int):
        return text

    text = arabic_digits_to_english(text)
    text = text.replace(",", "").replace("٬", "")

    match = re.search(r"\d+", text)
    if not match:
        return None

    try:
        return int(match.group())
    except ValueError:
        return None


def build_page_url(base_url, page_number):
    base_url = base_url.rstrip("/")

    if page_number == 1:
        return base_url + "/"

    return f"{base_url}/صفحة-{page_number}/"


def wait_before_retry(reason):
    print("=" * 90)
    print(f"⚠️ انتظار {RETRY_SLEEP_SECONDS} ثانية ثم إعادة المحاولة.")
    print(f"السبب: {reason}")
    print("=" * 90)
    time.sleep(RETRY_SLEEP_SECONDS)




def get_html(session, url):
    """
    يجلب HTML من خلال Chromium/Playwright بدل requests.
    اسم الوسيط session محفوظ حتى لا نغيّر بقية الدوال؛ لكنه BrowserContext.
    """
    page = None
    try:
        page = session.new_page()

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT * 1000,
        )

        # نعطي React/Next وقتًا قصيرًا لإكمال بناء الصفحة.
        page.wait_for_timeout(WAIT_AFTER_PAGE_LOAD_MS)

        # في صفحات النتائج ننتظر قليلًا حتى يظهر إعلان أو صندوق No hits.
        decoded_path = unquote(urlparse(url).path)
        if "/العقار/" not in decoded_path:
            try:
                page.wait_for_selector(
                    "li[role='article'][aria-label='Listing'], [aria-label='No hits box']",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass

        status_code = response.status if response else None
        final_url = page.url

        final_path = unquote(urlparse(final_url).path).rstrip("/")
        req_path = unquote(urlparse(url).path).rstrip("/")

        if final_path != req_path:
            return {
                "status": "redirected",
                "html": page.content(),
                "status_code": status_code,
                "final_url": unquote(final_url),
            }

        html_text = page.content()

        if status_code is not None and status_code != 200:
            return {
                "status": "error",
                "html": html_text,
                "status_code": status_code,
            }

        return {
            "status": "ok",
            "html": html_text,
            "status_code": status_code or 200,
            "final_url": final_url,
        }

    except Exception as e:
        return {
            "status": "error",
            "html": str(e),
            "status_code": None,
        }

    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def create_browser_context(playwright):
    """
    يفتح Chromium مرة واحدة ويُبقي Tab رئيسيًا ثابتًا طوال دورة السحب.

    كل استدعاء لـ get_html() سيفتح Tab مؤقتًا داخل نفس BrowserContext،
    وبعد انتهاء السحب يُغلق الـ Tab المؤقت فقط، بينما يظل المتصفح
    والـ Tab الرئيسي مفتوحين.
    """
    browser = playwright.chromium.launch(
        headless=HEADLESS,
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        locale="ar-SA",
        timezone_id="Asia/Riyadh",
        extra_http_headers={
            "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
        },
    )

    # ------------------------------------------------------------
    # Tab رئيسي ثابت
    # ------------------------------------------------------------
    # وجود هذا الـ Tab يمنع نافذة Chromium من الاختفاء بين كل طلب
    # والطلب التالي. get_html() يفتح Tab جديدًا ثم يغلقه، وهذا الـ Tab
    # يظل موجودًا طوال الدورة.
    main_page = context.new_page()

    try:
        main_page.goto(
            BASE_URL,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT * 1000,
        )
        main_page.wait_for_timeout(1000)
        print("✅ تم فتح متصفح Bayut وترك Tab رئيسي ثابت.")
    except Exception as e:
        # حتى لو لم تُحمّل الصفحة الرئيسية لأي سبب، نُبقي التاب نفسه
        # مفتوحًا (about:blank أو الصفحة التي وصل إليها) ليؤدي الغرض.
        print(f"⚠️ تعذر تحميل الصفحة الرئيسية في الـ Tab الثابت: {e}")
        print("ℹ️ سيبقى الـ Tab الرئيسي مفتوحًا وسيستمر السحب بشكل طبيعي.")

    return browser, context


def safe_close_browser(browser=None, context=None):
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


def touch_listing(ad_id=None, listing_url=None):
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
# استخراج كروت صفحة النتائج
# ============================================================

def page_has_no_hits_box(html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    box = soup.select_one("[aria-label='No hits box']")
    if box:
        return True

    text = clean_text(soup.get_text(" ", strip=True)) or ""
    return "نأسف، لا يوجد عقارات متوفرة حالياً مطابقة لمعايير بحثك" in text


def extract_cards_from_results_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    cards = []
    seen = set()

    # نبدأ من كل Listing حتى نقرأ النوع والسعر والتكرار من نفس الكارت.
    listing_nodes = soup.select("li[role='article'][aria-label='Listing'], article")

    for node in listing_nodes:
        a = node.select_one("a[aria-label='Listing link'][href], a[href*='تفاصيل-'][href], a[href*='.html'][href]")
        if not a:
            continue

        href = a.get("href")
        full_url = normalize_url(href)
        ad_id = extract_ad_id(full_url)

        if not full_url or not ad_id:
            continue
        if "تفاصيل-" not in unquote(full_url):
            continue
        if ad_id in seen:
            continue

        seen.add(ad_id)

        title_el = node.select_one("[aria-label='Title']")
        title = clean_text(title_el.get_text(" ", strip=True)) if title_el else clean_text(a.get("title"))

        type_el = node.select_one("[aria-label='Type']")
        property_type = clean_text(type_el.get_text(" ", strip=True)) if type_el else None

        price_el = node.select_one("[aria-label='Price']")
        price_text = clean_text(price_el.get_text(" ", strip=True)) if price_el else None

        frequency_el = node.select_one("[aria-label='Frequency']")
        frequency = clean_text(frequency_el.get_text(" ", strip=True)) if frequency_el else None

        location_el = node.select_one("[aria-label='Location'] h3, [aria-label='Location']")
        location_text = clean_text(location_el.get_text(" ", strip=True)) if location_el else None

        cards.append({
            "source_listing_id": ad_id,
            "listing_url": full_url,
            "title": title,
            "property_type": property_type,
            "price_text": price_text,
            "price": parse_price(price_text),
            "frequency": frequency,
            "rent_period": normalize_rent_period(frequency),
            "location_text": location_text,
        })

    return cards


# ============================================================
# استخراج JSON-LD
# ============================================================

def extract_json_ld_data(soup):
    result = {}

    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text(strip=True)

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        graphs = data.get("@graph", [])

        if isinstance(data, dict) and data.get("@type") == "RealEstateListing":
            graphs = [data]

        if not isinstance(graphs, list):
            continue

        for item in graphs:
            if not isinstance(item, dict):
                continue

            if item.get("@type") != "RealEstateListing":
                continue

            result["listing_name"] = item.get("name")
            result["listing_url"] = item.get("url")
            result["date_posted"] = item.get("datePosted")
            result["date_modified"] = item.get("dateModified")
            result["schema_description"] = item.get("description")
            result["breadcrumb"] = item.get("breadcrumb", {})
            result["main"] = item.get("mainEntity", {})

            return result

    return result


# ============================================================
# استخراج الموقع من Breadcrumb
# ============================================================

def extract_location_from_breadcrumb_names(names):
    result = {
        "city": None,
        "sub_region": None,
        "district": None,
    }

    for name in names:
        name = clean_text(name)

        if not name:
            continue

        # مثال: فلل للبيع في الرياض
        if "في " in name and ("للبيع" in name or "للإيجار" in name or "للايجار" in name):
            city = re.sub(r".*في\s+", "", name).strip()
            result["city"] = city

        # مثال: حي المرجان
        elif name.startswith("حي "):
            result["district"] = name.replace("حي ", "").strip()

        # مثال: شرق الرياض
        elif any(x in name for x in ["شرق", "غرب", "شمال", "جنوب", "وسط"]):
            result["sub_region"] = name

    return result


def extract_location_from_jsonld_breadcrumb(jsonld_data):
    breadcrumb = jsonld_data.get("breadcrumb") or {}
    items = breadcrumb.get("itemListElement") or []

    names = []

    for item in items:
        if not isinstance(item, dict):
            continue

        name = item.get("name")
        if name:
            names.append(name)

    return extract_location_from_breadcrumb_names(names)


def extract_location_from_html_breadcrumb(soup):
    breadcrumb = soup.find(attrs={"aria-label": "Breadcrumb"})

    if not breadcrumb:
        return {
            "city": None,
            "sub_region": None,
            "district": None,
        }

    names = []

    for tag in breadcrumb.find_all(attrs={"aria-label": "Link name"}):
        text = clean_text(tag.get_text(" ", strip=True))
        if text:
            names.append(text)

    return extract_location_from_breadcrumb_names(names)


def merge_location(jsonld_data, soup):
    location = extract_location_from_jsonld_breadcrumb(jsonld_data)

    if not location.get("city") and not location.get("district"):
        location = extract_location_from_html_breadcrumb(soup)

    main = jsonld_data.get("main") or {}
    address = main.get("address") or {}

    address_region = address.get("addressRegion")
    address_locality = address.get("addressLocality")

    # addressRegion في المثال = الرياض
    # addressLocality في المثال = شرق الرياض
    if address_region and not location.get("city"):
        location["city"] = clean_text(address_region)

    if address_locality and not location.get("sub_region"):
        location["sub_region"] = clean_text(address_locality)

    location["region"] = clean_text(address_region) or location.get("city")

    return location


# ============================================================
# استخراج بيانات صفحة التفاصيل
# ============================================================

def extract_title_from_soup_or_jsonld(soup, jsonld_data):
    h1 = soup.select_one("h1")

    if h1:
        return clean_text(h1.get_text(" ", strip=True))

    main = jsonld_data.get("main") or {}

    return clean_text(
        main.get("name")
        or jsonld_data.get("listing_name")
    )


def extract_description_from_soup_or_jsonld(soup, jsonld_data):
    desc_block = soup.find(attrs={"aria-label": "وصف العقار"})

    if desc_block:
        text = clean_text(desc_block.get_text("\n", strip=True))
        if text:
            return text

    main = jsonld_data.get("main") or {}

    return clean_text(
        main.get("description")
        or jsonld_data.get("schema_description")
    )


def extract_price_from_soup_or_jsonld(soup, jsonld_data):
    main = jsonld_data.get("main") or {}

    price = main.get("price")

    if price:
        parsed = parse_price(price)
        return parsed, str(parsed) if parsed is not None else str(price)

    price_tag = soup.select_one("[aria-label='Price']")

    if price_tag:
        price_text = clean_text(price_tag.get_text(" ", strip=True))
        return parse_price(price_text), price_text

    return None, None


def extract_property_header(soup):
    header = soup.find(attrs={"aria-label": "Property header"})

    if not header:
        return None

    return clean_text(header.get_text(" ", strip=True))


def extract_basic_specs_from_soup(soup):
    result = {
        "bedrooms_text": None,
        "bathrooms_text": None,
        "area_text": None,
    }

    beds = soup.find(attrs={"aria-label": "Beds"})
    baths = soup.find(attrs={"aria-label": "Baths"})
    area = soup.find(attrs={"aria-label": "Area"})

    if beds:
        result["bedrooms_text"] = clean_text(beds.get_text(" ", strip=True))

    if baths:
        result["bathrooms_text"] = clean_text(baths.get_text(" ", strip=True))

    if area:
        result["area_text"] = clean_text(area.get_text(" ", strip=True))

    return result


def extract_property_details_from_soup(soup):
    details = {}

    section = soup.select_one("ul[aria-label='Property details']")

    if not section:
        return details

    for li in section.select("li"):
        spans = li.find_all("span")

        if len(spans) < 2:
            continue

        key = clean_text(spans[0].get_text(" ", strip=True))
        value = clean_text(spans[1].get_text(" ", strip=True))

        if key and value:
            details[key] = value

    return details


def extract_features_from_jsonld_or_soup(jsonld_data, soup):
    main = jsonld_data.get("main") or {}

    features = []

    for item in main.get("amenityFeature") or []:
        if not isinstance(item, dict):
            continue

        name = clean_text(item.get("name"))

        if name and name not in features:
            features.append(name)

    if features:
        return features

    # fallback من HTML
    for tag in soup.select("span.c0327f5b"):
        text = clean_text(tag.get_text(" ", strip=True))

        if text and text not in features:
            features.append(text)

    return features


def normalize_location_link(url):
    if not url:
        return None

    url = clean_text(url)

    if not url:
        return None

    # لو srcset يحتوي أكثر من رابط، خذ أول رابط فقط
    url = url.split(",")[0].strip().split(" ")[0].strip()

    # توحيد روابط Google Maps وإزالة الشرطة المائلة الأخيرة حتى لا تتعطل بعض الأنظمة
    if "maps.google.com" in url or "google.com/maps" in url:
        url = url.replace("https://maps.google.com", "https://www.google.com/maps")
        url = url.replace("http://maps.google.com", "https://www.google.com/maps")
        url = url.rstrip("/")

    return url


def find_section_by_heading(soup, heading_text):
    for heading in soup.find_all(["h1", "h2", "h3"]):
        text = clean_text(heading.get_text(" ", strip=True))
        if text and heading_text in text:
            return heading.find_parent("div")

    return None


def extract_location_link_from_soup(soup):
    # 1) لو ظهر رابط خرائط مباشر داخل الصفحة
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue

        if (
            "google.com/maps" in href
            or "maps.google.com" in href
            or "goo.gl/maps" in href
            or "openstreetmap.org" in href
        ):
            return normalize_location_link(href)

    # 2) في بيوت غالباً الموقع يكون صورة خريطة داخل قسم: الموقع والأماكن القريبة
    location_section = find_section_by_heading(soup, "الموقع والأماكن القريبة")

    search_root = location_section
    if location_section:
        # نطلع للأب الأكبر القريب لأن الصورة قد تكون في sibling لنفس عنوان القسم
        for _ in range(4):
            parent = search_root.find_parent("div") if search_root else None
            if not parent:
                break
            if parent.select_one("img[alt='Map image'][src], img[src*='images.bayut.sa/thumbnails']"):
                search_root = parent
                break
            search_root = parent
    else:
        search_root = soup

    # الأفضل أخذ img src لأنه jpeg واضح وأسهل في الفتح من srcset
    img = search_root.select_one("img[alt='Map image'][src]") if search_root else None
    if img:
        return normalize_location_link(img.get("src"))

    # fallback داخل نفس قسم الموقع فقط
    if search_root:
        map_img = search_root.select_one("img[src*='images.bayut.sa/thumbnails'][src]")
        if map_img:
            return normalize_location_link(map_img.get("src"))

        source = search_root.select_one("source[srcset*='images.bayut.sa/thumbnails']")
        if source:
            return normalize_location_link(source.get("srcset"))

    return None


def extract_property_gallery_images(soup):
    images = []
    seen = set()

    # مهم: لا نسحب من كامل الصفحة حتى لا تدخل صورة الخريطة أو صور المعلن.
    # بيوت يضع صور العقار الأساسية داخل هذا القسم في أعلى الإعلان.
    gallery = soup.select_one("div._2e756e1e")

    if not gallery:
        return images

    for img in gallery.select("picture img[src], img[src]"):
        src = img.get("src")

        if not src:
            continue

        src = normalize_location_link(src)

        if not src:
            continue

        if "images.bayut.sa/thumbnails" not in src:
            continue

        # حماية إضافية: لا تدخل صورة الخريطة حتى لو ظهرت بالخطأ داخل نفس الصفحة
        alt = clean_text(img.get("alt")) or ""
        if "map" in alt.lower() or "خريطة" in alt:
            continue

        if src not in seen:
            seen.add(src)
            images.append(src)

    return images


def extract_images_and_videos(jsonld_data, soup):
    # حسب المطلوب: صور العقار فقط من معرض الصور العلوي، ولا يوجد fallback من كامل الصفحة.
    clean_images = extract_property_gallery_images(soup)

    main = jsonld_data.get("main") or {}
    videos = []

    thumbnail_url = clean_text(main.get("thumbnailUrl"))

    if thumbnail_url and ("youtu" in thumbnail_url or "youtube" in thumbnail_url):
        videos.append(
            {
                "video_url": thumbnail_url,
                "thumbnail": None,
            }
        )

    return clean_images, videos

def extract_geo_from_jsonld(jsonld_data):
    main = jsonld_data.get("main") or {}
    geo = main.get("geo") or {}

    return {
        "latitude": geo.get("latitude"),
        "longitude": geo.get("longitude"),
    }


def extract_advertiser_from_jsonld(jsonld_data):
    main = jsonld_data.get("main") or {}
    seller = main.get("seller") or {}

    advertiser = {
        "agent_name": clean_text(seller.get("name")),
        "agent_image": clean_text(seller.get("image")),
        "identifier": clean_text(seller.get("Identifier")),
    }

    organization = seller.get("memberOf") or {}

    if organization:
        advertiser["agency_name"] = clean_text(organization.get("name"))
        advertiser["agency_url"] = clean_text(organization.get("url"))
        advertiser["agency_image"] = clean_text(organization.get("image"))

    # مهم: لا نحفظ seller.telephone
    advertiser = {k: v for k, v in advertiser.items() if v}

    return advertiser


def extract_area_from_jsonld_or_soup(jsonld_data, soup):
    main = jsonld_data.get("main") or {}

    area_text = None
    area = None

    floor_size = main.get("floorSize") or {}

    if isinstance(floor_size, dict):
        value = floor_size.get("value")
        unit = floor_size.get("unitText")

        if value:
            area_text = f"{value} {unit or ''}".strip()
            area = parse_area(value)

    if not area:
        specs = extract_basic_specs_from_soup(soup)
        area_text = specs.get("area_text")

        if area_text:
            area = parse_area(area_text)

    return area, area_text


def infer_property_type(details, jsonld_data, fallback_property_type):
    detail_type = details.get("نوع العقار")
    if has_real_value(detail_type):
        return clean_text(detail_type)

    main = jsonld_data.get("main") or {}
    category = main.get("accommodationCategory")
    if has_real_value(category):
        return clean_text(category)

    if has_real_value(fallback_property_type):
        return clean_text(fallback_property_type)

    return None


def infer_purpose(details, fallback_purpose):
    # الغرض الذي يكتبه المستخدم للرابط هو المرجع الأساسي، مثل: إيجار يومي.
    if has_real_value(fallback_purpose):
        return clean_text(fallback_purpose)

    purpose = details.get("نوع العرض")
    if has_real_value(purpose):
        return clean_text(purpose)

    return None


# ============================================================
# صفحة التفاصيل مع retries
# ============================================================

def scrape_bayut_detail_page_once(session, card_data, property_type, purpose):
    listing_url = card_data["listing_url"]

    result = get_html(session, listing_url)

    if result["status"] != "ok":
        print(f"❌ فشل فتح صفحة التفاصيل: {listing_url}")
        print(f"الحالة: {result['status']} | status_code={result.get('status_code')}")
        return {
            "status": result["status"],
            "data": None,
        }

    soup = BeautifulSoup(result["html"], "html.parser")

    jsonld_data = extract_json_ld_data(soup)

    if not jsonld_data:
        print("⚠️ لم يتم العثور على JSON-LD في صفحة التفاصيل، سيتم الاعتماد على HTML فقط.")

    location = merge_location(jsonld_data, soup)
    location_link = extract_location_link_from_soup(soup)

    price, price_text = extract_price_from_soup_or_jsonld(soup, jsonld_data)
    title = extract_title_from_soup_or_jsonld(soup, jsonld_data) or card_data.get("title")
    description = extract_description_from_soup_or_jsonld(soup, jsonld_data)

    details = extract_property_details_from_soup(soup)
    features = extract_features_from_jsonld_or_soup(jsonld_data, soup)
    images, videos = extract_images_and_videos(jsonld_data, soup)
    geo = extract_geo_from_jsonld(jsonld_data)
    advertiser = extract_advertiser_from_jsonld(jsonld_data)

    area, area_text = extract_area_from_jsonld_or_soup(jsonld_data, soup)

    specs = extract_basic_specs_from_soup(soup)
    property_header = extract_property_header(soup)

    final_property_type = infer_property_type(details, jsonld_data, card_data.get("property_type") or property_type)
    final_purpose = infer_purpose(details, purpose)

    main = jsonld_data.get("main") or {}

    full_details = {
        **details,
        "property_header": property_header,
        "bedrooms": main.get("numberOfBedrooms"),
        "bathrooms": main.get("numberOfBathroomsTotal"),
        "bedrooms_text": specs.get("bedrooms_text"),
        "bathrooms_text": specs.get("bathrooms_text"),
        "geo": geo,
        "date_posted": jsonld_data.get("date_posted"),
        "date_modified": jsonld_data.get("date_modified"),
    }

    final_data = {
        "source": SOURCE_NAME,
        "source_listing_id": card_data.get("source_listing_id") or extract_ad_id(listing_url),

        "property_type": final_property_type,
        "purpose": final_purpose,

        "listing_url": listing_url,

        "title": title,
        "price": price,
        "price_text": price_text,
        "rent_period": card_data.get("rent_period"),

        "area": area,
        "area_text": area_text,

        "region": location.get("region"),
        "city": location.get("city"),
        "sub_region": location.get("sub_region"),
        "district": location.get("district"),
        "street": None,
        "building_number": None,
        "postal_code": None,
        "additional_number": None,
        "location_link": location_link,

        # مهم: لا نسحب ولا نحفظ الهاتف أو الواتساب
        "phone": None,
        "whatsapp_url": None,

        "description": description,

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


def scrape_bayut_detail_page_with_retries(session, card_data, property_type, purpose):
    for attempt in range(1, MAX_DETAIL_RETRIES + 1):
        result = scrape_bayut_detail_page_once(
            session=session,
            card_data=card_data,
            property_type=property_type,
            purpose=purpose,
        )

        if result["status"] == "ok":
            return result["data"]

        print(f"⚠️ محاولة صفحة التفاصيل فشلت: {attempt}/{MAX_DETAIL_RETRIES}")

        if result["status"] == "blocked":
            time.sleep(RETRY_SLEEP_SECONDS)
        else:
            time.sleep(3)

    return None


# ============================================================
# صفحة نتائج واحدة
# ============================================================

def scrape_results_page_once(
    session,
    page_url,
    purpose,
    initial_cycle,
    existing_ids_before_cycle,
    current_cycle_new_ids,
):
    print("=" * 90)
    print(f"فتح صفحة النتائج: {page_url}")
    print(f"الغرض: {purpose} | النوع: يُقرأ من كل إعلان تلقائيًا")
    print(f"وضع الدورة: {'الدورة الأولى' if initial_cycle else 'دورة متابعة'}")
    print("=" * 90)

    result = get_html(session, page_url)

    # نُبقي سلوك التحويل الموجود في الكود الأصلي كما هو.
    if result["status"] == "redirected":
        print("⛔ بيوت قام بتحويل الصفحة إلى رابط آخر. سيتم إنهاء هذا الرابط.")
        print(f"تم تحويلنا إلى: {result.get('final_url')}")
        return {"status": "ok", "stop_category": True, "reason": "redirected_by_bayut"}

    if result["status"] != "ok":
        print(f"⚠️ فشل فتح صفحة النتائج: {page_url}")
        print(f"الحالة: {result['status']} | status_code={result.get('status_code')}")
        return {"status": "retry", "stop_category": False, "reason": result["status"]}

    html_text = result["html"]

    # هذه هي إشارة نهاية النتائج التي طلبها المستخدم صراحة.
    if page_has_no_hits_box(html_text):
        print("⛔ بيوت يعرض: لا يوجد عقارات متوفرة حالياً مطابقة لمعايير البحث.")
        return {"status": "ok", "stop_category": True, "reason": "no_hits_box"}

    cards = extract_cards_from_results_html(html_text)

    # عدم وجود كروت وحده ليس دليلاً على نهاية الصفحات؛ قد يكون HTML ناقصًا أو تغيرًا مؤقتًا.
    if not cards:
        print("⚠️ لم يتم العثور على كروت، لكن صندوق No hits غير موجود؛ ستتم إعادة المحاولة بدل إنهاء الرابط.")
        return {"status": "retry", "stop_category": False, "reason": "no_cards_without_no_hits_box"}

    print(f"✅ عدد الكروت المستخرجة من الصفحة: {len(cards)}")

    for index, card in enumerate(cards, start=1):
        ad_id = str(card.get("source_listing_id") or "")
        listing_url = card.get("listing_url")
        card_type = card.get("property_type")

        print("-" * 90)
        print(f"إعلان {index}/{len(cards)}")
        print(f"ID: {ad_id}")
        print(f"النوع المكتشف من الصفحة: {card_type}")
        print(f"الرابط: {listing_url}")

        if listing_exists(ad_id, listing_url):
            touch_listing(ad_id=ad_id, listing_url=listing_url)
            if ad_id in current_cycle_new_ids:
                print(f"🟡 الإعلان محفوظ مسبقًا في نفس الدورة؛ تم تحديث last_seen_at والمتابعة: {ad_id}")
            else:
                print(f"🟡 إعلان موجود مسبقًا؛ تم تحديث last_seen_at والمتابعة دون إيقاف الرابط: {ad_id}")
            continue

        data = scrape_bayut_detail_page_with_retries(
            session=session,
            card_data=card,
            property_type=card_type,
            purpose=purpose,
        )

        if data:
            save_listing(data)
            current_cycle_new_ids.add(ad_id)

            print("✅ تم حفظ الإعلان في قاعدة البيانات")
            print(f"النوع: {data.get('property_type')}")
            print(f"الغرض: {data.get('purpose')}")
            print(f"العنوان: {data.get('title')}")
            print(f"السعر: {data.get('price_text')}")
            print(f"فترة الإيجار: {data.get('rent_period')}")
            print(f"المساحة: {data.get('area_text')}")
            print(f"المدينة: {data.get('city')}")
            print(f"المنطقة الفرعية: {data.get('sub_region')}")
            print(f"الحي: {data.get('district')}")
            print(f"رابط الموقع/صورة الموقع: {data.get('location_link')}")
            print(f"عدد الصور: {len(data.get('images') or [])}")
            print(f"عدد المميزات: {len(data.get('features') or [])}")
        else:
            print("⚠️ لم يتم حفظ الإعلان بسبب فشل صفحة التفاصيل، وسيكمل لباقي الإعلانات.")

        random_sleep(DELAY_BETWEEN_LISTINGS_MIN, DELAY_BETWEEN_LISTINGS_MAX, "بين الإعلانات")

        if index % DELAY_AFTER_EVERY_N_LISTINGS == 0:
            random_sleep(LONG_BREAK_MIN, LONG_BREAK_MAX, f"استراحة بعد {DELAY_AFTER_EVERY_N_LISTINGS} إعلان")

    return {"status": "ok", "stop_category": False, "reason": "page_done"}


def scrape_results_page_with_retries(
    session,
    page_url,
    purpose,
    initial_cycle,
    existing_ids_before_cycle,
    current_cycle_new_ids,
):
    retry_count = 0

    while True:
        result = scrape_results_page_once(
            session=session,
            page_url=page_url,
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
    session,
    category,
    initial_cycle,
    existing_ids_before_cycle,
    current_cycle_new_ids,
):
    base_url = category["url"]
    purpose = category["purpose"]

    page_number = 1

    while True:
        page_url = build_page_url(base_url, page_number)

        result = scrape_results_page_with_retries(
            session=session,
            page_url=page_url,
            purpose=purpose,
            initial_cycle=initial_cycle,
            existing_ids_before_cycle=existing_ids_before_cycle,
            current_cycle_new_ids=current_cycle_new_ids,
        )

        if result.get("stop_category"):
            print(f"✅ انتهى الرابط: {base_url}")
            print(f"الغرض: {purpose}")
            print(f"سبب الإنهاء: {result.get('reason')}")
            break

        page_number += 1
        random_sleep(DELAY_BETWEEN_PAGES_MIN, DELAY_BETWEEN_PAGES_MAX, "بين الصفحات")


# ============================================================
# تشغيل دورة واحدة
# ============================================================

def run_bayut_once():
    init_db()

    initial_cycle = not is_initial_collection_done()

    existing_ids_before_cycle = load_existing_ad_ids_before_cycle()
    current_cycle_new_ids = set()

    mark_cycle_started()

    print("\n" + "#" * 100)
    print(f"بدء دورة Bayut: {datetime.now().isoformat(timespec='seconds')}")
    print(f"هل هذه الدورة الأولى؟ {'نعم' if initial_cycle else 'لا'}")
    print(f"عدد الإعلانات الموجودة قبل بداية الدورة: {len(existing_ids_before_cycle)}")
    print("#" * 100)

    browser = None
    session = None

    with sync_playwright() as playwright:
        try:
            browser, session = create_browser_context(playwright)

            for category in BAYUT_LINKS:
                scrape_category(
                    session=session,
                    category=category,
                    initial_cycle=initial_cycle,
                    existing_ids_before_cycle=existing_ids_before_cycle,
                    current_cycle_new_ids=current_cycle_new_ids,
                )
        finally:
            safe_close_browser(browser=browser, context=session)

    if initial_cycle:
        mark_initial_collection_done()
        print("✅ تم تعليم الدورة الأولى لـ Bayut كمكتملة. الدورات التالية ستتجاوز القديم وتواصل حتى نهاية الصفحات.")
    else:
        mark_cycle_finished()

    print("✅ انتهت دورة Bayut.")


# ============================================================
# تشغيل مستمر كل ساعة
# ============================================================

def run_forever():
    while True:
        try:
            run_bayut_once()
        except Exception as e:
            print("❌ خطأ عام في دورة Bayut:")
            print(e)

        print(f"انتظار ساعة قبل الدورة التالية: {datetime.now().isoformat(timespec='seconds')}")
        time.sleep(3600)


if __name__ == "__main__":
    # تشغيل دورة واحدة:
    # run_bayut_once()

    # للتشغيل المستمر كل ساعة:
    run_forever()