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
        cx.execute('INSERT INTO sessions (id,source_url,status,started_at) VALUES (?,?,?,?)',(sid,'https://www.tiktok.com/@fake/live','completed','2026-10-09T12:00:00Z'))
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
    out=client.get(f'/media/exports/{eid}.mp4')
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
