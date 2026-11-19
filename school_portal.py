#!/usr/bin/env python3
"""School transport and parent portal: one file, standard library only.

Run:   python school_portal.py  (opens on http://localhost:8000)
Env:   ADMIN_PASSWORD, PORT, DB_PATH, ASSETLINKS (optional, for the Android app)
Flags: --no-seed  (start with an empty database instead of the fictional demo school)
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import struct
import sys
import threading
import time
import uuid
import zlib
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
ADMIN_ONLY = {"students", "school", "daypins"}           # no writes at all
READ_ADMIN_ONLY = {"daypins"}                            # today's handoff PINs: never sent to drivers or teachers
MAX_TRIES = 3                                            # wrong handoff PINs per child per day before it locks
CREATE_ADMIN_ONLY = {"vehicles": {"loc", "delay"},       # may only change these fields
                     "fees": {"paid", "rcpt", "ts"}}
HIDDEN = {"students": {"pin", "code"}, "vehicles": {"code"}, "school": {"staff"}}  # only the admin sees these
LOCK = threading.Lock()   # makes read-modify-write updates safe when several phones save at once
FAILS = {}                # wrong codes and PINs per address, to slow down guessing


def hide(coll, doc):
    return {k: v for k, v in doc.items() if k not in HIDDEN.get(coll, ())} if doc else doc


def too_many(ip):
    now = time.time()
    if len(FAILS) > 5000:
        FAILS.clear()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 600]
    return len(FAILS[ip]) >= 20


def failed(ip):
    FAILS.setdefault(ip, []).append(time.time())


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


def query(coll, qs, hidden=()):
    where = json.loads(qs.get("where", ["[]"])[0])
    if not isinstance(where, list) or not all(isinstance(w, list) and len(w) == 2 for w in where):
        raise ValueError("Bad query")
    order = qs.get("order", [None])[0]
    if any(w[0] in hidden for w in where) or order in hidden:
        raise ValueError("Bad query")
    docs = read_all(coll)
    for field, value in where:
        docs = [d for d in docs if d.get(field) == value]
    if order:
        rev = qs.get("dir", ["asc"])[0] == "desc"
        docs.sort(key=lambda d: (d.get(order) is None, str(type(d.get(order))), d.get(order) or 0), reverse=rev)
    if "limit" in qs:
        docs = docs[: int(qs["limit"][0])]
    return docs


def student_by_code(code):
    code = str(code or "").strip().upper()
    if not code:
        return None
    return next((s for s in read_all("students") if str(s.get("code", "")).upper() == code), None)


def valid_date(d):
    try:
        time.strptime(d, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def day_pin(date, sid):
    """Today's one-time handoff PIN for a child, created on first use. Call while holding LOCK."""
    p = read_one("daypins", f"{date}_{sid}")
    if not p:
        p = {"pin": f"{secrets.randbelow(10000):04d}", "tries": 0, "date": date, "sid": sid}
        write("daypins", f"{date}_{sid}", p)
    return p


def seed():
    if read_all("students"):
        return
    def in_days(n):
        return time.strftime("%Y-%m-%d", time.localtime(time.time() + n * 86400))

    items = {
        "school": {"main": {"name": "Greenfield Public School (demo)", "staff": "STAFF-5B", "resetAt": int(time.time() * 1000)}},
        "vehicles": {
            "v1": {"t": "School bus", "no": "DL1PC 2203", "d": "Suresh Yadav", "exp": in_days(140), "code": "V-2203"},
            "v2": {"t": "Van", "no": "DL3CA 4821", "d": "Ramesh Kumar", "exp": in_days(20), "code": "V-4821"},
            "v3": {"t": "E-rickshaw", "no": "DL1RE 9087", "d": "Imran Khan", "exp": in_days(90), "code": "V-9087"}},
        "students": {
            "s1": {"n": "Aarav Sharma", "c": "5-B", "v": "v2", "code": "AAR-4K7"},
            "s2": {"n": "Meera Nair", "c": "5-B", "v": "v2", "code": "MEE-2P9"},
            "s3": {"n": "Kabir Singh", "c": "5-B", "v": "v2", "code": "KAB-8M3"},
            "s4": {"n": "Diya Rao", "c": "5-B", "v": "v1", "code": "DIY-5T6"},
            "s5": {"n": "Rohan Das", "c": "5-B", "v": "v3", "code": "ROH-7Q2"}},
        "fees": {f"f{i}": {"sid": f"s{i}", "m": "November", "amt": a, "paid": 0}
                 for i, a in enumerate([1200, 1200, 1200, 1500, 900], 1)},
        "homework": {"h1": {"c": "5-B", "s": "Maths", "d": in_days(1),
                            "x": "Practice sheet 4, questions 1 to 10", "ts": int(time.time() * 1000)}},
    }
    for coll, docs in items.items():
        for doc_id, data in docs.items():
            write(coll, doc_id, data)


# ---- Installable-app support (manifest, offline shell, icons) ----
MANIFEST = {
    "id": "/", "name": "School Transport Portal", "short_name": "School Portal",
    "description": "Transport updates, attendance and homework for parents, drivers, teachers and schools.",
    "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
    "background_color": "#14213d", "theme_color": "#14213d",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}
PWA_TAGS = ('<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#14213d">'
            '<link rel="icon" href="/icon-192.png"><link rel="apple-touch-icon" href="/icon-192.png">')
SW_JS = """const C='school-portal-v1',SHELL=['/','/shim.js','/icon-192.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)));self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C).map(x=>caches.delete(x)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.pathname.startsWith('/api/'))return;
e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(C).then(c=>c.put(e.request,cp));return r}).catch(()=>caches.match(e.request)))});
"""
_ICONS = {}


