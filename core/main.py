"""LiveClip POC: one recorder per service, persistent DVR, manual clipping.

Deployment intentionally runs one core instance with a persistent volume. Do not scale
out without moving jobs/metadata to a shared durable queue and media store.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
import httpx
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from media import parse_hls, normalized_hls_content, available_duration, render_clip, finalize_recording, export_video

DATA = Path(os.environ.get("DATA_DIR", "/data" if Path("/data").exists() else "./data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "liveclip.sqlite"
API_TOKEN = os.environ.get("APP_ACCESS_TOKEN", "")
ALLOW_DEMO_SOURCE = os.environ.get("ALLOW_DEMO_SOURCE", "false").lower() == "true"
CORS_ORIGINS = [x.strip() for x in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if x.strip()]
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="liveclip")
LOCK = threading.RLock()
RUNNERS: dict[str, tuple[subprocess.Popen, subprocess.Popen]] = {}

app = FastAPI(title="LiveClip Core", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"], allow_credentials=False)

@contextmanager
def connect():
    cx = sqlite3.connect(DB, timeout=15)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    try:
        yield cx
        cx.commit()
    finally:
        cx.close()

with connect() as cx:
    cx.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source_url TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, start_seconds REAL NOT NULL, end_seconds REAL NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL, vertical INTEGER DEFAULT 0, error TEXT, created_at TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES sessions(id));
    CREATE TABLE IF NOT EXISTS edits (clip_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS exports (id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL);
    """)
    # Worker processes do not survive deploys. Treat old 'recording' as interrupted.
    cx.execute("UPDATE sessions SET status='interrupted', ended_at=? WHERE status IN ('starting','recording','stopping','finalizing')", (datetime.now(timezone.utc).isoformat(),))
    cx.execute("UPDATE clips SET status='failed', error='Render interrupted by service restart' WHERE status IN ('queued','rendering')")
    cx.execute("UPDATE exports SET status='failed', error='Export interrupted by service restart' WHERE status IN ('queued','rendering')")

def now():
    return datetime.now(timezone.utc).isoformat()

def require_access(authorization: str | None):
    if not API_TOKEN or not authorization or not secrets.compare_digest(authorization, "Bearer " + API_TOKEN):
        raise HTTPException(401, "Access token required")

def get_session(sid):
    with connect() as cx:
        row = cx.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row: raise HTTPException(404, "Recording not found")
    return dict(row)

def get_clip(cid):
    with connect() as cx:
        row = cx.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    if not row: raise HTTPException(404, "Clip not found")
    return dict(row)

def playlist_path(sid): return DATA / "sessions" / sid / "index.m3u8"

