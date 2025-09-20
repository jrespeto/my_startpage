# my_startpage.py — single-file Flask "Start.me"-style dashboard
# Includes:
# - Pages (6 columns), widgets (folders), bookmarks & notes
# - Drag & drop (widgets & items), favicons, open-all
# - Modal forms for add/edit; dark mode; collapse/expand all
# - Import Chrome/Firefox bookmarks.html (folders → widgets)
# - Dedupe (widgets by name per page; bookmarks by canonical URL per widget)
# - Manage panel + search popover
# - "Duplicate Bookmarks" viewer that stays open while deleting entries
# - **FIX**: Editing a bookmark now uses JSON (AJAX) responses to avoid gunicorn 30s timeouts
#
# Auth: simple session login (admin/password by default — override via env)
# Storage: bookmarks.csv (no DB). CLI helpers included at bottom.

import csv, os, re, html, sys, uuid
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from flask import (
    Flask, request, redirect, url_for, session, flash,
    render_template_string, jsonify
)
from markupsafe import Markup, escape
from html.parser import HTMLParser

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-change-me")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "password")

CSV_FILE = "bookmarks.csv"

# CSV schema:
# rowtype,id,page_id,widget_id,column,order,name,url,notes,color
FIELDS = ["rowtype","id","page_id","widget_id","column","order","name","url","notes","color"]
DEFAULT_PAGE_ID = "home"
DEFAULT_PAGE_NAME = "My Start Page"


# ----------------------------
# Storage helpers
# ----------------------------
def ensure_csv():
    """Create file with header if missing."""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()
        save_rows([dict(rowtype="page", id=DEFAULT_PAGE_ID, page_id="", widget_id="", column="", order="0",
                        name=DEFAULT_PAGE_NAME, url="", notes="", color="")])

def load_rows():
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            r.setdefault(k, "")
    return rows

def save_rows(rows):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})

def new_id():
    return uuid.uuid4().hex

def next_order(rows, predicate):
    orders = [int(r.get("order") or 0) for r in rows if predicate(r)]
    return (max(orders) + 1) if orders else 0

def next_page_order(rows):
    return next_order(rows, lambda r: r.get("rowtype") == "page")

def next_widget_order(rows, page_id, column):
    return next_order(rows, lambda r: r.get("rowtype") == "widget" and r.get("page_id")==page_id and int(r.get("column") or 1) == int(column))

def next_item_order(rows, wid):
    return next_order(rows, lambda r: r.get("rowtype") in ("bookmark","note") and r.get("widget_id")==wid)

def find_row(rows, rid):
    for r in rows:
        if r.get("id") == rid:
            return r
    return None


# ----------------------------
# Pages / Widgets builders
# ----------------------------
def get_pages(rows):
    pages = [
        {"id": r["id"], "name": r.get("name", ""), "order": int(r.get("order") or 0)}
        for r in rows if r.get("rowtype") == "page"
    ]
    pages.sort(key=lambda p: p["order"])
    if not any(p["id"] == DEFAULT_PAGE_ID for p in pages):
        pages.insert(0, {"id": DEFAULT_PAGE_ID, "name": DEFAULT_PAGE_NAME, "order": 0})
    return pages

def get_widgets(rows, page_id):
    widgets = {}
    for r in rows:
        if r.get("rowtype") == "widget" and r.get("page_id") == page_id:
            wid = r["id"]
            widgets[wid] = {
                "id": wid,
                "name": r.get("name", ""),
                "column": int(r.get("column") or 1),
                "order": int(r.get("order") or 0),
                "items": [],
                "bookmark_count": 0,
            }
    for r in rows:
        if r.get("rowtype") in ("bookmark", "note"):
            wid = r.get("widget_id")
            if wid in widgets:
                item = {
                    "rowtype": r["rowtype"],
                    "id": r["id"],
                    "widget_id": wid,
                    "order": int(r.get("order") or 0),
                    "name": r.get("name",""),
                    "url": r.get("url",""),
                    "notes": r.get("notes",""),
                    "color": r.get("color","")
                }
                widgets[wid]["items"].append(item)
                if item["rowtype"] == "bookmark":
                    widgets[wid]["bookmark_count"] += 1
    for w in widgets.values():
        w["items"].sort(key=lambda x: x["order"])
    return sorted(widgets.values(), key=lambda w: (w["column"], w["order"]))

def get_widgets_for_select(rows, page_id):
    return [
        {"id": r["id"], "name": r.get("name",""), "column": int(r.get("column") or 1)}
        for r in rows if r.get("rowtype")=="widget" and r.get("page_id")==page_id
    ]

def get_current_page_id(rows):
    pid = session.get("page_id")
    if pid and any(r.get("rowtype")=="page" and r.get("id")==pid for r in rows):
        return pid
    return DEFAULT_PAGE_ID

def get_page_name(rows, pid):
    for r in rows:
        if r.get("rowtype")=="page" and r.get("id")==pid:
            return r.get("name") or DEFAULT_PAGE_NAME
    return DEFAULT_PAGE_NAME


# ----------------------------
# Auth
# ----------------------------
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


# ----------------------------
# Utilities: favicons + titles + highlight + URL canon
# ----------------------------
@app.template_filter("favicon")
def favicon_filter(url, size=16):
    try:
        host = urlparse(url).netloc or ""
        host = host.split("@")[-1]
    except Exception:
        host = ""
    if not host:
        return ""
    return f"https://icons.duckduckgo.com/ip3/{host}.ico"

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

def normalize_url(u: str) -> str:
    if not u: return ""
    u = u.strip()
    if not u: return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        u = "https://" + u
    return u

def canonical_url(u: str) -> str:
    """Normalize URL for dedupe comparisons."""
    u = normalize_url(u)
    try:
        pr = urlparse(u)
        scheme = pr.scheme.lower() or "https"
        netloc = pr.netloc.lower()
        # strip default ports
        if netloc.endswith(":80") and scheme=="http": netloc = netloc[:-3]
        if netloc.endswith(":443") and scheme=="https": netloc = netloc[:-4]
        path = (pr.path or "/")
        if len(path) > 1:
            path = path.rstrip("/")
        # keep query; drop fragment
        return urlunparse((scheme, netloc, path, "", pr.query, ""))
    except Exception:
        return u.strip().lower()

def guess_title_from_url(u: str) -> str:
    try:
        p = urlparse(u)
        return p.netloc or u
    except Exception:
        return u

def sniff_charset_from_headers(headers: dict) -> str:
    ctype = headers.get("Content-Type") or headers.get("content-type") or ""
    m = re.search(r"charset=([\w\-\d_]+)", ctype, flags=re.I)
    return (m.group(1) if m else "").strip()

def decode_with_fallback(data: bytes, charset_hint: str) -> str:
    for enc in [charset_hint, "utf-8", "windows-1252", "latin-1"]:
        if not enc:
            continue
        try:
            return data.decode(enc, errors="ignore")
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")

def fetch_title(url: str, timeout: float = 5.0) -> str:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (StartPage-TitleFetcher)"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)
            charset = sniff_charset_from_headers(dict(resp.headers))
        text = decode_with_fallback(raw, charset)
        m = TITLE_RE.search(text)
        if not m:
            return guess_title_from_url(url)
        title = html.unescape(m.group(1))
        return re.sub(r"\s+", " ", title).strip() or guess_title_from_url(url)
    except Exception:
        return guess_title_from_url(url)

@app.template_filter("hilite")
def jinja_hilite(text, q):
    if not text:
        return ""
    s = str(text)
    if not q:
        return escape(s)
    try:
        rx = re.compile(re.escape(q), re.I)
    except Exception:
        return escape(s)
    out = []
    last = 0
    for m in rx.finditer(s):
        out.append(escape(s[last:m.start()]))
        out.append(Markup("<mark class='hl'>") + escape(m.group(0)) + Markup("</mark>"))
        last = m.end()
    out.append(escape(s[last:]))
    return Markup("").join(out)


# ----------------------------
# Netscape bookmarks.html parser
# ----------------------------
class NetscapeBookmarksParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.dl_depth = 0
        self.stack = []  # list of (depth, widget_id)
        self.in_h3 = False
        self.h3_text = []
        self.h3_depth = 0
        self.in_a = False
        self.a_text = []
        self.a_href = ""
        self.page_title = ""
        self.in_title = False
        self.on_folder_cb = None
        self.on_link_cb = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "dl":
            self.dl_depth += 1
        elif tag.lower() == "h3":
            self.in_h3 = True
            self.h3_text = []
            self.h3_depth = self.dl_depth
        elif tag.lower() == "a":
            self.in_a = True
            self.a_text = []
            self.a_href = ""
            for k,v in attrs:
                if k.lower()=="href":
                    self.a_href = v or ""
        elif tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        tl = tag.lower()
        if tl == "h3":
            self.in_h3 = False
            name = "".join(self.h3_text).strip()
            if self.on_folder_cb:
                wid = self.on_folder_cb(name, self.dl_depth)
                self.stack.append((self.dl_depth, wid))
        elif tl == "a":
            self.in_a = False
            title = "".join(self.a_text).strip()
            if self.on_link_cb:
                self.on_link_cb(self.a_href, title, self.dl_depth)
        elif tl == "dl":
            self.dl_depth -= 1
            while self.stack and self.stack[-1][0] >= self.dl_depth:
                self.stack.pop()
        elif tl == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_h3: self.h3_text.append(data)
        if self.in_a:  self.a_text.append(data)
        if self.in_title: self.page_title += data

    def current_widget(self):
        return self.stack[-1][1] if self.stack else None


# ----------------------------
# Importer (shared by web + CLI)
# ----------------------------
def import_bookmarks_html(file_bytes: bytes, rows: list, page_id: str|None, new_page_name: str|None, column_start: int = 1):
    parser = NetscapeBookmarksParser()
    created_pages = 0
    created_widgets = 0
    created_bookmarks = 0
    title_from_file = ""

    pid_used = page_id
    if not pid_used:
        try:
            head = file_bytes[:8192].decode("utf-8", errors="ignore")
            m = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.I|re.S)
            if m: title_from_file = html.unescape(m.group(1)).strip()
        except Exception:
            pass
        np_name = (new_page_name or title_from_file or "Imported").strip()
        pid_used = new_id()
        rows.append(dict(rowtype="page", id=pid_used, page_id="", widget_id="", column="", order=str(next_page_order(rows)),
                         name=np_name, url="", notes="", color=""))
        created_pages += 1

    col = max(1, min(6, int(column_start or 1)))

    def make_widget(name: str):
        nonlocal col, created_widgets
        if not name.strip(): name = "Unnamed"
        wid = new_id()
        rows.append(dict(rowtype="widget", id=wid, page_id=pid_used, widget_id="", column=str(col),
                         order=str(next_widget_order(rows, pid_used, col)), name=name.strip(), url="", notes="", color=""))
        created_widgets += 1
        col = (col % 6) + 1
        return wid

    fallback_widget_id = None

    def on_folder(name, depth):
        return make_widget(name)

    def on_link(href, title, depth):
        nonlocal created_bookmarks, fallback_widget_id
        href = normalize_url(href or "")
        if not href: return
        title = title.strip() or guess_title_from_url(href)
        wid = parser.current_widget()
        if not wid:
            if not fallback_widget_id:
                fallback_widget_id = make_widget("Imported Links")
            wid = fallback_widget_id
        rows.append(dict(rowtype="bookmark", id=new_id(), page_id="", widget_id=wid, column="",
                         order=str(next_item_order(rows, wid)), name=title, url=href, notes="", color=""))
        created_bookmarks += 1

    parser.on_folder_cb = on_folder
    parser.on_link_cb = on_link

    try:
        parser.feed(file_bytes.decode("utf-8", errors="ignore"))
    except Exception:
        parser = NetscapeBookmarksParser()
        parser.on_folder_cb = on_folder
        parser.on_link_cb = on_link
        parser.feed(file_bytes.decode("latin-1", errors="ignore"))

    return created_pages, created_widgets, created_bookmarks, pid_used, (parser.page_title or title_from_file)


