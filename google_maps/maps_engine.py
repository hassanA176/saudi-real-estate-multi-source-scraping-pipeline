"""Saudi Maps progress-driven search and details engine.

Experimental browser automation: Google Maps can change its UI, and map pins painted on
canvas are not guaranteed to react to result-card hover. Report fallback honestly.
Use only where the source permits gathering and retaining the relevant data/photos.
"""
from __future__ import annotations
import argparse
import asyncio
import csv
import io
import json
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SOURCE = 'google_maps'
FEED = 'div[role="feed"]'
CARD = 'div[role="article"] a.hfpxzc[href*="/maps/place/"]'
SPINNER = '.E626Jd.VjANhb'
END = 'span.HlvSq'


def stamp():
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None).isoformat(timespec='seconds')


def clean_url(url):
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, u.path, '', ''))


def place_id(url):
    decoded = unquote(url)
    for pattern in (r'!19s(ChIJ[^!?&/]+)', r'!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)', r'!16s(/g/[^!?&]+)'):
        m = re.search(pattern, decoded)
        if m:
            return m.group(1)
    return clean_url(url)


def arabic_digits(t):
    table = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
    return (t or '').translate(table).replace('٬', ',').replace('\u200e','').replace('\u200f','')


def extract_price(t):
    cleaned=arabic_digits(t)
    m=(re.search(r'(\d[\d,.]*)\s*(?:ر\s*\.\s*س|ريال|SAR)',cleaned,re.I) or
       re.search(r'(?:SAR|ر\s*\.\s*س|ريال)\s*(\d[\d,.]*)',cleaned,re.I))
    if not m:
        return None
    try:
        return int(float(m[1].replace(',', '')))
    except ValueError:
        return None


def number_after(text, words):
    txt = arabic_digits(text)
    for word in words:
        # Count usually follows the label; accept both: عدد الأشخاص: 5, 2 غرف نوم.
        for regex in (rf'{word}\s*[:：]?\s*(?:\([^)]*\)\s*)?(\d+)',
                      rf'(\d+)\s*{word}'):
            m = re.search(regex, txt, re.I)
            if m:
                return int(m[1])
    return None


def parsed_specs(specs, title=''):
    """Prefer explicit specifications, mark any title-only inference explicitly."""
    text = arabic_digits(' | '.join(specs or []))
    out = {'capacity':None,'bedrooms':None,'beds':None,'bathrooms':None}
    source = {}
    patterns = {
        'capacity': [r'(?:عدد\s*الأشخاص|الأشخاص|نزلاء|guests?)\s*[:：]?\s*(\d+)',
                     r'(\d+)\s*(?:أشخاص|ضيوف|guests?)'],
        'bedrooms': [r'(?:غرف(?:ة|ات)?\s*نوم|bedrooms?)\s*[:：]?\s*(\d+)',
                     r'(\d+)\s*(?:غرف(?:ة|ات)?\s*نوم|bedrooms?)',
                     r'غرفتا\s*\(?\s*(2)\s*\)?\s*نوم'],
        'beds': [r'(?:أسِرّة|أسرّة|أسرّة|سرير|beds?)\s*[:：]?\s*(\d+)',
                 r'(\d+)\s*(?:أسرّة|أسرة|سرير|beds?)'],
        'bathrooms': [r'(?:حمّ?ام(?:ات)?|bathrooms?)\s*[:：]?\s*(\d+)',
                      r'(\d+)\s*(?:حمّ?ام(?:ات)?|bathrooms?)',
                      r'حمّ?امان\s*\(?\s*(2)\s*\)?']
    }
    for key,regexes in patterns.items():
        for expr in regexes:
            m=re.search(expr,text,re.I)
            if m:
                out[key]=int(m[1]);source[key]='page_specs';break
    written={
        'bedrooms':[(r'غرفة\s*نوم\s*واحدة',1),(r'غرفتا\s*نوم',2),(r'غرفتان\s*نوم',2)],
        'beds':[(r'سرير\s*واحد',1),(r'سريران',2)],
        'bathrooms':[(r'حمّ?ام\s*واحد',1),(r'حمّ?امان',2)]
    }
    for key, variants in written.items():
        if out[key] is None:
            for expr,n in variants:
                if re.search(expr,text,re.I):
                    out[key]=n;source[key]='page_words';break
    if out['bedrooms'] is None:
        for expr,n in [(r'one[- ]bedroom',1),(r'two[- ]bedroom',2),
                       (r'three[- ]bedroom',3),(r'four[- ]bedroom',4),
                       (r'five[- ]bedroom',5),(r'six[- ]bedroom',6),
                       (r'غرفة\s*نوم\s*واحدة',1),(r'غرفتا\s*نوم',2)]:
            if re.search(expr,arabic_digits(title or ''),re.I):
                out['bedrooms']=n;source['bedrooms']='title_inference';break
    out['_spec_provenance']=source
    return out


def parse_address(address, search_query, expected_city=None):
    parts = [p.strip() for p in re.split(r'[,،]', address or '') if p.strip()]
    city = (expected_city if expected_city and expected_city in (address or '') else None)
    if city is None:
        city = next((c for c in ('الرياض', 'جدة', 'مكة', 'الدمام', 'بريدة', 'عنيزة') if c in (address or '')), None)
    # Last part can be Riyadh + zip. Only assign district when explicitly present before city.
    district = parts[-2] if city and len(parts) >= 2 and city in parts[-1] else None
    postal = re.search(r'(?<!\d)(\d{5})(?!\d)', arabic_digits(address or ''))
    return {'city':city, 'district':district, 'postal_code':postal[1] if postal else None,
            'address_raw':address, 'search_query':search_query}


def coords_from_url(url):
    s = unquote(url)
    m = re.search(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)', s)
    if not m:
        m = re.search(r'/@(-?\d+\.\d+),(-?\d+\.\d+)', s)
    return {'latitude':float(m[1]),'longitude':float(m[2])} if m else {}


