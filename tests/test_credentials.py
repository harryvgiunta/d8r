"""Credential references never cross homes, overwrite snapshots, or use unsafe stores."""

import keyring
from keyring.backends import fail, null
import pytest

from d8r.credentials import CredentialError, CredentialVault, _secure_backend


def test_references_are_scoped_and_rotation_cannot_overwrite_old_snapshot(tmp_path):
    home = tmp_path / "home"
    first = CredentialVault(home)
    previous = first.put(" previous secret ")
    rotated = first.put("new secret")
    same_home = CredentialVault(home / ".." / "home")
    another_home = CredentialVault(tmp_path / "other")
    assert previous != rotated
    assert same_home.get(previous) == " previous secret "
    assert same_home.get(rotated) == "new secret"
    assert another_home.get(previous) is None
    first.delete(rotated)
    assert same_home.get(previous) == " previous secret "
    assert same_home.get(rotated) is None
    first.delete(previous)


@pytest.mark.parametrize("backend", [fail.Keyring(), null.Keyring()])
def test_disabled_or_unavailable_backend_is_not_secure_storage(monkeypatch, backend):
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    with pytest.raises(CredentialError, match="never falls back to plaintext"):
        _secure_backend()


def test_unrecognized_backend_cannot_receive_credentials(monkeypatch, credential_backend):
    # Third-party backends (including plaintext files) are not trusted merely
    # because they advertise a high priority to python-keyring.
    monkeypatch.setattr(keyring, "get_keyring", lambda: credential_backend)
    with pytest.raises(CredentialError, match="Secure system keyring unavailable"):
        _secure_backend()
    assert credential_backend.values == {}


def test_failed_readback_discards_new_entry_but_keeps_previous_snapshot(tmp_path, monkeypatch, credential_backend):
    vault = CredentialVault(tmp_path)
    previous = vault.put("previous value")
    original = dict(credential_backend.values)
    get = credential_backend.get_password

    def wrong_new_value(service, username):
        return get(service, username) if (service, username) in original else "wrong result"

    monkeypatch.setattr(credential_backend, "get_password", wrong_new_value)
    with pytest.raises(CredentialError, match="save and verify"):
        vault.put("replacement secret")
    assert credential_backend.values == original
    assert vault.get(previous) == "previous value"


def test_partial_write_failure_is_sanitized_and_does_not_leave_new_secret(tmp_path, monkeypatch, credential_backend):
    previous = CredentialVault(tmp_path).put("original")
    original = dict(credential_backend.values)

    def denied_after_write(service, username, value):
        credential_backend.values[service, username] = value
        raise RuntimeError("driver accidentally includes sensitive-value")

    monkeypatch.setattr(credential_backend, "set_password", denied_after_write)
    with pytest.raises(CredentialError) as failure:
        CredentialVault(tmp_path).put("sensitive-value")
    assert "sensitive-value" not in str(failure.value)
    assert "session-only" in str(failure.value)
    assert credential_backend.values == original
    assert CredentialVault(tmp_path).get(previous) == "original"


def test_invalid_reference_cannot_reach_another_service(tmp_path, monkeypatch, credential_backend):
    def forbidden(*args):
        pytest.fail("Invalid references must not query the native store")

    monkeypatch.setattr(credential_backend, "get_password", forbidden)
    with pytest.raises(CredentialError, match="Invalid saved credential reference"):
        CredentialVault(tmp_path).get("other-app:credential")


def test_delete_failure_keeps_credential_and_reports_safe_error(tmp_path, monkeypatch, credential_backend):
    vault = CredentialVault(tmp_path)
    reference = vault.put("secret-not-in-error")

    def denied(*args):
        raise RuntimeError("secret-not-in-error")

    monkeypatch.setattr(credential_backend, "delete_password", denied)
    with pytest.raises(CredentialError) as failure:
        vault.delete(reference)
    assert "secret-not-in-error" not in str(failure.value)
    assert vault.get(reference) == "secret-not-in-error"
