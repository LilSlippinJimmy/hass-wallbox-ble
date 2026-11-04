"""Standalone, HA-free unit test for __init__.resolve_coordinator.

Run directly: `python tests/test_coordinator_for_device.py` (no pytest, no HA).

__init__.py imports Home Assistant at module top, so we can't import it. We
`ast`-extract ONLY the pure `resolve_coordinator` helper and exec it in an
isolated namespace. The helper raises `HomeAssistantError`, which isn't
installed here, so we inject a stand-in exception of that name into the exec
namespace before running it — the helper only ever references the name.

This exercises the real resolution rules the schedule services rely on:
  - a known device -> its coordinator,
  - unknown device -> error,
  - foreign (non-wallbox) device -> error,
  - device_id omitted + exactly one charger -> that charger,
  - device_id omitted + multiple chargers -> error,
  - device_id omitted + none -> error.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INIT = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "__init__.py"
)


class _StubHAError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError."""


def _load_resolver():
    with open(INIT, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=INIT)
    fn = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "resolve_coordinator"),
        None,
    )
    if fn is None:
        raise AssertionError("resolve_coordinator not found in __init__.py")
    module = ast.Module(body=[fn], type_ignores=[])
    ns: dict = {"HomeAssistantError": _StubHAError}
    exec(compile(module, INIT, "exec"), ns)  # noqa: S102 - trusted local source
    return ns["resolve_coordinator"]


def main() -> int:
    resolve = _load_resolver()
    failures = []

    def check(name, cond):
        if not cond:
            failures.append(name)

    def expect_error(name, fn):
        try:
            fn()
        except _StubHAError:
            return
        except Exception as err:  # noqa: BLE001
            failures.append(f"{name}: raised {type(err).__name__}, want HomeAssistantError")
            return
        failures.append(f"{name}: did not raise")

    coord_a = object()
    coord_b = object()
    coords = {"entry_a": coord_a, "entry_b": coord_b}
    domain_ids = {"entry_a", "entry_b"}

    # 1. Known device -> right coordinator (device attached to entry_b).
    got = resolve("dev_b", {"entry_b"}, domain_ids, coords)
    check("known device picks its coordinator", got is coord_b)

    # 2. Unknown device (registry returned None -> device_entry_ids is None).
    expect_error(
        "unknown device raises",
        lambda: resolve("dev_x", None, domain_ids, coords),
    )

    # 3. Foreign device: exists in registry but attached to a different
    #    integration's entries (no overlap with our domain entry ids).
    expect_error(
        "foreign device raises",
        lambda: resolve("dev_other", {"some_other_entry"}, domain_ids, coords),
    )

    # 4. device_id omitted + exactly one charger -> that charger.
    one = {"entry_a": coord_a}
    got = resolve(None, None, {"entry_a"}, one)
    check("omit + single returns sole coordinator", got is coord_a)

    # 5. device_id omitted + multiple chargers -> error (ambiguous).
    expect_error(
        "omit + multiple raises",
        lambda: resolve(None, None, domain_ids, coords),
    )

    # 6. device_id omitted + none configured -> error.
    expect_error(
        "omit + none raises",
        lambda: resolve(None, None, set(), {}),
    )

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("OK: all resolve_coordinator cases passed")
    return 0


def test_resolve_coordinator():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