def require_source_url(url):
    """Allow only TikTok-hosted HTTPS links, including vt.tiktok.com shortlinks."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    allowed = {"www.tiktok.com", "tiktok.com", "vt.tiktok.com", "m.tiktok.com"}
    if p.scheme != "https" or p.username or p.password or p.port or host not in allowed:
        raise HTTPException(422, "Only HTTPS TikTok LIVE links are allowed")
    if host == "vt.tiktok.com":
        if not re.fullmatch(r"/[A-Za-z0-9_\-]{5,120}/?",p.path):
            raise HTTPException(422, "Invalid TikTok shortlink")
        # Resolve only allowlisted HTTPS hosts, one redirect at a time. No general URL fetch.
        current=url
        try:
            with httpx.Client(timeout=6.0, follow_redirects=False) as client:
                for _ in range(6):
                    resp=client.head(current)
                    if resp.status_code in (403,405):
                        with client.stream("GET",current) as streamed:
                            status=streamed.status_code
                            location=streamed.headers.get("location")
                    else:
                        status=resp.status_code
                        location=resp.headers.get("location")
                    if status not in (301,302,303,307,308) or not location:
                        break
                    current=urljoin(current,location)
                    cp=urlparse(current)
                    if cp.scheme!='https' or cp.username or cp.password or cp.port or (cp.hostname or '').lower() not in allowed:
                        raise HTTPException(422, "Shortlink redirected away from TikTok")
            url=current
            p=urlparse(url)
        except httpx.HTTPError:
            raise HTTPException(422,"TikTok shortlink could not be resolved. Paste the full @username/live URL.")
    if (p.hostname or '').lower() not in {"www.tiktok.com","tiktok.com","m.tiktok.com"}:
        raise HTTPException(422,"Unable to resolve TikTok shortlink to LIVE")
    if not re.fullmatch(r"/@[\w.\-]{2,48}/live/?",p.path):
        raise HTTPException(422,"This URL does not point to a TikTok LIVE. Use @username/live")
    return "https://www.tiktok.com"+p.path.rstrip("/")

class StartRequest(BaseModel):
    url: str

class Caption(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=200)

class ExportRequest(BaseModel):
    trim_start: float = Field(default=0, ge=0)
    trim_end: float | None = None
    vertical: bool = True
    captions: list[Caption] = Field(default_factory=list, max_length=100)

class ClipRequest(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    title: str = Field(default="Untitled clip", max_length=100)
    vertical: bool = False

class EditRequest(BaseModel):
    data: dict

@app.get("/health")
def health():
    return {"status":"ok", "recorder_online":bool(RUNNERS), "data_dir":str(DATA)}

@app.get("/sessions")
def list_sessions(authorization: str | None = Header(None)):
    require_access(authorization)
    with connect() as cx:
        sessions = [dict(r) for r in cx.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 100")]
    for s in sessions:
        s["duration_seconds"] = round(available_duration(playlist_path(s["id"])), 2)
        s["segments"] = len(parse_hls(playlist_path(s["id"])))
        s["full_ready"] = (DATA / "sessions" / s["id"] / "full.mp4").exists()
    return sessions

@app.post("/sessions")
def create_session(body: StartRequest, authorization: str | None = Header(None)):
    require_access(authorization)
    source = require_source_url(body.url)
    with LOCK:
        with connect() as cx:
            active=cx.execute("SELECT 1 FROM sessions WHERE status IN ('starting','recording','stopping','finalizing') LIMIT 1").fetchone()
        if RUNNERS or active:
            raise HTTPException(409, "One recording is already running. Stop it before starting another.")
        sid = str(uuid4())
        workdir = DATA / "sessions" / sid
        workdir.mkdir(parents=True)
        with connect() as cx:
            cx.execute("INSERT INTO sessions (id,source_url,status,started_at) VALUES (?,?,?,?)", (sid,source,"starting",now()))
        POOL.submit(start_recorder, sid, source, workdir)
        return {"id":sid,"status":"starting"}

def start_recorder(sid, source, workdir):
    """Pipe Streamlink into ffmpeg. No shell; URL validated before execution."""
    get_stream = ["streamlink", "--stdout", "--retry-open", "3", source, "best"]
    ffmpeg = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_list_size", "0", "-hls_playlist_type", "event", "-hls_flags", "independent_segments+program_date_time+temp_file", "-hls_segment_filename", str(workdir / "seg_%06d.ts"), str(workdir / "index.m3u8")]
    src = proc = None
    try:
        src = subprocess.Popen(get_stream, stdout=subprocess.PIPE, stderr=(workdir / "streamlink.log").open("ab"), start_new_session=True)
        proc = subprocess.Popen(ffmpeg, stdin=src.stdout, stdout=subprocess.DEVNULL, stderr=(workdir / "ffmpeg.log").open("ab"), start_new_session=True)
        assert src.stdout is not None
        src.stdout.close()
        with LOCK:
            RUNNERS[sid] = (src, proc)
        with connect() as cx:
            cx.execute("UPDATE sessions SET status='recording' WHERE id=?",(sid,))
        while proc.poll() is None and src.poll() is None:
            time.sleep(.5)
        if src.poll() is not None and proc.poll() is None:
            # Give FFmpeg time to flush and close the last HLS segment.
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: pass
        with connect() as cx:
            state = cx.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()["status"]
        if state != "stopping":
            error = f"Stream ended (source={src.poll()}, ffmpeg={proc.poll()}); check logs"
            with connect() as cx: cx.execute("UPDATE sessions SET status='finalizing', error=? WHERE id=?", (error,sid))
    except Exception as ex:
        with connect() as cx: cx.execute("UPDATE sessions SET status='failed',error=?,ended_at=? WHERE id=?",(str(ex),now(),sid))
    finally:
        for p in (src,proc):
            if p and p.poll() is None:
                try: os.killpg(p.pid,signal.SIGTERM)
                except ProcessLookupError: pass
                try: p.wait(timeout=10)
                except subprocess.TimeoutExpired: os.killpg(p.pid,signal.SIGKILL)
        with LOCK: RUNNERS.pop(sid,None)
        if playlist_path(sid).exists():
            with connect() as cx: cx.execute("UPDATE sessions SET status='finalizing' WHERE id=? AND status IN ('stopping','recording')",(sid,))
            try:
                finalize_recording(playlist_path(sid), workdir / "full.mp4")
                with connect() as cx: cx.execute("UPDATE sessions SET status='completed',ended_at=? WHERE id=?",(now(),sid))
            except Exception as ex:
                with connect() as cx: cx.execute("UPDATE sessions SET status='failed',error=?,ended_at=? WHERE id=?",(str(ex),now(),sid))
        else:
            with connect() as cx:
                cx.execute("UPDATE sessions SET status='failed',error=COALESCE(error,'No HLS manifest produced; source may be offline'),ended_at=? WHERE id=?",(now(),sid))

@app.get("/sessions/{sid}")
def session_detail(sid: str, authorization: str | None = Header(None)):
    require_access(authorization)
    s = get_session(sid)
    s["duration_seconds"] = round(available_duration(playlist_path(sid)),2)
    s["segments"] = len(parse_hls(playlist_path(sid)))
    s["full_ready"] = (DATA / "sessions" / sid / "full.mp4").exists()
    return s

@app.post("/sessions/{sid}/stop")
def stop(sid: str, authorization: str | None = Header(None)):
    require_access(authorization)
    get_session(sid)
    with LOCK:
        running = RUNNERS.get(sid)
        if not running:
            raise HTTPException(409,"Recorder is not running")
        with connect() as cx: cx.execute("UPDATE sessions SET status='stopping' WHERE id=?",(sid,))
        src,proc = running
        if src.poll() is None: os.killpg(src.pid,signal.SIGTERM)
        # Let ffmpeg drain the final buffered segments when source closes.
    return {"status":"stopping"}

@app.post("/sessions/{sid}/clips")
def create_clip(sid: str, body: ClipRequest, authorization: str | None = Header(None)):
    require_access(authorization)
    get_session(sid)
    if body.end_seconds - body.start_seconds > 600 or body.end_seconds <= body.start_seconds:
        raise HTTPException(422,"Choose 0–600 seconds")
    buffered=available_duration(playlist_path(sid))
    if body.end_seconds > buffered-.05:
        raise HTTPException(409, f"Clip must end before currently buffered tail ({buffered:.1f}s)")
    cid=str(uuid4())
    with connect() as cx:
        cx.execute("INSERT INTO clips (id,session_id,start_seconds,end_seconds,status,title,vertical,created_at) VALUES (?,?,?,?,?,?,?,?)", (cid,sid,body.start_seconds,body.end_seconds,"queued",body.title,int(body.vertical),now()))
    POOL.submit(render_job,cid)
    return {"id":cid,"status":"queued"}

def render_job(cid):
    clip=get_clip(cid)
    manifest=playlist_path(clip["session_id"])
    outdir=DATA / "clips"
    outdir.mkdir(parents=True,exist_ok=True)
    with connect() as cx: cx.execute("UPDATE clips SET status='rendering' WHERE id=?",(cid,))
    try:
        render_clip(manifest,outdir / f"{cid}.mp4",clip["start_seconds"],clip["end_seconds"],bool(clip["vertical"]))
        with connect() as cx: cx.execute("UPDATE clips SET status='ready',error=NULL WHERE id=?",(cid,))
    except Exception as exc:
        (outdir / f"{cid}.mp4").unlink(missing_ok=True)
        with connect() as cx: cx.execute("UPDATE clips SET status='failed',error=? WHERE id=?",(str(exc),cid))

@app.get("/clips")
def list_clips(authorization: str | None = Header(None)):
    require_access(authorization)
    with connect() as cx: return [dict(r) for r in cx.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT 300")]

@app.get("/clips/{cid}")
def clip_detail(cid: str, authorization: str | None = Header(None)):
    require_access(authorization)
    return get_clip(cid)

@app.put("/clips/{cid}/edit")
def save_edit(cid: str, body: EditRequest, authorization: str | None = Header(None)):
    require_access(authorization)
    get_clip(cid)
    payload=json.dumps(body.data)
    if len(payload)>100_000: raise HTTPException(413,"Edit project is too large")
    with connect() as cx:
        cx.execute("INSERT INTO edits (clip_id,data,updated_at) VALUES (?,?,?) ON CONFLICT(clip_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",(cid,payload,now()))
    return {"status":"saved"}

@app.get("/clips/{cid}/edit")
def load_edit(cid: str, authorization: str | None = Header(None)):
    require_access(authorization)
    get_clip(cid)
    with connect() as cx:
        row=cx.execute("SELECT data,updated_at FROM edits WHERE clip_id=?",(cid,)).fetchone()
    return {"data":json.loads(row["data"]) if row else {},"updated_at":row["updated_at"] if row else None}

# Media endpoints permit unguessable UUID access in this single-user POC. Production
# MUST replace with short-lived signed URLs or cookie-auth proxy; do not index them.
@app.get("/media/sessions/{sid}/index.m3u8")
def media_playlist(sid: str):
    get_session(sid)
    path=playlist_path(sid)
    if not path.exists(): raise HTTPException(404,"DVR buffer not ready")
    content=normalized_hls_content(path)
    # Relative paths will resolve into /media/sessions/{sid}/seg_....ts.
    return Response(content,media_type="application/vnd.apple.mpegurl",headers={"Cache-Control":"no-cache, no-store", "X-Robots-Tag":"noindex"})

@app.get("/media/sessions/{sid}/{filename}")
def media_segment(sid: str, filename: str):
    get_session(sid)
    if not re.fullmatch(r"seg_\d{6}\.ts|full\.mp4", filename): raise HTTPException(404)
    path=DATA / "sessions" / sid / filename
    if not path.is_file(): raise HTTPException(404)
    return FileResponse(path,media_type="video/mp2t" if filename.endswith(".ts") else "video/mp4",headers={"Cache-Control":"public, max-age=300", "X-Robots-Tag":"noindex"})

@app.get("/media/clips/{cid}.mp4")
def media_clip(cid: str):
    get_clip(cid)
    path=DATA / "clips" / f"{cid}.mp4"
    if not path.is_file(): raise HTTPException(404)
    return FileResponse(path,media_type="video/mp4",headers={"X-Robots-Tag":"noindex"})


@app.post("/clips/{cid}/export")
def export_clip(cid: str, body: ExportRequest, authorization: str | None = Header(None)):
    require_access(authorization)
    clip=get_clip(cid)
    if clip["status"] != "ready": raise HTTPException(409,"Wait for the clip to render")
    duration=clip["end_seconds"]-clip["start_seconds"]
    end=body.trim_end if body.trim_end is not None else duration
    if end>duration+0.01 or end<=body.trim_start or end-body.trim_start>600:
        raise HTTPException(422,"Invalid export trim")
    for c in body.captions:
        if c.end> end-body.trim_start+0.01 or c.end<=c.start:
            raise HTTPException(422,"Caption timestamps must be relative to trimmed clip")
    eid=str(uuid4())
    with connect() as cx:
        cx.execute("INSERT INTO exports (id,clip_id,status,created_at) VALUES (?,?,?,?)",(eid,cid,"queued",now()))
    POOL.submit(export_job,eid,clip,body,body.trim_start,end)
    return {"id":eid,"status":"queued"}

def export_job(eid,clip,body,start,end):
    with connect() as cx: cx.execute("UPDATE exports SET status='rendering' WHERE id=?",(eid,))
    path=DATA/"exports"
    path.mkdir(parents=True,exist_ok=True)
    try:
        export_video(DATA/"clips"/f"{clip['id']}.mp4", path/f"{eid}.mp4",start,end,body.vertical,[c.model_dump() for c in body.captions])
        with connect() as cx: cx.execute("UPDATE exports SET status='ready' WHERE id=?",(eid,))
    except Exception as ex:
        (path/f"{eid}.mp4").unlink(missing_ok=True)
        with connect() as cx: cx.execute("UPDATE exports SET status='failed', error=? WHERE id=?",(str(ex),eid))

@app.get("/exports")
def list_exports(authorization: str | None = Header(None)):
    require_access(authorization)
    with connect() as cx: return [dict(r) for r in cx.execute("SELECT * FROM exports ORDER BY created_at DESC LIMIT 300")]

@app.get("/media/exports/{eid}.mp4")
def media_export(eid: str):
    with connect() as cx: row=cx.execute("SELECT id FROM exports WHERE id=?",(eid,)).fetchone()
    if not row: raise HTTPException(404)
    path=DATA/"exports"/f"{eid}.mp4"
    if not path.is_file(): raise HTTPException(404)
    return FileResponse(path,media_type="video/mp4",headers={"X-Robots-Tag":"noindex"})
