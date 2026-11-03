import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def tmp_data(monkeypatch):
    d = tempfile.mkdtemp(prefix="desk_test_")
    monkeypatch.setenv("DESK_DATA", d)
    monkeypatch.setenv("DESK_SECRETS", os.path.join(d, "secrets"))
    monkeypatch.setenv("DESK_SECRET", "test-secret")
    yield Path(d)
