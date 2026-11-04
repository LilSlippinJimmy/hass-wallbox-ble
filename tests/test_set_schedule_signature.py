"""Standalone, HA-free unit test proving max_current/enable were removed from
the schedule write path.

Run directly: `python tests/test_set_schedule_signature.py` (no pytest, no HA).

coordinator.py / __init__.py import Home Assistant at module top, which isn't
installed in the sandbox, so we can't import them. We `ast`-parse the modules
and assert on the SHAPE of the real shipped source:

  (a) coordinator.async_set_schedule no longer declares `max_current` or
      `enable` arguments (the dead/ignored params removed in the HACS fixes).
  (b) __init__._SET_SCHEDULE_SCHEMA's source text no longer mentions
      `max_current` (the writable service field was removed — w_sch can't
      carry it).
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COORDINATOR = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "coordinator.py"
)
INIT = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "__init__.py"
)


def _arg_names(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    a = fn.args
    names = set()
    for group in (a.posonlyargs, a.args, a.kwonlyargs):
        names.update(arg.arg for arg in group)
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def _schema_source(tree, src, name):
    """Return the source text of the assignment `name = ...`."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if name in targets:
                return ast.get_source_segment(src, node)
    return None


def main() -> int:
    failures = []

    with open(COORDINATOR, "r", encoding="utf-8") as fh:
        coord_tree = ast.parse(fh.read(), filename=COORDINATOR)

    fn = _find_func(coord_tree, "async_set_schedule")
    if fn is None:
        failures.append("async_set_schedule not found in coordinator.py")
    else:
        args = _arg_names(fn)
        for banned in ("max_current", "enable"):
            if banned in args:
                failures.append(
                    f"async_set_schedule still declares {banned!r} (args={sorted(args)})"
                )

    with open(INIT, "r", encoding="utf-8") as fh:
        init_src = fh.read()
    init_tree = ast.parse(init_src, filename=INIT)
    schema_src = _schema_source(init_tree, init_src, "_SET_SCHEDULE_SCHEMA")
    if schema_src is None:
        failures.append("_SET_SCHEDULE_SCHEMA not found in __init__.py")
    elif "max_current" in schema_src:
        failures.append("_SET_SCHEDULE_SCHEMA still references max_current")

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: schedule write path no longer accepts max_current/enable")
    return 0


def test_async_set_schedule_has_no_max_current_or_enable():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