def icon(size):
    """A yellow school-bus icon on navy, drawn with the standard library and cached."""
    if size in _ICONS:
        return _ICONS[size]
    navy, yellow, light = (20, 33, 61), (245, 183, 0), (232, 240, 251)

    def color(x, y):
        if 0.18 <= x <= 0.82 and 0.30 <= y <= 0.68:
            if 0.36 <= y <= 0.48 and (0.23 <= x <= 0.38 or 0.42 <= x <= 0.57 or 0.61 <= x <= 0.76):
                return light
            return yellow
        for cx in (0.32, 0.68):
            d = (x - cx) ** 2 + (y - 0.70) ** 2
            if d <= 0.035 ** 2:
                return navy
            if d <= 0.075 ** 2:
                return light
        return navy

    rows = [b"".join(bytes(color((i + .5) / size, (j + .5) / size)) for i in range(size)) for j in range(size)]
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    _ICONS[size] = data
    return data


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
        if n < 0 or n > MAX_BODY:
            raise ValueError("Request too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def ip(self):
        # Behind a host's proxy the last X-Forwarded-For entry is the real visitor and cannot be faked.
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[-1].strip()

    def allowed(self, method, coll, data):
        if self.is_admin():
            return True
        if coll in ADMIN_ONLY:
            return False
        if coll in CREATE_ADMIN_ONLY:
            return method == "PATCH" and set(data) <= CREATE_ADMIN_ONLY[coll]
        if coll == "days":
            # Day records are only merged, never replaced, and a confirmed home handoff
            # can only come from /api/handoff, which checks the guardian's PIN.
            # Parent confirmations only come from /api/confirm, which checks the parent's link code.
            home = data.get("home")
            return (method == "PATCH" and "conf" not in data
                    and (home is None or (isinstance(home, dict) and home.get("ok") == 0)))
        return True

    def join(self, data):
        if too_many(self.ip()):
            return self.reply(429, {"error": "Too many wrong codes. Try again in 10 minutes."})
        kind, code = data.get("kind"), str(data.get("code", "")).strip().upper()
        if not code:
            return self.reply(400, {"error": "Enter the code from the school."})
        if kind == "parent":
            for s in read_all("students"):
                if str(s.get("code", "")).upper() == code:
                    return self.reply(200, {"sid": s["id"]})
            msg = "No child matches that code. Check it with the school."
        elif kind == "driver":
            for v in read_all("vehicles"):
                if str(v.get("code", "")).upper() == code:
                    return self.reply(200, {"vid": v["id"]})
            msg = "No vehicle matches that code."
        elif kind == "teacher":
            staff = str((read_one("school", "main") or {}).get("staff", "")).upper()
            if staff and staff == code:
                return self.reply(200, {"ok": True})
            msg = "Staff code does not match."
        else:
            return self.reply(400, {"error": "Unknown role"})
        failed(self.ip())
        self.reply(404, {"error": msg})

    def handoff(self, data):
        if too_many(self.ip()):
            return self.reply(429, {"error": "Too many wrong PINs. Try again in 10 minutes."})
        sid, pin, date = str(data.get("sid", "")), str(data.get("pin", "")).strip(), str(data.get("date", ""))[:10]
        t = str(data.get("t", ""))[:20]
        student = read_one("students", sid)
        if not student or not valid_date(date):
            return self.reply(400, {"error": "Bad request"})
        vehicle = read_one("vehicles", str(student.get("v", ""))) or {}
        if vehicle.get("revoked"):
            return self.reply(403, {"error": "The school has removed this driver's access."})
        name, now, doc_id = student.get("n", "student"), int(time.time() * 1000), f"{date}_{sid}"
        with LOCK:
            day = read_one("days", doc_id) or {}
            if not day.get("pm"):
                return self.reply(409, {"error": "Record the home trip boarding first."})
            p = day_pin(date, sid)
            if p.get("tries", 0) >= MAX_TRIES:
                return self.reply(423, {"error": "This handoff is locked after 3 wrong PINs. Call the school; only the school can unlock it."})
            if not hmac.compare_digest(pin.encode(), str(p["pin"]).encode()):
                p["tries"] = p.get("tries", 0) + 1
                write("daypins", doc_id, p)
                failed(self.ip())
                left = MAX_TRIES - p["tries"]
                if left > 0:
                    return self.reply(403, {"error": f"PIN does not match today's PIN. {left} {'try' if left == 1 else 'tries'} left before the school is alerted."})
                day.update(date=date, sid=sid, home={"by": "System", "t": t, "ts": now, "ok": 0, "why": "locked"})
                write("days", doc_id, day)
                write("log", uuid.uuid4().hex[:12], {"ts": now, "who": "System",
                                                     "txt": f"handoff for {name} locked after 3 wrong PINs"})
                return self.reply(423, {"error": "Wrong PIN 3 times. The handoff is locked and the school has been alerted."})
            loc = vehicle.get("loc") or {}
            where = None
            if isinstance(loc, dict) and isinstance(loc.get("ts"), (int, float)) and now - loc["ts"] < 300_000:
                where = {"lat": loc.get("lat"), "lng": loc.get("lng"), "acc": loc.get("acc")}
            day.update(date=date, sid=sid, home={"by": "Driver", "t": t, "ts": now, "ok": 1, "where": where})
            write("days", doc_id, day)
        write("log", uuid.uuid4().hex[:12], {"ts": now, "who": "Driver",
                                             "txt": f"home drop for {name}, today's PIN confirmed"
                                                    + ("" if where else " (driver was not sharing location)")})
        self.reply(200, {"ok": True})

    def pin(self, data):
        """Today's handoff PIN, only for someone holding the child's link code (the parent)."""
        if too_many(self.ip()):
            return self.reply(429, {"error": "Too many wrong codes. Try again in 10 minutes."})
        student, date = student_by_code(data.get("code")), str(data.get("date", ""))[:10]
        if not student:
            failed(self.ip())
            return self.reply(404, {"error": "Link this child again with the school's code to see the PIN."})
        if not valid_date(date):
            return self.reply(400, {"error": "Bad request"})
        with LOCK:
            p = day_pin(date, student["id"])
        self.reply(200, {"pin": p["pin"], "locked": p.get("tries", 0) >= MAX_TRIES})

    def confirm(self, data):
        """The parent says whether the child is home. Needs the child's link code, so a driver cannot fake it."""
        if too_many(self.ip()):
            return self.reply(429, {"error": "Too many wrong codes. Try again in 10 minutes."})
        student, date = student_by_code(data.get("code")), str(data.get("date", ""))[:10]
        if not student:
            failed(self.ip())
            return self.reply(404, {"error": "Link this child again with the school's code first."})
        if not valid_date(date):
            return self.reply(400, {"error": "Bad request"})
        ok, now, sid = 1 if data.get("ok") else 0, int(time.time() * 1000), student["id"]
        with LOCK:
            day = read_one("days", f"{date}_{sid}") or {}
            if ok and not (day.get("home") or {}).get("ok"):
                return self.reply(409, {"error": "The driver has not recorded the handoff yet."})
            day.update(date=date, sid=sid, conf={"by": "Parent", "t": str(data.get("t", ""))[:20], "ts": now, "ok": ok})
            write("days", f"{date}_{sid}", day)
        name = student.get("n", "student")
        write("log", uuid.uuid4().hex[:12], {"ts": now, "who": "Parent",
                                             "txt": f"confirmed {name} is home safely" if ok
                                             else f"ALERT: reported {name} has NOT been received"})
        self.reply(200, {"ok": True})

    def static(self, name):
        if name in ("/", "/index.html"):
            page = INDEX_HTML
            page = page.replace("</head>", PWA_TAGS + '<script src="/shim.js"></script></head>', 1)
            return self.reply(200, page.encode(), "text/html; charset=utf-8")
        if name == "/manifest.webmanifest":
            return self.reply(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if name == "/sw.js":
            return self.reply(200, SW_JS.encode(), "text/javascript; charset=utf-8")
        if name in ("/icon-192.png", "/icon-512.png", "/favicon.ico"):
            return self.reply(200, icon(512 if "512" in name else 192), "image/png")
        if name == "/.well-known/assetlinks.json" and os.environ.get("ASSETLINKS"):
            return self.reply(200, os.environ["ASSETLINKS"].encode())
        if name == "/shim.js":
            return self.reply(200, SHIM_JS.encode(), "text/javascript; charset=utf-8")
        self.reply(404, {"error": "Not found"})

    def api(self, method):
        url = urlparse(self.path)
        if url.path == "/api/login" and method == "POST":
            if too_many(self.ip()):
                return self.reply(429, {"error": "Too many wrong passwords. Try again in 10 minutes."})
            ok = hmac.compare_digest(str(self.body().get("password", "")).encode(), ADMIN_PASSWORD.encode())
            if not ok:
                failed(self.ip())
            return self.reply(200, {"token": TOKEN}) if ok else self.reply(401, {"error": "Wrong password"})
        if url.path == "/api/whoami" and method == "GET":
            return self.reply(200, {"admin": self.is_admin()})
        if url.path == "/api/join" and method == "POST":
            return self.join(self.body())
        if url.path == "/api/handoff" and method == "POST":
            return self.handoff(self.body())
        if url.path == "/api/pin" and method == "POST":
            return self.pin(self.body())
        if url.path == "/api/reset" and method == "POST":
            # Admin only: wipe every record (trips, attendance, logs, fees, links) and restore the demo school.
            if not self.is_admin():
                return self.reply(403, {"error": "Only the school admin can reset the demo."})
            with LOCK:
                with closing(conn()) as c:
                    c.execute("DELETE FROM docs")
                    c.commit()
                seed()
            FAILS.clear()
            return self.reply(200, {"ok": True})
        if url.path == "/api/confirm" and method == "POST":
            return self.confirm(self.body())
        parts = url.path.split("/")  # ['', 'api', 'c', '<collection>', '<id>?']
        if len(parts) < 4 or parts[2] != "c":
            return self.reply(404, {"error": "Not found"})
        coll = unquote(parts[3])
        doc_id = unquote(parts[4]) if len(parts) > 4 and parts[4] else None
        if method == "GET":
            admin = self.is_admin()
            if coll in READ_ADMIN_ONLY and not admin:
                return self.reply(403, {"error": "Only the school admin can see that"})
            if doc_id:
                d = read_one(coll, doc_id)
                return self.reply(200, (d if admin else hide(coll, d)) or {"missing": True})
            docs = query(coll, parse_qs(url.query), () if admin else HIDDEN.get(coll, ()))
            return self.reply(200, {"docs": docs if admin else [hide(coll, d) for d in docs]})
        data = self.body()
        if not self.allowed(method, coll, data):
            return self.reply(403, {"error": "Only the school admin can do that"})
        if method == "POST" and not doc_id:
            doc_id = uuid.uuid4().hex[:12]
            write(coll, doc_id, data)
        elif method == "PUT" and doc_id:
            write(coll, doc_id, data)
        elif method == "PATCH" and doc_id:
            with LOCK:
                current = read_one(coll, doc_id)
                if not current and coll in CREATE_ADMIN_ONLY:
                    return self.reply(404, {"error": "Not found"})
                write(coll, doc_id, {**(current or {}), **data})
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
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self.reply(400, {"error": str(e)})
        except Exception:
            self.reply(500, {"error": "Server error"})

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_PUT = lambda self: self.handle_any("PUT")
    do_PATCH = lambda self: self.handle_any("PATCH")


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
const D={students:[],vehicles:[],days:{},hw:[],rm:[],fees:[],log:[],school:{},dp:{}};const PINS={};
let db,user,uid,owner=0,me={},S={pt:'today',cls:null,pick:null},pend=0,ready=0,err='',gw=null,mv=0,mvAt=0,lw=0;
const LAB={parent:'Parent',driver:'Driver',teacher:'Teacher',admin:'School admin'};
const toast=m=>{const e=$('#toast');e.textContent=m;e.className='show';clearTimeout(toast.h);toast.h=setTimeout(()=>e.className='',3200)};
const jq=s=>esc(String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'"));
const tomorrow=()=>{const d=new Date();d.setDate(d.getDate()+1);return d.toLocaleDateString('en-CA')};
const fmtD=x=>/^\d{4}-\d{2}-\d{2}$/.test(x||'')?new Date(x+'T00:00').toLocaleDateString('en-IN',{weekday:'short',day:'numeric',month:'short'}):x;
const chip=(t,c='')=>`<span class="chip ${c}">${t}</span>`;
const btn=(t,c,k='')=>`<button class="btn ${k}" onclick="${c}">${t}</button>`;
const inp=(id,ph,x='')=>`<input id="${id}" placeholder="${ph}" ${x}>`;
const nm=id=>(D.students.find(x=>x.id===id)||{n:'student'}).n;
const vh=id=>D.vehicles.find(x=>x.id===id)||null;
const day=s=>D.days[s]||{};
const ev=(by,x={})=>({by,t:T(),ts:Date.now(),...x});
const ago=ts=>{const s=Math.max(0,Math.round((Date.now()-ts)/1000));return s<90?s+' seconds ago':s<5400?Math.round(s/60)+' minutes ago':Math.round(s/3600)+' hours ago'};
const dl=e=>Math.ceil((new Date(e)-Date.now())/864e5);
const trip=d=>(d.am&&!d.sch)||(d.pm&&!d.home);
const W=async f=>{try{await f()}catch(e){toast('Could not save: '+(e.message||e.code||'no write access')+'. Check your connection, or sign in as admin if this is a school-only change.')}};
const log=(who,txt)=>db.collection('log').add({ts:Date.now(),who,txt});
const setDay=(s,p)=>{D.days[s]={...day(s),date:today,sid:s,...p};draw();return db.collection('days').doc(today+'_'+s).update({date:today,sid:s,...p})};
const still=()=>{if(mv>=2&&Date.now()-mvAt<60000){toast('The vehicle is moving. Record events when stopped.');return 0}return 1};
function saveMe(o){me=o;if(db)W(()=>db.collection('data/users/'+uid).doc('me').set(o));draw()}
const switchRole=()=>saveMe({...me,role:null});
const pick=r=>{S.pick=r;draw()};
async function join(){const c=($('#code').value||'').trim().toUpperCase(),p=S.pick;if(!c)return toast('Enter the code from the school.');
 try{const r=await portalApi.join(p,c);
  if(p==='parent'){saveMe({...me,role:'parent',sids:[...new Set([...(me.sids||[]),r.sid])],codes:{...(me.codes||{}),[r.sid]:c}});toast('Child linked.')}
  if(p==='driver')saveMe({...me,role:'driver',vid:r.vid});
  if(p==='teacher')saveMe({...me,role:'teacher'});
 }catch(e){toast(e.message)}}
const addKid=()=>{S.pick='parent';join()};
const act={
 plan:(s,v)=>W(async()=>{await setDay(s,{plan:v});log('Parent',`marked ${nm(s)} ${v?'travelling':'not travelling'} today`)}),
 board:s=>W(async()=>{await setDay(s,{plan:1,am:ev('Parent')});log('Parent',`reported ${nm(s)} boarded (morning)`)}),
 sch:s=>still()&&W(async()=>{await setDay(s,{sch:ev('Driver')});log('Driver',`recorded ${nm(s)} dropped at school`)}),
 pm:s=>still()&&W(async()=>{await setDay(s,{pm:ev('Driver')});log('Driver',`recorded ${nm(s)} boarded for the home trip`)}),
 home:async s=>{if(!still())return;const pin=($('#pin'+s).value||'').trim();if(!pin)return toast('Enter the guardian’s PIN.');
  try{await portalApi.handoff(s,pin,today,T());D.days[s]={...day(s),home:ev('Driver',{ok:1})};draw();toast('Handoff recorded. The parent will be asked to confirm.')}catch(e){toast(e.message)}},
 conf:async(s,ok)=>{const code=(me.codes||{})[s];if(!code)return toast('Link this child again with the school’s code first.');
  try{await portalApi.confirm(code,today,ok,T());D.days[s]={...day(s),conf:ev('Parent',{ok})};draw();toast(ok?'Thank you. Marked as home safely.':'The school has been alerted.')}catch(e){toast(e.message)}},
 reset:async()=>{if(!confirm('Reset the whole demo? Every trip, attendance mark, homework, fee payment, linked phone and log entry will be wiped, and the demo school will be restored. Everyone will go back to the start screen.'))return;
  try{await portalApi.reset();await db.collection('data/users/'+uid).doc('me').set({role:'admin'});toast('Demo reset. Starting fresh…');setTimeout(()=>location.reload(),900)}catch(e){toast(e.message)}},
 unlock:id=>W(async()=>{await db.collection('daypins').doc(id).update({tries:0});await db.collection('days').doc(id).update({home:null});log('School','unlocked a locked handoff PIN after checking with the parent')}),
 bd:s=>still()&&W(async()=>{await setDay(s,{am:ev('Driver')});log('Driver',`recorded ${nm(s)} boarded (morning)`)}),
 exc:s=>still()&&W(async()=>{await setDay(s,{home:ev('Driver',{ok:0})});log('Driver',`could not confirm handoff for ${nm(s)}, exception raised`)}),
 delay:v=>W(async()=>{await db.collection('vehicles').doc(v).update({delay:Date.now()});log('Driver',`reported a delay on ${vh(v).no}`);toast('Delay reported to the school and parents.')}),
 att:(s,st)=>{const d=day(s).att;let r='';if(d&&d.s!==st){r=($('#why').value||'').trim();if(!r)return toast('Add a reason to correct a saved record.')}else if(d)return;
  W(async()=>{await setDay(s,{att:{s:st,by:'Teacher',t:T(),ts:Date.now()}});log('Teacher',r?`corrected ${nm(s)} from ${d.s} to ${st} (${r})`:`marked ${nm(s)} ${st}`)})},
 hw:c=>{const x=($('#hx').value||'').trim();if(!x)return toast('Describe the task before posting.');if(!$('#hd').value)return toast('Pick a due date.');W(async()=>{await db.collection('homework').add({c,s:$('#hs').value,x,d:$('#hd').value,ts:Date.now()});log('Teacher',`posted ${$('#hs').value} homework for ${c}`)})},
 rem:()=>{const x=($('#rx').value||'').trim();if(!x)return toast('Write the remark first.');W(async()=>{await db.collection('remarks').add({sid:$('#rk').value,x,ts:Date.now()});log('Teacher',`added a private remark for ${nm($('#rk').value)}`)})},
 pay:f=>W(async()=>{await db.collection('fees').doc(f).update({paid:1,rcpt:'RCPT-'+Math.floor(1000+Math.random()*9000),ts:Date.now()});log('Parent','paid a transport fee (sandbox)')}),
 rev:v=>W(async()=>{const x=vh(v);await db.collection('vehicles').doc(v).update({revoked:!x.revoked});log('School',`${x.revoked?'restored':'removed'} ${x.d} (${x.no}) for its students`)}),
 school:()=>W(()=>db.doc('school/main').set({name:$('#sname').value.trim(),staff:$('#sstaff').value.trim()})),
 addV:()=>{const no=($('#vno').value||'').trim().toUpperCase(),d=($('#vd').value||'').trim();if(!no||!d)return toast('Enter the vehicle number and driver name.');let code='V-'+no.replace(/\s/g,'').slice(-4);while(D.vehicles.some(v=>(v.code||'').toUpperCase()===code))code='V-'+no.replace(/\s/g,'').slice(-4)+'-'+Math.random().toString(36).slice(2,4).toUpperCase();W(async()=>{await db.collection('vehicles').add({t:$('#vt').value,no,d,exp:$('#ve').value,code});log('School',`added ${$('#vt').value} ${no}`)})},
 addS:()=>{const n=($('#sn1').value||'').trim(),c=($('#sc').value||'').trim().toUpperCase();if(!n||!c)return toast('Enter the child’s name and class.');if(!$('#sv').value)return toast('Add a vehicle first.');const mkc=()=>n.replace(/[^A-Za-z]/g,'').slice(0,3).toUpperCase().padEnd(3,'X')+'-'+Math.random().toString(36).slice(2,8).slice(0,3).toUpperCase().padEnd(3,'7');let code=mkc();while(D.students.some(x=>(x.code||'').toUpperCase()===code))code=mkc();
  W(async()=>{const r=await db.collection('students').add({n,c,v:$('#sv').value,code});await db.collection('fees').add({sid:r.id,m:new Date().toLocaleDateString('en-IN',{month:'long',year:'numeric'}),amt:+$('#sf').value||0,paid:0});log('School',`added student ${n}`)})}
};
const go=(n,...a)=>act[n](...a);
function share(vid){if(gw!==null){navigator.geolocation.clearWatch(gw);gw=null;mv=0;draw();return}
 if(!navigator.geolocation)return toast('This device cannot share location.');
 gw=navigator.geolocation.watchPosition(p=>{mv=p.coords.speed||0;mvAt=Date.now();if(Date.now()-lw<10000)return;lw=Date.now();W(()=>db.collection('vehicles').doc(vid).update({loc:{lat:p.coords.latitude,lng:p.coords.longitude,acc:Math.round(p.coords.accuracy),ts:Date.now()}}))},()=>{gw=null;toast('Location is blocked here. Allow location access in the browser and try again.');draw()},{enableHighAccuracy:true,maximumAge:5000});draw()}
const mapA=(w,t)=>w&&isFinite(+w.lat)&&isFinite(+w.lng)?` <a href="https://www.google.com/maps?q=${+w.lat},${+w.lng}" target="_blank" rel="noopener">${t}</a>`:'';
const row=(l,e,w,okT='',badT='. Handoff not confirmed, school notified.')=>`<div class="st ${e?(e.ok===0?'bad':'on'):''}"><i></i><div><b>${l}</b><small>${e?`Reported by ${esc(e.by)}, ${esc(e.t)}${e.ok===0?(e.why==='locked'?'. Locked after 3 wrong PINs, school alerted.':badT):e.ok?okT:''}${mapA(e.where,'Handoff location')}`:w}</small></div></div>`;
function pinFor(sid){const code=(me.codes||{})[sid];if(!code)return null;const key=today+'_'+sid;
 if(!PINS[key]){PINS[key]={loading:1};portalApi.pin(code,today).then(r=>{PINS[key]=r;draw()}).catch(e=>{PINS[key]={err:e.message};setTimeout(()=>{delete PINS[key]},10000);draw()})}
 return PINS[key]}
function pinBlock(k,f){const p=pinFor(k.id);
 if(!p)return`<p class="note">Link ${f} again below with the school’s code to see today’s handoff PIN.</p>`;
 if(p.loading)return'<p class="mut">Getting today’s handoff PIN…</p>';
 if(p.err)return`<p class="note">${esc(p.err)}</p>`;
 if(p.locked)return'<p class="note">Wrong PIN entered 3 times. The handoff is locked and the school has been alerted.</p>';
 return`<p>Today’s handoff PIN for the adult receiving ${f}: <b style="font-size:20px;letter-spacing:2px">${esc(p.pin)}</b></p><small>A new PIN every day. Give it only to the adult receiving ${f}, who says it to the driver at the door.</small>`}

function locBlock(v,d){if(!trip(d))return'<p class="mut">Location sharing is off outside active trips.</p>';
 if(!v||!v.loc)return`<p>${chip('Waiting for driver','w')}</p><small>The driver has not started sharing the vehicle location.</small>`;
 const fresh=Date.now()-v.loc.ts<120000,lat=+v.loc.lat,lng=+v.loc.lng;if(!isFinite(lat)||!isFinite(lng))return'<p class="mut">Location data is not valid.</p>';
 return`<p>${fresh?chip('Updated '+ago(v.loc.ts),'ok'):chip('Signal lost, last update '+ago(v.loc.ts),'w')}</p><p><a href="https://www.google.com/maps?q=${lat},${lng}" target="_blank" rel="noopener">Open vehicle position in maps</a> <small>Accuracy about ${Math.round(+v.loc.acc)||'?'} m. The pin shows the vehicle, not the child.</small></p>`}

function pView(){
 const kids=(me.sids||[]).map(i=>D.students.find(s=>s.id===i)).filter(Boolean);
 let h=`<div class="seg">${[['today','Today'],['school','School'],['fees','Fees']].map(([t,n])=>`<button class="${S.pt===t?'on':''}" onclick="S.pt='${t}';draw()">${n}</button>`).join('')}</div>`;
 kids.forEach(k=>{const d=day(k.id),v=vh(k.v),f=k.n.split(' ')[0];
  h+=`<div class="card"><div class="rw"><h3>${esc(k.n)}</h3>${chip('Class '+esc(k.c))}</div>${v?`<small>${esc(v.t)} ${esc(v.no)}, driver ${esc(v.d)}. Identity and documents are checked by the school; parents see status only.</small>${v.revoked?`<p>${chip('Driver removed by school','b')} The school will assign an alternate driver.</p>`:''}${v.delay&&Date.now()-v.delay<36e5?`<p>${chip('Delay reported '+ago(v.delay),'w')}</p>`:''}`:''}</div>`;
  if(S.pt==='today'){
   h+=`<div class="card">${d.plan==null?`<b>Is ${esc(f)} using the vehicle today?</b><div class="rw l" style="margin-top:8px">${btn('Yes, travelling',`go('plan','${k.id}',1)`)}${btn('No',`go('plan','${k.id}',0)`,'o')}</div>`:`<div class="rw"><span>Today’s plan: <b>${d.plan?'Travelling':'Not travelling'}</b></span><button class="lk" onclick="go('plan','${k.id}',${d.plan?0:1})">Change</button></div>`}
   ${d.plan&&!d.am?`<p>${btn('My child is seated: mark boarded',`go('board','${k.id}')`)}</p>`:''}</div>
   <div class="card"><h3>Transport updates</h3>${row('Boarded in the morning',d.am,d.plan===0?'Not travelling today':'Waiting for you or the driver to mark boarding')}${row('Dropped at school',d.sch,'Not yet recorded')}${row('Boarded for the home trip',d.pm,'Not yet recorded')}${row('Home handoff',d.home,'Not yet recorded','. Today’s PIN confirmed.')}${d.pm?row('Confirmed home by parent',d.conf,'Waiting for your confirmation after the handoff','','. Child NOT received. School alerted.'):''}
   ${d.pm&&!(d.home&&d.home.ok)&&!d.conf?pinBlock(k,esc(f)):''}
   <div class="st ${d.att?'on':''}"><i></i><div><b>Classroom attendance</b><small>${d.att?`${esc(d.att.s)}, marked by Teacher, ${esc(d.att.t)}. Separate from transport.`:'Not yet marked. Separate from transport.'}</small></div></div></div>
   ${d.conf&&!d.conf.ok?`<div class="card bad"><b>You reported that ${esc(f)} was not received.</b><p>The school has been alerted. Call the school now.</p></div>`
    :d.pm&&!d.conf?`<div class="card">${d.home&&d.home.ok?`<b>Is ${esc(f)} with you now?</b><div class="rw l" style="margin-top:8px">${btn('Yes, '+esc(f)+' is home',`go('conf','${k.id}',1)`)}${btn('No, alert the school',`go('conf','${k.id}',0)`,'o')}</div>`
     :`<button class="lk" onclick="go('conf','${k.id}',0)">${esc(f)} has not arrived? Alert the school</button>`}</div>`:''}
   <div class="card"><h3>Vehicle location</h3>${locBlock(v,d)}</div>`}
  if(S.pt==='school'){
   h+=`<div class="card"><h3>Homework for ${esc(k.c)}</h3>${D.hw.filter(x=>x.c===k.c).map(x=>`<p><b>${esc(x.s)}</b>, due ${esc(fmtD(x.d))}<br>${esc(x.x)}</p>`).join('')||'<p class="mut">No homework posted.</p>'}</div>
   <div class="card"><h3>Remarks about ${esc(f)}</h3>${D.rm.filter(r=>r.sid===k.id).map(r=>`<p>${esc(r.x)}</p>`).join('')||'<p class="mut">No remarks yet. Only guardians and school staff can see them.</p>'}</div>`}
  if(S.pt==='fees'){
   h+=`<div class="card"><h3>Transport fees</h3>${D.fees.filter(x=>x.sid===k.id).map(x=>`<div class="rw" style="padding:6px 0"><span>${esc(x.m)}: <b>₹${esc(x.amt)}</b></span>${x.paid?chip('Paid, '+esc(x.rcpt),'ok'):btn('Pay (sandbox)',`go('pay','${x.id}')`)}</div>`).join('')||'<p class="mut">No fees set.</p>'}<small>Sandbox payment: no real money moves. A live version would use a regulated payment provider with the school as merchant.</small></div>`}});
 return h+`<div class="card"><h3>Link a child</h3><div class="rw l">${inp('code','Child link code')}${btn('Link child','addKid()','o')}</div></div>`}

function dView(){const v=vh(me.vid);
 if(!v)return'<div class="card bad"><p>Your vehicle was not found. Switch role and enter the code again.</p></div>';
 if(v.revoked)return`<div class="card bad"><h3>Access removed</h3><p>The school has removed your access to its student roster. Contact the transport coordinator.</p></div>`;
 let h=`<div class="card"><h3>${esc(v.t)} ${esc(v.no)}</h3><small>Driver ${esc(v.d)}. Record events only when stopped. The adult at home gives you today’s PIN.</small><div class="rw" style="margin-top:8px">${btn(gw!==null?'Stop sharing location':'Share live location',`share('${v.id}')`,gw!==null?'o':'')}<button class="btn o" onclick="go('delay','${v.id}')">Report a delay</button></div></div>`;
 D.students.filter(k=>k.v===v.id).forEach(k=>{const d=day(k.id);let a;
  const pinForm=`<div class="rw l">${inp('pin'+k.id,'Today’s PIN from guardian','inputmode="numeric" maxlength="4" size="9"')}${btn('Home drop',`go('home','${k.id}')`)}</div>`;
  if(d.home&&d.home.ok)a=chip('Handed to guardian','ok')+' '+(d.conf?chip(d.conf.ok?'Parent confirmed':'Parent says not received',d.conf.ok?'ok':'b'):chip('Waiting for parent to confirm'));
  else if(d.home)a=`<p>${chip('Exception raised','b')}</p><small>${d.home.why==='locked'?'Locked after 3 wrong PINs. Call the school.':'Guardian arrived later? Enter today’s PIN.'}</small>${d.home.why==='locked'?'':pinForm}`;
  else if(d.pm)a=`${pinForm}<button class="lk" onclick="go('exc','${k.id}')">No adult present: report exception</button>`;
  else if(d.am&&!d.sch)a=btn('Record school drop',`go('sch','${k.id}')`);
  else if(!d.am&&!d.sch)a=`${d.plan===0?'<small>Parent marked not travelling today.</small>':`<div class="rw l">${btn('Record boarding',`go('bd','${k.id}')`)}${d.plan==null?'<small>Parent has not confirmed today’s plan.</small>':''}</div>`}<button class="lk" onclick="go('pm','${k.id}')">Home trip only: board now</button>${d.plan===0?` <button class="lk" onclick="go('bd','${k.id}')">Boarded anyway</button>`:''}`;
  else a=btn('Board home trip',`go('pm','${k.id}')`);
  h+=`<div class="card"><div class="rw"><b>${esc(k.n)}</b>${chip(d.am?'Boarded':'Not boarded')}</div><div style="margin-top:8px">${a}</div></div>`});
 return h}

function tView(){const cl=[...new Set(D.students.map(s=>s.c))];if(!cl.length)return'<div class="card"><p class="mut">No students yet. The school admin adds students first.</p></div>';
 const c=cl.includes(S.cls)?S.cls:cl[0],A={P:'Present',A:'Absent',L:'Late',E:'Excused'};
 let h=`<div class="seg">${cl.map(x=>`<button class="${x===c?'on':''}" onclick="S.cls='${jq(x)}';draw()">${esc(x)}</button>`).join('')}</div><div class="card"><h3>Roll call, class ${esc(c)}</h3><small>P present, A absent, L late, E excused. Changing a saved mark needs a reason.</small><p>${inp('why','Reason for a correction','style="width:100%"')}</p>`;
 D.students.filter(k=>k.c===c).forEach(k=>{const d=day(k.id);h+=`<div class="st"><div style="flex:1"><b>${esc(k.n)}</b><small>${d.sch?'Transport: dropped at school '+esc(d.sch.t):'Transport: not yet at school'}</small></div><div class="seg" style="margin:0">${Object.entries(A).map(([x,s])=>`<button class="${d.att&&d.att.s===s?'on':''}" aria-label="${s}" onclick="go('att','${k.id}','${s}')" style="padding:6px 11px">${x}</button>`).join('')}</div></div>`});
 return h+`</div><div class="card"><h3>Post homework for ${esc(c)}</h3><div class="rw l"><select id="hs"><option>Maths</option><option>English</option><option>EVS</option><option>Science</option></select>${inp('hd','','type="date" aria-label="Due date" value="'+tomorrow()+'"')}</div><p>${inp('hx','Task for the class','style="width:100%"')}</p>${btn('Post homework',`go('hw','${jq(c)}')`)}</div>
 <div class="card"><h3>Private remark</h3><select id="rk">${D.students.filter(k=>k.c===c).map(k=>`<option value="${k.id}">${esc(k.n)}</option>`).join('')}</select><p>${inp('rx','Visible only to this child’s guardians','style="width:100%"')}</p>${btn('Send remark',"go('rem')")}</div>`}

function aView(){
 if(!owner)return'<div class="card bad"><p>Sign in as admin using the link at the top of the page.</p></div>';
 const ds=Object.values(D.days),n=f=>ds.filter(f).length,ex=[];
 const now=Date.now();
 ds.forEach(d=>{const nn=esc(nm(d.sid));
  if(d.conf&&!d.conf.ok)ex.unshift(`<b>${nn}: the parent reports the child was NOT received (${esc(d.conf.t)}). Act now.</b>`);
  if(d.home&&!d.home.ok)ex.push(`${nn}: ${d.home.why==='locked'?'handoff locked after 3 wrong PINs':'home handoff not confirmed'} (${esc(d.home.t)})`);
  if(d.home&&d.home.ok&&!d.conf&&now-d.home.ts>18e5)ex.push(`${nn}: handed over ${ago(d.home.ts)}, parent has not confirmed yet${mapA(d.home.where,'Handoff location')}`);
  if(d.home&&d.home.ok&&!d.home.where)ex.push(`${nn}: handoff recorded without vehicle location`);
  if(d.pm&&!d.home&&!d.conf&&now-(d.pm.ts||now)>54e5)ex.push(`${nn}: on the home trip for over 90 minutes without a handoff`)});
 Object.values(D.dp).forEach(p=>{if((p.tries||0)>=3)ex.push(`${esc(nm(p.sid))}: wrong PIN entered 3 times today <button class="lk" onclick="go('unlock','${jq(p.id)}')">Unlock after checking with the parent</button>`)});
 D.vehicles.forEach(v=>{const left=v.exp?dl(v.exp):null,on=D.students.some(k=>k.v===v.id&&trip(day(k.id)));
  if(left!==null&&left<0)ex.push(`${esc(v.t)} ${esc(v.no)}: documents expired`);else if(left!==null&&left<=30)ex.push(`${esc(v.t)} ${esc(v.no)}: documents expire in ${left} days`);
  if(on&&(!v.loc||Date.now()-v.loc.ts>120000))ex.push(`${esc(v.t)} ${esc(v.no)}: GPS signal is stale on an active trip`);
  if(v.delay&&Date.now()-v.delay<36e5)ex.push(`${esc(v.t)} ${esc(v.no)}: driver reported a delay`)});
 const vo=D.vehicles.map(v=>`<option value="${v.id}">${esc(v.t)} ${esc(v.no)}</option>`).join('');
 return`<div class="tiles"><div><b>${n(d=>d.plan===1)}</b>Planned to travel</div><div><b>${n(d=>d.am)}</b>Boarded</div><div><b>${n(d=>d.sch)}</b>At school</div><div><b>${n(d=>d.att&&d.att.s==='Present')}</b>Present in class</div></div>
 <div class="card ${ex.length?'bad':''}"><h3>Exceptions that need action</h3>${ex.length?`<ul>${ex.map(e=>`<li>${e}</li>`).join('')}</ul>`:'<p class="mut">Nothing needs action.</p>'}</div>
 <div class="card"><h3>Vehicles and drivers</h3><div class="scroll"><table><tr><th>Vehicle</th><th>Driver and code</th><th>Documents</th><th></th></tr>${D.vehicles.map(v=>{const l=v.exp?dl(v.exp):null;return`<tr><td>${esc(v.t)}<small>${esc(v.no)}</small></td><td>${esc(v.d)}<small>${esc(v.code)}</small></td><td>${l===null?'Not set':chip(l<0?'Expired':l<=30?'Expires in '+l+' days':'Valid',l<0?'b':l<=30?'w':'ok')}</td><td>${v.revoked?chip('Removed','b'):''}<button class="lk" onclick="go('rev','${v.id}')">${v.revoked?'Restore':'Remove for our students'}</button></td></tr>`}).join('')||'<tr><td colspan="4" class="mut">No vehicles yet. Add one below.</td></tr>'}</table></div></div>
 <div class="card"><h3>Students and link codes</h3><div class="scroll"><table><tr><th>Child</th><th>Class</th><th>Link code</th></tr>${D.students.map(k=>`<tr><td>${esc(k.n)}</td><td>${esc(k.c)}</td><td>${esc(k.code)}</td></tr>`).join('')||'<tr><td colspan="3" class="mut">No students yet.</td></tr>'}</table></div><small>Share each link code with that child’s guardian only. Handoff PINs change every day and only parents see them.</small></div>
 <div class="card"><h3>Add a vehicle</h3><div class="rw l"><select id="vt"><option>School bus</option><option>Van</option><option>Auto</option><option>E-rickshaw</option></select>${inp('vno','Vehicle number','size="12"')}${inp('vd','Driver name','size="14"')}${inp('ve','','type="date" aria-label="Documents valid until"')}</div><p>${btn('Add vehicle',"go('addV')")}</p></div>
 <div class="card"><h3>Add a student</h3><div class="rw l">${inp('sn1','Child name','size="14"')}${inp('sc','Class, e.g. 5-B','size="8"')}<select id="sv">${vo}</select>${inp('sf','Monthly fee ₹','inputmode="numeric" size="8"')}</div><p>${btn('Add student',"go('addS')")}</p></div>
 <div class="card" style="border-color:var(--w)"><div class="rw"><div><h3>Demo tools</h3><small>Wipes all records and restores the demo school with its original codes. Every open phone returns to the start screen.</small></div>${btn('Reset demo','go(\'reset\')')}</div></div>
 <div class="card"><h3>School settings</h3><div class="rw l">${inp('sname','School name',`value="${esc(D.school.name)}"`)}${inp('sstaff','Teacher staff code',`value="${esc(D.school.staff)}"`)}</div><p>${btn('Save settings',"go('school')",'o')}</p></div>
 <div class="card"><h3>Audit log</h3>${D.log.length?`<ul>${D.log.map(l=>`<li>${new Date(l.ts).toLocaleTimeString('en-IN',{hour:'numeric',minute:'2-digit',hour12:true})}, ${esc(l.who)}: ${esc(l.txt)}</li>`).join('')}</ul>`:'<p class="mut">Events from parents, drivers, teachers and the school appear here.</p>'}</div>`}

function setup(){const p=S.pick,ph={parent:'Child link code from the school',driver:'Vehicle code from the school',teacher:'Staff code from the school'};
 return`<div class="card"><h3>Who are you?</h3><div class="seg">${Object.entries(LAB).map(([r,n])=>`<button class="${p===r?'on':''}" onclick="pick('${r}')">${n}</button>`).join('')}</div>${!p?'<p class="mut">The school gives each parent a child link code, each driver a vehicle code, and teachers a staff code.</p>':p==='admin'?(owner?`<p>${btn('Continue as school admin',"saveMe({...me,role:'admin'})")}</p>`:'<p class="note">Sign in as admin using the link at the top of the page.</p>'):`<p>${inp('code',ph[p],'style="width:100%"')}</p>${btn('Continue',"join()")}`}</div>`}

function draw(){const a=document.activeElement;if(a&&['INPUT','SELECT'].includes(a.tagName)){pend=1;return}pend=0;
 $('#sn').textContent=D.school.name||'School transport and parent portal';
 $('#who').innerHTML=me.role?`${chip(LAB[me.role])} <button class="lk" onclick="switchRole()">Switch role</button>`:'';
 $('#app').innerHTML=(err?`<div class="note">${esc(err)}</div>`:'')+(!ready?'<p class="mut">Loading…</p>':!db?'<div class="card bad"><h3>Live data is not available</h3><p>The server could not be reached. Check your connection.</p></div>':({parent:pView,driver:dView,teacher:tView,admin:aView}[me.role]||setup)())}
document.addEventListener('focusout',()=>setTimeout(()=>pend&&draw(),200));
(async()=>{
 try{db=await claude.use('db');user=await claude.use('user')}catch(e){}
 if(!db||!user){db=null;ready=1;draw();return}
 uid=await user.id();owner=user.isOwner();
 const loadMe=async()=>{try{const m=await db.collection('data/users/'+uid).doc('me').get();if(m.exists)me=m.data();draw()}catch(e){setTimeout(loadMe,3000)}};await loadMe();
 const sub=(q,f)=>q.onSnapshot(s=>{err='';f(s.docs.map(d=>({id:d.id,...d.data()})));draw()},e=>{err='Connection problem: '+(e.message||e.code)+'. Retrying…';draw()});
 sub(db.collection('students'),a=>D.students=a);sub(db.collection('vehicles'),a=>D.vehicles=a);sub(db.collection('fees'),a=>D.fees=a);
 sub(db.collection('days').where('date','==',today),a=>{D.days={};a.forEach(x=>D.days[x.sid]=x)});
 sub(db.collection('homework').orderBy('ts','desc').limit(20),a=>D.hw=a);sub(db.collection('remarks').orderBy('ts','desc').limit(30),a=>D.rm=a);
 sub(db.collection('log').orderBy('ts','desc').limit(15),a=>D.log=a);
 if(owner)sub(db.collection('daypins').where('date','==',today),a=>{D.dp={};a.forEach(x=>D.dp[x.sid]=x)});
 db.doc('school/main').onSnapshot(s=>{const n=s.exists?s.data():{};if(D.school.resetAt&&n.resetAt&&n.resetAt!==D.school.resetAt){location.reload();return}D.school=n;draw()},()=>{});
 ready=1;draw();setInterval(()=>{if(new Date().toLocaleDateString('en-CA')!==today)location.reload();else draw()},15000);
})();
</script></body></html>
"""

SHIM_JS = r"""// Gives the page the same small database API it used on claude.ai, backed by this server.
(() => {
  const token = () => localStorage.getItem('adm') || '';
  const ticks = new Set();  // live queries, refreshed right after every save
  const call = async (method, url, body) => {
    const r = await fetch('/api' + url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token() },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || 'Error ' + r.status);
    if (method !== 'GET') setTimeout(() => ticks.forEach(t => t()), 30);
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
    ticks.add(tick);
    const h = setInterval(tick, 2000);
    return () => { dead = true; ticks.delete(tick); clearInterval(h); };
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
  // Drop a saved admin sign-in that no longer works (for example after the password changed).
  let checked;
  const check = () => checked || (checked = (async () => {
    if (!token()) return;
    try { if (!(await call('GET', '/whoami')).admin) localStorage.removeItem('adm'); } catch (e) {}
  })());
  window.claude = { use: async name => { await check(); return name === 'db' ? db : user; } };
  window.portalApi = {
    join: (kind, code) => call('POST', '/join', { kind, code }),
    handoff: (sid, pin, date, t) => call('POST', '/handoff', { sid, pin, date, t }),
    pin: (code, date) => call('POST', '/pin', { code, date }),
    confirm: (code, date, ok, t) => call('POST', '/confirm', { code, date, ok, t }),
    reset: () => call('POST', '/reset', {})
  };

  addEventListener('DOMContentLoaded', () => {
    // Admin sign-in uses a password box on the page, not a browser pop-up:
    // pop-ups are blocked in home-screen apps, in-app browsers and some phones.
    const header = document.querySelector('header');
    const b = document.createElement('button');
    b.className = 'lk';
    b.textContent = token() ? 'Admin sign-out' : 'Admin sign-in';
    const form = document.createElement('form');
    form.className = 'card';
    form.hidden = true;
    form.innerHTML = '<h3>School admin sign-in</h3>'
      + '<div class="rw l" style="margin-top:10px"><input id="admpw" type="password" placeholder="Admin password" autocomplete="current-password">'
      + '<button class="btn" type="submit">Sign in</button><button class="lk" type="button" id="admcancel">Cancel</button></div>'
      + '<small id="admerr" role="alert"></small>';
    b.onclick = () => {
      if (token()) { localStorage.removeItem('adm'); location.reload(); return; }
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector('#admpw').focus();
    };
    form.onsubmit = async e => {
      e.preventDefault();
      const pw = form.querySelector('#admpw').value, err = form.querySelector('#admerr');
      if (!pw) { err.textContent = 'Enter the admin password.'; return; }
      err.textContent = 'Checking…';
      try {
        localStorage.setItem('adm', (await call('POST', '/login', { password: pw })).token);
        // Go straight to the admin screen after signing in.
        try { await call('PATCH', '/c/' + encodeURIComponent('data/users/' + uid) + '/me', { role: 'admin' }); } catch (x) {}
        location.reload();
      } catch (x) {
        err.textContent = x.message === 'Wrong password' ? 'Wrong password. Try again.' : x.message;
        form.querySelector('#admpw').select();
      }
    };
    form.querySelector('#admcancel').onclick = () => { form.hidden = true; form.querySelector('#admerr').textContent = ''; };
    header.appendChild(b);
    header.appendChild(form);
    check().then(() => { b.textContent = token() ? 'Admin sign-out' : 'Admin sign-in'; });
  });
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
"""


if __name__ == "__main__":
    init_db()
    if "--no-seed" not in sys.argv:
        seed()
    if ADMIN_PASSWORD == "admin123":
        print("WARNING: using the default admin password. Set ADMIN_PASSWORD before sharing this.")
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is already in use. Close the other window running the portal, "
                 "or start it on another port (set PORT=8001).")
    print(f"Serving on http://localhost:{PORT}  (admin password via ADMIN_PASSWORD). Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
