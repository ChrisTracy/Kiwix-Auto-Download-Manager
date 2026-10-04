#!/usr/bin/env python3
"""Kiwix manager: keeps a Wikipedia ZIM fresh and points kiwix-serve at it.

Standard library only (plus the kiwix-manage binary). kiwix-serve runs with
--monitorLibrary, so rewriting library.xml is enough to switch editions:
no restart and no Docker socket needed.
"""
import base64
import collections
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

import schedule as sched

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_DIR = DATA_DIR / ".manager"
STATE_FILE = STATE_DIR / "state.json"
LINK_NAME = os.environ.get("LINK_NAME", "wikipedia.zim")
BASE_URL = os.environ.get("BASE_URL", "https://download.kiwix.org/zim/wikipedia/")
LIBRARY_FILE = DATA_DIR / os.environ.get("LIBRARY_NAME", "library.xml")
KIWIX_URL = os.environ.get("KIWIX_URL", "http://kiwix:8080").rstrip("/")
KIWIX_PORT = os.environ.get("KIWIX_PORT", "8080")
KIWIX_MANAGE = os.environ.get("KIWIX_MANAGE", "kiwix-manage")
RELOAD_TIMEOUT = 120  # how long to wait for kiwix-serve to pick up a new library.xml
EMPTY_LIBRARY = '<?xml version="1.0" encoding="UTF-8" ?>\n<library version="20110515"></library>\n'
PORT = int(os.environ.get("PORT", "8090"))
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
CATALOG_TTL = 6 * 3600
RETRY_AFTER_ERROR = 3600
DISK_MARGIN = 512 * 1024 * 1024
UA = "kiwix-manager/1.0"

ZIM_RE = re.compile(r"wikipedia_([a-z0-9-]+)_([a-z0-9_]+?)_(\d{4}-\d{2})\.zim")
LANG_RE = re.compile(r"^[a-z0-9-]{2,20}$")
FLAVOR_RE = re.compile(r"^[a-z0-9_]{2,40}$")

def _default_schedule():
    s = dict(sched.DEFAULTS, tz=os.environ.get("TZ") or "UTC")
    if os.environ.get("SCHEDULE_CRON"):
        s.update(mode="cron", cron=os.environ["SCHEDULE_CRON"])
    try:
        return sched.normalize(s)
    except ValueError as e:
        print(f"[warn] Ignoring bad default schedule: {e}", flush=True)
        return dict(sched.DEFAULTS)


DEFAULT_CONFIG = {
    "lang": os.environ.get("WIKI_LANG", "bm"),
    "flavor": os.environ.get("WIKI_FLAVOR", "all_maxi"),
    "schedule": _default_schedule(),
    "keep_old": False,
}

INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()


class Cancelled(Exception):
    pass


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None