def make_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_listing_id TEXT,
        property_type TEXT,purpose TEXT,listing_url TEXT NOT NULL,title TEXT,
        price INTEGER,price_text TEXT,rent_period TEXT,area INTEGER,area_text TEXT,
        region TEXT,city TEXT,sub_region TEXT,district TEXT,street TEXT,
        building_number TEXT,postal_code TEXT,additional_number TEXT,location_link TEXT,
        phone TEXT,whatsapp_url TEXT,description TEXT,details_json TEXT,features_json TEXT,
        images_json TEXT,videos_json TEXT,advertiser_json TEXT,
        first_seen_at TEXT,last_seen_at TEXT,is_active INTEGER DEFAULT 1,
        UNIQUE(source,source_listing_id), UNIQUE(source,listing_url));
    CREATE TABLE IF NOT EXISTS gm_discovered_links (
        source_listing_id TEXT PRIMARY KEY,query TEXT NOT NULL,title TEXT,url TEXT NOT NULL,
        region TEXT, city TEXT, category TEXT, last_seen_at TEXT, last_attempt_at TEXT,
        discovered_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT);
    CREATE TABLE IF NOT EXISTS gm_recovery_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,query TEXT NOT NULL,observed_at TEXT NOT NULL,
        reason TEXT NOT NULL,spinner_seconds REAL,click_mode TEXT,clicked_x INTEGER,
        clicked_y INTEGER,details_opened INTEGER,spinner_cleared INTEGER,note TEXT);
    CREATE TABLE IF NOT EXISTS gm_scroll_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, observed_at TEXT NOT NULL,
        iteration INTEGER NOT NULL, before_top REAL, after_top REAL, scroll_height REAL,
        client_height REAL, spinner_visible INTEGER, new_links INTEGER,
        progress_reason TEXT, stalled_seconds REAL, recovery_attempt INTEGER);
    ''')
    db.commit()
    return db


def save_card(db, query, title, url, region=None, city=None, category=None):
    """Save one Google place once across all queries/cycles; touch existing listing timestamps."""
    if not url or '/maps/place/' not in url:
        return False
    pid = place_id(url)
    now = stamp()
    try:
        known = db.execute('SELECT status FROM gm_discovered_links WHERE source_listing_id=?', (pid,)).fetchone()
        if known:
            db.execute('UPDATE gm_discovered_links SET last_seen_at=? WHERE source_listing_id=?',(now,pid))
            if known['status'] in ('done', 'partial'):
                db.execute('UPDATE listings SET last_seen_at=? WHERE source=? AND source_listing_id=?', (now,SOURCE,pid))
            db.commit()
            return False
        existing = db.execute("SELECT source_listing_id FROM listings WHERE source=? AND (source_listing_id=? OR listing_url=?) LIMIT 1",
            (SOURCE,pid,clean_url(url))).fetchone()
        status='done' if existing else 'pending'
        db.execute("INSERT OR IGNORE INTO gm_discovered_links (source_listing_id,query,title,url,region,city,category,last_seen_at,discovered_at,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid,query,title,clean_url(url),region,city,category,now,now,status))
        if existing:
            db.execute('UPDATE listings SET last_seen_at=? WHERE source=? AND source_listing_id=?',
                       (now,SOURCE,existing['source_listing_id']))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


async def cards_now(page):
    return await page.locator(FEED).first.locator(CARD).evaluate_all('''links => links.map(a => ({
         title:a.getAttribute('aria-label') || a.textContent.trim(),url:a.href
    })).filter(x => x.url)''')


async def spinner_visible(page):
    try:
        return await page.locator(FEED).first.locator(SPINNER).first.is_visible(timeout=300)
    except Exception:
        return False


async def end_text(page):
    marker = page.locator(FEED).first.locator(END).first
    try:
        if await marker.is_visible(timeout=250):
            txt = (await marker.inner_text(timeout=450)).strip()
            if 'نهاية' in txt or 'end of' in txt.lower():
                return txt
    except Exception:
        pass
    return None


async def feed_scroll(page):
    feed = page.locator(FEED).first
    return await feed.evaluate('''e => {
      const a=e.scrollTop;
      e.scrollTop += Math.max(225,Math.round(e.clientHeight*.84));
      return {oldTop:a,newTop:e.scrollTop,height:e.scrollHeight,client:e.clientHeight};
    }''')


async def feed_position(page):
    return await page.locator(FEED).first.evaluate('''e => ({
       top:e.scrollTop,height:e.scrollHeight,client:e.clientHeight,
       atBottom:e.scrollHeight-e.clientHeight-e.scrollTop<3
    })''')


async def map_rectangle(page):
    return await page.evaluate('''() => {
      const f=document.querySelector('[role="feed"]');
      if(!f) return null;
      const r=f.getBoundingClientRect();
      // Arabic Maps can put the result feed on the RIGHT and the map on the
      // LEFT. English Maps may do the reverse. Measure both visible sides.
      const y=72, height=innerHeight-y-52;
      const left={x:14,y,width:Math.floor(r.left-28),height};
      const right={x:Math.ceil(r.right+14),y,
                   width:Math.floor(innerWidth-r.right-38),height};
      const sides=[left,right].filter(v=>v.width>=170 && v.height>=150);
      return sides.sort((a,b)=>b.width-a.width)[0] || null;
    }''')


def candidate_from_images(before, hovered, after):
    """Pixel-level visual change that APPEARS on card hover and DISAPPEARS on mouseleave.

    Returns image-local point or None; never implies a candidate is a verified marker.
    """
    a=np.asarray(Image.open(io.BytesIO(before)).convert('RGB'), dtype=np.int16)
    b=np.asarray(Image.open(io.BytesIO(hovered)).convert('RGB'), dtype=np.int16)
    c=np.asarray(Image.open(io.BytesIO(after)).convert('RGB'), dtype=np.int16)
    if a.shape != b.shape or a.shape != c.shape:
        return None
    dif1 = np.max(np.abs(a-b),axis=2)>42
    dif2 = np.max(np.abs(b-c),axis=2)>42
    changed = dif1 & dif2
    H,W=changed.shape
    tile=12
    gh=(H//tile); gw=(W//tile)
    if gh<5 or gw<5:
        return None
    # Suppress animation speckles; find compact patches without assuming fixed marker colors.
    grid=(changed[:gh*tile,:gw*tile].reshape(gh,tile,gw,tile)
          .sum(axis=(1,3))>=11)
    seen=np.zeros_like(grid, dtype=bool)
    clusters=[]
    for yy,xx in zip(*np.where(grid)):
        if seen[yy,xx]: continue
        pending=[(int(yy),int(xx))];seen[yy,xx]=True;members=[]
        while pending:
            r,k=pending.pop();members.append((r,k))
            for nr,nk in ((r+1,k),(r-1,k),(r,k+1),(r,k-1)):
                if 0<=nr<gh and 0<=nk<gw and grid[nr,nk] and not seen[nr,nk]:
                    seen[nr,nk]=True;pending.append((nr,nk))
        y0=min(z[0] for z in members)*tile; y1=(max(z[0] for z in members)+1)*tile
        x0=min(z[1] for z in members)*tile; x1=(max(z[1] for z in members)+1)*tile
        width=x1-x0;height=y1-y0
        # Map pin usually occupies a compact region; broad repaint means unreliable.
        if 12<=width<=145 and 12<=height<=155 and len(members)>=2:
            # Prefer visual markers of medium size over tiny animated artifacts.
            score=min(len(members),30) - abs(width-height)*.015
            clusters.append((score,(x0+x1)//2,(y0+y1)//2, (x0+x1)//2,max(y0,y1-12)))
    if not clusters:
        return None
    clusters.sort(reverse=True)
    _,mx,my,bx,by=clusters[0]
    return [(mx,my), (bx,by)]


async def snapshot_map(page, rect):
    data=await page.screenshot(animations='disabled')
    with Image.open(io.BytesIO(data)) as im:
        return im.crop((rect['x'],rect['y'],rect['x']+rect['width'],rect['y']+rect['height'])).convert('RGB')


async def open_detail_confirmed(page, x, y, expected_title=None):
    old_url=page.url
    await page.mouse.click(x,y)
    try:
        heading=page.locator('h1.DUwDvf').first
        await heading.wait_for(state='visible',timeout=18000)
        title=(await heading.inner_text()).strip()
        if title and title != 'النتائج':
            return title
    except Exception:
        pass
    return None


async def restore_search(page, search_url, old_top):
    feed=page.locator(FEED).first
    # Close a details pane if present while search results remain behind it.
    if await page.locator('h1.DUwDvf').first.count():
        for css in ('button[aria-label="إغلاق"]','button[aria-label="Close"]',
                    'button[aria-label="رجوع"]','button[aria-label="Back"]'):
            b=page.locator(css).first
            if await b.count() and await b.is_visible():
                try:
                    await b.click(timeout=1500)
                    await asyncio.sleep(.45)
                    if await feed.count() and await feed.is_visible() and not await page.locator('h1.DUwDvf').first.is_visible():
                        break
                except Exception:
                    pass
    if not await feed.count() or not await feed.is_visible() or (await page.locator('h1.DUwDvf').first.count() and await page.locator('h1.DUwDvf').first.is_visible()):
        try:
            await page.go_back(wait_until='domcontentloaded',timeout=17000)
            await feed.wait_for(timeout=9000)
        except Exception:
            await page.goto(search_url,wait_until='domcontentloaded',timeout=45000)
            await feed.wait_for(timeout=25000)
    # After navigation the virtual feed can start short; grow it safely to the old point.
    for _ in range(35):
        vals=await feed.evaluate('(e,t)=>{e.scrollTop=t;return [e.scrollTop,e.scrollHeight-e.clientHeight]}', old_top)
        if vals[0]>=old_top-8 or vals[1]>=old_top-8:
            break
        await asyncio.sleep(.22)
    return feed


async def choose_automatic_map_points(page,out):
    rect=await map_rectangle(page)
    if rect is None:
        return [], 'map area unavailable'
    # A semantic DOM marker exists on some Maps UI variants even when the base map is Canvas.
    cards=page.locator(FEED).first.locator(CARD)
    cnt=await cards.count()
    for i in range(min(cnt,4)):
        a=cards.nth(i)
        try:
            if not await a.is_visible():continue
            title=(await a.get_attribute('aria-label') or '').strip()
            if title:
                candidates=await page.evaluate('''({title,rx,ry,rw,rh})=>{
                 const norm=s=>s.replace(/[\\s\\-]/g,'').toLowerCase();
                 const s=norm(title);let points=[];
                 document.querySelectorAll('[aria-label]').forEach(e=>{
                   const t=norm(e.getAttribute('aria-label')||'');const r=e.getBoundingClientRect();
                   if(t.length>=5 && (t.includes(s)||s.includes(t)) && r.width>=8 && r.width<=130 &&
                     r.height>=8 && r.height<=130 && r.left>rx && r.top>ry &&
                     r.right<rx+rw && r.bottom<ry+rh){
                      points.push([Math.round(r.x+r.width/2),Math.round(r.y+r.height/2)]);
                   }
                 });return points;
                }''',{'title':title,'rx':rect['x'],'ry':rect['y'],'rw':rect['width'],'rh':rect['height']})
                if candidates:
                    return candidates[:4], 'map_dom_accessible_marker'
        except Exception:
            continue
    # The Canvas marker may be revealed/highlighted when its result card is hovered.
    for i in range(min(cnt,4)):
        a=cards.nth(i)
        try:
            if not await a.is_visible(): continue
            # Do not let in-flight tile rendering trigger a false location.
            await page.mouse.move(25,56)
            await asyncio.sleep(.65)
            before=await snapshot_map(page,rect)
            await a.hover(timeout=2500)
            await asyncio.sleep(.65)
            hovered=await snapshot_map(page,rect)
            await page.mouse.move(25,56)
            await asyncio.sleep(.65)
            after=await snapshot_map(page,rect)
            if out:
                out.mkdir(parents=True,exist_ok=True)
                # Keep one visual diagnostic: what changed on hover, if anything.
                hovered.save(out/f'hover_candidate_{i}.png')
            def png(im):
                b=io.BytesIO();im.save(b,format='PNG');return b.getvalue()
            points=candidate_from_images(png(before),png(hovered),png(after))
            if points:
                return [(rect['x']+x,rect['y']+y) for x,y in points], 'canvas_visual_hover'
        except Exception:
            continue
    return [], 'no reliable marker detected; card hover did not expose a marker'


async def recover(page,db,query,search_url,reason,seconds,out):
    feed=page.locator(FEED).first
    try: old_top=await feed.evaluate('e=>e.scrollTop')
    except Exception: old_top=0
    screenshot_dir=out/'recovery_debug'
    screenshot_dir.mkdir(parents=True,exist_ok=True)
    event_number=db.execute('SELECT COUNT(*) FROM gm_recovery_events WHERE query=?',(query,)).fetchone()[0]+1
    before_path=screenshot_dir/f'event_{event_number:03d}_before.png'
    after_path=screenshot_dir/f'event_{event_number:03d}_after.png'
    try: await page.screenshot(path=str(before_path),animations='disabled',timeout=9000)
    except Exception: pass
    try:
        points,mode=await asyncio.wait_for(choose_automatic_map_points(page,screenshot_dir),timeout=40)
    except (asyncio.TimeoutError, Exception) as exc:
        points,mode=[],f'marker_detection_failed: {type(exc).__name__}'
    clicked=None; heading=None; notes=[]; actually_map=False
    for x,y in points[:5]:
        try:
            heading=await open_detail_confirmed(page,x,y)
            if heading:
                clicked=(x,y);actually_map=True
                notes.append('Map pin opened: '+heading)
                break
            notes.append(f'Canvas candidate did not open detail ({x},{y})')
            # Re-enter search if an unsuccessful click displaced the feed.
            if not await feed.is_visible():
                feed=await restore_search(page,search_url,old_top)
        except Exception as exc:
            notes.append(str(exc)[:150])
    # Never invent a marker coordinate. Honest degradation, to keep scroll moving.
    if not actually_map:
        cards=page.locator(FEED).first.locator(CARD)
        if await cards.count():
            try:
                link=cards.first
                await link.click(timeout=7000)
                await page.locator('h1.DUwDvf').first.wait_for(timeout=18000)
                heading=(await page.locator('h1.DUwDvf').first.inner_text()).strip()
                mode='fallback_card_click_NOT_MAP';notes.append('Used card because no verified map marker.')
            except Exception as exc:
                notes.append('Fallback failed: '+str(exc)[:200])
    feed_ready=False
    if heading:
        try:
            feed=await restore_search(page,search_url,old_top)
            feed_ready=await feed.is_visible()
        except Exception as exc:
            notes.append('Return failed: '+str(exc)[:200])
            heading=None
    # Diagnostic ONLY: the spinner can stay mounted/visible while the feed
    # is responsive. Never wait for it, nor use its disappearance as success.
    spinner_cleared=not await spinner_visible(page) if feed_ready else None
    try: await page.screenshot(path=str(after_path),animations='disabled',timeout=9000)
    except Exception: pass
    note='; '.join(notes+[f'screenshots: {before_path.name} / {after_path.name}'])
    db.execute('''INSERT INTO gm_recovery_events(query,observed_at,reason,spinner_seconds,
                click_mode,clicked_x,clicked_y,details_opened,spinner_cleared,note)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
               (query,stamp(),reason,round(seconds,1),mode,clicked[0] if clicked else None,
                clicked[1] if clicked else None,int(bool(heading)),
                None if spinner_cleared is None else int(spinner_cleared),note))
    db.commit()
    print(f'[استعادة] الوضع={mode}, تفاصيل={bool(heading)}, رجعت القائمة={feed_ready}, '
          f'العجلة اختفت={spinner_cleared} (معلومة فقط)',flush=True)
    if not actually_map:
        print('تنبيه: لم نؤكد ضغط علامة على الخريطة؛ استُخدمت البطاقة كحل احتياطي إن أمكن.',flush=True)
    return bool(heading) and feed_ready


