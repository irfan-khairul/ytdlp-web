"""Small web UI around yt-dlp.

Jobs are kept in memory, so run this as a single process (the bundled
waitress server does that). Each job downloads into its own folder under
DOWNLOAD_DIR, which keeps filenames from colliding and makes serving safe.
"""

import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from yt_dlp.utils import DownloadCancelled

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

CONTAINERS = ("mp4", "mkv")
SUBTITLE_EXTS = {".vtt", ".srt", ".ass", ".ssa", ".ttml", ".srv1", ".srv2", ".srv3", ".json3"}
SPONSOR_CATEGORIES = {
    "sponsor": "Sponsor",
    "selfpromo": "Self-promotion",
    "interaction": "Like/subscribe reminders",
    "intro": "Intro",
    "outro": "Outro/credits",
    "preview": "Preview/recap",
    "music_offtopic": "Non-music section",
    "filler": "Filler/tangent",
}

# Options arrive from the browser and become yt-dlp arguments, so every free
# text field is checked against a strict pattern first.
RE_ITEMS = re.compile(r"^\s*-?\d+(\s*[-:]\s*-?\d*)?(\s*,\s*-?\d+(\s*[-:]\s*-?\d*)?)*\s*$")
RE_LANGS = re.compile(r"^[A-Za-z0-9.*_\-]+(,[A-Za-z0-9.*_\-]+)*$")
RE_TIME = re.compile(r"^\d{1,3}(:\d{1,2}){0,2}(\.\d+)?$")
RE_RATE = re.compile(r"^\d+(\.\d+)?[KkMm]?$")

app = Flask(__name__, static_folder="static")
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
jobs = {}
jobs_lock = threading.Lock()


class OptionError(ValueError):
    pass


def parse_options(raw):
    """Validates the advanced options sent by the browser."""
    raw = raw or {}
    opts = {
        "playlist": bool(raw.get("playlist")),
        "playlist_items": (raw.get("playlist_items") or "").strip(),
        "subs": bool(raw.get("subs")),
        "sub_langs": (raw.get("sub_langs") or "en.*").replace(" ", ""),
        "auto_subs": bool(raw.get("auto_subs")),
        "sub_mode": raw.get("sub_mode") or "embed",
        "embed_chapters": raw.get("embed_chapters", True) is not False,
        "split_chapters": bool(raw.get("split_chapters")),
        "embed_thumbnail": raw.get("embed_thumbnail", True) is not False,
        "sponsorblock": raw.get("sponsorblock") or "off",
        "sponsor_categories": list(raw.get("sponsor_categories") or ["sponsor"]),
        "start": (raw.get("start") or "").strip(),
        "end": (raw.get("end") or "").strip(),
        "precise_cut": bool(raw.get("precise_cut")),
        "container": raw.get("container") or "mp4",
        "rate_limit": (raw.get("rate_limit") or "").strip(),
    }
    if opts["playlist_items"] and not RE_ITEMS.match(opts["playlist_items"]):
        raise OptionError("Playlist items should look like 1-5,8,10")
    if opts["subs"] and not RE_LANGS.match(opts["sub_langs"]):
        raise OptionError("Subtitle languages should look like en,ms or en.*")
    if opts["sub_mode"] not in ("embed", "file"):
        raise OptionError("Unknown subtitle mode")
    if opts["sponsorblock"] not in ("off", "mark", "remove"):
        raise OptionError("Unknown SponsorBlock mode")
    if opts["sponsorblock"] != "off":
        if not opts["sponsor_categories"] or any(c not in SPONSOR_CATEGORIES for c in opts["sponsor_categories"]):
            raise OptionError("Pick at least one valid SponsorBlock category")
    for key in ("start", "end"):
        if opts[key] and not RE_TIME.match(opts[key]):
            raise OptionError(f"{key.title()} time should look like 90, 1:30 or 1:02:03")
    if opts["container"] not in CONTAINERS:
        raise OptionError("Unknown container")
    if opts["rate_limit"] and not RE_RATE.match(opts["rate_limit"]):
        raise OptionError("Speed limit should look like 500K or 5M")
    if opts["start"] or opts["end"]:
        # yt-dlp doesn't shift subtitles, chapters or SponsorBlock segments to
        # match a clip, so they'd be out of sync. Drop them instead.
        opts.update(subs=False, embed_chapters=False, split_chapters=False, sponsorblock="off")
    else:
        opts["precise_cut"] = False
    return opts


def option_tags(preset, o):
    """Short labels shown on a job card for the options that are switched on."""
    tags = []
    if o["playlist"]:
        tags.append("Playlist" + (f" {o['playlist_items']}" if o["playlist_items"] else ""))
    if o["start"] or o["end"]:
        tags.append(f"Clip {o['start'] or '0'}–{o['end'] or 'end'}" + (" (precise)" if o["precise_cut"] else ""))
    if o["subs"]:
        tags.append(f"Subs {o['sub_langs']}")
    if o["split_chapters"]:
        tags.append("Split chapters")
    if o["sponsorblock"] != "off":
        tags.append(f"SponsorBlock {o['sponsorblock']}")
    if "audio" not in PRESETS[preset] and o["container"] != "mp4":
        tags.append(o["container"].upper())
    if o["rate_limit"]:
        tags.append(f"≤{o['rate_limit']}/s")
    return tags


