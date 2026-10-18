import json

import pytest

from app.auth import verify_password, verify_token
from app.cli import build_parser, main, run


def provision(database, *arguments):
    return run(build_parser().parse_args(arguments), database)


def get_client(database):
    with database.connect() as connection:
        return connection.execute("SELECT * FROM clients WHERE id = 'frigate-home'").fetchone()


def test_token_provision_rotate_revoke_and_scopes(database):
    message = provision(database, "create-client", "frigate-home", "--allow-prefix", "frigate")
    token = message.splitlines()[-1]
    client = get_client(database)
    assert len(token) >= 43
    assert client["token_hash"] != token
    assert verify_token(token, client["token_hash"])
    assert not verify_token("wrong", client["token_hash"])
    assert json.loads(client["allowed_prefixes_json"]) == ["frigate"]

    provision(database, "revoke-client", "frigate-home")
    assert get_client(database)["enabled"] == 0
    rotated = provision(database, "rotate-client", "frigate-home").splitlines()[-1]
    client = get_client(database)
    assert client["enabled"] == 1
    assert verify_token(rotated, client["token_hash"])
    assert not verify_token(token, client["token_hash"])

    provision(database, "set-client-prefixes", "frigate-home", "--allow-prefix", "backup.status")
    assert json.loads(get_client(database)["allowed_prefixes_json"]) == ["backup.status"]
    provision(database, "set-client-prefixes", "frigate-home", "--unrestricted")
    assert json.loads(get_client(database)["allowed_prefixes_json"]) == []


def test_admin_password_is_argon2id_and_never_returned(database, monkeypatch):
    password = "a sufficiently long password"
    monkeypatch.setattr("app.cli.read_password", lambda: password)
    message = provision(database, "create-admin", "admin")
    with database.connect() as connection:
        user = connection.execute("SELECT * FROM human_users").fetchone()
    assert user["password_hash"].startswith("$argon2id$")
    assert verify_password(password, user["password_hash"])
    assert not verify_password("wrong password", user["password_hash"])
    assert not verify_password(password, "broken hash")
    assert password not in message


def test_short_password_is_rejected(database, monkeypatch):
    monkeypatch.setattr("app.cli.read_password", lambda: "short")
    with pytest.raises(ValueError, match="12 characters"):
        provision(database, "create-admin", "admin")


@pytest.mark.parametrize("command", ["rotate-client", "revoke-client"])
def test_unknown_client_fails(database, command):
    with pytest.raises(ValueError, match="Client not found"):
        provision(database, command, "missing")


def test_invalid_scope_is_rejected_before_provisioning(database):
    with pytest.raises(ValueError, match="Invalid channel"):
        provision(database, "create-client", "frigate-home", "--allow-prefix", "Frigate.*")
    assert get_client(database) is None


def test_cli_does_not_echo_invalid_environment_secrets(monkeypatch, capsys):
    sensitive_value = "SECRET-VALUE-THAT-MUST-NOT-BE-LOGGED"
    monkeypatch.setenv("SES_ENV", sensitive_value)
    with pytest.raises(SystemExit) as error:
        main(["init-db"])
    assert error.value.code == 2
    assert sensitive_value not in capsys.readouterr().err
