"""Operator provisioning; tokens print once and passwords use a hidden prompt."""

import argparse
import getpass
import json
import re
import sqlite3
import warnings
from uuid import uuid4

from pydantic import ValidationError

from app.auth import hash_password, issue_token
from app.channels import validate_channel
from app.config import Settings
from app.db import Database, utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ses")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Apply pending SQLite migrations")
    admin = commands.add_parser(
        "create-admin", help="Create a dashboard user (hidden password prompt)"
    )
    admin.add_argument("username")
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
