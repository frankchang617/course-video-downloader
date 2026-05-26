#!/usr/bin/env python3
import http.server
import json
import subprocess
import threading
import os
import re
import shutil
import time
import random
import signal
from urllib.parse import urlparse, parse_qs
import urllib.request

PORT = 57821
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")

jobs = {}       # job_id -> job dict
jobs_lock = threading.Lock()

# ── history ──────────────────────────────────────────────────────────────────

def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_history(entry):
    history = load_history()
    history.insert(0, entry)
    history = history[:200]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ── curl parser ───────────────────────────────────────────────────────────────

def parse_curl(raw):
    raw = raw.strip()
    if raw.startswith("curl "):
        raw = raw[5:]
    url_match = re.search(r"['\"]?(https?://[^\s'\"\\]+)['\"]?", raw)
    if not url_match:
        return None, {}
    url = url_match.group(1).strip("'\"")
    headers = {}
    for m in re.finditer(r"-H\s+['\"]([^:]+):\s*(.*?)['\"](?=\s|$|\\)", raw):
        key = m.group(1).strip()
        val = m.group(2).strip()
        headers[key] = val
    return url, headers

def build_cmd(url, headers, folder, filename, quality, threads):
    folder = os.path.expanduser(folder)
    os.makedirs(folder, exist_ok=True)
    output = os.path.join(folder, filename + ".mp4")
    cmd = [
        "yt-dlp", "-f", quality,
        "--concurrent-fragments", str(threads),
        "--merge-output-format", "mp4",
        "--newline",
        "--legacy-server-connect",  # fix: SSL UNEXPECTED_EOF_WHILE_READING on some CDNs
        "--proxy", "",   # 绕过系统代理，直连
        "-o", output,
    ]
    skip = {"accept-encoding", "accept-language", "sec-fetch-site",
            "sec-fetch-mode", "sec-fetch-dest", "priority",
            "if-modified-since", "if-none-match", "accept"}
    for key, val in headers.items():
        lk = key.lower()
        if lk in skip:
            continue
        if lk == "user-agent":
            cmd += ["--user-agent", val]
        elif lk == "referer":
            cmd += ["--referer", val]
        else:
            display = "-".join(w.capitalize() for w in key.split("-"))
            cmd += ["--add-header", f"{display}:{val}"]
    cmd.append(url)
    return cmd, output

# ── download runner ───────────────────────────────────────────────────────────

def stream_proc(proc, job_id):
    """Read stdout line by line, handle pause/resume/cancel, parse progress."""
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue

        # handle pause
        with jobs_lock:
            status_now = jobs.get(job_id, {}).get("status")
        if status_now == "paused":
            try:
                os.kill(proc.pid, signal.SIGSTOP)
            except Exception:
                pass
            while True:
                with jobs_lock:
                    st = jobs.get(job_id, {}).get("status")
                if st != "paused":
                    break
                time.sleep(0.3)
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except Exception:
                pass

        # handle cancel
        with jobs_lock:
            if jobs.get(job_id, {}).get("status") == "cancelled":
                proc.terminate()
                return False

        with jobs_lock:
            jobs[job_id]["lines"].append(line)
            if len(jobs[job_id]["lines"]) > 300:
                jobs[job_id]["lines"] = jobs[job_id]["lines"][-300:]
            pm = re.search(r"(\d+\.?\d*)%", line)
            if pm:
                jobs[job_id]["progress"] = float(pm.group(1))
            sm = re.search(r"(\d+\.?\d*\s*[KMG]iB/s)", line)
            if sm:
                jobs[job_id]["speed"] = sm.group(1)
            em = re.search(r"ETA\s+(\S+)", line)
            if em:
                jobs[job_id]["eta"] = em.group(1)
    return True


