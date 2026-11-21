import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSTUDIO_LOCAL_MODE", "1")
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "JOBS", {})
    with TestClient(main.app) as client:
        yield client


def test_rejects_arbitrary_urls_and_invalid_settings(client):
    assert client.post("/api/jobs", json={"url": "http://localhost:8000"}).status_code == 422
    assert (
        client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ", "count": 50}).status_code == 422
    )
    assert client.post("/api/uploads", files={"file": ("bad.txt", b"no")}).status_code == 422
    assert client.post("/api/uploads", files={"file": ("empty.mp4", b"")}).status_code == 422


def test_upload_limit_and_cleanup(client, monkeypatch):
    monkeypatch.setattr(main.pipeline, "MAX_BYTES", 3)
    result = client.post("/api/uploads", files={"file": ("big.mp4", b"12345")})
    assert result.status_code == 413
    job = client.get("/api/jobs").json()[0]
    assert job["status"] == "failed"
    assert not list(main.DATA.glob("*/upload*"))
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get("/api/jobs").json() == []


def test_queue_and_export_access_are_guarded(client):
    jobs = [main.new_job("test", main.Options()) for _ in range(3)]
    assert client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}).status_code == 429
    assert client.delete(f"/api/jobs/{jobs[0]['id']}").status_code == 409
    assert client.get(f"/api/jobs/{jobs[0]['id']}/files/job.json").status_code == 404
    assert client.get("/api/jobs/not-real").status_code == 404


def test_restart_marks_interrupted_jobs_failed(client):
    job = main.new_job("test", main.Options())
    main.JOBS.clear()
    # A second startup reads the atomic manifest left by the first process.
    with TestClient(main.app) as restarted:
        restored = restarted.get(f"/api/jobs/{job['id']}").json()
        assert restored["status"] == "failed"
        assert "restarted" in restored["message"]


def test_save_retries_transient_access_denials(client, monkeypatch):
    job = main.new_job("Video", main.Options())
    manifest = main.DATA / job["id"] / "job.json"
    old = json.loads(manifest.read_text(encoding="utf-8"))
    replace = Path.replace
    attempts = []

    def temporarily_locked(path, target):
        attempts.append(1)
        if len(attempts) <= 2:
            assert json.loads(manifest.read_text(encoding="utf-8")) == old
            raise PermissionError("The file is in use")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", temporarily_locked)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    main.save(job["id"], progress=20, message="Downloading your video…")
    assert len(attempts) == 3
    assert json.loads(manifest.read_text(encoding="utf-8"))["progress"] == 20


