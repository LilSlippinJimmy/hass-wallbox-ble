"""Standalone, HA-free unit tests for the native clr_sch delete helpers.

Run directly: `python tests/test_clr_sch_delete.py` (no pytest, no HA).

Both modules import things that aren't installed in the sandbox (coordinator.py
imports Home Assistant at module top), so we can't just import them. Instead we
parse each module with `ast`, pull out ONLY the pure functions under test, and
exec them in an isolated namespace (providing `json` for the bapi helper). This
exercises the REAL shipped source — if a function changes, this test follows it.

What this proves:

  * bapi.build_clr_sch_par(sid) builds EXACTLY the object the official app sends
    to delete one slot: {"sid":[<id>]} — key "sid", value a one-element JSON
    ARRAY of the integer slot id (not a scalar; the array form is the whole
    point of this change).
  * coordinator.clr_sch_succeeded(after, sid) decides success from the r_schs
    read-back — NOT from the (possibly absent) clr_sch ack: the delete took when
    the target slot is gone or left inert (days 0); a None/empty read-back is
    treated as "not confirmed" so the caller falls back to the rewrite.

The BLE round-trip itself (clr_sch write + r_schs read-back under one lock hold)
is NOT unit-testable without hardware; only these pure decision helpers are.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "custom_components", "wallbox_ble")
BAPI = os.path.join(ROOT, "bapi.py")
COORDINATOR = os.path.join(ROOT, "coordinator.py")


def _load_fn(path, name, extra_globals=None):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    fn = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == name),
        None,
    )
    if fn is None:
        raise AssertionError(f"{name} not found in {os.path.basename(path)}")
    module = ast.Module(body=[fn], type_ignores=[])
    ns: dict = dict(extra_globals or {})
    exec(compile(module, path, "exec"), ns)  # noqa: S102 - trusted local source
    return ns[name]


def main() -> int:
    import json

    build_par = _load_fn(BAPI, "build_clr_sch_par", {"json": json})
    succeeded = _load_fn(COORDINATOR, "clr_sch_succeeded")
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # --- build_clr_sch_par: the exact wire par fragment ---------------------
    # 1. One slot -> sid is a ONE-ELEMENT ARRAY (the app's shape), compact JSON.
    check("par for sid 3", build_par(3), '{"sid":[3]}')
    check("par for sid 0", build_par(0), '{"sid":[0]}')
    # 2. A string sid is coerced to int (no quotes, still an array).
    check("par coerces str sid", build_par("2"), '{"sid":[2]}')
    # 3. It parses back to the typed shape: {"sid": [int]}.
    check("par round-trips", json.loads(build_par(1)), {"sid": [1]})

    # --- clr_sch_succeeded: read-back-driven success decision ---------------
    # 4. Target slot absent from the read-back -> deleted.
    after = [{"sid": 0, "days": 127}, {"sid": 2, "days": 65}]
    check("absent sid is gone", succeeded(after, 1), True)
    # 5. Target slot still present and ACTIVE (days != 0) -> not deleted.
    after = [{"sid": 0, "days": 127}, {"sid": 1, "days": 65}]
    check("active sid still there", succeeded(after, 1), False)
    # 6. Target slot present but INERT (days 0) counts as gone.
    after = [{"sid": 0, "days": 127}, {"sid": 1, "days": 0}]
    check("inert sid counts as gone", succeeded(after, 1), True)
    # 7. days masked to 7 bits: only the low 7 bits decide active/inert.
    after = [{"sid": 1, "days": 128}]  # 128 & 0x7F == 0 -> inert
    check("high bit only is inert", succeeded(after, 1), True)
    # 8. Empty read-back -> NOT confirmed (fall back), even though sid is absent.
    check("empty read-back not confirmed", succeeded([], 1), False)
    check("None read-back not confirmed", succeeded(None, 1), False)
    # 9. Slot present but missing the 'days' key -> days defaults to 0 (inert),
    #    so it counts as gone (mirrors the fallback's own days-default-0 logic).
    after = [{"sid": 1}]
    check("missing days defaults inert", succeeded(after, 1), True)

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: all clr_sch delete helper cases passed")
    return 0


def test_clr_sch_delete():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