# ----------------------------
# DEDUPE LOGIC
# ----------------------------
def dedupe_widgets(rows):
    """
    Merge widgets with identical (page_id, name.lower().strip()).
    Returns removed_count and a mapping {old_wid: primary_wid}.
    """
    key2primary = {}
    old2new = {}
    removed = 0

    widgets = [r for r in rows if r.get("rowtype")=="widget"]
    widgets.sort(key=lambda r: (r.get("page_id",""), int(r.get("column") or 1), int(r.get("order") or 0)))

    for w in widgets:
        key = (w.get("page_id",""), (w.get("name","") or "").strip().lower())
        if key not in key2primary:
            key2primary[key] = w["id"]
        else:
            primary = key2primary[key]
            if w["id"] == primary:
                continue
            old2new[w["id"]] = primary
            removed += 1

    if not old2new:
        return 0, {}

    # Move items to primary widget
    for r in rows:
        if r.get("rowtype") in ("bookmark","note"):
            wid = r.get("widget_id")
            if wid in old2new:
                new_wid = old2new[wid]
                r["widget_id"] = new_wid
                r["order"] = str(next_item_order(rows, new_wid))

    rows[:] = [r for r in rows if not (r.get("rowtype")=="widget" and r.get("id") in old2new)]
    return removed, old2new

def dedupe_bookmarks(rows):
    """
    Within each widget, remove duplicate bookmarks by canonical URL.
    Keep the first; if it has empty name and duplicate has one, copy title.
    """
    removed = 0
    by_wid = {}
    for r in rows:
        if r.get("rowtype")=="bookmark":
            by_wid.setdefault(r.get("widget_id",""), []).append(r)

    for wid, blist in by_wid.items():
        try:
            blist.sort(key=lambda r: int(r.get("order") or 0))
        except Exception:
            pass
        seen = {}
        for b in blist:
            cu = canonical_url(b.get("url",""))
            if not cu:
                cu = b.get("url","").strip().lower()
            if cu not in seen:
                seen[cu] = b
            else:
                primary = seen[cu]
                if not (primary.get("name") or "").strip() and (b.get("name") or "").strip():
                    primary["name"] = b.get("name","")
                b["_delete"] = True

    before = len(rows)
    rows[:] = [r for r in rows if not r.get("_delete")]
    removed += (before - len(rows))
    return removed

def run_dedupe(rows):
    wr, _map = dedupe_widgets(rows)
    br = dedupe_bookmarks(rows)
    return wr, br


# ----------------------------
# Duplicate Bookmarks viewer
# ----------------------------
def list_duplicate_bookmarks(rows, page_id: str):
    """Return a list of duplicates on a page: [{key, display, entries:[{id, wid, widget, name, url}...]}...]"""
    widget_name = {r["id"]: r.get("name","") for r in rows if r.get("rowtype")=="widget" and r.get("page_id")==page_id}
    buckets = {}
    for r in rows:
        if r.get("rowtype")!="bookmark":
            continue
        wid = r.get("widget_id")
        if wid not in widget_name:
            continue
        cu = canonical_url(r.get("url",""))
        if not cu:
            cu = (r.get("url","") or "").strip().lower()
        if not cu:
            continue
        entry = {
            "id": r["id"],
            "wid": wid,
            "widget": widget_name[wid],
            "name": r.get("name","") or r.get("url",""),
            "url": r.get("url",""),
        }
        buckets.setdefault(cu, []).append(entry)

    out = []
    for key, entries in buckets.items():
        if len(entries) < 2:
            continue
        display = next((e["name"] for e in entries if (e["name"] or "").strip()), "") or key
        entries.sort(key=lambda e: (e["widget"].lower(), e["name"].lower()))
        out.append({"key": key, "display": display, "entries": entries})
    out.sort(key=lambda g: g["display"].lower())
    return out


