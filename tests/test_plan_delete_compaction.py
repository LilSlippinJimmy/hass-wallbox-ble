"""Standalone, HA-free unit test for coordinator.plan_delete_compaction.

Run directly: `python tests/test_plan_delete_compaction.py` (no pytest, no HA).

coordinator.py imports Home Assistant at module top, which isn't installed in
the sandbox, so we can't just `import coordinator`. Instead we parse the module
with `ast`, pull out ONLY the pure functions the planner needs
(`_clean4` + `plan_delete_compaction`), and exec them in an isolated namespace.
This exercises the REAL shipped source — if the function changes in
coordinator.py, this test follows it.

What this proves (the delete-and-compact slot renumbering, which drives the
paced w_sch rewrite burst in async_delete_schedule):

  * The target sid is removed.
  * Surviving slots are renumbered contiguously into positions 0,1,2,... with
    their start/stop/days preserved exactly (no re-conversion).
  * Every trailing position that WAS occupied but is no longer needed gets a
    clear write (0000/0000/days 0), so no stale slot lingers past the tail.
  * The returned order IS the issue order (what the pacing loop walks).
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COORDINATOR = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "coordinator.py"
)


def _load_plan_fn():
    with open(COORDINATOR, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=COORDINATOR)
    wanted = {"_clean4", "plan_delete_compaction"}
    funcs = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in wanted
    ]
    found = {n.name for n in funcs}
    missing = wanted - found
    if missing:
        raise AssertionError(f"missing in coordinator.py: {sorted(missing)}")
    module = ast.Module(body=funcs, type_ignores=[])
    ns: dict = {}
    exec(compile(module, COORDINATOR, "exec"), ns)  # noqa: S102 - trusted local source
    return ns["plan_delete_compaction"]


def _slot(sid, start, stop, days):
    return {"sid": sid, "start": start, "stop": stop, "days": days}


def main() -> int:
    plan = _load_plan_fn()
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # 1. Delete the only slot -> one clear write at position 0.
    before = [_slot(0, "0200", "0300", 127)]
    check("delete lone slot", plan(before, 0), [(0, "0000", "0000", 0)])

    # 2. Delete slot 0 of two -> survivor (was sid 1) moves to pos 0, and the
    #    now-unused trailing position 1 is cleared.
    before = [_slot(0, "0200", "0300", 127), _slot(1, "0600", "0700", 65)]
    check(
        "delete first of two",
        plan(before, 0),
        [(0, "0600", "0700", 65), (1, "0000", "0000", 0)],
    )

    # 3. Delete the LAST slot of two -> survivor stays at pos 0, trailing pos 1
    #    cleared. Survivor's fields preserved exactly.
    before = [_slot(0, "0200", "0300", 127), _slot(1, "0600", "0700", 65)]
    check(
        "delete last of two",
        plan(before, 1),
        [(0, "0200", "0300", 127), (1, "0000", "0000", 0)],
    )

    # 4. Delete the middle of three -> the two survivors compact to 0,1 in
    #    order; trailing pos 2 cleared.
    before = [
        _slot(0, "0100", "0130", 1),
        _slot(1, "0200", "0230", 2),
        _slot(2, "0300", "0330", 4),
    ]
    check(
        "delete middle of three",
        plan(before, 1),
        [
            (0, "0100", "0130", 1),
            (1, "0300", "0330", 4),
            (2, "0000", "0000", 0),
        ],
    )

    # 5. sid not present -> no survivors dropped; everything renumbered as-is
    #    (positions already contiguous so no clears appended).
    before = [_slot(0, "0200", "0300", 127), _slot(1, "0600", "0700", 65)]
    check(
        "delete absent sid keeps all",
        plan(before, 5),
        [(0, "0200", "0300", 127), (1, "0600", "0700", 65)],
    )

    # 6. Days bitmask is masked to 7 bits and preserved (not re-converted).
    before = [_slot(0, "0800", "0900", 200)]  # 200 & 0x7F == 72
    check("days masked to 7 bits", plan(before, 9), [(0, "0800", "0900", 72)])

    # 7. Start/stop normalised to 4-digit HHMM via _clean4 (colon stripped).
    before = [_slot(0, "2:05", "3:05", 127)]
    check("times normalised", plan(before, 9), [(0, "0205", "0305", 127)])

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: all plan_delete_compaction cases passed")
    return 0


def test_plan_delete_compaction():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
