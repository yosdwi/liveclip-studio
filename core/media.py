"""Small and deliberately explicit FFmpeg pipeline for a single-recording POC."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import json
import math
import re
import statistics
import subprocess
from typing import Iterable

@dataclass(frozen=True)
class Segment:
    filename: str
    duration: float
    start: float


def parse_hls(manifest: Path) -> list[Segment]:
    """Read completed segments, clamping a spurious initial PTS offset.

    TikTok FLV clocks can start at 7400+ seconds: FFmpeg then writes an
    anomalous first EXTINF even when the TS fragment is only ~2 seconds long.
    """
    if not manifest.exists():
        return []
    raw_segments: list[tuple[str, float]] = []
    duration = None
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            if not re.fullmatch(r"seg_\d{6}\.ts", line):
                raise ValueError("Unsafe segment filename in playlist")
            raw_segments.append((line, duration))
            duration = None
    if len(raw_segments) >= 4:
        following = [d for _, d in raw_segments[1:30] if 0 < d < 30]
        if len(following) >= 3:
            typical = statistics.median(following)
            name, first = raw_segments[0]
            if first > 60 and first > typical * 8:
                raw_segments[0] = (name, typical)
    position = 0.0
    segments: list[Segment] = []
    for name, duration in raw_segments:
        segments.append(Segment(name, duration, position))
        position += duration
    return segments


def normalized_hls_content(manifest: Path) -> str:
    """Keep browser DVR timing aligned with the clip selection timebase."""
    raw = manifest.read_text(encoding="utf-8")
    segments = parse_hls(manifest)
    if not segments:
        return raw
    lines = raw.splitlines()
    raw_first = next((float(line.split(":",1)[1].split(",",1)[0]) for line in lines if line.startswith("#EXTINF:")),None)
    if raw_first is None or abs(segments[0].duration-raw_first) < .001:
        return raw
    changed = False
    result = []
    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            line = f"#EXT-X-TARGETDURATION:{max(1,int(max(x.duration for x in segments)+0.999))}"
        elif line.startswith("#EXTINF:") and not changed:
            line = f"#EXTINF:{segments[0].duration:.6f},"
            changed = True
        result.append(line)
    return "\n".join(result)+"\n"


def available_duration(manifest: Path) -> float:
    return sum(s.duration for s in parse_hls(manifest))


def write_snapshot(manifest: Path, dest: Path, end_seconds: float) -> None:
    """Make a finite playlist so ffmpeg never waits for the *live* tail."""
    segments = parse_hls(manifest)
    included = [s for s in segments if s.start < end_seconds]
    if not included:
        raise ValueError("No completed media segment is available yet")
    playlist = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:" + str(max(1, int(max(s.duration for s in included) + .999))), "#EXT-X-MEDIA-SEQUENCE:0"]
    for seg in included:
        playlist += [f"#EXTINF:{seg.duration:.6f},", seg.filename]
    playlist += ["#EXT-X-ENDLIST", ""]
    dest.write_text("\n".join(playlist), encoding="utf-8")


def run_ffmpeg(cmd: list[str], **kwargs) -> None:
    try:
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE, **kwargs)
    except subprocess.CalledProcessError as exc:
        output = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        raise RuntimeError(f"FFmpeg exited {exc.returncode}: {output[-1200:]}") from exc



def verify_media(path: Path, expected_duration: float | None = None) -> dict:
    """Do not publish a video without confirming decodable video frames."""
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError("Output media is empty or missing")
    process = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration:stream=codec_type,codec_name,width,height,pix_fmt",
         "-of","json",str(path)],capture_output=True,text=True,timeout=30,check=True)
    metadata = json.loads(process.stdout)
    video = next((s for s in metadata.get("streams",[]) if s.get("codec_type")=="video"),None)
    if not video or video.get("codec_name")!="h264" or video.get("pix_fmt")!="yuv420p":
        raise ValueError("Video is not compatible H.264 yuv420p")
    duration=float(metadata.get("format",{}).get("duration") or 0)
    if not math.isfinite(duration) or duration < .15:
        raise ValueError("Video duration invalid")
    if expected_duration is not None and not (max(.1,expected_duration*.5) <= duration <= expected_duration+max(3,expected_duration*.2)):
        raise ValueError(f"Media duration {duration:.2f}s does not match {expected_duration:.2f}s")
    for offset in (min(.3,duration/3), max(.1,duration/2)):
        decoded=subprocess.run(["ffmpeg","-v","error","-ss",str(offset),"-i",str(path),
          "-map","0:v:0","-frames:v","1","-f","null","-"],
          capture_output=True,timeout=30)
        if decoded.returncode:
            raise ValueError("Video has undecodable frames")
    return {"duration_seconds":round(duration,3),"width":video.get("width"),"height":video.get("height")}


def atomic_video(output: Path, produce, expected_duration: float | None = None) -> dict:
    """Write .partial.mp4, verify, only then atomically expose final.mp4."""
    output.parent.mkdir(parents=True,exist_ok=True)
    part=output.with_name(output.stem+".partial.mp4")
    part.unlink(missing_ok=True)
    try:
        produce(part)
        result=verify_media(part,expected_duration)
        os.replace(part,output)
        return result
    finally:
        part.unlink(missing_ok=True)


def generate_poster(video: Path, output: Path) -> bool:
    temp=output.with_suffix(".partial.jpg")
    try:
        run_ffmpeg(["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-y",
            "-ss","0.25","-i",str(video),"-frames:v","1","-q:v","4",str(temp)],timeout=35)
        if temp.is_file() and temp.stat().st_size>500:
            os.replace(temp,output)
            return True
    except Exception:
        pass
    finally:
        temp.unlink(missing_ok=True)
    return False


def render_clip(manifest: Path, output: Path, start: float, end: float, vertical: bool = False) -> None:
    if start < 0 or end <= start or end - start > 600:
        raise ValueError("Clip duration must be between 0 and 600 seconds")
    buffered = available_duration(manifest)
    if end > buffered - 0.05:
        raise ValueError(f"Selection goes beyond completed DVR buffer ({buffered:.1f}s)")
    temp = manifest.parent / f"snapshot_{output.stem}.m3u8"
    write_snapshot(manifest, temp, end)
    try:
        vf = ["-vf", "crop=trunc(min(iw\\,ih*9/16)/2)*2:trunc(min(ih\\,iw*16/9)/2)*2:(iw-ow)/2:(ih-oh)/2,scale=720:1280"] if vertical else []
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file,crypto,data", "-i", str(temp), "-ss", f"{start:.3f}", "-t", f"{end-start:.3f}", *vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)]
        atomic_video(output, lambda part: run_ffmpeg(cmd[:-1] + [str(part)], timeout=420, stdout=subprocess.DEVNULL), end-start)
    finally:
        temp.unlink(missing_ok=True)


def finalize_recording(manifest: Path, output: Path) -> None:
    segments = parse_hls(manifest)
    if not segments:
        return
    snapshot = manifest.parent / "archive.m3u8"
    write_snapshot(manifest, snapshot, available_duration(manifest) + .01)
    try:
        def render(part):
            run_ffmpeg(["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-y","-protocol_whitelist","file,crypto,data",
                "-fflags","+genpts","-i",str(snapshot),"-vf","setpts=PTS-STARTPTS",
                "-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p",
                "-c:a","aac","-b:a","128k","-movflags","+faststart","-avoid_negative_ts","make_zero",
                str(part)],timeout=3600)
        return atomic_video(output,render,available_duration(manifest))
    finally:
        snapshot.unlink(missing_ok=True)


def export_video(source: Path, output: Path, start: float, end: float, vertical: bool, captions: list[dict]) -> None:
    if start < 0 or end <= start or end - start > 600:
        raise ValueError("Invalid export trim")
    filters = []
    if vertical:
        filters.append(r"crop=trunc(min(iw\,ih*9/16)/2)*2:trunc(min(ih\,iw*16/9)/2)*2:(iw-ow)/2:(ih-oh)/2")
        filters.append("scale=720:1280")
    subtitle_path = output.with_suffix(".ass")
    if captions:
        def clock(seconds):
            centiseconds = int(round(seconds*100))
            h, rem = divmod(centiseconds,360000)
            m, rem = divmod(rem,6000)
            return f"{h}:{m:02d}:{rem//100:02d}.{rem%100:02d}"
        header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,40,40,240,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        for c in captions:
            begin, finish = float(c["start"]),float(c["end"])
            if begin < 0 or finish <= begin or finish > end-start+0.01: raise ValueError("Caption time is out of clip bounds")
            txt = str(c["text"]).replace("\\","\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n",r"\N").replace("\r", "")
            events.append(f"Dialogue: 0,{clock(begin)},{clock(finish)},Default,,0,0,0,,{txt}")
        subtitle_path.write_text(header+"\n".join(events)+"\n",encoding="utf-8")
        # Pass a filename in the same output directory to avoid filter escaping for absolute paths.
        filters.append("subtitles='"+subtitle_path.name.replace("'", "")+"'")
    cmd = ["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-y","-ss",str(start),"-i",str(source),"-t",str(end-start)]
    if filters: cmd += ["-vf",",".join(filters)]
    cmd += ["-c:v","libx264","-preset","veryfast","-crf","22","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-movflags","+faststart",str(output)]
    try:
        return atomic_video(output, lambda part: run_ffmpeg(cmd[:-1]+[str(part)],timeout=420,cwd=output.parent),end-start)
    finally:
        subtitle_path.unlink(missing_ok=True)
