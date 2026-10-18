import pytest

from app.config import Settings
from app.db import Database


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, environment="development", data_dir=tmp_path / "data")


@pytest.fixture
def database(settings):
    database = Database(settings.database_path)
    database.initialize()
    return database
