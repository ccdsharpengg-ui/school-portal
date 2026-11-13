#!/usr/bin/env python3
"""School transport and parent portal: one file, standard library only.

Run:   python app.py            (opens on http://localhost:8000)
Env:   ADMIN_PASSWORD, PORT, DB_PATH
Flags: --no-seed  (start with an empty database instead of the fictional demo school)
"""
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "school.db"))
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
TOKEN = hmac.new(b"school-portal", ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()
MAX_BODY = 100_000

# Server-side rules for people who are not signed in as admin.
ADMIN_ONLY = {"students", "school"}                      # no writes at all
CREATE_ADMIN_ONLY = {"vehicles": {"loc", "delay"},       # may only change these fields
                     "fees": {"paid", "rcpt", "ts"}}


def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS docs(coll TEXT, id TEXT, data TEXT, PRIMARY KEY(coll, id))")
    return c


def init_db():
    with closing(conn()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.commit()


def read_all(coll):
    with closing(conn()) as c:
        rows = c.execute("SELECT id, data FROM docs WHERE coll=?", (coll,)).fetchall()
    return [{"id": i, **json.loads(d)} for i, d in rows]


def read_one(coll, doc_id):
    with closing(conn()) as c:
        r = c.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, doc_id)).fetchone()
    return {"id": doc_id, **json.loads(r[0])} if r else None


def write(coll, doc_id, data):
    data = {k: v for k, v in data.items() if k != "id"}
    with closing(conn()) as c:
        c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?)", (coll, doc_id, json.dumps(data)))
        c.commit()


def query(coll, qs):
    docs = read_all(coll)
    for field, value in json.loads(qs.get("where", ["[]"])[0]):
        docs = [d for d in docs if d.get(field) == value]
    if "order" in qs:
        f, rev = qs["order"][0], qs.get("dir", ["asc"])[0] == "desc"
        docs.sort(key=lambda d: (d.get(f) is None, d.get(f)), reverse=rev)
    if "limit" in qs:
        docs = docs[: int(qs["limit"][0])]
    return docs


