import json
import shutil
import subprocess
from itertools import pairwise

import pytest

from app import pipeline


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=123",
        "https://youtu.be/dQw4w9WgXcQ?t=20",
        "https://m.youtube.com/shorts/dQw4w9WgXcQ",
        "https://youtube.com/live/dQw4w9WgXcQ",
    ],
)
def test_youtube_urls_canonicalized(url):
    assert pipeline.youtube_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
        "https://evil.com",
        "https://youtube.com/playlist?list=hi",
        "https://youtu.be/invalid",
        "https://user@youtube.com/watch?v=dQw4w9WgXcQ",
        "http://127.0.0.1",
        "https://youtube.com:8000/watch?v=dQw4w9WgXcQ",
    ],
)
def test_download_target_is_restricted(url):
    with pytest.raises(ValueError):
        pipeline.youtube_url(url)


def segments():
    return [
        {
            "start": float(i),
            "end": float(i + 4),
            "text": "Why this discovery can change the way you think about science.",
            "words": [
                {"start": i + j * 0.3, "end": i + j * 0.3 + 0.25, "word": w}
                for j, w in enumerate(
                    [
                        "Why",
                        "this",
                        "discovery",
                        "can",
                        "change",
                        "the",
                        "way",
                        "you",
                        "think",
                        "about",
                        "science.",
                    ]
                )
            ],
        }
        for i in range(0, 120, 5)
    ]


def test_highlights_are_bounded_and_do_not_overlap():
    clips = pipeline.choose_clips(segments(), 120, 30, 3)
    assert len(clips) == 3
    for clip in clips:
        assert 0 <= clip["start"] < clip["end"] <= 120
        assert clip["end"] - clip["start"] <= 30.01
        assert all(clip["start"] <= w["start"] < w["end"] <= clip["end"] for w in clip["words"])
    for previous, current in pairwise(clips):
        assert previous["end"] <= current["start"]


def test_silent_and_short_videos_get_a_bounded_excerpt():
    clips = pipeline.choose_clips([], 3.5, 30, 5)
    assert len(clips) == 1
    assert clips[0]["end"] == 3.5
    assert clips[0]["words"] == []


def test_subtitles_escape_ass_commands_and_use_clip_relative_times(tmp_path):
    clip = {"start": 10, "end": 20, "words": [{"start": 11, "end": 13, "word": "{\\pos(0,0)}hello\\Nworld"}]}
    output = tmp_path / "test.ass"
    pipeline.write_subtitles(clip, output)
    event = output.read_text().split("Dialogue: ")[1]
    assert "0:00:01.00,0:00:03.00" in event
    assert "{" not in event and "\\" not in event


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize(
    "framing,audio,captions,resolution",
    [("fit", True, True, 1080), ("crop", False, False, 1080), ("fit", True, True, 720)],
)
def test_real_ffmpeg_exports_vertical_playable_clips(tmp_path, framing, audio, captions, resolution):
    source = tmp_path / "source.mp4"
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=30:d=3"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)]
    subprocess.run(command, check=True)
    assert pipeline.probe(source)["audio"] == audio
    clip = {
        "start": 0,
        "end": 3,
        "title": "Test",
        "text": "Caption test",
        "reason": "Test",
        "words": [{"start": 0.5, "end": 2, "word": "Caption test"}],
    }
    result = pipeline.render(
        source, clip, tmp_path, 1, framing, captions, "fast" if resolution == 720 else "balanced", resolution
    )
    info = json.loads(
        pipeline.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(tmp_path / result["file"])]
        )
    )
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["codec_name"]) == (
        resolution,
        resolution * 16 // 9,
        "h264",
    )
    assert video["pix_fmt"] == "yuv420p"
    assert (tmp_path / result["poster"]).is_file()
    assert any(s["codec_type"] == "audio" for s in info["streams"]) == audio


def test_reject_disguised_playlist(tmp_path):
    source = tmp_path / "upload.mp4"
    source.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:5\nhttps://example.com/media.ts\n")
    with pytest.raises(RuntimeError):
        pipeline.probe(source)


def test_whisper_audio_decoder_compatibility(tmp_path):
    """Exercise the PyAV API Whisper actually calls, without downloading a model."""
    import wave

    from faster_whisper.audio import decode_audio

    source = tmp_path / "silence.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    decoded = decode_audio(str(source))
    assert len(decoded) == 16000
