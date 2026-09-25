"""Table-valued functions: the language rule, the call expansion, the seam.

A FN here is a named document with a positional signature; calling it inlines
each argument where the body names its `@param`, so the engine sees literals and
ibis never learns a function exists. These tests run the whole path — save, the
call's expansion, and execution against the real demo registry — through the
headless `Session`, plus the palette rows that make a function callable from the
document. No terminal and no network: the `Session` seams its own schema.
"""

from __future__ import annotations

from anyql.query import parse_body, parse_query
from anyql.tui.palette import view_for
from anyql.tui.session import Session

HOT_BODY = "\\from events\n\\where amount > @min_amount\n\\select user_id, amount"


def session_with_functions() -> Session:
    """A session holding one threshold function and one no-argument function."""
    ns = Session()
    ns.save_fn("hot", "min_amount", HOT_BODY, "events above a threshold")
    ns.save_fn("all_events", "", "\\from events\n\\select user_id", "every event")
    return ns


# -- the language rule ------------------------------------------------------


def test_parse_body_rejects_an_undeclared_parameter():
    """A `@name` the signature does not declare is named before the body can run."""
    assert parse_body(HOT_BODY, ["min_amount"]) == []
    messages = parse_body("\\from events\n\\where amount > @nope\n\\select *", ["min_amount"])
    assert messages == ['unknown parameter "@nope" — declare it or remove it']


def test_save_validates_the_signature_and_body():
    """Saving refuses a bad name, a repeated parameter, and a body that is not a query."""
    ns = Session()
    for name, params, body, why in (
        ("bad name", "", HOT_BODY, "identifier"),
        ("dup", "a, a", "\\from events\n\\select *", "duplicate"),
        ("empty", "", "\\select 1", "\\from"),
        ("mystery", "x", "\\from events\n\\where amount > @missing\n\\select *", "@missing"),
    ):
        try:
            ns.save_fn(name, params, body, "")
        except ValueError as exc:
            assert why in str(exc), (name, str(exc))
        else:  # pragma: no cover - a refusal is required
            raise AssertionError(f"{name!r} should not save")


# -- the store, in memory ---------------------------------------------------


def test_the_library_is_an_ordered_in_memory_registry():
    """Names keep definition order, replacing edits in place, and delete removes."""
    ns = session_with_functions()
    assert list(ns.fns) == ["hot", "all_events"]

    ns.save_fn("hot", "min_amount, region", HOT_BODY + "\n\\where region = @region", "now with region")
    assert list(ns.fns) == ["hot", "all_events"]  # replaced keeps its slot
    assert tuple(ns.fns["hot"].params) == ("min_amount", "region")
    assert ns.fns["hot"].doc == "now with region"

    ns.delete_fn("hot")
    assert list(ns.fns) == ["all_events"]
    ns.delete_fn("not-there")  # deleting what is absent is a no-op, not a crash
    assert list(ns.fns) == ["all_events"]


def test_saved_functions_resolve_until_deleted():
    ns = Session()
    ns.save_fn("hot", "min_amount", HOT_BODY, "")
    document = "\\from hot(0)\n\\select *"
    assert parse_query(document, schema=ns.schema).errors == []
    ns.delete_fn("hot")
    assert parse_query(document, schema=ns.schema).errors


# -- the call, executed -----------------------------------------------------


def test_a_call_filters_by_its_argument():
    """`\from hot(n)` runs the body with the threshold inlined; larger n, fewer rows."""
    ns = session_with_functions()
    loose = ns.run("\\from hot(0)\n\\select *")
    assert loose.error == ""
    assert loose.total == 100  # every event is above zero
    assert loose.columns == ["user_id", "amount"]

    tight = ns.run("\\from hot(5000)\n\\select *")
    assert tight.total == 0  # nothing clears that threshold


