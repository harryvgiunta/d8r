"""Generic credential rotation must not orphan saved database targets."""

import pytest

from d8r.engine.connectors import profile_id, profile_secrets, validate_profile
from d8r.tui.session import Session


def test_nested_credentials_rotate_without_retargeting_workspace(sources, tmp_path):
    profile = validate_profile({
        "backend": "trino", "display": "Warehouse",
        "options": {
            "host": "warehouse.example", "port": "8443", "user": "reader",
            "catalog": "lake", "schema": "analytics", "http_scheme": "https",
            "password": "  old password  ",
            "http_headers": {"Authorization": "Bearer old-header-secret"},
        },
    })
    session = Session(sources, data_dir=tmp_path)
    session.remember_backend(profile)
    key = profile_id(profile)
    session.restore_source(key)
    session.save_workspace(source=key, dialect="trino", document="\\from events")
    stale = Session(sources, data_dir=tmp_path)
    replacement = validate_profile({
        **profile,
        "options": {**profile["options"], "password": "  new password  ",
                    "http_headers": {"Authorization": "Bearer new-header-secret"}},
    })
    assert profile_id(replacement) == key
    session.remember_backend(replacement)
    session.save_fn("event_copy", "", "\\from events", "")
    with pytest.raises(ValueError, match="changed outside"):
        stale.remember_backend(profile)
    restored = Session(sources, data_dir=tmp_path)
    assert restored.backend_profiles == [replacement]
    assert restored.source_key() == key
    assert not restored.source_connected()
    assert restored.dialect == "trino"
    returned = restored.saved_source_profile(key)
    returned["options"]["http_headers"]["Authorization"] = "mutated copy"
    assert restored.saved_source_profile(key)["options"] == replacement["options"]
    assert set(profile_secrets(replacement)) >= {"  new password  ", "Bearer new-header-secret"}
    workspace = restored.workspace_path.read_text()
    assert all(secret not in workspace for secret in profile_secrets(profile) + profile_secrets(replacement))
    other_schema = {**replacement, "options": {**replacement["options"], "schema": "another"}}
    assert profile_id(other_schema) != key
    other_user = {**replacement, "options": {**replacement["options"], "user": "another_reader"}}
    restored.remember_backend(other_user)
    assert restored.saved_source_profile(key)["options"]["user"] == "reader"
    assert restored.saved_source_profile(profile_id(other_user))["options"]["user"] == "another_reader"


def test_advanced_trino_authentication_cannot_bypass_https():
    profile = {
        "backend": "trino", "display": "Warehouse",
        "options": {
            "host": "warehouse.example", "user": "reader", "catalog": "lake",
            "http_scheme": "http", "http_headers": {"Authorization": "Bearer private-token"},
        },
    }
    with pytest.raises(ValueError, match="require HTTPS") as failure:
        validate_profile(profile)
    assert "private-token" not in str(failure.value)
    secure = validate_profile({**profile, "options": {**profile["options"], "http_scheme": "https"}})
    assert secure["options"]["http_headers"]["Authorization"] == "Bearer private-token"
    anonymous = validate_profile({**profile, "options": {key: value for key, value in profile["options"].items()
                                                       if key != "http_headers"}})
    assert anonymous["options"]["http_scheme"] == "http"
