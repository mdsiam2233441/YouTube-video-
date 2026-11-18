# -*- coding: utf-8 -*-
"""
YOUTUBE VIDEO DOWNLOADER BOT  (Telegram - Pyrogram/MTProto + yt-dlp + ffmpeg)

Send a YouTube link -> pick video quality -> pick audio track(s) -> pick subtitles
-> live download/upload progress -> the file arrives in Telegram (up to ~1.9 GB per
file, bigger files are split automatically).

ENV VARS : API_ID, API_HASH (my.telegram.org), BOT_TOKEN, ADMIN_IDS, DATA_DIR
Use it only for videos you own, that are Creative Commons / public domain, or that
you have the owner's permission to download.
"""
import asyncio
import html
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.request
import uuid
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadCancelled
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb

# ============================== CONFIG ==============================
API_ID = int(os.getenv("API_ID", "24017397"))
API_HASH = os.getenv("API_HASH", "0f2fe9bc1733e762a3ceef7c1372ce40")
BOT_TOKEN = os.getenv("BOT_TOKEN", "9105774355:AAHwwbRPr6i56ue0Gxs5XGNNoPxDVzkl9j0")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "1686274364").replace(" ", "1686274364").split(",") if x}
DATA = Path(os.getenv("DATA_DIR", "data"))
TMP = Path(os.getenv("TMP_DIR", str(Path(tempfile.gettempdir()) / "ytdl")))
THUMBS = DATA / "thumbs"
COOKIES = DATA / "cookies.txt"
MAX_JOBS = int(os.getenv("MAX_JOBS", "2"))
UPLOAD_LIMIT = 1950 * 1024 * 1024            # Telegram bot upload limit is 2 GB
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
for _p in (DATA, TMP, THUMBS):
    _p.mkdir(parents=True, exist_ok=True)
# =====================================================================

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("ytbot")
app = Client("ytbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=str(DATA),
             parse_mode=enums.ParseMode.HTML)

