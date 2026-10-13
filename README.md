# LiveClip Studio · POC v0.1

Next.js web app + Python FastAPI/FFmpeg recorder for user-controlled TikTok LIVE capture, DVR rewind, manual clipping and server-side MP4 export. No automatic AI highlight ranking.

## POC status (2026-10-09)

- Implemented: TikTok LIVE canonical/shortlink validation, FFmpeg/Streamlink ingest, HLS EVENT DVR playlist, disk-persistent recording, background archive MP4, segment-based in/out clipping, persisted edit drafts, caption overlay, reframe 9:16, server-side export, responsive browser UI, recording and media libraries.
- Tests: 4 Python tests pass (including actual FFmpeg video/audio HLS clipping, export with burned captions and MP4 download). Frontend TSX syntax was checked with TypeScript transpilation, **not** a full Next.js build (package registry unavailable in build sandbox).
- **Not verified:** actual TikTok LIVE compatibility, Docker build, Next.js production build, Railway live deployments, Safari/iPhone playback; these require network-enabled runtime and an active permitted LIVE. The Railway project exists, but the two services have no source connected or active deployment.
- Future: auto-transcription using faster-whisper, rich Twick timeline editor, object-store archival, session reconnect and resume, multi-user auth.

## Architecture

```
 Browser (Next.js, responsive)
    | API auth bearer key                 | HLS / TS / MP4 preview
    v                                     v
 FastAPI core (1 instance with mounted persistent volume)
    | Start / stop / in-out               | Render/export jobs
    v                                     v
 Streamlink ---> FFmpeg HLS EVENT       FFmpeg clip & subtitle render
    |                                     |
 /data/sessions/<uuid>/seg_*.ts          /data/clips /data/exports
    |                                     |
 /data/sessions/<uuid>/index.m3u8       metadata in /data/liveclip.sqlite
    |
 ffmpeg finalize -> /data/sessions/<uuid>/full.mp4
```

MVP records everything **from the moment Start is clicked**, not from the moment a TikTok broadcast first started. The browser can close without stopping the server-side recorder. The core is purposely **single replica, one concurrent LIVE** to keep media and database consistent on one Railway Volume. Exceeding the mounted volume capacity stops writing; cap source duration/quality and monitor usage.

## Local (Docker recommended)

1. Create a `.env` in the root with `APP_ACCESS_TOKEN=<your long secret>`.
2. Run `docker compose up --build` from this directory.
3. Open http://localhost:3000 and enter that token. Core healthcheck: http://localhost:8000/health.
4. Paste `https://www.tiktok.com/@streamer/live` or a `https://vt.tiktok.com/...` shortlink, click **Start recording**.
5. Select a previous timestamp by scrubbing DVR, press Set IN and Set OUT, click Generate clip, then go to Library → Edit → Export video.

TikTok LIVE must be active, publicly accessible and compatible with the installed Streamlink plugin. If the stream cannot be ingested, check `/data/sessions/<id>/streamlink.log` and `/data/sessions/<id>/ffmpeg.log` within the recorder volume. Recording runs on the server, not inside the browser.

## Railway project already provisioned

- Project: `liveclip-studio-poc` (`1ff504e6-4f05-4417-ba00-1e110efd8802`)
- Environment: `production`
- `liveclip-core` (`4cc3eca2-e477-4161-9808-9427151a59ba`) with 4800 MB volume at `/data`
- `liveclip-web` (`eb5f6e5c-51ba-474d-b43e-bb2f048e29ca`)
- Core domain reserved: `https://liveclip-core-production.up.railway.app`
- Web domain reserved: `https://liveclip-web-production.up.railway.app`
- Environment vars already configured for access token, core data mount, CORS and public API URL.

**Important: these are RESERVED domains, not running apps yet.** GitHub creation is not available in the currently connected GitHub tool set. Do not assume a green deploy until Railway reports `SUCCESS` and the endpoints pass health/smoke tests.

### To finish deployment from your machine

Create a new dedicated private GitHub repository and upload this source code, for example:

```bash
cd liveclip-studio
git init && git add . && git commit -m 'feat: liveclip full recording and manual editing POC'
gh repo create yosdwi/liveclip-studio --private --source=. --remote=origin --push
```

Then connect the same repo to BOTH existing Railway services (no new services needed):

- `liveclip-core`: GitHub repo `yosdwi/liveclip-studio`, branch `main`, **root directory** `/core`, Dockerfile `/Dockerfile` relative to root, 1 replica, mount `/data`.
- `liveclip-web`: repo `yosdwi/liveclip-studio`, branch `main`, **root directory** `/web`, Dockerfile `/Dockerfile` relative to root, 1 replica.

Core env `APP_ACCESS_TOKEN` was set directly on Railway; rotate this secret for production. Domain and CORS variables have been configured. Web `NEXT_PUBLIC_CORE_URL` must be present at **build** time (Dockerfile declares it as ARG). Verify the domain after both services show a Railway successful deployment and `/health` reports `ok`.

**Security limitation:** Media URLs are accessible with opaque UUIDs in this single-user MVP. Before handling private or licensed assets commercially, protect media with short-lived signed URLs and move authentication to an HttpOnly session cookie / BFF. Restrict public registration. Use only LIVE content you are allowed to record, reuse and publish; check TikTok terms and rights.

## API summary

`GET /health` (public)

Authenticated with `Authorization: Bearer <APP_ACCESS_TOKEN>`:

```
GET  /sessions
POST /sessions                    {"url":"https://www.tiktok.com/@username/live"}
GET  /sessions/{id}
POST /sessions/{id}/stop
POST /sessions/{id}/clips         {"start_seconds":22,"end_seconds":48,"vertical":true,"title":"Highlight"}
GET  /clips
GET  /clips/{id}
PUT  /clips/{id}/edit             {"data":{"captions":[...]}}
GET  /clips/{id}/edit
POST /clips/{id}/export           {"trim_start":0,"trim_end":20,"vertical":true,"captions":[{"start":0.5,"end":2.5,"text":"Hello!"}]}
GET  /exports
```

Public media with opaque recording/export IDs:

```
GET /media/sessions/{id}/index.m3u8
GET /media/sessions/{id}/seg_000000.ts
GET /media/sessions/{id}/full.mp4
GET /media/clips/{id}.mp4
GET /media/exports/{id}.mp4
```

Note: the HLS timestamp is relative to captured media, not TikTok wall-clock broadcast time. In/out points are seconds from recording start; the render engine takes a frozen manifest so it doesn't wait for the LIVE tail.

## Tests and tools

```bash
cd core
pip install -r requirements.txt
pytest -q
```

Tests use a synthetic `testsrc2` + sine audio signal; no third-party LIVE required. They exercise m3u8 manifests, FFmpeg crop, MP4 muxing, captions, API metadata and HTTP download. End-to-end against TikTok is a separate acceptance test, not covered by synthetic tests.

## POC limitations / next steps

1. Validate TikTok short URL redirect and Streamlink extraction against a LIVE you own or are permitted to record. TikTok can change undocumented stream interfaces without notice.
2. Build/test Next.js with npm and validate HLS.js on Chrome + native HLS on iOS Safari.
3. Add periodic recording health probes, auto-restart/reconnect with explicit timeline gaps and robust disk quotas; core currently marks stream interruption as completed if some media was recovered.
4. Add S3 object storage for segments/archive/clip assets and server-side signed media delivery. The current 4.8 GB volume is NOT suitable for unlimited continuous recordings; 4 Mbps video approximates 1.8 GB/hour, excluding archive duplication.
5. Integrate a real timeline editor SDK (Twick) when licensing, performance and mobile compatibility have been verified; the current editing UI is intentionally basic.
6. Auto-transcription by faster-whisper, editable word timestamps, subtitle templates and improved styling.
7. Add headless Chrome/Safari smoke tests, logging, metrics and user/session auth before scaling beyond a private single-user POC.