def test_a_function_is_composable_like_a_table():
    """A call aliases, seeds a CTE, feeds a set operation, and nests in a subquery.

    These are the shapes the grammar gives any derived table; a plain (uncorrelated)
    `\join` of a derived table is refused for a call exactly as it is for an inline
    `( \from … )`, so a FN composes wherever a sub-query does — no less.
    """
    ns = session_with_functions()

    aliased = ns.run("\\from hot(0) as h\n\\select h.user_id, h.amount")
    assert aliased.error == "" and aliased.total == 100  # its columns qualify under the alias

    cte = ns.run("\\with c\n  \\from hot(0)\n\\from c\n\\select amount")
    assert cte.error == "" and cte.total == 100

    setop = ns.run("\\from hot(0)\n\\union all hot(0)")
    assert setop.error == "" and setop.total == 200

    nested = ns.run("\\from events\n\\where user_id in (\\from hot(0) \\select user_id)\n\\select user_id")
    assert nested.error == "" and nested.total == 100


def test_a_no_argument_function_still_calls():
    """A function with an empty signature is a plain saved query."""
    ns = session_with_functions()
    outcome = ns.run("\\from all_events()\n\\select *")
    assert outcome.error == "" and outcome.total == 100


def test_an_unknown_function_or_arity_is_named():
    """A call to a name the library lacks, or with the wrong arity, refuses by name."""
    ns = session_with_functions()
    missing = ns.run("\\from nope(1)\n\\select *")
    assert "nope" in missing.error and "hot" in missing.error
    arity = ns.run("\\from hot()\n\\select *")
    assert "hot" in arity.error


def test_a_parameter_never_leaks_as_a_column():
    """The only place `@param` is legal is the body; a document cannot reference it."""
    ns = session_with_functions()
    stray = ns.run("\\from events\n\\where amount > @min_amount\n\\select *")
    assert stray.error != ""  # `@min_amount` is not a column of events


# -- the preview, out of history --------------------------------------------


def test_the_preview_calls_without_touching_history():
    """`fn_preview` runs a call like the document path but records no history row."""
    ns = session_with_functions()
    before = len(ns.history)
    outcome = ns.fn_preview("hot", "0")
    assert outcome.error == "" and outcome.total == 100
    assert len(ns.history) == before

    bad = ns.fn_preview("hot", "not-a-number")
    assert bad.error != ""  # a bad argument is an outcome, never an exception
    assert len(ns.history) == before


# -- the palette, so a function is callable ---------------------------------


def test_bare_fn_opens_the_library_and_offers_a_new_function():
    ns = session_with_functions()
    view = view_for(ns, "\\fn", "\\fn", len("\\fn"))
    assert [entry.action for entry in view.entries] == ["fn-new:", "fn-open:hot", "fn-open:all_events"]


def test_fn_with_a_name_opens_it_or_offers_to_create_it():
    ns = session_with_functions()
    matched = view_for(ns, "\\fn ho", "\\fn ho", len("\\fn ho"))
    assert [entry.action for entry in matched.entries] == ["fn-new:ho", "fn-open:hot"]

    fresh = view_for(ns, "\\fn zzz", "\\fn zzz", len("\\fn zzz"))
    assert [entry.action for entry in fresh.entries] == ["fn-new:zzz"]


def test_a_dataset_clause_completes_a_call_with_the_caret_inside():
    """`\from ho` offers `hot()`, and accepting leaves the caret between the parens."""
    ns = session_with_functions()
    view = view_for(ns, "\\from ho", "\\from ho", len("\\from ho"))
    call = next(entry for entry in view.entries if entry.label == "hot()")
    assert call.insert == "hot()"
    assert call.cursor_back == 1


def test_fn_completion_respects_the_dataset_guard():
    """A clause that already names a table offers nothing — no second source appended."""
    ns = session_with_functions()
    line = "\\from events "
    assert view_for(ns, line, line, len(line)) is None


def test_fn_projects_literal_arguments_without_converting_numeric_text():
    ns = Session()
    ns.save_fn("tagged", "tag", "\\from events\n\\select @tag as tag, 'x' as marker\n\\limit 2", "")
    for argument, value in [("'1'", "1"), ("1", 1), ("'x'", "x"), ('"a,b"', "a,b"), ("null", None)]:
        preview = ns.fn_preview("tagged", argument)
        assert preview.error == ""
        assert preview.columns == ["tag", "marker"]
        assert preview.rows == [[value, "x"], [value, "x"]]
    assert ns.history == []
