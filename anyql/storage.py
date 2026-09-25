"""Local, token-free memory with atomic writes and stale-session protection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import UUID

# How long a lock or temp file may exist before it is assumed to belong to a
# crashed save. A save is two writes plus one fsync (sub-second), so anything
# older than a minute is debris, not an in-flight save.
_STALE_SECONDS = 60.0


def data_directory() -> Path:
    """Resolve the user data directory without creating it."""
    override = os.environ.get("ANYQL_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "anyql"


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key "{key}"')
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _record(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or value.keys() != keys:
        raise ValueError(f"{label} must contain exactly {', '.join(sorted(keys))}")
    return value


def _text(value: object, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{label} must be {'a nonempty string' if nonempty else 'a string'}")


def _validate(value: object) -> dict:
    """Validate structure, never parse/execute bodies against the active schema."""
    document = _record(value, {"version", "functions", "d1_profiles"}, "memory")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("unsupported memory version (expected 1)")
    functions = document["functions"]
    if not isinstance(functions, list):
        raise ValueError("functions must be a list")
    names = set()
    for function in functions:
        fn = _record(function, {"name", "params", "body", "description"}, "function")
        _text(fn["name"], "function name", nonempty=True)
        _text(fn["body"], "function body")
        _text(fn["description"], "function description")
        if fn["name"] in names:
            raise ValueError("duplicate function name")
        names.add(fn["name"])
        if not isinstance(fn["params"], list):
            raise ValueError("function params must be an ordered list")
        for param in fn["params"]:
            _text(param, "function parameter", nonempty=True)
        if len(set(fn["params"])) != len(fn["params"]):
            raise ValueError("duplicate function parameter")
    profiles = document["d1_profiles"]
    if not isinstance(profiles, list):
        raise ValueError("d1_profiles must be a list")
    identities = set()
    for profile in profiles:
        record = _record(profile, {"account_id", "database", "display"}, "D1 profile")
        for key, text in record.items():
            _text(text, f"D1 profile {key}", nonempty=True)
        try:
            database = UUID(record["database"])
        except ValueError:
            raise ValueError("D1 profile database must be a resolved database UUID") from None
        identity = (record["account_id"], database)
        if identity in identities:
            raise ValueError("duplicate D1 profile")
        identities.add(identity)
    return document


class MemoryStore:
    """One loaded snapshot; errors preserve the file and block unsafe writes."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.path = (data_directory() if data_dir is None else Path(data_dir)) / "memory.json"
        self.error = ""
        self.document = {"version": 1, "functions": [], "d1_profiles": []}
        self._snapshot: bytes | None = None
        try:
            self._snapshot = self._read()
            if self._snapshot is not None:
                self.document = _validate(json.loads(
                    self._snapshot.decode("utf-8"), object_pairs_hook=_object,
                    parse_constant=_constant,
                ))
        except (OSError, ValueError, RecursionError) as exc:
            self.error = (
                f"Cannot load local memory at {self.path}: {exc}. "
                "The original file is unchanged; repair it or move it aside, then restart anyQL. "
                "Local saves are disabled until restart."
            )
        self._sweep()

    def _read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _sweep(self) -> None:
        """Remove temp files older than the grace period — orphaned debris of
        crashed saves. Fresh ones belong to a save in flight, active or not."""
        deadline = time.time() - _STALE_SECONDS
        try:
            artifacts = list(self.path.parent.glob(".memory-*.tmp"))
        except OSError:
            return
        for artifact in artifacts:
            try:
                if artifact.stat().st_mtime < deadline:
                    artifact.unlink(missing_ok=True)
            except OSError:
                pass

    def _acquire_lock(self, lock: Path) -> None:
        """Take the save lock, clearing it only when it is older than the
        grace period — a crashed save's lock, not one being held now."""
        for attempt in (0, 1):
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if attempt:
                    raise ValueError(
                        f"Local memory is busy at {self.path}. Retry after the other "
                        f"save finishes; a lock left by a crash clears itself after "
                        f"{int(_STALE_SECONDS)} seconds."
                    ) from None
                try:
                    age = time.time() - lock.stat().st_mtime
                except FileNotFoundError:
                    continue  # vanished between checks; retry the open
                if age <= _STALE_SECONDS:
                    raise ValueError(
                        f"Local memory is busy at {self.path}. Retry after the other "
                        f"save finishes; a lock left by a crash clears itself after "
                        f"{int(_STALE_SECONDS)} seconds."
                    ) from None
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass  # raced another taker; retry the open
                continue
            os.close(descriptor)
            return

    def save(self, functions: list[dict], profiles: list[dict[str, str]]) -> None:
        """Commit a complete snapshot, only if the loaded file is still current."""
        if self.error:
            raise ValueError(self.error)
        document = _validate({"version": 1, "functions": functions, "d1_profiles": profiles})
        data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        lock = self.path.with_suffix(".lock")
        temporary: str | None = None
        locked = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._acquire_lock(lock)
            locked = True
            if self._read() != self._snapshot:
                raise ValueError(
                    f"Local memory changed outside this session at {self.path}; nothing was saved. "
                    "Copy your unsaved draft and restart anyQL to load the latest memory."
                )
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".memory-", suffix=".tmp", dir=self.path.parent, delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Detect external edits made while the temporary file was being written.
            if self._read() != self._snapshot:
                raise ValueError(
                    f"Local memory changed outside this session at {self.path}; nothing was saved. "
                    "Copy your unsaved draft and restart anyQL to load the latest memory."
                )
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise ValueError(
                f"Cannot save local memory at {self.path}: {exc}. "
                "Nothing was changed; check directory permissions and free disk space, then retry."
            ) from exc
        finally:
            # Cleanup must not turn a successful replace into a reported failed save.
            for artifact in (temporary, lock if locked else None):
                if artifact is not None:
                    try:
                        Path(artifact).unlink(missing_ok=True)
                    except OSError:
                        pass
        self._snapshot = data
        self.document = document
