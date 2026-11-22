import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app import accounts, main, qr_billing


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPSTUDIO_LOCAL_MODE", raising=False)
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "JOBS", {})
    monkeypatch.setenv("PUBLIC_URL", "https://studio.example")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "synthetic")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "synthetic")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "synthetic-webhook")
    with TestClient(main.app) as client:
        yield client


def login(client, uid):
    accounts.register_google({"sub": uid, "email": uid + "@example.com", "email_verified": True})
    token = accounts.new_session(uid)
    signed = (
        TimestampSigner(main.secret_file.read_text().strip())
        .sign(base64.b64encode(json.dumps({"token": token}).encode()))
        .decode()
    )
    client.cookies.set("session", signed)


def mock_provider(monkeypatch):
    requests = []

    async def provider(method, path, payload=None):
        requests.append((method, path, payload))
        return {"id": "qr_Test123", "image_url": "https://rzp.io/i/synthetic", **payload, "status": "active"}

    monkeypatch.setattr(qr_billing, "razorpay", provider)
    return requests


def sign(event):
    body = json.dumps(event).encode()
    signature = hmac.new(b"synthetic-webhook", body, hashlib.sha256).hexdigest()
    return body, {"x-razorpay-signature": signature}


def paid_event(attempt, amount, created_at=None):
    return {
        "event": "qr_code.credited",
        "payload": {
            "qr_code": {
                "entity": {
                    "id": "qr_Test123",
                    "type": "upi_qr",
                    "usage": "single_use",
                    "fixed_amount": True,
                    "payment_amount": amount,
                    "notes": {"clipstudio_payment": attempt},
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_Test123",
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                    "captured": True,
                    "created_at": created_at if created_at is not None else int(time.time()),
                }
            },
        },
    }


@pytest.mark.parametrize("plan,amount", [("monthly", 10000), ("yearly", 100000)])
def test_fixed_price_five_minute_qr_and_account_isolation(client, monkeypatch, plan, amount):
    calls = mock_provider(monkeypatch)
    login(client, "alice")
    before = int(time.time())
    response = client.post("/api/billing/qr", json={"plan": plan})
    assert response.status_code == 200
    result = response.json()
    body = calls[0][2]
    assert body["payment_amount"] == amount and result["amount"] == amount
    assert body["fixed_amount"] is True and body["usage"] == "single_use"
    assert before + 300 <= body["close_by"] <= int(time.time()) + 300
    assert body["notes"]["clipstudio_payment"] == result["id"]
    assert client.post("/api/billing/qr", json={"plan": plan}).json()["id"] == result["id"]
    assert len(calls) == 1
    login(client, "bob")
    assert client.get("/api/billing/qr/" + result["id"]).status_code == 404
    assert client.post("/api/billing/qr", json={"plan": "invalid"}).status_code == 422


def test_payment_requires_verified_matching_capture_and_is_idempotent(client, monkeypatch):
    mock_provider(monkeypatch)
    login(client, "alice")
    result = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    event = paid_event(result["id"], result["amount"])
    body, headers = sign(event)
    assert client.post("/api/billing/razorpay/webhook", content=body).status_code == 400
    event["payload"]["payment"]["entity"]["amount"] = 1
    wrong, wrong_headers = sign(event)
    assert (
        client.post("/api/billing/razorpay/webhook", content=wrong, headers=wrong_headers).status_code == 400
    )
    assert client.get("/api/me").json()["unlimited"] is False
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 200
    status = client.get("/api/billing/qr/" + result["id"]).json()
    assert status["status"] == "paid"
    assert client.get("/api/me").json()["unlimited"] is True
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 200
    assert client.get("/api/billing/qr/" + result["id"]).json()["paid_until"] == status["paid_until"]


@pytest.mark.parametrize(
    "field,value",
    [("currency", "USD"), ("status", "authorized"), ("captured", False), ("created_at", 9999999999)],
)
def test_invalid_payment_never_activates(client, monkeypatch, field, value):
    mock_provider(monkeypatch)
    login(client, "alice")
    result = client.post("/api/billing/qr", json={"plan": "yearly"}).json()
    event = paid_event(result["id"], result["amount"])
    event["payload"]["payment"]["entity"][field] = value
    body, headers = sign(event)
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 400
    assert client.get("/api/me").json()["unlimited"] is False


def test_expired_qr_but_delayed_valid_payment_is_processed(client, monkeypatch):
    mock_provider(monkeypatch)
    login(client, "alice")
    result = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    actual = int(time.time())
    monkeypatch.setattr(qr_billing.time, "time", lambda: result["expires"] + 10)
    assert client.get("/api/billing/qr/" + result["id"]).json()["status"] == "expired"
    body, headers = sign(paid_event(result["id"], result["amount"], actual))
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 200
    assert client.get("/api/billing/qr/" + result["id"]).json()["status"] == "paid"


def test_calendar_periods_handle_month_end_and_leap_year():
    d = int(datetime(2024, 1, 31, 12, tzinfo=UTC).timestamp())
    assert datetime.fromtimestamp(qr_billing.add_months(d, 1), UTC).day == 29
    d = int(datetime(2024, 2, 29, 12, tzinfo=UTC).timestamp())
    assert datetime.fromtimestamp(qr_billing.add_months(d, 12), UTC).day == 28