def seed():
    if read_all("students"):
        return
    items = {
        "school": {"main": {"name": "Greenfield Public School (demo)", "staff": "STAFF-5B"}},
        "vehicles": {
            "v1": {"t": "School bus", "no": "DL1PC 2203", "d": "Suresh Yadav", "exp": "2027-03-31", "code": "V-2203"},
            "v2": {"t": "Van", "no": "DL3CA 4821", "d": "Ramesh Kumar", "exp": "2026-11-24", "code": "V-4821"},
            "v3": {"t": "E-rickshaw", "no": "DL1RE 9087", "d": "Imran Khan", "exp": "2027-02-15", "code": "V-9087"}},
        "students": {
            "s1": {"n": "Aarav Sharma", "c": "5-B", "v": "v2", "code": "AAR-4K7", "pin": "4821"},
            "s2": {"n": "Meera Nair", "c": "5-B", "v": "v2", "code": "MEE-2P9", "pin": "3150"},
            "s3": {"n": "Kabir Singh", "c": "5-B", "v": "v2", "code": "KAB-8M3", "pin": "7726"},
            "s4": {"n": "Diya Rao", "c": "5-B", "v": "v1", "code": "DIY-5T6", "pin": "9042"},
            "s5": {"n": "Rohan Das", "c": "5-B", "v": "v3", "code": "ROH-7Q2", "pin": "6618"}},
        "fees": {f"f{i}": {"sid": f"s{i}", "m": "November", "amt": a, "paid": 0}
                 for i, a in enumerate([1200, 1200, 1200, 1500, 900], 1)},
        "homework": {"h1": {"c": "5-B", "s": "Maths", "d": "Fri 13 Nov",
                            "x": "Practice sheet 4, questions 1 to 10", "ts": int(time.time() * 1000)}},
    }
    for coll, docs in items.items():
        for doc_id, data in docs.items():
            write(coll, doc_id, data)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def is_admin(self):
        return hmac.compare_digest(self.headers.get("X-Admin-Token", ""), TOKEN)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Request too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def allowed(self, method, coll, data):
        if self.is_admin():
            return True
        if coll in ADMIN_ONLY:
            return False
        if coll in CREATE_ADMIN_ONLY:
            return method == "PATCH" and set(data) <= CREATE_ADMIN_ONLY[coll]
        return True

    def static(self, name):
        if name in ("/", "/index.html"):
            page = INDEX_HTML
            page = page.replace("</head>", PWA_HEAD + '<script src="/shim.js"></script></head>', 1)
            return self.reply(200, page.encode(), "text/html; charset=utf-8")
        if name == "/favicon.ico":
            name = "/icon-192.png"
        if name == "/manifest.webmanifest":
            return self.reply(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if name == "/sw.js":
            return self.reply(200, SW_JS.encode(), "text/javascript; charset=utf-8")
        if name in ICONS:
            f = ROOT / name[1:]
            if f.is_file():
                return self.reply(200, f.read_bytes(), "image/png")
        if name == "/shim.js":
            return self.reply(200, SHIM_JS.encode(), "text/javascript; charset=utf-8")
        self.reply(404, {"error": "Not found"})

    def api(self, method):
        url = urlparse(self.path)
        if url.path == "/api/login" and method == "POST":
            ok = hmac.compare_digest(self.body().get("password", ""), ADMIN_PASSWORD)
            return self.reply(200, {"token": TOKEN}) if ok else self.reply(401, {"error": "Wrong password"})
        parts = url.path.split("/")  # ['', 'api', 'c', '<collection>', '<id>?']
        if len(parts) < 4 or parts[2] != "c":
            return self.reply(404, {"error": "Not found"})
        coll = unquote(parts[3])
        doc_id = unquote(parts[4]) if len(parts) > 4 and parts[4] else None
        if method == "GET":
            if doc_id:
                d = read_one(coll, doc_id)
                return self.reply(200, d or {"missing": True})
            return self.reply(200, {"docs": query(coll, parse_qs(url.query))})
        data = self.body()
        if not self.allowed(method, coll, data):
            return self.reply(403, {"error": "Only the school admin can do that"})
        if method == "POST" and not doc_id:
            doc_id = uuid.uuid4().hex[:12]
            write(coll, doc_id, data)
        elif method == "PUT" and doc_id:
            write(coll, doc_id, data)
        elif method == "PATCH" and doc_id:
            current = read_one(coll, doc_id)
            if not current:
                return self.reply(404, {"error": "Not found"})
            write(coll, doc_id, {**current, **data})
        else:
            return self.reply(400, {"error": "Bad request"})
        self.reply(200, {"id": doc_id})

    def handle_any(self, method):
        try:
            if self.path.startswith("/api/"):
                self.api(method)
            elif method == "GET":
                self.static(urlparse(self.path).path)
            else:
                self.reply(405, {"error": "Method not allowed"})
        except (ValueError, json.JSONDecodeError) as e:
            self.reply(400, {"error": str(e)})
        except Exception:
            self.reply(500, {"error": "Server error"})

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_PUT = lambda self: self.handle_any("PUT")
    do_PATCH = lambda self: self.handle_any("PATCH")


# --- Installable app (PWA) pieces: manifest, icons, service worker ---
ICONS = {"/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"}
MANIFEST = {
    "id": "/",
    "name": "School Transport Portal",
    "short_name": "School Bus",
    "description": "School transport tracking and parent communication portal.",
    "lang": "en",
    "dir": "ltr",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#f2f4f8",
    "theme_color": "#14213d",
    "categories": ["education", "productivity"],
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}
PWA_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<meta name="theme-color" content="#14213d">'
    '<link rel="icon" href="/icon-192.png">'
    '<link rel="apple-touch-icon" href="/icon-192.png">'
    "<script>if('serviceWorker' in navigator)addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}))</script>"
)
SW_JS = r"""// Keeps a copy of the app screen so it still opens with no signal. Live data always comes from the server.
const CACHE = 'school-portal-v1';
const SHELL = ['/', '/shim.js', '/icon-192.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return r;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
"""


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>School Transport Portal</title>
<style>
:root{color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);
--bg:#f2f4f8;--card:#fff;--ink:#14213d;--mut:#5b6577;--line:#dde2ea;--pri:#14213d;--prit:#fff;--acc:#f5b700;--acct:#14213d;--ok:#17703f;--okbg:#e0f3e8;--w:#9a5200;--wbg:#fdeccf;--b:#b3261e;--bbg:#fbe4e1}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0d1424;--card:#162036;--ink:#eaf0fb;--mut:#9aa6bd;--line:#26324c;--pri:#f5b700;--prit:#14213d;--ok:#5fd18e;--okbg:#14352a;--w:#f0a95a;--wbg:#3a2a16;--b:#f2877f;--bbg:#3a1d1a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header,main{max-width:720px;margin:0 auto;padding:12px 14px}
h1{font-size:18px;margin:0}h3{font-size:16px;margin:0}p{margin:8px 0}
.note{background:var(--wbg);color:var(--w);border-radius:8px;padding:6px 10px;font-size:13px;margin:8px 0}
.seg{display:flex;gap:6px;margin:8px 0;overflow-x:auto}
.seg button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:99px;padding:7px 14px;cursor:pointer;white-space:nowrap}
.seg button.on{background:var(--acc);border-color:var(--acc);color:var(--acct);font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}.card.bad{border-color:var(--b)}
.mut,small{color:var(--mut)}small{display:block;font-size:13px}
.btn{background:var(--pri);color:var(--prit);border:0;border-radius:9px;padding:9px 14px;font:inherit;font-weight:600;cursor:pointer}
.btn.o{background:none;color:var(--ink);border:1px solid var(--line)}
.lk{background:none;border:0;color:var(--mut);text-decoration:underline;font:inherit;padding:6px 0;cursor:pointer}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
.chip{display:inline-block;font-size:12px;padding:2px 9px;border-radius:99px;background:var(--line);color:var(--ink);white-space:nowrap}
.chip.ok{background:var(--okbg);color:var(--ok)}.chip.w{background:var(--wbg);color:var(--w)}.chip.b{background:var(--bbg);color:var(--b)}
.rw{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}.rw.l{justify-content:flex-start}
.st{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
.st i{width:14px;height:14px;border-radius:50%;border:2px solid var(--line);margin-top:4px;flex:none}
.st.on i{background:var(--ok);border-color:var(--ok)}.st.bad i{background:var(--b);border-color:var(--b)}
input,select{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);min-width:0;max-width:100%}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.tiles div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;font-size:12px;color:var(--mut)}.tiles b{display:block;font-size:22px;color:var(--ink)}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}th{border-top:0;color:var(--mut);font-weight:600}
ul{margin:6px 0;padding-left:18px}li{margin:3px 0}a{color:var(--ink)}
#toast{position:fixed;left:50%;bottom:calc(16px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--pri);color:var(--prit);padding:9px 14px;border-radius:9px;opacity:0;pointer-events:none;transition:opacity .2s;max-width:90%;text-align:center}#toast.show{opacity:1}
</style></head>
<body>
<header><div class="rw"><h1 id="sn">School transport and parent portal</h1><div id="who"></div></div></header>
<main id="app"><p class="mut">Loading…</p></main>
<div id="toast" role="status"></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today=new Date().toLocaleDateString('en-CA');
const T=()=>new Date().toLocaleTimeString('en-IN',{hour:'numeric',minute:'2-digit',hour12:true});
const D={students:[],vehicles:[],days:{},hw:[],rm:[],fees:[],log:[],school:{}};
let db,user,uid,owner=0,me={},S={pt:'today',cls:null,pick:null},pend=0,ready=0,err='',gw=null,mv=0,lw=0;
const LAB={parent:'Parent',driver:'Driver',teacher:'Teacher',admin:'School admin'};
const toast=m=>{const e=$('#toast');e.textContent=m;e.className='show';clearTimeout(toast.h);toast.h=setTimeout(()=>e.className='',3200)};
const chip=(t,c='')=>`<span class="chip ${c}">${t}</span>`;
const btn=(t,c,k='')=>`<button class="btn ${k}" onclick="${c}">${t}</button>`;
const inp=(id,ph,x='')=>`<input id="${id}" placeholder="${ph}" ${x}>`;
const nm=id=>(D.students.find(x=>x.id===id)||{n:'student'}).n;
const vh=id=>D.vehicles.find(x=>x.id===id)||null;
const day=s=>D.days[s]||{};
const ev=(by,x={})=>({by,t:T(),ts:Date.now(),...x});
const ago=ts=>{const s=Math.round((Date.now()-ts)/1000);return s<90?s+' seconds ago':Math.round(s/60)+' minutes ago'};
const dl=e=>Math.ceil((new Date(e)-Date.now())/864e5);
const trip=d=>(d.am&&!d.sch)||(d.pm&&!d.home);
const W=async f=>{try{await f()}catch(e){toast('Could not save: '+(e.message||e.code||'no write access')+'. Check your connection, or sign in as admin if this is a school-only change.')}};
const log=(who,txt)=>db.collection('log').add({ts:Date.now(),who,txt});
const setDay=(s,p)=>db.collection('days').doc(today+'_'+s).set({...day(s),date:today,sid:s,...p});
const still=()=>{if(mv>=2){toast('The vehicle is moving. Record events when stopped.');return 0}return 1};
function saveMe(o){me=o;if(db)W(()=>db.collection('data/users/'+uid).doc('me').set(o));draw()}
const switchRole=()=>saveMe({...me,role:null});
const pick=r=>{S.pick=r;draw()};
function join(){const c=($('#code').value||'').trim().toUpperCase(),p=S.pick,u=x=>(x||'').toUpperCase()===c&&c;
 if(p==='parent'){const s=D.students.find(x=>u(x.code));if(!s)return toast('No child matches that code. Check it with the school.');saveMe({role:'parent',sids:[...new Set([...(me.sids||[]),s.id])]})}
 if(p==='driver'){const v=D.vehicles.find(x=>u(x.code));if(!v)return toast('No vehicle matches that code.');saveMe({role:'driver',vid:v.id})}
 if(p==='teacher'){if(!u(D.school.staff))return toast('Staff code does not match.');saveMe({...me,role:'teacher'})}}
