"""Single-use Razorpay UPI payments; access follows verified captured payment events."""

import calendar
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app import accounts

PLANS = {"monthly": (10000, 1), "yearly": (100000, 12)}  # INR paise, calendar months


def ready():
    return all(
        os.getenv(k) for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")
    ) and os.getenv("PUBLIC_URL", "").startswith("https://")


def configure():
    with accounts.database() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS qr_payments (
            id TEXT PRIMARY KEY, uid TEXT NOT NULL, plan TEXT NOT NULL, amount INTEGER NOT NULL,
            qr_id TEXT UNIQUE, image_url TEXT, expires INTEGER NOT NULL, status TEXT NOT NULL,
            payment_id TEXT UNIQUE, paid_until INTEGER
        )""")


def add_months(timestamp, months):
    date = datetime.fromtimestamp(timestamp, UTC)
    index = date.year * 12 + date.month - 1 + months
    year, month0 = divmod(index, 12)
    return int(
        date.replace(
            year=year, month=month0 + 1, day=min(date.day, calendar.monthrange(year, month0 + 1)[1])
        ).timestamp()
    )


async def razorpay(method, path, payload=None):
    base = os.getenv("RAZORPAY_API_BASE", "https://api.razorpay.com/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method,
                base + path,
                json=payload,
                auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]),
            )
        if response.status_code >= 400:
            raise HTTPException(502, "Payment provider unavailable. Please try again later.")
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Payment provider unavailable. Please try again later.") from None


def safe_image(url):
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (host == "rzp.io" or host.endswith(".razorpay.com") or host == "razorpay.com")
    )


async def create(user, plan):
    if plan not in PLANS:
        raise HTTPException(422, "Choose monthly or yearly.")
    if not ready():
        raise HTTPException(503, "Payments are not activated yet. Please contact the studio owner.")
    if user.get("local") or user.get("unlimited"):
        raise HTTPException(400, "This account does not need a payment.")
    amount, _ = PLANS[plan]
    now = int(time.time())
    attempt_id = uuid.uuid4().hex
    expires = now + 300
    with accounts.database() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM qr_payments WHERE uid=? AND expires>? AND status IN ('creating','pending','unknown') ORDER BY expires DESC LIMIT 1",
            (user["id"], now),
        ).fetchone()
        if existing:
            if existing["status"] == "pending" and existing["image_url"]:
                return {
                    "id": existing["id"],
                    "image_url": existing["image_url"],
                    "expires": existing["expires"],
                    "plan": existing["plan"],
                    "amount": existing["amount"],
                }
            raise HTTPException(409, "A payment QR is being prepared. Please wait before trying again.")
        db.execute(
            "INSERT INTO qr_payments(id,uid,plan,amount,expires,status) VALUES(?,?,?,?,?,'creating')",
            (attempt_id, user["id"], plan, amount, expires),
        )
    try:
        result = await razorpay(
            "POST",
            "/payments/qr_codes",
            {
                "type": "upi_qr",
                "name": "Clipstudio",
                "usage": "single_use",
                "fixed_amount": True,
                "payment_amount": amount,
                "description": f"Clipstudio {plan}",
                "close_by": expires,
                "notes": {"clipstudio_payment": attempt_id},
            },
        )
        qr_id, image_url, provider_expiry = result.get("id"), result.get("image_url"), result.get("close_by")
        if (
            not isinstance(qr_id, str)
            or not re.fullmatch(r"qr_[A-Za-z0-9]+", qr_id)
            or not safe_image(image_url)
            or result.get("type") != "upi_qr"
            or result.get("usage") != "single_use"
            or result.get("fixed_amount") is not True
            or result.get("payment_amount") != amount
            or type(provider_expiry) is not int
            or provider_expiry > expires
            or provider_expiry <= int(time.time())
        ):
            raise HTTPException(502, "Payment provider returned an invalid QR. Please try again later.")
        with accounts.database() as db:
            changed = db.execute(
                "UPDATE qr_payments SET qr_id=?, image_url=?, expires=?, status=CASE WHEN status='paid' THEN status ELSE 'pending' END WHERE id=? AND (qr_id IS NULL OR qr_id=?)",
                (qr_id, image_url, provider_expiry, attempt_id, qr_id),
            )
            if changed.rowcount != 1:
                raise HTTPException(502, "Payment QR could not be matched. Please contact support.")
        return {
            "id": attempt_id,
            "image_url": image_url,
            "expires": provider_expiry,
            "plan": plan,
            "amount": amount,
        }
    except Exception:
        # A timeout can happen after the provider created a payable QR. Keep the attempt
        # reserved until it expires so a retry cannot create overlapping requests.
        with accounts.database() as db:
            db.execute(
                "UPDATE qr_payments SET status='unknown' WHERE id=? AND status='creating'", (attempt_id,)
            )
        raise


def status(user, attempt_id):
    with accounts.database() as db:
        row = db.execute(
            "SELECT * FROM qr_payments WHERE id=? AND uid=?", (attempt_id, user["id"])
        ).fetchone()
    if not row:
        raise HTTPException(404, "Payment not found.")
    state = row["status"]
    if state != "paid" and row["expires"] <= time.time():
        state = "expired"
    return {"id": row["id"], "status": state, "expires": row["expires"], "paid_until": row["paid_until"]}


def webhook(body, signature):
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Payments are not configured.")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise HTTPException(400, "Invalid webhook signature.")
    try:
        event = json.loads(body)
        if event.get("event") != "qr_code.credited":
            return {"received": True}
        qr = event["payload"]["qr_code"]["entity"]
        payment = event["payload"]["payment"]["entity"]
        attempt_id = qr["notes"]["clipstudio_payment"]
        qr_id, payment_id = qr["id"], payment["id"]
        if not all(isinstance(x, str) for x in (attempt_id, qr_id, payment_id)):
            raise ValueError
        if (
            not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
            or not re.fullmatch(r"qr_[A-Za-z0-9]+", qr_id)
            or not re.fullmatch(r"pay_[A-Za-z0-9]+", payment_id)
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, "Invalid payment event.") from None
    with accounts.database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM qr_payments WHERE id=?", (attempt_id,)).fetchone()
        if not row:
            raise HTTPException(400, "Unknown payment QR.")
        if row["status"] == "paid":
            if row["payment_id"] == payment_id and row["qr_id"] == qr_id:
                return {"received": True}
            raise HTTPException(400, "Payment QR already used.")
        if (
            row["qr_id"] not in (None, qr_id)
            or qr.get("type") != "upi_qr"
            or qr.get("usage") != "single_use"
            or qr.get("fixed_amount") is not True
            or qr.get("payment_amount") != row["amount"]
            or payment.get("amount") != row["amount"]
            or payment.get("currency") != "INR"
            or payment.get("status") != "captured"
            or payment.get("captured") is not True
            or type(payment.get("created_at")) is not int
            or payment["created_at"] > row["expires"]
        ):
            raise HTTPException(400, "Payment could not be verified.")
        account = db.execute(
            "SELECT paid_until, qr_paid_until FROM users WHERE id=?", (row["uid"],)
        ).fetchone()
        if not account:
            raise HTTPException(400, "Payment account not found.")
        end = add_months(
            max(int(time.time()), account["paid_until"], account["qr_paid_until"]), PLANS[row["plan"]][1]
        )
        db.execute("UPDATE users SET qr_paid_until=? WHERE id=?", (end, row["uid"]))
        db.execute(
            "UPDATE qr_payments SET status='paid', qr_id=?, payment_id=?, paid_until=? WHERE id=?",
            (qr_id, payment_id, end, attempt_id),
        )
    return {"received": True}
