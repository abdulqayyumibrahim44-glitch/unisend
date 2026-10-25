#!/usr/bin/env python3
"""
unisend.py (v3) - Wi-Fi file sharing.

ONLY ONE DEVICE NEEDS THIS APP. The other person can use either:
  1) a normal web browser  -> http://<your-ip>:8080  (browse folders, upload, download)
  2) any FTP client        -> ftp://<your-ip>:2121   (built into most file managers)

Features
  * Desktop window: live sending/receiving speed, speed graph, a progress bar for
    every transfer, and a "Change folder" button to pick which folder you share
    (falls back to the terminal if there is no display, e.g. Termux; or --nogui)
  * Browser page: browse subfolders, tick several files/folders and download them
    as ONE zip, upload files (into whatever folder you are viewing), live speeds
  * FTP server powered by pyftpdlib

Setup:
    pip install pyftpdlib qrcode pillow     (qrcode + pillow are optional, for the QR code)

Run:
    python unisend.py
    python unisend.py --dir "C:\\Users\\you\\Downloads"
    python unisend.py --user me --password secret     (adds a login to both FTP and web)
    python unisend.py --nogui                         (terminal only)
    python unisend.py --no-ftp                        (browser page only, no FTP)

Both devices must be on the same Wi-Fi, or one device's hotspot.

Wording (from the point of view of the device running this script):
    Sending   = the other person is DOWNLOADING files from you
    Receiving = the other person is UPLOADING files to you
"""
import argparse
import base64
import collections
import json
import os
import socket
import sys
import threading
import time
import warnings
import zipfile
import http.client
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

try:   # FTP is optional: the browser page works without it
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import DTPHandler, FTPHandler
    from pyftpdlib.servers import FTPServer
    FTP_AVAILABLE = True
except ImportError:
    DTPHandler = FTPHandler = object
    FTP_AVAILABLE = False

warnings.filterwarnings("ignore", category=RuntimeWarning, module="pyftpdlib")

CHUNK = 1024 * 1024        # bigger chunks = fewer reads/writes = faster on fast networks
SOCK_BUF = 1024 * 1024     # bigger TCP buffers help on Wi-Fi
AUTH = None   # the FTP authorizer, so the shared folder can be changed while running


# ----------------------------------------------------------------- stats ---
class Stats:
    """Shared counters for FTP + web transfers."""
    lock = threading.Lock()
    sent = 0          # total bytes sent to others (their downloads)
    received = 0      # total bytes received from others (their uploads)
    clients = 0       # connected FTP clients
    down = 0.0        # current speed, bytes/s, sending
    up = 0.0          # current speed, bytes/s, receiving
    history = collections.deque(maxlen=120)   # (down, up) samples, 0.5 s apart
    transfers = {}    # key -> dict(name, dir, done, total, finished)
    _next = 0

    @classmethod
    def add_sent(cls, n):
        with cls.lock:
            cls.sent += n

    @classmethod
    def add_received(cls, n):
        with cls.lock:
            cls.received += n

    @classmethod
    def begin(cls, name, direction, total=0):
        with cls.lock:
            cls._next += 1
            key = cls._next
            cls.transfers[key] = dict(name=name, dir=direction, done=0,
                                      total=total, finished=None)
            return key

    @classmethod
    def progress(cls, key, n):
        with cls.lock:
            t = cls.transfers.get(key)
            if t:
                t["done"] += n

    @classmethod
    def end(cls, key):
        with cls.lock:
            t = cls.transfers.get(key)
            if t and t["finished"] is None:
                t["finished"] = time.time()
                if not t["total"]:
                    t["total"] = t["done"]

    @classmethod
    def snapshot(cls):
        now = time.time()
        with cls.lock:
            for k in [k for k, t in cls.transfers.items()
                      if t["finished"] and now - t["finished"] > 6]:
                del cls.transfers[k]
            return {k: dict(t) for k, t in cls.transfers.items()}


def sampler():
    last_s, last_r, last_t = Stats.sent, Stats.received, time.time()
    while True:
        time.sleep(0.5)
        now = time.time()
        dt = max(now - last_t, 1e-6)
        with Stats.lock:
            s, r = Stats.sent, Stats.received
        Stats.down = (s - last_s) / dt
        Stats.up = (r - last_r) / dt
        Stats.history.append((Stats.down, Stats.up))
        last_s, last_r, last_t = s, r, now


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} PB"


def speed(n):
    return human(n) + "/s"


# ------------------------------------------------------------------- FTP ---
class CountingDTP(DTPHandler):
    """Counts every byte on the FTP data connection and tracks file progress."""
    _tk = None
    ac_in_buffer_size = 256 * 1024
    ac_out_buffer_size = 256 * 1024

    def _start(self, direction):
        if self._tk is None:
            fo = getattr(self, "file_obj", None)
            if fo is None:       # directory listing etc.
                self._tk = False
                return
            path = getattr(fo, "name", "") or "file"
            total = 0
            if direction == "out":
                try:
                    total = os.path.getsize(path)
                except OSError:
                    pass
            self._tk = Stats.begin(os.path.basename(str(path)), direction, total)

    def send(self, data):
        n = super().send(data)
        if n:
            Stats.add_sent(n)
            self._start("out")
            if self._tk:
                Stats.progress(self._tk, n)
        return n

    def recv(self, buffer_size):
        data = super().recv(buffer_size)
        if data:
            Stats.add_received(len(data))
            self._start("in")
            if self._tk:
                Stats.progress(self._tk, len(data))
        return data

    def close(self):
        if self._tk:
            Stats.end(self._tk)
            self._tk = False
        super().close()