const addKid=()=>{S.pick='parent';join()};
const act={
 plan:(s,v)=>W(async()=>{await setDay(s,{plan:v});log('Parent',`marked ${nm(s)} ${v?'travelling':'not travelling'} today`)}),
 board:s=>W(async()=>{await setDay(s,{plan:1,am:ev('Parent')});log('Parent',`reported ${nm(s)} boarded (morning)`)}),
 sch:s=>still()&&W(async()=>{await setDay(s,{sch:ev('Driver')});log('Driver',`recorded ${nm(s)} dropped at school`)}),
 pm:s=>still()&&W(async()=>{await setDay(s,{pm:ev('Driver')});log('Driver',`recorded ${nm(s)} boarded for the home trip`)}),
 home:s=>{if(!still())return;if(($('#pin'+s).value||'').trim()!==(D.students.find(x=>x.id===s).pin||''))return toast('PIN does not match the guardian’s PIN.');W(async()=>{await setDay(s,{home:ev('Driver',{ok:1})});log('Driver',`home drop for ${nm(s)}, guardian PIN confirmed`)})},
 exc:s=>still()&&W(async()=>{await setDay(s,{home:ev('Driver',{ok:0})});log('Driver',`could not confirm handoff for ${nm(s)}, exception raised`)}),
 delay:v=>W(async()=>{await db.collection('vehicles').doc(v).update({delay:Date.now()});log('Driver',`reported a delay on ${vh(v).no}`);toast('Delay reported to the school and parents.')}),
 att:(s,st)=>{const d=day(s).att;let r='';if(d&&d.s!==st){r=($('#why').value||'').trim();if(!r)return toast('Add a reason to correct a saved record.')}else if(d)return;
  W(async()=>{await setDay(s,{att:{s:st,by:'Teacher',t:T(),ts:Date.now()}});log('Teacher',r?`corrected ${nm(s)} from ${d.s} to ${st} (${r})`:`marked ${nm(s)} ${st}`)})},
 hw:c=>{const x=($('#hx').value||'').trim();if(!x)return toast('Describe the task before posting.');W(async()=>{await db.collection('homework').add({c,s:$('#hs').value,x,d:$('#hd').value,ts:Date.now()});log('Teacher',`posted ${$('#hs').value} homework for ${c}`)})},
 rem:()=>{const x=($('#rx').value||'').trim();if(!x)return toast('Write the remark first.');W(async()=>{await db.collection('remarks').add({sid:$('#rk').value,x,ts:Date.now()});log('Teacher',`added a private remark for ${nm($('#rk').value)}`)})},
 pay:f=>W(async()=>{await db.collection('fees').doc(f).update({paid:1,rcpt:'RCPT-'+Math.floor(1000+Math.random()*9000),ts:Date.now()});log('Parent','paid a transport fee (sandbox)')}),
 rev:v=>W(async()=>{const x=vh(v);await db.collection('vehicles').doc(v).update({revoked:!x.revoked});log('School',`${x.revoked?'restored':'removed'} ${x.d} (${x.no}) for its students`)}),
 school:()=>W(()=>db.doc('school/main').set({name:$('#sname').value.trim(),staff:$('#sstaff').value.trim()})),
 addV:()=>{const no=($('#vno').value||'').trim().toUpperCase(),d=($('#vd').value||'').trim();if(!no||!d)return toast('Enter the vehicle number and driver name.');W(async()=>{await db.collection('vehicles').add({t:$('#vt').value,no,d,exp:$('#ve').value,code:('V-'+no.replace(/\s/g,'').slice(-4))});log('School',`added ${$('#vt').value} ${no}`)})},
 addS:()=>{const n=($('#sn1').value||'').trim(),c=($('#sc').value||'').trim().toUpperCase();if(!n||!c)return toast('Enter the child’s name and class.');const code=n.slice(0,3).toUpperCase()+'-'+Math.random().toString(36).slice(2,5).toUpperCase(),pin=String(Math.floor(1000+Math.random()*9000));
  W(async()=>{const r=await db.collection('students').add({n,c,v:$('#sv').value,code,pin});await db.collection('fees').add({sid:r.id,m:'This month',amt:+$('#sf').value||0,paid:0});log('School',`added student ${n}`)})}
};
const go=(n,...a)=>act[n](...a);
function share(vid){if(gw!==null){navigator.geolocation.clearWatch(gw);gw=null;draw();return}
 if(!navigator.geolocation)return toast('This device cannot share location.');
 gw=navigator.geolocation.watchPosition(p=>{mv=p.coords.speed||0;if(Date.now()-lw<10000)return;lw=Date.now();W(()=>db.collection('vehicles').doc(vid).update({loc:{lat:p.coords.latitude,lng:p.coords.longitude,acc:Math.round(p.coords.accuracy),ts:Date.now()}}))},()=>{gw=null;toast('Location is blocked here. Allow location access in the browser and try again.');draw()},{enableHighAccuracy:true,maximumAge:5000});draw()}
