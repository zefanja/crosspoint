"""Daily study reminder (Web Push): subscription routes and sending."""

import json
import re
from datetime import datetime

import pytest
from pywebpush import WebPushException

from app import reminders
from app.models import PushSubscription, Reminder

FCM = "https://fcm.googleapis.com/fcm/send/abc123"


@pytest.fixture
def pushed(monkeypatch):
    """Capture pushes instead of sending; endpoints in `gone` answer 410."""
    sent, gone = [], set()

    class Gone:
        status_code = 410

    def fake_webpush(info, data, **kwargs):
        if info["endpoint"] in gone:
            raise WebPushException("gone", response=Gone())
        sent.append((info["endpoint"], json.loads(data), kwargs))

    monkeypatch.setattr(reminders, "webpush", fake_webpush)
    return sent, gone


def subscribe(client, csrf, endpoint=FCM, time="19:00"):
    return client.post("/reminder", headers={"X-CSRF-Token": csrf}, json={
        "endpoint": endpoint, "keys": {"p256dh": "BPkey", "auth": "authkey"}, "time": time})


def add_verse(client, csrf):
    page = client.get("/")
    deck = re.search(r'href="/review/(\d+)"', page.text).group(1)
    client.post("/notes/new", data={"csrf_token": csrf, "notetype": "Bible Verse",
                                    "deck_id": deck, "field_0": "Joh 3,16",
                                    "field_1": "Denn also hat Gott die Welt geliebt",
                                    "field_2": "LUT17"})


def test_vapid_key_is_generated_once(data_dir):
    key = reminders.public_key()
    assert len(key) == 87 and reminders.public_key() == key  # 65 bytes, base64url
    assert (data_dir / "vapid_private.pem").exists()


def test_me_page_shows_reminder(client, login):
    login()
    page = client.get("/me").text
    assert "Tägliche Erinnerung" in page and 'value="19:00"' in page
    assert f'data-key="{reminders.public_key()}"' in page


def test_subscribe_and_unsubscribe(client, login, db):
    user, csrf = login()
    assert subscribe(client, csrf, time="07:30").status_code == 204
    assert db.get(Reminder, user.id).time == "07:30"
    assert 'value="07:30"' in client.get("/me").text
    # same device again: updated, not duplicated
    assert subscribe(client, csrf, time="08:00").status_code == 204
    db.expire_all()
    assert db.query(PushSubscription).count() == 1 and db.get(Reminder, user.id).time == "08:00"

    r = client.post("/reminder/off", headers={"X-CSRF-Token": csrf}, json={"endpoint": FCM})
    assert r.status_code == 204
    db.expire_all()
    assert db.query(PushSubscription).count() == 0 and db.get(Reminder, user.id) is None


def test_subscribe_rejects_bad_input(client, login):
    _, csrf = login()
    assert subscribe(client, csrf, endpoint="https://intranet.local/x").status_code == 400
    assert subscribe(client, csrf, endpoint="http://fcm.googleapis.com/x").status_code == 400
    assert subscribe(client, csrf, time="25:00").status_code == 400
    assert client.post("/reminder", headers={"X-CSRF-Token": "x"}, json={}).status_code == 403


def test_time_already_passed_starts_tomorrow(db, make_user):
    user, _ = make_user()
    now = datetime(2026, 10, 30, 20, 0)
    reminders.subscribe(db, user.id, FCM, "k", "a", "19:00", "de", now=now)
    assert db.get(Reminder, user.id).last_sent_on == now.date()
    reminders.subscribe(db, user.id, FCM, "k", "a", "21:00", "de", now=now)
    assert db.get(Reminder, user.id).last_sent_on is None


def test_send_due_only_with_cards_once_per_day(client, login, db, pushed, run):
    sent, _ = pushed
    user, csrf = login()
    reminders.subscribe(db, user.id, FCM, "k", "a", "18:00", "de",
                        now=datetime(2026, 10, 30, 12, 0))
    evening = datetime(2026, 10, 30, 18, 0)

    assert run(reminders.send_due(evening)) == 0  # nothing to study → no push
    db.get(Reminder, user.id).last_sent_on = None
    db.commit()

    add_verse(client, csrf)
    assert run(reminders.send_due(datetime(2026, 10, 30, 17, 59))) == 0  # too early
    assert run(reminders.send_due(evening)) == 1
    assert run(reminders.send_due(evening.replace(hour=19))) == 0  # once per day
    endpoint, payload, kwargs = sent[0]
    assert endpoint == FCM and payload["url"] == "/"
    assert payload["title"] == "Zeit zum Lernen" and payload["body"].endswith(": 1")
    assert kwargs["vapid_claims"] == {"sub": "http://testserver"}

    assert run(reminders.send_due(evening.replace(day=31))) == 1  # next day again


def test_gone_subscription_is_removed(client, login, db, pushed):
    sent, gone = pushed
    user, csrf = login()
    subscribe(client, csrf)
    gone.add(FCM)
    r = client.post("/reminder/test", headers={"X-CSRF-Token": csrf})
    assert r.json() == {"reached": 0} and not sent
    db.expire_all()
    assert db.query(PushSubscription).count() == 0 and db.get(Reminder, user.id) is None


def test_test_push(client, login, pushed):
    sent, _ = pushed
    _, csrf = login()
    subscribe(client, csrf)
    assert client.post("/reminder/test", headers={"X-CSRF-Token": csrf}).json() == {"reached": 1}
    assert sent[0][1]["body"] == "Erinnerungen funktionieren."
