"""Boundary-aware channel syntax, also used when provisioning client scopes."""

import re

SEGMENT = re.compile(r"[a-z][a-z0-9_-]{0,39}", re.ASCII)


def validate_channel(channel: str) -> str:
    segments = channel.split(".")
    if (
        len(channel) > 255
        or not 1 <= len(segments) <= 12
        or any(SEGMENT.fullmatch(segment) is None for segment in segments)
    ):
        raise ValueError("Invalid channel: use 1-12 lowercase ASCII segments, up to 255 characters")
    return channel


def channel_matches(prefix: str, channel: str) -> bool:
    return channel == prefix or channel.startswith(prefix + ".")