const row=(l,e,w)=>`<div class="st ${e?(e.ok===0?'bad':'on'):''}"><i></i><div><b>${l}</b><small>${e?`Reported by ${e.by}, ${e.t}${e.ok===0?'. Handoff not confirmed, school notified.':e.ok?'. Guardian PIN confirmed.':''}`:w}</small></div></div>`;

function locBlock(v,d){if(!trip(d))return'<p class="mut">Location sharing is off outside active trips.</p>';
 if(!v||!v.loc)return`<p>${chip('Waiting for driver','w')}</p><small>The driver has not started sharing the vehicle location.</small>`;
 const fresh=Date.now()-v.loc.ts<120000;
 return`<p>${fresh?chip('Updated '+ago(v.loc.ts),'ok'):chip('Signal lost, last update '+ago(v.loc.ts),'w')}</p><p><a href="https://www.google.com/maps?q=${v.loc.lat},${v.loc.lng}" target="_blank" rel="noopener">Open vehicle position in maps</a> <small>Accuracy about ${v.loc.acc} m. The pin shows the vehicle, not the child.</small></p>`}

function pView(){
 const kids=(me.sids||[]).map(i=>D.students.find(s=>s.id===i)).filter(Boolean);
 let h=`<div class="seg">${[['today','Today'],['school','School'],['fees','Fees']].map(([t,n])=>`<button class="${S.pt===t?'on':''}" onclick="S.pt='${t}';draw()">${n}</button>`).join('')}</div>`;
 kids.forEach(k=>{const d=day(k.id),v=vh(k.v),f=k.n.split(' ')[0];
  h+=`<div class="card"><div class="rw"><h3>${esc(k.n)}</h3>${chip('Class '+esc(k.c))}</div>${v?`<small>${esc(v.t)} ${esc(v.no)}, driver ${esc(v.d)}. Identity and documents are checked by the school; parents see status only.</small>${v.revoked?`<p>${chip('Driver removed by school','b')} The school will assign an alternate driver.</p>`:''}${v.delay&&Date.now()-v.delay<36e5?`<p>${chip('Delay reported '+ago(v.delay),'w')}</p>`:''}`:''}</div>`;
  if(S.pt==='today'){
   h+=`<div class="card">${d.plan==null?`<b>Is ${esc(f)} using the vehicle today?</b><div class="rw l" style="margin-top:8px">${btn('Yes, travelling',`go('plan','${k.id}',1)`)}${btn('No',`go('plan','${k.id}',0)`,'o')}</div>`:`<div class="rw"><span>Today’s plan: <b>${d.plan?'Travelling':'Not travelling'}</b></span><button class="lk" onclick="go('plan','${k.id}',${d.plan?0:1})">Change</button></div>`}
   ${d.plan&&!d.am?`<p>${btn('My child is seated: mark boarded',`go('board','${k.id}')`)}</p>`:''}</div>
   <div class="card"><h3>Transport updates</h3>${row('Boarded in the morning',d.am,'Waiting for you to mark boarding')}${row('Dropped at school',d.sch,'Not yet recorded')}${row('Boarded for the home trip',d.pm,'Not yet recorded')}${row('Home handoff',d.home,'Not yet recorded')}
   ${d.pm&&!d.home?`<p>Handoff PIN for the adult receiving ${esc(f)}: <b>${esc(k.pin)}</b></p>`:''}
   <div class="st ${d.att?'on':''}"><i></i><div><b>Classroom attendance</b><small>${d.att?`${d.att.s}, marked by Teacher, ${d.att.t}. Separate from transport.`:'Not yet marked. Separate from transport.'}</small></div></div></div>
   <div class="card"><h3>Vehicle location</h3>${locBlock(v,d)}</div>`}
  if(S.pt==='school'){
   h+=`<div class="card"><h3>Homework for ${esc(k.c)}</h3>${D.hw.filter(x=>x.c===k.c).map(x=>`<p><b>${esc(x.s)}</b>, due ${esc(x.d)}<br>${esc(x.x)}</p>`).join('')||'<p class="mut">No homework posted.</p>'}</div>
   <div class="card"><h3>Remarks about ${esc(f)}</h3>${D.rm.filter(r=>r.sid===k.id).map(r=>`<p>${esc(r.x)}</p>`).join('')||'<p class="mut">No remarks yet. Only guardians and school staff can see them.</p>'}</div>`}
  if(S.pt==='fees'){
   h+=`<div class="card"><h3>Transport fees</h3>${D.fees.filter(x=>x.sid===k.id).map(x=>`<div class="rw" style="padding:6px 0"><span>${esc(x.m)}: <b>₹${esc(x.amt)}</b></span>${x.paid?chip('Paid, '+esc(x.rcpt),'ok'):btn('Pay (sandbox)',`go('pay','${x.id}')`)}</div>`).join('')||'<p class="mut">No fees set.</p>'}<small>Sandbox payment: no real money moves. A live version would use a regulated payment provider with the school as merchant.</small></div>`}});
 return h+`<div class="card"><h3>Link a child</h3><div class="rw l">${inp('code','Child link code')}${btn('Link child','addKid()','o')}</div></div>`}

