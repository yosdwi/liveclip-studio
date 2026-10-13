import os
from pathlib import Path
import sys
import tempfile
import subprocess
import time

# Must set before importing app; fails closed without token.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="liveclip-test-")
os.environ["APP_ACCESS_TOKEN"] = "test-secret"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from main import app, DATA
from media import parse_hls, available_duration, write_snapshot, render_clip

client=TestClient(app)
AUTH={"Authorization":"Bearer test-secret"}

def test_auth_and_url_validation():
    assert client.get("/sessions").status_code == 401
    assert client.get("/sessions",headers=AUTH).status_code == 200
    for url in ["http://www.tiktok.com/@user/live", "https://127.0.0.1/@foo/live", "https://www.tiktok.com.evil.net/@foo/live", "https://www.tiktok.com@evil.net/@foo/live"]:
        assert client.post("/sessions",json={"url":url},headers=AUTH).status_code == 422

def test_manifest_parsing_and_snapshot(tmp_path):
    m=tmp_path/"index.m3u8"
    m.write_text("#EXTM3U\n#EXTINF:3.500,\nseg_000000.ts\n#EXTINF:4.000,\nseg_000001.ts\n#EXTINF:3.000,\nseg_000002.ts\n")
    for n in range(3): (tmp_path/f"seg_{n:06d}.ts").write_bytes(b"fixture")
    segs=parse_hls(m)
    assert len(segs)==3 and segs[1].start==3.5
    assert available_duration(m)==10.5
    out=tmp_path/"snap.m3u8"
    write_snapshot(m,out,6)
    assert "seg_000002.ts" not in out.read_text()
    assert out.read_text().endswith("#EXT-X-ENDLIST\n")

def test_real_ffmpeg_clip(tmp_path):
    # Synthetic stream exercises HLS -> snapshot -> trim -> MP4 with no TikTok account.
    m=tmp_path/"index.m3u8"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i","testsrc2=size=320x240:rate=15","-f","lavfi","-i","sine=frequency=400:sample_rate=44100","-t","6","-c:v","libx264","-g","15","-c:a","aac","-f","hls","-hls_time","2","-hls_list_size","0","-hls_segment_filename",str(tmp_path/"seg_%06d.ts"),str(m)],check=True)
    assert available_duration(m)>=5.9
    out=tmp_path/"result.mp4"
    render_clip(m,out,1.0,4.0,vertical=True)
    assert out.exists() and out.stat().st_size>4000
    probe=subprocess.run(["ffprobe","-v","error","-show_entries","stream=width,height","-of","csv=p=0",str(out)],capture_output=True,text=True,check=True)
    assert "720,1280" in probe.stdout

def test_api_clip_edit_export_complete(tmp_path):
    from uuid import uuid4
    from main import connect
    sid = str(uuid4())
    session_dir=DATA/'sessions'/sid
    session_dir.mkdir(parents=True,exist_ok=True)
    m=session_dir/'index.m3u8'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error',
        '-f','lavfi','-i','testsrc2=size=320x240:rate=15',
        '-f','lavfi','-i','sine=frequency=500:sample_rate=44100',
        '-t','6','-c:v','libx264','-g','15','-c:a','aac',
        '-f','hls','-hls_time','2','-hls_list_size','0',
        '-hls_segment_filename',str(session_dir/'seg_%06d.ts'),str(m)],check=True)
    with connect() as cx:
        cx.execute('INSERT INTO sessions (id,source_url,status,started_at) VALUES (?,?,?,?)',(sid,'https://www.tiktok.com/@fake/live','recording','2026-10-09T12:00:00Z'))
    resp=client.post(f'/sessions/{sid}/clips',headers=AUTH,json={'start_seconds':1,'end_seconds':4,'vertical':False,'title':'Clip 1'})
    assert resp.status_code == 200,resp.text
    cid=resp.json()['id']
    for _ in range(60):
        info=client.get(f'/clips/{cid}',headers=AUTH).json()
        if info['status'] in ('ready','failed'):break
        time.sleep(.2)
    assert info['status']=='ready',info
    assert client.put(f'/clips/{cid}/edit',headers=AUTH,json={'data':{'captions':[{'start':0,'end':2,'text':'Hello LIVE'}]}}).status_code==200
    assert client.get(f'/clips/{cid}/edit',headers=AUTH).json()['data']['captions'][0]['text']=='Hello LIVE'
    resp=client.post(f'/clips/{cid}/export',headers=AUTH,json={'trim_start':0,'trim_end':2,'vertical':True,'captions':[{'start':0.2,'end':1.8,'text':'Hello LIVE'}]})
    assert resp.status_code==200,resp.text
    eid=resp.json()['id']
    for _ in range(70):
        info=[r for r in client.get('/exports',headers=AUTH).json() if r['id']==eid][0]
        if info['status'] in ('ready','failed'):break
        time.sleep(.2)
    assert info['status']=='ready',info
    access=client.get('/media/access',headers=AUTH,params={'kind':'export','asset_id':eid})
    assert access.status_code==200
    out=client.get(access.json()['url'])
    assert out.status_code==200 and len(out.content)>10000
    assert client.post(f'/sessions/{sid}/clips',headers=AUTH,json={'start_seconds':3,'end_seconds':100}).status_code==409


