"""Single-user local app. Start with one Uvicorn worker."""

import json
import logging
import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import pipeline

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("CLIPSTUDIO_DATA", ROOT.parent / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
JOBS: dict[str, dict] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=1)
logger = logging.getLogger("clipstudio")


@asynccontextmanager
async def lifespan(app):
    for path in DATA.glob("*/job.json"):
        try:
            job = json.loads(path.read_text())
            if job["status"] in {"queued", "processing", "uploading"}:
                job.update(status="failed", message="The app restarted during processing. Please try again.")
                path.write_text(json.dumps(job))
            JOBS[job["id"]] = job
        except (ValueError, KeyError, OSError):
            logger.warning("Could not restore job %s", path.parent.name)
    yield
    EXECUTOR.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Clipstudio", lifespan=lifespan)


class Options(BaseModel):
    length: int = Field(default=30, ge=15, le=60)
    count: int = Field(default=3, ge=1, le=5)
    framing: str = "fit"
    captions: bool = True
    speed: str = Field(default="balanced", pattern="^(fast|balanced)$")
    analysis_minutes: int = Field(default=0, ge=0, le=120)
    resolution: int = Field(default=1080)

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value):
        if value not in {720, 1080}:
            raise ValueError("Choose 720p or 1080p.")
        return value

    @field_validator("framing")
    @classmethod
    def validate_framing(cls, value):
        if value not in {"fit", "crop"}:
            raise ValueError("Choose fit or crop framing.")
        return value


class LinkRequest(Options):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return pipeline.youtube_url(value)


def save(job_id: str, **changes):
    with LOCK:
        JOBS[job_id].update(changes)
        folder = DATA / job_id
        temp = folder / "job.tmp"
        temp.write_text(json.dumps(JOBS[job_id]), encoding="utf-8")
        temp.replace(folder / "job.json")


def new_job(title: str, options: Options) -> dict:
    if not all(pipeline.requirements()[k] for k in ("ffmpeg", "ffprobe")):
        raise HTTPException(503, "Install FFmpeg and restart the app. See the VS Code setup guide.")
    with LOCK:
        if sum(j["status"] in {"queued", "processing", "uploading"} for j in JOBS.values()) >= 3:
            raise HTTPException(429, "Your queue is full. Wait for a video to finish, then try again.")
        job_id = uuid.uuid4().hex
        (DATA / job_id).mkdir()
        job = {
            "id": job_id,
            "title": title,
            "status": "queued",
            "progress": 0,
            "message": "Waiting in queue…",
            "created": datetime.now(UTC).isoformat(),
            "options": options.model_dump(),
            "clips": [],
        }
        JOBS[job_id] = job
        save(job_id)
        return dict(job)


def process(job_id: str, url: str | None = None, source: Path | None = None):
    folder = DATA / job_id

    def progress(value, message):
        save(job_id, progress=value, message=message)

    def metadata(**values):
        save(job_id, **values)

    try:
        save(job_id, status="processing", progress=5, message="Getting your video ready…")
        if url:
            source, title = pipeline.download(url, folder, progress, metadata=metadata)
            save(job_id, title=title)
        media = pipeline.probe(source)
        progress(25, "Checking audio and video…")
        options = JOBS[job_id]["options"]
        scan_seconds = options.get("analysis_minutes", 0) * 60
        analyzed_duration = min(media["duration"], scan_seconds or media["duration"])
        save(job_id, source_duration=media["duration"], analyzed_duration=analyzed_duration)
        segments = (
            pipeline.transcribe(source, folder, progress, options.get("speed", "balanced"), scan_seconds)
            if media["audio"]
            else []
        )
        (folder / "transcript.json").write_text(json.dumps(segments), encoding="utf-8")
        clips = pipeline.choose_clips(segments, analyzed_duration, options["length"], options["count"])
        (folder / "transcript.txt").write_text("\n".join(s["text"] for s in segments), encoding="utf-8")
        rendered = []
        for i, clip in enumerate(clips, 1):
            progress(60 + int((i - 1) / len(clips) * 36), f"Rendering clip {i} of {len(clips)}…")
            rendered.append(
                pipeline.render(
                    source,
                    clip,
                    folder,
                    i,
                    options["framing"],
                    options["captions"],
                    options.get("speed", "balanced"),
                    options.get("resolution", 1080),
                )
            )
        save(
            job_id,
            status="completed",
            progress=100,
            message="Your clips are ready.",
            clips=rendered,
            source_duration=media["duration"],
            speech_detected=bool(segments),
            transcript_available=True,
        )
    except Exception as error:
        logger.exception("Processing failed for %s", job_id)
        if url and source is None:
            message = (
                "YouTube couldn't provide this video. It may require sign-in, be restricted, or block downloads. "
                "Upload a local copy of the video, or try another public link."
            )
        elif isinstance(error, ValueError):
            message = str(error)
        elif isinstance(error, (ConnectionError, OSError)) or "huggingface" in str(error).lower():
            message = "Couldn't load the speech model or media. Check your internet connection and free disk space, then retry."
        else:
            message = "Video processing failed. Check the terminal for details, confirm FFmpeg is installed, and try an MP4 file."
        save(job_id, status="failed", message=message)
    finally:
        # Keep only exports and transcript; remove large source and partial downloads.
        for path in folder.iterdir():
            if path.name.startswith(("source.", "upload.", "audio.")) or path.suffix in {
                ".part",
                ".ytdl",
                ".ass",
            }:
                path.unlink(missing_ok=True)


