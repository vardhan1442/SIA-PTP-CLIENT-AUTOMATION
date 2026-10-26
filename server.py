#!/usr/bin/env python3
"""runDashboard web server (standard library only).

    python3 server.py --port 8765                        # local only
    python3 server.py --host 0.0.0.0 --port 8765         # share with the team
"""

import argparse
import concurrent.futures as cf
import csv
import gzip
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(APP_DIR, "static")
USERS_FILE = os.path.join(APP_DIR, "users.json")
ACCESS_LOG_FILE = os.path.join(APP_DIR, "access_log.jsonl")
ACCESS_LOG_MAX = 500
ALLOWED_ROOTS = []
SUMMARY_POOL = cf.ThreadPoolExecutor(8)
DEEP_POOL = cf.ThreadPoolExecutor(2)  # tracker parsing is heavy; keep NFS + CPU load modest
_inflight, _inflight_lock = {}, threading.Lock()

SESSION_COOKIE = "rd_session"
SESSION_TTL_S = 12 * 3600
PBKDF2_ITERATIONS = 200_000
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_S = 300
_users_lock = threading.Lock()
_sessions, _sessions_lock = {}, threading.Lock()
_login_attempts, _login_attempts_lock = {}, threading.Lock()
_access_lock = threading.Lock()


def _load_users():
    with _users_lock:
        if not os.path.exists(USERS_FILE):
            return {}
        with open(USERS_FILE) as fh:
            return json.load(fh)


def _save_users(users):
    with _users_lock:
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(users, fh, indent=2)
        os.replace(tmp, USERS_FILE)


def _hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def _verify_password(password, salt_hex, hash_hex):
    _, digest_hex = _hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(digest_hex, hash_hex)


def _record_access(entry):
    with _access_lock:
        lines = []
        if os.path.exists(ACCESS_LOG_FILE):
            with open(ACCESS_LOG_FILE) as fh:
                lines = fh.readlines()
        lines.append(json.dumps(entry) + "\n")
        lines = lines[-ACCESS_LOG_MAX:]
        tmp = ACCESS_LOG_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp, ACCESS_LOG_FILE)


def _recent_access(limit=100):
    if not os.path.exists(ACCESS_LOG_FILE):
        return []
    with _access_lock, open(ACCESS_LOG_FILE) as fh:
        lines = fh.readlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()
    return out


def _bootstrap_users():
    if _load_users():
        return
    password = secrets.token_urlsafe(12)
    salt_hex, hash_hex = _hash_password(password)
    _save_users({"admin": {"salt": salt_hex, "hash": hash_hex, "is_admin": True}})
    print(f"[runDashboard] Created default admin account. Username: admin  Password: {password}", flush=True)
    print("[runDashboard] Log in and change this password immediately (Setup page).", flush=True)


def _new_session(username, is_admin):
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = {"user": username, "is_admin": is_admin, "expires": time.time() + SESSION_TTL_S}
    return token


def _get_session(token):
    if not token:
        return None
    with _sessions_lock:
        s = _sessions.get(token)
        if s and s["expires"] < time.time():
            del _sessions[token]
            s = None
        return dict(s) if s else None


def _drop_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


def _drop_sessions_for_user(username):
    with _sessions_lock:
        for token, s in list(_sessions.items()):
            if s["user"] == username:
                del _sessions[token]


def _login_allowed(username):
    now = time.time()
    with _login_attempts_lock:
        attempts = [t for t in _login_attempts.get(username, []) if now - t < LOGIN_WINDOW_S]
        _login_attempts[username] = attempts
        return len(attempts) < LOGIN_MAX_ATTEMPTS


def _record_login_failure(username):
    with _login_attempts_lock:
        _login_attempts.setdefault(username, []).append(time.time())


def _clear_login_failures(username):
    with _login_attempts_lock:
        _login_attempts.pop(username, None)


def resolve(path):
    """Return the realpath if inside an allowed root, else raise PermissionError."""
    if not path:
        raise ValueError("path is required")
    real = os.path.realpath(os.path.expanduser(path.strip()))
    if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in ALLOWED_ROOTS):
        raise PermissionError(f"path must be under one of: {', '.join(ALLOWED_ROOTS)}")
    if not os.path.isdir(real):
        raise FileNotFoundError(f"not a directory: {real}")
    return real


