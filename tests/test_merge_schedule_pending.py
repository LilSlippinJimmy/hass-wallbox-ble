"""Standalone, HA-free unit test for coordinator.merge_schedule_pending.

Run directly: `python tests/test_merge_schedule_pending.py` (no pytest, no HA).

coordinator.py imports Home Assistant at module top, which isn't installed in
the sandbox, so we can't just `import coordinator`. Instead we parse the module
with `ast`, pull out ONLY the `merge_schedule_pending` function, and exec that
single pure function in an isolated namespace. This exercises the REAL shipped
source — if the function changes in coordinator.py, this test follows it.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COORDINATOR = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "coordinator.py"
)


def _load_merge_fn():
    with open(COORDINATOR, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=COORDINATOR)
    fn = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "merge_schedule_pending"),
        None,
    )
    if fn is None:
        raise AssertionError("merge_schedule_pending not found in coordinator.py")
    module = ast.Module(body=[fn], type_ignores=[])
    ns: dict = {}
    exec(compile(module, COORDINATOR, "exec"), ns)  # noqa: S102 - trusted local source
    return ns["merge_schedule_pending"]


def main() -> int:
    merge = _load_merge_fn()
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # 1. Start-only edit then stop-only edit merges to ONE {start, stop} payload.
    buf = merge(None, {"start": "0200", "stop": None})
    check("start-only stages start", buf, {"start": "0200"})
    buf = merge(buf, {"start": None, "stop": "0230"})
    check("stop edit merges in", buf, {"start": "0200", "stop": "0230"})

    # 2. A None field never clobbers a previously-staged value.
    buf2 = merge({"start": "0100", "stop": "0130"}, {"start": None, "stop": None})
    check("all-None update keeps buffer", buf2, {"start": "0100", "stop": "0130"})

    # 3. A non-None field overrides the staged one (re-editing the same field).
    buf3 = merge({"start": "0200", "stop": "0230"}, {"start": "0300"})
    check("re-edit overrides start", buf3, {"start": "0300", "stop": "0230"})

    # 4. days and max_current merge the same way.
    buf4 = merge({"start": "0200"}, {"days": 65, "max_current": 32})
    check("days+mcr merge", buf4, {"start": "0200", "days": 65, "max_current": 32})
    buf4 = merge(buf4, {"max_current": None})
    check("None max_current keeps prior", buf4,
          {"start": "0200", "days": 65, "max_current": 32})

    # 5. Inputs are not mutated (returns a fresh dict).
    original = {"start": "0200"}
    merge(original, {"stop": "0230"})
    check("input not mutated", original, {"start": "0200"})

    # 6. Two independent merges (modelling different sids) don't share state.
    a = merge(None, {"start": "0200", "stop": "0230"})
    b = merge(None, {"start": "0600", "stop": "0700"})
    check("sid A independent", a, {"start": "0200", "stop": "0230"})
    check("sid B independent", b, {"start": "0600", "stop": "0700"})

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: all merge_schedule_pending cases passed")
    return 0


def test_merge_schedule_pending():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