async def pause_before_details(seconds, links_count, discovery_status):
    """Keep the completed discovery screen open *after* collecting all links.

    No initial or per-spinner pause. The search page is left at the bottom of
    the results feed until this pause ends. Details start only afterwards.
    """
    seconds = max(0.0, float(seconds))
    if not links_count or seconds == 0:
        return
    state = ('تم التأكد من ظهور عبارة نهاية القائمة.' if discovery_status == 'end_marker'
             else 'تحذير: لم تُؤكد نهاية القائمة؛ سيتم فحص الروابط المتاحة فقط.')
    print(f'\n[فاصل قبل التفاصيل] جُمعت {links_count} روابط. {state}', flush=True)
    print(f'[فاصل قبل التفاصيل] توقف {seconds:g} ثانية بعد التمرير؛ صفحة البحث باقية مفتوحة أمامك.', flush=True)
    started = time.monotonic()
    previous = None
    while True:
        remaining = max(0.0, seconds - (time.monotonic() - started))
        if remaining == 0:
            break
        checkpoint = int(remaining + 0.999)
        if checkpoint != previous and (checkpoint == int(seconds + 0.999) or checkpoint % 10 == 0 or checkpoint <= 5):
            print(f'[فاصل قبل التفاصيل] متبقٍّ {checkpoint} ثانية...', flush=True)
            previous = checkpoint
        await asyncio.sleep(min(1.0, remaining))
    print('[فاصل قبل التفاصيل] انتهت الدقيقة؛ سأبدأ فتح الروابط في تبويبات لاستخراج التفاصيل.', flush=True)


