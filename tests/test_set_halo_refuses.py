"""Standalone, HA-free unit test for coordinator.halo_config_known.

Run directly: `python tests/test_set_halo_refuses.py` (no pytest, no HA).

coordinator.py imports Home Assistant at module top, so we can't import it.
We `ast`-extract ONLY the pure `halo_config_known` helper and exec it in an
isolated namespace, exercising the REAL shipped source.

`async_set_halo` refuses a partial write (raises HomeAssistantError) when this
helper returns False — i.e. when neither live data nor cached static holds a
halo config. Refusing is what stops a brightness-only change from inventing
hardcoded defaults that silently turn the halo on / reset its timer.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COORDINATOR = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "coordinator.py"
)


def _load_helper():
    with open(COORDINATOR, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=COORDINATOR)
    fn = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "halo_config_known"),
        None,
    )
    if fn is None:
        raise AssertionError("halo_config_known not found in coordinator.py")
    module = ast.Module(body=[fn], type_ignores=[])
    ns: dict = {}
    exec(compile(module, COORDINATOR, "exec"), ns)  # noqa: S102 - trusted local source
    return ns["halo_config_known"]


def main() -> int:
    known = _load_helper()
    failures = []

    def check(name, got, want):
        if bool(got) != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    halo = {"mode": 1, "time_s": 10, "bright": 100}

    # Unknown: both empty / None -> must refuse (False).
    check("both None -> unknown", known(None, None), False)
    check("both empty -> unknown", known({}, {}), False)
    check("data without halo -> unknown", known({"status": {}}, {}), False)
    check("empty halo dict -> unknown", known({"halo": {}}, {"halo": {}}), False)

    # Known: a halo config in live data OR in cached static -> allowed (True).
    check("halo in live data -> known", known({"halo": halo}, {}), True)
    check("halo in static -> known", known({}, {"halo": halo}), True)
    check("halo in both -> known", known({"halo": halo}, {"halo": halo}), True)

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: all halo_config_known cases passed")
    return 0


def test_halo_config_known_guard():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
