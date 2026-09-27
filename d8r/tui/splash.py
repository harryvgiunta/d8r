"""The 6-second boot splash: the dozer clears the data wall, D8R lands.

`play()` writes one frame every 50 ms for 6.000 s straight to a terminal, then
returns; the IDE's `main()` plays it, once, before the first frame. It is pure
stdlib and touches no server, no socket, and the engine not at all — a
keystroke never reaches it, and a non-tty (a pipe, a file, the pilot) skips it.

Choreography — 120 frames x 50 ms = 6.000 s:

  frames  0..20  CHAOS   dense fast streams of SQL / DuckDB / Polars text
                         and database endpoints fill the screen, scrolling
                         left — the old world, unreadable and relentless.
  frame     21   ENTRY   the D8R dozer appears at the left edge: one frame,
                         no fade.
  frames 21..43  SWEEP   it drives in, blade flat, pushing the whole wall of
                         data rightward until it spills off the right edge
                         and the machine parks flush against it. Behind the
                         blade: nothing. Clean.
  frames 44..54  WIPE    the D8R wordmark wipes in, left to right, in solid
                         blocks — no noise, no flicker.
  frames 56..71  TITLE   "THE DATA HARNESS" types in beneath it.
  frames 72..119 HOLD    the ending stays clean: wordmark, title, and the
                         dozer parked at the edge it cleared to.
"""

import sys
import time

FRAME_MS = 50
TOTAL_FRAMES = 120  # 120 x 50 ms = exactly 6.000 s

# --- choreography seams ------------------------------------------------------
ENTRY = 21     # the dozer appears, one frame, at the left edge
SPEED = 2      # columns/frame the blade drives right
PARK_X = 43    # parked flush right (43 + 19-wide art reaches the edge)
PARK_AT = 43   # blade reaches the edge; the whole wall has been pushed off
WIPE_AT = 44   # wordmark wipe begins (2 cols/frame over 11 frames)
TYPE_AT = 56   # subtitle typing begins, one character per frame

# --- geometry ---------------------------------------------------------------
ROWS, W = 8, 62
LOGO_ROWS, LOGO_X, LOGO_Y = 5, 20, 1        # wordmark box, cols 20..40
ART_Y = 3                                    # dozer art occupies rows 3..7
WALL_ROWS = (1, 2, 3, 4, 5, 6, 7)           # the data wall fills the screen
TITLE, TITLE_X, TITLE_ROW = "THE DATA HARNESS", 20, 7

# the data being replaced: tapes of real syntax + endpoints
TAPES = {
    0: ("SELECT user_id, sum(amount) AS revenue FROM events WHERE ts >= "
        "now() - INTERVAL '7d' GROUP BY 1 HAVING count(*) > 3 ORDER BY "
        "revenue DESC LIMIT 50 ; JOIN users ON users.id = events.user_id ; "),
    1: ("duckdb> ATTACH 'warehouse.duckdb' AS wh; COPY (SELECT * FROM "
        "wh.campaigns) TO 'out.parquet' (FORMAT PARQUET); FROM readings; "
        "SELECT * FROM sqlite_scan('legacy.db', 'orders'); "),
    2: ("pl.scan_parquet('events.parquet').filter(col('ts') > t0)"
        ".group_by('user_id').agg(pl.sum('amount')).join(users, on='id')"
        ".sort('revenue', descending=True)  "),
    3: ("postgres://orders@prod:5432  mysql://reviews  "
        "snowflake://analytics.public  bigquery://web_sessions  "
        "cloudflare:d1/stations  spend_log  conversions  customers  "),
}
ROW_TAPE = {1: 0, 2: 1, 3: 3, 4: 0, 5: 1, 6: 2, 7: 3}  # wall row -> tape
SCROLL_IN, PUSH = 4, 3                       # leftward chaos / rightward push

_G = {  # wordmark glyphs, 5x5
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "8": [" ███ ", "█   █", " ███ ", "█   █", " ███ "],
    "R": ["████ ", "█   █", "████ ", "█ █  ", "█  █ "],
}

# the machine, 19 wide x 5 tall: cab + exhaust stack over a sealed track loop
# (╒│╙ frame, ◙ road wheels on ▒ shoes), push arms to the blade plate at cols
# 16..18. It parks flush right; nothing here names it in the subtitle.
_DOZER = [
    "  ┌─┐▄            ",   # cab roof, exhaust stack
    " ┌┤█├▐███▓▓▓▄    ▄▄",   # cab, body, hood slope, blade top
    "╒▒▒▒▒▒▒▒▒▒▒▒▒╦══╗▐█",   # top track run, push arms, blade plate
    "│◙▒◙▒◙▒▒▒▒▒▒▒╩══╣▐█",   # road wheels riding the bottom run
    "╙▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ ▀▀",   # closed bottom run, blade edge
]
PROW = 19                              # first column past the blade plate

DIM, AMBER, CYAN, GREEN = "2", "33", "36", "32"