function dView(){const v=vh(me.vid);
 if(!v)return'<div class="card bad"><p>Your vehicle was not found. Switch role and enter the code again.</p></div>';
 if(v.revoked)return`<div class="card bad"><h3>Access removed</h3><p>The school has removed your access to its student roster. Contact the transport coordinator.</p></div>`;
 let h=`<div class="card"><h3>${esc(v.t)} ${esc(v.no)}</h3><small>Driver ${esc(v.d)}. Record events only when stopped.</small><div class="rw" style="margin-top:8px">${btn(gw!==null?'Stop sharing location':'Share live location',`share('${v.id}')`,gw!==null?'o':'')}<button class="btn o" onclick="go('delay','${v.id}')">Report a delay</button></div></div>`;
 D.students.filter(k=>k.v===v.id).forEach(k=>{const d=day(k.id);let a;
  if(d.plan===0)a='<small>Not travelling today</small>';
  else if(!d.am)a=`<small>Waiting for parent to ${d.plan==null?'confirm the plan':'mark boarding'}</small>`;
  else if(!d.sch)a=btn('Record school drop',`go('sch','${k.id}')`);
  else if(!d.pm)a=btn('Board home trip',`go('pm','${k.id}')`);
  else if(!d.home)a=`<div class="rw l">${inp('pin'+k.id,'Guardian PIN','inputmode="numeric" maxlength="4" size="9"')}${btn('Home drop',`go('home','${k.id}')`)}</div><button class="lk" onclick="go('exc','${k.id}')">No adult present: report exception</button>`;
  else a=d.home.ok?chip('Handed to guardian','ok'):chip('Exception raised','b');
  h+=`<div class="card"><div class="rw"><b>${esc(k.n)}</b>${chip(d.am?'Boarded':'Not boarded')}</div><div style="margin-top:8px">${a}</div></div>`});
 return h}