def test_missing_payment_configuration_is_closed(client, monkeypatch):
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET")
    login(client, "alice")
    assert client.get("/api/config").json()["payments_ready"] is False
    assert client.post("/api/billing/qr", json={"plan": "monthly"}).status_code == 503


def test_captured_webhook_before_qr_creation_response(client, monkeypatch):
    login(client, "alice")

    async def provider(method, path, payload=None):
        attempt_id = payload["notes"]["clipstudio_payment"]
        body, headers = sign(paid_event(attempt_id, payload["payment_amount"]))
        assert qr_billing.webhook(body, headers["x-razorpay-signature"]) == {"received": True}
        return {"id": "qr_Test123", "image_url": "https://rzp.io/i/synthetic", **payload, "status": "active"}

    monkeypatch.setattr(qr_billing, "razorpay", provider)
    result = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    assert client.get("/api/billing/qr/" + result["id"]).json()["status"] == "paid"
    assert client.get("/api/me").json()["unlimited"] is True


def test_legacy_stripe_cancellation_does_not_remove_qr_access(client, monkeypatch):
    mock_provider(monkeypatch)
    login(client, "alice")
    result = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    body, headers = sign(paid_event(result["id"], result["amount"]))
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 200
    with accounts.database() as db:
        db.execute("UPDATE users SET paid_until=0 WHERE id='alice'")
    assert client.get("/api/me").json()["unlimited"] is True


def test_invalid_provider_qr_not_displayed(client, monkeypatch):
    login(client, "alice")

    async def provider(method, path, payload=None):
        return {"id": "qr_Test123", "image_url": "https://evil.example/qr", **payload}

    monkeypatch.setattr(qr_billing, "razorpay", provider)
    assert client.post("/api/billing/qr", json={"plan": "monthly"}).status_code == 502
    assert client.get("/api/me").json()["unlimited"] is False


def test_unknown_or_mismatched_qr_cannot_activate(client, monkeypatch):
    mock_provider(monkeypatch)
    login(client, "alice")
    result = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    event = paid_event(result["id"], result["amount"])
    event["payload"]["qr_code"]["entity"]["id"] = "qr_Other"
    body, headers = sign(event)
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 400
    event["payload"]["qr_code"]["entity"]["id"] = "qr_Test123"
    event["payload"]["qr_code"]["entity"]["notes"]["clipstudio_payment"] = "f" * 32
    body, headers = sign(event)
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 400
    assert client.get("/api/me").json()["unlimited"] is False


def test_existing_subscription_cancellation_does_not_cancel_paid_qr(client, monkeypatch):
    from app import billing

    mock_provider(monkeypatch)
    login(client, "alice")
    result = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    body, headers = sign(paid_event(result["id"], result["amount"]))
    assert client.post("/api/billing/razorpay/webhook", content=body, headers=headers).status_code == 200
    with accounts.database() as db:
        db.execute("UPDATE users SET subscription='sub_legacy', paid_until=2000000000 WHERE id='alice'")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "stripe-test")

    async def stripe(*args, **kwargs):
        return {"id": "sub_legacy", "status": "canceled", "metadata": {"uid": "alice"}}

    monkeypatch.setattr(billing, "stripe", stripe)
    event = {
        "id": "evt_cancel_legacy",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_legacy"}},
    }
    payload = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"stripe-test", timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    assert (
        client.post(
            "/api/billing/webhook",
            content=payload,
            headers={"stripe-signature": f"t={timestamp},v1={signature}"},
        ).status_code
        == 200
    )
    assert client.get("/api/me").json()["unlimited"] is True


def test_fresh_qr_after_expiry(client, monkeypatch):
    login(client, "alice")
    calls = []

    async def provider(method, path, payload=None):
        calls.append(payload)
        return {"id": f"qr_Test{len(calls)}", "image_url": "https://rzp.io/i/synthetic", **payload}

    monkeypatch.setattr(qr_billing, "razorpay", provider)
    first = client.post("/api/billing/qr", json={"plan": "monthly"}).json()
    monkeypatch.setattr(qr_billing.time, "time", lambda: first["expires"] + 1)
    assert client.get("/api/billing/qr/" + first["id"]).json()["status"] == "expired"
    second = client.post("/api/billing/qr", json={"plan": "yearly"}).json()
    assert second["id"] != first["id"]
    assert second["amount"] == 100000
    assert len(calls) == 2


def test_unknown_provider_outcome_does_not_create_overlapping_qrs(client, monkeypatch):
    from fastapi import HTTPException

    login(client, "alice")
    calls = []

    async def provider(method, path, payload=None):
        calls.append(payload)
        raise HTTPException(502, "Timeout")

    monkeypatch.setattr(qr_billing, "razorpay", provider)
    assert client.post("/api/billing/qr", json={"plan": "monthly"}).status_code == 502
    assert client.post("/api/billing/qr", json={"plan": "monthly"}).status_code == 409
    assert len(calls) == 1