def build_args(preset_id, o):
    """Turns a preset and options into yt-dlp command-line arguments.

    Letting yt-dlp's own parser handle these keeps postprocessor ordering
    (SponsorBlock, subtitles, chapters, thumbnails) the same as the CLI.
    """
    preset = PRESETS[preset_id]
    audio = preset.get("audio")
    args = ["-f", preset["format"], "--embed-metadata", "--windows-filenames"]

    if audio:
        args += ["-x", "--audio-format", audio, "--audio-quality", "0"]
    else:
        args += ["--merge-output-format", o["container"]]

    if o["playlist"]:
        args += ["--yes-playlist", "--ignore-errors"]
        if o["playlist_items"]:
            args += ["--playlist-items", o["playlist_items"].replace(" ", "")]
    else:
        args.append("--no-playlist")

    args.append("--embed-chapters" if o["embed_chapters"] else "--no-embed-chapters")
    if o["split_chapters"]:
        args.append("--split-chapters")

    if o["subs"]:
        args += ["--write-subs", "--sub-langs", o["sub_langs"]]
        if o["auto_subs"]:
            args.append("--write-auto-subs")
        # Audio files can't carry subtitles, so those always get .srt files.
        if o["sub_mode"] == "embed" and not audio:
            args.append("--embed-subs")
        else:
            args += ["--convert-subs", "srt"]

    if o["embed_thumbnail"]:
        args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]

    if o["sponsorblock"] != "off":
        args += [f"--sponsorblock-{o['sponsorblock']}", ",".join(o["sponsor_categories"])]

    if o["start"] or o["end"]:
        args += ["--download-sections", f"*{o['start'] or '0'}-{o['end'] or 'inf'}"]
        if o["precise_cut"]:
            args.append("--force-keyframes-at-cuts")

    if o["rate_limit"]:
        args += ["--limit-rate", o["rate_limit"]]

    return args


class Job:
    def __init__(self, url, preset, options):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.preset = preset
        self.options = options
        self.status = "queued"  # queued, downloading, processing, done, error, cancelled
        self.title = None
        self.thumbnail = None
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.stage = None
        self.item_index = None
        self.item_count = None
        self.error = None
        self.errors = []
        self.files = []
        self.created = time.time()
        self.finished = None
        self.cancel_requested = False

    @property
    def dir(self):
        return DOWNLOAD_DIR / self.id

    @property
    def zip_path(self):
        return DOWNLOAD_DIR / f"{self.id}.zip"

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "preset": self.preset,
            "preset_label": PRESETS[self.preset]["label"],
            "tags": option_tags(self.preset, self.options),
            "status": self.status,
            "title": self.title,
            "thumbnail": self.thumbnail,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "eta": self.eta,
            "stage": self.stage,
            "item_index": self.item_index,
            "item_count": self.item_count,
            "error": self.error,
            "item_errors": self.errors[:20] if self.status == "done" else [],
            "files": self.files,
            "total_size": sum(f["size"] for f in self.files),
            "created": self.created,
        }


class JobLogger:
    """Collects yt-dlp errors so skipped playlist items can be reported."""

    def __init__(self, job):
        self.job = job

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.job.errors.append(clean_error(msg))


def base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
    }


def collect_files(job):
    files = []
    for p in sorted(job.dir.rglob("*")):
        if not p.is_file() or p.suffix in (".part", ".ytdl", ".temp") or ".part-Frag" in p.name:
            continue
        files.append({"name": p.relative_to(job.dir).as_posix(), "size": p.stat().st_size})
    return files


