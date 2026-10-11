"""Shared fixtures for the monitor's own tests."""

from __future__ import annotations

import pytest

from tests.helpers import monitor_database


@pytest.fixture
def monitor_db(tmp_path):
    con = monitor_database(tmp_path)
    yield con
    con.close()
