"""Keep admin tests away from the real user data/config directories."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