def run_job(job):
    if job.cancel_requested:
        job.status = "cancelled"
        return

    o = job.options
    job.status = "downloading"
    job.dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d):
        if job.cancel_requested:
            raise DownloadCancelled("Cancelled by user")
        info = d.get("info_dict") or {}
        if o["playlist"]:
            job.title = job.title or info.get("playlist_title") or info.get("playlist")
            job.item_index = info.get("playlist_autonumber") or info.get("playlist_index")
            job.item_count = info.get("n_entries") or info.get("playlist_count")
        else:
            job.title = job.title or info.get("title")
        job.thumbnail = job.thumbnail or info.get("thumbnail")
        if job.status == "processing":
            job.status = "downloading"  # next playlist item
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
            job.percent = 100.0
            job.speed = job.eta = None

    def postprocessor_hook(d):
        if job.cancel_requested:
            raise DownloadCancelled("Cancelled by user")
        if d["status"] == "started":
            job.status = "processing"
            job.stage = d.get("postprocessor")

    name = "%(title).150B [%(id)s].%(ext)s"
    if o["playlist"]:
        name = "%(playlist_index)03d - " + name
    ydl_opts = yt_dlp.parse_options(build_args(job.preset, o)).ydl_opts
    ydl_opts.update(base_opts())
    ydl_opts.update({
        "noplaylist": not o["playlist"],
        "logger": JobLogger(job),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "paths": {"home": str(job.dir)},
        "outtmpl": ydl_opts["outtmpl"] | {
            "default": name,
            "chapter": "%(title).120B [%(id)s]/%(section_number)02d - %(section_title).100B.%(ext)s",
        },
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(job.url, download=True)
        if o["subs"] and o["sub_mode"] == "embed" and "audio" not in PRESETS[job.preset]:
            # yt-dlp can leave the subtitle files behind (e.g. with SponsorBlock
            # cuts) even though they're already inside the video.
            for p in job.dir.rglob("*"):
                if p.suffix in SUBTITLE_EXTS:
                    p.unlink(missing_ok=True)
        job.files = collect_files(job)
        if not job.files:
            raise RuntimeError(job.errors[-1] if job.errors else "Download finished but no output file was found")
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


def remove_job_files(job):
    shutil.rmtree(job.dir, ignore_errors=True)
    job.zip_path.unlink(missing_ok=True)


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
            remove_job_files(j)


def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


def first_thumbnail(entries):
    for e in entries or []:
        thumbs = (e or {}).get("thumbnails") or []
        if thumbs:
            return thumbs[-1].get("url")
    return None


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/presets")
def list_presets():
    return jsonify(
        presets=[{"id": k, "label": v["label"], "audio": "audio" in v} for k, v in PRESETS.items()],
        sponsor_categories=[{"id": k, "label": v} for k, v in SPONSOR_CATEGORIES.items()],
    )


@app.post("/api/info")
def video_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="URL is required"), 400
    flat = base_opts() | {"skip_download": True, "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(flat) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=clean_error(e)), 400

    if info.get("_type") == "playlist":
        entries = list(info.get("entries") or [])
        return jsonify(
            kind="playlist",
            title=info.get("title"),
            thumbnail=first_thumbnail(entries) or (info.get("thumbnails") or [{}])[-1].get("url"),
            uploader=info.get("uploader") or info.get("channel"),
            playlist={"title": info.get("title"), "count": info.get("playlist_count") or len(entries)},
        )

    playlist = None
    if "list=" in url:
        # A video opened from a playlist: also report the playlist so the UI
        # can offer to grab all of it.
        try:
            with yt_dlp.YoutubeDL(flat | {"noplaylist": False, "playlistend": 1}) as ydl:
                pl = ydl.extract_info(url, download=False)
            if pl.get("_type") == "playlist":
                playlist = {"title": pl.get("title"), "count": pl.get("playlist_count")}
        except Exception:
            pass

    subs = sorted(k for k in (info.get("subtitles") or {}) if k != "live_chat")
    return jsonify(
        kind="video",
        title=info.get("title"),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        uploader=info.get("uploader") or info.get("channel"),
        max_height=max((f.get("height") or 0 for f in info.get("formats") or []), default=None),
        chapters=len(info.get("chapters") or []),
        subtitles=subs,
        auto_subtitles=bool(info.get("automatic_captions")),
        playlist=playlist,
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
    try:
        options = parse_options(data.get("options"))
    except OptionError as e:
        return jsonify(error=str(e)), 400
    job = Job(url, preset, options)
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
    remove_job_files(job)
    return "", 204


@app.get("/api/jobs/<job_id>/files/<path:name>")
def download_one(job_id, name):
    job = get_job(job_id)
    if job.status != "done":
        abort(404)
    # send_from_directory rejects paths that escape the job folder.
    return send_from_directory(job.dir, name, as_attachment=True, download_name=Path(name).name)


@app.get("/api/jobs/<job_id>/zip")
def download_zip(job_id):
    job = get_job(job_id)
    if job.status != "done" or not job.files:
        abort(404)
    if not job.zip_path.exists():
        # Media is already compressed, so store rather than deflate. Build to
        # a temp name first so a concurrent request never sees half a zip.
        tmp = job.zip_path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            for f in job.files:
                zf.write(job.dir / f["name"], f["name"])
        os.replace(tmp, job.zip_path)
    name = re.sub(r'[\\/:*?"<>|]+', "_", job.title or job.id).strip() or job.id
    return send_file(job.zip_path, as_attachment=True, download_name=f"{name}.zip")


@app.get("/api/jobs/<job_id>/file")
def download_file(job_id):
    """The single output file, or a zip when there are several."""
    job = get_job(job_id)
    if job.status != "done" or not job.files:
        abort(404)
    if len(job.files) > 1:
        return download_zip(job_id)
    return download_one(job_id, job.files[0]["name"])


def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    from waitress import serve

    print(f"ytdlp-web on http://{HOST}:{PORT}  (files in {DOWNLOAD_DIR})")
    for tool in ("ffmpeg", "deno"):
        print(f"{tool}: {shutil.which(tool) or 'NOT FOUND - run scripts/setup.sh'}")
    serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