async def discover(page,db,args,out):
    '''Keep scrolling regardless of a spinner; recover only from *measured* stagnation.

    Two things count as forward progress: a previously unseen place URL, or
    scrolling past the furthest position reached / an increased feed height.
    The spinner is diagnostics only. An absent end marker is never completion.
    '''
    q=args.query
    print('\n[1/2] بحث: '+q+' | جمع الروابط حتى نهاية القائمة المرئية',flush=True)
    if getattr(args,'test_html',None):
        await page.set_content(args.test_html)
    else:
        await page.goto('https://www.google.com/maps/?hl=ar&gl=sa',wait_until='domcontentloaded',timeout=60000)
    field=page.locator('input[name="q"]').first
    await field.wait_for(timeout=45000)
    await field.fill(q)
    await field.press('Enter')
    feed=page.locator(FEED).first
    # Wait for Google Maps results; do not pause here. The viewing pause comes AFTER scrolling.
    feed_ready=True
    feed_timeout_ms=int(max(0.1, float(getattr(args,'feed_timeout',20.0)))*1000)
    try:
        await feed.wait_for(state='visible',timeout=feed_timeout_ms)
    except PlaywrightTimeoutError:
        feed_ready=False
        print('لم تظهر قائمة النتائج بعد؛ سأعيد انتظارها قليلًا قبل إعلان عدم اكتمال البحث.',flush=True)
    search_url=page.url
    if args.ui_scale != 1:
        await page.evaluate('(z)=>document.documentElement.style.zoom = String(z)',args.ui_scale)
        await asyncio.sleep(.7)
    print(f'الشاشة مصغّرة بنسبة {int(args.ui_scale*100)}% | المتصفح: {args.width}x{args.height}',flush=True)
    if not feed_ready or not await feed.is_visible():
        # Do not pretend that an empty screen is proof of the end of results.
        try:
            await feed.wait_for(state='visible',timeout=feed_timeout_ms)
        except PlaywrightTimeoutError:
            print('لم تظهر قائمة نتائج قابلة للتمرير بعد المحاولتين. البحث غير مكتمل.',flush=True)
            try: await page.screenshot(path=str(out/'NO_FEED_AFTER_WAIT.png'),animations='disabled')
            except Exception: pass
            (out/'run_status.txt').write_text(
                'query: '+q+'\ndiscovery_status: no_feed_after_wait\n'
                +'incomplete_if_no_end_marker: True\ncompleted_at: '+stamp()+'\n',encoding='utf-8')
            return 'no_feed_after_wait'
    print('العجلة للمراقبة فقط: لا تمنع التمرير ولا تحدد نجاح الاستعادة.',flush=True)
    seen=set()
    initial=await feed_position(page)
    best_top=initial['top']
    best_height=initial['height']
    last_progress=time.monotonic()
    last_new_link=time.monotonic()
    next_recovery_after=0.0
    recoveries_without_progress=0
    spinner_since=None
    scrolls=0
    stalled_scrolls=0
    forced=False
    status='incomplete'
    end_seen=None
    max_recoveries=getattr(args,'max_recoveries',3)
    stalled_for=0.0
    while True:
        try:
            feed=page.locator(FEED).first  # refresh after every return from detail/map
            if not await feed.count() or not await feed.is_visible():
                print('القائمة اختفت؛ استعادة نتائج البحث والعودة لموضع التمرير...',flush=True)
                feed=await restore_search(page,search_url,best_top)
            cards=await cards_now(page)
            old_count=len(seen)
            fresh=0
            for card in cards:
                pid=place_id(card['url'])
                seen.add(pid)
                fresh+=int(save_card(db,q,card['title'],card['url'],getattr(args,'region',None),getattr(args,'city',None),getattr(args,'category',None)))
            new_in_round=len(seen)-old_count
            pos=await feed_position(page)
        except Exception as exc:
            status='search_restore_failed'
            print('تعذر استعادة قائمة النتائج: '+str(exc)[:190],flush=True)
            try: await page.screenshot(path=str(out/'INCOMPLETE_NO_END.png'),animations='disabled')
            except Exception: pass
            break

        progress=[]
        if new_in_round:
            progress.append('new_links')
        # A truly new scroll position after navigation, not replaying old ground.
        if pos['top']>best_top+2:
            best_top=pos['top']
            progress.append('forward_scroll')
        if pos['height']>best_height+12:
            best_height=pos['height']
            progress.append('feed_grew')
        if progress:
            last_progress=time.monotonic()
            recoveries_without_progress=0
            stalled_scrolls=0
        if new_in_round:
            last_new_link=time.monotonic()
            total=db.execute('SELECT COUNT(*) FROM gm_discovered_links WHERE query=?',(q,)).fetchone()[0]
            print(f'روابط مرئية وفريدة={len(seen)} | محفوظة للبحث={total} | جديدة={fresh}',flush=True)
        exact=await end_text(page)
        if exact:
            status='end_marker';end_seen=exact
            try:
                marker=page.locator(FEED).first.locator(END).first
                await marker.scroll_into_view_if_needed(timeout=2300)
                await page.screenshot(path=str(out/'END_OF_LIST_PROOF.png'),animations='disabled')
                (out/'end_marker_proof.txt').write_text('VERIFIED VISIBLE END MARKER\n'+exact+'\n'+stamp(),encoding='utf-8')
            except Exception as exc:
                (out/'end_marker_proof.txt').write_text('Marker visible but screenshot failed: '+str(exc)+'\n'+exact,encoding='utf-8')
            print('✓ ظهرت رسالة نهاية القائمة حرفيًا: '+repr(exact),flush=True)
            break

        # Avoid an endlessly extending skeleton feed producing zero new places forever.
        # This is time-based and does not impose any limit on scrolls or places.
        no_new_limit=float(getattr(args,'max_no_new_seconds',300))
        if no_new_limit>0 and time.monotonic()-last_new_link>=no_new_limit:
            status='no_new_places_for_too_long'
            print(f'تحذير: لم تظهر روابط فريدة جديدة لمدة {no_new_limit:g} ثانية رغم المحاولات؛ '
                  'حفظ النتائج المتاحة ووضع هذا الاستعلام كغير مكتمل، ثم الانتقال للتالي.',flush=True)
            try:await page.screenshot(path=str(out/'INCOMPLETE_NO_NEW_PLACES.png'),animations='disabled')
            except Exception:pass
            break

        # A controlled, optional diagnostic: never run automatically on normal tests.
        if args.force_recovery_test and not forced and scrolls>=2:
            forced=True
            print('[اختبار إجباري اختياري] تجربة علامة الخريطة قبل وجود تعليق...',flush=True)
            await recover(page,db,q,search_url,'forced_recovery_test',0,out)
            next_recovery_after=time.monotonic()+args.stall_seconds
            continue

        now=time.monotonic()
        busy=await spinner_visible(page)
        if busy and spinner_since is None: spinner_since=now
        if not busy: spinner_since=None
        spinner_duration=(now-spinner_since) if spinner_since is not None else 0.0
        stalled_for=now-last_progress
        # Spinner status does NOT gate progress. Require real scroll attempts too.
        if (stalled_for>=args.stall_seconds and scrolls>=2 and
            stalled_scrolls>=2 and now>=next_recovery_after):
            if recoveries_without_progress>=max_recoveries:
                status='stalled_no_end_marker'
                print(f'✗ لا روابط جديدة ولا تقدم في التمرير لمدة {stalled_for:.1f}ث؛ '
                      f'استُنفدت {max_recoveries} محاولات الاستعادة. لن ندّعي بلوغ النهاية.',flush=True)
                try: await page.screenshot(path=str(out/'INCOMPLETE_NO_END.png'),animations='disabled')
                except Exception: pass
                break
            recoveries_without_progress+=1
            attempt=recoveries_without_progress
            print(f'⚠ توقف حقيقي: روابط جديدة=0، تقدم تمرير=0، المدة={stalled_for:.1f}ث، '
                  f'العجلة={busy}، الاستعادة {attempt}/{max_recoveries}',flush=True)
            ok=await recover(page,db,q,search_url,'no_link_or_scroll_progress',spinner_duration,out)
            # Count success ONLY after the next scroll/new link, never after spinner disappears.
            print(f'اختبار ما بعد الاستعادة: عادت القائمة={ok}؛ سأحاول التمرير فورًا.',flush=True)
            next_recovery_after=time.monotonic()+args.stall_seconds
            stalled_scrolls=0
            continue

        # CRITICAL FIX: scroll EVEN IF the spinner remains visible.
        try:
            scroll=await feed_scroll(page)
        except Exception as exc:
            print('تعذر تمرير القائمة؛ سأحاول إعادة إظهار النتائج: '+str(exc)[:140],flush=True)
            try:
                await restore_search(page,search_url,best_top)
                next_recovery_after=time.monotonic()+1
            except Exception:
                status='search_restore_failed'
                break
            continue
        scrolls+=1
        jumped=scroll['newTop']>best_top+2
        height_grew=scroll['height']>best_height+12
        if jumped:
            best_top=scroll['newTop']
        if height_grew:
            best_height=scroll['height']
        if jumped or height_grew:
            last_progress=time.monotonic()
            stalled_scrolls=0
            recoveries_without_progress=0
            progress.append('scroll_advanced' if jumped else 'feed_grew_after_scroll')
        else:
            stalled_scrolls+=1
        stalled_for=time.monotonic()-last_progress
        label=','.join(progress) or 'no_new_progress'
        db.execute('''INSERT INTO gm_scroll_events(query,observed_at,iteration,before_top,after_top,
          scroll_height,client_height,spinner_visible,new_links,progress_reason,stalled_seconds,recovery_attempt)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
          (q,stamp(),scrolls,round(scroll['oldTop'],1),round(scroll['newTop'],1),
           round(scroll['height'],1),round(scroll['client'],1),int(busy),new_in_round,
           label,round(stalled_for,1),recoveries_without_progress))
        db.commit()
        print(f'[تمرير {scrolls}] {scroll["oldTop"]:.0f}→{scroll["newTop"]:.0f}px | '
              f'ارتفاع={scroll["height"]:.0f} | روابط جديدة={new_in_round} | '
              f'العجلة={"ظاهرة" if busy else "مخفية"} | '
              f'الحالة={label} | بلا تقدم={stalled_for:.1f}ث',flush=True)
        # Let async Google Maps render new cards; they are scanned at loop start.
        await asyncio.sleep(args.scroll_delay)

    total=db.execute('SELECT COUNT(*) FROM gm_discovered_links WHERE query=?',(q,)).fetchone()[0]
    (out/'run_status.txt').write_text(
        'query: '+q+'\ndiscovery_status: '+status+'\n'
        +'exact_end_text: '+repr(end_seen)+'\nlinks_for_query: '+str(total)+'\n'
        +'total_scroll_attempts: '+str(scrolls)+'\n'
        +'gm_recovery_events: '+str(db.execute('SELECT COUNT(*) FROM gm_recovery_events WHERE query=?',(q,)).fetchone()[0])+'\n'
        +'incomplete_if_no_end_marker: '+str(status!='end_marker')+'\n'
        +'completed_at: '+stamp()+'\n',encoding='utf-8')
    return status


async def first_text(page, css):
    el=page.locator(css).first
    try:
        return (await el.inner_text(timeout=1200)).strip() if await el.count() else None
    except Exception:
        return None


async def first_attr(page, css, name):
    el=page.locator(css).first
    try:
        return await el.get_attribute(name,timeout=1200) if await el.count() else None
    except Exception:
        return None


async def hero_photos(page):
    """Capture header photos BEFORE clicking another detail tab."""
    nodes=page.locator('.yaCQre img[src], .p5hId img[src]')
    try:
        return list(dict.fromkeys(await nodes.evaluate_all(
            "nodes=>nodes.map(n=>n.currentSrc||n.src).filter(u=>u && u.startsWith('http'))")))
    except Exception:return []


async def gallery_photos(page, out=None, initial_hero=None):
    """Extract lazy gallery photos; explicit diagnostics distinguish absent from failed."""
    hero=list(initial_hero or []) or await hero_photos(page)
    seen={}
    diagnostics={'hero_count':len(hero),'button_found':False,
                 'gallery_opened':False,'gallery_photos':0,'error':None,
                 'gm_scroll_events':[]}
    button=page.locator('button.Dx2nRe').filter(has_text='عرض الصور').first
    if not await button.count():
        button=page.get_by_role('button',name=re.compile('عرض الصور|View photos|See photos',re.I)).first
    if not await button.count():
        try:await button.wait_for(state='visible',timeout=6000)
        except Exception:pass
    diagnostics['button_found']=bool(await button.count())
    start=time.monotonic()
    try:
        if await button.count():
            await button.click(timeout=9000)
            gallery=page.locator('[role="main"][aria-label^="صور"], [role="main"][aria-label*="photos"]').first
            await gallery.wait_for(state='visible',timeout=15000)
            diagnostics['gallery_opened']=True
            scroll=gallery.locator('div.m6QErb.DxyBCb.kA9KIf').last
            if not await scroll.count():
                diagnostics['error']='Gallery opened but scroll container not found'
            else:
                stalled=0
                while True:
                    arr=await gallery.locator('a.MIgS0d[data-photo-index]').evaluate_all(r'''items=>items.map(a=>{
                        const layers=[...a.querySelectorAll('.gCPOGf,.aHpZye,img[src]')];
                        const urls=[];
                        for(const el of layers){
                            if(el.tagName==='IMG' && (el.currentSrc||el.src)) urls.push(el.currentSrc||el.src);
                            const bg=getComputedStyle(el).backgroundImage;
                            if(bg && bg!=='none'){
                                const m=bg.match(/url\(["']?(.*?)["']?\)/i);
                                if(m && m[1])urls.push(m[1]);
                            }
                        }
                        return {index:Number(a.dataset.photoIndex),urls:[...new Set(urls)]};
                    })''')
                    prev=len(seen)
                    for item in arr:
                        good=[u for u in item['urls'] if u.startswith('http')]
                        if good:
                            best=next((u for u in good if re.search(r'=s\d+',u)),good[0])
                            seen[item['index']]=best
                    before=await scroll.evaluate('e=>({top:e.scrollTop,max:e.scrollHeight-e.clientHeight,height:e.clientHeight})')
                    await scroll.evaluate('e=>e.scrollBy(0,Math.max(e.clientHeight*.85,260))')
                    await page.wait_for_timeout(650)
                    after=await scroll.evaluate('e=>({top:e.scrollTop,max:e.scrollHeight-e.clientHeight})')
                    diagnostics['gm_scroll_events'].append({'from':round(before['top']),
                                                          'to':round(after['top']), 'loaded':len(seen)})
                    if after['top']<=before['top']+1 and len(seen)==prev:stalled+=1
                    else:stalled=0
                    if stalled>=4 and after['top']>=after['max']-5:break
                    if stalled>=10:
                        diagnostics['error']='Gallery stalled before confirmed end';break
                    if time.monotonic()-start>240:
                        diagnostics['error']='Gallery timeout 240 seconds';break
        else:
            diagnostics['error']='View photos button not found'
    except Exception as exc:
        diagnostics['error']='Gallery error: '+str(exc)[:300]
        if out:
            out.mkdir(parents=True,exist_ok=True)
            try:await page.screenshot(path=str(out/'gallery_failure.png'),timeout=3500)
            except Exception:pass
    # Prefer gallery-size URLs, but retain a distinct cover photo when omitted.
    photos=[];photo_keys=set()
    for u in [seen[k] for k in sorted(seen)]+hero:
        key=re.sub(r'=(?:w\d+-h\d+|s\d+).*$', '',u)
        if key not in photo_keys:
            photo_keys.add(key);photos.append(u)
    diagnostics['gallery_photos']=len(seen)
    if diagnostics['gallery_opened']:
        try:
            total_slots=await page.locator('a.MIgS0d[data-photo-index]').count()
            diagnostics['gallery_slots_seen']=total_slots
            diagnostics['slots_without_images']=max(0,total_slots-len(seen))
            if total_slots>len(seen) and not diagnostics['error']:
                diagnostics['error']=f'Gallery showed {total_slots} slots, but only {len(seen)} photos loaded'
        except Exception:pass
    diagnostics['photos_saved']=len(photos)
    diagnostics['elapsed_seconds']=round(time.monotonic()-start,1)
    if out:
        out.mkdir(parents=True,exist_ok=True)
        if not photos:
            # Save a browser-side screenshot and small DOM excerpt for debugging 0-image results.
            try:await page.screenshot(path=str(out/'ZERO_IMAGES_SCREEN.png'),timeout=5000)
            except Exception:pass
            try:(out/'ZERO_IMAGES_DOM.html').write_text((await page.content())[:90000],encoding='utf-8')
            except Exception:pass
        (out/'gallery_diagnostics.json').write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'gallery_diagnostics.txt').write_text('Loaded gallery images: '+str(len(seen))+\
            '\nPhotos saved: '+str(len(photos))+\
            '\nProblem: '+str(diagnostics['error']),encoding='utf-8')
    videos=[]
    try:
        videos=await page.locator('video[src],source[src],a[href*="youtube.com"],a[href*="youtu.be"]').evaluate_all(
            'nodes=>[...new Set(nodes.map(e=>e.src||e.href).filter(Boolean))]')
    except Exception: pass
    return photos,videos,diagnostics['error'],diagnostics


async def detail_sections(page):
    sections=await page.locator('div[role="region"]').evaluate_all('''regions=>regions.map(e=>({
         label:e.getAttribute('aria-label')||'',text:(e.innerText||'').trim()
    })).filter(x=>x.label&&x.text&&x.text.length<9000)''')
    return sections[:65]


async def scrape_one(context,db,record,out,test_detail_html=None):
    page=await context.new_page()
    uid=record['source_listing_id']
    try:
        if test_detail_html:
            await page.set_content(test_detail_html, wait_until="domcontentloaded")
        else:
            await page.goto(record['url'],wait_until='domcontentloaded',timeout=65000)
        await page.locator('h1.DUwDvf').first.wait_for(timeout=25000)
        title=await first_text(page,'h1.DUwDvf') or record['title']
        original_url=page.url
        # Photos MUST be collected before the user-interface tabs remove the gallery button.
        initial_hero=await hero_photos(page)
        if not initial_hero:
            try:await page.locator('.yaCQre img[src], .p5hId img[src]').first.wait_for(timeout=6000)
            except Exception:pass
            initial_hero=await hero_photos(page)
        initial_price=(await first_attr(page,'button.fT414d[aria-label]','aria-label') or
                       await first_text(page,'.Mo9Fqf .Cbys4b') or
                       await first_text(page,'.fT414d .Cbys4b') or None)
        initial_specs=[s.strip() for s in await page.locator('li.AIsQOd .c1HoAd, .B1NF3e li').all_text_contents() if s.strip()]
        initial_phone=await first_attr(page,'[data-item-id^="phone:"]','data-item-id')
        photo_out=out/'galleries'/re.sub(r'[^\w-]','_',uid)[:35] if out else None
        photos,videos,gallery_error,gallery_diag=await gallery_photos(page,photo_out,initial_hero)
        # Navigating back to the same detail URL restores all standard property sections.
        if not test_detail_html:
            try:
                await page.goto(original_url,wait_until='domcontentloaded',timeout=65000)
                await page.locator('h1.DUwDvf').first.wait_for(timeout=25000)
                try:
                    await page.locator('button.fT414d[aria-label], [data-item-id="address"], .B1NF3e').first.wait_for(timeout=6500)
                except Exception:pass
            except Exception as exc:
                gallery_diag['return_to_details_warning']=str(exc)[:200]
        raw_price=(await first_attr(page,'button.fT414d[aria-label]','aria-label') or
                   await first_text(page,'.Mo9Fqf .Cbys4b') or
                   await first_text(page,'.fT414d .Cbys4b') or initial_price)
        price=extract_price(raw_price)
        address=await first_text(page,'[data-item-id="address"] .Io6YTe')
        if not address:
            raw=await first_attr(page,'[data-item-id="address"]','aria-label')
            address=re.sub(r'^العنوان:\s*','',raw).strip() if raw else None
        loc=parse_address(address,record['query'],record['city'] if 'city' in record.keys() else None)
        raw_phone=await first_attr(page,'[data-item-id^="phone:"]','data-item-id') or initial_phone
        phone=raw_phone.split('tel:',1)[-1] if raw_phone and 'tel:' in raw_phone else None
        if not phone:
            tel=await first_attr(page,'a[href^="tel:"]','href')
            phone=tel.removeprefix('tel:') if tel else None
        website=await first_attr(page,'a[data-item-id="authority"]','href')
        kind=await first_text(page,'button.DkEaL') or await first_text(page,'.mgr77e')
        specs=[s.strip() for s in await page.locator('li.AIsQOd .c1HoAd, .B1NF3e li').all_text_contents() if s.strip()]
        specs=list(dict.fromkeys(specs+initial_specs))
        counts=parsed_specs(specs,title)
        spec_provenance=counts.pop('_spec_provenance',{})
        rating=await first_text(page,'.F7nice span[aria-hidden="true"] span[dir="ltr"]')
        review_label=await first_attr(page,'.F7nice [aria-label*="مراجعة"]','aria-label')
        owner_desc=await first_text(page,'.SBD2Rc .VpMB0')
        plus=await first_text(page,'[data-item-id="oloc"] .Io6YTe')
        sections=await detail_sections(page)
        try:
            visible_detail_text=(await page.locator('div.m6QErb.DxyBCb.kA9KIf').first.inner_text(timeout=3500))[:30000]
        except Exception:
            visible_detail_text=None
        info_rows=await page.locator('[data-item-id]').evaluate_all('''nodes=>nodes.map(n=>({
            id:n.getAttribute('data-item-id'), label:n.getAttribute('aria-label'),
            text:(n.innerText||'').trim().slice(0,2500), href:n.getAttribute('href')
        })).filter(x=>x.text||x.href)''')
        amenities=await page.locator('span.gSamH').all_text_contents()
        all_top_text=await page.locator('h1.DUwDvf').first.locator('xpath=../..').inner_text()
        price_date=await first_text(page,'button.fT414d .QqtZTe')
        booking_links=await page.locator('a.TO5I4c[href]').evaluate_all('nodes=>nodes.map(e=>({text:e.innerText.trim(),url:e.href}))')
        outbound=[x for x in booking_links if x['url']]
        period='يومي' if raw_price and ('الليلة' in raw_price or 'يوم' in raw_price) else None
        if not period:
            m=re.search(r'(إيجار|ايجار)\s*(يومي|شهري|سنوي|أسبوعي)',title)
            if m:period=m[2]
        # Secondary tabs differ: save available data, don't manufacture missing data.
        other_tabs={}
        for button in await page.locator('button[role="tab"]').all():
            try:
                name=(await button.inner_text()).strip()
                if name in ('حول','لمحة','الأسعار'):
                    await button.click(timeout=1800)
                    await asyncio.sleep(.3)
                    other_tabs[name]=(await page.locator('div.m6QErb').first.inner_text(timeout=1200))[:9000]
            except Exception:
                pass
        data={**counts,**coords_from_url(record['url']), 'rating':rating,
              'review_count_label':review_label,'source_category':kind,'search_region':record['region'] if 'region' in record.keys() else None,
              'search_city':record['city'] if 'city' in record.keys() else None,
              'city_verified_by_address':bool(loc['city']),
              'category_requested':record['category'] if 'category' in record.keys() else None,'specs_raw':specs,
              'website':website,'address_raw':address,'plus_code':plus,
              'price_dates':price_date,'raw_price':raw_price,'booking_links':outbound,
              'information_sections':sections,'information_rows':info_rows,'about_tabs':other_tabs,
              'gallery_warning':gallery_error,'gallery_diagnostics':gallery_diag,
              'spec_field_sources':spec_provenance, 'visible_detail_text':visible_detail_text,
              'all_top_text':all_top_text}
        area_txt=next((x for x in specs if re.search(r'(?:م²|متر\s*مربع|sqm)',x,re.I)),None)
        am=re.search(r'(\d+(?:[.,]\d+)?)',arabic_digits(area_txt or ''))
        area=int(float(am[1].replace(',',''))) if am else None
        stored={
         'source':SOURCE,'source_listing_id':uid,'property_type':record['category'] if 'category' in record.keys() and record['category'] else (kind or None),
         'purpose':'إيجار' if ('category' in record.keys() and record['category']) or period or any(w in title for w in ('ايجار','إيجار','استئجار')) else None,
         'listing_url':record['url'],'title':title,'price':price,'price_text':raw_price,
         'rent_period':period,'area':area,'area_text':area_txt,
         'region':(record['region'] if 'region' in record.keys() and 'city' in record.keys() and record['city'] and loc['city']==record['city'] else None),
         'city':loc['city'],'sub_region':None,'district':loc['district'],
         'street':None,'building_number':None,'postal_code':loc['postal_code'],
         'additional_number':None,'location_link':record['url'],'phone':phone,
         'whatsapp_url':None,'description':owner_desc,
         'details_json':json.dumps(data,ensure_ascii=False),
         'features_json':json.dumps(list(dict.fromkeys(amenities)),ensure_ascii=False),
         'images_json':json.dumps(photos,ensure_ascii=False),
         'videos_json':json.dumps(videos,ensure_ascii=False),
         'advertiser_json':json.dumps({'owner_post':owner_desc},ensure_ascii=False),
         'first_seen_at':stamp(),'last_seen_at':stamp(),'is_active':1
        }
        # When retrying a partial place, do not discard useful fields and photos
        # from the first attempt just because an image/price section did not load.
        previous=db.execute('SELECT * FROM listings WHERE source=? AND source_listing_id=?',
                            (SOURCE,uid)).fetchone()
        if previous:
            for column in ('phone','district','postal_code','description','rent_period',
                           'area','area_text','price','price_text'):
                if stored.get(column) is None and previous[column] is not None:
                    stored[column]=previous[column]
            for column in ('images_json','features_json','videos_json'):
                try:old=json.loads(previous[column] or '[]')
                except (ValueError,TypeError):old=[]
                try:new=json.loads(stored[column] or '[]')
                except (ValueError,TypeError):new=[]
                if column=='images_json':
                    previous_keys=set(old)
                    merged=old+[u for u in new if u not in previous_keys]
                else:
                    merged=old+[v for v in new if v not in old]
                stored[column]=json.dumps(merged,ensure_ascii=False)
            try:old_data=json.loads(previous['details_json'] or '{}')
            except (ValueError,TypeError):old_data={}
            if isinstance(old_data,dict):
                # Preserve useful known counts when the new page omits them.
                for k in ('bedrooms','bathrooms','capacity','beds'):
                    if data.get(k) is None and old_data.get(k) is not None:
                        data[k]=old_data[k]
                stored['details_json']=json.dumps(data,ensure_ascii=False)
        cols=list(stored)
        vals=[stored[c] for c in cols]
        assignments=','.join(f'{c}=excluded.{c}' for c in cols if c not in ('source','source_listing_id','first_seen_at'))
        db.execute(f'''INSERT INTO listings({','.join(cols)}) VALUES({','.join('?' for _ in cols)})
                      ON CONFLICT(source,source_listing_id) DO UPDATE SET {assignments}''',vals)
        db.execute('UPDATE gm_discovered_links SET status=?,error=NULL,last_attempt_at=? WHERE source_listing_id=?',
                   ('partial' if not photos or gallery_error else 'done',stamp(),uid))
        db.commit()
        result_label='⚠ بيانات جزئية' if (not photos or gallery_error) else '✓'
        print(f'  {result_label} {title} | السعر={price if price is not None else "غير ظاهر"} | '
              f'الهاتف={phone or "غير ظاهر"} | الصور={len(photos)} | '
              f'غرف={counts["bedrooms"]} | حمامات={counts["bathrooms"]} | '
              f'gallery_note={gallery_error or "OK"}',flush=True)
    except Exception as exc:
        # Roll back a partially failed INSERT before recording the error. One bad
        # place must never terminate the batch or trigger the NOT NULL bug.
        try:
            db.rollback()
            db.execute('UPDATE gm_discovered_links SET status=?,error=?,last_attempt_at=? WHERE source_listing_id=?',
                       ('error',str(exc)[:700],stamp(),uid))
            db.commit()
        except Exception as log_error:
            db.rollback()
            print(f'  [DB ERROR WHILE LOGGING] {log_error}',flush=True)
        print(f'  ✗ فشل: {record["title"]}: {str(exc)[:160]}',flush=True)
    finally:
        try: await page.close()
        except Exception: pass


def export_table(db,sql,path):
    c=db.execute(sql)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow([x[0] for x in c.description]);w.writerows(c.fetchall())