def _wordmark() -> dict[tuple[int, int], str]:
    cells: dict[tuple[int, int], str] = {}
    for row in range(LOGO_ROWS):
        for i, ch in enumerate("D8R"):
            for col in range(5):
                g = _G[ch][row][col]
                if g != " ":
                    cells[(row, i * 7 + col)] = g
    return cells


LOGO = _wordmark()


class _Grid:
    """Cell grid; each cell is ' ' or (char, color)."""

    def __init__(self) -> None:
        self.c: list[list] = [[" "] * W for _ in range(ROWS)]

    def put(self, r: int, col: int, ch: str, color: str) -> None:
        if ch != " " and 0 <= r < ROWS and 0 <= col < W:
            self.c[r][col] = (ch, color)

    def art(self, r: int, col: int, art: list[str], color: str) -> None:
        for dy, row in enumerate(art):
            for dx, g in enumerate(row):
                self.put(r + dy, col + dx, g, color)

    def row(self, r: int, color: bool) -> str:
        if not color:
            return "".join(c if isinstance(c, str) else c[0] for c in self.c[r])
        out, cur = [], ""
        for c in self.c[r]:
            ch, code = c if isinstance(c, tuple) else (c, "")
            if code != cur:
                out.append(f"\x1b[{code}m" if code else "\x1b[0m")
                cur = code
            out.append(ch)
        out.append("\x1b[0m")
        return "".join(out)


def blade_x(f: int) -> int:
    """Dozer position: appears at ENTRY, drives SPEED cols/frame, parks flush."""
    return min(PARK_X, SPEED * (f - ENTRY))


def _tape_char(row: int, x: int, f: int, rightward: bool) -> str:
    """Wall character: the row's tape, scrolling left or being pushed right."""
    base = TAPES[ROW_TAPE[row]]
    off = -f * PUSH if rightward else f * SCROLL_IN
    return base[(x + off) % len(base)]


def build(f: int) -> _Grid:
    """The single source of truth for frame `f`."""
    g = _Grid()

    # CHAOS + SWEEP: the wall scrolls left; once the blade arrives it reverses
    # everything ahead of the plate, shoving it right, off-screen. Columns
    # behind the prow are left empty — that is the cleared lane.
    if f < PARK_AT + 1:
        rightward = f >= ENTRY
        left = blade_x(f) + PROW if rightward else 0
        for row in WALL_ROWS:
            for x in range(left, W):
                g.c[row][x] = (_tape_char(row, x, f, rightward), DIM)

    # the machine: from ENTRY it rides in, clamped at the parked position
    if f >= ENTRY:
        g.art(ART_Y, blade_x(f), _DOZER, CYAN)

    # WIPE: the wordmark appears in clean solid blocks, left to right
    if f >= WIPE_AT:
        front = (f - WIPE_AT) * 2
        for (row, col), ch in LOGO.items():
            if col <= front:
                g.put(LOGO_Y + row, LOGO_X + col, ch, AMBER)

    # TITLE: typed subtitle with a block cursor that disappears when typed out
    if f >= TYPE_AT:
        typed = min(len(TITLE), f - TYPE_AT)
        for x, ch in enumerate(TITLE[:typed]):
            g.put(TITLE_ROW, TITLE_X + x, ch, GREEN)
        if typed < len(TITLE):
            g.put(TITLE_ROW, TITLE_X + typed, "▊", GREEN)

    return g


def frame_lines(f: int) -> list[str]:
    """Frame `f` as plain text (no ANSI) — what a test asserts against."""
    g = build(f)
    return [g.row(r, False) for r in range(ROWS)]


def _enable_vt() -> None:
    """Ask the Windows 10+ console to process ANSI escapes."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetConsoleHandle(-11), 7)
    except Exception:
        pass


def play(color: bool = True, out=None, *, sleep=time.sleep) -> None:
    """Draw the whole splash once, deadline-paced to exactly 6.000 s.

    `out` defaults to stdout; `sleep` is injectable so a test can drive all
    120 frames without waiting on the clock.
    """
    out = out if out is not None else sys.stdout
    _enable_vt()
    perf = time.perf_counter
    t0 = perf()
    if color:
        out.write("\x1b[?25l")  # hide cursor for the run
    try:
        for f in range(TOTAL_FRAMES):
            g = build(f)
            body = "\n".join(g.row(r, color) for r in range(ROWS))
            out.write("\x1b[H" + ("\x1b[2J" if f == 0 else "") + body)
            out.flush()
            sleep(max(0.0, t0 + (f + 1) * FRAME_MS / 1000 - perf()))
    finally:
        if color:
            out.write("\x1b[0m\x1b[?25h")  # restyle + cursor back
        out.write("\n")
        out.flush()


def should_play(stream=None) -> bool:
    """Play only on an interactive terminal — never a pipe, a file, or the pilot."""
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


__all__ = ["play", "should_play", "build", "frame_lines", "TOTAL_FRAMES", "TITLE"]