function tView(){const cl=[...new Set(D.students.map(s=>s.c))];if(!cl.length)return'<div class="card"><p class="mut">No students yet. The school admin adds students first.</p></div>';
 const c=cl.includes(S.cls)?S.cls:cl[0],A={P:'Present',A:'Absent',L:'Late',E:'Excused'};
 let h=`<div class="seg">${cl.map(x=>`<button class="${x===c?'on':''}" onclick="S.cls='${esc(x)}';draw()">${esc(x)}</button>`).join('')}</div><div class="card"><h3>Roll call, class ${esc(c)}</h3><small>P present, A absent, L late, E excused. Changing a saved mark needs a reason.</small><p>${inp('why','Reason for a correction','style="width:100%"')}</p>`;
 D.students.filter(k=>k.c===c).forEach(k=>{const d=day(k.id);h+=`<div class="st"><div style="flex:1"><b>${esc(k.n)}</b><small>${d.sch?'Transport: dropped at school '+d.sch.t:'Transport: not yet at school'}</small></div><div class="seg" style="margin:0">${Object.entries(A).map(([x,s])=>`<button class="${d.att&&d.att.s===s?'on':''}" aria-label="${s}" onclick="go('att','${k.id}','${s}')" style="padding:6px 11px">${x}</button>`).join('')}</div></div>`});
 return h+`</div><div class="card"><h3>Post homework for ${esc(c)}</h3><div class="rw l"><select id="hs"><option>Maths</option><option>English</option><option>EVS</option><option>Science</option></select>${inp('hd','Due date','value="Mon 16 Nov" size="10"')}</div><p>${inp('hx','Task for the class','style="width:100%"')}</p>${btn('Post homework',`go('hw','${esc(c)}')`)}</div>
 <div class="card"><h3>Private remark</h3><select id="rk">${D.students.filter(k=>k.c===c).map(k=>`<option value="${k.id}">${esc(k.n)}</option>`).join('')}</select><p>${inp('rx','Visible only to this child’s guardians','style="width:100%"')}</p>${btn('Send remark',"go('rem')")}</div>`}

