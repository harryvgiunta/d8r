"""Small adapter to python-keyring's native OS stores; never a file-based vault.

The library discovers backends. D8R restricts that selection to its built-in
native stores so an installed plaintext/third-party fallback cannot receive secrets.
References are immutable and scoped to the configured data directory, allowing
JSON storage to commit a new reference before retiring its predecessor.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class CredentialError(ValueError):
    """A public credential-storage error, without underlying OS or secret details."""


_NATIVE_MODULES = frozenset({
    "keyring.backends.Windows", "keyring.backends.macOS",
    "keyring.backends.SecretService", "keyring.backends.kwallet",
    "keyring.backends.libsecret",
})
_UNAVAILABLE = (
    "Secure system keyring unavailable or locked. Unlock or configure your OS "
    "credential store, or turn off Remember to use credentials for this session only. "
    "D8R never falls back to plaintext credential storage."
)


def _secure_backend() -> KeyringBackend:
    """Use the library's discovery and priority order, but no unsafe fallback."""
    try:
        backend = keyring.get_keyring()
        if type(backend).__module__ == "keyring.backends.chainer":
            # Never call the chain: get_password could fall through to a plaintext
            # plugin. Retain the library's priority order among native stores only.
            backend = next((candidate for candidate in backend.backends
                            if type(candidate).__module__ in _NATIVE_MODULES), None)
        if backend is None or type(backend).__module__ not in _NATIVE_MODULES:
            raise CredentialError(_UNAVAILABLE)
        return backend
    except CredentialError:
        raise
    except Exception:
        raise CredentialError(_UNAVAILABLE) from None


class CredentialVault:
    """Opaque, data-directory-scoped references to native keyring entries.

    Each reference uses a distinct service name. In particular, this avoids the
    Windows backend's overwrite/move behavior for multiple users of one service.
    No keyring access occurs merely by constructing the adapter.
    """

    def __init__(self, data_dir: Path) -> None:
        canonical = os.path.normcase(str(Path(data_dir).expanduser().resolve()))
        self._namespace = "d8r:" + hashlib.sha256(os.fsencode(canonical)).hexdigest()
        self._backend: KeyringBackend | None = None

    def _store(self) -> KeyringBackend:
        if self._backend is None:
            self._backend = _secure_backend()
        return self._backend

    def _service(self, reference: str) -> str:
        try:
            valid = isinstance(reference, str) and UUID(reference).hex == reference
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise CredentialError("Invalid saved credential reference; re-enter and save the credentials.")
        return f"{self._namespace}:{reference}"

    def get(self, reference: str) -> str | None:
        service = self._service(reference)
        try:
            return self._store().get_password(service, "credential")
        except CredentialError:
            raise
        except Exception:
            raise CredentialError(_UNAVAILABLE) from None

    def put(self, value: str) -> str:
        if not isinstance(value, str):
            raise CredentialError("Credentials must be text; nothing was saved.")
        reference = uuid4().hex
        service = self._service(reference)
        backend = self._store()
        try:
            backend.set_password(service, "credential", value)
            if backend.get_password(service, "credential") != value:
                raise CredentialError("The system keyring could not verify the saved credential.")
        except Exception:
            # This entry was created for this attempt only; never touch an entry
            # referenced by an existing JSON snapshot on a failed write.
            try:
                backend.delete_password(service, "credential")
            except Exception:
                pass
            raise CredentialError(
                "Could not save and verify credentials in the system keyring. "
                "It may be locked, access may be denied, or the credential may exceed "
                "the OS store's size limit. Unlock it or turn off Remember for session-only use. "
                "No plaintext credentials were saved."
            ) from None
        return reference

    def delete(self, reference: str) -> None:
        service = self._service(reference)
        try:
            backend = self._store()
            if backend.get_password(service, "credential") is not None:
                backend.delete_password(service, "credential")
        except PasswordDeleteError:
            # A concurrent cleanup can remove the same retired entry. Only a
            # confirmed missing value is success; denied deletion is not hidden.
            if self.get(reference) is not None:
                raise CredentialError("Could not remove a retired credential from the system keyring.") from None
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("Could not remove a retired credential from the system keyring.") from None
