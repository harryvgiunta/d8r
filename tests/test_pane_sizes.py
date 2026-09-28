"""Pane geometry preferences use the same safe settings transaction as other edits."""

import pytest

from d8r import storage
from d8r.tui.session import Session


def test_sparse_pane_sizes_survive_partial_updates_without_load_rewrite(sources, tmp_path):
    path = tmp_path / "settings.json"
    original = b'{"pane_sizes":{"explorer":48},"panes":{"sql":false}}\n'
    path.write_bytes(original)
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert session.pane_sizes["explorer"] == 48
    assert path.read_bytes() == original

    session.update_settings(pane_sizes={"ai": 63, "editor": 1})
    session.update_settings(pane_sizes={"editor": 1000})
    restored = Session(sources, data_dir=tmp_path)
    assert restored.pane_sizes == {"explorer": 48, "ai": 63, "editor": 1000}
    assert restored.pane_visibility["sql"] is False


@pytest.mark.parametrize("sizes", [
    {"explorer": True}, {"editor": 0}, {"ai": 1001}, {"unknown": 20},
])
def test_invalid_pane_sizes_preserve_saved_and_live_preferences(sources, sizes):
    session = Session(sources)
    session.update_settings(pane_sizes={"explorer": 48, "editor": 16, "ai": 52})
    before = session.settings_path.read_bytes()
    with pytest.raises(ValueError):
        session.update_settings(pane_sizes=sizes)
    assert session.pane_sizes == {"explorer": 48, "editor": 16, "ai": 52}
    assert session.settings_path.read_bytes() == before


def test_failed_pane_resize_save_preserves_preference_and_allows_retry(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(pane_sizes={"explorer": 48})
    before = session.settings_path.read_bytes()

    def deny_replace(source, destination):
        raise PermissionError("unwritable")

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", deny_replace)
        with pytest.raises(ValueError, match="Cannot save local settings"):
            session.update_settings(pane_sizes={"explorer": 62})
    assert session.pane_sizes["explorer"] == 48
    assert session.settings_path.read_bytes() == before
    session.update_settings(pane_sizes={"explorer": 62})
    assert Session(sources).pane_sizes["explorer"] == 62