def resolve_file(path):
    """Like resolve(), but for a regular file rather than a directory."""
    if not path:
        raise ValueError("path is required")
    real = os.path.realpath(os.path.expanduser(path.strip()))
    if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in ALLOWED_ROOTS):
        raise PermissionError(f"path must be under one of: {', '.join(ALLOWED_ROOTS)}")
    if not os.path.isfile(real):
        raise FileNotFoundError(f"not a file: {real}")
    return real


def deep_future(test_dir):
    with _inflight_lock:
        fut = _inflight.get(test_dir)
        if fut is None or fut.done():
            fut = DEEP_POOL.submit(extract.cached_deep, test_dir)
            _inflight[test_dir] = fut
        return fut


class Handler(SimpleHTTPRequestHandler):
    server_version = "runDashboard/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s [%s] %s\n" % (self.address_string(), time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, default=str), extra=extra)

    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _session(self):
        return _get_session(self._cookie_token())

    def _set_session_cookie(self, token):
        jar = SimpleCookie()
        jar[SESSION_COOKIE] = token
        jar[SESSION_COOKIE]["path"] = "/"
        jar[SESSION_COOKIE]["httponly"] = True
        jar[SESSION_COOKIE]["samesite"] = "Lax"
        jar[SESSION_COOKIE]["max-age"] = SESSION_TTL_S
        return jar[SESSION_COOKIE].OutputString()

    def _clear_session_cookie(self):
        jar = SimpleCookie()
        jar[SESSION_COOKIE] = ""
        jar[SESSION_COOKIE]["path"] = "/"
        jar[SESSION_COOKIE]["max-age"] = 0
        return jar[SESSION_COOKIE].OutputString()

    def _origin_ok(self):
        """Same-origin check for state-changing requests (defense in depth alongside SameSite=Lax)."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True
        return urlparse(origin).netloc == host

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/me":
                    return self.api_me(q)
                session = self._session()
                if not session:
                    return self._json({"error": "not authenticated"}, 401)
                route = {
                    "/api/config": self.api_config,
                    "/api/scan": self.api_scan,
                    "/api/deep": self.api_deep,
                    "/api/flow": self.api_flow,
                    "/api/csv": self.api_csv,
                    "/api/chainsaw": self.api_chainsaw,
                    "/api/chainsaw/image": self.api_chainsaw_image,
                    "/api/browse": self.api_browse,
                    "/api/file": self.api_file,
                    "/api/users": lambda qq: self.api_users_list(qq, session),
                    "/api/access": lambda qq: self.api_access_list(qq, session),
                }.get(u.path)
                if not route:
                    return self._json({"error": "unknown endpoint"}, 404)
                route(q)
            except PermissionError as e:
                self._json({"error": str(e)}, HTTPStatus.FORBIDDEN)
            except (FileNotFoundError, ValueError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        if u.path in ("/login.html", "/favicon.ico"):
            return super().do_GET()
        session = self._session()
        if not session:
            self.send_response(302)
            self.send_header("Location", "/login.html?next=" + quote(self.path, safe=""))
            self.end_headers()
            return
        if u.path == "/setup.html" and not session["is_admin"]:
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return self._json({"error": "invalid JSON body"}, 400)
        try:
            if u.path == "/api/login":
                return self.api_login(body)
            if not self._origin_ok():
                return self._json({"error": "cross-origin request blocked"}, 403)
            route = {
                "/api/logout": self.api_logout,
                "/api/users": self.api_users_create,
                "/api/users/password": self.api_users_password,
                "/api/access": self.api_access_record,
            }.get(u.path)
            if not route:
                return self._json({"error": "unknown endpoint"}, 404)
            session = self._session()
            if not session:
                return self._json({"error": "not authenticated"}, 401)
            route(body, session)
        except PermissionError as e:
            self._json({"error": str(e)}, HTTPStatus.FORBIDDEN)
        except (FileNotFoundError, ValueError) as e:
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if not self._origin_ok():
                return self._json({"error": "cross-origin request blocked"}, 403)
            if u.path != "/api/users":
                return self._json({"error": "unknown endpoint"}, 404)
            session = self._session()
            if not session:
                return self._json({"error": "not authenticated"}, 401)
            self.api_users_delete(q, session)
        except PermissionError as e:
            self._json({"error": str(e)}, HTTPStatus.FORBIDDEN)
        except (FileNotFoundError, ValueError) as e:
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def api_login(self, body):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._json({"error": "username and password are required"}, 400)
        if not _login_allowed(username):
            return self._json({"error": "too many failed attempts; try again later"}, 429)
        record = _load_users().get(username)
        if not record or not _verify_password(password, record["salt"], record["hash"]):
            _record_login_failure(username)
            return self._json({"error": "invalid username or password"}, 401)
        _clear_login_failures(username)
        token = _new_session(username, bool(record.get("is_admin")))
        self._json({"ok": True, "user": username, "is_admin": bool(record.get("is_admin"))},
                   extra={"Set-Cookie": self._set_session_cookie(token)})

    def api_logout(self, body, session):
        _drop_session(self._cookie_token())
        self._json({"ok": True}, extra={"Set-Cookie": self._clear_session_cookie()})

    def api_me(self, q):
        session = self._session()
        if not session:
            return self._json({"error": "not authenticated"}, 401)
        self._json({"user": session["user"], "is_admin": session["is_admin"]})

    def api_users_list(self, q, session):
        if not session["is_admin"]:
            raise PermissionError("admin only")
        users = _load_users()
        self._json({"users": [{"username": u, "is_admin": bool(r.get("is_admin"))}
                               for u, r in sorted(users.items())]})

    def api_users_create(self, body, session):
        if not session["is_admin"]:
            raise PermissionError("admin only")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            raise ValueError("username and password are required")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        users = _load_users()
        if username in users:
            raise ValueError("user already exists")
        salt_hex, hash_hex = _hash_password(password)
        users[username] = {"salt": salt_hex, "hash": hash_hex, "is_admin": bool(body.get("is_admin"))}
        _save_users(users)
        self._json({"ok": True})

    def api_users_delete(self, q, session):
        if not session["is_admin"]:
            raise PermissionError("admin only")
        username = q.get("username")
        if not username:
            raise ValueError("username is required")
        users = _load_users()
        if username not in users:
            raise ValueError("user not found")
        admins = [u for u, r in users.items() if r.get("is_admin")]
        if username in admins and len(admins) <= 1:
            raise ValueError("cannot remove the last admin account")
        del users[username]
        _save_users(users)
        _drop_sessions_for_user(username)
        self._json({"ok": True})

    def api_users_password(self, body, session):
        target = (body.get("username") or session["user"]).strip()
        new_password = body.get("new_password") or ""
        if len(new_password) < 8:
            raise ValueError("password must be at least 8 characters")
        users = _load_users()
        record = users.get(target)
        if not record:
            raise ValueError("user not found")
        if target == session["user"]:
            if not _verify_password(body.get("current_password") or "", record["salt"], record["hash"]):
                raise PermissionError("current password is incorrect")
        elif not session["is_admin"]:
            raise PermissionError("admin only")
        record["salt"], record["hash"] = _hash_password(new_password)
        _save_users(users)
        self._json({"ok": True})

    def api_config(self, q):
        self._json({"allowed_roots": ALLOWED_ROOTS, "cache_dir": extract.CACHE_DIR})

    def api_access_record(self, body, session):
        """Log who connected from which machine, and report whether the data root is reachable from here."""
        machine = (body.get("machine") or "").strip()[:200]
        root = ALLOWED_ROOTS[0] if ALLOWED_ROOTS else None
        root_ok = bool(root and os.path.isdir(root))
        entry = {
            "ts": time.time(),
            "user": session["user"],
            "machine": machine,
            "remote_addr": self.client_address[0],
            "root": root,
            "root_ok": root_ok,
        }
        _record_access(entry)
        self._json({"ok": True, "root": root, "root_ok": root_ok})

    def api_access_list(self, q, session):
        if not session["is_admin"]:
            raise PermissionError("admin only")
        self._json({"entries": _recent_access(100)})

    def api_browse(self, q):
        real = resolve(q.get("path"))
        entries = []
        with os.scandir(real) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "is_dir": e.is_dir(follow_symlinks=False),
                    "size": None if e.is_dir(follow_symlinks=False) else st.st_size,
                    "mtime": st.st_mtime,
                })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = os.path.dirname(real.rstrip(os.sep))
        has_parent = any(parent == r or parent.startswith(r.rstrip(os.sep) + os.sep) for r in ALLOWED_ROOTS)
        self._json({"path": real, "parent": parent if has_parent and parent != real else None, "entries": entries})

    def api_file(self, q):
        real = resolve_file(q.get("path"))
        size = os.path.getsize(real)
        if size > 25 * 1024 * 1024:
            raise ValueError("file exceeds 25 MB download limit")
        with open(real, "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        name = os.path.basename(real)
        self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})

    def _test_dir(self, q):
        test_dir = resolve(q.get("path"))
        if not extract.is_test_dir(test_dir):
            raise ValueError("not a test directory")
        return test_dir

    def api_chainsaw(self, q):
        self._json({"groups": extract.chainsaw_gallery(self._test_dir(q))})

    def api_chainsaw_image(self, q):
        test_dir = self._test_dir(q)
        name = q.get("file", "")
        parts = name.split("/")
        if (not name.endswith(".png.gz") or not parts or
                any(part in ("", ".", "..") for part in parts) or
                len(parts) > 5 or
                (len(parts) > 1 and not any(part.startswith("chainsaw_") or
                                            part.startswith("ddr_latency_") for part in parts[:-1]))):
            raise FileNotFoundError("Chainsaw graph not found")
        path = os.path.realpath(os.path.join(test_dir, name))
        if not path.startswith(test_dir + os.sep):
            raise PermissionError("graph outside test directory")
        if not os.path.isfile(path):
            raise FileNotFoundError("Chainsaw graph not found")
        with gzip.open(path, "rb") as fh:
            data = fh.read(12 * 1024 * 1024 + 1)
        if len(data) > 12 * 1024 * 1024:
            raise ValueError("graph exceeds 12 MB")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("graph is not a PNG")
        self._send(200, data, "image/png", {"X-Content-Type-Options": "nosniff"})

    def api_scan(self, q):
        root = resolve(q.get("path"))
        t0 = time.time()
        tests = extract.find_test_dirs(root)
        sums = list(SUMMARY_POOL.map(extract.cached_summary, tests))
        for s in sums:
            s["rel"] = os.path.relpath(s["test_dir"], root)
        self._json({"root": root, "count": len(sums), "elapsed_s": round(time.time() - t0, 2), "tests": sums})

    def api_deep(self, q):
        test_dir = resolve(q.get("path"))
        if not extract.is_test_dir(test_dir):
            raise ValueError("not a test directory (no *.rpt.gz)")
        self._json(deep_future(test_dir).result())

    def api_flow(self, q):
        self._json(extract.flow_summary(self._test_dir(q)))

    def api_csv(self, q):
        root = resolve(q.get("path"))
        tests = extract.find_test_dirs(root)
        sums = list(SUMMARY_POOL.map(extract.cached_summary, tests))
        deeps = [deep_future(t) for t in tests] if q.get("deep") == "1" else []
        deeps = [f.result() for f in deeps] or [None] * len(tests)
        rows = [extract.flat_row(s, d) for s, d in zip(sums, deeps)]
        fields = list(dict.fromkeys(extract.CSV_FIELDS + [k for r in rows for k in r]))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        name = os.path.basename(root.rstrip("/")) or "run"
        self._send(200, buf.getvalue(), "text/csv",
                   {"Content-Disposition": f'attachment; filename="{name}_summary.csv"'})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to share on the network)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-root", action="append", default=None,
                    help="directory prefix users may browse (repeatable). Default: /nfs/site/disks")
    a = ap.parse_args()
    ALLOWED_ROOTS[:] = [os.path.realpath(r) for r in (a.allow_root or ["/nfs/site/disks"])]
    _bootstrap_users()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    host = os.uname().nodename if a.host in ("0.0.0.0", "") else a.host
    print(f"runDashboard on http://{host}:{a.port}/  (roots: {', '.join(ALLOWED_ROOTS)}; cache: {extract.CACHE_DIR})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