function aView(){
 if(!owner)return'<div class="card bad"><p>Sign in as admin using the link at the top of the page.</p></div>';
 const ds=Object.values(D.days),n=f=>ds.filter(f).length,ex=[];
 ds.forEach(d=>{if(d.home&&!d.home.ok)ex.push(`${esc(nm(d.sid))}: home handoff not confirmed (${d.home.t})`)});
 D.vehicles.forEach(v=>{const left=v.exp?dl(v.exp):null,on=D.students.some(k=>k.v===v.id&&trip(day(k.id)));
  if(left!==null&&left<0)ex.push(`${esc(v.t)} ${esc(v.no)}: documents expired`);else if(left!==null&&left<=30)ex.push(`${esc(v.t)} ${esc(v.no)}: documents expire in ${left} days`);
  if(on&&(!v.loc||Date.now()-v.loc.ts>120000))ex.push(`${esc(v.t)} ${esc(v.no)}: GPS signal is stale on an active trip`);
  if(v.delay&&Date.now()-v.delay<36e5)ex.push(`${esc(v.t)} ${esc(v.no)}: driver reported a delay`)});
 const vo=D.vehicles.map(v=>`<option value="${v.id}">${esc(v.t)} ${esc(v.no)}</option>`).join('');
 return`<div class="tiles"><div><b>${n(d=>d.plan===1)}</b>Planned to travel</div><div><b>${n(d=>d.am)}</b>Boarded</div><div><b>${n(d=>d.sch)}</b>At school</div><div><b>${n(d=>d.att&&d.att.s==='Present')}</b>Present in class</div></div>
 <div class="card ${ex.length?'bad':''}"><h3>Exceptions that need action</h3>${ex.length?`<ul>${ex.map(e=>`<li>${e}</li>`).join('')}</ul>`:'<p class="mut">Nothing needs action.</p>'}</div>
 <div class="card"><h3>Vehicles and drivers</h3><div class="scroll"><table><tr><th>Vehicle</th><th>Driver and code</th><th>Documents</th><th></th></tr>${D.vehicles.map(v=>{const l=v.exp?dl(v.exp):null;return`<tr><td>${esc(v.t)}<small>${esc(v.no)}</small></td><td>${esc(v.d)}<small>${esc(v.code)}</small></td><td>${l===null?'Not set':chip(l<0?'Expired':l<=30?'Expires in '+l+' days':'Valid',l<0?'b':l<=30?'w':'ok')}</td><td>${v.revoked?chip('Removed','b'):''}<button class="lk" onclick="go('rev','${v.id}')">${v.revoked?'Restore':'Remove for our students'}</button></td></tr>`}).join('')||'<tr><td colspan="4" class="mut">No vehicles yet. Add one below.</td></tr>'}</table></div></div>
 <div class="card"><h3>Students and link codes</h3><div class="scroll"><table><tr><th>Child</th><th>Class</th><th>Link code</th><th>Handoff PIN</th></tr>${D.students.map(k=>`<tr><td>${esc(k.n)}</td><td>${esc(k.c)}</td><td>${esc(k.code)}</td><td>${esc(k.pin)}</td></tr>`).join('')||'<tr><td colspan="4" class="mut">No students yet.</td></tr>'}</table></div><small>Share each link code with that child’s guardian only.</small></div>
 <div class="card"><h3>Add a vehicle</h3><div class="rw l"><select id="vt"><option>School bus</option><option>Van</option><option>Auto</option><option>E-rickshaw</option></select>${inp('vno','Vehicle number','size="12"')}${inp('vd','Driver name','size="14"')}${inp('ve','','type="date" aria-label="Documents valid until"')}</div><p>${btn('Add vehicle',"go('addV')")}</p></div>
 <div class="card"><h3>Add a student</h3><div class="rw l">${inp('sn1','Child name','size="14"')}${inp('sc','Class, e.g. 5-B','size="8"')}<select id="sv">${vo}</select>${inp('sf','Monthly fee ₹','inputmode="numeric" size="8"')}</div><p>${btn('Add student',"go('addS')")}</p></div>
 <div class="card"><h3>School settings</h3><div class="rw l">${inp('sname','School name',`value="${esc(D.school.name)}"`)}${inp('sstaff','Teacher staff code',`value="${esc(D.school.staff)}"`)}</div><p>${btn('Save settings',"go('school')",'o')}</p></div>
 <div class="card"><h3>Audit log</h3>${D.log.length?`<ul>${D.log.map(l=>`<li>${new Date(l.ts).toLocaleTimeString('en-IN',{hour:'numeric',minute:'2-digit',hour12:true})}, ${esc(l.who)}: ${esc(l.txt)}</li>`).join('')}</ul>`:'<p class="mut">Events from parents, drivers, teachers and the school appear here.</p>'}</div>`}

function setup(){const p=S.pick,ph={parent:'Child link code from the school',driver:'Vehicle code from the school',teacher:'Staff code from the school'};
 return`<div class="card"><h3>Who are you?</h3><div class="seg">${Object.entries(LAB).map(([r,n])=>`<button class="${p===r?'on':''}" onclick="pick('${r}')">${n}</button>`).join('')}</div>${!p?'<p class="mut">The school gives each parent a child link code, each driver a vehicle code, and teachers a staff code.</p>':p==='admin'?(owner?`<p>${btn('Continue as school admin',"saveMe({...me,role:'admin'})")}</p>`:'<p class="note">Sign in as admin using the link at the top of the page.</p>'):`<p>${inp('code',ph[p],'style="width:100%"')}</p>${btn('Continue',"join()")}`}</div>`}

function draw(){const a=document.activeElement;if(a&&['INPUT','SELECT'].includes(a.tagName)){pend=1;return}pend=0;
 $('#sn').textContent=D.school.name||'School transport and parent portal';
 $('#who').innerHTML=me.role?`${chip(LAB[me.role])} <button class="lk" onclick="switchRole()">Switch role</button>`:'';
 $('#app').innerHTML=(err?`<div class="note">${esc(err)}</div>`:'')+(!ready?'<p class="mut">Loading…</p>':!db?'<div class="card bad"><h3>Live data is not available</h3><p>The server could not be reached. Check your connection.</p></div>':!me.role?setup():{parent:pView,driver:dView,teacher:tView,admin:aView}[me.role]())}
