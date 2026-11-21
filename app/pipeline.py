"""Local transcription, transparent highlight ranking, and vertical video rendering."""

import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_DURATION = int(os.getenv("CLIPSTUDIO_MAX_DURATION", "0"))
MAX_BYTES = int(os.getenv("CLIPSTUDIO_MAX_BYTES", "0"))


def youtube_url(value: str) -> str:
    """Accept only single YouTube video identifiers, never arbitrary download URLs."""
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port:
            raise ValueError
        host = (parsed.hostname or "").lower()
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/shorts/", "/live/")):
                video_id = parsed.path.split("/")[2]
            else:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError
        return f"https://www.youtube.com/watch?v={video_id}"
    except (ValueError, IndexError):
        raise ValueError(
            "Paste a valid YouTube video link, such as https://www.youtube.com/watch?v=VIDEO_ID."
        ) from None


def run(command: list[str], timeout: int = 1200) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Video processing failed: {result.stderr[-1800:]}")
    return result.stdout


def probe(source: Path) -> dict:
    info = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov,matroska,webm",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(source),
            ],
            30,
        )
    )
    streams = info.get("streams", [])
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or (duration < 3 or (MAX_DURATION and duration > MAX_DURATION)):
        raise ValueError("Use a video at least 3 seconds long and within the configured duration limit.")
    if not any(s["codec_type"] == "video" for s in streams):
        raise ValueError("This file has no video track. Upload an MP4, MOV, MKV, or WebM video.")
    return {"duration": duration, "audio": any(s["codec_type"] == "audio" for s in streams)}


def download(url: str, folder: Path, progress, metadata=None) -> tuple[Path, str]:
    from yt_dlp import YoutubeDL

    def hook(event):
        if event["status"] == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            downloaded = event.get("downloaded_bytes", 0)
            if MAX_BYTES and downloaded > MAX_BYTES:
                raise ValueError("This video exceeds the configured size download limit.")
            percent = min(1, downloaded / total) if total else 0
            progress(8 + int(percent * 15), "Downloading your video…")

    def accept(info, *, incomplete=False):
        if metadata and not incomplete:
            metadata(
                title=info.get("title") or "YouTube video",
                channel=info.get("uploader") or info.get("channel") or "",
                source_duration=info.get("duration"),
            )
        if info.get("is_live"):
            return "Live streams must finish before they can be clipped."
        if MAX_DURATION and (info.get("duration") or 0) > MAX_DURATION:
            return "Use a video within the configured duration limit."
        if MAX_BYTES and (info.get("filesize") or info.get("filesize_approx") or 0) > MAX_BYTES:
            return "Use a video under configured size."
        return None

    options = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]",
        "outtmpl": str(folder / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "max_filesize": MAX_BYTES or None,
        "match_filter": accept,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "js_runtimes": {"node": {}},
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    sources = [p for p in folder.glob("source.*") if p.suffix in {".mp4", ".webm", ".mkv"}]
    if not sources:
        raise ValueError("YouTube did not return a downloadable video. Try uploading a local copy.")
    source = max(sources, key=lambda p: p.stat().st_size)
    if MAX_BYTES and source.stat().st_size > MAX_BYTES:
        raise ValueError("This video exceeds the configured size download limit.")
    return source, info.get("title", "YouTube video")


_MODELS = {}


def transcribe(source: Path, folder: Path, progress, speed="balanced", scan_seconds=0) -> list[dict]:
    from faster_whisper import WhisperModel

    progress(29, "Preparing local speech recognition (first run downloads the model)…")
    audio = folder / "audio.wav"
    command = ["ffmpeg", "-y", "-v", "error", "-i", str(source)]
    if scan_seconds:
        command += ["-t", str(scan_seconds)]
    run(command + ["-vn", "-ar", "16000", "-ac", "1", str(audio)])
    model_name = "tiny" if speed == "fast" else "base"
    if model_name not in _MODELS:
        _MODELS[model_name] = WhisperModel(
            model_name,
            device=os.getenv("WHISPER_DEVICE", "cpu"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            cpu_threads=min(8, os.cpu_count() or 4),
        )
    started = time.monotonic()
    segments, info = _MODELS[model_name].transcribe(
        str(audio), word_timestamps=True, vad_filter=True, beam_size=1 if speed == "fast" else 3
    )

    result = []
    for segment in segments:
        progress(
            32 + int(min(1, segment.end / max(info.duration, 1)) * 26),
            f"Transcribing {int(segment.end // 60)}:{int(segment.end % 60):02} / "
            f"{int(info.duration // 60)}:{int(info.duration % 60):02} of audio · "
            f"{int(time.monotonic() - started)}s elapsed",
        )
        result.append(
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "words": [
                    {"start": w.start, "end": w.end, "word": w.word.strip()} for w in segment.words or []
                ],
            }
        )
    audio.unlink(missing_ok=True)
    return result


def choose_clips(segments: list[dict], duration: float, length: int, count: int) -> list[dict]:
    """Rank sentence windows using speech density, distinctive words and hook language.

    Scores are internal ordering signals, never predictions of views or virality.
    """
    if not segments:
        return [
            {
                "start": 0,
                "end": min(length, duration),
                "title": "Opening moment",
                "text": "",
                "reason": "No speech detected · opening excerpt",
                "words": [],
            }
        ]
    frequencies = Counter(re.findall(r"\w+", " ".join(s["text"] for s in segments).lower()))
    candidates = []
    for i, first in enumerate(segments):
        start = max(0, first["start"] - 0.12)
        end_limit = min(duration, start + length)
        included = []
        for segment in islice(segments, i, None):
            if segment["start"] >= end_limit:
                break
            included.append(segment)
        if not included:
            continue
        complete = [s for s in included if s["end"] <= end_limit]
        end = complete[-1]["end"] + 0.15 if complete else end_limit
        end = min(end_limit, end, duration)
        if end - start < min(8, duration * 0.5):
            continue
        words = [w for s in included for w in s["words"] if w["start"] >= start and w["end"] <= end]
        text = " ".join(w["word"] for w in words) if words else " ".join(s["text"] for s in complete)
        tokens = re.findall(r"\w+", text.lower())
        if not tokens:
            continue
        # Word-level text may tokenize differently from the full transcript.
        distinctive = sum(1 / frequencies.get(t, 1) for t in set(tokens)) / math.sqrt(len(tokens))
        hook = bool(
            re.search(
                r"\b(why|how|secret|mistake|never|imagine|because|discovered|actually)\b",
                text[:150],
                re.IGNORECASE,
            )
        )
        density = min(1, len(tokens) / max(end - start, 1) / 2)
        score = distinctive + hook * 0.7 + density + min(1, (end - start) / length)
        candidates.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "text": text,
                "words": words,
                "title": " ".join(text.split()[:9]).rstrip(".,!?:") or "A moment worth sharing",
                "reason": "Hook language · clear speech" if hook else "Distinctive topic · clear speech",
                "score": score,
            }
        )
    selected = []
    for candidate in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if all(min(candidate["end"], s["end"]) - max(candidate["start"], s["start"]) <= 0 for s in selected):
            selected.append(candidate)
            if len(selected) == count:
                break
    if not selected:
        return [
            {
                "start": 0,
                "end": min(length, duration),
                "title": "Opening moment",
                "text": "",
                "reason": "Short source · opening excerpt",
                "words": [],
            }
        ]
    return sorted(selected, key=lambda c: c["start"])


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def write_subtitles(clip: dict, destination: Path):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,64,&H00FFFFFF,&H000000FF,&H00202020,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,80,80,360,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = [header]
    words = clip["words"]
    for index in range(0, len(words), 5):
        group = words[index : index + 5]
        # Strip ASS control syntax from untrusted spoken text.
        text = " ".join(w["word"] for w in group)
        text = re.sub(r"[{}\\\r\n]", "", text)
        start = ass_time(group[0]["start"] - clip["start"])
        end = ass_time(min(group[-1]["end"], clip["end"]) - clip["start"])
        rows.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")
    destination.write_text("".join(rows), encoding="utf-8")


