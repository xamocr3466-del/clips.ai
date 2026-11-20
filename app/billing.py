"""Optional Stripe hosted subscriptions. No entitlement is granted by the browser."""

import hashlib
import hmac
import json
import os
import time

import httpx
from fastapi import HTTPException

from app import accounts

PLANS = {
    "monthly": (500, "month", "STRIPE_MONTHLY_PRICE_ID"),
    "yearly": (6000, "year", "STRIPE_YEARLY_PRICE_ID"),
}


def ready():
    return all(
        os.getenv(k)
        for k in (
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
            "STRIPE_MONTHLY_PRICE_ID",
            "STRIPE_YEARLY_PRICE_ID",
            "PUBLIC_URL",
        )
    )


async def stripe(method, path, data=None):
    endpoint = os.getenv("STRIPE_API_BASE", "https://api.stripe.com/v1").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            endpoint + path,
            data=data,
            headers={
                "Authorization": "Bearer " + os.environ["STRIPE_SECRET_KEY"],
                "Stripe-Version": "2025-06-30.basil",
            },
        )
    if response.status_code >= 400:
        raise HTTPException(502, "Payment provider unavailable. Please try again later.")
    return response.json()


async def checkout(user, plan):
    if plan not in PLANS:
        raise HTTPException(422, "Choose monthly or yearly.")
    if not ready():
        raise HTTPException(503, "Payments are not activated yet. Please contact the studio owner.")
    if user.get("local") or user.get("unlimited"):
        raise HTTPException(400, "This account does not need a subscription.")
    amount, interval, key = PLANS[plan]
    price = await stripe("GET", "/prices/" + os.environ[key])
    if (
        price.get("unit_amount") != amount
        or price.get("currency") != "usd"
        or price.get("recurring", {}).get("interval") != interval
        or price.get("recurring", {}).get("interval_count", 1) != 1
        or not price.get("active")
    ):
        raise HTTPException(503, "The plan is not configured correctly. Contact the studio owner.")
    base = os.environ["PUBLIC_URL"].rstrip("/")
    result = await stripe(
        "POST",
        "/checkout/sessions",
        {
            "mode": "subscription",
            "line_items[0][price]": os.environ[key],
            "line_items[0][quantity]": "1",
            "customer_email": user["email"],
            "client_reference_id": user["id"],
            "subscription_data[metadata][uid]": user["id"],
            "success_url": base + "/?payment=received",
            "cancel_url": base + "/?payment=cancelled",
        },
    )
    return {"url": result["url"]}


def verify(payload, signature):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "Payments are not configured.")
    try:
        fields = [piece.split("=", 1) for piece in signature.split(",")]
        timestamp = next(value for key, value in fields if key == "t")
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError
        digest = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(digest, value) for key, value in fields if key == "v1"):
            raise ValueError
        return json.loads(payload)
    except (ValueError, StopIteration, TypeError):
        raise HTTPException(400, "Invalid webhook signature.") from None


async def webhook(payload, signature):
    event = verify(payload, signature)
    event_id = event.get("id")
    if not isinstance(event_id, str):
        raise HTTPException(400, "Missing event identifier.")
    with accounts.database() as db:
        if db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            return {"received": True}
    obj = event.get("data", {}).get("object", {})
    kind = event.get("type")
    subscription = None
    if kind == "invoice.paid":
        sub_id = obj.get("subscription") or obj.get("parent", {}).get("subscription_details", {}).get(
            "subscription"
        )
        if sub_id:
            subscription = await stripe("GET", "/subscriptions/" + sub_id)
    elif kind in {"customer.subscription.updated", "customer.subscription.deleted"}:
        subscription = await stripe("GET", "/subscriptions/" + obj["id"])
    with accounts.database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            return {"received": True}
        if subscription:
            uid = subscription.get("metadata", {}).get("uid")
            periods = [
                item.get("current_period_end", 0) for item in subscription.get("items", {}).get("data", [])
            ]
            end = max(periods, default=subscription.get("current_period_end", 0))
            if kind == "invoice.paid" and subscription.get("status") == "active":
                db.execute(
                    "UPDATE users SET paid_until=?, customer=?, subscription=? WHERE id=?",
                    (end, subscription.get("customer"), subscription["id"], uid),
                )
            elif subscription.get("status") in {"canceled", "unpaid", "incomplete_expired", "paused"}:
                db.execute(
                    "UPDATE users SET paid_until=0 WHERE id=? AND subscription=?", (uid, subscription["id"])
                )
        db.execute("INSERT INTO events VALUES(?)", (event_id,))
    return {"received": True}
