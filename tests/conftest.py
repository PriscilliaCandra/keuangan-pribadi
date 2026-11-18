from datetime import date

import pytest

from keuangan.db import Database

TODAY = date(2026, 11, 17)  # Selasa
CHAT = 1


@pytest.fixture
def db():
    return Database(":memory:")
