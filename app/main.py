"""Google-account studio API. Start with one Uvicorn worker."""

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from app import accounts, billing, pipeline

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("CLIPSTUDIO_DATA", ROOT.parent / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
JOBS: dict[str, dict] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=1)
logger = logging.getLogger("clipstudio")


@asynccontextmanager
async def lifespan(app):
    accounts.configure(DATA)
    for path in DATA.glob("*/job.json"):
        try:
            job = json.loads(path.read_text())
            if job["status"] in {"queued", "processing", "uploading"}:
                accounts.finish(job["id"], False)
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
    analysis_minutes: int = Field(default=0, ge=0, le=1440)
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
        for attempt in range(8):
            try:
                temp.write_text(json.dumps(JOBS[job_id]), encoding="utf-8")
                temp.replace(folder / "job.json")
                break
            except PermissionError:
                if attempt == 7:
                    raise
                # Windows may temporarily lock either file while scanning it.
                time.sleep(min(0.05 * (2**attempt), 1))


def caused_by_permission_error(error: BaseException) -> bool:
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def new_job(title: str, options: Options, user=None) -> dict:
    if user is None:
        if not accounts.local_mode():
            raise HTTPException(401, "Sign in with Google to create clips.")
        user = {"id": "local", "unlimited": True}
    if not all(pipeline.requirements()[k] for k in ("ffmpeg", "ffprobe")):
        raise HTTPException(503, "Install FFmpeg and restart the app. See the VS Code setup guide.")
    with LOCK:
        if sum(j["status"] in {"queued", "processing", "uploading"} for j in JOBS.values()) >= 3:
            raise HTTPException(429, "Your queue is full. Wait for a video to finish, then try again.")
        job_id = uuid.uuid4().hex
        (DATA / job_id).mkdir()
        try:
            accounts.reserve(user, job_id)
        except Exception:
            (DATA / job_id).rmdir()
            raise
        job = {
            "id": job_id,
            "title": title,
            "owner_id": user["id"],
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
        with LOCK:
            job = JOBS[job_id]
            if job["progress"] != value or job["message"] != message:
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
        accounts.finish(job_id, True)
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
        if caused_by_permission_error(error):
            message = (
                "Couldn't access the project files. Close programs using the files, "
                "check folder permissions, and try again."
            )
        elif url and source is None:
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
        accounts.finish(job_id, False)
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
    return {
        "status": "ok",
        "requirements": pipeline.requirements(),
        "model": "Whisper tiny/base · " + os.getenv("WHISPER_DEVICE", "cpu"),
    }


@app.get("/api/jobs")
def list_jobs(request: Request):
    with LOCK:
        return sorted(
            (j for j in JOBS.values() if owns(request.state.user, j)),
            key=lambda j: j["created"],
            reverse=True,
        )


def get_job(job_id: str):
    with LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "Video not found.")
        return dict(JOBS[job_id])


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str, request: Request):
    job = get_job(job_id)
    if not owns(request.state.user, job):
        raise HTTPException(404, "Video not found.")
    return job


@app.post("/api/jobs", status_code=202)
def create_link(payload: LinkRequest, request: Request):
    job = new_job("YouTube video", payload, request.state.user)
    save(job["id"], source_url=payload.url, video_id=payload.url.split("v=")[1])
    job = get_job(job["id"])
    EXECUTOR.submit(process, job["id"], url=payload.url)
    return job


@app.post("/api/uploads", status_code=202)
async def create_upload(
    request: Request,
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
    job = new_job(title, options, request.state.user)
    save(job["id"], status="uploading", message="Receiving your video…")
    source = DATA / job["id"] / f"upload{suffix}"
    try:
        total = 0
        with source.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if pipeline.MAX_BYTES and total > pipeline.MAX_BYTES:
                    raise HTTPException(413, "This file exceeds the configured upload limit.")
                stream.write(chunk)
        if total == 0:
            raise HTTPException(422, "This file is empty. Choose a video file.")
    except BaseException:
        source.unlink(missing_ok=True)
        accounts.finish(job["id"], False)
        save(job["id"], status="failed", message="Upload did not finish. Please try again.")
        raise
    finally:
        await file.close()
    save(job["id"], status="queued", message="Waiting in queue…")
    EXECUTOR.submit(process, job["id"], source=source)
    return get_job(job["id"])


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request):
    with LOCK:
        job = read_job(job_id, request)
        if job["status"] in {"queued", "processing", "uploading"}:
            raise HTTPException(409, "Wait until processing finishes before deleting this video.")
        shutil.rmtree(DATA / job_id)
        del JOBS[job_id]


@app.get("/api/jobs/{job_id}/files/{filename}")
def media_file(job_id: str, filename: str, request: Request, download: bool = False):
    job = read_job(job_id, request)
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


def owns(user, job):
    return job.get("owner_id") == user["id"] or (
        not job.get("owner_id") and (user.get("owner") or user.get("local"))
    )


