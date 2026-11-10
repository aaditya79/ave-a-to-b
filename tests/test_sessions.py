"""Restorable sessions: GET, PUT and DELETE /session/{id}, with a stand-in for the Cloud Storage bucket."""
import json
import secrets

import pytest
from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


class MemoryStore:
    def __init__(self):
        self.files = {}

    def get(self, sid):
        return json.loads(self.files[sid]) if sid in self.files else None

    def put(self, sid, data):
        self.files[sid] = data

    def delete(self, sid):
        self.files.pop(sid, None)


class DownStore:
    def get(self, sid):
        raise ConnectionError("bucket unreachable")
    put = delete = get


@pytest.fixture
def store(monkeypatch):
    memory = MemoryStore()
    monkeypatch.setattr(app, "session_store", memory)
    app.session_asked.clear()
    return memory


def new_id():
    return secrets.token_urlsafe(16)  # 128 bits, as the page makes them


def trip(i, size=0):
    return {"id": i, "at": 1_700_000_000_000 + i, "trip": {"start": "Columbia University", "end": "Union Square", "when": ""},
            "asked": "I bike from Columbia University to Union Square.", "rec": {"name": "Balanced", "line": [[40.8, -73.96]], "miles": 5.3, "minutes": 34},
            "result": {"response": "x" * size, "map": {"routes": {}}}}


def test_a_session_saves_and_restores_its_trips(store):
    sid = new_id()
    assert len(sid) == 22 and app.SESSION_ID.match(sid)
    saved = client.put(f"/session/{sid}", json={"trips": [trip(1), trip(2)]})
    assert saved.status_code == 200 and saved.json() == {"ok": True, "trips": 2}
    back = client.get(f"/session/{sid}").json()
    assert [t["id"] for t in back["trips"]] == [1, 2] and back["trips"][0]["result"]["map"] == {"routes": {}} and back["updated"]


def test_only_trip_fields_are_stored(store):
    sid = new_id()
    extra = {**trip(1), "ip": "1.2.3.4", "email": "someone@example.com"}
    client.put(f"/session/{sid}", json={"trips": [extra], "owner": "me"}, headers={"x-forwarded-for": "1.2.3.4"})
    stored = json.loads(store.files[sid])
    assert set(stored) == {"updated", "trips"} and set(stored["trips"][0]) == set(trip(1)) <= app.SESSION_TRIP_KEYS
    assert "1.2.3.4" not in store.files[sid].decode() and "someone@" not in store.files[sid].decode()


@pytest.mark.parametrize("bad", ["short", "a" * 23, "has space in it_______", "../../etc/passwd______", "abc%2Fdef" + "x" * 13, "!" * 22])
def test_malformed_ids_get_the_same_answer_as_unknown_ones(store, bad):
    unknown = client.get(f"/session/{new_id()}")
    malformed = client.get(f"/session/{bad}")
    assert unknown.status_code == 404 and unknown.json() == {"error": "No session found"}
    assert malformed.status_code == 404
    if "/" not in bad and "%2F" not in bad:  # a slash never reaches the session route: the app's plain 404 answers
        assert malformed.json() == unknown.json()
    assert client.put(f"/session/{bad}", json={"trips": []}).status_code == 404 and store.files == {}


def test_a_session_holds_at_most_25_trips_and_about_1_mb(store):
    sid = new_id()
    assert client.put(f"/session/{sid}", json={"trips": [trip(i) for i in range(25)]}).status_code == 200
    too_many = client.put(f"/session/{sid}", json={"trips": [trip(i) for i in range(26)]})
    assert too_many.status_code == 413 and "25 trips" in too_many.json()["error"]
    too_big = client.put(f"/session/{sid}", json={"trips": [trip(1, size=1_000_001)]})
    assert too_big.status_code == 413 and "1 MB" in too_big.json()["error"]
    assert len(client.get(f"/session/{sid}").json()["trips"]) == 25  # the refused saves changed nothing
    assert client.put(f"/session/{sid}", content=b"not json").status_code == 422
    assert client.put(f"/session/{sid}", json={"trips": "nope"}).status_code == 422


def test_delete_removes_the_server_copy(store):
    sid = new_id()
    client.put(f"/session/{sid}", json={"trips": [trip(1)]})
    assert client.delete(f"/session/{sid}").json() == {"ok": True}
    assert client.get(f"/session/{sid}").json() == {"error": "No session found"}
    assert client.delete(f"/session/{sid}").json() == {"ok": True}  # the same answer when nothing is there
    assert client.delete("/session/bad").json() == {"ok": True}


def test_session_requests_have_their_own_hourly_limit(store, monkeypatch):
    monkeypatch.setattr(app, "SESSION_REQUESTS_PER_HOUR", 3)
    sid, ip = new_id(), {"x-forwarded-for": "7.7.7.7"}
    codes = [client.get(f"/session/{sid}", headers=ip).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]
    assert client.get(f"/session/{sid}", headers={"x-forwarded-for": "7.7.7.8"}).status_code == 404  # another visitor
    assert app.asked.get("7.7.7.7") is None  # syncing never counts as a question


def test_an_unreachable_bucket_says_sessions_cannot_sync(monkeypatch):
    monkeypatch.setattr(app, "session_store", DownStore())
    app.session_asked.clear()
    sid = new_id()
    for reply in [client.get(f"/session/{sid}"), client.put(f"/session/{sid}", json={"trips": []}), client.delete(f"/session/{sid}")]:
        assert reply.status_code == 503 and reply.json() == {"error": app.SESSIONS_OFFLINE, "offline": True}


def test_no_bucket_setting_means_offline_not_a_crash(monkeypatch):
    monkeypatch.setattr(app, "session_store", app.SessionStore(None))
    app.session_asked.clear()
    assert client.get(f"/session/{new_id()}").status_code == 503