def fmt(n):
    if n is None:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def kiwix_get(path, timeout=3):
    """GET a kiwix-serve URL; returns the HTTP status, or None if it can't be reached."""
    try:
        req = urllib.request.Request(KIWIX_URL + path, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(1 << 16)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


class Manager:
    def __init__(self):
        self.wake = threading.Event()
        self.cancel = threading.Event()
        self.log = collections.deque(maxlen=300)
        self.catalog, self.catalog_at = {}, 0
        self.phase, self.progress = "idle", None
        self.last_error, self.latest_available = None, None
        self.force = False
        saved = self._load()
        cfg = {**DEFAULT_CONFIG, **saved.get("config", {})}
        if "interval_hours" in cfg:  # migrate the old setting
            hours = float(cfg.pop("interval_hours"))
            cfg["schedule"] = dict(cfg["schedule"], mode="interval" if hours > 0 else "manual",
                                   every=max(1, int(hours)), unit="hours")
        try:
            cfg["schedule"] = sched.normalize(cfg["schedule"])
        except ValueError:
            cfg["schedule"] = DEFAULT_CONFIG["schedule"]
        self.config = cfg
        self.started_at = time.time()
        self.schedule_since = saved.get("schedule_since") or self.started_at
        self.last_check = saved.get("last_check")
        self.retry_at = saved.get("retry_at")
        self.served = saved.get("served")  # file library.xml was last pointed at

    # ---------- persistence and logging ----------
    def _load(self):
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "config": self.config,
            "last_check": self.last_check,
            "retry_at": self.retry_at,
            "served": self.served,
            "schedule_since": self.schedule_since,
        }, indent=2))
        os.replace(tmp, STATE_FILE)

    def say(self, msg, level="info"):
        self.log.appendleft({"t": iso(time.time()), "level": level, "msg": msg})
        print(f"[{level}] {msg}", flush=True)

    # ---------- status ----------
    def current_file(self):
        link = DATA_DIR / LINK_NAME
        if link.is_symlink():
            target = os.readlink(link)
            if (DATA_DIR / target).exists():
                return target
        return None

    def schedule_base(self, s=None, since=None):
        s = s or self.config["schedule"]
        base = max(self.last_check or 0, since if since is not None else self.schedule_since)
        if not s["catch_up"]:
            base = max(base, self.started_at)  # skip runs missed while the manager was off
        return base

    def upcoming(self, s=None, since=None, count=5):
        s = s or self.config["schedule"]
        return sched.upcoming(s, self.schedule_base(s, since), time.time(), count)

    def next_check(self):
        candidates = self.upcoming(count=1)
        if self.retry_at:
            candidates.append(self.retry_at)
        return min(candidates) if candidates else None

    def container_state(self):
        return "running" if kiwix_get("/") == 200 else None

    def status(self):
        cur = self.current_file()
        current = None
        if cur:
            m = ZIM_RE.fullmatch(cur)
            current = {
                "file": cur,
                "lang": m.group(1) if m else None,
                "flavor": m.group(2) if m else None,
                "date": m.group(3) if m else None,
                "size": (DATA_DIR / cur).stat().st_size,
            }
        stored = sorted(
            ({"file": p.name, "size": p.stat().st_size} for p in DATA_DIR.glob("wikipedia_*.zim")),
            key=lambda x: x["file"],
        )
        disk = shutil.disk_usage(DATA_DIR)
        return {
            "config": self.config,
            "phase": self.phase,
            "progress": self.progress,
            "current": current,
            "stored": stored,
            "latest_available": self.latest_available,
            "last_check": iso(self.last_check),
            "next_check": iso(self.next_check()),
            "retry_at": iso(self.retry_at),
            "upcoming": [iso(t) for t in self.upcoming()],
            "last_error": self.last_error,
            "disk": {"free": disk.free, "total": disk.total},
            "container": self.container_state(),
            "kiwix_port": KIWIX_PORT,
            "log": list(self.log)[:100],
        }

    # ---------- catalog ----------
    def fetch_catalog(self, force=False):
        if not force and self.catalog and time.time() - self.catalog_at < CATALOG_TTL:
            return self.catalog
        req = urllib.request.Request(BASE_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "replace")
        cat = {}
        for lang, flavor, date in set(ZIM_RE.findall(html)):
            d = cat.setdefault(lang, {})
            if date > d.get(flavor, ""):
                d[flavor] = date
        if not cat:
            raise RuntimeError("The Kiwix download listing had no Wikipedia files in it")
        self.catalog, self.catalog_at = cat, time.time()
        return cat

    # ---------- the refresh job ----------
    def run_check(self):
        self.cancel.clear()
        cfg = dict(self.config)
        ok = False
        try:
            self.phase, self.progress, self.last_error = "checking", None, None
            cat = self.fetch_catalog(force=True)
            date = cat.get(cfg["lang"], {}).get(cfg["flavor"])
            if not date:
                raise RuntimeError(
                    f"Kiwix doesn't publish a '{cfg['flavor']}' edition for language '{cfg['lang']}'"
                )
            name = f"wikipedia_{cfg['lang']}_{cfg['flavor']}_{date}.zim"
            self.latest_available = name
            switched = False
            if self.current_file() == name and self.served == name:
                self.say(f"Up to date with {name}")
            else:
                switched = True
                if not (DATA_DIR / name).exists():
                    self.download(name)
                self.activate(name)
                self.served = name
                self.say(f"Switched the library to {name}")
            live = self.wait_for_reload(self.library_book_id(LIBRARY_FILE))
            if live and self.served == name and switched:
                self.say(f"Now serving {name}")
            elif not live:
                self.say(f"kiwix-serve at {KIWIX_URL} isn't serving {name} yet. "
                         "Is the kiwix container running? It loads library.xml when it starts.", "warn")
            if not cfg["keep_old"]:
                self.cleanup(name, live)
            ok = True
        except Cancelled:
            self.say("Download cancelled. The partial file is kept, so the next attempt resumes it.")
        except Exception as e:
            self.last_error = str(e)
            self.say(f"{e}. Retrying in an hour.", "error")
            self.retry_at = time.time() + RETRY_AFTER_ERROR
        finally:
            self.last_check = time.time()
            if ok:
                self.retry_at = None
            self._save()
            self.phase, self.progress = "idle", None

    def remote_size(self, url):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                n = r.headers.get("Content-Length")
                return int(n) if n else None
        except Exception:
            return None

    def download(self, name):
        url = BASE_URL + name
        part = DATA_DIR / (name + ".part")
        total = self.remote_size(url)
        have = part.stat().st_size if part.exists() else 0

        free = shutil.disk_usage(DATA_DIR).free
        if total and (total - have) > free - DISK_MARGIN:
            raise RuntimeError(
                f"Not enough disk space for {name}: needs {fmt(total - have)}, {fmt(free)} free"
            )

        already_complete = bool(total and have >= total)
        done = have
        if not already_complete:
            headers = {"User-Agent": UA}
            if have:
                headers["Range"] = f"bytes={have}-"
            self.say(f"Downloading {name} ({fmt(total)})"
                     + (f", resuming at {fmt(have)}" if have else ""))
            self.phase = "downloading"
            self.progress = {"done": have, "total": total, "rate": 0}
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
            except urllib.error.HTTPError as e:
                if e.code == 416 and have:
                    already_complete = True
                else:
                    raise RuntimeError(f"Download failed with HTTP {e.code}")
            if not already_complete:
                with r:
                    if have and r.status != 206:
                        done = 0  # server ignored the range; start over
                    mode = "ab" if done else "wb"
                    last_t, last_done = time.time(), done
                    with open(part, mode) as f:
                        while True:
                            if self.cancel.is_set():
                                raise Cancelled()
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if now - last_t >= 1:
                                rate = (done - last_done) / (now - last_t)
                                last_t, last_done = now, done
                                self.progress = {"done": done, "total": total, "rate": rate}
                if total and done < total:
                    raise RuntimeError(
                        f"Download stopped at {fmt(done)} of {fmt(total)}; the next check resumes it"
                    )

        self.verify(name, part)
        os.replace(part, DATA_DIR / name)
        self.say(f"Downloaded {name} ({fmt(part.parent.joinpath(name).stat().st_size)})")

    def verify(self, name, part):
        try:
            req = urllib.request.Request(BASE_URL + name + ".sha256", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                m = re.search(r"\b([0-9a-f]{64})\b", r.read().decode("utf-8", "replace"))
            expected = m.group(1) if m else None
        except Exception:
            expected = None
        if not expected:
            self.say("No checksum published for this file; skipping verification", "warn")
            return
        self.phase = "verifying"
        total = part.stat().st_size
        h, done = hashlib.sha256(), 0
        with open(part, "rb") as f:
            while chunk := f.read(4 << 20):
                if self.cancel.is_set():
                    raise Cancelled()
                h.update(chunk)
                done += len(chunk)
                self.progress = {"done": done, "total": total, "rate": 0}
        if h.hexdigest() != expected:
            part.unlink()
            raise RuntimeError(f"Checksum mismatch for {name}; the file was deleted and will be downloaded again")
        self.say("Checksum verified")

    # ---------- library.xml ----------
    @staticmethod
    def library_book_id(path):
        try:
            book = ElementTree.parse(path).getroot().find("book")
            return book.get("id") if book is not None else None
        except Exception:
            return None

    def build_library(self, name):
        """Write a temporary library.xml listing `name` under the stable link path.

        The book's path is the link (wikipedia.zim), so kiwix-serve keeps serving it at
        /content/wikipedia whatever the edition. Returns the temporary file.
        """
        tmp = DATA_DIR / (LIBRARY_FILE.name + ".tmp")
        tmp.write_text(EMPTY_LIBRARY)  # start from an XML file so kiwix-manage keeps the XML format
        if not name:
            return tmp
        cmd = [KIWIX_MANAGE, str(tmp), "add", f"--zimPathToSave={DATA_DIR / LINK_NAME}", str(DATA_DIR / name)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{KIWIX_MANAGE} isn't installed in this container; rebuild the manager image")
        if r.returncode != 0 or not self.library_book_id(tmp):
            tmp.unlink(missing_ok=True)
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError(f"kiwix-manage couldn't add {name} to the library"
                               + (f" ({detail[-1]})" if detail else ""))
        return tmp

    @staticmethod
    def publish_library(tmp):
        """Move tmp over library.xml if it differs. Returns True if library.xml changed."""
        new = tmp.read_bytes()
        try:
            old, old_mtime = LIBRARY_FILE.read_bytes(), LIBRARY_FILE.stat().st_mtime
        except FileNotFoundError:
            old, old_mtime = None, 0
        if new == old:
            tmp.unlink()
            return False
        os.replace(tmp, LIBRARY_FILE)
        # kiwix-serve reloads when the mtime goes up, compared in whole seconds,
        # so make sure two writes in the same second still count as a change.
        t = max(time.time(), int(old_mtime) + 1)
        os.utime(LIBRARY_FILE, (t, t))
        return True

    def sync_library(self):
        """On startup, make library.xml match the current link (or be empty) so kiwix-serve can start."""
        cur = self.current_file()
        try:
            if self.publish_library(self.build_library(cur)):
                self.say(f"Wrote {LIBRARY_FILE.name} for " + (cur or "an empty library"))
        except Exception as e:
            self.say(str(e), "error")
            if not LIBRARY_FILE.exists():  # kiwix-serve won't start without one
                LIBRARY_FILE.write_text(EMPTY_LIBRARY)

    def activate(self, name):
        link = DATA_DIR / LINK_NAME
        if link.exists() and not link.is_symlink():
            raise RuntimeError(
                f"{LINK_NAME} is a regular file, not a link. Rename or move it so the manager can manage it"
            )
        lib_tmp = self.build_library(name)  # reads the new file's metadata before anything changes
        tmp = DATA_DIR / (LINK_NAME + ".tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(name, tmp)  # relative, so it resolves inside the kiwix container too
        os.replace(tmp, link)
        self.publish_library(lib_tmp)

    def wait_for_reload(self, book_id):
        """True once kiwix-serve lists book_id; False if it doesn't within RELOAD_TIMEOUT."""
        if not book_id:
            return False
        self.phase = "reloading"
        deadline = time.time() + RELOAD_TIMEOUT
        while True:
            if kiwix_get(f"/catalog/v2/entry/{book_id}") == 200:
                return True
            if time.time() >= deadline or self.cancel.is_set():
                return False
            time.sleep(2)

    def cleanup(self, keep, live=True):
        old = [p for p in DATA_DIR.glob("wikipedia_*.zim*")
               if p.name != keep and not p.is_symlink() and p.suffix in (".zim", ".part")]
        if not live and any(p.suffix == ".zim" for p in old):
            # kiwix-serve may still have an old edition open; delete it once it has switched over
            self.say("Keeping older editions until kiwix-serve is serving the new one")
            old = [p for p in old if p.suffix == ".part"]
        for p in old:
            try:
                size = p.stat().st_size
                p.unlink()
                self.say(f"Removed {p.name} ({fmt(size)})")
            except OSError as e:
                self.say(f"Couldn't remove {p.name}: {e}", "warn")

    # ---------- control ----------
    def trigger(self):
        if self.phase != "idle":
            return False
        self.force = True
        self.wake.set()
        return True

    def update_config(self, data):
        new = dict(self.config)
        if "lang" in data:
            if not LANG_RE.match(str(data["lang"])):
                return 400, {"error": "That language code isn't valid"}
            new["lang"] = data["lang"]
        if "flavor" in data:
            if not FLAVOR_RE.match(str(data["flavor"])):
                return 400, {"error": "That edition isn't valid"}
            new["flavor"] = data["flavor"]
        schedule_changed = False
        if "schedule" in data:
            try:
                new["schedule"] = sched.normalize(data["schedule"])
            except ValueError as e:
                return 400, {"error": str(e)}
            schedule_changed = new["schedule"] != self.config["schedule"]
        if "keep_old" in data:
            new["keep_old"] = bool(data["keep_old"])

        edition_changed = (new["lang"], new["flavor"]) != (self.config["lang"], self.config["flavor"])
        if edition_changed and self.phase != "idle":
            return 409, {"error": "Cancel the current download before switching editions"}
        self.config = new
        if schedule_changed:
            self.schedule_since = time.time()  # don't fire runs the new schedule "missed"
            self.retry_at = None
        self._save()
        self.say("Settings saved")
        if edition_changed:
            self.trigger()
        self.wake.set()
        return 200, {"ok": True, "started": edition_changed}

    def worker(self):
        if not self.current_file():
            self.say("Nothing is being served yet; checking for an edition now")
            self.force = True
        while True:
            nxt = self.next_check()
            if self.force or (nxt is not None and time.time() >= nxt):
                self.force = False
                self.run_check()
            self.wake.wait(timeout=30)
            self.wake.clear()


M = Manager()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _authorized(self):
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Kiwix manager"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if not self._authorized():
            return
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(M.status())
        elif path == "/api/catalog":
            try:
                cat = M.fetch_catalog(force="refresh=1" in self.path)
                self._json({"catalog": cat, "fetched_at": iso(M.catalog_at)})
            except Exception as e:
                self._json({"error": f"Couldn't load the Kiwix catalog: {e}"}, 502)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "Request body must be JSON"}, 400)
        path = self.path.split("?")[0]
        if path == "/api/check":
            if M.trigger():
                self._json({"ok": True})
            else:
                self._json({"error": "A check is already running"}, 409)
        elif path == "/api/cancel":
            M.cancel.set()
            self._json({"ok": True})
        elif path == "/api/preview":
            try:
                s = sched.normalize(data.get("schedule"))
                changed = s != M.config["schedule"]
                times = M.upcoming(s, since=time.time() if changed else None)
                self._json({"upcoming": [iso(t) for t in times], "tz": s["tz"]})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif path == "/api/config":
            code, obj = M.update_config(data)
            self._json(obj, code)
        else:
            self._json({"error": "Not found"}, 404)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    M.sync_library()
    threading.Thread(target=M.worker, daemon=True).start()
    if not PASSWORD:
        M.say("ADMIN_PASSWORD is not set; the portal is open to anyone who can reach it", "warn")
    print(f"Kiwix manager on :{PORT}, managing {LIBRARY_FILE} for kiwix-serve at {KIWIX_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
