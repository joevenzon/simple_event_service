"""Operator provisioning; tokens print once and passwords use a hidden prompt."""

import argparse
import getpass
import json
import re
import sqlite3
import warnings
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.auth import hash_password, issue_token
from app.channels import validate_channel
from app.config import Settings
from app.db import Database, utc_now
from app.notifications import NotificationRule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ses")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Apply pending SQLite migrations")
    admin = commands.add_parser(
        "create-admin", help="Create a dashboard user (hidden password prompt)"
    )
    admin.add_argument("username")
    revoke_sessions = commands.add_parser("revoke-sessions", help="Revoke all sessions for a user")
    revoke_sessions.add_argument("username")
    client = commands.add_parser("create-client", help="Create a client and display its token once")
    client.add_argument("client_id")
    client.add_argument("--name", help="Display name; defaults to client ID")
    client.add_argument(
        "--allow-prefix",
        action="append",
        default=[],
        help="Repeat for multiple scopes; omit for all",
    )
    for name in ("rotate-client", "revoke-client"):
        command = commands.add_parser(name)
        command.add_argument("client_id")
    scopes = commands.add_parser("set-client-prefixes", help="Replace a client's channel scopes")
    scopes.add_argument("client_id")
    group = scopes.add_mutually_exclusive_group(required=True)
    group.add_argument("--allow-prefix", action="append")
    group.add_argument("--unrestricted", action="store_true")
    rule = commands.add_parser("add-notification-rule", help="Allow or suppress push for a subtree")
    rule.add_argument("channel_prefix", help="A valid channel prefix, or '*' for all channels")
    rule.add_argument("--disabled", action="store_true", help="Suppress matching notifications")
    rule.add_argument(
        "--min-severity", choices=("debug", "info", "warning", "error", "critical"), default="info"
    )
    rule.add_argument(
        "--ntfy-priority",
        type=int,
        choices=range(1, 6),
        help="Override the severity-based priority",
    )
    rule.add_argument("--rule-priority", type=int, default=0, help="Higher wins equal-prefix ties")
    rule.add_argument("--sort-order", type=int, default=0, help="Lower wins remaining ties")
    commands.add_parser("list-notification-rules", help="List policy as JSON in insertion order")
    remove = commands.add_parser("remove-notification-rule", help="Remove a rule for future events")
    remove.add_argument("rule_id", type=int)
    requeue = commands.add_parser(
        "requeue-delivery", help="Reset a failed event delivery for retry"
    )
    requeue.add_argument("event_id", type=UUID)
    return parser


def read_password() -> str:
    # Refuse getpass's echoing fallback when there is no usable terminal.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("Password (at least 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    return password


