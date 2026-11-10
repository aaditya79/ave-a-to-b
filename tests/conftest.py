import pytest


@pytest.fixture(autouse=True)
def fresh_limits():
    """Every test starts under the rate limits: the daily cap is low (8 a day per instance), so counts from
    earlier tests in the same run would otherwise block later ones."""
    import app
    app.asked.clear()
    app.day_count.update(day="", n=0)
    yield
