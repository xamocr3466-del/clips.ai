"""Google identity, opaque sessions, and durable per-account entitlements."""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request

OWNER_EMAIL = "xamoxm366@gmail.com"
DB_PATH = Path("data/accounts.sqlite3")


def configure(folder):
    global DB_PATH
    DB_PATH = folder / "accounts.sqlite3"
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
          paid_until INTEGER NOT NULL DEFAULT 0, customer TEXT, subscription TEXT);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, uid TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS usage (job TEXT PRIMARY KEY, uid TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY);
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "qr_paid_until" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN qr_paid_until INTEGER NOT NULL DEFAULT 0")


@contextmanager
def database():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def local_mode():
    return os.getenv("CLIPSTUDIO_LOCAL_MODE") == "1"


def google_ready():
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def register_google(claims):
    if claims.get("email_verified") is not True or not claims.get("sub") or not claims.get("email"):
        raise HTTPException(401, "Google must verify your email address.")
    uid = claims["sub"]
    email = claims["email"].strip().lower()
    with database() as db:
        db.execute(
            "INSERT INTO users(id,email,name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email,name=excluded.name",
            (uid, email, claims.get("name", email)[:200]),
        )
    return uid


def new_session(uid):
    token = secrets.token_urlsafe(32)
    with database() as db:
        db.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), uid, int(time.time()) + 604800),
        )
    return token


def current(request: Request):
    if local_mode():
        return {
            "id": "local",
            "email": "",
            "name": "Local studio",
            "owner": False,
            "unlimited": True,
            "local": True,
        }
    token = request.session.get("token", "")
    if not token:
        return None
    with database() as db:
        row = db.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id=sessions.uid WHERE token=? AND expires>?",
            (hashlib.sha256(token.encode()).hexdigest(), int(time.time())),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["owner"] = result["email"] == OWNER_EMAIL
    result["unlimited"] = result["owner"] or max(result["paid_until"], result["qr_paid_until"]) > time.time()
    return result


def reserve(user, job):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        used = db.execute(
            "SELECT count(*) FROM usage WHERE uid=? AND state IN ('reserved','completed')", (user["id"],)
        ).fetchone()[0]
        if not user["unlimited"] and used:
            raise HTTPException(
                402, "Your free video has been used. Choose a monthly or yearly plan to continue."
            )
        db.execute("INSERT INTO usage VALUES(?,?,'reserved')", (job, user["id"]))


def finish(job, success):
    with database() as db:
        db.execute("UPDATE usage SET state=? WHERE job=?", ("completed" if success else "failed", job))


def summary(user):
    if not user:
        return None
    with database() as db:
        used = db.execute(
            "SELECT count(*) FROM usage WHERE uid=? AND state IN ('reserved','completed')", (user["id"],)
        ).fetchone()[0]
    return {k: user.get(k) for k in ("id", "email", "name", "owner", "unlimited", "local")} | {
        "paid_until": max(user.get("paid_until") or 0, user.get("qr_paid_until") or 0),
        "trial_available": not bool(used),
        "has_subscription": bool(user.get("subscription")),
    }