class UnisendHandler(FTPHandler):
    dtp_handler = CountingDTP
    banner = "Unisend ready."

    def on_connect(self):
        with Stats.lock:
            Stats.clients += 1

    def on_disconnect(self):
        with Stats.lock:
            Stats.clients = max(0, Stats.clients - 1)


def start_ftp(args):
    global AUTH
    authorizer = DummyAuthorizer()
    perms = "elradfmwMT"
    if args.user:
        authorizer.add_user(args.user, args.password, args.dir, perm=perms)
    else:
        authorizer.add_anonymous(args.dir, perm=perms)
    AUTH = authorizer
    UnisendHandler.authorizer = authorizer
    UnisendHandler.passive_ports = range(args.ftp_port + 1, args.ftp_port + 101)
    server = FTPServer(("0.0.0.0", args.ftp_port), UnisendHandler)
    server.max_cons = 256
    server.max_cons_per_ip = 30
    threading.Thread(target=lambda: server.serve_forever(handle_exit=False),
                     daemon=True).start()
    return server


def set_share_dir(args, path):
    """Change the shared folder while the app is running.
    The web page switches immediately; new FTP connections use the new folder."""
    path = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    os.makedirs(path, exist_ok=True)
    args.dir = path
    if AUTH is not None:
        user = args.user or "anonymous"
        try:
            AUTH.user_table[user]["home"] = path
        except KeyError:
            pass
    return path


