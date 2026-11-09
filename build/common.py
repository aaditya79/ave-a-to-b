"""Shared helpers for the offline build: paths, paging through NYC Open Data, and retries."""

import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # streets.py lives at the repo root so the app can use it too
DATA = ROOT / "data"
RAW = DATA / "raw"  # git-ignored downloads
RAW.mkdir(parents=True, exist_ok=True)

# The first year of the study window: the last 5 full years plus this year.
START_YEAR = 2021
START = f"{START_YEAR}-01-01T00:00:00"

SODA = "https://data.cityofnewyork.us/resource/{dataset}.json"
PAGE = 50_000


def soda_page(dataset: str, params: dict, tries: int = 6) -> list[dict]:
    """One page from the Open Data API. The API times out under load, so back off and retry."""
    headers = {}
    if os.environ.get("NYC_OPEN_DATA_APP_TOKEN"):
        headers["X-App-Token"] = os.environ["NYC_OPEN_DATA_APP_TOKEN"]
    for attempt in range(tries):
        try:
            response = requests.get(SODA.format(dataset=dataset), params=params, headers=headers, timeout=300)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            if attempt == tries - 1:
                raise
            wait = 5 * 2**attempt
            print(f"  retry in {wait}s: {type(e).__name__}", flush=True)
            time.sleep(wait)
    return []


def soda_all(dataset: str, select: str, where: str | None = None, order: str = ":id") -> pd.DataFrame:
    """Every matching row, one page at a time. order must be stable or pages overlap."""
    rows: list[dict] = []
    while True:
        params = {"$select": select, "$limit": PAGE, "$offset": len(rows), "$order": order}
        if where:
            params["$where"] = where
        page = soda_page(dataset, params)
        rows += page
        print(f"  {dataset}: {len(rows):,} rows", flush=True)
        if len(page) < PAGE:
            return pd.DataFrame(rows)


def cached(name: str, fetch) -> pd.DataFrame:
    """Reuse a raw download if we have it, so rerunning a script doesn't hit the API again.
    Delete data/raw/<name>.parquet to refresh."""
    path = RAW / f"{name}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    frame = fetch()
    frame.to_parquet(path, index=False)
    return frame


def report(name: str, frame: pd.DataFrame) -> None:
    """Every script ends by saying what it wrote."""
    path = DATA / f"{name}.parquet"
    frame.to_parquet(path, index=False)
    print(f"wrote {path.name}: {len(frame):,} rows, {path.stat().st_size / 1e6:.1f} MB", flush=True)
