"""Recurring Saudi Arabia Google Maps collector for the EXISTING shared SQLite database.

No CAPTCHA bypass, third-party credentials, or hidden API calls. A Google Maps
HTML change/network or access restriction can make an individual search incomplete;
we save that status and continue to the next query. Images are links, not downloads.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from playwright.async_api import async_playwright
from maps_engine import SOURCE, discover, scrape_one, pause_before_details, make_db, stamp, export_table

ROOT = Path(__file__).resolve().parent
CATEGORY_ORDER = ('شاليه', 'مخيم', 'استوديو', 'شقة')
EXPECTED_COLUMNS = {'source', 'source_listing_id', 'property_type', 'purpose', 'listing_url',
    'title', 'price', 'price_text', 'rent_period', 'region', 'city', 'district', 'phone',
    'details_json', 'features_json', 'images_json', 'videos_json', 'advertiser_json',
    'first_seen_at', 'last_seen_at', 'is_active'}


class TeeOutput:
    """Persist browser-engine print statements without hiding console output."""
    def __init__(self, original, logfile):
        self.original=original
        self.file=open(logfile,'a',encoding='utf-8',buffering=1)
    def write(self, value):
        self.original.write(value)
        try:self.file.write(value)
        except OSError:pass
        return len(value)
    def flush(self):
        self.original.flush()
        try:self.file.flush()
        except OSError:pass
    def close(self):
        self.file.close()


def say(message):
    text = str(message)
    print(text, flush=True)
    logging.info(text)



def load_config(path):
    config_path = Path(path).expanduser().resolve()

    c = json.loads(
        config_path.read_text(encoding='utf-8-sig')
    )

    # تحديد مجلد قاعدة البيانات أو مسار الملف مباشرة
    db_text = str(
        c.get('shared_db_path')
        or os.getenv('REAL_ESTATE_DB_PATH', '')
    ).strip()

    if not db_text:
        raise ValueError(
            'حدد مجلد قاعدة البيانات في config.json '
            'باستخدام shared_db_path.'
        )

    db_path = Path(
        os.path.expandvars(db_text)
    ).expanduser()

    # المسارات النسبية تُحسب من موقع config.json
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path

    db_path = db_path.resolve()

    # إذا كان المسار مجلدًا، نبحث داخله عن قاعدة البيانات
    if db_path.is_dir():
        db_file = db_path / 'real_estate.db'
    else:
        db_file = db_path

    # حماية قاعدة البيانات من إنشاء ملف جديد بالخطأ
    if not db_file.is_file():
        raise FileNotFoundError(
            f'قاعدة البيانات المشتركة غير موجودة: {db_file}. '
            'تأكد من اسم الملف ومكان المجلد.'
        )

    c['db_file'] = db_file
    c['config_directory'] = config_path.parent

    # تحميل ملف المناطق والمدن
    locations_file = Path(
        c.get('locations_file', 'saudi_locations.json')
    )

    if not locations_file.is_absolute():
        locations_file = config_path.parent / locations_file

    c['locations'] = json.loads(
        locations_file.read_text(encoding='utf-8-sig')
    )

    # مجلد حفظ سجلات التشغيل
    output = Path(
        c.get('output_directory', 'maps_production_logs')
    )

    if not output.is_absolute():
        output = config_path.parent / output

    output.mkdir(parents=True, exist_ok=True)

    c['out'] = output

    # التحقق من ترتيب فئات العقارات
    searches = c.get('category_searches') or {}

    if tuple(searches) != CATEGORY_ORDER:
        raise ValueError(
            'الترتيب المطلوب للفئات هو: '
            'شاليه، مخيم، استوديو، شقة.'
        )

    # التحقق من مناطق المملكة
    if len(c['locations']['regions']) != 13:
        raise ValueError(
            'ملف المناطق يجب أن يحتوي على مناطق '
            'المملكة الـ13. راجع saudi_locations.json.'
        )

    return c

def validate_existing_db(db_path):
    # Never silently create a different database when the goal is shared storage.
    with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro', uri=True, timeout=30)) as d:
        row = d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='listings'").fetchone()
        if not row:
            raise ValueError('قاعدة البيانات المحددة لا تحتوي على جدول listings؛ هذه ليست قاعدة المواقع المشتركة المطلوبة.')
        columns = {r[1] for r in d.execute('PRAGMA table_info(listings)')}
        missing = EXPECTED_COLUMNS - columns
        if missing:
            raise ValueError('جدول listings لا يطابق المخطط المتفق عليه. أعمدة مفقودة: '+', '.join(sorted(missing)))
        # Uniqueness is important to avoid accidental duplication.
        indexes = d.execute("PRAGMA index_list('listings')").fetchall()
        if not any(i[2] for i in indexes):
            raise ValueError('جدول listings بلا فهارس UNIQUE؛ راجع ملف قاعدة البيانات الصحيح.')


def open_shared_db(c):
    db_file=c['db_file']
    validate_existing_db(db_file)
    # Back up safely from SQLite, including data not yet checkpointed from WAL.
    mark_file=c['out']/('.schema_backup_'+hashlib.sha256(str(db_file).encode()).hexdigest()[:12]+'.txt')
    if not mark_file.exists():
        backups=c['out']/'backups'
        backups.mkdir(exist_ok=True)
        backup=backups/f'{db_file.stem}_before_google_maps_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
        with closing(sqlite3.connect(str(db_file), timeout=45)) as live, closing(sqlite3.connect(str(backup))) as archived:
            live.backup(archived, pages=300, sleep=0.05)
        mark_file.write_text(f'{db_file}\n{backup}\n',encoding='utf-8')
        say(f'[نسخة احتياطية لقاعدة البيانات] {backup}')
    db=make_db(db_file)
    db.execute('PRAGMA busy_timeout=45000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS scraper_state (
            source TEXT PRIMARY KEY,
            initial_collection_done INTEGER DEFAULT 0,
            last_cycle_started_at TEXT,
            last_cycle_finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gm_query_runs (
            query TEXT PRIMARY KEY, category TEXT, region TEXT, city TEXT,
            last_cycle_id TEXT, status TEXT, last_start TEXT, last_end TEXT,
            new_links INTEGER DEFAULT 0, attempted_details INTEGER DEFAULT 0,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS gm_runner_checkpoint (
            source TEXT PRIMARY KEY, cycle_id TEXT, plan_hash TEXT,
            next_job_number INTEGER NOT NULL DEFAULT 1,
            last_updated_at TEXT
        );

    ''')
    # Non-destructive upgrades if an early production release created the GM table.
    old={r['name'] for r in db.execute('PRAGMA table_info(gm_discovered_links)')}
    for name in ('region','city','category','last_seen_at','last_attempt_at'):
        if name not in old:
            db.execute(f'ALTER TABLE gm_discovered_links ADD COLUMN {name} TEXT')
    db.execute('CREATE INDEX IF NOT EXISTS idx_gm_discovery_query_status ON gm_discovered_links(query,status)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_gm_discovery_seen ON gm_discovered_links(last_seen_at)')
    db.execute('INSERT OR IGNORE INTO scraper_state(source,initial_collection_done) VALUES (?,0)',(SOURCE,))
    db.commit()
    return db


def build_place_order(c):
    """First Riyadh, then Jeddah, then every other configured city, then 13 regional searches."""
    locations=c['locations']; regions=locations['regions']
    by_city={}
    for region in regions:
        for city in region['cities']:
            if city in by_city and by_city[city] != region['name']:
                raise ValueError(f'المدينة {city} مكررة في منطقتين؛ صحّح ملف المدن.')
            by_city[city]=region['name']
    ordered=[]; used=set()
    for city in locations.get('priority_first',['الرياض','جدة']):
        if city not in by_city:
            raise ValueError(f'المدينة الأولية {city} غير موجودة في ملف المناطق')
        ordered.append((by_city[city],city));used.add(city)
    for region in regions:
        for city in region['cities']:
            if city not in used:
                ordered.append((region['name'],city));used.add(city)
    if c.get('include_region_level_queries',True):
        for region in regions:
            ordered.append((region['name'],None))
    return ordered


@dataclass(frozen=True)
class QueryJob:
    category: str
    region: str
    city: Optional[str]
    query: str
    number: int


def build_jobs(c):
    places=build_place_order(c)
    jobs=[]
    seen=set()
    for category in CATEGORY_ORDER:
        for region,city in places:
            place=f'{city} السعودية' if city else f'{region} السعودية'
            for template in c['category_searches'][category]:
                q=template.format(place=place)
                if q in seen:
                    continue
                seen.add(q)
                jobs.append(QueryJob(category,region,city,q,len(jobs)+1))
    return jobs


def data_due(db, query, c, now=None):
    now=now or datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    failure_cutoff=(now-timedelta(hours=float(c.get('retry_failed_after_hours',2)))).isoformat(timespec='seconds')
    partial_cutoff=(now-timedelta(hours=float(c.get('retry_no_images_after_hours',24)))).isoformat(timespec='seconds')
    return db.execute('''SELECT * FROM gm_discovered_links WHERE query=? AND
      (status='pending' OR
       (status='error' AND COALESCE(last_attempt_at, discovered_at)<=?) OR
       (status='partial' AND COALESCE(last_attempt_at, discovered_at)<=?))
      ORDER BY discovered_at,source_listing_id''',(query,failure_cutoff,partial_cutoff)).fetchall()


def query_dir(c, cycle_id, job):
    ident=hashlib.sha1(job.query.encode('utf-8')).hexdigest()[:8]
    dirname=f'{job.number:04d}_{job.category}_{ident}'
    directory=c['out']/'runs'/cycle_id/dirname
    directory.mkdir(parents=True,exist_ok=True)
    return directory


def write_query_run(db,job,cycle_id,status,new_links,attempted,note=''):
    db.execute('''INSERT INTO gm_query_runs(query,category,region,city,last_cycle_id,status,last_start,last_end,new_links,attempted_details,note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(query) DO UPDATE SET category=excluded.category,region=excluded.region,city=excluded.city,
        last_cycle_id=excluded.last_cycle_id,status=excluded.status,last_end=excluded.last_end,
        new_links=excluded.new_links,attempted_details=excluded.attempted_details,note=excluded.note''',
       (job.query,job.category,job.region,job.city,cycle_id,status,stamp(),stamp(),new_links,attempted,note[:400]))
    db.commit()


async def safe_screenshot(page,path):
    try: await page.screenshot(path=str(path),animations='disabled',timeout=5000)
    except Exception:pass


async def maps_access_restricted(page):
    """Only identify visible access restriction; never circumvent it."""
    try:
        url=(page.url or '').lower()
        if '/sorry/' in url or '/recaptcha/' in url:
            return True
        title=(await page.title()).lower()
        if 'unusual traffic' in title or 'recaptcha' in title:
            return True
        body=(await page.locator('body').inner_text(timeout=1800))[:12000].lower()
        return ('unusual traffic from your computer network' in body or
                'our systems have detected unusual traffic' in body or
                'تم رصد زيارات غير معتادة' in body)
    except Exception:
        return False


async def run_query(context,db,c,job,cycle_id):
    out=query_dir(c,cycle_id,job)
    say(f'\n[بحث {job.number}] الفئة={job.category} | المنطقة={job.region} | المدينة={job.city or "بحث إقليمي"}')
    say('الاستعلام: '+job.query)
    before=db.execute('SELECT COUNT(*) FROM gm_discovered_links').fetchone()[0]
    page=await context.new_page()
    state='unattempted'
    attempted=0
    note=''
    try:
        args=SimpleNamespace(query=job.query,region=job.region,city=job.city,category=job.category,
            ui_scale=float(c.get('ui_scale',.8)),width=int(c.get('window_width',1180)),
            height=int(c.get('window_height',630)),feed_timeout=float(c.get('feed_timeout_seconds',20)),
            force_recovery_test=False,stall_seconds=float(c.get('stall_seconds',12)),
            max_recoveries=int(c.get('max_recoveries',3)),scroll_delay=float(c.get('seconds_between_scrolls',.85)),
            max_no_new_seconds=float(c.get('max_seconds_without_new_links',300)))
        # Find every visible link by feed progress, not by presence of a spinner.
        try:
            state=await discover(page,db,args,out)
        except Exception as exc:
            state='discovery_error'
            note=f'{type(exc).__name__}: {exc}'
            say('خطأ أثناء البحث؛ سنحفظ الروابط الموجودة وننتقل للتفاصيل: '+note[:240])
            await safe_screenshot(page,out/'DISCOVERY_EXCEPTION.png')
            (out/'discovery_exception.txt').write_text(traceback.format_exc(),encoding='utf-8')
        if await maps_access_restricted(page):
            state='access_restricted'
            note='Google Maps displayed an access restriction; no CAPTCHA bypass attempted.'
            await safe_screenshot(page,out/'ACCESS_RESTRICTED.png')
            say('تقييد وصول ظاهر من Google؛ لن تتم محاولة تجاوز الحماية. سيتم الانتظار قبل متابعة البحث.')
        after=db.execute('SELECT COUNT(*) FROM gm_discovered_links').fetchone()[0]
        new_links=after-before
        say(f'انتهى جمع الروابط لهذه المدينة: الحالة={state} | الجديدة على مستوى المملكة={new_links}')
        # Show final map results AFTER scrolling, BEFORE opening detail tabs.
        # If an exception displaced the search page, no misleading pause/proof is claimed.
        show_pause = c.get('pause_mode', 'first_query_only')=='every_query' or (c.get('pause_mode', 'first_query_only')=='first_query_only' and job.number==1)
        if state == 'end_marker' and show_pause:
            count=db.execute('SELECT COUNT(*) FROM gm_discovered_links WHERE query=?',(job.query,)).fetchone()[0]
            await pause_before_details(float(c.get('pause_after_search_seconds',60)),count,state)
        items=[] if state=='access_restricted' else data_due(db,job.query,c)
        if items:
            say(f'[تفاصيل جديدة أو مستحقة لإعادة المحاولة] {len(items)}')
        try:await page.close()
        except Exception:pass
        # One browser tab per place, opened and closed by scrape_one.
        for k,item in enumerate(items,1):
            attempted+=1
            say(f'[{k}/{len(items)}] {item["title"] or item["url"]}')
            try:
                await asyncio.wait_for(scrape_one(context,db,item,out),timeout=480)
            except Exception as exc:
                # This also handles an external browser crash, DB errors in the extractor,
                # and timeouts; never fail the whole city because of one place.
                db.rollback()
                try:
                    db.execute('''UPDATE gm_discovered_links SET status=?,error=?,last_attempt_at=?
                        WHERE source_listing_id=?''',('error',f'{type(exc).__name__}: {exc}'[:700],stamp(),item['source_listing_id']))
                    db.commit()
                except Exception as db_exc:
                    db.rollback();say(f'تعذّر تسجيل خطأ العقار في قاعدة البيانات: {db_exc}')
                say(f'خطأ العقار، الانتقال للتالي: {type(exc).__name__}: {str(exc)[:190]}')
                (out/'detail_errors.txt').open('a',encoding='utf-8').write(f'{stamp()} | {item["url"]} | {exc}\n')
            delay=float(c.get('delay_between_details_seconds',1.5))
            if delay>0:await asyncio.sleep(delay)
        try:
            write_query_run(db,job,cycle_id,state,new_links,attempted,note)
            export_table(db,"SELECT * FROM gm_discovered_links WHERE query='"+job.query.replace("'","''")+"'",out/'discovered_links.csv')
            export_table(db,"SELECT * FROM gm_scroll_events WHERE query='"+job.query.replace("'","''")+"'",out/'scroll_events.csv')
            export_table(db,"SELECT * FROM gm_recovery_events WHERE query='"+job.query.replace("'","''")+"'",out/'recovery_events.csv')
        except Exception as exc:
            say('تعذر تصدير تقرير هذه المدينة فقط: '+str(exc)[:200])
        return state
    finally:
        try:await page.close()
        except Exception:pass


async def new_browser(playwright,c):
    width=int(c.get('window_width',1180)); height=int(c.get('window_height',630))
    browser=await playwright.chromium.launch(headless=bool(c.get('headless',False)),
                     args=[f'--window-size={width},{height+80}'])
    context=await browser.new_context(locale='ar-SA',timezone_id='Asia/Riyadh',
          viewport={'width':width,'height':height},device_scale_factor=1)
    context.set_default_timeout(16000)
    return browser,context


async def sleep_with_log(seconds,why):
    until=time.monotonic()+float(seconds)
    while True:
        remain=until-time.monotonic()
        if remain<=0:break
        if int(remain)%300==0 or remain<60:
            say(f'{why} | متبقٍ نحو {int(remain)} ثانية')
        await asyncio.sleep(min(60,remain))


def export_cycle(db,c,cycle_id):
    target=c['out']/'summaries'/cycle_id
    target.mkdir(parents=True,exist_ok=True)
    export_table(db,"SELECT * FROM listings WHERE source='google_maps'",target/'all_google_maps_listings.csv')
    export_table(db,'SELECT * FROM gm_discovered_links',target/'all_discovered_links.csv')
    export_table(db,'SELECT * FROM gm_query_runs',target/'query_status.csv')
    statuses=db.execute('SELECT status,COUNT(*) FROM gm_discovered_links GROUP BY status').fetchall()
    summary={'cycle_id':cycle_id,'summary_time':stamp(),
             'saved_listings':db.execute("SELECT COUNT(*) FROM listings WHERE source='google_maps'").fetchone()[0],
             'discovered_links':db.execute('SELECT COUNT(*) FROM gm_discovered_links').fetchone()[0],
             'discovery_statuses':{s[0]:s[1] for s in statuses}}
    (target/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    say('ملخص الدورة: '+json.dumps(summary,ensure_ascii=False))


async def run_cycle(db,c,cycle_no,once=False,max_queries=0):
    new_cycle_id=datetime.now(timezone(timedelta(hours=3))).strftime('%Y%m%d_%H%M%S')+f'_c{cycle_no:03d}'
    jobs=build_jobs(c)
    # Checkpoint only full production sweeps, not diagnostic --once / --max-queries runs.
    checkpoint_enabled=(max_queries==0 and bool(c.get('enable_checkpoint',True)))
    plan_hash=hashlib.sha256('\n'.join(j.query for j in jobs).encode()).hexdigest()
    cycle_id=new_cycle_id
    start_number=1
    if checkpoint_enabled:
        prior=db.execute('SELECT * FROM gm_runner_checkpoint WHERE source=?',(SOURCE,)).fetchone()
        if (prior and prior['plan_hash']==plan_hash and prior['cycle_id'] and
            1<=prior['next_job_number']<=len(jobs)):
            cycle_id=prior['cycle_id']
            start_number=prior['next_job_number']
            say(f'[استكمال بعد إعادة التشغيل] الدورة {cycle_id}، بدءًا من الاستعلام {start_number}')
        else:
            db.execute('''INSERT INTO gm_runner_checkpoint(source,cycle_id,plan_hash,next_job_number,last_updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cycle_id=excluded.cycle_id,
                plan_hash=excluded.plan_hash,next_job_number=1,last_updated_at=excluded.last_updated_at''',
                (SOURCE,cycle_id,plan_hash,1,stamp()))
            db.commit()
    if max_queries>0:jobs=jobs[:max_queries]
    total=len(jobs)
    this_pass=jobs[start_number-1:]
    say(f'\n========== بداية الدورة {cycle_id} ==========')
    say(f'المدن والمناطق: {len(build_place_order(c))} | إجمالي الاستعلامات: {total} | المتبقية الآن: {len(this_pass)} | الفئات: '+', '.join(CATEGORY_ORDER))
    db.execute('UPDATE scraper_state SET last_cycle_started_at=? WHERE source=?',(stamp(),SOURCE));db.commit()
    errors=0
    successful_end_markers=0
    cut_short_for_access=False
    async with async_playwright() as p:
        browser=None; context=None; used=0
        try:
            for job in this_pass:
                try:
                    if context is None or used>=int(c.get('recycle_browser_every_queries',12)):
                        if context:
                            try:await context.close()
                            except Exception:pass
                        if browser:
                            try:await browser.close()
                            except Exception:pass
                        browser,context=await new_browser(p,c)
                        used=0
                    used+=1
                    state=await run_query(context,db,c,job,cycle_id)
                    successful_end_markers+=int(state=='end_marker')
                    if state=='access_restricted':
                        cut_short_for_access=True
                except Exception as exc:
                    errors+=1
                    say(f'خطأ كامل في الاستعلام {job.query}؛ متابعة التالي: {exc}')
                    target=query_dir(c,cycle_id,job)
                    (target/'QUERY_ERROR.txt').write_text(traceback.format_exc(),encoding='utf-8')
                    try:write_query_run(db,job,cycle_id,'query_error',0,0,str(exc))
                    except Exception:db.rollback()
                    if context:
                        try:await context.close()
                        except Exception:pass
                    if browser:
                        try:await browser.close()
                        except Exception:pass
                    context=browser=None
                    # External site or network may be temporarily unavailable;
                    # wait briefly, but never fake a completed search.
                    await asyncio.sleep(5)
                finally:
                    if checkpoint_enabled:
                        try:
                            next_no=job.number if cut_short_for_access else job.number+1
                            db.execute('''UPDATE gm_runner_checkpoint SET next_job_number=?,last_updated_at=?
                                WHERE source=?''',(next_no,stamp(),SOURCE))
                            db.commit()
                        except Exception:db.rollback()
                if cut_short_for_access:
                    say('تم تعليق بقية استعلامات هذه الدورة مؤقتًا بسبب تقييد الوصول؛ ستحاول الدورة التالية استكمال الاستعلام نفسه.')
                    break
                between=float(c.get('delay_between_searches_seconds',3.0))
                if between>0:await asyncio.sleep(between)
        finally:
            if context:
                try:await context.close()
                except Exception:pass
            if browser:
                try:await browser.close()
                except Exception:pass
    # When resuming, prior query end statuses come from the same cycle's run records.
    if checkpoint_enabled:
        completed_set={r[0] for r in db.execute('SELECT query FROM gm_query_runs WHERE last_cycle_id=? AND status=?',
                                                (cycle_id,'end_marker')).fetchall()}
        successful_end_markers=len(completed_set)
    complete=(len(jobs)>0 and successful_end_markers==len(jobs) and max_queries==0)
    if checkpoint_enabled and not cut_short_for_access:
        db.execute('''UPDATE gm_runner_checkpoint SET cycle_id=NULL,next_job_number=1,
            last_updated_at=? WHERE source=?''',(stamp(),SOURCE))
        db.commit()
    db.execute('''UPDATE scraper_state SET last_cycle_finished_at=?,
        initial_collection_done=CASE WHEN ? THEN 1 ELSE initial_collection_done END
        WHERE source=?''',(stamp(),int(complete),SOURCE))
    db.commit()
    try:export_cycle(db,c,cycle_id)
    except Exception as exc:say(f'تعذر تصدير ملفات CSV لهذه الدورة: {exc}')
    say(f'========== نهاية الدورة: {successful_end_markers}/{total} بلغوا نهاية القائمة، أخطاء عامة={errors} ==========')
    return complete


async def main():
    a=argparse.ArgumentParser(description='Persistent all-Saudi Maps scraper, writes only to existing shared DB')
    a.add_argument('--config',default=str(ROOT/'config.json'))
    a.add_argument('--once',action='store_true',help='Run only one configured sweep, then exit')
    a.add_argument('--max-queries',type=int,default=0,help='Diagnostic: restrict this run to first N queries; 0 means all')
    a.add_argument('--skip-pause',action='store_true',help='Diagnostic only: skip the 60s after-search viewing pause')
    args=a.parse_args()
    try:
        c=load_config(args.config)
        validate_existing_db(c['db_file'])
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        print(f'خطأ إعدادات يتطلب تدخلك قبل التشغيل: {exc}',flush=True)
        raise SystemExit(2)
    logfile=c['out']/'saudi_maps.log'
    logging.basicConfig(filename=str(logfile),level=logging.INFO,format='%(asctime)s %(levelname)s: %(message)s',encoding='utf-8')
    sys.stdout=TeeOutput(sys.stdout,c['out']/'console_output.log')
    if args.skip_pause:c['pause_after_search_seconds']=0
    say('قاعدة البيانات المشتركة: '+str(c['db_file']))
    say('ملف السجل: '+str(logfile))
    cycle_no=0
    while True:
        db=None
        cycle_failed=False
        try:
            db=open_shared_db(c)
            cycle_no+=1
            await run_cycle(db,c,cycle_no,max_queries=max(0,args.max_queries))
        except (KeyboardInterrupt, asyncio.CancelledError):
            say('توقف يدوي؛ تمت المحافظة على السجلات المحفوظة.')
            raise
        except Exception as exc:
            say(f'[خطأ دورة] سيعاد التشغيل تلقائيًا؛ {type(exc).__name__}: {exc}')
            logging.error(traceback.format_exc())
            cycle_failed=True
        finally:
            if db:
                try:db.close()
                except Exception:pass
        if args.once:return
        if cycle_failed:
            say('الدورة انهارت بخطأ عام؛ إعادة المحاولة بعد 60 ثانية، دون الانتظار نصف ساعة.')
            await sleep_with_log(60,'إعادة التشغيل بعد الخطأ')
            continue
        wait=max(0,int(c.get('pause_after_full_cycle_seconds',1800)))
        say(f'انتهت الدورة. الانتظار {wait//60} دقيقة ثم العودة إلى الرياض / الشاليهات.')
        await sleep_with_log(wait,'انتظار الدورة التالية')


if __name__=='__main__':
    initial_stdout=sys.stdout
    try:asyncio.run(main())
    except KeyboardInterrupt:print('\nتم الإيقاف يدويًا؛ يمكنك تشغيله لاحقًا لاستكمال الاكتشاف.',flush=True)
    finally:
        if sys.stdout is not initial_stdout:
            stream=sys.stdout
            sys.stdout=initial_stdout
            stream.close()

