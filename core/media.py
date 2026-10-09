"""Small and deliberately explicit FFmpeg pipeline for a single-recording POC."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Iterable

@dataclass(frozen=True)
class Segment:
    filename: str
    duration: float
    start: float


def parse_hls(manifest: Path) -> list[Segment]:
    if not manifest.exists():
        return []
    segments = []
    duration = None
    position = 0.0
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            if not re.fullmatch(r"seg_\d{6}\.ts", line):
                raise ValueError("Unsafe segment filename in playlist")
            segments.append(Segment(line, duration, position))
            position += duration
            duration = None
    return segments


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
        subprocess.run(cmd, check=True, timeout=420, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    finally:
        temp.unlink(missing_ok=True)


def finalize_recording(manifest: Path, output: Path) -> None:
    segments = parse_hls(manifest)
    if not segments:
        return
    snapshot = manifest.parent / "archive.m3u8"
    write_snapshot(manifest, snapshot, available_duration(manifest) + .01)
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file,crypto,data", "-i", str(snapshot), "-c", "copy", "-movflags", "+faststart", str(output)], check=True, timeout=1800, stderr=subprocess.PIPE)
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
        subprocess.run(cmd,check=True,timeout=420,cwd=output.parent,stderr=subprocess.PIPE)
    finally:
        subtitle_path.unlink(missing_ok=True)
