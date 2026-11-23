#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

import imgconv

INDEX_HTML = Path(__file__).resolve().parent / "docs" / "index.html"
WORKSPACE = Path(tempfile.gettempdir()) / "nolan-converter"
HOME = Path.home().resolve()
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
JOB_TTL_SECONDS = 3600

jobs: dict[str, "Job"] = {}
state_lock = threading.Lock()
# 一次只跑一個轉檔工作：多個分頁同時送出時排隊，避免同時把 CPU 和記憶體吃滿
convert_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.dir = WORKSPACE / self.id
        self.in_dir = self.dir / "in"
        self.out_dir = self.dir / "out"
        self.in_dir.mkdir(parents=True)
        self.output_dir = self.out_dir
        self.status = "uploading" if kind == "upload" else "queued"
        self.created = time.time()
        self.fmt = None
        self.total = self.done = self.failures = 0
        self.log: list[dict] = []
        self.skipped: list[str] = []
        self.error = None

    def snapshot(self, since: int) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "failures": self.failures,
            "skipped": self.skipped,
            "log": self.log[since:],
            "error": self.error,
            "output_dir": str(self.output_dir) if self.kind == "folder" else None,
        }

    def tidy(self, message: str) -> str:
        # 上傳模式的路徑是暫存資料夾，對使用者沒意義，只留檔名
        return message.replace(f"{self.in_dir}/", "").replace(f"{self.out_dir}/", "")

    def record(self, src: Path, dst: Path, error: str | None):
        with state_lock:
            self.done += 1
            if error:
                self.failures += 1
            self.log.append({"name": src.name, "output": dst.name, "error": error})


def new_job(kind: str) -> Job:
    now = time.time()
    with state_lock:
        for job_id, job in list(jobs.items()):
            if job.status not in ("queued", "running") and now - job.created > JOB_TTL_SECONDS:
                shutil.rmtree(job.dir, ignore_errors=True)
                del jobs[job_id]
        job = Job(kind)
        jobs[job.id] = job
    return job


def get_job(job_id: str) -> Job:
    with state_lock:
        job = jobs.get(job_id)
    if not job:
        raise ApiError(404, "找不到這個轉檔工作（可能已過期）")
    return job


def parse_options(body: dict) -> dict:
    fmt = body.get("format")
    if fmt not in imgconv.OUTPUT_FORMATS:
        raise ApiError(400, "不支援的輸出格式")
    quality = body.get("quality")
    if quality is not None and not (isinstance(quality, int) and 1 <= quality <= 100):
        raise ApiError(400, "品質要是 1–100 的整數")
    max_side = body.get("max_side")
    if max_side is not None and not (isinstance(max_side, int) and max_side > 0):
        raise ApiError(400, "長邊上限要是正整數")
    return {
        "fmt": fmt,
        "quality": quality or imgconv.DEFAULT_QUALITY.get(fmt),
        "max_side": max_side,
        "fast": bool(body.get("fast")),
    }


def resolve_home_path(raw, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, f"請輸入{label}路徑")
    # 從 Finder 拖進輸入框或從終端機複製時常會帶引號
    path = Path(raw.strip().strip("'\"")).expanduser().resolve()
    if not path.is_relative_to(HOME):
        raise ApiError(403, f"{label}只能在家目錄（{HOME}）底下")
    return path


def run_job(job: Job, inputs: list[Path], output_dir: Path, recursive: bool, options: dict):
    with convert_lock:
        job.status = "running"
        try:
            args = SimpleNamespace(
                inputs=inputs, recursive=recursive, output=output_dir, to=options["fmt"], overwrite=False
            )
            pairs, skipped = imgconv.collect_jobs(args)
            with state_lock:
                job.total = len(pairs)
                job.skipped = [job.tidy(message) for message in skipped]
            imgconv.run_jobs(
                pairs, options["fmt"], options["quality"], options["max_side"], options["fast"],
                os.cpu_count(), job.record,
            )
            job.status = "done"
        except Exception as error:
            job.error = str(error)
            job.status = "error"


