import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

BASE_URL = "http://localhost:8080"


@pytest.fixture(scope="session")
def accounting_api():
    """Starts the real accounting_api.py mock server for the test session
    and deletes all records before yielding, so tests run against a clean,
    real instance of the unmodifiable API -- not a mock of the mock."""
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "accounting_api.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            try:
                if requests.get(f"{BASE_URL}/health", timeout=1).ok:
                    break
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(0.25)
        else:
            proc.terminate()
            raise RuntimeError("accounting_api.py did not become healthy")
        yield BASE_URL
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture()
def clean_api(accounting_api):
    requests.delete(f"{accounting_api}/invoices", headers={"X-API-Key": "demo-key-1234"})
    yield accounting_api