# ============================== DATABASE ==============================
db = sqlite3.connect(DATA / "bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, name TEXT, ext TEXT DEFAULT 'MKV', upload TEXT DEFAULT 'video',
  tag TEXT DEFAULT '', thumb INTEGER DEFAULT 0, stream INTEGER DEFAULT 1, style TEXT DEFAULT 'dots', caption INTEGER DEFAULT 1,
  premium_until INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, joined INTEGER, last_active INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS downloads(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ts INTEGER, title TEXT, size INTEGER, ok INTEGER);
""")
db.commit()

DEFAULTS = {
    "free_daily": "5", "free_max_height": "720", "free_max_mb": "700",
    "update_channel": "", "default_tag": "", "tag_lock": "0",
    "about_text": "A fast YouTube video downloader. Choose quality, audio and subtitles, then get the file right inside Telegram.",
    "plan_contact": "Contact the owner to buy or extend Premium.",
}


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def one(sql, args=()):
    return db.execute(sql, args).fetchone()


def count(sql, args=()):
    return one(sql, args)[0]


def S(k):
    r = one("SELECT value FROM settings WHERE key=?", (k,))
    return r[0] if r else DEFAULTS.get(k, "")


def Si(k):
    try:
        return int(float(S(k)))
    except ValueError:
        return 0


def setS(k, v):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def now():
    return int(time.time())


def touch(tu):
    u = one("SELECT * FROM users WHERE uid=?", (tu.id,))
    name = (tu.first_name or "") + (" " + tu.last_name if getattr(tu, "last_name", None) else "")
    if not u:
        run("INSERT INTO users(uid,name,joined,last_active) VALUES(?,?,?,?)", (tu.id, name.strip(), now(), now()))
    else:
        run("UPDATE users SET name=?, last_active=? WHERE uid=?", (name.strip(), now(), tu.id))
    return one("SELECT * FROM users WHERE uid=?", (tu.id,))


def get_user(uid):
    return one("SELECT * FROM users WHERE uid=?", (uid,))


def is_premium(u):
    return u["premium_until"] > now() or u["uid"] in ADMIN_IDS


def eff_tag(u):
    if S("tag_lock") == "1" and not is_premium(u):
        return S("default_tag")
    return u["tag"] or S("default_tag")


# ============================== HELPERS ==============================
def esc(s):
    return html.escape(str(s if s is not None else ""))


def hsize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def htime(s):
    s = int(s or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def bar(p, n=20):
    f = int(n * max(0, min(100, p)) / 100)
    return "■" * f + "□" * (n - f)


def B(text, data=None, url=None):
    return Btn(text, callback_data=data, url=url)


def chunk_rows(btns, cols):
    return [btns[i:i + cols] for i in range(0, len(btns), cols)]


LANGS = {"en": "English", "bn": "Bengali", "hi": "Hindi", "ta": "Tamil", "te": "Telugu", "ur": "Urdu", "ar": "Arabic",
         "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "ru": "Russian", "ja": "Japanese",
         "ko": "Korean", "id": "Indonesian", "tr": "Turkish", "it": "Italian", "zh": "Chinese", "mr": "Marathi"}


def lang_name(code):
    if not code or code == "und":
        return "Original"
    base = code.split("-")[0].lower()
    return LANGS.get(base, code.upper())


def acodec_name(c):
    c = (c or "").lower()
    for k, v in (("mp4a", "AAC"), ("opus", "OPUS"), ("ac-3", "AC3"), ("ec-3", "EAC3"), ("vorbis", "VORBIS"), ("flac", "FLAC")):
        if c.startswith(k):
            return v
    return c.upper() or "AUDIO"


def vcodec_name(c):
    c = (c or "").lower()
    for k, v in (("avc", "H.264"), ("vp09", "VP9"), ("vp9", "VP9"), ("av01", "AV1"), ("hev", "H.265"), ("hvc", "H.265")):
        if c.startswith(k):
            return v
    return c.upper() or "VIDEO"


# ============================== YT-DLP LAYER ==============================
VIDEO_EXT = {".mkv", ".mp4", ".webm", ".mov", ".avi"}
URL_RE = re.compile(r"https?://\S+")


def ydl_base():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30}
    if COOKIES.exists():
        o["cookiefile"] = str(COOKIES)
    return o


def extract(url):
    with yt_dlp.YoutubeDL(ydl_base()) as y:
        return y.extract_info(url, download=False)


def video_tracks(info, ext):
    best = {}
    for f in info.get("formats") or []:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        if (f.get("dynamic_range") or "SDR") != "SDR":
            continue
        avc = (f.get("vcodec") or "").startswith("avc")
        score = (1 if (avc and ext == "MP4") else 0, f.get("tbr") or 0)
        h = f["height"]
        if h not in best or score > best[h][0]:
            best[h] = (score, f)
    return [best[h][1] for h in sorted(best, reverse=True)][:8]


def audio_tracks(info):
    best = {}
    for f in info.get("formats") or []:
        if f.get("acodec") in (None, "none") or f.get("vcodec") not in (None, "none"):
            continue
        lang = f.get("language") or "und"
        score = f.get("abr") or f.get("tbr") or 0
        if lang not in best or score > best[lang][0]:
            best[lang] = (score, f)
    out = [v[1] for v in best.values()]
    out.sort(key=lambda f: (-(f.get("language_preference") or 0), f.get("language") or "zz"))
    return out[:8]


def sub_tracks(info):
    subs = info.get("subtitles") or {}
    out = []
    for code, v in subs.items():
        if code == "live_chat":
            continue
        out.append((code, (v[0].get("name") if v and v[0].get("name") else code)))
    return out[:12]


def vlabel(f):
    w = f.get("width") or int(f["height"] * 16 / 9)
    s = f"{w}x{f['height']} • {int(f.get('tbr') or 0)}K"
    size = f.get("filesize") or f.get("filesize_approx")
    return s + (f" • {hsize(size)}" if size else "")


def alabel(f):
    return (f"{lang_name(f.get('language'))} • {int(f.get('abr') or f.get('tbr') or 0)}Kbps • "
            f"{acodec_name(f.get('acodec'))} • {int(f.get('audio_channels') or 2)}CH")


def clean_title(t):
    t = re.sub(r"[^\w\s\-\(\)\[\]']", " ", t or "video", flags=re.UNICODE)
    return " ".join(t.split())[:80] or "video"


def make_name(job):
    vf = job["vsel"]
    words = clean_title(job["info"].get("title")).split()
    base = ".".join(words) if job["style"] == "dots" else " ".join(words)
    sep = "." if job["style"] == "dots" else " "
    parts = [f"{vf['height']}p"]
    if job["asel"]:
        parts += [acodec_name(job["asel"][0].get("acodec")), lang_name(job["asel"][0].get("language"))]
    parts.append(vcodec_name(vf.get("vcodec")))
    if job["tag"]:
        parts.append(re.sub(r"\s+", "", job["tag"]))
    name = base + sep + sep.join(parts)
    return name[:170]


def build_format(job):
    ids = [job["vsel"]["format_id"]] + [a["format_id"] for a in job["asel"]]
    return "+".join(ids)


def download(job, d):
    st = job["st"]

    def hook(dct):
        if job["cancel"].is_set():
            raise DownloadCancelled()
        fid = (dct.get("info_dict") or {}).get("format_id") or dct.get("filename", "x")
        s = st["streams"].setdefault(fid, {"done": 0, "total": 0})
        s["done"] = dct.get("downloaded_bytes") or 0
        s["total"] = dct.get("total_bytes") or dct.get("total_bytes_estimate") or s["total"]
        st["speed"] = dct.get("speed") or 0
        st["eta"] = dct.get("eta") or 0
        if dct.get("status") == "finished" and s["total"]:
            s["done"] = s["total"]

    o = ydl_base()
    o.update({"format": build_format(job), "outtmpl": str(d / "video.%(ext)s"), "merge_output_format": job["ext"].lower(),
              "allow_multiple_audio_streams": True, "progress_hooks": [hook], "concurrent_fragment_downloads": 4,
              "retries": 5, "fragment_retries": 5, "overwrites": True})
    if job["subs"]:
        o.update({"writesubtitles": True, "subtitleslangs": job["subs"],
                  "postprocessors": [{"key": "FFmpegEmbedSubtitle"}]})
    with yt_dlp.YoutubeDL(o) as y:
        y.download([job["url"]])
    files = [p for p in d.iterdir() if p.suffix.lower() in VIDEO_EXT]
    if not files:
        raise RuntimeError("Download finished but no video file was produced.")
    job["file"] = max(files, key=lambda p: p.stat().st_size)


async def ffmpeg(*args):
    p = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", *[str(a) for a in args],
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0:
        log.warning("ffmpeg failed: %s", err.decode(errors="ignore")[:300])
    return p.returncode == 0


def fetch_url(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read(10 * 1024 * 1024))


async def to_thumb(src, out):
    return await ffmpeg("-y", "-i", src, "-vf", "scale='min(320,iw)':-2", "-q:v", "5", out)


# ============================== JOBS ==============================
JOBS = {}
STATE = {}
SEM = asyncio.Semaphore(MAX_JOBS)


def new_job(uid, url, info, u):
    for k in [k for k, j in JOBS.items() if now() - j["created"] > 3600 and j["stage"] == "select"]:
        JOBS.pop(k, None)
    job = {"id": uuid.uuid4().hex[:6], "uid": uid, "url": url, "info": info, "created": now(), "stage": "select",
           "ext": u["ext"], "upload": u["upload"], "style": u["style"], "caption": u["caption"], "stream": u["stream"],
           "tag": eff_tag(u), "thumb_on": u["thumb"], "premium": is_premium(u),
           "vsel": None, "asel": [], "subs": [], "cancel": threading.Event(), "done": False,
           "st": {"streams": {}, "speed": 0, "eta": 0, "t0": time.time()}, "up": {"done": 0, "total": 0, "t0": 0},
           "title": clean_title(info.get("title"))}
    v = video_tracks(info, u["ext"])
    if not job["premium"]:
        cap = Si("free_max_height")
        v = [f for f in v if f["height"] <= cap] or v[-1:]
    job["vids"] = v
    job["auds"] = audio_tracks(info)
    job["sublist"] = sub_tracks(info)
    job["steps"] = 2 + (1 if job["sublist"] else 0)
    JOBS[job["id"]] = job
    return job


def step1(job):
    n = len(job["vids"])
    text = (f"✅ <b>File Name:</b> <code>{esc(job['title'])}</code>\n\n<b>Step 1 Of {job['steps']}: Select Video Track</b>\n"
            f"<b>Video:</b> {n} | <b>Audio:</b> {len(job['auds'])} | <b>Subtitle:</b> {len(job['sublist'])}")
    rows = [[B("🎬 Select Video Track", "noop")]]
    btns = [B(vlabel(f), f"v:{job['id']}:{f['format_id']}") for f in job["vids"]]
    rows += chunk_rows(btns[:4], 1) + chunk_rows(btns[4:], 2)
    rows.append([B("⚡ Auto Best Select", f"vb:{job['id']}")])
    rows.append([B("🚫 Cancel", f"x:{job['id']}")])
    return text, Kb(rows)


def sel_summary(job):
    a = ", ".join(lang_name(x.get("language")) for x in job["asel"]) or "none selected"
    s = ", ".join(job["subs"]) or "none selected"
    return a, s


def step2(job):
    a, s = sel_summary(job)
    text = (f"🎵 <b>Step 2 Of {job['steps']}: Select Audio Tracks</b>\n\n<i>Select One Or More, Then Tap Done.</i>\n\n"
            f"<b>Video:</b> <code>{vlabel(job['vsel'])}</code> |\n<b>Audio:</b> <code>{esc(a)}</code> | <b>Subtitle:</b> <code>{esc(s)}</code>")
    rows = [[B("🔊 Select Audio (Multi)", "noop")]]
    for i, f in enumerate(job["auds"]):
        mark = "✅ " if f in job["asel"] else ""
        rows.append([B(mark + alabel(f), f"a:{job['id']}:{i}")])
    rows.append([B("✅ All Audio", f"aall:{job['id']}"), B("⏭ Skip Audio", f"askip:{job['id']}")])
    if job["asel"]:
        rows.append([B("✔️ Done", f"adone:{job['id']}")])
    rows.append([B("🚫 Cancel", f"x:{job['id']}")])
    return text, Kb(rows)


def step3(job):
    a, s = sel_summary(job)
    text = (f"💬 <b>Step 3 Of 3: Select Subtitles</b>\n\n<i>Select One Or More, Then Tap Done.</i>\n\n"
            f"<b>Video:</b> <code>{vlabel(job['vsel'])}</code> |\n<b>Audio:</b> <code>{esc(a)}</code> | <b>Subtitle:</b> <code>{esc(s)}</code>")
    rows = [[B("💬 Select Subtitles (Multi)", "noop")]]
    btns = []
    for i, (code, name) in enumerate(job["sublist"]):
        mark = "✅ " if code in job["subs"] else ""
        btns.append(B(f"{mark}{name}", f"s:{job['id']}:{i}"))
    rows += chunk_rows(btns, 2)
    rows.append([B("✅ All Subtitles", f"sall:{job['id']}"), B("⏭ Skip Subtitles", f"sskip:{job['id']}")])
    if job["subs"]:
        rows.append([B("✔️ Done", f"sdone:{job['id']}")])
    rows.append([B("🚫 Cancel", f"x:{job['id']}")])
    return text, Kb(rows)


def cancel_kb(job):
    return Kb([[B("🚫 Cancel", f"x:{job['id']}")]])


def render(job):
    st, up = job["st"], job["up"]
    qual = f"{job['vsel']['width'] or ''}x{job['vsel']['height']} • {int(job['vsel'].get('tbr') or 0)}Kbps" if job["vsel"] else "-"
    if job["stage"] == "download":
        done = sum(s["done"] for s in st["streams"].values())
        total = sum(s["total"] for s in st["streams"].values())
        pct = done * 100 / total if total else 0
        lines = ["📥 <b>Downloading File</b>", "", "<b>Selected Engine:</b> <code>Auto</code>", "<b>Using Engine:</b> <code>yt-dlp</code>", "",
                 f"[{bar(pct)}] {pct:.1f}%", "", f"<b>Speed:</b> <code>{hsize(st['speed'])}/s</code>",
                 f"<b>ETA:</b> <code>{htime(st['eta'])}</code>", f"<b>Elapsed:</b> <code>{htime(time.time() - st['t0'])}</code>", ""]
        for i, s in enumerate(st["streams"].values()):
            p = s["done"] * 100 / s["total"] if s["total"] else 0
            name = "🎬 Video" if i == 0 else ("🔊 Audio" if len(st["streams"]) <= 2 else f"🔊 Audio {i}")
            lines.append(f"{name}: [{bar(p, 10)}] {p:.1f}%\n<code>{hsize(s['done'])}/{hsize(s['total'])}</code>")
        lines += ["", f"<b>File:</b> <code>{esc(job['title'][:60])}</code>", f"<b>Quality:</b> <code>{qual}</code>"]
        return "\n".join(lines)
    if job["stage"] == "process":
        return (f"✅ <b>Download Complete</b>\n\n<b>File:</b> <code>{esc(job['title'][:60])}</code>\n\n"
                f"<i>Processing Your File, Please Wait.</i> ⚙️")
    pct = up["done"] * 100 / up["total"] if up["total"] else 0
    el = max(time.time() - up["t0"], 0.001)
    speed = up["done"] / el
    eta = (up["total"] - up["done"]) / speed if speed else 0
    part = f" (Part {job['part']}/{job['parts']})" if job.get("parts", 1) > 1 else ""
    return (f"📤 <b>Uploading {'Video' if job['upload'] == 'video' else 'File'}</b>{part}\n[{bar(pct)}]\n"
            f"<b>Progress:</b> {pct:.2f}%\n<b>Done:</b> <code>{hsize(up['done'])}</code>\n<b>Total:</b> <code>{hsize(up['total'])}</code>\n"
            f"<b>Speed:</b> <code>{hsize(speed)}/s</code>\n<b>ETA:</b> <code>{htime(eta)}</code>")


async def updater(job):
    last = ""
    while not job["done"]:
        await asyncio.sleep(3)
        if job["done"]:
            break
        txt = render(job)
        if txt == last:
            continue
        try:
            await job["msg"].edit_text(txt, reply_markup=cancel_kb(job))
            last = txt
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except RPCError:
            pass


async def up_progress(cur, total, c, job):
    if job["cancel"].is_set():
        c.stop_transmission()
    job["up"]["done"], job["up"]["total"] = cur, total


async def send_one(c, job, path, thumb, caption, dur, w, h):
    kw = dict(caption=caption, progress=up_progress, progress_args=(c, job))
    if job["upload"] == "video":
        kw.update(duration=int(dur or 0), width=int(w or 0), height=int(h or 0), supports_streaming=bool(job["stream"]))
        fn = c.send_video
        args = (job["uid"], str(path))
    else:
        kw["force_document"] = True
        fn = c.send_document
        args = (job["uid"], str(path))
    if thumb:
        kw["thumb"] = thumb
    try:
        return await fn(*args, **kw)
    except TypeError:                      # library without 'thumb' / 'force_document' argument
        kw.pop("thumb", None)
        kw.pop("force_document", None)
        return await fn(*args, **kw)


async def run_job(c, job):
    uid, msg = job["uid"], job["msg"]
    d = TMP / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    task = None
    try:
        async with SEM:
            job["stage"] = "download"
            job["st"]["t0"] = time.time()
            task = asyncio.create_task(updater(job))
            await asyncio.to_thread(download, job, d)
            job["stage"] = "process"
            await msg.edit_text(render(job), reply_markup=cancel_kb(job))
            name = make_name(job)
            src = job["file"]
            want = "." + job["ext"].lower()
            final = d / f"{name}{want}"
            if src.suffix.lower() != want:
                if not await ffmpeg("-y", "-i", src, "-map", "0", "-c", "copy", final):
                    final = d / f"{name}{src.suffix.lower()}"
                    src.rename(final)
                else:
                    src.unlink(missing_ok=True)
            else:
                src.rename(final)
            size = final.stat().st_size
            if not job["premium"] and size > Si("free_max_mb") * 1024 * 1024:
                raise RuntimeError(f"File is {hsize(size)} - free plan limit is {Si('free_max_mb')} MB. Pick a lower quality or get Premium.")
            thumb = None
            custom = THUMBS / f"{uid}.jpg"
            if job["thumb_on"] and custom.exists():
                thumb = str(custom)
            elif job["info"].get("thumbnail"):
                raw, out = d / "thumb_raw", d / "thumb.jpg"
                try:
                    await asyncio.to_thread(fetch_url, job["info"]["thumbnail"], raw)
                    if await to_thumb(raw, out):
                        thumb = str(out)
                except Exception as e:
                    log.warning("thumb failed: %s", e)
            dur = job["info"].get("duration") or 0
            files = [final]
            if size > UPLOAD_LIMIT:
                parts = math.ceil(size / (UPLOAD_LIMIT * 0.92))
                seg = max(60, int(dur / parts) + 1)
                pat = d / f"{name}.part%02d{final.suffix}"
                if not await ffmpeg("-y", "-i", final, "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", seg,
                                    "-reset_timestamps", "1", pat):
                    raise RuntimeError("Could not split the big file.")
                final.unlink(missing_ok=True)
                files = sorted(d.glob(f"{name}.part*{final.suffix}"))
                dur = seg
            job["stage"] = "upload"
            job["parts"] = len(files)
            for i, f in enumerate(files, 1):
                job["part"] = i
                job["up"] = {"done": 0, "total": f.stat().st_size, "t0": time.time()}
                cap = f"<code>{esc(f.name)}</code>" if job["caption"] else ""
                sent = await send_one(c, job, f, thumb, cap, dur, job["vsel"].get("width"), job["vsel"].get("height"))
                if job["cancel"].is_set() or sent is None:
                    raise DownloadCancelled()
            job["done"] = True
            task.cancel()
            await msg.edit_text("✅ <b>Upload Complete</b>")
            run("INSERT INTO downloads(uid,ts,title,size,ok) VALUES(?,?,?,?,1)", (uid, now(), job["title"], size))
    except Exception as e:
        job["done"] = True
        if task:
            task.cancel()
        if job["cancel"].is_set() or isinstance(e, DownloadCancelled):
            txt = "🚫 <b>Cancelled.</b>"
        else:
            log.exception("job failed")
            msg_e = str(e)
            if "Sign in" in msg_e or "bot" in msg_e.lower():
                msg_e += "\n\nYouTube asked for a sign-in. The admin must upload a cookies file (/admin → Cookies)."
            txt = f"❌ <b>Failed</b>\n<code>{esc(msg_e[:600])}</code>"
        try:
            await msg.edit_text(txt)
        except RPCError:
            pass
        run("INSERT INTO downloads(uid,ts,title,size,ok) VALUES(?,?,?,?,0)", (uid, now(), job["title"], 0))
    finally:
        shutil.rmtree(d, ignore_errors=True)
        JOBS.pop(job["id"], None)


async def begin(c, q, job):
    job["stage"] = "queued"
    job["msg"] = q.message
    if SEM.locked():
        await q.message.edit_text("⏳ <b>Queued…</b> Your download starts as soon as a slot is free.", reply_markup=cancel_kb(job))
    else:
        await q.message.edit_text("⏳ <b>Starting…</b>", reply_markup=cancel_kb(job))
    asyncio.create_task(run_job(c, job))


def best_audio(job):
    return job["auds"][:1]


# ============================== SCREENS ==============================
def channel_url():
    ch = S("update_channel").strip()
    if ch.startswith("@"):
        return "https://t.me/" + ch[1:]
    return ch if ch.startswith("http") else ""


def main_text(name):
    return (f"👋 <b>Hello {esc(name)}!</b>\n\n🔐 Welcome To The <b>YouTube Video Downloader Bot</b> 🎬\n\n"
            f"⚡ <i>This Bot Downloads YouTube Videos In Your Chosen Quality, Audio And Subtitles</i> 🍿\n\n"
            f"✨ <i>Just Send A Valid YouTube Link And The Magic Begins</i> ✨\n\n"
            f"⚙️ Use /settings To Set Up Your Settings 🌐\n\n💡 Send /help For Full Usage Guide 📖")


def main_kb():
    rows = [[B("❓ Help", "menu:help"), B("ℹ️ About", "menu:about")], [B("💎 Plan", "menu:plan")],
            [B("🧩 Advanced Settings", "menu:adv")], [B("⚙️ Settings", "menu:set")]]
    if channel_url():
        rows.append([B("📢 Join Update Channel", url=channel_url())])
    rows.append([B("🚫 Close", "close")])
    return Kb(rows)


HELP = ("📖 <b>Help &amp; Usage Guide</b> 📘\n\n🎯 <b>What This Bot Can Do:</b>\n• Download YouTube Videos 🎬\n• Pick Quality, Audio And Subtitles 🌐\n\n"
        "⚙️ <b>How To Use:</b>\n1️⃣ Send A YouTube Video Link 🔗\n2️⃣ Choose Your Preferred Video Quality 🎞\n"
        "3️⃣ Select Audio Track(s) If Available 🎵\n4️⃣ Select Subtitle Track(s) If Available 💬\n5️⃣ Download Starts Instantly ⚡\n"
        "6️⃣ File Is Delivered Directly To You 📥\n\n🚀 <b>Bot Features:</b>\n• Multiple Video Quality Options 🎬\n• Multi-Audio Track Selection 🎵\n"
        "• Subtitle Track Selection 💬\n• Real-Time Download Progress 📊\n• Automatic Thumbnail Generation 🖼\n"
        "• Automatic Cleanup After Delivery 🧹\n• Big Files Are Split Automatically ✂️\n• File Extension And Thumbnail Settings Via /settings ⚙️\n\n"
        "⚠️ <b>Important Notes:</b>\n• Only YouTube Links Work ❗\n• Processing Time Depends On File Size And Server Speed ⏳\n"
        "• Download Only Videos You Own Or Have Permission To Use ⚖️")


def plan_text(u):
    if is_premium(u):
        left = max(u["premium_until"] - now(), 0)
        till = "Unlimited (admin)" if u["uid"] in ADMIN_IDS and not u["premium_until"] else \
            dt.datetime.fromtimestamp(u["premium_until"], dt.timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        tl = "∞" if u["uid"] in ADMIN_IDS and not u["premium_until"] else f"{left // 86400} Days {left % 86400 // 3600} Hours"
        body = (f"🎉 <i>You Have Full Access. Enjoy Your Downloads Without Limits.</i>\n\n📋 <b>Plan Details</b>\n"
                f"🎬 <b>Access:</b> <code>All Qualities</code>\n📅 <b>Valid Till:</b> <code>{till}</code>\n⏳ <b>Time Left:</b> <code>{tl}</code>\n"
                f"📦 <b>Big Upload:</b> ✅ Enabled\n✂️ <b>File Splitting:</b> ✅ Enabled\n📥 <b>Daily Downloads:</b> ✅ Unlimited")
        title = "Your Premium Plan"
    else:
        used = count("SELECT COUNT(*) FROM downloads WHERE uid=? AND ok=1 AND ts>=?", (u["uid"], day_start()))
        body = (f"🆓 <i>You Are On The Free Plan.</i>\n\n📋 <b>Plan Details</b>\n🎬 <b>Max Quality:</b> <code>{Si('free_max_height')}p</code>\n"
                f"📥 <b>Today:</b> <code>{used}/{Si('free_daily')}</code>\n📦 <b>Max File Size:</b> <code>{Si('free_max_mb')} MB</code>\n"
                f"✂️ <b>File Splitting:</b> ✅ Enabled")
        title = "Your Free Plan"
    return (f"👑 <u><b>{title}</b></u>\n\n👋 Hi {esc(u['name'])},\n\n{body}\n\n🔄 <b>Extend / Upgrade Your Plan</b>\n\n"
            f"💡 <i>{esc(S('plan_contact'))}</i>")


def day_start():
    return int(dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def settings_text(u):
    return (f"⚙️ <b>Settings Panel</b> 🛠\n\n📦 <b>File Ext:</b> <code>{u['ext']}</code>\n🖼 <b>Custom Thumbnail:</b> <code>{bool(u['thumb'])}</code>\n"
            f"☁️ <b>Upload To:</b> <code>{'Telegram Video' if u['upload'] == 'video' else 'Telegram File'}</code>\n"
            f"🏷 <b>Custom Credit Tag:</b> <code>{esc(eff_tag(u) or '—')}</code>{' 🔒' if S('tag_lock') == '1' and not is_premium(u) else ''}\n\n"
            f"👇 Choose A Section Below To Manage 👇")


def settings_kb(u):
    lock = " 🔒" if S("tag_lock") == "1" and not is_premium(u) else ""
    return Kb([[B(f"📦 File Ext: {u['ext']}", "set:ext")], [B(f"🖼 Thumbnail: {bool(u['thumb'])}", "set:thumb")],
               [B(f"☁️ Upload To: {'Telegram Video' if u['upload'] == 'video' else 'Telegram File'}", "set:up")],
               [B(f"🏷 Credit Tag: {eff_tag(u) or '—'}{lock}", "set:tag")], [B("🚫 Close", "close")]])


def adv_text(u):
    return (f"🧩 <b>Advanced Settings</b>\n\n▶️ <b>Streamable Video:</b> <code>{'On' if u['stream'] else 'Off'}</code>\n"
            f"🔤 <b>File Name Style:</b> <code>{'Dots (A.B.C)' if u['style'] == 'dots' else 'Spaces (A B C)'}</code>\n"
            f"📝 <b>Caption With File Name:</b> <code>{'On' if u['caption'] else 'Off'}</code>")


def adv_kb(u):
    return Kb([[B(f"▶️ Streamable: {'On' if u['stream'] else 'Off'}", "adv:stream")],
               [B(f"🔤 Name Style: {u['style'].title()}", "adv:style")],
               [B(f"📝 Caption: {'On' if u['caption'] else 'Off'}", "adv:caption")], [B("⬅️ Back", "back:main")]])


async def edit(q, text, kb=None):
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


# ============================== CALLBACKS ==============================
@app.on_callback_query()
async def on_cb(c, q):
    uid = q.from_user.id
    u = touch(q.from_user)
    if u["banned"] and uid not in ADMIN_IDS:
        await q.answer("🚫 You are banned.", show_alert=True)
        return
    p = (q.data or "").split(":")
    try:
        await route(c, q, u, p)
    except Exception:
        log.exception("callback failed: %s", q.data)
    try:
        await q.answer()
    except Exception:
        pass


async def route(c, q, u, p):
    uid, k = u["uid"], p[0]
    if k in ("ad", "adset") and uid not in ADMIN_IDS:
        await q.answer("⛔ Admin only", show_alert=True)
        return
    if k == "noop":
        return
    if k == "close":
        await q.message.delete()
    elif k == "back":
        if p[1] == "main":
            await edit(q, main_text(u["name"]), main_kb())
        else:
            await edit(q, settings_text(u), settings_kb(u))
    elif k == "menu":
        if p[1] == "help":
            await edit(q, HELP, Kb([[B("⬅️ Back", "back:main")]]))
        elif p[1] == "about":
            await edit(q, f"ℹ️ <b>About</b>\n\n{esc(S('about_text'))}", Kb([[B("⬅️ Back", "back:main")]]))
        elif p[1] == "plan":
            await edit(q, plan_text(u), Kb([[B("⬅️ Back", "back:main")]]))
        elif p[1] == "adv":
            await edit(q, adv_text(u), adv_kb(u))
        elif p[1] == "set":
            await edit(q, settings_text(u), settings_kb(u))
    elif k == "adv":
        col = {"stream": "stream", "style": "style", "caption": "caption"}[p[1]]
        if col == "style":
            run("UPDATE users SET style=? WHERE uid=?", ("spaces" if u["style"] == "dots" else "dots", uid))
        else:
            run(f"UPDATE users SET {col}=? WHERE uid=?", (0 if u[col] else 1, uid))
        u = get_user(uid)
        await edit(q, adv_text(u), adv_kb(u))
    elif k == "set":
        await settings_panel(q, u, p[1])
    elif k == "ext":
        run("UPDATE users SET ext=? WHERE uid=?", (p[1], uid))
        await settings_panel(q, get_user(uid), "ext")
    elif k == "up":
        run("UPDATE users SET upload=? WHERE uid=?", (p[1], uid))
        await settings_panel(q, get_user(uid), "up")
    elif k == "th":
        if p[1] == "add":
            STATE[uid] = ("thumb",)
            await q.message.reply_text("🖼 Send a <b>photo</b> or a <b>direct image link</b> now. (/cancel to stop)")
        elif p[1] == "del":
            run("UPDATE users SET thumb=0 WHERE uid=?", (uid,))
            (THUMBS / f"{uid}.jpg").unlink(missing_ok=True)
            await settings_panel(q, get_user(uid), "thumb")
        elif p[1] == "view" and (THUMBS / f"{uid}.jpg").exists():
            await q.message.reply_photo(str(THUMBS / f"{uid}.jpg"), caption="🖼 Your custom thumbnail")
    elif k == "tag":
        if p[1] == "set":
            if S("tag_lock") == "1" and not is_premium(u):
                await q.answer("🔒 Credit tag is locked on the free plan.", show_alert=True)
                return
            STATE[uid] = ("tag",)
            await q.message.reply_text("🏷 Send your credit tag (1-12 letters/numbers). It is added to every file name. (/cancel to stop)")
        elif p[1] == "reset":
            run("UPDATE users SET tag='' WHERE uid=?", (uid,))
            await settings_panel(q, get_user(uid), "tag")
    elif k in ("v", "vb", "a", "aall", "askip", "adone", "s", "sall", "sskip", "sdone", "x"):
        await job_cb(c, q, u, p)
    elif k == "ad":
        await admin_cb(c, q, p)
    elif k == "adset":
        STATE[uid] = ("set", p[1])
        await q.message.reply_text(f"✏️ Send the new value for <code>{p[1]}</code> (current: <code>{esc(S(p[1])) or '—'}</code>). /cancel to stop.")


async def settings_panel(q, u, which):
    back = [B("⬅️ Back", "back:set")]
    if which == "ext":
        await edit(q, f"📦 <b>File Ext</b>\n\n🎯 <b>Current:</b> <code>{u['ext']}</code>\n\nChoose The Output Container Format.",
                   Kb([[B(("✅ " if u["ext"] == "MKV" else "") + "MKV", "ext:MKV")], [B(("✅ " if u["ext"] == "MP4" else "🎬 ") + "MP4", "ext:MP4")], back]))
    elif which == "thumb":
        if u["thumb"]:
            await edit(q, "🖼 <b>Thumbnail Panel</b>\n\n✅ A custom thumbnail is set.",
                       Kb([[B("👁 View", "th:view"), B("🗑 Delete", "th:del")], [B("➕ Change Thumbnail", "th:add")], back]))
        else:
            await edit(q, "🖼 <b>Thumbnail Panel</b>\n\nℹ️ No Custom Thumbnail Set Yet.\n📥 Send A Photo Or A Direct Image Link After Tapping Add Thumbnail.",
                       Kb([[B("➕ Add Thumbnail", "th:add")], back]))
    elif which == "up":
        cur = "Telegram Video" if u["upload"] == "video" else "Telegram File"
        await edit(q, f"📥 <b>Upload To</b>\n\n🎯 <b>Current:</b> <code>{cur}</code>\n\n💡 <b>Video:</b> <i>Plays Inside Telegram.</i>\n"
                      f"💡 <b>File:</b> <i>Sent As A Document, Original Quality.</i>\n\n👇 <u>Choose Where Your Downloads Should Be Sent!</u>",
                   Kb([[B("✅ 📲 Telegram", "noop")],
                       [B(("✅ " if u["upload"] == "video" else "") + "🎬 Video", "up:video"), B(("✅ " if u["upload"] == "file" else "") + "📁 File", "up:file")], back]))
    elif which == "tag":
        await edit(q, f"🏷 <b>Credit Tag</b>\n\n🎯 <b>Current:</b> <code>{esc(eff_tag(u) or '—')}</code>\n\nThe tag is added to the end of every file name.",
                   Kb([[B("✏️ Set Tag", "tag:set"), B("♻️ Reset", "tag:reset")], back]))


async def job_cb(c, q, u, p):
    k = p[0]
    job = JOBS.get(p[1])
    if not job or job["uid"] != u["uid"]:
        await q.answer("This menu expired. Send the link again.", show_alert=True)
        return
    if k == "x":
        job["cancel"].set()
        if job["stage"] in ("select",):
            JOBS.pop(job["id"], None)
            await edit(q, "🚫 <b>Cancelled.</b>")
        else:
            await edit(q, "🚫 <b>Cancelling…</b>")
        return
    if job["stage"] != "select":
        return
    if k == "v":
        job["vsel"] = next((f for f in job["vids"] if f["format_id"] == p[2]), None)
        if not job["vsel"]:
            return
        if job["vsel"].get("acodec") not in (None, "none"):     # muxed stream already has audio
            return await after_audio(c, q, job)
        if not job["auds"]:
            return await after_audio(c, q, job)
        t, kb = step2(job)
        await edit(q, t, kb)
    elif k == "vb":
        job["vsel"] = job["vids"][0]
        job["asel"] = [] if job["vsel"].get("acodec") not in (None, "none") else best_audio(job)
        job["subs"] = []
        await begin(c, q, job)
    elif k == "a":
        f = job["auds"][int(p[2])]
        job["asel"] = [x for x in job["asel"] if x is not f] if f in job["asel"] else job["asel"] + [f]
        t, kb = step2(job)
        await edit(q, t, kb)
    elif k == "aall":
        job["asel"] = list(job["auds"])
        await after_audio(c, q, job)
    elif k == "askip":
        job["asel"] = []
        await after_audio(c, q, job)
    elif k == "adone":
        await after_audio(c, q, job)
    elif k == "s":
        code = job["sublist"][int(p[2])][0]
        job["subs"] = [x for x in job["subs"] if x != code] if code in job["subs"] else job["subs"] + [code]
        t, kb = step3(job)
        await edit(q, t, kb)
    elif k == "sall":
        job["subs"] = [x[0] for x in job["sublist"]]
        await begin(c, q, job)
    elif k == "sskip":
        job["subs"] = []
        await begin(c, q, job)
    elif k == "sdone":
        await begin(c, q, job)


async def after_audio(c, q, job):
    if job["sublist"]:
        t, kb = step3(job)
        await edit(q, t, kb)
    else:
        await begin(c, q, job)


# ============================== ADMIN ==============================
def admin_text():
    today = day_start()
    return (f"👑 <b>ADMIN PANEL</b>\n━━━━━━━━━━━━━━\n👥 Users: {count('SELECT COUNT(*) FROM users')} | "
            f"💎 Premium: {count('SELECT COUNT(*) FROM users WHERE premium_until>?', (now(),))} | ⛔ Banned: {count('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
            f"📥 Downloads today: {count('SELECT COUNT(*) FROM downloads WHERE ok=1 AND ts>=?', (today,))} | total: {count('SELECT COUNT(*) FROM downloads WHERE ok=1')}\n"
            f"⚙️ Active jobs: {len([j for j in JOBS.values() if j['stage'] != 'select'])}\n🍪 Cookies: {'✅ uploaded' if COOKIES.exists() else '❌ none'}\n\n"
            f"<b>Free plan:</b> {S('free_daily')}/day, max {S('free_max_height')}p, max {S('free_max_mb')} MB\n"
            f"<b>Commands:</b> /addpremium uid days | /removepremium uid | /ban uid | /unban uid | /stats")


def admin_kb():
    return Kb([[B("📢 Broadcast", "ad:bc"), B("📊 Stats", "ad:stats")],
               [B("💎 Add Premium", "ad:grant"), B("🗑 Remove Premium", "ad:revoke")],
               [B("⛔ Ban", "ad:ban"), B("✅ Unban", "ad:unban")],
               [B("📅 Free Daily Limit", "adset:free_daily"), B("🎬 Free Max Height", "adset:free_max_height")],
               [B("📦 Free Max MB", "adset:free_max_mb"), B("📢 Update Channel", "adset:update_channel")],
               [B("🏷 Default Tag", "adset:default_tag"), B("🔒 Tag Lock On/Off", "ad:taglock")],
               [B("ℹ️ About Text", "adset:about_text"), B("💎 Plan Contact", "adset:plan_contact")],
               [B("🍪 Cookies", "ad:cookies"), B("🔄 Refresh", "ad:refresh")]])


async def admin_cb(c, q, p):
    uid, a = q.from_user.id, p[1]
    if a == "refresh":
        await edit(q, admin_text(), admin_kb())
    elif a == "stats":
        r = db.execute("SELECT uid, COUNT(*) n FROM downloads WHERE ok=1 GROUP BY uid ORDER BY n DESC LIMIT 5").fetchall()
        await q.message.reply_text("📊 <b>Top downloaders</b>\n" + ("\n".join(f"<code>{x['uid']}</code> — {x['n']}" for x in r) or "—"))
    elif a == "taglock":
        setS("tag_lock", "0" if S("tag_lock") == "1" else "1")
        await q.answer(f"Tag lock is now {'ON' if S('tag_lock') == '1' else 'OFF'}", show_alert=True)
    elif a == "cookies":
        await q.message.reply_text("🍪 <b>Cookies</b>\n\nIf YouTube says <i>“Sign in to confirm you’re not a bot”</i>, export your YouTube cookies "
                                   "(Netscape <code>cookies.txt</code> format, from a browser extension while logged in with a spare account) and send the file "
                                   "to this bot as a <b>document with the caption</b> <code>cookies</code>.")
    elif a in ("bc", "grant", "revoke", "ban", "unban"):
        STATE[uid] = (a,)
        prompt = {"bc": "📢 Send the message to broadcast to ALL users (text/photo/video).",
                  "grant": "💎 Send: <code>user_id days</code>", "revoke": "🗑 Send the user ID.",
                  "ban": "⛔ Send the user ID to ban.", "unban": "✅ Send the user ID to unban."}[a]
        await q.message.reply_text(prompt + "\n/cancel to stop.")


async def broadcast(c, m):
    ids = [r[0] for r in db.execute("SELECT uid FROM users WHERE banned=0").fetchall()]
    await m.reply_text(f"🚀 Sending to {len(ids)} users…")

    async def job():
        ok = fail = 0
        for i in ids:
            try:
                await c.copy_message(i, m.chat.id, m.id)
                ok += 1
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except RPCError:
                fail += 1
            await asyncio.sleep(0.05)
        await c.send_message(m.chat.id, f"✅ Broadcast done. Sent: {ok} | Failed: {fail}")
    asyncio.create_task(job())


# ============================== MESSAGES ==============================
CMDS = ["start", "help", "settings", "plan", "cancel", "admin", "stats", "addpremium", "removepremium", "ban", "unban"]


def allowed_url(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


@app.on_message(filters.private & filters.command("start"))
async def cmd_start(c, m):
    u = touch(m.from_user)
    STATE.pop(m.from_user.id, None)
    await m.reply_text(main_text(u["name"]), reply_markup=main_kb())


@app.on_message(filters.private & filters.command("help"))
async def cmd_help(c, m):
    touch(m.from_user)
    await m.reply_text(HELP)


@app.on_message(filters.private & filters.command("settings"))
async def cmd_settings(c, m):
    u = touch(m.from_user)
    await m.reply_text(settings_text(u), reply_markup=settings_kb(u))


@app.on_message(filters.private & filters.command("plan"))
async def cmd_plan(c, m):
    u = touch(m.from_user)
    await m.reply_text(plan_text(u))


@app.on_message(filters.private & filters.command("cancel"))
async def cmd_cancel(c, m):
    STATE.pop(m.from_user.id, None)
    for j in JOBS.values():
        if j["uid"] == m.from_user.id and j["stage"] != "select":
            j["cancel"].set()
    await m.reply_text("❌ Cancelled.")


def admin_only(f):
    async def w(c, m):
        if m.from_user and m.from_user.id in ADMIN_IDS:
            touch(m.from_user)
            await f(c, m)
    return w


@app.on_message(filters.private & filters.command("admin"))
@admin_only
async def cmd_admin(c, m):
    await m.reply_text(admin_text(), reply_markup=admin_kb())


@app.on_message(filters.private & filters.command("stats"))
@admin_only
async def cmd_stats(c, m):
    await m.reply_text(admin_text())


def grant(uid, days):
    u = get_user(uid)
    if not u:
        return False
    start = max(u["premium_until"], now())
    run("UPDATE users SET premium_until=? WHERE uid=?", (start + int(days * 86400), uid))
    return True


@app.on_message(filters.private & filters.command("addpremium"))
@admin_only
async def cmd_addpremium(c, m):
    try:
        uid, days = int(m.command[1]), float(m.command[2])
    except (IndexError, ValueError):
        await m.reply_text("Usage: /addpremium user_id days")
        return
    if grant(uid, days):
        await m.reply_text(f"✅ Premium +{days:g} days for <code>{uid}</code>")
        try:
            await c.send_message(uid, f"💎 <b>Premium activated!</b> +{days:g} days. Check /plan")
        except RPCError:
            pass
    else:
        await m.reply_text("❌ User not found (they must /start the bot first).")


@app.on_message(filters.private & filters.command("removepremium"))
@admin_only
async def cmd_removepremium(c, m):
    try:
        run("UPDATE users SET premium_until=0 WHERE uid=?", (int(m.command[1]),))
        await m.reply_text("✅ Premium removed.")
    except (IndexError, ValueError):
        await m.reply_text("Usage: /removepremium user_id")


@app.on_message(filters.private & filters.command(["ban", "unban"]))
@admin_only
async def cmd_ban(c, m):
    try:
        run("UPDATE users SET banned=? WHERE uid=?", (1 if m.command[0] == "ban" else 0, int(m.command[1])))
        await m.reply_text("✅ Done.")
    except (IndexError, ValueError):
        await m.reply_text("Usage: /ban user_id   or   /unban user_id")


async def handle_state(c, m, u, st):
    uid, kind = u["uid"], st[0]
    if kind == "thumb":
        src = THUMBS / f"{uid}.src"
        try:
            if m.photo:
                await m.download(file_name=str(src))
            elif m.text and m.text.startswith("http"):
                await asyncio.to_thread(fetch_url, m.text.strip(), src)
            else:
                await m.reply_text("❌ Send a photo or a direct image link (or /cancel).")
                return
        except Exception as e:
            await m.reply_text(f"❌ Could not get the image: <code>{esc(e)}</code>")
            return
        ok = await to_thumb(src, THUMBS / f"{uid}.jpg")
        src.unlink(missing_ok=True)
        STATE.pop(uid, None)
        if ok:
            run("UPDATE users SET thumb=1 WHERE uid=?", (uid,))
            u = get_user(uid)
            await m.reply_text("✅ Thumbnail saved!", reply_markup=settings_kb(u))
        else:
            await m.reply_text("❌ That image could not be used.")
    elif kind == "tag":
        tag = (m.text or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,12}", tag):
            await m.reply_text("❌ Use 1-12 letters/numbers only.")
            return
        run("UPDATE users SET tag=? WHERE uid=?", (tag, uid))
        STATE.pop(uid, None)
        u = get_user(uid)
        await m.reply_text("✅ Tag saved!", reply_markup=settings_kb(u))
    elif uid not in ADMIN_IDS:
        STATE.pop(uid, None)
    elif kind == "bc":
        STATE.pop(uid, None)
        await broadcast(c, m)
    elif kind == "set":
        key, val = st[1], (m.text or "").strip()
        if key in ("free_daily", "free_max_height", "free_max_mb") and not val.isdigit():
            await m.reply_text("❌ Send a whole number.")
            return
        setS(key, val)
        STATE.pop(uid, None)
        await m.reply_text(f"✅ <code>{key}</code> updated.", reply_markup=admin_kb())
    elif kind == "grant":
        try:
            t = (m.text or "").split()
            ok = grant(int(t[0]), float(t[1]))
        except (IndexError, ValueError):
            await m.reply_text("❌ Format: <code>user_id days</code>")
            return
        STATE.pop(uid, None)
        await m.reply_text("✅ Premium added." if ok else "❌ User not found.")
    elif kind in ("revoke", "ban", "unban"):
        try:
            t = int((m.text or "").strip())
        except ValueError:
            await m.reply_text("❌ Send a numeric user ID.")
            return
        if kind == "revoke":
            run("UPDATE users SET premium_until=0 WHERE uid=?", (t,))
        else:
            run("UPDATE users SET banned=? WHERE uid=?", (1 if kind == "ban" else 0, t))
        STATE.pop(uid, None)
        await m.reply_text("✅ Done.")


@app.on_message(filters.private & ~filters.command(CMDS))
async def on_any(c, m):
    if not m.from_user:
        return
    uid = m.from_user.id
    u = touch(m.from_user)
    if u["banned"] and uid not in ADMIN_IDS:
        await m.reply_text("🚫 You are banned.")
        return
    st = STATE.get(uid)
    if st:
        await handle_state(c, m, u, st)
        return
    if m.document and uid in ADMIN_IDS and (m.caption or "").strip().lower().startswith("cookies"):
        await m.download(file_name=str(COOKIES))
        await m.reply_text("🍪 Cookies saved. Try a link again.")
        return
    if not m.text:
        return
    mt = URL_RE.search(m.text)
    if not mt:
        await m.reply_text("🔗 Send a valid <b>YouTube</b> link to start. Use /help for the guide.")
        return
    url = mt.group(0)
    if not allowed_url(url):
        await m.reply_text("❌ Only YouTube links are supported.")
        return
    if not is_premium(u):
        used = count("SELECT COUNT(*) FROM downloads WHERE uid=? AND ok=1 AND ts>=?", (uid, day_start()))
        if used >= Si("free_daily"):
            await m.reply_text(f"⏳ Daily limit reached ({used}/{Si('free_daily')}). Try again tomorrow or get Premium — see /plan.")
            return
    if any(j["uid"] == uid and j["stage"] not in ("select",) and not j["done"] for j in JOBS.values()):
        await m.reply_text("⏳ You already have a download running. Wait for it or /cancel it.")
        return
    status = await m.reply_text("🔎 <b>Fetching video info…</b>")
    try:
        info = await asyncio.to_thread(extract, url)
    except Exception as e:
        txt = str(e)
        hint = "\n\nThe admin must upload a cookies file (/admin → Cookies)." if ("Sign in" in txt or "bot" in txt.lower()) else ""
        await status.edit_text(f"❌ <b>Could not read this link.</b>\n<code>{esc(txt[:400])}</code>{hint}")
        return
    if info.get("is_live"):
        await status.edit_text("❌ Live streams are not supported.")
        return
    job = new_job(uid, url, info, u)
    if not job["vids"]:
        await status.edit_text("❌ No downloadable video formats were found.")
        JOBS.pop(job["id"], None)
        return
    t, kb = step1(job)
    await status.edit_text(t, reply_markup=kb)


if __name__ == "__main__":
    if not (API_ID and API_HASH and BOT_TOKEN):
        raise SystemExit("Set API_ID, API_HASH and BOT_TOKEN environment variables.")
    log.info("YouTube bot starting. Admins: %s", ADMIN_IDS)
    app.run()