def start_job(job: Job, inputs: list[Path], output_dir: Path, recursive: bool, options: dict):
    job.fmt = options["fmt"]
    job.status = "queued"
    threading.Thread(target=run_job, args=(job, inputs, output_dir, recursive, options), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "Nolan"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def dispatch(self, method: str):
        try:
            self.check_request(method)
            url = urlparse(self.path)
            parts = [part for part in url.path.split("/") if part]
            self.route(method, parts, parse_qs(url.query))
        except ApiError as error:
            self.close_connection = True
            self.send_json({"error": str(error)}, error.status)
        except Exception as error:
            self.close_connection = True
            self.send_json({"error": f"伺服器內部錯誤：{error}"}, 500)

    def check_request(self, method: str):
        # 只接受直接連 127.0.0.1/localhost 的請求，擋 DNS rebinding
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            raise ApiError(403, "不允許的 Host")
        # 自訂標頭會觸發 CORS 預檢，其他網站就無法跨站叫這台 Mac 轉檔或寫檔
        if method != "GET" and self.headers.get("X-Nolan") != "1":
            raise ApiError(403, "缺少 X-Nolan 標頭")

    def route(self, method: str, parts: list[str], query: dict):
        match (method, parts):
            case ("GET", []):
                self.send_file(INDEX_HTML, "text/html; charset=utf-8")
            case ("GET", ["api", "ping"]):
                self.send_json({"engine": "local", "cpu": os.cpu_count(), "home": str(HOME)})
            case ("GET", ["api", "jobs", job_id]):
                since = int(query.get("since", ["0"])[0] or 0)
                job = get_job(job_id)
                with state_lock:
                    snapshot = job.snapshot(since)
                self.send_json(snapshot)
            case ("GET", ["api", "jobs", job_id, "zip"]):
                self.send_zip(get_job(job_id))
            case ("POST", ["api", "jobs"]):
                self.send_json({"id": new_job("upload").id})
            case ("PUT", ["api", "jobs", job_id, "files"]):
                self.receive_upload(get_job(job_id), query.get("name", [""])[0])
            case ("POST", ["api", "jobs", job_id, "start"]):
                self.start_upload_job(get_job(job_id))
            case ("POST", ["api", "jobs", job_id, "reveal"]):
                self.reveal(get_job(job_id))
            case ("POST", ["api", "folder"]):
                self.start_folder_job()
            case _:
                raise ApiError(404, "找不到")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON_BYTES:
            raise ApiError(413, "請求太大")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "JSON 格式錯誤")
        if not isinstance(body, dict):
            raise ApiError(400, "JSON 格式錯誤")
        return body

    def receive_upload(self, job: Job, raw_name: str):
        if job.status != "uploading":
            raise ApiError(409, "這個工作已經開始轉檔，不能再上傳")
        name = Path(raw_name).name
        if not name or name.startswith(".") or Path(name).suffix.lower() not in imgconv.INPUT_EXTS:
            raise ApiError(400, f"不支援的檔案：{raw_name}")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            raise ApiError(413, f"{name} 超過單檔上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
        with (job.in_dir / name).open("wb") as file:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    raise ApiError(400, f"{name} 上傳中斷")
                file.write(chunk)
                remaining -= len(chunk)
        self.send_json({"ok": True})

    def start_upload_job(self, job: Job):
        options = parse_options(self.read_json())
        if job.status != "uploading":
            raise ApiError(409, "這個工作已經開始了")
        if not any(job.in_dir.iterdir()):
            raise ApiError(400, "還沒有上傳任何檔案")
        start_job(job, [job.in_dir], job.out_dir, False, options)
        self.send_json({"id": job.id})

    def start_folder_job(self):
        body = self.read_json()
        options = parse_options(body)
        folder = resolve_home_path(body.get("path"), "資料夾")
        if not folder.is_dir():
            raise ApiError(400, f"找不到這個資料夾：{folder}")
        if body.get("output"):
            output_dir = resolve_home_path(body["output"], "輸出資料夾")
        else:
            output_dir = folder.parent / f"{folder.name}_{options['fmt']}"
        recursive = bool(body.get("recursive"))
        if recursive and output_dir.is_relative_to(folder):
            raise ApiError(400, "包含子資料夾時，輸出資料夾不能放在來源資料夾裡面")
        job = new_job("folder")
        job.output_dir = output_dir
        start_job(job, [folder], output_dir, recursive, options)
        self.send_json({"id": job.id, "output_dir": str(output_dir)})

    def reveal(self, job: Job):
        if not job.output_dir.exists():
            raise ApiError(404, "輸出資料夾不存在")
        subprocess.run(["open", str(job.output_dir)], check=False)
        self.send_json({"ok": True})

    def send_zip(self, job: Job):
        if job.kind != "upload" or job.status != "done":
            raise ApiError(409, "轉檔還沒完成")
        zip_path = job.dir / "result.zip"
        if not zip_path.exists():
            partial = job.dir / "result.zip.part"
            # 圖檔本身已壓縮過，用 STORED 省下大量 CPU
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as archive:
                for file in sorted(job.out_dir.glob("*")):
                    archive.write(file, file.name)
            partial.rename(zip_path)
        self.send_file(zip_path, "application/zip", download_name=f"Nolan轉檔_{job.fmt}.zip")

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str, download_name: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header(
                "Content-Disposition", f"attachment; filename=\"nolan.zip\"; filename*=UTF-8''{quote(download_name)}"
            )
        self.end_headers()
        with path.open("rb") as file:
            shutil.copyfileobj(file, self.wfile, 1 << 20)


def main():
    parser = argparse.ArgumentParser(description="Nolan｜快速轉檔器 本機伺服器（只接受這台電腦的連線）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="啟動後不要自動開瀏覽器")
    args = parser.parse_args()

    shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Nolan｜快速轉檔器：{url}（Ctrl+C 結束）")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutil.rmtree(WORKSPACE, ignore_errors=True)


if __name__ == "__main__":
    main()