@app.get("/api/health")
def health():
    return {"status": "ok", "requirements": pipeline.requirements(), "model": "Whisper base · CPU int8"}


@app.get("/api/jobs")
def list_jobs():
    with LOCK:
        return sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "Video not found.")
        return dict(JOBS[job_id])


@app.post("/api/jobs", status_code=202)
def create_link(request: LinkRequest):
    job = new_job("YouTube video", request)
    save(job["id"], source_url=request.url, video_id=request.url.split("v=")[1])
    job = get_job(job["id"])
    EXECUTOR.submit(process, job["id"], url=request.url)
    return job


@app.post("/api/uploads", status_code=202)
async def create_upload(
    file: Annotated[UploadFile, File()],
    length: int = Form(30),
    count: int = Form(3),
    framing: str = Form("fit"),
    captions: bool = Form(True),
    speed: str = Form("balanced"),
    analysis_minutes: int = Form(0),
    resolution: int = Form(1080),
):
    try:
        options = Options(
            length=length,
            count=count,
            framing=framing,
            captions=captions,
            speed=speed,
            analysis_minutes=analysis_minutes,
            resolution=resolution,
        )
    except ValueError:
        raise HTTPException(422, "Invalid clip settings.") from None
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(422, "Choose an MP4, MOV, MKV, M4V, or WebM video.")
    title = (file.filename or "Uploaded video")[:200]
    job = new_job(title, options)
    save(job["id"], status="uploading", message="Receiving your video…")
    source = DATA / job["id"] / f"upload{suffix}"
    try:
        total = 0
        with source.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > pipeline.MAX_BYTES:
                    raise HTTPException(413, "This file exceeds the 750 MB upload limit.")
                stream.write(chunk)
        if total == 0:
            raise HTTPException(422, "This file is empty. Choose a video file.")
    except BaseException:
        source.unlink(missing_ok=True)
        save(job["id"], status="failed", message="Upload did not finish. Try a video under 750 MB.")
        raise
    finally:
        await file.close()
    save(job["id"], status="queued", message="Waiting in queue…")
    EXECUTOR.submit(process, job["id"], source=source)
    return get_job(job["id"])


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    with LOCK:
        job = get_job(job_id)
        if job["status"] in {"queued", "processing", "uploading"}:
            raise HTTPException(409, "Wait until processing finishes before deleting this video.")
        shutil.rmtree(DATA / job_id)
        del JOBS[job_id]


@app.get("/api/jobs/{job_id}/files/{filename}")
def media_file(job_id: str, filename: str, download: bool = False):
    job = get_job(job_id)
    if filename == "transcript.txt" and job["status"] == "completed" and (DATA / job_id / filename).is_file():
        return FileResponse(DATA / job_id / filename, filename=filename, media_type="text/plain")
    allowed = {c[key] for c in job["clips"] for key in ("file", "poster")}
    if (
        job["status"] != "completed"
        or filename not in allowed
        or not re.fullmatch(r"clip-\d{2}\.(mp4|jpg)", filename)
    ):
        raise HTTPException(404, "Clip not found.")
    return FileResponse(
        DATA / job_id / filename,
        filename=filename if download else None,
        media_type="video/mp4" if filename.endswith(".mp4") else "image/jpeg",
    )


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="studio")
