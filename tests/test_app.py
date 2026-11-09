"""Rate limits. No model calls: refused questions never reach the agents."""

import time

from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


def reset():
    app.asked.clear()
    app.day_count.update(day="", n=0)


def test_visitor_limit_is_30_an_hour():
    reset()
    app.asked["testclient"] = [time.time()] * 30
    reply = client.post("/chat", json={"message": "hi"})
    assert reply.status_code == 429 and "30 questions in the last hour" in reply.json()["response"]


def test_daily_cap_covers_everyone():
    reset()
    app.over_limit.__globals__["day_count"]["day"] = time.strftime("%Y-%m-%d", time.localtime(time.time() - 5 * 3600))
    app.day_count["n"] = app.QUESTIONS_PER_DAY // app.MAX_INSTANCES
    reply = client.post("/chat", json={"message": "hi"}, headers={"x-forwarded-for": "1.2.3.4"})
    assert reply.status_code == 429 and "for today" in reply.json()["response"]


def test_old_questions_stop_counting():
    reset()
    app.asked["testclient"] = [time.time() - 3700] * 30
    assert app.over_limit(type("R", (), {"headers": {}, "client": type("C", (), {"host": "testclient"})()})()) is None
