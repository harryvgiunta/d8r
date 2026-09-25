"""D8R's Textual terminal UI — the application itself.

The React front end and its HTTP sidecar are gone: this package is the whole
product. It renders the `\\command` document, the live schema, and the rows the
engine returns, all in one process, with no server anywhere.
"""

from .app import D8RApp, main
from .results import ResultsTable
from .palette import EditorPane
from .session import PREVIEW_ROW_CAP, Session

__all__ = ["D8RApp", "EditorPane", "PREVIEW_ROW_CAP", "ResultsTable", "Session", "main"]
