import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_requires_https_and_non_placeholder_secret():
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        Settings(_env_file=None, environment="production", session_secret="replace-with-secret")
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            base_url="http://events.example.com",
            session_secret="x" * 40,
        )
    Settings(
        _env_file=None,
        environment="production",
        base_url="https://events.example.com",
        session_secret="x" * 40,
    )


def test_settings_read_environment_without_exposing_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("SES_ENV", "production")
    monkeypatch.setenv("SES_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("SES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("EVENT_RETENTION_DAYS", "9")
    settings = Settings(_env_file=None)
    assert settings.database_path == tmp_path / "ses.sqlite3"
    assert settings.retention_days == 9
    assert "s" * 40 not in repr(settings)


def test_image_publishing_cannot_be_enabled_prematurely():
    with pytest.raises(ValidationError, match="not implemented"):
        Settings(_env_file=None, environment="development", ntfy_send_images=True)


@pytest.mark.parametrize(
    "topic", ["frigate", "replace-with-long-random-topic-12345", "x" * 129, "x" * 32 + "/private"]
)
def test_topic_rejects_short_placeholder_and_unsafe_names(topic):
    with pytest.raises(ValidationError, match="NTFY_TOPIC"):
        Settings(_env_file=None, environment="development", ntfy_topic=topic)


def test_empty_topic_disables_and_valid_topic_is_redacted():
    assert Settings(_env_file=None, environment="development", ntfy_topic="").ntfy_topic is None
    settings = Settings(_env_file=None, environment="development", ntfy_topic="x" * 48)
    assert "x" * 48 not in repr(settings)


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("base_url", "https://user:password@events.example.com"),
        ("base_url", "https://events.example.com/?secret=value"),
        ("base_url", "https://events.example.com/subpath"),
        ("ntfy_url", "https://ntfy.sh/topic"),
        ("ntfy_url", "https://ntfy.sh/#fragment"),
    ],
)
def test_notification_origins_reject_credentials_and_embedded_paths(name, url):
    with pytest.raises(ValidationError, match="origins"):
        Settings(_env_file=None, environment="development", **{name: url})


def test_production_relay_requires_https():
    with pytest.raises(ValidationError, match="NTFY_URL must use HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            session_secret="x" * 48,
            ntfy_url="http://ntfy.sh",
        )