def render(
    source: Path,
    clip: dict,
    folder: Path,
    number: int,
    framing: str,
    captions: bool,
    speed="balanced",
    resolution=1080,
) -> dict:
    filename = f"clip-{number:02}.mp4"
    subtitles = folder / f"clip-{number:02}.ass"
    write_subtitles(clip, subtitles)
    if framing == "crop":
        filters = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v]"
    else:
        filters = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,scale=1080:1920[blur];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[front];"
            "[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        )
    if captions and clip["words"]:
        # Run in the job directory so the subtitle filter receives a safe generated basename.
        filters += f";[v]ass={subtitles.name}[out]"
    else:
        filters += ";[v]null[out]"
    filters = "[0:v]fps=30[input];" + filters.replace("[0:v]", "[input]")
    if resolution == 720:
        filters = filters.replace("[out]", "[full]") + ";[full]scale=720:1280[out]"
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-threads",
        "2",
        "-ss",
        str(clip["start"]),
        "-i",
        str(source),
        "-t",
        str(clip["end"] - clip["start"]),
        "-filter_complex_threads",
        "2",
        "-filter_complex",
        filters,
        "-map",
        "[out]",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "superfast" if speed == "fast" else "veryfast",
        "-crf",
        "22",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        filename,
    ]
    result = subprocess.run(command, cwd=folder, capture_output=True, text=True, check=False, timeout=1200)
    if result.returncode:
        raise RuntimeError(f"Could not render clip: {result.stderr[-1200:]}")
    poster = f"clip-{number:02}.jpg"
    run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(folder / filename),
            "-frames:v",
            "1",
            "-update",
            "1",
            "-vf",
            "scale=360:640",
            str(folder / poster),
        ],
        60,
    )
    return {k: v for k, v in clip.items() if k not in {"score", "words"}} | {
        "file": filename,
        "poster": poster,
        "duration": round(clip["end"] - clip["start"], 1),
    }


def requirements() -> dict:
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "javascript": bool(shutil.which("node") or shutil.which("deno")),
    }
