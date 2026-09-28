# ytdlp-web

A small, mobile-friendly web UI for downloading videos with [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Paste a link, pick a quality, and save the file once it's ready. Downloads run on the
server with live progress, and can be cancelled.

Presets: Best quality, 1080p, 720p, 480p (MP4, H.264 preferred), Audio MP3, Audio M4A.

**Advanced options** (collapsible panel, remembered per device):

- **Playlist:** whole playlist or selected items (`1-5,8`). Unavailable items are skipped and reported.
- **Clip:** download only a start–end range, optionally with a precise (re-encoded) cut.
- **Subtitles:** languages (`en.*,ms`), auto-generated captions, embedded or as `.srt` files.
- **Chapters:** embed them, or split into one file per chapter.
- **SponsorBlock:** mark sponsor/intro/outro/etc. segments as chapters, or cut them out.
- **Other:** thumbnail embedded as cover art (on by default), MP4 or MKV, speed limit.

Jobs that produce several files can be saved one at a time or all at once as a `.zip`.

The release is fully self-contained. Python, the Python packages, ffmpeg and Deno (the
JavaScript runtime yt-dlp needs for YouTube) are all bundled, so the target machine
needs nothing installed.

## Running a release

Requires 64-bit Linux with glibc (Debian, Ubuntu, Fedora, Arch, Raspberry Pi OS 64-bit, …).
Alpine and other musl-based distros are not supported. Check the architecture with
`uname -m`: `x86_64` for most PCs and servers, `aarch64` for ARM boards.

```bash
tar -xzf ytdlp-web-linux-x86_64.tar.gz
cd ytdlp-web-linux-x86_64
./run.sh start
```

Open http://127.0.0.1:8080.

### Managing the app

```bash
./run.sh start        # run in the background; keeps going after you close SSH
./run.sh stop
./run.sh restart      # e.g. after git pull or editing .env
./run.sh status       # running? which URLs to open, free disk space
./run.sh logs         # follow the log (Ctrl+C stops following, not the app)
./run.sh check-port   # what is using the port, is it reachable, firewall hints
./run.sh              # run in the foreground instead (Ctrl+C stops it)
```

The log is `ytdlp-web.log` in the app folder. It doesn't start again by itself after a
reboot; run `./run.sh start`, or use the systemd service below.

### Using it from your phone

By default it only listens on localhost. To reach it from other devices on your network,
set `HOST=0.0.0.0` in `.env` (see [Configuration](#configuration)) and run `./run.sh`, or
for a one-off run:

```bash
HOST=0.0.0.0 ./run.sh
```

Then open `http://<linux-machine-ip>:8080` on your phone. You can add it to your home
screen. The **Paste** button only appears over HTTPS or on localhost, because browsers
block clipboard access on plain http. Long-press the input to paste instead. You can also
prefill a link with `http://<host>:8080/?url=<video-url>`, which works with share shortcuts.

**There is no authentication.** Only expose it on a network you trust, or put it behind
a reverse proxy with auth (e.g. Caddy/nginx basic auth, Tailscale).

## Configuration

Put settings in a `.env` file next to `app.py` so a plain `./run.sh` picks them up:

```bash
cp .env.example .env    # then edit, e.g. HOST=0.0.0.0
```

`.env` is git-ignored, so `git pull` never touches it. Variables set on the command line
(`PORT=9000 ./run.sh`) override the file.

Settings:

| Variable          | Default       | Meaning                                              |
|-------------------|---------------|------------------------------------------------------|
| `HOST`            | `127.0.0.1`   | Interface to bind                                    |
| `PORT`            | `8080`        | Port                                                 |
| `DOWNLOAD_DIR`    | `./downloads` | Where files are stored (one subfolder per job)       |
| `MAX_CONCURRENT`  | `2`           | Downloads running at once; the rest queue            |
| `RETENTION_HOURS` | `24`          | Downloads are deleted this long after they were written, including ones left over from before a restart. `0` keeps them |
| `MIN_FREE_GB`     | `5`           | Free space to keep on the download disk. New downloads are refused, and running ones stopped, below this. `0` disables |

## Run as a service (optional)

Only needed if you want it to start automatically on boot. Use either this or
`./run.sh start`, not both.

Put the extracted folder at `/opt/ytdlp-web`, edit `ytdlp-web.service` (set `User`,
and `HOST` if you want LAN access), then:

```bash
sudo cp ytdlp-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ytdlp-web
journalctl -u ytdlp-web -f
```

## Building releases

From the repo, on macOS or Linux (needs `python3` with pip, `curl`, `unzip`):

```bash
scripts/package.sh            # both architectures
scripts/package.sh x86_64     # just one
```

This writes `dist/ytdlp-web-linux-<arch>.tar.gz`, about 200MB each. Every download is
checked against its published SHA-256. The bundled versions are pinned in
`scripts/versions.env`.

`runtime/` and `dist/` are git-ignored. The bundled binaries exceed GitHub's 100MB
per-file limit, so ship the tarballs (e.g. as GitHub Release assets) rather than
committing them.

## Keeping yt-dlp current

YouTube changes often, and an outdated yt-dlp is the usual cause of failed downloads.

- **Rebuild:** run `scripts/package.sh` again. It always pulls the newest yt-dlp and ffmpeg.
- **Update in place** on a running install, from its folder:
  ```bash
  runtime/python/bin/python3 -m pip install -U "yt-dlp[default]" \
    --target runtime/python/lib/python3.13/site-packages
  sudo systemctl restart ytdlp-web   # if running as a service
  ```
- **Refresh the whole runtime:** on the Linux machine, `scripts/setup.sh` re-downloads
  it into `runtime/`.

## Development (macOS or without the bundle)

Uses system ffmpeg and deno (`brew install ffmpeg deno`):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

On Linux you can instead run `scripts/setup.sh` once and then `./run.sh`.

## Notes

- The download folder shares its disk with the rest of the system, so a full disk can break
  other things too. `MIN_FREE_GB` guards against that, and the page shows the free space.
  If a playlist is stopped by it, the items that already finished are kept.
- Jobs are kept in memory, so the list clears on restart. Run a single process only (the
  bundled waitress server does this).
- Playlist URLs download only the single video (`noplaylist`).
