"""Focusable pane handles; preferred sizes are independent of viewport clamping."""

from __future__ import annotations

from typing import Literal

from textual import events
from textual.message import Message
from textual.widget import Widget


class PaneSplitter(Widget, can_focus=True):
    """Resize a sibling pane, emitting one persistence request per user action.

    Mount between the target and its flexible sibling. For a right/bottom target,
    pass ``reverse=True``. Set the saved preference with ``set_preferred_size``;
    call ``refresh_size`` after parent size or pane visibility changes. Sibling
    handles share the available space and never overwrite preferred sizes merely
    because the terminal shrank. ``min_remaining`` reserves room for each other
    visible, non-target sibling (normally the workbench or results).

    Handle ``Changed`` in the app with Session.update_settings(pane_sizes=...).
    On failure, display the error and restore the saved preference. This widget
    deliberately knows nothing about storage or application sessions.
    """

    DEFAULT_CSS = """
    PaneSplitter {
        width: 1;
        height: 1fr;
        background: $panel-lighten-2;
    }
    PaneSplitter.horizontal {
        width: 1fr;
        height: 1;
    }
    PaneSplitter:hover, PaneSplitter:focus, PaneSplitter.dragging {
        background: $accent;
    }
    """

    class Changed(Message):
        """An explicit drag/key adjustment requests saving a preferred cell size."""

        def __init__(self, splitter: PaneSplitter, size: int) -> None:
            super().__init__()
            self.splitter = splitter
            self.setting_key = splitter.setting_key
            self.size = size

        @property
        def control(self) -> PaneSplitter:
            return self.splitter

    def __init__(
        self, pane_selector: str, dimension: Literal["width", "height"],
        setting_key: str, *, reverse: bool = False, min_size: int = 8,
        max_size: int = 1000, min_remaining: int | None = None, **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if dimension not in ("width", "height"):
            raise ValueError("Pane dimension must be width or height.")
        if not 1 <= min_size <= max_size <= 1000:
            raise ValueError("Pane bounds must satisfy 1 <= min <= max <= 1000.")
        if min_remaining is not None and min_remaining < 1:
            raise ValueError("Remaining pane space must be positive.")
        self.pane_selector = pane_selector
        self.dimension = dimension
        self.setting_key = setting_key
        self.reverse = reverse
        self.min_size = min_size
        self.max_size = max_size
        self.min_remaining = min_remaining if min_remaining is not None else (24 if dimension == "width" else 5)
        self.preferred_size: int | None = None
        self._drag: tuple[int, int, int | None] | None = None
        self.set_class(dimension == "height", "horizontal")
        arrows = "Left/Right" if dimension == "width" else "Up/Down"
        self.tooltip = f"Drag to resize; focus and press {arrows} (Shift: 5 cells). Escape cancels a drag."

    @property
    def pane(self) -> Widget:
        return self.parent.query_one(self.pane_selector, Widget)

    def set_preferred_size(self, size: int) -> None:
        """Apply a saved cell preference without emitting a persistence request."""
        self.preferred_size = max(self.min_size, min(self.max_size, size))
        if self.is_mounted:
            self.refresh_size()

    def _space(self) -> tuple[list[PaneSplitter], int]:
        children = [child for child in self.parent.children if child.display]
        handles = [child for child in children if isinstance(child, PaneSplitter)
                   and child.dimension == self.dimension and child.pane.display]
        targets = {handle.pane for handle in handles}
        other = [child for child in children if child not in targets and child not in handles]
        reserved = max((handle.min_remaining for handle in handles), default=self.min_remaining)
        available = getattr(self.parent.content_size, self.dimension)
        # Handles occupy one terminal cell. Target margins are outside their size.
        margins = sum(getattr(pane.styles.margin, self.dimension) for pane in targets)
        return handles, max(0, available - len(handles) - margins - len(other) * reserved)

    def refresh_size(self) -> None:
        """Reclamp all sibling panes to the viewport, preserving their preferences."""
        if not self.is_mounted or not self.display or not self.pane.display:
            return
        handles, available = self._space()
        if not handles or not getattr(self.parent.content_size, self.dimension):
            return
        sizes = []
        for handle in handles:
            if handle.preferred_size is None:
                handle.preferred_size = max(handle.min_size, min(
                    handle.max_size, getattr(handle.pane.outer_size, handle.dimension),
                ))
            sizes.append(handle.preferred_size)
        minimums = [handle.min_size for handle in handles]
        if sum(sizes) > available:
            floor = sum(minimums)
            if floor >= available:
                sizes = [available * size // floor for size in minimums]
            else:
                extra = sum(size - minimum for size, minimum in zip(sizes, minimums))
                sizes = [minimum + (size - minimum) * (available - floor) // extra
                         for size, minimum in zip(sizes, minimums)]
        for handle, size in zip(handles, sizes):
            setattr(handle.pane.styles, handle.dimension, size)

    def render(self) -> str:
        return ""

    def on_mount(self) -> None:
        self.call_after_refresh(self.refresh_size)

    def on_resize(self) -> None:
        self.call_after_refresh(self.refresh_size)

    def _coordinate(self, event: events.MouseEvent) -> int:
        return event.screen_x if self.dimension == "width" else event.screen_y

    def _adjust(self, size: int) -> None:
        handles, available = self._space()
        maximum = max(self.min_size, available - sum(handle.min_size for handle in handles if handle is not self))
        self.preferred_size = max(self.min_size, min(self.max_size, maximum, size))
        self.refresh_size()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self.focus()
        self._drag = (self._coordinate(event), getattr(self.pane.outer_size, self.dimension), self.preferred_size)
        self.capture_mouse()
        self.add_class("dragging")

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag is None:
            return
        event.stop()
        origin, size, previous = self._drag
        delta = self._coordinate(event) - origin
        if delta == 0:
            self.preferred_size = previous
            self.refresh_size()
        else:
            self._adjust(size + (-delta if self.reverse else delta))

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag is None or event.button != 1:
            return
        event.stop()
        self.on_mouse_move(event)
        _, _, previous = self._drag
        self._drag = None
        self.release_mouse()
        self.remove_class("dragging")
        if self.preferred_size != previous:
            self.post_message(self.Changed(self, self.preferred_size))

    def _cancel_drag(self) -> None:
        if self._drag is not None:
            _, _, self.preferred_size = self._drag
            self._drag = None
            self.release_mouse()
            self.remove_class("dragging")
            self.refresh_size()

    def on_mouse_release(self) -> None:
        self._cancel_drag()

    def on_hide(self) -> None:
        self._cancel_drag()

    def on_unmount(self) -> None:
        self._cancel_drag()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._drag is not None:
            self._cancel_drag()
        else:
            keys = ("left", "right") if self.dimension == "width" else ("up", "down")
            key = event.key.removeprefix("shift+")
            if key not in keys:
                return
            delta = (-1 if key == keys[0] else 1) * (5 if event.key.startswith("shift+") else 1)
            before = self.preferred_size
            self._adjust(getattr(self.pane.outer_size, self.dimension) + (-delta if self.reverse else delta))
            if self.preferred_size != before:
                self.post_message(self.Changed(self, self.preferred_size))
        event.stop()
        event.prevent_default()
