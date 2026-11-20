import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
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
