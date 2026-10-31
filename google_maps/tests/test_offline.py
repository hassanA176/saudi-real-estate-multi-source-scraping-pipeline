"""All tests are offline. They never access Google Maps or the user's database."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from maps_engine import discover, make_db, save_card, scrape_one, stamp, extract_price
from saudi_maps import build_jobs, build_place_order, open_shared_db, data_due, load_config
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parent.parent
DETAIL=r'''<!doctype html><html lang="ar"><meta charset="utf-8">
<style>.MIgS0d{display:block;height:120px}.aHpZye{width:180px;height:90px}
 #scroll{height:160px;overflow-y:auto}</style>
<h1 class="DUwDvf">One-Bedroom Chalet – Example</h1>
<div class="mgr77e">شاليه</div>
<button data-item-id="address"><span class="Io6YTe">الرياض، المملكة العربية السعودية</span></button>
<ul><li class="AIsQOd"><span class="c1HoAd">عدد الأشخاص: 5</span></li>
<li class="AIsQOd"><span class="c1HoAd">حمام واحد</span></li></ul>
<div class="yaCQre"><img src="https://example.org/cover.jpg"></div>
<button class="Dx2nRe">عرض الصور</button>
<div role="main" aria-label="صور Sample" hidden>
 <div id="scroll" class="m6QErb DxyBCb kA9KIf">
  <a class="MIgS0d" data-photo-index="0"><div class="aHpZye" style="background-image:url('https://example.org/0.jpg')"></div></a>
  <a class="MIgS0d" data-photo-index="1"><div class="aHpZye" style="background-image:url('https://example.org/1.jpg')"></div></a>
 </div></div>
<script>document.querySelector('.Dx2nRe').onclick=()=>document.querySelector('[role="main"]').hidden=false</script>
'''
SEARCH=r'''<!doctype html><html lang="ar"><meta charset="utf-8">
<style>body{margin:0}input{position:relative;z-index:9}
#feed{position:absolute;top:60px;left:0;width:340px;height:290px;overflow-y:scroll;background:#fff}
[role="article"]{display:block;height:70px}#spacer{height:850px}
</style>
<input name="q"/>
<div id="feed" role="feed">
 <div id="cards">
  <div role="article"><a class="hfpxzc" aria-label="First" href="https://www.google.com/maps/place/First/data=!19sChIJTEST001">First</a></div>
  <div role="article"><a class="hfpxzc" aria-label="Second" href="https://www.google.com/maps/place/Second/data=!19sChIJTEST002">Second</a></div>
 </div>
 <div id="spacer"></div>
 <div class="E626Jd VjANhb">Spinner that NEVER disappears</div>
 <span class="HlvSq" hidden>لقد وصلت إلى نهاية القائمة.</span>
</div>
<script>let once=false;document.getElementById('feed').onscroll=()=>{
 const top=document.getElementById('feed').scrollTop;
 if(top>75&&!once){once=true;
 document.getElementById('cards').insertAdjacentHTML('beforeend',
 '<div role="article"><a class="hfpxzc" aria-label="Third" href="https://www.google.com/maps/place/Third/data=!19sChIJTEST003">Third</a></div>')}
 if(top>510)document.querySelector('.HlvSq').hidden=false;
}</script>
'''


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
        self.db=make_db(self.path/'real_estate.db')
        self.db.execute("INSERT INTO listings(source,source_listing_id,listing_url,title,first_seen_at) VALUES('aqar','A1','https://aqar.example/1','Other source',?)",(stamp(),))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def config(self):
        c=json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
        c['locations']=json.loads((ROOT/'saudi_locations.json').read_text(encoding='utf-8'))
        return c

    def test_category_order_and_geography(self):
        c=self.config();jobs=build_jobs(c)
        self.assertEqual({r['name'] for r in c['locations']['regions']},set(r['name'] for r in c['locations']['regions']))
        self.assertEqual(len(c['locations']['regions']),13)
        self.assertEqual(jobs[0].city,'الرياض')
        self.assertEqual(jobs[1].city,'جدة')
        boundaries=[(j.category,j.number) for i,j in enumerate(jobs) if i==0 or j.category!=jobs[i-1].category]
        self.assertEqual([b[0] for b in boundaries],['شاليه','مخيم','استوديو','شقة'])
        self.assertEqual(len(jobs),len({job.query for job in jobs}))
        self.assertTrue(any(job.city is None for job in jobs),'regional coverage searches missing')
        print('PASS: 13 regions, Riyadh/Jeddah first, 4 complete category passes, duplicate-free queries')

    def test_shared_db_dedupe_and_source_isolation(self):
        self.db.close()
        c=self.config();c['db_file']=self.path/'real_estate.db';c['out']=self.path/'logs';c['out'].mkdir()
        db=open_shared_db(c)
        try:
            url='https://www.google.com/maps/place/Sample/data=!19sChIJTEST999?hl=ar'
            q='شاليهات للايجار في الرياض السعودية'
            self.assertTrue(save_card(db,q,'Sample',url,'منطقة الرياض','الرياض','شاليه'))
            self.assertFalse(save_card(db,'شاليهات للايجار في جدة السعودية','Sample',url,'منطقة مكة المكرمة','جدة','شاليه'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM gm_discovered_links').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM listings WHERE source='aqar'").fetchone()[0],1)
            self.assertEqual(len(data_due(db,q,c)),1)
            db.execute("UPDATE gm_discovered_links SET status='done' WHERE query=?",(q,));db.commit()
            self.assertEqual(len(data_due(db,q,c)),0)
            self.assertTrue(list((c['out']/'backups').glob('*.db')))
            print('PASS: shared existing DB, backup, old source untouched, cross-city dedup, no re-scrape')
        finally:
            db.close()
            self.db=sqlite3.connect(self.path/'real_estate.db')

    def test_no_price_or_phone(self):
        self.assertEqual(extract_price('SAR 1,590'),1590)
        self.assertIsNone(extract_price(None))
        self.assertIsNone(extract_price('السعر غير ظاهر'))
        print('PASS: optional price accepts NULL')

    def test_sql_error_recording_regression(self):
        url='https://www.google.com/maps/place/Error/data=!19sChIJERROR999'
        save_card(self.db,'Error city','Bad',url)
        record=self.db.execute('SELECT * FROM gm_discovered_links WHERE query=?',('Error city',)).fetchone()
        class BrokenPage:
            async def set_content(self,*a,**kw):raise ValueError('synthetic page error')
            async def close(self):pass
        class BrokenContext:
            async def new_page(self):return BrokenPage()
        asyncio.run(scrape_one(BrokenContext(),self.db,record,self.path,test_detail_html='<bad>'))
        result=self.db.execute('SELECT status,error,last_attempt_at FROM gm_discovered_links WHERE source_listing_id=?',
                               (record['source_listing_id'],)).fetchone()
        self.assertEqual(result['status'],'error')
        self.assertIn('synthetic page error',result['error'])
        self.assertIsNotNone(result['last_attempt_at'])
        print('PASS: failing property becomes error (no NOT NULL crash), batch can continue')


class BrowserTests(unittest.TestCase):
    def test_spinner_does_not_block_scroll_and_detail_with_no_price(self):
        asyncio.run(self._run())

    async def _run(self):
        from shutil import which
        c=json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)
            db=make_db(out/'offline.db')
            async with async_playwright() as p:
                launch={'headless':True}
                if which('chromium'):launch.update(executable_path=which('chromium'),args=['--no-sandbox'])
                browser=await p.chromium.launch(**launch)
                context=await browser.new_context(locale='ar-SA',viewport={'width':1280,'height':720})
                page=await context.new_page()
                try:
                    args=SimpleNamespace(query='شاليهات للايجار في الرياض السعودية',region='منطقة الرياض',city='الرياض',category='شاليه',
                            test_html=SEARCH,ui_scale=1,width=1280,height=720,feed_timeout=.3,
                            force_recovery_test=False,stall_seconds=10,max_recoveries=2,scroll_delay=.09)
                    status=await asyncio.wait_for(discover(page,db,args,out),timeout=20)
                    self.assertEqual(status,'end_marker')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM gm_discovered_links').fetchone()[0],3)
                    self.assertTrue((out/'END_OF_LIST_PROOF.png').exists())
                    # The scanner saved a known place; open the *mock* detail, with no price or phone.
                    row=db.execute('SELECT * FROM gm_discovered_links ORDER BY discovered_at LIMIT 1').fetchone()
                    await scrape_one(context,db,row,out,test_detail_html=DETAIL)
                    row=db.execute('SELECT price,phone,images_json,details_json,title FROM listings WHERE source=?',('google_maps',)).fetchone()
                    self.assertIsNone(row['price'])
                    self.assertIsNone(row['phone'])
                    self.assertGreaterEqual(len(json.loads(row['images_json'])),2)
                    self.assertEqual(json.loads(row['details_json'])['bedrooms'],1)
                    self.assertEqual(json.loads(row['details_json'])['capacity'],5)
                    self.assertEqual(db.execute("SELECT status FROM gm_discovered_links WHERE source_listing_id=?",(db.execute("SELECT source_listing_id FROM listings WHERE source='google_maps'").fetchone()[0],)).fetchone()[0],'done')
                    print('PASS: sticky spinner yet progress/end marker; photo gallery; bedroom/capacity; absent price/phone NULL')
                finally:
                    db.close();await context.close();await browser.close()


class RunnerTests(unittest.TestCase):
    def test_one_failed_city_does_not_stop_remaining_cities(self):
        from unittest.mock import patch
        from saudi_maps import run_cycle, open_shared_db
        async def exercise():
            with tempfile.TemporaryDirectory() as temp:
                out=Path(temp)
                d=make_db(out/'shared.db')
                d.close()
                c=json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
                c['locations']=json.loads((ROOT/'saudi_locations.json').read_text(encoding='utf-8'))
                c.update(db_file=out/'shared.db',out=out/'logs',
                         recycle_browser_every_queries=10,delay_between_searches_seconds=0)
                c['out'].mkdir()
                db=open_shared_db(c)
                visited=[]
                class B:
                    async def close(self):pass
                class FakePW:
                    async def __aenter__(self):return object()
                    async def __aexit__(self,*_):pass
                async def fake_new_browser(_p,_c):return B(),B()
                async def fake_run_query(_ctx,_db,_config,job,_cycle_id):
                    visited.append(job.city)
                    if job.city=='جدة':raise RuntimeError('synthetic city failure')
                    return 'end_marker'
                try:
                    with patch('saudi_maps.async_playwright',return_value=FakePW()), \
                         patch('saudi_maps.new_browser',new=fake_new_browser), \
                         patch('saudi_maps.run_query',new=fake_run_query):
                        done=await run_cycle(db,c,1,max_queries=3)
                    self.assertFalse(done)
                    self.assertEqual(visited[:2],['الرياض','جدة'])
                    self.assertEqual(len(visited),3,'third city should still run after second fails')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM gm_query_runs WHERE status=?',('query_error',)).fetchone()[0],1)
                    print('PASS: a failing city query does not stop the category sequence')
                finally:db.close()
        asyncio.run(exercise())


if __name__=='__main__':unittest.main(verbosity=2)
