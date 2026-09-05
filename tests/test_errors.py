"""describe_exception: unwrap ExceptionGroup / cause chains for the audit log."""
from agentic.errors import describe_exception


def _raise_group():
    """Reproduce the masked scanner failure: a TaskGroup-style ExceptionGroup wrapping a real error."""
    try:
        raise ValueError("the real root cause")
    except ValueError as inner:
        return ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])


def test_exception_group_unwrapped():
    eg = _raise_group()
    d = describe_exception(eg)
    assert d["type"] == "ExceptionGroup"
    # The masked top-level message is kept for continuity...
    assert "TaskGroup" in d["error"]
    # ...but the real cause is now surfaced.
    assert d["root_causes"] == ["ValueError: the real root cause"]
    assert "ValueError" in d["traceback"]


def test_nested_group_flattened():
    inner1 = ValueError("first")
    inner2 = KeyError("second")
    eg = ExceptionGroup("outer", [ExceptionGroup("mid", [inner1]), inner2])
    d = describe_exception(eg)
    assert "ValueError: first" in d["root_causes"]
    assert any("KeyError" in rc for rc in d["root_causes"])


def test_cause_chain_followed():
    try:
        try:
            raise ConnectionError("broker socket dropped")
        except ConnectionError as base:
            raise RuntimeError("scan failed") from base
    except RuntimeError as exc:
        d = describe_exception(exc)
    # str() would only show "scan failed"; the root cause is the ConnectionError.
    assert d["root_causes"] == ["ConnectionError: broker socket dropped"]


def test_plain_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        d = describe_exception(exc)
    assert d["type"] == "RuntimeError"
    assert d["root_causes"] == ["RuntimeError: boom"]
    assert "boom" in d["traceback"]
