#!/usr/bin/env python3
"""runDashboard web server (standard library only).

    python3 server.py --port 8765                        # local only
    python3 server.py --host 0.0.0.0 --port 8765         # share with the team
"""

import argparse
import concurrent.futures as cf
import csv
import gzip
import io
import json
import os
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ALLOWED_ROOTS = []
SUMMARY_POOL = cf.ThreadPoolExecutor(8)
DEEP_POOL = cf.ThreadPoolExecutor(2)  # tracker parsing is heavy; keep NFS + CPU load modest
_inflight, _inflight_lock = {}, threading.Lock()


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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            route = {
                "/api/config": self.api_config,
                "/api/scan": self.api_scan,
                "/api/deep": self.api_deep,
                "/api/flow": self.api_flow,
                "/api/csv": self.api_csv,
                "/api/chainsaw": self.api_chainsaw,
                "/api/chainsaw/image": self.api_chainsaw_image,
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

    def api_config(self, q):
        self._json({"allowed_roots": ALLOWED_ROOTS, "cache_dir": extract.CACHE_DIR})

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