document.addEventListener('focusout',()=>setTimeout(()=>pend&&draw(),200));
(async()=>{
 try{db=await claude.use('db');user=await claude.use('user')}catch(e){}
 if(!db||!user){db=null;ready=1;draw();return}
 uid=await user.id();owner=user.isOwner();
 try{const m=await db.collection('data/users/'+uid).doc('me').get();me=m.exists?m.data():{}}catch(e){}
 const sub=(q,f)=>q.onSnapshot(s=>{f(s.docs.map(d=>({id:d.id,...d.data()})));draw()},e=>{err=e.message||e.code;draw()});
 sub(db.collection('students'),a=>D.students=a);sub(db.collection('vehicles'),a=>D.vehicles=a);sub(db.collection('fees'),a=>D.fees=a);
 sub(db.collection('days').where('date','==',today),a=>{D.days={};a.forEach(x=>D.days[x.sid]=x)});
 sub(db.collection('homework').orderBy('ts','desc').limit(20),a=>D.hw=a);sub(db.collection('remarks').orderBy('ts','desc').limit(30),a=>D.rm=a);
 sub(db.collection('log').orderBy('ts','desc').limit(15),a=>D.log=a);
 db.doc('school/main').onSnapshot(s=>{D.school=s.exists?s.data():{};draw()},()=>{});
 ready=1;draw();setInterval(draw,15000);
})();
</script></body></html>
"""

SHIM_JS = r"""// Gives the page the same small database API it used on claude.ai, backed by this server.
(() => {
  const token = () => localStorage.getItem('adm') || '';
  const call = async (method, url, body) => {
    const r = await fetch('/api' + url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token() },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || 'Error ' + r.status);
    return out;
  };
  const path = c => '/c/' + encodeURIComponent(c);
  const snap = d => ({
    exists: d != null,
    id: d && d.id,
    data: () => { if (!d) return undefined; const { id, ...rest } = d; return rest; }
  });
  // Real-time updates by polling every 2 seconds; the callback only fires when data changed.
  const poll = (fetcher, cb, onError) => {
    let last, dead = false, failed = false;
    const tick = async () => {
      if (dead) return;
      try {
        const v = await fetcher(), s = JSON.stringify(v);
        if (s !== last) { last = s; cb(v); }
        failed = false;
      } catch (e) { if (!failed && onError) onError(e); failed = true; }
    };
    tick();
    const h = setInterval(tick, 2000);
    return () => { dead = true; clearInterval(h); };
  };
  class Doc {
    constructor(c, id) { this.c = c; this.id = id; }
    async get() {
      const out = await call('GET', path(this.c) + '/' + encodeURIComponent(this.id));
      return snap(out.missing ? null : out);
    }
    set(d) { return call('PUT', path(this.c) + '/' + encodeURIComponent(this.id), d); }
    update(d) { return call('PATCH', path(this.c) + '/' + encodeURIComponent(this.id), d); }
    onSnapshot(cb, onError) {
      return poll(async () => { const s = await this.get(); return s.exists ? { id: this.id, ...s.data() } : null; }, d => cb(snap(d)), onError);
    }
  }
  class Query {
    constructor(c, w = [], o = null, l = 0) { this.c = c; this.w = w; this.o = o; this.l = l; }
    where(f, op, v) { return new Query(this.c, [...this.w, [f, v]], this.o, this.l); }
    orderBy(f, dir = 'asc') { return new Query(this.c, this.w, [f, dir], this.l); }
    limit(n) { return new Query(this.c, this.w, this.o, n); }
    doc(id) { return new Doc(this.c, id); }
    add(d) { return call('POST', path(this.c), d).then(r => new Doc(this.c, r.id)); }
    async get() {
      const p = new URLSearchParams();
      if (this.w.length) p.set('where', JSON.stringify(this.w));
      if (this.o) { p.set('order', this.o[0]); p.set('dir', this.o[1]); }
      if (this.l) p.set('limit', this.l);
      return (await call('GET', path(this.c) + '?' + p)).docs;
    }
    onSnapshot(cb, onError) { return poll(() => this.get(), docs => cb({ docs: docs.map(snap) }), onError); }
  }
  let uid = localStorage.getItem('uid');
  if (!uid) { uid = crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2); localStorage.setItem('uid', uid); }
  const db = { collection: c => new Query(c), doc: p => { const [c, i] = p.split('/'); return new Doc(c, i); } };
  const user = { id: async () => uid, isOwner: () => !!localStorage.getItem('adm') };
  window.claude = { use: async name => (name === 'db' ? db : user) };

  addEventListener('DOMContentLoaded', () => {
    const b = document.createElement('button');
    b.className = 'lk';
    b.textContent = token() ? 'Admin sign-out' : 'Admin sign-in';
    b.onclick = async () => {
      if (token()) localStorage.removeItem('adm');
      else {
        const password = prompt('Admin password');
        if (!password) return;
        try { localStorage.setItem('adm', (await call('POST', '/login', { password })).token); }
        catch (e) { alert('Wrong password'); return; }
      }
      location.reload();
    };
    document.querySelector('header').appendChild(b);
  });
})();
"""


if __name__ == "__main__":
    init_db()
    if "--no-seed" not in sys.argv:
        seed()
    if ADMIN_PASSWORD == "admin123":
        print("WARNING: using the default admin password. Set ADMIN_PASSWORD before sharing this.")
    print(f"Serving on http://localhost:{PORT}  (admin password via ADMIN_PASSWORD)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