# ----------------------------
# Templates
# ----------------------------
BASE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{{ page_title or "Start Page" }}</title>
  <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
  <style>
    :root{
      --gap: 0.7rem; --radius: 6px; --btn-radius: 6px; --muted:#6b7280; --brand:#2b6cb0; --danger:#ef4444;
      --bg:#f5f7fb; --text:#0f172a; --card-bg:#ffffff; --header-bg:#253858; --border:#e5e7eb; --hover:#f3f4f6;
      --link-size: 0.92rem; --overlay: rgba(15, 23, 42, .55); --hl:#fde68a;
    }
    .dark{ --bg:#0b1220; --text:#e5e7eb; --card-bg:#0f172a; --header-bg:#0e223c; --border:#1f2937; --hover:#142036; --brand:#7aa2ff; --muted:#94a3b8; --overlay: rgba(0,0,0,.6); --hl:#8b6f00; }
    *{ box-sizing: border-box; } html, body { height: 100%; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:var(--bg); color:var(--text); margin:0; }
    header { background:var(--header-bg); color:#fff; padding:0.6rem 1rem; display:flex; justify-content:space-between; align-items:center; gap:1rem; position:relative; z-index:5; }
    header a { color:#fff; text-decoration:none; font-weight:600; }
    .titlebar { display:flex; align-items:center; gap:.6rem; }
    .page-select { background:#fff; color:#111827; border:0; border-radius:var(--btn-radius); padding:0.3rem 0.5rem; font-weight:600; }
    .dark .page-select { background:#0f172a; color:#e5e7eb; border:1px solid var(--border); }
    .container { padding:0.8rem; max-width: min(2400px, 98vw); margin: 0 auto; }
    .flash { padding:0.45rem 0.7rem; border-radius:var(--radius); margin: 0 0 0.6rem 0; }
    .flash.success { background:#e8fff2; color:#155d2e; } .flash.info { background:#eef2ff; color:#1e3a8a; } .flash.danger { background:#fff1f2; color:#991b1b; }
    .dark .flash.success { background:#0f2a1b; color:#86efac; } .dark .flash.info { background:#0f1530; color:#93c5fd; } .dark .flash.danger { background:#2a0f14; color:#fda4af; }

    .grid { display:grid; gap: var(--gap); grid-template-columns: repeat(6, minmax(0, 1fr)); }
    .col { min-width: 0; display:flex; flex-direction:column; gap: var(--gap); }

    .widget { background:var(--card-bg); padding:0.6rem; border-radius:var(--radius); box-shadow:0 1px 6px rgba(0,0,0,.06); position:relative; border:1px solid var(--border); }
    .dark .widget { box-shadow: 0 1px 0 rgba(255,255,255,0.03); }
    .widget-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem; gap:.5rem; position:relative; }
    .widget-title { font-size:1.0rem; font-weight:800; margin:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer; display:flex; align-items:center; gap:.4rem; }
    .widget-tools { display:flex; align-items:center; gap:.3rem; }
    .drag-handle { color:#9ca3af; cursor:grab; }
    .menu-btn { background:transparent; border:0; cursor:pointer; color:inherit; padding: .15rem; border-radius:var(--btn-radius); }
    .menu-btn:hover { background:var(--hover); }
    .dropdown { position:absolute; right:0; top:1.9rem; background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius); box-shadow:0 6px 20px rgba(0,0,0,.12); display:none; min-width:260px; z-index:20; }
    .dropdown.show { display:block; }
    .dropdown a, .dropdown button { width:100%; text-align:left; background:none; border:0; padding:.45rem .7rem; cursor:pointer; font-size:0.9rem; display:flex; align-items:center; gap:.5rem; color:inherit; border-radius:0; }
    .dropdown a:hover, .dropdown button:hover { background:var(--hover); }

    .items { display:flex; flex-direction:column; gap: 0.18rem; }
    .item { display:flex; align-items:center; gap:0.35rem; padding:0.15rem 0.3rem; border-radius:4px; }
    .item:hover { background: var(--hover); }
    .grip { color:#9ca3af; cursor:grab; font-size:0.88rem; }
    .favicon { width:16px; height:16px; border-radius:3px; box-shadow:0 0 0 1px var(--border) inset; background:#fff; flex:0 0 auto; cursor:grab; }
    .dark .favicon { background:transparent; box-shadow:0 0 0 1px #111 inset; }
    .label { min-width:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; line-height:1.15; }
    .label a { color: var(--brand); text-decoration:none; overflow-wrap:anywhere; font-size: var(--link-size); }
    .label a:hover { text-decoration:underline; }
    .note { white-space:pre-wrap; color:inherit; background:var(--hover); padding:0.35rem 0.45rem; border-radius:4px; width:100%; overflow-wrap:anywhere; font-size:0.9rem; }

    .btn { display:inline-flex; align-items:center; gap:0.35rem; background:var(--brand); color:#fff; text-decoration:none; border:0; padding:0.3rem 0.55rem; border-radius:var(--btn-radius); cursor:pointer; font-size:0.88rem; }
    .btn.small { padding: 0.22rem 0.45rem; font-size: 0.82rem; }
    .btn.ghost { background:var(--hover); color:inherit; }
    .btn.danger { background:var(--danger); }
    .muted { color: var(--muted); }

    .collapsed .items { display:none; }
    .collapsed .dropdown { display:none; }

    .pill { font-size:.75rem; padding:.05rem .45rem; border:1px solid var(--border); border-radius:999px; color:#e2e8f0; background:rgba(255,255,255,.12); }
    .dark .pill { border-color: var(--border); color:#94a3b8; background:rgba(0,0,0,.25); }

    /* Modal */
    .modal { display:none; position:fixed; inset:0; z-index:1000; background:var(--overlay); align-items:center; justify-content:center; padding:1rem; }
    .modal.show { display:flex; }
    .modal-box { background:var(--card-bg); color:var(--text); width:min(860px, 96vw); border-radius:10px; border:1px solid var(--border); box-shadow:0 15px 60px rgba(0,0,0,.25); max-height:90vh; display:flex; flex-direction:column; }
    .modal-head { display:flex; justify-content:space-between; align-items:center; padding:.8rem 1rem; border-bottom:1px solid var(--border); }
    .modal-body { padding:1rem; overflow:auto; }
    .modal-foot { display:flex; justify-content:flex-end; gap:.5rem; padding: .8rem 1rem; border-top:1px solid var(--border); }
    .modal h3 { margin:0; font-size:1.05rem; }
    .modal label { display:block; margin:.4rem 0 .2rem; font-weight:600; }
    .modal input[type="text"], .modal input[type="url"], .modal textarea, .modal select, .modal input[type="color"], .modal input[type="file"] {
      width:100%; padding:.45rem .55rem; border-radius:6px; border:1px solid var(--border); background:var(--card-bg); color:inherit;
      appearance:none; -webkit-appearance:none; -moz-appearance:none;
    }
    .modal select { background-image:
        linear-gradient(45deg, transparent 50%, currentColor 50%),
        linear-gradient(135deg, currentColor 50%, transparent 50%);
      background-position:
        calc(100% - 18px) calc(50% - 3px),
        calc(100% - 13px) calc(50% - 3px);
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
      padding-right: 28px;
    }
    .modal option { background: var(--card-bg); color: var(--text); }
    .dark .modal select, .dark .modal input[type="text"], .dark .modal input[type="url"], .dark .modal textarea, .dark .modal input[type="color"], .dark .modal input[type="file"] {
      background: #0f172a; color: #e5e7eb; border-color: var(--border);
    }
    .dark .modal option { background:#0f172a; color:#e5e7eb; }

    .xbtn { border:0; background:transparent; cursor:pointer; font-size:1.2rem; color:inherit; }

    .manage-list { list-style:none; padding:0; margin:0; }
    .manage-list li { display:flex; align-items:center; gap:.6rem; padding:.45rem .4rem; border-top:1px solid var(--border); background: var(--card-bg); }
    .dark .manage-list li { background: #0f172a; }
    .manage-actions { margin-left:auto; display:flex; gap:.4rem; }
    .group-title { margin:.2rem 0 .3rem; font-weight:800; font-size:.9rem; opacity:.8; display:flex; align-items:center; gap:.4rem; }
    .group { border:1px dashed var(--border); border-radius:8px; padding:.4rem .6rem; margin:.4rem 0; }

    /* Search popover */
    .top-right { display:flex; align-items:center; gap:.45rem; position:relative; }
    .search-wrap { position:relative; }
    .search-popover {
      display:none; position:absolute; right:0; top: calc(100% + 8px); z-index:30;
      width:min(720px, 92vw); background:var(--card-bg); color:var(--text);
      border:1px solid var(--border); border-radius:10px; box-shadow:0 12px 40px rgba(0,0,0,.25);
    }
    .search-popover.show { display:block; }
    .sp-head { display:flex; align-items:center; gap:.5rem; padding:.6rem .8rem; border-bottom:1px solid var(--border); }
    .sp-head input[type="search"]{
      flex:1; border:1px solid var(--border); border-radius:8px; padding:.45rem .55rem; background:var(--card-bg); color:inherit;
      outline:0;
    }
    .sp-body { max-height: 60vh; overflow:auto; padding:.2rem 0; }
    .sp-item { display:flex; align-items:center; gap:.6rem; padding:.45rem .8rem; border-top:1px solid var(--border); text-decoration:none; color:inherit; }
    .sp-item:hover { background:var(--hover); }
    .sp-item .title { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sp-item .sub { color:var(--muted); font-size:.85rem; overflow:hidden; text-overflow:ellipsis; }
    mark.hl { background: var(--hl); color: inherit; padding: 0 .15rem; border-radius: 3px; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js"></script>
</head>
<body>
  <header>
    <div class="titlebar">
      <i class="fa-solid fa-earth-americas"></i>
      <form method="post" action="{{ url_for('switch_page') }}">
        <select name="page_id" class="page-select" onchange="this.form.submit()">
          {% for p in pages %}
            <option value="{{ p['id'] }}" {% if p['id']==current_page_id %}selected{% endif %}>{{ p['name'] }}</option>
          {% endfor %}
        </select>
      </form>
    </div>
    <div class="top-right">
      <span class="pill" title="Total bookmarks on this page"><i class="fa-solid fa-bookmark"></i> {{ total_bookmarks or 0 }}</span>

      <div class="search-wrap">
        <button id="searchToggle" class="btn ghost small" type="button" title="Search bookmarks">
          <i class="fa-solid fa-magnifying-glass"></i> Search
        </button>
        <div id="searchPanel" class="search-popover" aria-hidden="true">
          <div class="sp-head">
            <i class="fa-solid fa-magnifying-glass"></i>
            <input id="searchInput" type="search" placeholder="Search bookmarks on this page...">
            <button id="searchClose" class="btn ghost small" type="button" title="Close"><i class="fa-solid fa-xmark"></i></button>
          </div>
          <div class="sp-body" id="searchResults"></div>
        </div>
      </div>

      <button id="toggleAll" class="btn ghost small" type="button" title="Collapse/Expand all"><i class="fa-solid fa-compress"></i> Collapse all</button>
      <button id="themeToggle" class="btn ghost small" type="button" title="Toggle dark mode"><i class="fa-solid fa-moon"></i> Dark</button>
      {% if session.get('logged_in') %}
        <button class="btn ghost small" type="button" id="manageButton"><i class="fa-solid fa-sliders"></i> Manage</button>
        <a class="btn danger small" href="{{ url_for('logout') }}"><i class="fa-solid fa-right-from-bracket"></i> Logout</a>
      {% else %}
        <a class="btn small" href="{{ url_for('login') }}"><i class="fa-solid fa-right-to-bracket"></i> Login</a>
      {% endif %}
    </div>
  </header>

  <div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category,msg in messages %}
        <div class="flash {{ category }}">{{ msg }}</div>
      {% endfor %}
    {% endwith %}

    {{ content|safe }}
  </div>

  <!-- MODAL -->
  <div id="modal" class="modal" aria-hidden="true">
    <div class="modal-box">
      <div class="modal-head">
        <h3 id="modalTitle">Form</h3>
        <button class="xbtn" id="modalClose" title="Close"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="modal-body">
        <!-- Bookmark ADD -->
        <form id="bookmarkForm" method="post" action="{{ url_for('add_bookmark') }}" style="display:none" enctype="multipart/form-data">
          <input type="hidden" name="widget_id" id="bm_widget_id">
          <label>Widget</label>
          <select name="widget_id_select" id="bm_widget_select">
            {% for w in (widgets_select or []) %}
              <option value="{{ w['id'] }}"> {{ w['name'] }} (col {{ w['column'] }})</option>
            {% endfor %}
          </select>
          <label style="margin-top:.5rem;">Title (optional)</label>
          <input type="text" name="name" placeholder="Custom title">
          <label>URL</label>
          <input type="url" name="url" placeholder="https://example.com">
          <details style="margin-top:.7rem;">
            <summary style="cursor:pointer">Add multiple URLs</summary>
            <div style="margin-top:.4rem;">
              <label>URLs (comma or newline separated)</label>
              <textarea name="urls_bulk" rows="5" placeholder="google.com, https://cnn.com"></textarea>
              <div style="display:flex; align-items:center; gap:.4rem; margin:.4rem 0;">
                <input type="checkbox" id="auto_titles" name="auto_titles" value="1" checked>
                <label for="auto_titles" style="margin:0; font-weight:500;">Fetch page titles automatically</label>
              </div>
            </div>
          </details>
        </form>

        <!-- Note ADD -->
        <form id="noteForm" method="post" action="{{ url_for('add_note') }}" style="display:none">
          <input type="hidden" name="widget_id" id="note_widget_id">
          <label>Widget</label>
          <select name="widget_id_select" id="note_widget_select">
            {% for w in (widgets_select or []) %}
              <option value="{{ w['id'] }}"> {{ w['name'] }} (col {{ w['column'] }})</option>
            {% endfor %}
          </select>
          <label>Note</label>
          <textarea name="notes" rows="7" placeholder="Type your note..."></textarea>
          <label>Background color</label>
          <input type="color" name="color" value="#FEF3C7">
        </form>

        <!-- Manage (Actions + Items) -->
        <div id="managePanel" style="display:none">
          <p class="muted" id="manageWidgetName"></p>

          <div id="actionsPanel">
            <div class="group">
              <div class="group-title"><i class="fa-solid fa-file-lines"></i> Page — add / edit / delete</div>
              <ul class="manage-list">
                <li><i class="fa-solid fa-plus"></i> Add page<div class="manage-actions"><button class="btn small" type="button" data-open="addPageForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
                <li><i class="fa-solid fa-pen-to-square"></i> Edit page title<div class="manage-actions"><button class="btn small" type="button" data-open="renamePageForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
                <li><i class="fa-solid fa-trash"></i> Delete page<div class="manage-actions"><button class="btn small" type="button" data-open="removePageForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
              </ul>
            </div>
            <div class="group">
              <div class="group-title"><i class="fa-solid fa-layer-group"></i> Widget — add / edit / delete</div>
              <ul class="manage-list">
                <li><i class="fa-solid fa-plus"></i> Add widget<div class="manage-actions"><button class="btn small" type="button" data-open="addWidgetForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
                <li><i class="fa-solid fa-pen-to-square"></i> Rename widget<div class="manage-actions"><button class="btn small" type="button" data-open="renameWidgetForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
                <li><i class="fa-solid fa-trash"></i> Delete widget<div class="manage-actions"><button class="btn small" type="button" data-open="removeWidgetForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
              </ul>
            </div>
            <div class="group">
              <div class="group-title"><i class="fa-solid fa-file-import"></i> Import / tools</div>
              <ul class="manage-list">
                <li><i class="fa-solid fa-file-import"></i> Import bookmarks (HTML)<div class="manage-actions"><button class="btn small" type="button" data-open="importForm"><i class="fa-solid fa-arrow-right"></i></button></div></li>
                <li><i class="fa-solid fa-broom"></i> Dedupe (widgets & bookmarks)<div class="manage-actions"><button class="btn small" id="runDedupeBtn" type="button" title="Merge duplicate widgets and bookmarks"><i class="fa-solid fa-play"></i></button></div></li>
                <li><i class="fa-solid fa-clone"></i> Show duplicate bookmarks<div class="manage-actions"><button class="btn small" type="button" data-open="dupesPanel"><i class="fa-solid fa-arrow-right"></i></button></div></li>
              </ul>
            </div>
          </div>

          <ul class="manage-list" id="manageList" style="display:none"></ul>
        </div>

        <!-- Duplicate Bookmarks Panel -->
        <div id="dupesPanel" style="display:none">
          <p class="muted">Shows duplicate bookmarks across all widgets on the <strong>current page</strong>.</p>
          <div id="dupesBox" class="group" style="max-height:60vh; overflow:auto;"></div>
        </div>

        <!-- Edit Bookmark -->
        <form id="editBookmarkForm" method="post" style="display:none">
          <input type="hidden" name="id" id="eb_id">
          <label>Title</label>
          <input type="text" name="name" id="eb_name" required>
          <label>URL</label>
          <input type="url" name="url" id="eb_url" required>
          <label>Widget</label>
          <select name="widget_id" id="eb_widget">
            {% for w in (widgets_select or []) %}
              <option value="{{ w['id'] }}">{{ w['name'] }} (col {{ w['column'] }})</option>
            {% endfor %}
          </select>
        </form>

        <!-- Edit Note -->
        <form id="editNoteForm" method="post" style="display:none">
          <input type="hidden" name="id" id="en_id">
          <label>Note</label>
          <textarea name="notes" id="en_notes" rows="7" required></textarea>
          <label>Background color</label>
          <input type="color" name="color" id="en_color" value="#FEF3C7">
          <label>Widget</label>
          <select name="widget_id" id="en_widget">
            {% for w in (widgets_select or []) %}
              <option value="{{ w['id'] }}">{{ w['name'] }} (col {{ w['column'] }})</option>
            {% endfor %}
          </select>
        </form>

        <!-- Add Page -->
        <form id="addPageForm" method="post" action="{{ url_for('add_page') }}" style="display:none">
          <label>Page name</label>
          <input type="text" name="name" placeholder="New page name" required>
        </form>

        <!-- Rename Page -->
        <form id="renamePageForm" method="post" action="{{ url_for('rename_page') }}" style="display:none">
          <label>Choose page</label>
          <select name="page_id" id="rnp_select">
            {% for p in pages %}
              <option value="{{ p['id'] }}">{{ p['name'] }}</option>
            {% endfor %}
          </select>
          <label>New title</label>
          <input type="text" name="name" id="rnp_name" placeholder="New page title" required>
        </form>

        <!-- Add Widget -->
        <form id="addWidgetForm" method="post" action="{{ url_for('add_widget') }}" style="display:none">
          <label>Widget name</label>
          <input type="text" name="name" placeholder="Widget name" required>
          <label>Page</label>
          <select name="page_id">
            {% for p in pages %}
              <option value="{{ p['id'] }}" {% if p['id']==current_page_id %}selected{% endif %}>{{ p['name'] }}</option>
            {% endfor %}
          </select>
          <label>Column</label>
          <select name="column">
            {% for i in range(1,7) %}<option value="{{ i }}">Column {{ i }}</option>{% endfor %}
          </select>
        </form>

        <!-- Remove Page -->
        <form id="removePageForm" method="post" action="{{ url_for('delete_page') }}" style="display:none">
          <label>Remove page</label>
          <select name="page_id">
            {% for p in pages %}
              <option value="{{ p['id'] }}">{{ p['name'] }}</option>
            {% endfor %}
          </select>
          <p class="muted">Removing a page will also delete its widgets and their items.</p>
        </form>

        <!-- Remove Widget -->
        <form id="removeWidgetForm" method="post" action="{{ url_for('delete_widget') }}" style="display:none">
          <label>Remove widget (current page)</label>
          <select name="widget_id" id="rmw_select">
            {% for w in (widgets_select or []) %}
              <option value="{{ w['id'] }}">{{ w['name'] }} (col {{ w['column'] }})</option>
            {% endfor %}
          </select>
          <p class="muted">This deletes the widget and all of its bookmarks/notes.</p>
        </form>

        <!-- Rename Widget -->
        <form id="renameWidgetForm" method="post" action="{{ url_for('rename_widget') }}" style="display:none">
          <label>Choose widget</label>
          <select name="widget_id" id="rnw_select">
            {% for w in (widgets_select or []) %}
              <option value="{{ w['id'] }}">{{ w['name'] }} (col {{ w['column'] }})</option>
            {% endfor %}
          </select>
          <label>New title</label>
          <input type="text" name="name" id="rnw_name" placeholder="New widget title" required>
        </form>

        <!-- Move Widget -->
        <form id="moveWidgetForm" method="post" action="{{ url_for('move_widget') }}" style="display:none">
          <input type="hidden" name="widget_id" id="mvw_id">
          <label>Move to page</label>
          <select name="page_id" id="mvw_page">
            {% for p in pages %}
              <option value="{{ p['id'] }}">{{ p['name'] }}</option>
            {% endfor %}
          </select>
          <label>Target column</label>
          <select name="column" id="mvw_col">
            {% for i in range(1,7) %}<option value="{{ i }}">Column {{ i }}</option>{% endfor %}
          </select>
        </form>

        <!-- Copy Widget -->
        <form id="copyWidgetForm" method="post" action="{{ url_for('copy_widget') }}" style="display:none">
          <input type="hidden" name="widget_id" id="cpw_id">
          <label>Copy to page</label>
          <select name="page_id" id="cpw_page">
            {% for p in pages %}
              <option value="{{ p['id'] }}">{{ p['name'] }}</option>
            {% endfor %}
          </select>
          <label>Target column</label>
          <select name="column" id="cpw_col">
            {% for i in range(1,7) %}<option value="{{ i }}">Column {{ i }}</option>{% endfor %}
          </select>
        </form>

        <!-- Import Bookmarks (HTML) -->
        <form id="importForm" method="post" action="{{ url_for('import_html') }}" style="display:none" enctype="multipart/form-data">
          <label>Bookmarks HTML file</label>
          <input type="file" name="html" accept=".html,text/html" required>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:.6rem; margin-top:.5rem;">
            <div>
              <label>Target page (existing)</label>
              <select name="page_id">
                <option value="">(create new page)</option>
                {% for p in pages %}
                  <option value="{{ p['id'] }}" {% if p['id']==current_page_id %}selected{% endif %}>{{ p['name'] }}</option>
                {% endfor %}
              </select>
            </div>
            <div>
              <label>Or new page name</label>
              <input type="text" name="new_page_name" placeholder="Imported from browser">
            </div>
          </div>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:.6rem; margin-top:.5rem;">
            <div>
              <label>Start column</label>
              <select name="column_start">
                {% for i in range(1,7) %}<option value="{{ i }}">Column {{ i }}</option>{% endfor %}
              </select>
            </div>
            <div>
              <label class="muted" style="margin-top:1.9rem;">Folders become widgets, links go inside.</label>
            </div>
          </div>
        </form>

      </div>
      <div class="modal-foot" id="modalFoot">
        <button class="btn" type="button" id="modalSubmit"><i class="fa-solid fa-paper-plane"></i> Submit</button>
      </div>
    </div>
  </div>

  <script>
  // Theme toggle
  (function() {
    const key = 'theme';
    const btn = document.getElementById('themeToggle');
    function apply(t){
      document.documentElement.classList.toggle('dark', t === 'dark');
      if(btn){
        btn.innerHTML = (t==='dark')
          ? '<i class="fa-solid fa-sun"></i> Light'
          : '<i class="fa-solid fa-moon"></i> Dark';
      }
    }
    let stored = localStorage.getItem(key) || 'light';
    apply(stored);
    if(btn){
      btn.addEventListener('click', function(){
        stored = (document.documentElement.classList.toggle('dark')) ? 'dark' : 'light';
        localStorage.setItem(key, stored);
        apply(stored);
      });
    }
  })();

  // Collapse/Expand All
  (function(){
    const btn = document.getElementById('toggleAll');
    let collapsedAll = localStorage.getItem('collapsedAll') === '1';
    function apply(){
      document.querySelectorAll('.widget').forEach(w => {
        if(collapsedAll) w.classList.add('collapsed'); else w.classList.remove('collapsed');
      });
      if(btn){
        btn.innerHTML = collapsedAll
          ? '<i class="fa-solid fa-expand"></i> Expand all'
          : '<i class="fa-solid fa-compress"></i> Collapse all';
      }
    }
    apply();
    if(btn){
      btn.addEventListener('click', ()=>{
        collapsedAll = !collapsedAll;
        localStorage.setItem('collapsedAll', collapsedAll ? '1' : '0');
        apply();
      });
    }
  })();

  // Search popover
  (function(){
    const toggle = document.getElementById('searchToggle');
    const panel = document.getElementById('searchPanel');
    const input = document.getElementById('searchInput');
    const closeBtn = document.getElementById('searchClose');
    const results = document.getElementById('searchResults');

    function openPanel(){
      panel.classList.add('show');
      panel.setAttribute('aria-hidden','false');
      setTimeout(()=>input.focus(), 0);
      input.select();
      doSearch(input.value);
    }
    function closePanel(){
      panel.classList.remove('show');
      panel.setAttribute('aria-hidden','true');
    }
    if(toggle){ toggle.addEventListener('click', (e)=>{ e.stopPropagation(); if(panel.classList.contains('show')) closePanel(); else openPanel(); }); }
    if(closeBtn){ closeBtn.addEventListener('click', closePanel); }
    document.addEventListener('click', (e)=>{ if(!panel.contains(e.target) && !toggle.contains(e.target)) closePanel(); });
    document.addEventListener('keydown', (e)=>{ if(e.key === 'Escape') closePanel(); });

    let t = null, lastQ = '';
    input.addEventListener('input', ()=>{ clearTimeout(t); t = setTimeout(()=> doSearch(input.value), 120); });

    function highlight(text, q){
      if(!q) return text;
      try {
        const rx = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'ig');
        return text.replace(rx, m => `<mark class="hl">${m}</mark>`);
      } catch(e){ return text; }
    }

    async function doSearch(q){
      q = (q||'').trim();
      if(q === lastQ && results.dataset.had === '1') return;
      lastQ = q;
      results.innerHTML = '<div class="sp-item"><span class="sub">Searching...</span></div>';
      try{
        const rsp = await fetch(`/api/search?q=${encodeURIComponent(q)}`, {credentials:'same-origin'});
        const data = await rsp.json();
        const arr = data.results || [];
        if(arr.length === 0){
          results.innerHTML = '<div class="sp-item"><span class="sub">No matches.</span></div>';
          results.dataset.had = '1';
          return;
        }
        results.innerHTML = '';
        arr.forEach(r => {
          const el = document.createElement('div');
          el.className = 'sp-item';
          el.innerHTML = `
            <img class="favicon" src="${r.favicon || ''}" alt="" onerror="this.remove()" referrerpolicy="no-referrer">
            <div style="min-width:0; flex:1;">
              <div class="title">${highlight(r.name || r.url, q)} <span class="sub">(${r.widget})</span></div>
              <div class="sub">${highlight(r.url, q)}</div>
            </div>
            <a href="${r.url}" target="_blank" rel="noopener noreferrer" class="btn small"><i class="fa-solid fa-up-right-from-square"></i></a>
          `;
          results.appendChild(el);
        });
        results.dataset.had = '1';
      }catch(e){
        results.innerHTML = '<div class="sp-item"><span class="sub">Search failed.</span></div>';
        results.dataset.had = '1';
      }
    }
  })();

  // Modal helpers
  const modal = document.getElementById('modal');
  const modalTitle = document.getElementById('modalTitle');
  const modalClose = document.getElementById('modalClose');
  const modalSubmit = document.getElementById('modalSubmit');
  const modalFoot = document.getElementById('modalFoot');
  const actionsPanel = document.getElementById('actionsPanel');

  function hideAllSections(){
    document.querySelectorAll('#modal form, #managePanel, #dupesPanel').forEach(el => el.style.display='none');
  }
  function showModalWith(sectionId, title){
    hideAllSections();
    const el = document.getElementById(sectionId);
    if(el) el.style.display='';
    if(sectionId==='managePanel'){
      if(actionsPanel) actionsPanel.style.display = '';
      const list = document.getElementById('manageList');
      if(list){ list.style.display='none'; list.innerHTML=''; }
      document.getElementById('manageWidgetName').textContent = '';
      document.getElementById('managePanel').dataset.wid = '';
      modalFoot.style.display='none';
    }
    if(sectionId==='dupesPanel'){
      modalFoot.style.display='none';
    }
    modalTitle.textContent = title || 'Form';
    modal.classList.add('show');
    modal.setAttribute('aria-hidden', 'false');
    modalSubmit.dataset.target = sectionId;
    if(el && el.tagName === 'FORM'){ modalFoot.style.display='flex'; }
    else if(sectionId!=='managePanel' && sectionId!=='dupesPanel'){ modalFoot.style.display='none'; }
  }
  function hideModal(){
    modal.classList.remove('show');
    modal.setAttribute('aria-hidden', 'true');
    modalSubmit.dataset.target = '';
    modalFoot.style.display='none';
  }
  modalClose.addEventListener('click', hideModal);
  modal.addEventListener('click', (e)=>{ if(e.target===modal) hideModal(); });

  function getWidForSection(id){
    if(id==='managePanel') return document.getElementById('managePanel').dataset.wid || null;
    if(id==='bookmarkForm') return document.getElementById('bm_widget_select')?.value || null;
    if(id==='noteForm') return document.getElementById('note_widget_select')?.value || null;
    if(id==='editBookmarkForm') return document.getElementById('eb_widget')?.value || null;
    if(id==='editNoteForm') return document.getElementById('en_widget')?.value || null;
    if(id==='renameWidgetForm') return document.getElementById('rnw_select')?.value || null;
    return null;
  }
  function rememberModal(sectionId, title, wid){
    try{ localStorage.setItem('reopenModal', JSON.stringify({sectionId, title, wid})); }catch(e){}
  }
  window.addEventListener('DOMContentLoaded', ()=>{
    const raw = localStorage.getItem('reopenModal');
    if(raw){
      localStorage.removeItem('reopenModal');
      try{
        const s = JSON.parse(raw);
        if(s && s.sectionId){
          if(s.sectionId==='managePanel' && s.wid){
            showModalWith('managePanel', 'Manage Items');
            const widget = document.querySelector(`.widget[data-wid="${s.wid}"]`);
            if(widget) buildManageListForWidget(widget);
            return;
          }
          if(s.sectionId==='bookmarkForm' && s.wid){ const x=document.getElementById('bm_widget_select'); if(x) x.value=s.wid; }
          if(s.sectionId==='noteForm' && s.wid){ const x=document.getElementById('note_widget_select'); if(x) x.value=s.wid; }
          if(s.sectionId==='editBookmarkForm' && s.wid){ const x=document.getElementById('eb_widget'); if(x) x.value=s.wid; }
          if(s.sectionId==='editNoteForm' && s.wid){ const x=document.getElementById('en_widget'); if(x) x.value=s.wid; }
          if(s.sectionId==='renameWidgetForm' && s.wid){ const x=document.getElementById('rnw_select'); if(x) x.value=s.wid; }
          showModalWith(s.sectionId, s.title || 'Form');
        }
      }catch(e){}
    }
  });

  // ====== DUPES state (keeps modal open) ======
  let DUPES = [];

  const managePanelEl = document.getElementById('managePanel');
  managePanelEl.addEventListener('click', function(e){
    const btn = e.target.closest('[data-open]');
    if(btn){
      const id = btn.getAttribute('data-open');
      const titles = {
        addPageForm:'Add Page', renamePageForm:'Rename Page', removePageForm:'Remove Page',
        addWidgetForm:'Add Widget', renameWidgetForm:'Rename Widget', removeWidgetForm:'Remove Widget',
        importForm:'Import Bookmarks (HTML)', dupesPanel:'Duplicate Bookmarks'
      };
      showModalWith(id, titles[id] || 'Action');
      if(id==='dupesPanel'){
        (async ()=>{
          const box = document.getElementById('dupesBox');
          box.innerHTML = '<div class="muted">Scanning…</div>';
          try{
            const rsp = await fetch('/api/dupes', {credentials:'same-origin'});
            const data = await rsp.json();
            DUPES = data.groups || [];
            renderDupes();
          }catch(e){
            DUPES = [];
            box.innerHTML = '<div class="muted">Failed to load duplicates.</div>';
          }
        })();
      }
    }
    const dedupeBtn = e.target.closest('#runDedupeBtn');
    if(dedupeBtn){
      rememberModal('managePanel', 'Actions', null);
      fetch('/dedupe', {method:'POST'}).then(()=>location.reload());
    }
  });

  function renderDupes(groups){
    const box = document.getElementById('dupesBox');
    if(!box) return;
    const data = groups || DUPES;
    if(!data || data.length === 0){
      box.innerHTML = '<div class="muted">No duplicates found on this page 🎉</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    data.forEach(g => {
      const wrap = document.createElement('div');
      wrap.style.padding = '.4rem .2rem';
      wrap.style.borderTop = '1px solid var(--border)';
      const title = document.createElement('div');
      title.style.fontWeight = '700';
      title.style.margin = '.2rem 0 .35rem';
      title.textContent = g.display + ':';
      wrap.appendChild(title);

      g.entries.forEach((e, idx) => {
        const row = document.createElement('div');
        row.style.display = 'flex';
        row.style.alignItems = 'center';
        row.style.gap = '.5rem';
        row.style.padding = '0 .2rem .25rem 1.2rem';
        row.innerHTML = `
          <span class="muted" style="min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
            ${e.widget}
          </span>
          <button class="btn ${idx===0?'ghost':'danger'} small" ${idx===0?'disabled':''} data-del-id="${e.id}" title="${idx===0?'Primary copy kept':'Delete this duplicate'}">
            <i class="fa-solid fa-trash"></i>
          </button>
        `;
        wrap.appendChild(row);
      });
      frag.appendChild(wrap);
    });
    box.innerHTML = '';
    box.appendChild(frag);

    box.onclick = async function(ev){
      const btn = ev.target.closest('[data-del-id]');
      if(!btn || btn.disabled) return;
      const id = btn.getAttribute('data-del-id');
      btn.disabled = true;

      try{
        await fetch(`/items/${id}/delete`, {method:'POST', credentials:'same-origin', headers:{'Accept':'application/json','X-Requested-With':'fetch'}});
        removeFromDupes(id);
        renderDupes();
      }catch(e){
        btn.disabled = false;
      }
    };
  }

  function removeFromDupes(itemId){
    for(let gi = 0; gi < DUPES.length; gi++){
      const g = DUPES[gi];
      const idx = g.entries.findIndex(e => e.id === itemId);
      if(idx !== -1){
        g.entries.splice(idx, 1);
        if(g.entries.length < 2){
          DUPES.splice(gi, 1);
        }
        return true;
      }
    }
    return false;
  }

  function buildManageListForWidget(widgetEl){
    const list = document.getElementById('manageList');
    const name = document.getElementById('manageWidgetName');
    const wid = widgetEl.dataset.wid;
    const wname = widgetEl.querySelector('.widget-title')?.childNodes[0]?.textContent?.trim() || widgetEl.querySelector('.widget-title')?.textContent || '';
    name.textContent = 'Widget: ' + wname;
    document.getElementById('managePanel').dataset.wid = wid;
    const actionsPanel = document.getElementById('actionsPanel');
    if(actionsPanel) actionsPanel.style.display = 'none';
    list.innerHTML = '';
    list.style.display = '';
    widgetEl.querySelectorAll('.items .item').forEach(it => {
      const iid = it.dataset.iid;
      const type = it.dataset.type;
      let title = '';
      if(type === 'bookmark'){ title = (it.querySelector('.label a')?.textContent || '').trim(); }
      else { title = (it.querySelector('.note')?.textContent || '').trim().slice(0,80); }
      const li = document.createElement('li');
      li.innerHTML = `
        <span class="pill">${type}</span>
        <span style="max-width:55%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${title || '(untitled)'}</span>
        <div class="manage-actions">
          <button class="btn ghost small" type="button" data-edit="${iid}" data-type="${type}"><i class="fas fa-edit"></i></button>
          <button class="btn danger small" type="button" data-del="${iid}"><i class="fa-solid fa-trash"></i></button>
        </div>`;
      list.appendChild(li);
    });

    list.onclick = async function(e){
      const eb = e.target.closest('[data-edit]');
      const db = e.target.closest('[data-del]');
      if(eb){
        const iid = eb.getAttribute('data-edit');
        const type = eb.getAttribute('data-type');
        if(type === 'bookmark'){
          const item = widgetEl.querySelector(`.item[data-iid="${iid}"]`);
          const a = item.querySelector('.label a');
          const nameVal = a?.textContent?.trim() || '';
          const urlVal = a?.getAttribute('href') || '';
          const ebf = document.getElementById('editBookmarkForm');
          ebf.action = `/items/${iid}/edit`;
          document.getElementById('eb_id').value = iid;
          document.getElementById('eb_name').value = nameVal;
          document.getElementById('eb_url').value = urlVal;
          const wsel = document.getElementById('eb_widget'); if (wsel) wsel.value = wid;
          showModalWith('editBookmarkForm', 'Edit Bookmark');
        } else {
          const item = widgetEl.querySelector(`.item[data-iid="${iid}"]`);
          const txt = item.querySelector('.note')?.textContent || '';
          const color = item.querySelector('.note')?.style.backgroundColor || '';
          const enf = document.getElementById('editNoteForm');
          enf.action = `/items/${iid}/edit`;
          document.getElementById('en_id').value = iid;
          document.getElementById('en_notes').value = txt;
          if(color){ document.getElementById('en_color').value = '#FEF3C7'; } // simple fallback
          const wsel = document.getElementById('en_widget'); if (wsel) wsel.value = wid;
          showModalWith('editNoteForm', 'Edit Note');
        }
      }
      if(db){
        const iid = db.getAttribute('data-del');
        rememberModal('managePanel', 'Manage Items', wid);
        await fetch(`/items/${iid}/delete`, {method:'POST', headers:{'Accept':'application/json','X-Requested-With':'fetch'}});
        // remove from DOM list immediately
        const row = widgetEl.querySelector(`.item[data-iid="${iid}"]`);
        if(row) row.remove();
        buildManageListForWidget(widgetEl);
      }
    };
  }

  // Widget menu
  document.addEventListener('click', function(e){
    const menuBtn = e.target.closest('.menu-btn');
    if(menuBtn){
      const header = menuBtn.closest('.widget-header');
      const menu = header.querySelector('.dropdown');
      document.querySelectorAll('.dropdown').forEach(d => { if(d !== menu) d.classList.remove('show'); });
      menu.classList.toggle('show');
      return;
    }
    if(!e.target.closest('.dropdown')) { document.querySelectorAll('.dropdown').forEach(d => d.classList.remove('show')); }

    const openModal = e.target.closest('[data-action="open-modal"]');
    if(openModal){
      e.preventDefault();
      const kind = openModal.dataset.kind;
      const wid = openModal.dataset.wid || '';
      if (kind === 'bookmark') {
        const bmSelect = document.getElementById('bm_widget_select');
        const bmHidden = document.getElementById('bm_widget_id');
        if (bmSelect && wid) bmSelect.value = wid;
        if (bmHidden) bmHidden.value = wid || '';
        showModalWith('bookmarkForm', 'Add Bookmark(s)');
      } else {
        const noteSelect = document.getElementById('note_widget_select');
        const noteHidden = document.getElementById('note_widget_id');
        if (noteSelect && wid) noteSelect.value = wid;
        if (noteHidden) noteHidden.value = wid || '';
        showModalWith('noteForm', 'Add Note');
      }
    }

    const manage = e.target.closest('[data-action="manage-items"]');
    if(manage){
      e.preventDefault();
      const widget = manage.closest('.widget');
      showModalWith('managePanel', 'Manage Items');
      buildManageListForWidget(widget);
      const menu = manage.closest('.dropdown'); if(menu) menu.classList.remove('show');
    }

    const rename = e.target.closest('[data-action="rename-widget"]');
    if(rename){
      e.preventDefault();
      const widget = rename.closest('.widget');
      const wid = widget.dataset.wid;
      const currentName = widget.querySelector('.widget-title')?.childNodes[0]?.textContent?.trim() || widget.querySelector('.widget-title')?.textContent || '';
      const sel = document.getElementById('rnw_select'); if(sel){ sel.value = wid; }
      const nm = document.getElementById('rnw_name'); if(nm){ nm.value = currentName; }
      showModalWith('renameWidgetForm', 'Rename Widget');
      const menu = rename.closest('.dropdown'); if(menu) menu.classList.remove('show');
    }

    const removeWidget = e.target.closest('[data-action="remove-widget"]');
    if(removeWidget){
      e.preventDefault();
      const widget = removeWidget.closest('.widget');
      const wid = widget.dataset.wid;
      const fd = new FormData(); fd.append('widget_id', wid);
      fetch('{{ url_for("delete_widget") }}', {method:'POST', body: fd})
        .then(()=>location.reload());
      const menu = removeWidget.closest('.dropdown'); if(menu) menu.classList.remove('show');
    }

    const moveWidget = e.target.closest('[data-action="move-widget"]');
    if(moveWidget){
      e.preventDefault();
      const widget = moveWidget.closest('.widget');
      const wid = widget.dataset.wid;
      const wsel = document.getElementById('mvw_id'); if(wsel) wsel.value = wid;
      const pg = document.getElementById('mvw_page'); if(pg) pg.value = '{{ current_page_id }}';
      showModalWith('moveWidgetForm', 'Move Widget');
      const menu = moveWidget.closest('.dropdown'); if(menu) menu.classList.remove('show');
    }

    const copyWidget = e.target.closest('[data-action="copy-widget"]');
    if(copyWidget){
      e.preventDefault();
      const widget = copyWidget.closest('.widget');
      const wid = widget.dataset.wid;
      const wsel = document.getElementById('cpw_id'); if(wsel) wsel.value = wid;
      const pg = document.getElementById('cpw_page'); if(pg) pg.value = '{{ current_page_id }}';
      showModalWith('copyWidgetForm', 'Copy Widget');
      const menu = copyWidget.closest('.dropdown'); if(menu) menu.classList.remove('show');
    }

    const openAll = e.target.closest('[data-action="open-all"]');
    if(openAll){
      e.preventDefault();
      const widget = openAll.closest('.widget');
      const links = Array.from(widget.querySelectorAll('.item[data-type="bookmark"] a'));
      for (let i = 0; i < links.length; i++) { window.open(links[i].href, '_blank', 'noopener,noreferrer'); }
      const menu = openAll.closest('.dropdown'); if(menu) menu.classList.remove('show');
    }

    const title = e.target.closest('.widget-title');
    if(title){
      const widget = title.closest('.widget');
      widget.classList.toggle('collapsed');
    }
  });

  // Modal submit (AJAX). For edit routes, we use JSON to avoid long redirects/timeouts under gunicorn.
  modalSubmit.addEventListener('click', async function(){
    const id = modalSubmit.dataset.target || '';
    const el = document.getElementById(id);
    if(!el) return;
    if(el.tagName !== 'FORM') return;

    const fd = new FormData(el);
    const action = el.getAttribute('action') || window.location.pathname;
    const method = (el.getAttribute('method') || 'POST').toUpperCase();

    // If it's one of the inline edit forms, do JSON and update the DOM without a full reload.
    if(id === 'editBookmarkForm' || id === 'editNoteForm'){
      try{
        const rsp = await fetch(action, {
          method,
          body: fd,
          credentials: 'same-origin',
          headers: {'Accept':'application/json','X-Requested-With':'fetch'}
        });
        const data = await rsp.json();

        if(data.ok){
          const wid = data.item.widget_id;
          const iid = data.item.id;
          // Update DOM minimally
          if(data.item.rowtype === 'bookmark'){
            // If widget changed, move element
            let elItem = document.querySelector(`.item[data-iid="${iid}"]`);
            if(!elItem){
              // if not found (e.g., moved widgets), just reload
              location.reload();
              return;
            }
            const currentWid = elItem.closest('.items')?.dataset.wid;
            if(currentWid !== wid){
              const targetList = document.querySelector(`.items[data-wid="${wid}"]`);
              if(targetList){ targetList.appendChild(elItem); }
            }
            // Update anchor
            const a = elItem.querySelector('.label a');
            if(a){
              a.textContent = data.item.name;
              a.href = data.item.url;
            }
          } else {
            // note
            let elItem = document.querySelector(`.item[data-iid="${iid}"]`);
            if(!elItem){ location.reload(); return; }
            const currentWid = elItem.closest('.items')?.dataset.wid;
            if(currentWid !== wid){
              const targetList = document.querySelector(`.items[data-wid="${wid}"]`);
              if(targetList){ targetList.appendChild(elItem); }
            }
            const note = elItem.querySelector('.note');
            if(note){
              note.textContent = data.item.notes || '';
              if(data.item.color){ note.style.background = data.item.color; }
            }
          }
          // Return to Manage Items list (keep modal open)
          const widget = document.querySelector(`.widget[data-wid="${getWidForSection('managePanel')||wid}"]`);
          if(widget){ buildManageListForWidget(widget); showModalWith('managePanel','Manage Items'); }
        } else {
          // fallback
          location.reload();
        }
      }catch(e){
        location.reload();
      }
      return;
    }

    // For all other forms, keep the "reopen" behavior via localStorage and reload.
    rememberModal(id, document.getElementById('modalTitle').textContent, getWidForSection(id));
    try{ await fetch(action, {method, body: fd, credentials:'same-origin'}); }catch(e){}
    location.reload();
  });

  const manageButton = document.getElementById('manageButton');
  if(manageButton){ manageButton.addEventListener('click', ()=> showModalWith('managePanel', 'Actions')); }

  function buildManageListForWidget(widgetEl){
    const list = document.getElementById('manageList');
    const name = document.getElementById('manageWidgetName');
    const wid = widgetEl.dataset.wid;
    const wname = widgetEl.querySelector('.widget-title')?.childNodes[0]?.textContent?.trim() || widgetEl.querySelector('.widget-title')?.textContent || '';
    name.textContent = 'Widget: ' + wname;
    document.getElementById('managePanel').dataset.wid = wid;
    const actionsPanel = document.getElementById('actionsPanel');
    if(actionsPanel) actionsPanel.style.display = 'none';
    list.innerHTML = '';
    list.style.display = '';
    widgetEl.querySelectorAll('.items .item').forEach(it => {
      const iid = it.dataset.iid;
      const type = it.dataset.type;
      let title = '';
      if(type === 'bookmark'){ title = (it.querySelector('.label a')?.textContent || '').trim(); }
      else { title = (it.querySelector('.note')?.textContent || '').trim().slice(0,80); }
      const li = document.createElement('li');
      li.innerHTML = `
        <span class="pill">${type}</span>
        <span style="max-width:55%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${title || '(untitled)'}</span>
        <div class="manage-actions">
          <button class="btn ghost small" type="button" data-edit="${iid}" data-type="${type}"><i class="fas fa-edit"></i></button>
          <button class="btn danger small" type="button" data-del="${iid}"><i class="fa-solid fa-trash"></i></button>
        </div>`;
      list.appendChild(li);
    });

    list.onclick = async function(e){
      const eb = e.target.closest('[data-edit]');
      const db = e.target.closest('[data-del]');
      if(eb){
        const iid = eb.getAttribute('data-edit');
        const type = eb.getAttribute('data-type');
        if(type === 'bookmark'){
          const item = widgetEl.querySelector(`.item[data-iid="${iid}"]`);
          const a = item.querySelector('.label a');
          const nameVal = a?.textContent?.trim() || '';
          const urlVal = a?.getAttribute('href') || '';
          const ebf = document.getElementById('editBookmarkForm');
          ebf.action = `/items/${iid}/edit`;
          document.getElementById('eb_id').value = iid;
          document.getElementById('eb_name').value = nameVal;
          document.getElementById('eb_url').value = urlVal;
          const wsel = document.getElementById('eb_widget'); if (wsel) wsel.value = wid;
          showModalWith('editBookmarkForm', 'Edit Bookmark');
        } else {
          const item = widgetEl.querySelector(`.item[data-iid="${iid}"]`);
          const txt = item.querySelector('.note')?.textContent || '';
          const color = item.querySelector('.note')?.style.backgroundColor || '';
          const enf = document.getElementById('editNoteForm');
          enf.action = `/items/${iid}/edit`;
          document.getElementById('en_id').value = iid;
          document.getElementById('en_notes').value = txt;
          if(color){ document.getElementById('en_color').value = '#FEF3C7'; }
          const wsel = document.getElementById('en_widget'); if (wsel) wsel.value = wid;
          showModalWith('editNoteForm', 'Edit Note');
        }
      }
      if(db){
        const iid = db.getAttribute('data-del');
        await fetch(`/items/${iid}/delete`, {method:'POST', headers:{'Accept':'application/json','X-Requested-With':'fetch'}});
        const row = widgetEl.querySelector(`.item[data-iid="${iid}"]`);
        if(row) row.remove();
        buildManageListForWidget(widgetEl);
      }
    };
  }

  // Drag & drop
  function sendReorder(){
    const widgets = [];
    document.querySelectorAll(".col").forEach((colDiv) => {
      const col = parseInt(colDiv.dataset.col);
      colDiv.querySelectorAll(".widget").forEach((w, idx) => {
        widgets.push({id: w.dataset.wid, column: col, order: idx});
      });
    });

    const items = [];
    document.querySelectorAll(".items").forEach((list) => {
      const wid = list.dataset.wid;
      list.querySelectorAll(".item").forEach((it, idx) => {
        items.push({id: it.dataset.iid, widget_id: wid, order: idx});
      });
    });

    fetch("{{ url_for('reorder') }}", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({widgets, items})
    });
  }

  document.querySelectorAll(".col").forEach((col) => {
    new Sortable(col, { group: "widgets", handle: ".drag-handle", animation: 150, draggable: ".widget", onEnd: sendReorder });
  });
  document.querySelectorAll(".items").forEach((list) => {
    new Sortable(list, { group: "items", handle: ".grip", animation: 150, draggable: ".item", onEnd: sendReorder });
  });
  </script>
</body>
</html>
"""

INDEX = """
<div class="grid" id="grid">
  {% for col in range(1,7) %}
  <div class="col" id="col-{{ col }}" data-col="{{ col }}">
    {% for w in widgets if w["column"] == col %}
    <div class="widget" data-wid="{{ w["id"] }}">
      <div class="widget-header">
        <h3 class="widget-title" title="Click to collapse/expand">
          {{ w["name"] }}
          <span class="pill" title="Bookmarks in this widget">{{ w["bookmark_count"] }}</span>
        </h3>
        <div class="widget-tools">
          <i class="fa-solid fa-grip-vertical drag-handle" title="Drag widget"></i>
          <button class="menu-btn" title="Widget menu"><i class="fa-solid fa-bars"></i></button>
          <div class="dropdown">
            <a href="#" data-action="open-modal" data-kind="bookmark" data-wid="{{ w['id'] }}"><i class="fa-solid fa-bookmark"></i> Add bookmark(s)</a>
            <a href="#" data-action="open-modal" data-kind="note" data-wid="{{ w['id'] }}"><i class="fa-regular fa-note-sticky"></i> Add note</a>
            <button data-action="manage-items"><i class="fas fa-edit"></i> Manage bookmarks/notes</button>
            <button data-action="rename-widget"><i class="fa-solid fa-pen-to-square"></i> Rename widget</button>
            <button data-action="move-widget"><i class="fa-solid fa-right-left"></i> Move to page…</button>
            <button data-action="copy-widget"><i class="fa-solid fa-copy"></i> Copy to page…</button>
            <button data-action="open-all"><i class="fa-solid fa-up-right-from-square"></i> Open all bookmarks</button>
            <button data-action="remove-widget"><i class="fa-solid fa-trash"></i> Remove widget</button>
          </div>
        </div>
      </div>

      <div class="items" data-wid="{{ w['id'] }}">
        {% for it in w["items"] %}
          {% if it["rowtype"] == "bookmark" %}
            <div class="item" data-iid="{{ it['id'] }}" data-type="bookmark">
              {% set fav = (it['url']|favicon) %}
              {% if fav %}
                <img class="favicon grip" src="{{ fav }}" alt="" referrerpolicy="no-referrer"
                     onerror="var n=this.nextElementSibling; if(n) n.style.display='inline-block'; this.remove();">
                <i class="fa-solid fa-grip-vertical grip" style="display:none" title="Drag"></i>
              {% else %}
                <i class="fa-solid fa-grip-vertical grip" title="Drag"></i>
              {% endif %}
              <span class="label"><a href="{{ it['url'] }}" target="_blank" rel="noopener noreferrer">{{ it['name'] }}</a></span>
            </div>
          {% elif it["rowtype"] == "note" %}
            <div class="item" data-iid="{{ it['id'] }}" data-type="note">
              <i class="fa-solid fa-grip-vertical grip" title="Drag"></i>
              <div class="note" style="background: {{ it['color'] if it['color'] else 'var(--hover)' }};">{{ it["notes"] }}</div>
            </div>
          {% endif %}
        {% endfor %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% endfor %}
</div>
"""

LOGIN = """
<h2>Admin Login</h2>
<form method="post">
  <p><input name="username" placeholder="Username" required></p>
  <p><input type="password" name="password" placeholder="Password" required></p>
  <p><button class="btn" type="submit"><i class="fa-solid fa-right-to-bracket"></i> Login</button></p>
</form>
<p class="muted">Default: admin / password — change via env vars.</p>
"""

def page(tpl, **ctx):
    rows = load_rows()
    pages = get_pages(rows)
    current_page_id = get_current_page_id(rows)
    ctx.setdefault("pages", pages)
    ctx.setdefault("current_page_id", current_page_id)
    ctx.setdefault("page_title", get_page_name(rows, current_page_id))
    ctx.setdefault("widgets_select", get_widgets_for_select(rows, current_page_id))
    return render_template_string(BASE, content=render_template_string(tpl, **ctx), **ctx)


# ----------------------------
# Routes
# ----------------------------
@app.route("/", methods=["GET"])
def index():
    rows = load_rows()
    pid = get_current_page_id(rows)
    widgets = get_widgets(rows, pid)
    total_bookmarks = sum(w.get("bookmark_count", 0) for w in widgets)
    return page(INDEX, widgets=widgets, total_bookmarks=total_bookmarks)

@app.route("/api/search")
def api_search():
    rows = load_rows()
    pid = get_current_page_id(rows)
    q = (request.args.get("q") or "").strip().lower()
    widget_map = {r["id"]: (r.get("name","") or "") for r in rows if r.get("rowtype")=="widget" and r.get("page_id")==pid}
    res = []
    for r in rows:
        if r.get("rowtype")!="bookmark": continue
        wid = r.get("widget_id")
        if wid not in widget_map: continue
        name = r.get("name","")
        url = r.get("url","")
        if q and (q not in name.lower()) and (q not in url.lower()):
            continue
        res.append({
            "id": r["id"], "name": name or url, "url": url,
            "widget_id": wid, "widget": widget_map[wid],
            "favicon": favicon_filter(url),
        })
    if q:
        def score(x):
            n = x["name"].lower()
            return (0 if q in n else 1, n)
        res.sort(key=score)
    else:
        res.sort(key=lambda x: x["name"].lower())
    return jsonify({"results": res[:200]})

# dupes API
@app.route("/api/dupes")
@login_required
def api_dupes():
    rows = load_rows()
    pid = get_current_page_id(rows)
    groups = list_duplicate_bookmarks(rows, pid)
    return jsonify({"groups": groups})

@app.route("/switch_page", methods=["POST"])
def switch_page():
    rows = load_rows()
    pid = request.form.get("page_id") or DEFAULT_PAGE_ID
    if any(r.get("rowtype")=="page" and r.get("id")==pid for r in rows):
        session["page_id"] = pid
    return redirect(url_for("index"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form.get("username")==ADMIN_USER and request.form.get("password")==ADMIN_PASS:
            session["logged_in"] = True
            flash("Logged in.", "success")
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Invalid credentials.", "danger")
    return page(LOGIN)

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))

# ---- Add: Pages & Widgets ----
@app.route("/pages/add", methods=["POST"])
@login_required
def add_page():
    rows = load_rows()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Page name required.", "danger")
        return redirect(url_for("index"))
    pid = new_id() if name != DEFAULT_PAGE_NAME else DEFAULT_PAGE_ID
    if pid == DEFAULT_PAGE_ID and any(r.get("rowtype")=="page" and r.get("id")==DEFAULT_PAGE_ID for r in rows):
        pid = new_id()
    rows.append(dict(rowtype="page", id=pid, page_id="", widget_id="", column="",
                     order=str(next_page_order(rows)), name=name, url="", notes="", color=""))
    save_rows(rows)
    session["page_id"] = pid
    flash("Page added.", "success")
    return redirect(url_for("index"))

@app.route("/pages/rename", methods=["POST"])
@login_required
def rename_page():
    rows = load_rows()
    pid = request.form.get("page_id","")
    name = (request.form.get("name") or "").strip()
    r = find_row(rows, pid)
    if r and r.get("rowtype")=="page" and name:
        r["name"] = name
        save_rows(rows)
    return redirect(url_for("index"))

@app.route("/widgets/add", methods=["POST"])
@login_required
def add_widget():
    rows = load_rows()
    name = (request.form.get("name") or "").strip()
    page_id = request.form.get("page_id") or get_current_page_id(rows)
    column = int(request.form.get("column", "1"))
    if not name:
        flash("Widget name required.", "danger")
        return redirect(url_for("index"))
    if not any(r.get("rowtype")=="page" and r.get("id")==page_id for r in rows):
        flash("Selected page not found.", "danger")
        return redirect(url_for("index"))
    wid = new_id()
    rows.append(dict(rowtype="widget", id=wid, page_id=page_id, widget_id="", column=str(column),
                     order=str(next_widget_order(rows, page_id, column)), name=name, url="", notes="", color=""))
    wr, br = run_dedupe(rows)
    save_rows(rows)
    if wr or br: flash(f"Dedupe: merged {wr} widget(s), removed {br} duplicate bookmark(s).", "info")
    else: flash("Widget added.", "success")
    session["page_id"] = page_id
    return redirect(url_for("index"))

# ---- Remove: Pages & Widgets ----
@app.route("/pages/delete", methods=["POST"])
@login_required
def delete_page():
    rows = load_rows()
    pid = request.form.get("page_id","")
    widget_ids = {r["id"] for r in rows if r.get("rowtype")=="widget" and r.get("page_id")==pid}
    rows2 = []
    for r in rows:
        if r.get("rowtype")=="page" and r.get("id")==pid: continue
        if r.get("rowtype")=="widget" and r.get("id") in widget_ids: continue
        if r.get("rowtype") in ("bookmark","note") and r.get("widget_id") in widget_ids: continue
        rows2.append(r)
    if not any(r.get("rowtype")=="page" for r in rows2):
        rows2.append(dict(rowtype="page", id=DEFAULT_PAGE_ID, page_id="", widget_id="", column="", order="0",
                          name=DEFAULT_PAGE_NAME, url="", notes="", color=""))
        session["page_id"] = DEFAULT_PAGE_ID
    else:
        if session.get("page_id")==pid:
            first = next((r["id"] for r in rows2 if r.get("rowtype")=="page"), DEFAULT_PAGE_ID)
            session["page_id"] = first
    save_rows(rows2)
    return redirect(url_for("index"))

@app.route("/widgets/delete", methods=["POST"])
@login_required
def delete_widget():
    rows = load_rows()
    wid = request.form.get("widget_id","")
    rows2 = []
    for r in rows:
        if r.get("rowtype")=="widget" and r.get("id")==wid: continue
        if r.get("rowtype") in ("bookmark","note") and r.get("widget_id")==wid: continue
        rows2.append(r)
    save_rows(rows2)
    # JSON for AJAX delete via widget menu
    if request.headers.get('Accept','').find('application/json') >= 0 or request.headers.get('X-Requested-With'):
        return jsonify({"ok": True})
    return redirect(url_for("index"))

# ---- Rename widget ----
@app.route("/widgets/rename", methods=["POST"])
@login_required
def rename_widget():
    rows = load_rows()
    wid = request.form.get("widget_id","")
    name = (request.form.get("name") or "").strip()
    r = find_row(rows, wid)
    if r and r.get("rowtype")=="widget" and name:
        r["name"] = name
        wr, br = run_dedupe(rows)
        save_rows(rows)
        if wr or br: flash(f"Dedupe: merged {wr} widget(s), removed {br} duplicate bookmark(s).", "info")
    return redirect(url_for("index"))

# ---- Move/Copy widget ----
@app.route("/widgets/move", methods=["POST"])
@login_required
def move_widget():
    rows = load_rows()
    wid = request.form.get("widget_id","")
    target_page = request.form.get("page_id","")
    target_col = int(request.form.get("column","1"))
    w = find_row(rows, wid)
    if not w or w.get("rowtype")!="widget": return redirect(url_for("index"))
    if not any(r.get("rowtype")=="page" and r.get("id")==target_page for r in rows): return redirect(url_for("index"))
    w["page_id"] = target_page
    w["column"] = str(max(1,min(6,target_col)))
    w["order"] = str(next_widget_order(rows, target_page, int(w["column"])))
    wr, br = run_dedupe(rows)
    save_rows(rows)
    flash(f"Widget moved. Dedupe merged {wr} widget(s), removed {br} duplicate bookmark(s).", "success")
    return redirect(url_for("index"))

@app.route("/widgets/copy", methods=["POST"])
@login_required
def copy_widget():
    rows = load_rows()
    wid = request.form.get("widget_id","")
    target_page = request.form.get("page_id","")
    target_col = int(request.form.get("column","1"))
    w = find_row(rows, wid)
    if not w or w.get("rowtype")!="widget": return redirect(url_for("index"))
    if not any(r.get("rowtype")=="page" and r.get("id")==target_page for r in rows): return redirect(url_for("index"))
    new_wid = new_id()
    rows.append(dict(rowtype="widget", id=new_wid, page_id=target_page, widget_id="", column=str(max(1,min(6,target_col))),
                     order=str(next_widget_order(rows, target_page, max(1,min(6,target_col)))), name=w.get("name",""), url="", notes="", color=""))
    for r in rows[:]:
        if r.get("rowtype") in ("bookmark","note") and r.get("widget_id")==wid:
            nr = r.copy(); nr["id"] = new_id(); nr["widget_id"] = new_wid; rows.append(nr)
    wr, br = run_dedupe(rows)
    save_rows(rows)
    flash(f"Widget copied. Dedupe merged {wr} widget(s), removed {br} duplicate bookmark(s).", "success")
    return redirect(url_for("index"))

# ---- Items: add/edit/delete ----
@app.route("/items/bookmark", methods=["POST"])
@login_required
def add_bookmark():
    rows = load_rows()
    wid = request.form.get("widget_id_select") or request.form.get("widget_id")
    if not wid or not any(r.get("rowtype")=="widget" and r.get("id")==wid for r in rows):
        flash("Widget required.", "danger"); return redirect(url_for("index"))

    single_url = (request.form.get("url") or "").strip()
    single_name = (request.form.get("name") or "").strip()
    created = 0

    def add_single(url, name_override="", auto=True):
        nonlocal created
        url = normalize_url(url)
        if not url: return
        title = name_override.strip() if name_override else (fetch_title(url) if auto else guess_title_from_url(url))
        rows.append(dict(rowtype="bookmark", id=new_id(), page_id="", widget_id=wid, column="",
                         order=str(next_item_order(rows, wid)), name=title or guess_title_from_url(url),
                         url=url, notes="", color=""))
        created += 1

    if single_url:
        add_single(single_url, single_name, auto=True)

    urls_bulk = (request.form.get("urls_bulk") or "").strip()
    auto_titles = bool(request.form.get("auto_titles"))
    if urls_bulk:
        parts = [p.strip() for p in re.split(r"[\n,]+", urls_bulk) if p.strip()]
        for u in parts:
            add_single(u, "", auto=auto_titles)

    if created:
        br = dedupe_bookmarks(rows)
        save_rows(rows)
        if br: flash(f"Added {created} bookmark(s). Removed {br} duplicate(s).", "success")
        else: flash(f"Added {created} bookmark(s).", "success")
    else:
        flash("No valid URLs provided.", "danger")
    return redirect(url_for("index"))

@app.route("/items/note", methods=["POST"])
@login_required
def add_note():
    rows = load_rows()
    wid = request.form.get("widget_id_select") or request.form.get("widget_id")
    if not wid or not any(r.get("rowtype")=="widget" and r.get("id")==wid for r in rows):
        flash("Widget required.", "danger"); return redirect(url_for("index"))
    text = (request.form.get("notes") or "").strip()
    color = (request.form.get("color") or "").strip()
    if not text:
        flash("Note text required.", "danger"); return redirect(url_for("index"))
    iid = new_id()
    rows.append(dict(rowtype="note", id=iid, page_id="", widget_id=wid, column="", order=str(next_item_order(rows, wid)), name="", url="", notes=text, color=color))
    save_rows(rows)
    flash("Note added.", "success")
    return redirect(url_for("index"))

@app.route("/items/<iid>/edit", methods=["POST"])
@login_required
def edit_item(iid):
    """
    JSON-aware edit to prevent long redirect chains under gunicorn:
    - If 'Accept: application/json' or 'X-Requested-With' is set, return JSON {ok:True,item:{...}}
    - Else, redirect back to index (legacy behavior)
    """
    rows = load_rows()
    r = find_row(rows, iid)
    if not r or r.get("rowtype") not in ("bookmark","note"):
        if request.headers.get('Accept','').find('application/json') >= 0 or request.headers.get('X-Requested-With'):
            return jsonify({"ok": False, "error": "not_found"}), 404
        return redirect(url_for("index"))

    if r["rowtype"]=="bookmark":
        r["name"] = request.form.get("name", r.get("name",""))
        r["url"]  = request.form.get("url", r.get("url",""))
        _ = dedupe_bookmarks(rows)
    else:
        r["notes"] = request.form.get("notes", r.get("notes",""))
        r["color"] = request.form.get("color", r.get("color",""))

    new_wid = request.form.get("widget_id", r.get("widget_id"))
    if new_wid and new_wid != r.get("widget_id"):
        r["widget_id"] = new_wid
        r["order"] = str(next_item_order(rows, new_wid))

    save_rows(rows)

    # JSON fast-path for AJAX
    if request.headers.get('Accept','').find('application/json') >= 0 or request.headers.get('X-Requested-With'):
        if r["rowtype"]=="bookmark":
            payload = {"id": r["id"], "rowtype":"bookmark", "name": r.get("name",""), "url": r.get("url",""), "widget_id": r.get("widget_id")}
        else:
            payload = {"id": r["id"], "rowtype":"note", "notes": r.get("notes",""), "color": r.get("color",""), "widget_id": r.get("widget_id")}
        return jsonify({"ok": True, "item": payload})

    return redirect(url_for("index"))

@app.route("/items/<iid>/delete", methods=["POST"])
@login_required
def delete_item(iid):
    rows = load_rows()
    rows2 = [r for r in rows if r.get("id") != iid]
    if len(rows2) != len(rows):
        save_rows(rows2)
    if request.headers.get('Accept','').find('application/json') >= 0 or request.headers.get('X-Requested-With'):
        return jsonify({"ok": True})
    return redirect(url_for("index"))

# ---- Import HTML route ----
@app.route("/import_html", methods=["POST"])
@login_required
def import_html():
    rows = load_rows()
    f = request.files.get("html")
    if not f or not f.filename:
        flash("Please select a bookmarks HTML file.", "danger"); return redirect(url_for("index"))
    data = f.read()
    page_id = request.form.get("page_id") or None
    new_page_name = (request.form.get("new_page_name") or "").strip() or None
    column_start = int(request.form.get("column_start") or "1")
    created_pages, created_widgets, created_bookmarks, pid_used, title_from_file = import_bookmarks_html(
        data, rows, page_id, new_page_name, column_start
    )
    wr, br = run_dedupe(rows)
    save_rows(rows)
    session["page_id"] = pid_used
    flash(f"Imported {created_widgets} widget(s), {created_bookmarks} bookmark(s). Dedupe merged {wr} widget(s), removed {br} duplicate bookmark(s).", "success")
    return redirect(url_for("index"))

# ---- Drag & drop reorder ----
@app.route("/reorder", methods=["POST"])
@login_required
def reorder():
    payload = request.get_json(force=True, silent=True) or {}
    rows = load_rows()

    for w in payload.get("widgets", []):
        row = find_row(rows, w.get("id",""))
        if row and row.get("rowtype")=="widget":
            row["column"] = str(int(w.get("column", 1)))
            row["order"]  = str(int(w.get("order", 0)))

    for it in payload.get("items", []):
        row = find_row(rows, it.get("id",""))
        if row and row.get("rowtype") in ("bookmark","note"):
            row["widget_id"] = it.get("widget_id", row.get("widget_id"))
            row["order"]     = str(int(it.get("order", 0)))

    save_rows(rows)
    return jsonify({"status":"ok"})

# ---- Manual dedupe trigger ----
@app.route("/dedupe", methods=["POST"])
@login_required
def dedupe_route():
    rows = load_rows()
    wr, br = run_dedupe(rows)
    save_rows(rows)
    flash(f"Dedupe complete: merged {wr} widget(s), removed {br} duplicate bookmark(s).", "success")
    return redirect(url_for("index"))


# ----------------------------
# CLI tool
# ----------------------------
def cli_main(argv):
    ensure_csv()
    import argparse
    parser = argparse.ArgumentParser(description="Manage Start Page CSV / import bookmarks / dedupe")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list-pages", help="List pages")

    lpw = sub.add_parser("list-widgets", help="List widgets for a page")
    lpw.add_argument("--page", required=True, help="Page ID")

    addw = sub.add_parser("add-widget", help="Add a widget")
    addw.add_argument("--page", required=True)
    addw.add_argument("--name", required=True)
    addw.add_argument("--column", type=int, default=1)

    renp = sub.add_parser("rename-page", help="Rename a page")
    renp.add_argument("--id", required=True)
    renp.add_argument("--name", required=True)

    renw = sub.add_parser("rename-widget", help="Rename a widget")
    renw.add_argument("--id", required=True)
    renw.add_argument("--name", required=True)

    delw = sub.add_parser("delete-widget", help="Delete a widget (and its items)")
    delw.add_argument("--id", required=True)

    addb = sub.add_parser("add-bookmark", help="Add bookmark to a widget")
    addb.add_argument("--widget", required=True)
    addb.add_argument("--url", required=True)
    addb.add_argument("--name", default="")

    addn = sub.add_parser("add-note", help="Add note to a widget")
    addn.add_argument("--widget", required=True)
    addn.add_argument("--text", required=True)
    addn.add_argument("--color", default="")

    deli = sub.add_parser("delete-item", help="Delete bookmark/note by ID")
    deli.add_argument("--id", required=True)

    imp = sub.add_parser("import", help="Import bookmarks.html")
    imp.add_argument("--file", required=True)
    pid = imp.add_argument_group("Page selection")
    pid.add_argument("--page", help="Existing page ID (if omitted, creates new)")
    pid.add_argument("--new-page", help="New page name (if creating)")
    imp.add_argument("--column-start", type=int, default=1)

    sub.add_parser("dedupe", help="Run widget + bookmark dedupe")

    args = parser.parse_args(argv)
    rows = load_rows()

    if args.cmd == "list-pages":
        for r in rows:
            if r.get("rowtype")=="page":
                print(f"{r['id']}\t{r.get('name','')}")
        return 0

    if args.cmd == "list-widgets":
        ps = args.page
        for r in rows:
            if r.get("rowtype")=="widget" and r.get("page_id")==ps:
                print(f"{r['id']}\tcol {r.get('column')}\t{r.get('name','')}")
        return 0

    if args.cmd == "add-widget":
        page_id = args.page
        if not any(r.get("rowtype")=="page" and r.get("id")==page_id for r in rows):
            print("Page not found", file=sys.stderr); return 1
        wid = new_id()
        rows.append(dict(rowtype="widget", id=wid, page_id=page_id, widget_id="", column=str(max(1,min(6,args.column))),
                         order=str(next_widget_order(rows, page_id, max(1,min(6,args.column)))), name=args.name, url="", notes="", color=""))
        wr, br = run_dedupe(rows)
        save_rows(rows)
        print(f"widget={wid} dedupe_widgets={wr} dedupe_bookmarks={br}")
        return 0

    if args.cmd == "rename-page":
        r = find_row(rows, args.id)
        if not r or r.get("rowtype")!="page":
            print("Page not found", file=sys.stderr); return 1
        r["name"] = args.name
        save_rows(rows)
        return 0

    if args.cmd == "rename-widget":
        r = find_row(rows, args.id)
        if not r or r.get("rowtype")!="widget":
            print("Widget not found", file=sys.stderr); return 1
        r["name"] = args.name
        wr, br = run_dedupe(rows)
        save_rows(rows)
        print(f"dedupe_widgets={wr} dedupe_bookmarks={br}")
        return 0

    if args.cmd == "delete-widget":
        wid = args.id
        rows2 = []
        for r in rows:
          if r.get("rowtype")=="widget" and r.get("id")==wid: continue
          if r.get("rowtype") in ("bookmark","note") and r.get("widget_id")==wid: continue
          rows2.append(r)
        save_rows(rows2)
        return 0

    if args.cmd == "add-bookmark":
        wid = args.widget
        if not any(r.get("rowtype")=="widget" and r.get("id")==wid for r in rows):
            print("Widget not found", file=sys.stderr); return 1
        url = normalize_url(args.url)
        name = (args.name or "").strip() or guess_title_from_url(url)
        rows.append(dict(rowtype="bookmark", id=new_id(), page_id="", widget_id=wid, column="",
                         order=str(next_item_order(rows, wid)), name=name, url=url, notes="", color=""))
        br = dedupe_bookmarks(rows)
        save_rows(rows)
        print(f"dedupe_bookmarks={br}")
        return 0

    if args.cmd == "add-note":
        wid = args.widget
        if not any(r.get("rowtype")=="widget" and r.get("id")==wid for r in rows):
            print("Widget not found", file=sys.stderr); return 1
        rows.append(dict(rowtype="note", id=new_id(), page_id="", widget_id=wid, column="",
                         order=str(next_item_order(rows, wid)), name="", url="", notes=args.text, color=args.color))
        save_rows(rows)
        return 0

    if args.cmd == "delete-item":
        iid = args.id
        rows2 = [r for r in rows if r.get("id") != iid]
        save_rows(rows2)
        return 0

    if args.cmd == "import":
        path = args.file
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            print(f"Failed to read file: {e}", file=sys.stderr); return 1
        created_pages, created_widgets, created_bookmarks, pid_used, title = import_bookmarks_html(
            data, rows, args.page or None, (args.new_page or None), args.column_start
        )
        wr, br = run_dedupe(rows)
        save_rows(rows)
        print(f"Imported page={pid_used} widgets={created_widgets} bookmarks={created_bookmarks} | dedupe_widgets={wr} dedupe_bookmarks={br}")
        return 0

    if args.cmd == "dedupe":
        wr, br = run_dedupe(rows)
        save_rows(rows)
        print(f"dedupe_widgets={wr} dedupe_bookmarks={br}")
        return 0

    parser.print_help()
    return 0


# ------------- Run -------------
if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli_main(sys.argv[1:]))

    ensure_csv()
    rows = load_rows()
    if sum(1 for r in rows if r.get("rowtype")=="widget") == 0:
        wid_news = new_id()
        wid_dev  = new_id()
        rows.extend([
            dict(rowtype="widget", id=wid_news, page_id=DEFAULT_PAGE_ID, widget_id="", column="1", order="0", name="News", url="", notes="", color=""),
            dict(rowtype="widget", id=wid_dev,  page_id=DEFAULT_PAGE_ID, widget_id="", column="2", order="0", name="Dev Tools", url="", notes="", color=""),
            dict(rowtype="bookmark", id=new_id(), page_id="", widget_id=wid_news, column="", order="0", name="Hacker News", url="https://news.ycombinator.com", notes="", color=""),
            dict(rowtype="bookmark", id=new_id(), page_id="", widget_id=wid_dev,  column="", order="0", name="GitHub",       url="https://github.com", notes="", color=""),
            dict(rowtype="note",     id=new_id(), page_id="", widget_id=wid_news, column="", order="1", name="", url="", notes="Sticky notes go here.", color="#FEF3C7")
        ])
        run_dedupe(rows)
        save_rows(rows)
    app.run(debug=True, host="127.0.0.1", port=5000)
