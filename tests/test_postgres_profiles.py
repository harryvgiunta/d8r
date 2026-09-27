"""Credential persistence and disconnected PostgreSQL target identity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from d8r.tui.session import Session


PROFILE = {
    "host": "localhost", "port": "55433", "database": "d8r", "user": "reader",
    "password": "  preserved password  ", "schema": "public", "sslmode": "prefer",
    "display": "Local PostgreSQL",
}


def test_postgres_profiles_survive_other_memory_saves_and_stale_writers(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    session.remember_postgres(PROFILE)
    stale = Session(sources, data_dir=tmp_path)
    session.remember_postgres({**PROFILE, "password": "rotated"})
    session.save_fn("events_copy", "", "\\from events", "")
    session.remember_d1("account", "12345678-1234-5678-9abc-123456789abc", "D1", "token")
    loaded = Session(sources, data_dir=tmp_path)
    assert loaded.postgres_profiles == [{**PROFILE, "password": "rotated"}]
    with pytest.raises(ValueError, match="changed outside"):
        stale.remember_postgres({**PROFILE, "password": "stale"})
    assert stale.postgres_profiles == [PROFILE]
    assert Session(sources, data_dir=tmp_path).postgres_profiles == loaded.postgres_profiles


def test_postgres_workspace_restores_disconnected_and_reconnects_same_target(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    session.remember_postgres(PROFILE)
    metadata = {key: value for key, value in PROFILE.items() if key not in {"password", "display"}}
    connection = SimpleNamespace(con=SimpleNamespace(info=SimpleNamespace(server_version=170009)))
    source = replace(sources["demo"], id="local-pg", kind="postgres-live", dialect="postgres",
                     postgres=metadata, con=connection, datasets={})
    session.register(source)
    target = session.source_key()
    session.save_workspace(source=target, dialect="postgres", document="\\from events")

    def refuse_network(*args, **kwargs):
        raise AssertionError("startup must never connect")

    monkeypatch.setattr("d8r.tui.session.add_postgres_source", refuse_network)
    restored = Session(sources, data_dir=tmp_path)
    assert not restored.source_connected()
    assert restored.dialect == "postgres"
    assert restored.source_key() == target
    assert restored.saved_source_profile(target) == {**PROFILE, "kind": "postgres-live"}
    assert restored.run("\\from events").error
    restored.register(source)
    assert restored.source_key() == target
    assert target not in restored.sources
    assert [value for _, value in restored.source_options()].count("local-pg") == 1
    assert PROFILE["password"] not in restored.workspace_path.read_text()


def test_schema_user_and_tls_define_distinct_saved_targets(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    for profile in (PROFILE, {**PROFILE, "schema": "analytics"},
                    {**PROFILE, "user": "another"}, {**PROFILE, "sslmode": "verify-full"}):
        session.remember_postgres(profile)
    options = [(label, key) for label, key in session.source_options() if key.startswith("saved-postgres:")]
    assert len({key for _, key in options}) == 4
    assert all(session.saved_source_profile(key) for _, key in options)
    assert Session(sources, data_dir=tmp_path).postgres_profiles[0]["password"] == PROFILE["password"]