def run(args: argparse.Namespace, database: Database) -> str:
    database.initialize()
    if args.command == "init-db":
        return "Database initialized."
    if args.command == "add-notification-rule":
        policy = NotificationRule(
            channel_prefix=args.channel_prefix,
            enabled=not args.disabled,
            min_severity=args.min_severity,
            ntfy_priority=args.ntfy_priority,
            rule_priority=args.rule_priority,
            sort_order=args.sort_order,
        )
        with database.transaction() as connection:
            result = connection.execute(
                """INSERT INTO notification_rules(channel_prefix, enabled, min_severity,
                    ntfy_priority, rule_priority, sort_order) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    policy.channel_prefix,
                    policy.enabled,
                    policy.min_severity,
                    policy.ntfy_priority,
                    policy.rule_priority,
                    policy.sort_order,
                ),
            )
        return f"Notification rule {result.lastrowid} created."
    if args.command == "list-notification-rules":
        with database.connect() as connection:
            rules = [
                dict(row)
                for row in connection.execute("SELECT * FROM notification_rules ORDER BY id")
            ]
        return json.dumps(rules, indent=2)
    if args.command == "remove-notification-rule":
        with database.transaction() as connection:
            result = connection.execute(
                "DELETE FROM notification_rules WHERE id = ?", (args.rule_id,)
            )
            if result.rowcount != 1:
                raise ValueError("Notification rule not found")
        return "Notification rule removed; queued deliveries are unchanged."
    if args.command == "requeue-delivery":
        with database.transaction() as connection:
            result = connection.execute(
                """UPDATE notification_deliveries SET state = 'pending', attempts = 0,
                    next_attempt_at = ?, last_attempt_at = NULL, last_error = NULL,
                    provider_response_id = NULL, sent_at = NULL, claim_token = NULL
                    WHERE event_id = ? AND state = 'failed'""",
                (utc_now(), str(args.event_id)),
            )
            if result.rowcount != 1:
                raise ValueError("Failed delivery not found")
        return "Failed delivery requeued."
    if args.command == "revoke-sessions":
        with database.transaction() as connection:
            user = connection.execute(
                "SELECT id FROM human_users WHERE username = ?", (args.username,)
            ).fetchone()
            if user is None:
                raise ValueError("User not found")
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        return "User sessions revoked."
    if args.command == "create-admin":
        if not args.username.strip() or len(args.username) > 100:
            raise ValueError("Username must contain 1-100 characters")
        password_hash = hash_password(read_password())
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO human_users(id, username, password_hash, created_at)
                    VALUES (?, ?, ?, ?)""",
                (str(uuid4()), args.username, password_hash, utc_now()),
            )
        return "Dashboard user created."
    if args.command == "create-client":
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", args.client_id, re.ASCII) is None:
            raise ValueError("Client ID must be 1-80 lowercase ASCII letters, digits, '-' or '_'")
        name = args.name if args.name is not None else args.client_id
        if not name.strip() or len(name) > 200:
            raise ValueError("Client display name must contain 1-200 characters")
        prefixes = sorted({validate_channel(prefix) for prefix in args.allow_prefix})
        token, digest = issue_token()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO clients
                    (id, name, token_hash, allowed_prefixes_json, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                (args.client_id, name, digest, json.dumps(prefixes), utc_now()),
            )
        return f"Client created. Save this token now; it will not be shown again:\n{token}"
    if args.command == "set-client-prefixes":
        prefixes = sorted({validate_channel(prefix) for prefix in args.allow_prefix or []})
        with database.transaction() as connection:
            result = connection.execute(
                "UPDATE clients SET allowed_prefixes_json = ? WHERE id = ?",
                (json.dumps(prefixes), args.client_id),
            )
            if result.rowcount != 1:
                raise ValueError("Client not found")
        return "Client channel scopes updated."
    if args.command == "rotate-client":
        token, digest = issue_token()
        with database.transaction() as connection:
            result = connection.execute(
                "UPDATE clients SET token_hash = ?, enabled = 1 WHERE id = ?",
                (digest, args.client_id),
            )
            if result.rowcount != 1:
                raise ValueError("Client not found")
        return f"Token rotated and client enabled. Save this token now:\n{token}"
    with database.transaction() as connection:
        result = connection.execute(
            "UPDATE clients SET enabled = 0 WHERE id = ?", (args.client_id,)
        )
        if result.rowcount != 1:
            raise ValueError("Client not found")
    return "Client revoked."


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        message = run(args, Database(settings.database_path))
    except ValidationError:
        if args.command == "add-notification-rule":
            parser.exit(2, "Invalid notification rule; check its channel and policy fields.\n")
        # Pydantic's complete error representation can contain environment values.
        parser.exit(
            2, "Invalid environment configuration; check SES settings in docs/operations.md.\n"
        )
    except sqlite3.IntegrityError:
        parser.exit(
            2, "A client or username already exists, or a database constraint was violated.\n"
        )
    except (ValueError, RuntimeError, OSError, sqlite3.Error, getpass.GetPassWarning) as exc:
        parser.exit(2, f"{exc}\n")
    print(message)


if __name__ == "__main__":
    main()