def test_real_live_nonzero_pts_outlier_is_not_fake_dvr_hours(tmp_path):
    from media import normalized_hls_content
    playlist = tmp_path/'index.m3u8'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:7409\n#EXT-X-MEDIA-SEQUENCE:0\n'
                        '#EXTINF:7408.087,\nseg_000000.ts\n'
                        '#EXTINF:2.049,\nseg_000001.ts\n'
                        '#EXTINF:2.049,\nseg_000002.ts\n'
                        '#EXTINF:2.053,\nseg_000003.ts\n'
                        '#EXTINF:2.052,\nseg_000004.ts\n')
    for n in range(5): (tmp_path/f"seg_{n:06d}.ts").write_bytes(b"fixture")
    segments = parse_hls(playlist)
    assert len(segments) == 5
    assert 2.04 < segments[0].duration < 2.06
    assert 10.2 <= available_duration(playlist) <= 10.3
    fixed = normalized_hls_content(playlist)
    assert '#EXT-X-TARGETDURATION:3' in fixed
    assert '#EXTINF:7408.087' not in fixed
    snap = tmp_path/'snap.m3u8'
    write_snapshot(playlist,snap,8)
    assert 'seg_000004.ts' not in snap.read_text()
    assert 'seg_000003.ts' in snap.read_text()

def test_atomic_media_guard_and_poster(tmp_path):
    from media import atomic_video, generate_poster, verify_media
    output=tmp_path/"safe.mp4"
    def render(part):
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
            "-i","testsrc2=size=320x240:rate=15","-f","lavfi",
            "-i","sine=frequency=500:sample_rate=44100","-t","3",
            "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(part)],check=True)
        assert not output.exists()
    info=atomic_video(output,render,3)
    assert info["width"]==320 and output.is_file()
    assert verify_media(output,3)["duration_seconds"]>=2.9
    assert generate_poster(output,tmp_path/"poster.jpg")
    assert (tmp_path/"poster.jpg").stat().st_size>500

def test_atomic_media_rejects_corruption(tmp_path):
    from media import atomic_video
    import pytest
    target=tmp_path/"broken.mp4"
    with pytest.raises(Exception):
        atomic_video(target,lambda part: part.write_bytes(b"bad media"*500))
    assert not target.exists() and not (tmp_path/"broken.partial.mp4").exists()

def test_media_urls_are_private_and_expiring():
    from main import connect
    from uuid import uuid4
    sid=str(uuid4())
    with connect() as cx:
        cx.execute("INSERT INTO sessions(id,source_url,status,started_at) VALUES(?,?,?,?)",
            (sid,"https://www.tiktok.com/@test/live","completed","2026-10-13T00:00:00Z"))
    assert client.get(f"/media/sessions/{sid}/index.m3u8").status_code==403
    assert client.get("/media/access",params={"kind":"session","asset_id":sid}).status_code==401
    ticket=client.get("/media/access",headers=AUTH,params={"kind":"session","asset_id":sid})
    assert ticket.status_code==200
    assert "?expires=" in ticket.json()["url"] and "sig=" in ticket.json()["url"]
    assert client.get(ticket.json()["url"]).status_code==404
    assert client.get(f"/media/sessions/{sid}/index.m3u8",params={"expires":"1","sig":"no"}).status_code==403

def test_repair_archive_and_postlive_clip(tmp_path):
    from main import connect, DATA
    from uuid import uuid4
    sid=str(uuid4())
    folder=DATA/"sessions"/sid
    folder.mkdir(parents=True,exist_ok=True)
    manifest=folder/"index.m3u8"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error",
        "-f","lavfi","-i","testsrc2=size=320x240:rate=15",
        "-f","lavfi","-i","sine=frequency=440:sample_rate=44100",
        "-t","6","-c:v","libx264","-g","15","-c:a","aac",
        "-f","hls","-hls_time","2","-hls_list_size","0",
        "-hls_segment_filename",str(folder/"seg_%06d.ts"),str(manifest)],check=True)
    with connect() as cx:
        cx.execute("INSERT INTO sessions(id,source_url,status,started_at) VALUES(?,?,?,?)",
            (sid,"https://www.tiktok.com/@test/live","completed","2026-10-13T00:00:00Z"))
    assert client.post(f"/sessions/{sid}/clips",headers=AUTH,
        json={"start_seconds":.5,"end_seconds":3}).status_code==409
    response=client.post(f"/sessions/{sid}/repair",headers=AUTH)
    assert response.status_code==200,response.text
    for _ in range(80):
        row=client.get(f"/sessions/{sid}",headers=AUTH).json()
        if row["status"] in ("completed","failed"): break
        time.sleep(.2)
    assert row["status"]=="completed",row
    assert row["full_ready"]
    assert (folder/"poster.jpg").exists()
    clip=client.post(f"/sessions/{sid}/clips",headers=AUTH,
        json={"start_seconds":.5,"end_seconds":3,"vertical":True,"title":"After repair"})
    assert clip.status_code==200,clip.text
    for _ in range(80):
        c=client.get(f"/clips/{clip.json()['id']}",headers=AUTH).json()
        if c["status"] in ("ready","failed"):break
        time.sleep(.2)
    assert c["status"]=="ready",c


def test_legacy_pdt_discontinuity_detected_and_removed(tmp_path):
    from media import source_clock_discontinuous, normalized_hls_content
    manifest=tmp_path/"index.m3u8"
    manifest.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
        "#EXTINF:2.0,\n#EXT-X-PROGRAM-DATE-TIME:2026-10-13T13:25:25+00:00\nseg_000000.ts\n"
        "#EXTINF:2.0,\n#EXT-X-PROGRAM-DATE-TIME:2026-10-13T14:05:39+00:00\nseg_000001.ts\n"
        "#EXTINF:2.0,\n#EXT-X-PROGRAM-DATE-TIME:2026-10-13T14:05:41+00:00\nseg_000002.ts\n"
    )
    for n in range(3): (tmp_path/f"seg_{n:06d}.ts").write_bytes(b"test")
    assert source_clock_discontinuous(manifest)
    normalized=normalized_hls_content(manifest)
    assert "#EXT-X-PROGRAM-DATE-TIME" not in normalized
    assert "#EXTINF:2.0," in normalized
