"""The "How we calculate risk" page: every number on it comes from the data."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))

import b11_method  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402


def test_the_page_matches_a_fresh_computation():
    n = b11_method.numbers()
    page = (ROOT / "method.html").read_text()
    assert page == b11_method.page(n)  # rebuilt from the current data, not edited by hand
    for key in ["crashes", "low", "high", "class_rate", "expected", "blended_rate", "risk_assumed", "risk_half", "risk_double"]:
        assert str(n[key]) in page, key


def test_the_worked_numbers_add_up():
    n = b11_method.numbers()
    assert n["low"] < n["crashes"] < n["high"] and n["low"] > n["expected"]  # it stands out beyond chance
    blended = n["own_weight_pct"] / 100 * n["own_rate"] + (1 - n["own_weight_pct"] / 100) * n["class_rate"]
    assert abs(blended - n["blended_rate"]) < 0.15
    assert n["risk_half"] < n["risk_assumed"] < n["risk_double"]


def test_the_page_follows_the_writing_rules():
    page = (ROOT / "method.html").read_text()
    text = re.sub(r"<[^>]+>", " ", page.split("<body>")[1])
    assert "—" not in text and "safer" not in text.lower() and "per rider" not in text.lower()
    assert TestClient(app.app).get("/how-we-calculate-risk").status_code == 200