@app.middleware("http")
async def require_account(request: Request, call_next):
    public = {"/api/health", "/api/me", "/api/config", "/api/billing/webhook"}
    if request.method in {"POST", "PUT", "DELETE"} and request.url.path != "/api/billing/webhook":
        origin = request.headers.get("origin")
        expected = os.getenv("PUBLIC_URL", str(request.base_url)).rstrip("/")
        if origin and origin != expected:
            return JSONResponse({"detail": "Invalid request origin."}, status_code=403)

    if request.url.path.startswith("/api/") and request.url.path not in public:
        user = accounts.current(request)
        if not user:
            return JSONResponse({"detail": "Sign in with Google to continue."}, status_code=401)
        request.state.user = user
        if (
            request.method in {"POST", "PUT"}
            and request.url.path
            in {
                "/api/jobs",
                "/api/uploads",
                "/api/uploads/stream",
            }
            and not user["unlimited"]
            and not accounts.summary(user)["trial_available"]
        ):
            return JSONResponse(
                {"detail": "Your free video has been used. Choose a plan to continue."}, status_code=402
            )
    return await call_next(request)


@app.get("/api/config")
def config():
    return {
        "google_ready": accounts.google_ready(),
        "local_mode": accounts.local_mode(),
        "payments_ready": billing.ready(),
        "max_bytes": pipeline.MAX_BYTES,
        "max_duration": pipeline.MAX_DURATION,
        "plans": [
            {"id": "monthly", "price": 5, "period": "month"},
            {"id": "yearly", "price": 60, "period": "year"},
        ],
    }


@app.get("/api/me")
def me(request: Request):
    return accounts.summary(accounts.current(request))


OAUTH = OAuth()
OAUTH.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@app.get("/auth/google")
async def google_login(request: Request):
    if not accounts.google_ready():
        raise HTTPException(503, "Google sign-in needs configuration by the studio owner.")
    callback = os.getenv("PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/") + "/auth/callback"
    return await OAUTH.google.authorize_redirect(request, callback)


@app.get("/auth/callback")
async def google_callback(request: Request):
    try:
        token = await OAUTH.google.authorize_access_token(request)
        uid = accounts.register_google(token["userinfo"])
    except Exception:  # noqa: BLE001 - provider errors must not expose token details
        return RedirectResponse("/?login=failed", status_code=303)
    request.session.clear()
    request.session["token"] = accounts.new_session(uid)
    return RedirectResponse("/", status_code=303)


@app.post("/api/logout")
def logout(request: Request):
    token = request.session.get("token", "")
    import hashlib

    with accounts.database() as db:
        db.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))
    request.session.clear()
    return {"ok": True}


class CheckoutRequest(BaseModel):
    plan: str


@app.post("/api/billing/checkout")
async def checkout(payload: CheckoutRequest, request: Request):
    return await billing.checkout(request.state.user, payload.plan)


@app.post("/api/billing/portal")
async def billing_portal(request: Request):
    user = request.state.user
    if not billing.ready() or not user.get("customer"):
        raise HTTPException(400, "No subscription to manage.")
    result = await billing.stripe(
        "POST",
        "/billing_portal/sessions",
        {"customer": user["customer"], "return_url": os.environ["PUBLIC_URL"]},
    )
    return {"url": result["url"]}


@app.post("/api/billing/webhook")
async def payment_event(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "Webhook too large.")
    body = bytes(body)
    return await billing.webhook(body, request.headers.get("stripe-signature", ""))


@app.put("/api/uploads/stream", status_code=202)
async def stream_upload(request: Request, options: Annotated[Options, Query()]):
    from urllib.parse import unquote

    title = unquote(request.headers.get("x-file-name", "video.mp4"))[:200]
    suffix = Path(title).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(422, "Choose an MP4, MOV, MKV, M4V, or WebM video.")
    job = new_job(title, options, request.state.user)
    source = DATA / job["id"] / ("upload" + suffix)
    save(job["id"], status="uploading", message="Receiving your video…")
    try:
        total = 0
        with source.open("wb") as file:
            async for chunk in request.stream():
                total += len(chunk)
                if pipeline.MAX_BYTES and total > pipeline.MAX_BYTES:
                    raise HTTPException(413, "This video exceeds the configured upload limit.")
                if shutil.disk_usage(DATA).free < len(chunk) + 256 * 1024 * 1024:
                    raise HTTPException(507, "Not enough disk space for this upload.")
                file.write(chunk)
        if not total:
            raise HTTPException(422, "Choose a video that is not empty.")
    except BaseException:
        source.unlink(missing_ok=True)
        accounts.finish(job["id"], False)
        save(job["id"], status="failed", message="Upload did not finish. Please retry.")
        raise
    save(job["id"], status="queued", message="Waiting in queue…")
    EXECUTOR.submit(process, job["id"], source=source)
    return get_job(job["id"])


secret_file = DATA / "session.key"
if not secret_file.exists():
    try:
        with secret_file.open("x") as file:
            file.write(secrets.token_hex(32))
    except FileExistsError:
        pass
app.add_middleware(
    SessionMiddleware,
    secret_key=secret_file.read_text().strip(),
    same_site="lax",
    https_only=os.getenv("PUBLIC_URL", "").startswith("https://"),
    max_age=604800,
)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="studio")
