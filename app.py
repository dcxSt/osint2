#!/usr/bin/env python3
"""Run `python3 app.py`, then open http://127.0.0.1:8765."""
import argparse
import json
import pathlib
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from research import FetchError, Job, markdown_report, normalize_url, run_job

ROOT = pathlib.Path(__file__).resolve().parent
JOBS = {}
JOBS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "Fieldnotes/1.0"

    def log_message(self, fmt, *args):
        # Profile URLs and response bodies are not written to logs.
        pass

    def send(self, status, data, content_type="application/json; charset=utf-8", filename=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted_request(self):
        port = self.server.server_address[1]
        hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
        if self.headers.get("Host") not in hosts:
            self.send(403, {"error": "This server accepts local requests only"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            self.send(403, {"error": "Cross-origin requests are not permitted"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send(403, {"error": "Cross-site requests are not permitted"})
            return False
        return True

    def do_GET(self):
        if not self.trusted_request():
            return
        path = urlsplit(self.path).path
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if path in static:
            filename, content_type = static[path]
            self.send(200, (ROOT / "static" / filename).read_bytes(), content_type)
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/(report\.md|report\.json))?", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match[1])
            if job is None:
                self.send(404, {"error": "Research session not found; sessions reset when the server restarts"})
                return
            report = job.snapshot()
            if match[2] == "report.md":
                self.send(200, markdown_report(report), "text/markdown; charset=utf-8", "fieldnotes-report.md")
            elif match[2] == "report.json":
                self.send(200, report, filename="fieldnotes-report.json")
            else:
                self.send(200, report)
            return
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if not self.trusted_request():
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send(415, {"error": "Expected application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Request must be between 1 and 4096 bytes")
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, OSError):
            self.send(400, {"error": "Invalid JSON request"})
            return
        path = urlsplit(self.path).path
        if path == "/api/jobs":
            try:
                seed = normalize_url(data.get("url", ""))
                pages, depth = data.get("pages", 10), data.get("depth", 2)
                if type(pages) is not int or type(depth) is not int or not 1 <= pages <= 20 or not 0 <= depth <= 3:
                    raise ValueError("Use 1–20 pages and a depth of 0–3")
            except (FetchError, ValueError) as exc:
                self.send(400, {"error": str(exc)})
                return
            with JOBS_LOCK:
                if sum(j.status in ("queued", "running") for j in JOBS.values()) >= 2:
                    self.send(429, {"error": "Two research sessions are already running. Cancel one or wait for it to finish."})
                    return
                while len(JOBS) >= 20:
                    oldest = next((key for key, j in JOBS.items() if j.status not in ("queued", "running")), None)
                    if oldest is None:
                        break
                    del JOBS[oldest]
                job = Job(uuid.uuid4().hex, seed, pages, depth)
                JOBS[job.id] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self.send(202, {"id": job.id})
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match[1])
            if job:
                job.cancel.set()
                job.event("Cancellation requested")
                self.send(200, {"ok": True})
            else:
                self.send(404, {"error": "Research session not found"})
            return
        self.send(404, {"error": "Not found"})


def main():
    parser = argparse.ArgumentParser(description="Fieldnotes — sourced public-profile research")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--url", help="Research a public profile in the terminal instead")
    parser.add_argument("--pages", type=int, choices=range(1, 21), default=10)
    parser.add_argument("--depth", type=int, choices=range(4), default=2)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    if args.url:
        try:
            seed = normalize_url(args.url)
        except FetchError as exc:
            parser.error(str(exc))
        job = Job(uuid.uuid4().hex, seed, args.pages, args.depth)
        run_job(job)
        report = job.snapshot()
        result = json.dumps(report, indent=2, ensure_ascii=False) if args.format == "json" else markdown_report(report)
        if args.output:
            args.output.write_text(result, encoding="utf-8")
        else:
            print(result)
        return 0 if report["sources"] and report["status"] == "complete" else 1
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Fieldnotes is running at http://127.0.0.1:" + str(server.server_address[1]), flush=True)
    print("Reports stay in memory until you export them. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        for job in JOBS.values():
            job.cancel.set()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
