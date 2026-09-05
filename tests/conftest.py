from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from datamanager.config import Config
from datamanager.db import connect


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return replace(Config(), db_path=tmp_path / "index.db")


@pytest.fixture
def conn(cfg: Config) -> sqlite3.Connection:
    c = connect(cfg.db_path)
    yield c
    c.close()


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    """Stand-in for the NAS mount."""
    root = tmp_path / "nas"
    root.mkdir()
    return root