def run_ytdlp(cmd, job_id):
    """Run a yt-dlp command, return (returncode, was_cancelled)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    with jobs_lock:
        jobs[job_id]["process"] = proc
        jobs[job_id]["pid"] = proc.pid
    completed = stream_proc(proc, job_id)
    proc.wait()
    return proc.returncode, not completed


ERROR_KEYWORDS = ["timed out", "Connection refused", "Network is unreachable",
                  "Unable to download", "HTTPConnectionPool", "proxy", "SSL"]


def get_system_proxy():
    """Return the system HTTPS/HTTP proxy URL, or empty string if none."""
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http") or ""

def looks_like_network_error(lines):
    tail = "\n".join(lines[-10:]).lower()
    return any(k.lower() in tail for k in ERROR_KEYWORDS)


def run_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        base_cmd = job["cmd"]
        job["status"] = "running"
        job["started_at"] = time.time()

    # Remove any existing --proxy arg from base_cmd
    def strip_proxy(cmd):
        result = []
        skip = False
        for tok in cmd:
            if tok == "--proxy":
                skip = True
                continue
            if skip:
                skip = False
                continue
            result.append(tok)
        return result

    base_cmd = strip_proxy(base_cmd)

    try:
        # ── Attempt 1: direct (no proxy) ──────────────────────────────────
        with jobs_lock:
            jobs[job_id]["lines"].append("⚡ 尝试直连...")
        cmd_direct = base_cmd[:-1] + ["--proxy", ""] + [base_cmd[-1]]
        rc, cancelled = run_ytdlp(cmd_direct, job_id)

        if cancelled:
            with jobs_lock:
                jobs[job_id]["status"] = "cancelled"
            return

        if rc != 0 and looks_like_network_error(jobs[job_id]["lines"]):
            # ── Attempt 2: system proxy ────────────────────────────────────
            proxy_url = get_system_proxy()
            with jobs_lock:
                jobs[job_id]["lines"].append("")
                proxy_label = proxy_url if proxy_url else "系统代理"
                jobs[job_id]["lines"].append(f"🔄 直连失败，切换代理重试（{proxy_label}）...")
                jobs[job_id]["progress"] = 0.0
                jobs[job_id]["speed"] = ""
                jobs[job_id]["eta"] = ""
                jobs[job_id]["process"] = None
                jobs[job_id]["pid"] = None
            # explicitly pass the proxy URL so yt-dlp doesn't guess wrong
            if proxy_url:
                cmd_proxy = base_cmd[:-1] + ["--proxy", proxy_url] + [base_cmd[-1]]
            else:
                cmd_proxy = base_cmd
            rc, cancelled = run_ytdlp(cmd_proxy, job_id)
            if cancelled:
                with jobs_lock:
                    jobs[job_id]["status"] = "cancelled"
                return

        with jobs_lock:
            j = jobs.get(job_id, {})
            if j.get("status") not in ("cancelled",):
                status = "done" if rc == 0 else "error"
                jobs[job_id]["status"] = status
                jobs[job_id]["finished_at"] = time.time()
                if status == "done":
                    jobs[job_id]["progress"] = 100.0
                save_history({
                    "job_id": job_id,
                    "filename": j.get("filename", ""),
                    "output": j.get("output", ""),
                    "status": status,
                    "started_at": j.get("started_at"),
                    "finished_at": jobs[job_id]["finished_at"],
                    "url": j.get("url", ""),
                })

    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["lines"].append(f"Exception: {e}")

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/check":
            self.send_json(200, {"ok": True, "ytdlp": shutil.which("yt-dlp") or "not found"})

        elif parsed.path == "/status":
            job_id = qs.get("job_id", [""])[0]
            since = int(qs.get("since", ["0"])[0])
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_json(404, {"error": "not found"})
                return
            with jobs_lock:
                new_lines = job["lines"][since:]
                self.send_json(200, {
                    "status": job["status"],
                    "progress": job.get("progress", 0),
                    "speed": job.get("speed", ""),
                    "eta": job.get("eta", ""),
                    "lines": new_lines,
                    "total_lines": len(job["lines"]),
                })

        elif parsed.path == "/jobs":
            with jobs_lock:
                result = []
                for jid, j in jobs.items():
                    result.append({
                        "job_id": jid,
                        "filename": j.get("filename", ""),
                        "output": j.get("output", ""),
                        "status": j["status"],
                        "progress": j.get("progress", 0),
                        "speed": j.get("speed", ""),
                        "eta": j.get("eta", ""),
                    })
            self.send_json(200, result)

        elif parsed.path == "/history":
            self.send_json(200, load_history())

        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        try:
            data = self.read_json()
        except Exception:
            self.send_json(400, {"error": "invalid JSON"})
            return

        if self.path == "/download":
            curl_raw = data.get("curl", "").strip()
            folder   = data.get("folder", "~/Desktop").strip() or "~/Desktop"
            filename = data.get("filename", "video").strip() or "video"
            quality  = data.get("quality", "bestvideo+bestaudio/best")
            threads  = int(data.get("threads", 16))

            if not curl_raw:
                self.send_json(400, {"error": "curl is empty"})
                return

            url, headers = parse_curl(curl_raw)
            if not url:
                self.send_json(400, {"error": "无法提取 URL"})
                return

            cmd, output = build_cmd(url, headers, folder, filename, quality, threads)
            job_id = f"{int(time.time()*1000)}_{random.randint(1000,9999)}"
            with jobs_lock:
                jobs[job_id] = {
                    "status": "pending",
                    "lines": [],
                    "process": None,
                    "pid": None,
                    "progress": 0.0,
                    "speed": "",
                    "eta": "",
                    "cmd": cmd,
                    "output": output,
                    "url": url,
                    "filename": filename,
                    "started_at": None,
                    "finished_at": None,
                }
            threading.Thread(target=run_download, args=(job_id,), daemon=True).start()
            self.send_json(200, {"job_id": job_id, "output": output})

        elif self.path == "/pause":
            job_id = data.get("job_id", "")
            with jobs_lock:
                job = jobs.get(job_id)
                if job and job["status"] == "running":
                    jobs[job_id]["status"] = "paused"
                    # SIGSTOP sent in the reader thread
                    self.send_json(200, {"ok": True})
                else:
                    self.send_json(400, {"error": "not running"})

        elif self.path == "/resume":
            job_id = data.get("job_id", "")
            with jobs_lock:
                job = jobs.get(job_id)
                if job and job["status"] == "paused":
                    jobs[job_id]["status"] = "running"
                    pid = job.get("pid")
                    if pid:
                        try:
                            os.kill(pid, signal.SIGCONT)
                        except Exception:
                            pass
                    self.send_json(200, {"ok": True})
                else:
                    self.send_json(400, {"error": "not paused"})

        elif self.path == "/cancel":
            job_id = data.get("job_id", "")
            with jobs_lock:
                job = jobs.get(job_id)
            if job:
                with jobs_lock:
                    jobs[job_id]["status"] = "cancelled"
                proc = job.get("process")
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                self.send_json(200, {"ok": True})
            else:
                self.send_json(404, {"error": "not found"})

        elif self.path == "/pick_folder":
            try:
                script = (
                    'tell application "System Events"\n'
                    '    activate\n'
                    '    set chosen to POSIX path of (choose folder with prompt "选择下载文件夹")\n'
                    '    return chosen\n'
                    'end tell'
                )
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0:
                    path = result.stdout.strip().rstrip("/")
                    self.send_json(200, {"path": path})
                else:
                    self.send_json(200, {"path": None, "cancelled": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == "/clear_history":
            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump([], f)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        else:
            self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"✅  yt-dlp 下载服务已启动，端口 {PORT}")
    print(f"    请在浏览器打开 index.html")
    print(f"    按 Ctrl+C 停止服务\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
