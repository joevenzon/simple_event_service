import pytest

from app.channels import channel_matches, validate_channel


@pytest.mark.parametrize(
    "channel",
    ["backup", "backup.status.freespace", "frigate.front.person", "server.n95.disk_warning"],
)
def test_valid_channels(channel):
    assert validate_channel(channel) == channel


@pytest.mark.parametrize(
    "channel",
    [
        "",
        "Backup.status",
        "backup..status",
        ".backup",
        "backup.",
        "backup.*",
        "back up",
        "évent",
        "backup/status",
        "1backup",
        ".".join(["a"] * 13),
        "a" * 41,
        ".".join(["a" * 40] * 7),
        "backup.status\n",
    ],
)
def test_invalid_channels(channel):
    with pytest.raises(ValueError):
        validate_channel(channel)


def test_channel_matching_has_segment_boundaries():
    assert channel_matches("backup.status", "backup.status")
    assert channel_matches("backup.status", "backup.status.watchdog.restart")
    assert not channel_matches("backup.status", "backup.statuses")
    assert not channel_matches("backup.status", "backup.health")