def test_save_retries_when_temporary_file_cannot_be_opened(client, monkeypatch):
    job = main.new_job("Video", main.Options())
    write_text = Path.write_text
    attempts = []

    def temporarily_locked(path, *args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("The file is in use")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", temporarily_locked)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    main.save(job["id"], progress=20)
    assert len(attempts) == 2
    manifest = main.DATA / job["id"] / "job.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["progress"] == 20


def test_save_propagates_persistent_access_denial_without_corrupting_manifest(client, monkeypatch):
    job = main.new_job("Video", main.Options())
    manifest = main.DATA / job["id"] / "job.json"
    original = manifest.read_bytes()
    attempts = []

    def locked(path, target):
        attempts.append(1)
        raise PermissionError("The file is in use")

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        main.save(job["id"], progress=20)
    assert 1 < len(attempts) <= 10
    assert manifest.read_bytes() == original


def test_wrapped_permission_error_reports_file_access_problem(client, monkeypatch):
    job = main.new_job("Video", main.Options())

    def blocked_download(*args, **kwargs):
        try:
            raise PermissionError("The file is in use")
        except PermissionError as exc:
            raise RuntimeError("Download failed") from exc

    monkeypatch.setattr(main.pipeline, "download", blocked_download)
    main.process(job["id"], url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    result = client.get(f"/api/jobs/{job['id']}").json()
    assert result["status"] == "failed"
    assert "file" in result["message"].lower()
    assert "YouTube couldn't provide" not in result["message"]


def test_download_continues_after_manifest_temporarily_locked(client, monkeypatch):
    job = main.new_job("Video", main.Options())
    replace = Path.replace
    attempts = []
    downloading = False

    def locked_during_download(path, target):
        if downloading:
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError("The file is in use")
        return replace(path, target)

    def download(url, folder, progress, metadata):
        nonlocal downloading
        downloading = True
        try:
            progress(8, "Downloading your video…")
        except PermissionError as exc:
            raise RuntimeError("Download failed") from exc
        finally:
            downloading = False
        source = folder / "source.mp4"
        source.write_bytes(b"synthetic")
        return source, "Video"

    monkeypatch.setattr(Path, "replace", locked_during_download)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(main.pipeline, "download", download)
    monkeypatch.setattr(main.pipeline, "probe", lambda source: {"duration": 10, "audio": False})
    monkeypatch.setattr(
        main.pipeline, "render", lambda *args: {"file": "clip-01.mp4", "poster": "clip-01.jpg"}
    )
    main.process(job["id"], url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert len(attempts) == 3
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"


def test_duplicate_download_progress_is_not_rewritten(client, monkeypatch):
    job = main.new_job("Video", main.Options())
    actual_save = main.save
    saves = []

    def track_save(job_id, **changes):
        if changes.get("message") == "Downloading your video…":
            saves.append(changes["progress"])
        return actual_save(job_id, **changes)

    def download(url, folder, progress, metadata):
        for value in (8, 8, 8, 9, 9):
            progress(value, "Downloading your video…")
        source = folder / "source.mp4"
        source.write_bytes(b"synthetic")
        return source, "Video"

    monkeypatch.setattr(main, "save", track_save)
    monkeypatch.setattr(main.pipeline, "download", download)
    monkeypatch.setattr(main.pipeline, "probe", lambda source: {"duration": 10, "audio": False})
    monkeypatch.setattr(
        main.pipeline, "render", lambda *args: {"file": "clip-01.mp4", "poster": "clip-01.jpg"}
    )
    main.process(job["id"], url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
    assert saves == [8, 9]


def test_speed_options_reject_invalid_settings(client):
    url = "https://youtu.be/dQw4w9WgXcQ"
    for options in ({"speed": "turbo"}, {"resolution": 480}, {"analysis_minutes": -1}):
        assert client.post("/api/jobs", json={"url": url, **options}).status_code == 422


def test_source_metadata_and_transcript_are_persisted(client, monkeypatch):
    job = main.new_job("Video", main.Options(speed="fast", analysis_minutes=5, resolution=720))
    folder = main.DATA / job["id"]

    def download(url, folder, progress, metadata):
        source = folder / "source.mp4"
        source.write_bytes(b"synthetic")
        metadata(title="Original title", channel="Original channel", source_duration=1200)
        return source, "Original title"

    monkeypatch.setattr(main.pipeline, "download", download)
    monkeypatch.setattr(main.pipeline, "probe", lambda source: {"duration": 1200, "audio": False})
    seen = []

    def render(source, clip, folder, number, framing, captions, speed, resolution):
        seen.append((clip["end"], speed, resolution))
        return {"file": "clip-01.mp4", "poster": "clip-01.jpg"}

    monkeypatch.setattr(main.pipeline, "render", render)
    main.process(job["id"], url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    restored = client.get(f"/api/jobs/{job['id']}").json()
    assert restored["status"] == "completed"
    assert restored["channel"] == "Original channel"
    assert restored["title"] == "Original title"
    assert restored["analyzed_duration"] == 300
    assert restored["source_duration"] == 1200
    assert seen == [(30, "fast", 720)]
    assert client.get(f"/api/jobs/{job['id']}/files/transcript.txt").status_code == 200
    assert not (folder / "source.mp4").exists()


def test_job_completes_when_word_timestamps_differ_from_transcript(client, monkeypatch):
    job = main.new_job("Video", main.Options())
    source = main.DATA / job["id"] / "upload.mp4"
    source.write_bytes(b"synthetic")
    transcript = [
        {
            "start": 0,
            "end": 10,
            "text": "hello everyone",
            "words": [
                {"start": 0, "end": 4, "word": "hello"},
                {"start": 4, "end": 9, "word": "every"},
            ],
        }
    ]
    monkeypatch.setattr(main.pipeline, "probe", lambda source: {"duration": 10, "audio": True})
    monkeypatch.setattr(main.pipeline, "transcribe", lambda *args: transcript)
    rendered = []

    def render(source, clip, *args):
        rendered.append(clip)
        return {"file": "clip-01.mp4", "poster": "clip-01.jpg"}

    monkeypatch.setattr(main.pipeline, "render", render)
    main.process(job["id"], source=source)
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
    assert rendered[0]["text"] == "hello every"
    assert rendered[0]["words"] == transcript[0]["words"]
