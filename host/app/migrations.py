"""Append migrations; never change an applied version. Each version is atomic."""

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """CREATE TABLE clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                allowed_prefixes_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                stale_timeout_seconds INTEGER NOT NULL DEFAULT 180
                    CHECK (stale_timeout_seconds > 0),
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                last_heartbeat_at TEXT,
                last_health_json TEXT
            )""",
            """CREATE TABLE images (
                id TEXT PRIMARY KEY,
                disk_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                sha256 TEXT NOT NULL,
                width INTEGER NOT NULL CHECK (width > 0),
                height INTEGER NOT NULL CHECK (height > 0),
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE events (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES clients(id),
                event_key TEXT NOT NULL,
                channel TEXT NOT NULL,
                severity TEXT NOT NULL
                    CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                occurred_at TEXT,
                received_at TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                image_id TEXT UNIQUE REFERENCES images(id),
                notify_hint INTEGER CHECK (notify_hint IN (0, 1)),
                payload_digest TEXT NOT NULL,
                UNIQUE (client_id, event_key)
            )""",
            "CREATE INDEX events_received ON events(received_at, id)",
            "CREATE INDEX events_client_received ON events(client_id, received_at)",
            "CREATE INDEX events_channel_received ON events(channel, received_at)",
            """CREATE TABLE notification_rules (
                id INTEGER PRIMARY KEY,
                channel_prefix TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                min_severity TEXT NOT NULL
                    CHECK (min_severity IN ('debug', 'info', 'warning', 'error', 'critical')),
                ntfy_priority INTEGER NOT NULL CHECK (ntfy_priority BETWEEN 1 AND 5),
                rule_priority INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE notification_deliveries (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                provider TEXT NOT NULL CHECK (provider = 'ntfy'),
                state TEXT NOT NULL CHECK (state IN
                    ('pending', 'sent_to_ntfy', 'failed', 'suppressed_cooldown')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                next_attempt_at TEXT NOT NULL,
                last_attempt_at TEXT,
                provider_response_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE (event_id, provider)
            )""",
            """CREATE INDEX deliveries_due
                ON notification_deliveries(state, next_attempt_at)""",
            """CREATE TABLE human_users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
            )""",
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES human_users(id) ON DELETE CASCADE,
                secret_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX sessions_expiry ON sessions(expires_at)",
        ),
    ),
)
