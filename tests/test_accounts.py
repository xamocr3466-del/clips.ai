import asyncio
import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app import accounts, billing, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPSTUDIO_LOCAL_MODE", raising=False)
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "JOBS", {})
    with TestClient(main.app) as client:
        yield client


def user(uid="alice", email="alice@example.com"):
    accounts.register_google({"sub": uid, "email": email, "email_verified": True, "name": uid})
    return {"id": uid, "email": email, "unlimited": email == accounts.OWNER_EMAIL}


def login(client, uid):
    token = accounts.new_session(uid)
    signer = TimestampSigner(main.secret_file.read_text().strip())
    cookie = signer.sign(base64.b64encode(json.dumps({"token": token}).encode())).decode()
    client.cookies.set("session", cookie)


def test_google_only_fail_closed_and_verified_owner(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.put("/api/uploads/stream", content=b"invalid").status_code == 401
    assert client.get("/api/me").json() is None
    with pytest.raises(HTTPException):
        accounts.register_google({"sub": "fake", "email": accounts.OWNER_EMAIL, "email_verified": False})
    user("owner", accounts.OWNER_EMAIL)
    login(client, "owner")
    assert client.get("/api/me").json()["owner"] is True
    assert client.get("/api/me").json()["unlimited"] is True


def test_trial_cannot_be_reset_by_deleting_project(client):
    alice = user()
    login(client, "alice")
    job = main.new_job("First", main.Options(), alice)
    accounts.finish(job["id"], True)
    main.save(job["id"], status="completed")
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}).status_code == 402
    assert client.put("/api/uploads/stream", content=b"anything").status_code == 402
    assert client.get("/api/me").json()["trial_available"] is False


def test_failed_attempt_refunds_and_concurrent_reservations_are_atomic(client):
    alice = user()
    accounts.reserve(alice, "failed")
    accounts.finish("failed", False)

    def reserve(number):
        try:
            accounts.reserve(alice, str(number))
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, [1, 2])) == [200, 402]


def test_project_and_export_isolation_and_csrf(client):
    alice = user()
    job = main.new_job("Private", main.Options(), alice)
    user("bob", "bob@example.com")
    login(client, "bob")
    assert client.get("/api/jobs").json() == []
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/files/clip-01.mp4").status_code == 404
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert client.post("/api/logout", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/jobs").status_code == 401


def test_stream_upload_without_multipart_spooling(client, monkeypatch):
    alice = user()
    login(client, "alice")
    monkeypatch.setattr(main.pipeline, "MAX_BYTES", 0)
    monkeypatch.setattr(main.EXECUTOR, "submit", lambda *a, **k: None)
    response = client.put(
        "/api/uploads/stream?speed=fast&resolution=720",
        content=b"video-data",
        headers={"x-file-name": "my%20video.mp4"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["owner_id"] == alice["id"]
    assert job["options"]["resolution"] == 720
    assert (main.DATA / job["id"] / "upload.mp4").read_bytes() == b"video-data"


def signed(event):
    body = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    digest = hmac.new(b"webhook-test", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={timestamp},v1={digest}"}


def test_subscription_requires_signed_server_event_and_replay_is_safe(client, monkeypatch):
    user()
    login(client, "alice")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "webhook-test")
    future = int(time.time()) + 3600
    sub = {
        "id": "sub_test",
        "status": "active",
        "customer": "cus_test",
        "metadata": {"uid": "alice"},
        "items": {"data": [{"current_period_end": future}]},
    }
    calls = []

    async def stripe(*args, **kwargs):
        calls.append(args)
        return sub

    monkeypatch.setattr(billing, "stripe", stripe)
    event = {"id": "evt_test", "type": "invoice.paid", "data": {"object": {"subscription": "sub_test"}}}
    body, headers = signed(event)
    assert client.post("/api/billing/webhook", content=body).status_code == 400
    assert client.get("/api/me").json()["unlimited"] is False
    assert client.post("/api/billing/webhook", content=body, headers=headers).status_code == 200
    assert client.get("/api/me").json()["unlimited"] is True
    assert client.post("/api/billing/webhook", content=body, headers=headers).status_code == 200
    assert len(calls) == 1
    sub["status"] = "canceled"
    event.update(id="evt_cancel", type="customer.subscription.deleted", data={"object": {"id": "sub_test"}})
    body, headers = signed(event)
    assert client.post("/api/billing/webhook", content=body, headers=headers).status_code == 200
    assert client.get("/api/me").json()["unlimited"] is False


def test_upload_limit_failure_refunds_trial(client, monkeypatch):
    user()
    login(client, "alice")
    monkeypatch.setattr(main.pipeline, "MAX_BYTES", 4)
    response = client.put("/api/uploads/stream", content=b"12345", headers={"x-file-name": "large.mp4"})
    assert response.status_code == 413
    assert client.get("/api/me").json()["trial_available"] is True
    assert not list(main.DATA.glob("*/upload.mp4"))


def test_mispriced_checkout_cannot_charge_customer(client, monkeypatch):
    user()
    login(client, "alice")
    for key in [
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_MONTHLY_PRICE_ID",
        "STRIPE_YEARLY_PRICE_ID",
    ]:
        monkeypatch.setenv(key, "synthetic-test-value")
    monkeypatch.setenv("PUBLIC_URL", "https://studio.example")
    calls = []

    async def stripe(method, path, data=None):
        calls.append((method, path))
        return {"unit_amount": 5000, "currency": "usd", "recurring": {"interval": "month"}, "active": True}

    monkeypatch.setattr(billing, "stripe", stripe)
    assert client.post("/api/billing/checkout", json={"plan": "monthly"}).status_code == 410
    with pytest.raises(HTTPException) as error:
        asyncio.run(billing.checkout({"id": "alice", "email": "alice@example.com"}, "monthly"))
    assert error.value.status_code == 503
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert client.get("/api/me").json()["unlimited"] is False
