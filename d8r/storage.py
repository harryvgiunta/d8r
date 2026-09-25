"""Local credential/settings storage with atomic writes and stale-session protection.

New directories and files are owner-only on POSIX; Windows uses filesystem ACLs.
JSON files (and their backups) contain plaintext credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
from uuid import UUID

from d8r.ai.client import AIConfig
from d8r.engine.datasources import DIALECT_BY_NAME

# How long a lock or temp file may exist before it is assumed to belong to a
# crashed save. A save is two writes plus one fsync (sub-second), so anything
# older than a minute is debris, not an in-flight save.
_STALE_SECONDS = 60.0


def data_directory() -> Path:
    """Resolve the user data directory without creating it."""
    override = os.environ.get("D8R_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".d8r"


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


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
        if not isinstance(profile, dict) or profile.keys() not in (
            {"account_id", "database", "display"},
            {"account_id", "database", "display", "api_token"},
        ):
            raise ValueError("invalid D1 profile fields")
        record = {**profile, "api_token": profile.get("api_token", "")}
        for key in ("account_id", "database", "display"):
            _text(record[key], f"D1 profile {key}", nonempty=True)
        _text(record["api_token"], "D1 profile API token")
        try:
            database = UUID(record["database"])
        except ValueError:
            raise ValueError("D1 profile database must be a resolved database UUID") from None
        identity = (record["account_id"], database)
        if identity in identities:
            raise ValueError("duplicate D1 profile")
        identities.add(identity)
    return {**document, "d1_profiles": [
        {**profile, "api_token": profile.get("api_token", "")} for profile in profiles
    ]}


def _settings(value: object) -> dict:
    """Validate editable settings and fill omitted keys without retaining aliases."""
    defaults = {
        "version": 1, "intellisense": True,
        "panes": {"results": True, "sql": True, "history": True, "schema": True},
        "source": "demo", "dialect": "duckdb",
        "default_rows": 50,
        "ai": asdict(AIConfig()),
    }
    if not isinstance(value, dict) or not value.keys() <= defaults.keys():
        raise ValueError("invalid settings fields")
    document = defaults | value
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("unsupported settings version (expected 1)")
    if type(document["intellisense"]) is not bool:
        raise ValueError("intellisense must be a boolean")
    for field in ("panes", "ai"):
        provided = document[field]
        if not isinstance(provided, dict) or not provided.keys() <= defaults[field].keys():
            raise ValueError(f"invalid {field} settings fields")
        document[field] = defaults[field] | provided
    if any(type(visible) is not bool for visible in document["panes"].values()):
        raise ValueError("pane visibility must be a boolean")
    _text(document["source"], "source", nonempty=True)
    _text(document["dialect"], "dialect", nonempty=True)
    if document["dialect"] not in DIALECT_BY_NAME:
        raise ValueError("unsupported dialect")
    if type(document["default_rows"]) is not int or not 0 <= document["default_rows"] <= 1_000_000:
        raise ValueError("Default rows returned must be an integer between 0 and 1000000 (0 disables the default).")
    ai = document["ai"]
    for field in ("base_url", "model", "api_key"):
        _text(ai[field], f"AI {field}")
    if any(ai[field] for field in ("base_url", "model", "api_key")):
        AIConfig(**ai).validate()
    else:
        AIConfig(**ai).validate_limits()
    return document


class _JSONStore:
    """One loaded snapshot; errors preserve the file and block unsafe writes."""

    def __init__(self, data_dir: Path | None, name: str, default: dict, validate) -> None:
        self.path = (data_directory() if data_dir is None else Path(data_dir)) / f"{name}.json"
        self._name = name
        self._validate = validate
        self.error = ""
        self.document = default
        self._snapshot: bytes | None = None
        try:
            self._snapshot = self._read()
            if self._snapshot is not None:
                self.document = self._validate(json.loads(
                    self._snapshot.decode("utf-8"), object_pairs_hook=_object,
                    parse_constant=_constant,
                ))
        except (OSError, ValueError, RecursionError):
            self.error = (
                f"Cannot load local {self._name} at {self.path}: invalid or unreadable file. "
                "The original file is unchanged; repair it or move it aside, then restart D8R. "
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
            artifacts = list(self.path.parent.glob(f".{self._name}-*.tmp"))
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
                        f"Local {self._name} is busy at {self.path}. Retry after the other "
                        f"save finishes; a lock left by a crash clears itself after "
                        f"{int(_STALE_SECONDS)} seconds."
                    ) from None
                try:
                    age = time.time() - lock.stat().st_mtime
                except FileNotFoundError:
                    continue  # vanished between checks; retry the open
                if age <= _STALE_SECONDS:
                    raise ValueError(
                        f"Local {self._name} is busy at {self.path}. Retry after the other "
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

    def _save(self, value: dict) -> None:
        """Commit a complete snapshot, only if the loaded file is still current."""
        if self.error:
            raise ValueError(self.error)
        document = self._validate(value)
        try:
            data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError(f"Invalid local {self._name}; nothing was saved.") from None
        lock = self.path.with_suffix(".lock")
        temporary: str | None = None
        locked = False
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._acquire_lock(lock)
            locked = True
            if self._read() != self._snapshot:
                raise ValueError(
                    f"Local {self._name} changed outside this session at {self.path}; nothing was saved. "
                    f"Copy your unsaved draft and restart D8R to load the latest {self._name}."
                )
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{self._name}-", suffix=".tmp", dir=self.path.parent, delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Detect external edits made while the temporary file was being written.
            if self._read() != self._snapshot:
                raise ValueError(
                    f"Local {self._name} changed outside this session at {self.path}; nothing was saved. "
                    f"Copy your unsaved draft and restart D8R to load the latest {self._name}."
                )
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ValueError(
                f"Cannot save local {self._name} at {self.path}. "
                "Nothing was changed; check directory permissions and free disk space, then retry."
            ) from None
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


class MemoryStore(_JSONStore):
    """Durable function definitions and saved D1 credentials (memory schema v1)."""

    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__(data_dir, "memory", {"version": 1, "functions": [], "d1_profiles": []}, _validate)

    def save(self, functions: list[dict], profiles: list[dict[str, str]]) -> None:
        self._save({"version": 1, "functions": functions, "d1_profiles": profiles})


class SettingsStore(_JSONStore):
    """Editable app/AI preferences (settings schema v1), with safe snapshot saves."""

    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__(data_dir, "settings", _settings({}), _settings)

    def save(self, document: dict) -> None:
        self._save(document)


def _cursor(value: object) -> None:
    if not isinstance(value, list) or len(value) != 2 or any(type(item) is not int or item < 0 for item in value):
        raise ValueError("invalid workspace cursor")


def _chat_messages(messages: object) -> None:
    """Only complete exchanges may be replayed; loading never executes tools."""
    if not isinstance(messages, list):
        raise ValueError("chat messages must be a list")
    next_role = "user"
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("invalid chat message")
        role = message.get("role")
        if role != next_role:
            raise ValueError("incomplete chat exchange")
        if role == "user":
            _record(message, {"role", "content"}, "user message")
            _text(message["content"], "user message")
            next_role = "assistant"
        elif role == "assistant":
            calls = message.get("tool_calls")
            if calls is None:
                _record(message, {"role", "content"}, "assistant message")
                _text(message["content"], "assistant message", nonempty=True)
                next_role = "user"
            else:
                _record(message, {"role", "content", "tool_calls"}, "assistant tool message")
                if message["content"] is not None:
                    _text(message["content"], "assistant message")
                if not isinstance(calls, list) or not calls:
                    raise ValueError("invalid saved tool calls")
                for call in calls:
                    _record(call, {"id", "type", "function"}, "tool call")
                    _text(call["id"], "tool call id", nonempty=True)
                    if call["type"] != "function" or call["id"] in pending:
                        raise ValueError("invalid saved tool call")
                    function = _record(call["function"], {"name", "arguments"}, "tool function")
                    _text(function["name"], "tool function name", nonempty=True)
                    _text(function["arguments"], "tool arguments")
                    pending.add(call["id"])
                next_role = "tool"
        else:
            _record(message, {"role", "tool_call_id", "content"}, "tool result")
            _text(message["tool_call_id"], "tool result id", nonempty=True)
            _text(message["content"], "tool result")
            if message["tool_call_id"] not in pending:
                raise ValueError("unmatched saved tool result")
            pending.remove(message["tool_call_id"])
            if not pending:
                next_role = "assistant"
    if next_role != "user":
        raise ValueError("incomplete saved conversation")


def _workspace_document(value: object) -> dict:
    document = _record(value, {
        "version", "document", "document_id", "cursor", "source", "dialect", "sources",
        "active_view", "function_draft", "chats", "history",
    }, "workspace")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("unsupported workspace version (expected 1)")
    if document["document"] is not None:
        _text(document["document"], "workspace document")
    for name in ("document_id", "source", "dialect"):
        _text(document[name], f"workspace {name}")
    if document["dialect"] and document["dialect"] not in DIALECT_BY_NAME:
        raise ValueError("unsupported workspace dialect")
    _cursor(document["cursor"])
    if document["active_view"] not in ("workspace", "function"):
        raise ValueError("invalid workspace view")
    if not isinstance(document["sources"], dict):
        raise ValueError("invalid workspace sources")
    for key, source in document["sources"].items():
        _text(key, "source key", nonempty=True)
        _record(source, {"id", "display", "kind", "path"}, "workspace source")
        for name, text in source.items():
            _text(text, f"workspace source {name}")
    draft = document["function_draft"]
    if draft is not None:
        _record(draft, {"identity", "source", "selected", "name", "description", "parameters",
                        "body", "arguments", "cursor"}, "function draft")
        for name, text in draft.items():
            if name != "cursor":
                _text(text, f"function draft {name}")
        _cursor(draft["cursor"])
    if not isinstance(document["chats"], dict):
        raise ValueError("invalid saved chats")
    for key, chat in document["chats"].items():
        _text(key, "chat key", nonempty=True)
        _record(chat, {"messages", "transcript", "input", "turns", "visible"}, "chat")
        _chat_messages(chat["messages"])
        for field in ("transcript", "input"):
            _text(chat[field], f"chat {field}")
        if type(chat["turns"]) is not int or chat["turns"] < 0 or type(chat["visible"]) is not bool:
            raise ValueError("invalid saved chat state")
    if not isinstance(document["history"], list):
        raise ValueError("invalid query history")
    for entry in document["history"]:
        _record(entry, {"at", "source", "dialect", "rows", "ms", "doc", "target"}, "query history entry")
        for field in ("at", "source", "dialect", "doc", "target"):
            _text(entry[field], f"history {field}")
        if type(entry["rows"]) is not int or entry["rows"] < 0:
            raise ValueError("invalid history row count")
        if type(entry["ms"]) not in (int, float) or not 0 <= entry["ms"] < float("inf"):
            raise ValueError("invalid history duration")
    return document


class WorkspaceStore(_JSONStore):
    """Autosaved drafts and histories; never executable state or credentials."""

    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__(data_dir, "workspace", {
            "version": 1, "document": None, "document_id": "", "cursor": [0, 0],
            "source": "", "dialect": "", "sources": {}, "active_view": "workspace",
            "function_draft": None, "chats": {}, "history": [],
        }, _workspace_document)

    def save(self, document: dict) -> None:
        self._save(document)