# ------------------------------------------------------------------- web ---
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unisend</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--mut:#94a3b8;--acc:#38bdf8;--ok:#4ade80}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 16px}
h2{font-size:15px;margin:0 0 10px;color:var(--mut);font-weight:600}
.card{background:var(--card);border-radius:14px;padding:16px;margin-bottom:16px}
.speeds{display:flex;gap:12px}
.speeds div{flex:1;text-align:center}
.speeds b{display:block;font-size:22px;color:var(--acc)}
.speeds span{font-size:12px;color:var(--mut)}
.drop{border:2px dashed var(--mut);border-radius:12px;padding:28px 12px;text-align:center;cursor:pointer;margin-top:8px}
.drop.over{border-color:var(--acc);background:#0b2a3b}
.row{display:flex;align-items:center;padding:10px 0;border-bottom:1px solid #334155;gap:10px}
.row:last-child{border-bottom:0}
.row a{color:var(--acc);text-decoration:none;word-break:break-all;flex:1}
.row span{color:var(--mut);font-size:13px;white-space:nowrap}
.row input[type=checkbox]{width:18px;height:18px;flex:none}
.bar{height:6px;background:#334155;border-radius:4px;overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;width:0;background:var(--ok);transition:width .15s}
.mut{color:var(--mut);font-size:13px}
.crumbs{margin-bottom:10px;word-break:break-all}
.crumbs a{color:var(--acc);text-decoration:none}
.tools{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
button{background:#334155;color:var(--fg);border:0;border-radius:8px;padding:9px 12px;font-size:14px;cursor:pointer}
button.primary{background:var(--acc);color:#04202e;font-weight:600}
</style></head><body><div class="wrap">
<h1>Unisend <span id="dn" class="mut"></span></h1>
<div class="card speeds">
  <div><b id="down">0 B/s</b><span>Download speed (to you)</span></div>
  <div><b id="up">0 B/s</b><span>Upload speed (from you)</span></div>
</div>
<div class="card"><h2>Files on this device</h2>
  <div class="crumbs" id="crumbs"></div>
  <div class="tools">
    <button id="all">Select all</button>
    <button id="dl" class="primary">Download selected</button>
    <button id="dlz">Selected as one zip</button>
    <button id="dlf">This folder as zip</button>
  </div>
  <div id="files"></div>
</div>
<div class="card"><h2>Send files to this device</h2>
  <div class="mut" id="target"></div>
  <div class="drop" id="drop">Tap to choose files or drop them here</div>
  <input type="file" id="pick" multiple hidden>
  <div id="queue"></div>
</div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const enc=p=>p.split('/').map(encodeURIComponent).join('/');
function fmt(n){const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++}return n.toFixed(i?1:0)+' '+u[i]}
function fromHash(){try{return decodeURIComponent(location.hash.slice(1))}catch(e){return ''}}
let cur=fromHash(), lastJson='';

async function stats(){try{const r=await (await fetch('/api/stats')).json();
  $('#down').textContent=fmt(r.sending)+'/s';$('#up').textContent=fmt(r.receiving)+'/s';if(r.name)$('#dn').textContent='- '+r.name}catch(e){}}

async function list(force){
  let r,txt;
  try{txt=await (await fetch('/api/files?path='+encodeURIComponent(cur))).text();r=JSON.parse(txt)}catch(e){return}
  if(r.error){if(cur){go('')}return}
  cur=r.path;
  if(!force&&txt===lastJson)return;
  lastJson=txt;
  const sel=new Set([...document.querySelectorAll('#files input:checked')].map(i=>i.dataset.n));
  const parts=cur?cur.split('/'):[];let acc='';
  let h='<a href="#" data-go="">Home</a>';
  parts.forEach(p=>{acc=acc?acc+'/'+p:p;h+=' / <a href="#" data-go="'+esc(acc)+'">'+esc(p)+'</a>'});
  $('#crumbs').innerHTML=h;
  $('#target').textContent='Uploads go into: /'+cur;
  let rows='';
  if(cur){rows+='<div class="row"><a href="#" data-go="'+esc(parts.slice(0,-1).join('/'))+'">&#11014; Up one folder</a></div>'}
  r.folders.forEach(f=>{const p=cur?cur+'/'+f:f;
    rows+='<div class="row"><input type="checkbox" data-t="d" data-n="'+esc(f)+'"><a href="#" data-go="'+esc(p)+'">&#128193; '+esc(f)+'</a><span></span></div>'});
  r.files.forEach(f=>{
    rows+='<div class="row"><input type="checkbox" data-t="f" data-n="'+esc(f.name)+'"><a href="/files/'+enc(cur?cur+'/'+f.name:f.name)+'" download>'+esc(f.name)+'</a><span>'+fmt(f.size)+'</span></div>'});
  $('#files').innerHTML=rows||'<p class="mut">This folder is empty</p>';
  document.querySelectorAll('#files input').forEach(i=>{if(sel.has(i.dataset.n))i.checked=true});
}
function go(p){cur=p;history.pushState(null,'',p?'#'+encodeURIComponent(p):location.pathname);lastJson='';list(true)}
window.onpopstate=()=>{cur=fromHash();lastJson='';list(true)};
document.addEventListener('click',e=>{const a=e.target.closest('[data-go]');if(a){e.preventDefault();go(a.dataset.go)}});

function zipUrl(names){return '/zip?path='+encodeURIComponent(cur)+names.map(n=>'&n='+encodeURIComponent(n)).join('')}
function fileUrl(n){return '/files/'+enc(cur?cur+'/'+n:n)}
function dlFile(n){const a=document.createElement('a');a.href=fileUrl(n);a.download=n;document.body.appendChild(a);a.click();a.remove()}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
$('#dl').onclick=async()=>{
  const sel=[...document.querySelectorAll('#files input:checked')];
  if(!sel.length){alert('Tick the files or folders you want first');return}
  const files=sel.filter(i=>i.dataset.t==='f').map(i=>i.dataset.n);
  const dirs=sel.filter(i=>i.dataset.t==='d').map(i=>i.dataset.n);
  for(const n of files){dlFile(n);await wait(500)}   // files download one by one, normally
  if(dirs.length){location.href=zipUrl(dirs)}         // folders have to be zipped
};
$('#dlz').onclick=()=>{const n=[...document.querySelectorAll('#files input:checked')].map(i=>i.dataset.n);
  if(!n.length){alert('Tick the files or folders you want first');return}location.href=zipUrl(n)};
$('#dlf').onclick=()=>{location.href=zipUrl([])};
$('#all').onclick=()=>{const b=[...document.querySelectorAll('#files input')];const all=b.length&&b.every(i=>i.checked);b.forEach(i=>i.checked=!all)};

function send(file){
  const dir=cur;
  const row=document.createElement('div');row.className='row';
  row.innerHTML='<div style="flex:1"><div>'+esc(file.name)+'</div><div class="bar"><i></i></div></div><span>0%</span>';
  $('#queue').prepend(row);
  const bar=row.querySelector('i'),pct=row.querySelector('span');
  return new Promise(res=>{
    const x=new XMLHttpRequest();x.open('POST','/upload?path='+encodeURIComponent(dir));
    x.setRequestHeader('X-Filename',encodeURIComponent(file.name));
    x.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);bar.style.width=p+'%';pct.textContent=p+'%'}};
    x.onload=()=>{bar.style.width='100%';pct.textContent=x.status==200?'Done':'Failed';list();res()};
    x.onerror=()=>{pct.textContent='Error';res()};
    x.send(file)})}
async function sendAll(files){for(const f of files)await send(f)}
const drop=$('#drop'),pick=$('#pick');
drop.onclick=()=>pick.click();
pick.onchange=()=>{sendAll([...pick.files]);pick.value=''};
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};
drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');sendAll([...e.dataTransfer.files])};
stats();list(true);setInterval(stats,1000);setInterval(()=>list(false),4000);
</script></body></html>
"""


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path = os.path.join(folder, name)
    i = 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return path


def inside(root_real, full):
    r = os.path.normcase(root_real).rstrip(os.sep)
    f = os.path.normcase(full)
    return f == r or f.startswith(r + os.sep)


def safe_join(root, rel):
    """Resolve a browser-supplied relative path inside root. Returns None if it escapes."""
    root_real = os.path.realpath(root)
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    full = os.path.realpath(os.path.join(root_real, *parts))
    return full if inside(root_real, full) else None


class ZipStream:
    """Write-only, non-seekable file object: sends zip bytes straight to the client."""

    def __init__(self, wfile, key):
        self.wfile, self.key = wfile, key

    def write(self, data):
        self.wfile.write(data)
        Stats.add_sent(len(data))
        Stats.progress(self.key, len(data))
        return len(data)

    def flush(self):
        pass


def make_web_handler(args):
    expected = None
    if args.user:
        expected = "Basic " + base64.b64encode(
            f"{args.user}:{args.password}".encode()).decode()

    class WebHandler(BaseHTTPRequestHandler):
        server_version = "Unisend"

        def log_message(self, *a):
            pass

        def setup(self):
            super().setup()
            try:
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        def _auth(self):
            if expected and self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Unisend"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            path = u.path
            if path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/stats":
                self._json(dict(sending=Stats.down, receiving=Stats.up,
                                sent=Stats.sent, received=Stats.received,
                                name=args.name))
            elif path == "/api/files":
                self._list(qs.get("path", [""])[0])
            elif path.startswith("/files/"):
                full = safe_join(args.dir, unquote(path[7:]))
                if full and os.path.isfile(full):
                    self._download(full)
                else:
                    self.send_error(404)
            elif path == "/zip":
                self._zip(qs.get("path", [""])[0], qs.get("n", []))
            else:
                self.send_error(404)

        def _list(self, rel):
            full = safe_join(args.dir, rel)
            if not full or not os.path.isdir(full):
                self._json(dict(error="not found"), 404)
                return
            folders, files = [], []
            try:
                for e in os.scandir(full):
                    if e.name.endswith(".part"):
                        continue
                    try:
                        if e.is_dir():
                            folders.append(e.name)
                        elif e.is_file():
                            st = e.stat()
                            files.append((st.st_mtime, dict(name=e.name, size=st.st_size)))
                    except OSError:
                        pass
            except OSError:
                pass
            folders.sort(key=str.lower)
            files.sort(key=lambda x: -x[0])
            rel_norm = os.path.relpath(full, os.path.realpath(args.dir)).replace("\\", "/")
            if rel_norm == ".":
                rel_norm = ""
            self._json(dict(path=rel_norm, folders=folders, files=[f for _, f in files]))

        def _download(self, full):
            name = os.path.basename(full)
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(name))
            self.end_headers()
            key = Stats.begin(name, "out", size)
            try:
                with open(full, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        Stats.add_sent(len(chunk))
                        Stats.progress(key, len(chunk))
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                Stats.end(key)

        def _zip(self, rel, names):
            base = safe_join(args.dir, rel)
            if not base or not os.path.isdir(base):
                self.send_error(404)
                return
            root_real = os.path.realpath(args.dir)
            if names:
                items = []
                for n in names:
                    n = os.path.basename(n.replace("\\", "/"))
                    p = os.path.realpath(os.path.join(base, n)) if n else None
                    if p and inside(root_real, p) and os.path.exists(p):
                        items.append((p, n))
                zip_name = "selected"
            else:
                zip_name = os.path.basename(base.rstrip("/\\")) or "shared"
                items = [(base, zip_name)]

            entries = []   # (full path, name inside zip)
            for p, arc in items:
                if os.path.isfile(p):
                    entries.append((p, arc))
                elif os.path.isdir(p):
                    for dp, dn, fn in os.walk(p):
                        for f in fn:
                            fp = os.path.join(dp, f)
                            if f.endswith(".part") or not os.path.isfile(fp):
                                continue
                            if not inside(root_real, os.path.realpath(fp)):
                                continue
                            sub = os.path.relpath(fp, p).replace("\\", "/")
                            entries.append((fp, arc + "/" + sub))
            total = 0
            for fp, _ in entries:
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(zip_name + ".zip"))
            self.end_headers()   # no Content-Length: the zip is streamed
            key = Stats.begin(zip_name + ".zip", "out", total)
            try:
                stream = ZipStream(self.wfile, key)
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED,
                                     allowZip64=True, strict_timestamps=False) as zf:
                    for fp, arc in entries:
                        try:
                            zf.write(fp, arc)
                        except FileNotFoundError:
                            pass
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                Stats.end(key)

        def do_POST(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            if u.path != "/upload":
                self.send_error(404)
                return
            rel = parse_qs(u.query).get("path", [""])[0]
            dest_dir = safe_join(args.dir, rel)
            if not dest_dir or not os.path.isdir(dest_dir):
                self._json(dict(ok=False, error="folder not found"), 404)
                return
            name = os.path.basename(unquote(self.headers.get("X-Filename", ""))
                                    .replace("\\", "/")).strip()
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(dict(ok=False, error="Content-Length required"), 411)
                return
            if not name or name in (".", ".."):
                self._json(dict(ok=False, error="bad filename"), 400)
                return
            dest = unique_path(dest_dir, name)
            tmp = dest + ".part"
            sender = unquote(self.headers.get("X-Sender", "")).strip()[:40]
            label = os.path.basename(dest) + (f"  (from {sender})" if sender else "")
            key = Stats.begin(label, "in", length)
            remaining = length
            ok = False
            try:
                with open(tmp, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                        Stats.add_received(len(chunk))
                        Stats.progress(key, len(chunk))
                ok = remaining == 0
            except OSError:
                ok = False
            finally:
                Stats.end(key)
            if ok:
                os.replace(tmp, dest)
                self._json(dict(ok=True, name=os.path.basename(dest)))
            else:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                try:
                    self._json(dict(ok=False, error="upload interrupted"), 400)
                except OSError:
                    pass

    return WebHandler


def start_web(args):
    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), make_web_handler(args))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# --------------------------------------------------------------- helpers ---
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def print_qr(text):
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


# ------------------------------------------------------- nearby devices ---
DISCOVERY_PORT = 45454
DEVICE_ID = uuid.uuid4().hex[:12]


class Peers:
    """Other Unisend apps seen on the same Wi-Fi."""
    lock = threading.Lock()
    items = {}   # id -> dict(name, ip, port, auth, seen)

    @classmethod
    def update(cls, pid, name, ip, port, auth):
        with cls.lock:
            cls.items[pid] = dict(name=name, ip=ip, port=port, auth=auth, seen=time.time())

    @classmethod
    def alive(cls):
        now = time.time()
        with cls.lock:
            for k in [k for k, p in cls.items.items() if now - p["seen"] > 10]:
                del cls.items[k]
            return {k: dict(p) for k, p in cls.items.items()}


def start_discovery(args):
    """Tell the Wi-Fi we are here (UDP broadcast) and listen for other Unisend apps."""
    msg = json.dumps(dict(app="unisend", id=DEVICE_ID, name=args.name,
                          port=args.http_port, auth=bool(args.user))).encode()

    def announcer():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        while True:
            targets = {args.discovery_addr}
            parts = get_local_ip().split(".")
            if len(parts) == 4 and parts[0] != "127":
                targets.add(".".join(parts[:3] + ["255"]))
            for t in targets:
                try:
                    sock.sendto(msg, (t, DISCOVERY_PORT))
                except OSError:
                    pass
            time.sleep(3)

    def listener():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError:
            return
        while True:
            try:
                data, addr = sock.recvfrom(2048)
                m = json.loads(data.decode("utf-8", "replace"))
                port = int(m["port"])
                if (m.get("app") != "unisend" or m.get("id") == DEVICE_ID
                        or not 0 < port < 65536):
                    continue
                Peers.update(str(m["id"])[:24], str(m.get("name", "Unknown"))[:40],
                             addr[0], port, bool(m.get("auth")))
            except (OSError, ValueError, KeyError, TypeError):
                continue

    threading.Thread(target=announcer, daemon=True).start()
    threading.Thread(target=listener, daemon=True).start()


def send_to_peer(peer, path, sender, user=None, password=None):
    """Upload one file into another Unisend's shared folder. Returns (ok, message)."""
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, str(e)
    auth = None
    if user is not None:
        auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    conn = None
    key = None
    try:
        if auth:   # check the login first, so a wrong password gives a clear message
            c = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=10)
            c.request("GET", "/api/stats", headers={"Authorization": auth})
            r = c.getresponse()
            r.read()
            c.close()
            if r.status == 401:
                return False, "wrong username or password"
        conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=30)
        conn.putrequest("POST", "/upload?path=")
        conn.putheader("X-Filename", quote(name))
        conn.putheader("X-Sender", quote(sender))
        conn.putheader("Content-Length", str(size))
        if auth:
            conn.putheader("Authorization", auth)
        conn.endheaders()
        key = Stats.begin(name, "out", size)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                conn.send(chunk)
                Stats.add_sent(len(chunk))
                Stats.progress(key, len(chunk))
        resp = conn.getresponse()
        resp.read()
        if resp.status == 200:
            return True, ""
        if resp.status == 401:
            return False, "they need a login"
        return False, f"they refused it (error {resp.status})"
    except (OSError, http.client.HTTPException) as e:
        return False, f"could not reach {peer['name']} ({e})"
    finally:
        if key is not None:
            Stats.end(key)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def resource_path(name):
    """Find a bundled file, both when run as a script and inside a PyInstaller EXE."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
    except Exception:
        pass


# ------------------------------------------------------------------- GUI ---
def run_gui(args, ip):
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    web_url = f"http://{ip}:{args.http_port}"
    ftp_url = f"ftp://{ip}:{args.ftp_port}"

    # Crisp text on Windows 10/11 with display scaling (125%, 150%...)
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title("Unisend")
    h = min(900, max(600, root.winfo_screenheight() - 100))   # fits small laptop screens
    root.geometry(f"620x{h}")
    root.minsize(540, 600)

    # Optional window/taskbar picture: put icon.png next to this file
    icon_path = resource_path("icon.png")
    if os.path.exists(icon_path):
        try:
            try:   # smooth, multi-size icon (needs pillow)
                from PIL import Image, ImageTk
                lanczos = getattr(Image, "Resampling", Image).LANCZOS
                src = Image.open(icon_path).convert("RGBA")
                icons = [ImageTk.PhotoImage(src.resize((s, s), lanczos))
                         for s in (256, 64, 48, 32, 16)]
                root.iconphoto(True, *icons)
                root._icons = icons
            except ImportError:
                _icon = tk.PhotoImage(file=icon_path)
                root.iconphoto(True, _icon)
                root._icon = _icon
        except Exception:
            pass
    ico_path = resource_path("icon.ico")
    if sys.platform.startswith("win") and os.path.exists(ico_path):
        try:
            root.iconbitmap(ico_path)   # best quality in the Windows taskbar
        except Exception:
            pass
    BG, CARD, FG, MUT = "#0f172a", "#1e293b", "#e2e8f0", "#94a3b8"
    DOWN_C, UP_C = "#38bdf8", "#4ade80"
    root.configure(bg=BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TProgressbar", troughcolor="#334155", background=UP_C,
                    bordercolor=CARD, lightcolor=UP_C, darkcolor=UP_C)
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=FG, rowheight=26, borderwidth=0)
    style.configure("Treeview.Heading", background="#334155", foreground=FG)
    style.map("Treeview", background=[("selected", "#334155")])

    def card(parent):
        f = tk.Frame(parent, bg=CARD, padx=12, pady=10)
        f.pack(fill="x", padx=12, pady=(10, 0))
        return f

    # --- shared folder
    fcard = card(root)
    tk.Label(fcard, text="Folder you are sharing:", bg=CARD, fg=MUT,
             anchor="w").pack(fill="x")
    frow = tk.Frame(fcard, bg=CARD)
    frow.pack(fill="x", pady=(4, 0))
    folder_var = tk.StringVar(value=args.dir)
    fe = tk.Entry(frow, textvariable=folder_var, relief="flat", bg="#0f172a", fg=FG,
                  readonlybackground="#0f172a", state="readonly")
    fe.pack(side="left", fill="x", expand=True, ipady=3)

    def change_folder():
        p = filedialog.askdirectory(initialdir=args.dir, parent=root,
                                    title="Choose the folder to share")
        if p:
            folder_var.set(set_share_dir(args, p))

    tk.Button(frow, text="Change folder...", command=change_folder).pack(side="left", padx=(8, 0))
    tk.Button(frow, text="Open", command=lambda: open_folder(args.dir)).pack(side="left", padx=(6, 0))
    tk.Label(fcard, text="The other person sees this folder and everything inside it. "
                         "Already-connected FTP apps must reconnect to see a new folder.",
             bg=CARD, fg=MUT, anchor="w", justify="left", wraplength=560,
             font=("Helvetica", 8)).pack(fill="x", pady=(4, 0))

    # --- connection info
    top = card(root)
    left = tk.Frame(top, bg=CARD)
    left.pack(side="left", fill="x", expand=True)
    tk.Label(left, text="Tell the other person to open:", bg=CARD, fg=MUT,
             anchor="w").pack(fill="x")

    state = {"ip": ip, "latest": ip}
    web_var = tk.StringVar(value=web_url)
    ftp_var = tk.StringVar(value=ftp_url)

    def addr_row(label, var):
        r = tk.Frame(left, bg=CARD)
        r.pack(fill="x", pady=2)
        tk.Label(r, text=label, width=9, anchor="w", bg=CARD, fg=MUT).pack(side="left")
        e = tk.Entry(r, textvariable=var, relief="flat", bg="#0f172a", fg=FG,
                     insertbackground=FG, readonlybackground="#0f172a", state="readonly")
        e.pack(side="left", fill="x", expand=True)

    addr_row("Browser", web_var)
    if args.use_ftp:
        addr_row("FTP app", ftp_var)
    warn = tk.Label(left, text="", bg=CARD, fg="#fbbf24", anchor="w", justify="left",
                    wraplength=330)
    warn.pack(fill="x")
    if args.user:
        tk.Label(left, text=f"Login: {args.user} / {args.password}", bg=CARD,
                 fg=MUT, anchor="w").pack(fill="x", pady=(4, 0))
    else:
        tk.Label(left, text="No password (anyone on your Wi-Fi can connect)",
                 bg=CARD, fg=MUT, anchor="w").pack(fill="x", pady=(4, 0))
    btns = tk.Frame(left, bg=CARD)
    btns.pack(fill="x", pady=(8, 0))

    def copy_link():
        root.clipboard_clear()
        root.clipboard_append(web_var.get())
    tk.Button(btns, text="Copy web link", command=copy_link).pack(side="left")

    # QR code (optional)
    qr_ok = False
    qr_state = {}
    try:
        import qrcode
        from PIL import Image, ImageTk
        nearest = getattr(Image, "Resampling", Image).NEAREST

        def make_qr_img(url):
            qr = qrcode.QRCode(border=2, box_size=10)
            qr.add_data(url)
            qr.make(fit=True)
            im = qr.make_image(fill_color="black", back_color="white")
            return getattr(im, "get_image", lambda: im)().convert("RGB")

        def qr_photo(size):
            return ImageTk.PhotoImage(qr_state["img"].resize((size, size), nearest))

        qr_state["img"] = make_qr_img(web_var.get())
        small = qr_photo(150)
        lbl = tk.Label(top, image=small, bg=CARD, cursor="hand2")
        lbl.image = small
        lbl.pack(side="right", padx=(10, 0))

        def refresh_qr():
            photo = qr_photo(150)
            lbl.configure(image=photo)
            lbl.image = photo

        def show_big(_event=None):
            win = tk.Toplevel(root)
            win.title("Scan to open Unisend")
            win.configure(bg="white")
            big = qr_photo(420)
            tk.Label(win, image=big, bg="white").pack(padx=10, pady=(10, 4))
            tk.Label(win, text=web_var.get(), bg="white", fg="black").pack(pady=(0, 10))
            win._big = big

        lbl.bind("<Button-1>", show_big)
        tk.Label(top, text="Click to enlarge", bg=CARD, fg=MUT,
                 font=("Helvetica", 8)).pack(side="bottom", anchor="e")
        qr_ok = True
    except Exception:
        tk.Label(top, text="QR code not available.\nInstall it with:\npip install qrcode pillow\nthen rebuild.",
                 bg=CARD, fg=MUT, justify="left",
                 font=("Helvetica", 8)).pack(side="right", padx=(10, 0))

    # --- notice when this PC's address changes (Wi-Fi gave it a new one)
    def ip_watcher():
        while True:
            time.sleep(3)
            try:
                state["latest"] = get_local_ip()
            except Exception:
                pass

    threading.Thread(target=ip_watcher, daemon=True).start()

    def check_ip():
        new_ip = state["latest"]
        if new_ip != state["ip"] and not new_ip.startswith("127."):
            state["ip"] = new_ip
            web_var.set(f"http://{new_ip}:{args.http_port}")
            ftp_var.set(f"ftp://{new_ip}:{args.ftp_port}")
            warn.config(text=f"Your address changed to {new_ip}. Rescan the QR code "
                             f"or use the new link on the other device.")
            if qr_ok:
                try:
                    qr_state["img"] = make_qr_img(web_var.get())
                    refresh_qr()
                except Exception:
                    pass
        root.after(1000, check_ip)

    root.after(1000, check_ip)

    # --- nearby devices (other Unisend apps on the same Wi-Fi)
    nb = card(root)
    nb_head = tk.Frame(nb, bg=CARD)
    nb_head.pack(fill="x")
    tk.Label(nb_head, text="Nearby devices running Unisend", bg=CARD, fg=MUT,
             anchor="w").pack(side="left")
    tk.Label(nb_head, text=f"You are: {args.name}", bg=CARD, fg=MUT,
             anchor="e").pack(side="right")
    plist = tk.Listbox(nb, height=3, bg="#0f172a", fg=FG, selectbackground="#334155",
                       selectforeground=FG, relief="flat", highlightthickness=0,
                       exportselection=False, activestyle="none")
    plist.pack(fill="x", pady=(6, 6))
    send_msg = {"t": ""}
    peer_ids, shown = [], []

    def refresh_peers():
        peers = Peers.alive()
        ids = sorted(peers, key=lambda k: peers[k]["name"].lower())
        labels = [f'{peers[k]["name"]}   ({peers[k]["ip"]})'
                  + ("   [needs password]" if peers[k]["auth"] else "") for k in ids]
        if ids != peer_ids or labels != shown:
            cur = plist.curselection()
            keep = peer_ids[cur[0]] if cur and cur[0] < len(peer_ids) else None
            plist.delete(0, "end")
            for lab in labels:
                plist.insert("end", lab)
            peer_ids[:] = ids
            shown[:] = labels
            if keep in ids:
                plist.selection_set(ids.index(keep))
        placeholder = "" if ids else ("Looking for devices... both must be on the same "
                                      "Wi-Fi with Unisend open.")
        send_status.config(text=send_msg["t"] or placeholder)

    def chosen_peer():
        cur = plist.curselection()
        if not cur or cur[0] >= len(peer_ids):
            messagebox.showinfo("Unisend", "Click a device in the list first.", parent=root)
            return None
        peer = Peers.alive().get(peer_ids[cur[0]])
        if not peer:
            messagebox.showinfo("Unisend", "That device just went away.", parent=root)
        return peer

    def send_files():
        peer = chosen_peer()
        if not peer:
            return
        paths = filedialog.askopenfilenames(parent=root,
                                            title=f"Choose files to send to {peer['name']}")
        if not paths:
            return
        user = password = None
        if peer["auth"]:
            user = simpledialog.askstring("Unisend", f"{peer['name']} needs a login.\n\nUsername:",
                                          parent=root)
            if user is None:
                return
            password = simpledialog.askstring("Unisend", "Password:", show="*", parent=root)
            if password is None:
                return

        def worker():
            ok_n, err = 0, ""
            for i, pth in enumerate(paths, 1):
                send_msg["t"] = f"Sending {os.path.basename(pth)} to {peer['name']} " \
                                f"({i} of {len(paths)})..."
                ok, why = send_to_peer(peer, pth, args.name, user, password)
                if ok:
                    ok_n += 1
                else:
                    err = why
                    break
            if err:
                send_msg["t"] = f"Stopped: {err}. Sent {ok_n} of {len(paths)}."
            else:
                send_msg["t"] = f"Sent {ok_n} file(s) to {peer['name']}."

        threading.Thread(target=worker, daemon=True).start()

    def open_their_page():
        peer = chosen_peer()
        if peer:
            webbrowser.open(f"http://{peer['ip']}:{peer['port']}")

    nb_btns = tk.Frame(nb, bg=CARD)
    nb_btns.pack(fill="x")
    tk.Button(nb_btns, text="Send files to selected...", command=send_files).pack(side="left")
    tk.Button(nb_btns, text="Browse their files", command=open_their_page).pack(side="left", padx=6)
    send_status = tk.Label(nb, text="", bg=CARD, fg=MUT, anchor="w", justify="left",
                           wraplength=540)
    send_status.pack(fill="x", pady=(6, 0))

    # --- speeds
    sp = card(root)
    sp.columnconfigure((0, 1), weight=1)
    down_v = tk.Label(sp, text="0 B/s", font=("Helvetica", 22, "bold"), bg=CARD, fg=DOWN_C)
    up_v = tk.Label(sp, text="0 B/s", font=("Helvetica", 22, "bold"), bg=CARD, fg=UP_C)
    down_v.grid(row=0, column=0)
    up_v.grid(row=0, column=1)
    tk.Label(sp, text="Sending (they download)", bg=CARD, fg=MUT).grid(row=1, column=0)
    tk.Label(sp, text="Receiving (they upload)", bg=CARD, fg=MUT).grid(row=1, column=1)
    tot = tk.Label(sp, text="", bg=CARD, fg=MUT)
    tot.grid(row=2, column=0, columnspan=2, pady=(6, 0))

    # --- graph
    gr = card(root)
    canvas = tk.Canvas(gr, height=100, bg="#0f172a", highlightthickness=0)
    canvas.pack(fill="x")
    peak = tk.Label(gr, text="", bg=CARD, fg=MUT, anchor="e")
    peak.pack(fill="x")

    # --- transfers
    tr = card(root)
    tr.pack(fill="both", expand=True, pady=(10, 12))
    tk.Label(tr, text="Transfers", bg=CARD, fg=MUT, anchor="w").pack(fill="x")
    cols = ("file", "dir", "progress", "size")
    tree = ttk.Treeview(tr, columns=cols, show="headings", height=5)
    for c, w, t in (("file", 230, "File"), ("dir", 70, "Direction"),
                    ("progress", 80, "Progress"), ("size", 90, "Size")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w" if c == "file" else "center")
    tree.pack(fill="both", expand=True, pady=(4, 6))
    cur_lbl = tk.Label(tr, text="No active transfer", bg=CARD, fg=MUT, anchor="w")
    cur_lbl.pack(fill="x")
    cur_bar = ttk.Progressbar(tr, maximum=100)
    cur_bar.pack(fill="x", pady=(2, 0))

    rows = {}

    def draw_graph():
        canvas.delete("all")
        w = max(canvas.winfo_width(), 200)
        h = 100
        data = list(Stats.history)
        top_v = max([1024.0] + [max(d, u) for d, u in data])
        for frac in (0.25, 0.5, 0.75):
            canvas.create_line(0, h * frac, w, h * frac, fill="#1e293b")
        for idx, color in ((0, DOWN_C), (1, UP_C)):
            if len(data) < 2:
                continue
            step = w / (Stats.history.maxlen - 1)
            x0 = w - step * (len(data) - 1)
            pts = []
            for i, s in enumerate(data):
                pts += [x0 + i * step, h - 4 - (h - 10) * (s[idx] / top_v)]
            canvas.create_line(*pts, fill=color, width=2)
        canvas.create_text(6, 6, anchor="nw", fill=MUT,
                           text="blue = sending   green = receiving")
        peak.config(text=f"scale: {speed(top_v)}")

    def tick():
        down_v.config(text=speed(Stats.down))
        up_v.config(text=speed(Stats.up))
        tot.config(text=f"Total sent {human(Stats.sent)}   |   total received "
                        f"{human(Stats.received)}"
                        + (f"   |   FTP clients: {Stats.clients}" if args.use_ftp else ""))
        draw_graph()
        refresh_peers()

        snap = Stats.snapshot()
        for k in [k for k in rows if k not in snap]:
            tree.delete(rows.pop(k))
        active = None
        for k, t in snap.items():
            total = t["total"]
            pct = f"{min(100, int(t['done'] * 100 / total))}%" if total else "..."
            if t["finished"]:
                pct = "Done"
            vals = (t["name"], "Sending" if t["dir"] == "out" else "Receiving",
                    pct, human(total or t["done"]))
            if k in rows:
                tree.item(rows[k], values=vals)
            else:
                rows[k] = tree.insert("", 0, values=vals)
            if not t["finished"]:
                active = t
        if active:
            if active["total"]:
                cur_bar.config(value=min(100, active["done"] * 100 / active["total"]))
            else:
                cur_bar.config(value=0)
            cur_lbl.config(text=f"{'Sending' if active['dir'] == 'out' else 'Receiving'}: "
                                f"{active['name']}  ({human(active['done'])})")
        else:
            cur_bar.config(value=0)
            cur_lbl.config(text="No active transfer")
        root.after(500, tick)

    tick()
    root.mainloop()


# -------------------------------------------------------------- terminal ---
def run_terminal(args, ip):
    web_url = f"http://{ip}:{args.http_port}"
    ftp_url = f"ftp://{ip}:{args.ftp_port}"
    print("=" * 60)
    print("  UNISEND is running")
    print("=" * 60)
    print(f"  Sharing folder : {args.dir}")
    print(f"  Browser        : {web_url}   (no app needed)")
    if args.use_ftp:
        print(f"  FTP            : {ftp_url}")
    if args.user:
        print(f"  Login          : {args.user} / {args.password}")
    else:
        print("  Login          : none (anyone on your Wi-Fi can connect)")
    print("  (use --dir \"path\" to share a different folder)")
    print("=" * 60)
    print_qr(web_url)
    print("  Scan the QR code or type the address in the other device's browser.")
    print("  Press Ctrl+C to stop.\n")
    try:
        ticks = 0
        while True:
            time.sleep(1)
            ticks += 1
            if ticks % 3 == 0:    # notice when Wi-Fi gives this PC a new address
                new_ip = get_local_ip()
                if new_ip != ip and not new_ip.startswith("127."):
                    ip = new_ip
                    print(f"\n\n  ** Your address changed! New link: http://{ip}:{args.http_port}")
                    print_qr(f"http://{ip}:{args.http_port}")
                    print("  Rescan the QR code on the other device.\n")
            sys.stdout.write(
                f"\r  Sending {speed(Stats.down):>11} | Receiving {speed(Stats.up):>11} | "
                f"clients {Stats.clients} | out {human(Stats.sent)} in {human(Stats.received)}    ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n  Stopping...")


# ------------------------------------------------------------------ main ---
def main():
    p = argparse.ArgumentParser(description="Wi-Fi file sharing")
    p.add_argument("--dir", default=os.path.expanduser("~/UnisendShare"),
                   help="folder to share (default ~/UnisendShare)")
    p.add_argument("--ftp-port", type=int, default=2121)
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--user", help="optional username (protects FTP and web)")
    p.add_argument("--password", help="password for --user")
    p.add_argument("--nogui", action="store_true", help="terminal mode only")
    p.add_argument("--name", help="name other devices see (default: this computer's name)")
    p.add_argument("--no-discovery", action="store_true",
                   help="do not announce this device or look for other Unisend apps")
    p.add_argument("--discovery-addr", default="255.255.255.255", help=argparse.SUPPRESS)
    p.add_argument("--no-ftp", action="store_true",
                   help="browser page only, do not start the FTP server")
    args = p.parse_args()

    if args.user and not args.password:
        p.error("--user needs --password")

    args.dir = os.path.realpath(os.path.abspath(os.path.expanduser(args.dir)))
    os.makedirs(args.dir, exist_ok=True)
    ip = get_local_ip()

    args.use_ftp = FTP_AVAILABLE and not args.no_ftp
    if not args.no_ftp and not FTP_AVAILABLE:
        print("(pyftpdlib is not installed, so FTP is off. The browser page still works. "
              "Install it with: pip install pyftpdlib)")

    threading.Thread(target=sampler, daemon=True).start()
    if args.use_ftp:
        start_ftp(args)
    start_web(args)
    args.name = (args.name or socket.gethostname() or "Unisend device").strip()[:40]
    if not args.no_discovery:
        start_discovery(args)

    if not args.nogui:
        try:
            run_gui(args, ip)
            return
        except Exception as e:   # no tkinter / no display
            print(f"(No desktop window available: {e}. Using terminal mode.)")
    run_terminal(args, ip)


if __name__ == "__main__":
    main()
