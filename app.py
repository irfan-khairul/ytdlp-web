"""Small web UI around yt-dlp.

Jobs are kept in memory, so run this as a single process (the bundled
waitress server does that). Each job downloads into its own folder under
DOWNLOAD_DIR, which keeps filenames from colliding and makes serving safe.
"""

import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from yt_dlp.utils import DownloadCancelled, DownloadError

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", Path(__file__).parent / "downloads")).resolve()
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
# Finished files older than this are deleted automatically. 0 disables cleanup.
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Put the bundled ffmpeg/ffprobe/deno (from scripts/setup.sh) ahead of any
# system copies. yt-dlp finds all of them via PATH.
BUNDLED_BIN = Path(__file__).parent / "runtime" / "bin"
if BUNDLED_BIN.is_dir():
    os.environ["PATH"] = f"{BUNDLED_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

# Video presets prefer H.264/AAC so the result plays everywhere, then fall
# back to whatever is best if that combination isn't available.
PRESETS = {
    "best": {"label": "Best quality", "format": "bv*+ba/b"},
    "1080": {"label": "1080p", "format": "bv*[height<=1080][vcodec^=avc]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]"},
    "720": {"label": "720p", "format": "bv*[height<=720][vcodec^=avc]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]"},
    "480": {"label": "480p", "format": "bv*[height<=480][vcodec^=avc]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]"},
    "mp3": {"label": "Audio (MP3)", "format": "ba/b", "audio": "mp3"},
    "m4a": {"label": "Audio (M4A)", "format": "ba[ext=m4a]/ba/b", "audio": "m4a"},
}

app = Flask(__name__, static_folder="static")
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
jobs = {}
jobs_lock = threading.Lock()


class Job:
    def __init__(self, url, preset):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.preset = preset
        self.status = "queued"  # queued, downloading, processing, done, error, cancelled
        self.title = None
        self.thumbnail = None
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.stage = None
        self.error = None
        self.filename = None
        self.filesize = None
        self.created = time.time()
        self.finished = None
        self.cancel_requested = False

    @property
    def dir(self):
        return DOWNLOAD_DIR / self.id

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "preset": self.preset,
            "preset_label": PRESETS[self.preset]["label"],
            "status": self.status,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "eta": self.eta,
            "stage": self.stage,
            "error": self.error,
            "filename": self.filename,
            "filesize": self.filesize,
            "created": self.created,
        }


def base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
    }


def run_job(job):
    if job.cancel_requested:
        job.status = "cancelled"
        return

    preset = PRESETS[job.preset]
    job.status = "downloading"
    job.dir.mkdir(parents=True, exist_ok=True)
    parts_done = 0

    def progress_hook(d):
        nonlocal parts_done
        if job.cancel_requested:
            raise DownloadCancelled("Cancelled by user")
        info = d.get("info_dict") or {}
        job.title = job.title or info.get("title")
        job.thumbnail = job.thumbnail or info.get("thumbnail")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job.percent = min(100.0, d.get("downloaded_bytes", 0) / total * 100)
            job.speed = d.get("speed")
            job.eta = d.get("eta")
            if info.get("vcodec") not in (None, "none") and info.get("acodec") in (None, "none"):
                job.stage = "video"
            elif info.get("acodec") not in (None, "none") and info.get("vcodec") in (None, "none"):
                job.stage = "audio"
            else:
                job.stage = None
        elif d["status"] == "finished":
            parts_done += 1
            job.percent = 100.0
            job.speed = job.eta = None

    def postprocessor_hook(d):
        if d["status"] == "started":
            job.status = "processing"
            job.stage = d.get("postprocessor")

    opts = base_opts() | {
        "format": preset["format"],
        "outtmpl": str(job.dir / "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "restrictfilenames": False,
        "windowsfilenames": True,
    }
    if "audio" in preset:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": preset["audio"], "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegMetadata"}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job.url, download=True)
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads else None
        if not path or not path.exists():
            # Fall back to whatever ended up in the job folder.
            files = [p for p in job.dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
            path = max(files, key=lambda p: p.stat().st_size) if files else None
        if not path:
            raise DownloadError("Download finished but no output file was found")
        job.filename = path.name
        job.filesize = path.stat().st_size
        job.status = "done"
        job.percent = 100.0
        job.stage = None
    except DownloadCancelled:
        job.status = "cancelled"
        shutil.rmtree(job.dir, ignore_errors=True)
    except Exception as e:  # yt-dlp raises a variety of errors; surface them all
        if job.cancel_requested:
            job.status = "cancelled"
            shutil.rmtree(job.dir, ignore_errors=True)
        else:
            job.status = "error"
            job.error = clean_error(e)
    finally:
        job.finished = time.time()
        job.speed = job.eta = None


def clean_error(e):
    msg = str(e)
    for prefix in ("ERROR: ", "\x1b[0;31mERROR:\x1b[0m "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    return msg.strip()


def cleanup_loop():
    while True:
        time.sleep(600)
        if RETENTION_HOURS <= 0:
            continue
        cutoff = time.time() - RETENTION_HOURS * 3600
        with jobs_lock:
            stale = [j for j in jobs.values() if j.finished and j.finished < cutoff]
            for j in stale:
                jobs.pop(j.id, None)
        for j in stale:
            shutil.rmtree(j.dir, ignore_errors=True)


def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/presets")
def list_presets():
    return jsonify([{"id": k, "label": v["label"]} for k, v in PRESETS.items()])


@app.post("/api/info")
def video_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="URL is required"), 400
    try:
        with yt_dlp.YoutubeDL(base_opts() | {"skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=clean_error(e)), 400
    return jsonify(
        title=info.get("title"),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        uploader=info.get("uploader") or info.get("channel"),
        max_height=max((f.get("height") or 0 for f in info.get("formats") or []), default=None),
    )


@app.post("/api/jobs")
def create_job():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    preset = data.get("preset") or "best"
    if not url:
        return jsonify(error="URL is required"), 400
    if preset not in PRESETS:
        return jsonify(error="Unknown preset"), 400
    job = Job(url, preset)
    job.title = data.get("title")
    job.thumbnail = data.get("thumbnail")
    with jobs_lock:
        jobs[job.id] = job
    executor.submit(run_job, job)
    return jsonify(job.to_dict()), 201


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        items = sorted(jobs.values(), key=lambda j: j.created, reverse=True)
    return jsonify([j.to_dict() for j in items])


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    job = get_job(job_id)
    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
    return jsonify(job.to_dict())


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    job = get_job(job_id)
    if job.status in ("queued", "downloading", "processing"):
        return jsonify(error="Cancel the job before removing it"), 409
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(job.dir, ignore_errors=True)
    return "", 204


@app.get("/api/jobs/<job_id>/file")
def download_file(job_id):
    job = get_job(job_id)
    if job.status != "done" or not job.filename:
        abort(404)
    path = job.dir / job.filename
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=job.filename)


def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    from waitress import serve

    print(f"ytdlp-web on http://{HOST}:{PORT}  (files in {DOWNLOAD_DIR})")
    for tool in ("ffmpeg", "deno"):
        print(f"{tool}: {shutil.which(tool) or 'NOT FOUND - run scripts/setup.sh'}")
    serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
