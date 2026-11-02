"""Standalone, HA-free unit test for client.classify_mode_read_error.

Run directly: `python tests/test_classify_mode_read_error.py` (no pytest, no HA).

client.py imports bleak_retry_connector / homeassistant at module top, which are
not installed in the sandbox, so we cannot `import client`. Instead we parse the
module with `ast`, pull out ONLY the pure `classify_mode_read_error` function
and the two marker tuples it depends on, and exec those in an isolated
namespace. This exercises the REAL shipped source — if the helper changes in
client.py, this test follows it.

The helper decides what a failed mode-characteristic read means, driving the
new connect state machine (Fix B, findings §2/§6):
  - 'already_authorized' (GATT 8 etc.): bond exists -> continue, NO re-pair.
  - 'encryption_needed' (GATT 5/15/143, "not paired"): bond+retry when NOT
    already bonded.
  - 'fatal': recycle the link.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(
    HERE, "..", "custom_components", "wallbox_ble", "client.py"
)

_WANTED = {
    "_ENCRYPTION_NEEDED_MARKERS",
    "_ALREADY_AUTHORIZED_MARKERS",
    "classify_mode_read_error",
}


def _load_helper():
    with open(CLIENT, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=CLIENT)
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            picked.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & _WANTED:
                picked.append(node)
    if not any(isinstance(n, ast.FunctionDef) for n in picked):
        raise AssertionError("classify_mode_read_error not found in client.py")
    module = ast.Module(body=picked, type_ignores=[])
    ns: dict = {}
    exec(compile(module, CLIENT, "exec"), ns)  # noqa: S102 - trusted local source
    return ns["classify_mode_read_error"]


# (description, input text, expected verdict)
CASES = [
    # --- already_authorized: GATT 8 is NON-FATAL when bonded; continue -------
    ("GATT code 8 in parens", "GATT error: Insufficient Authorization (8)",
     "already_authorized"),
    ("bare 'code 8'", "write failed with code 8", "already_authorized"),
    ("insufficient authorization text",
     "org.bluez.Error: Insufficient Authorization", "already_authorized"),
    ("already exists (bond present)",
     "org.bluez.Error.AlreadyExists: Already Exists", "already_authorized"),
    ("AuthenticationFailed", "org.bluez.Error.AuthenticationFailed: auth",
     "already_authorized"),
    ("authorization keyword only", "Authorization error", "already_authorized"),

    # --- encryption_needed: triggers fallback bond when NOT bonded -----------
    ("GATT code 5 authentication", "Insufficient Authentication (5)",
     "encryption_needed"),
    ("GATT code 15 encryption", "Insufficient Encryption (15)",
     "encryption_needed"),
    ("GATT code 143", "some gatt failure (143)", "encryption_needed"),
    ("not paired", "Device is Not Paired", "encryption_needed"),
    ("bare encryption word", "encryption required", "encryption_needed"),

    # --- fatal: recycle the link ---------------------------------------------
    ("disconnected", "Device disconnected during read", "fatal"),
    ("generic bleak error", "BleakError: characteristic not found", "fatal"),
    ("empty string", "", "fatal"),

    # --- precedence: authorization beats generic encryption substring --------
    ("authorization not swallowed by encryption match",
     "Insufficient Authorization during encryption setup", "already_authorized"),
]


def _decide(verdict, bonded):
    """Mirror of _bring_up_link's branch selection, so the test documents the
    exact state-machine decisions Fix B relies on."""
    if verdict == "already_authorized":
        return "continue"
    if verdict == "encryption_needed" and bonded is not True:
        return "fallback_bond"
    if verdict == "encryption_needed" and bonded is True:
        return "continue"
    return "recycle"


def main() -> int:
    classify = _load_helper()
    failures = []

    # Exercise BOTH the str path and the Exception path for every case.
    for desc, text, expected in CASES:
        for kind, value in (("str", text), ("exc", RuntimeError(text))):
            got = classify(value)
            if got != expected:
                failures.append(
                    f"[{kind}] {desc}: input={text!r} want={expected!r} got={got!r}"
                )

    # State-machine scenario checks (the decisions that fix the drop storm).
    scenarios = [
        ("auth-8 + bonded => continue (no re-pair)",
         classify("Insufficient Authorization (8)"), True, "continue"),
        ("auth-8 + unknown => continue (no re-pair)",
         classify("Insufficient Authorization (8)"), None, "continue"),
        ("encryption + not bonded => fallback bond",
         classify("Insufficient Encryption (15)"), False, "fallback_bond"),
        ("encryption + unknown => fallback bond",
         classify("not paired"), None, "fallback_bond"),
        ("encryption + bonded => continue (no re-pair)",
         classify("Insufficient Encryption (15)"), True, "continue"),
        ("fatal => recycle", classify("Device disconnected"), None, "recycle"),
    ]
    for desc, verdict, bonded, expected_action in scenarios:
        got = _decide(verdict, bonded)
        if got != expected_action:
            failures.append(
                f"scenario {desc}: verdict={verdict!r} bonded={bonded!r} "
                f"want={expected_action!r} got={got!r}"
            )

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    total = len(CASES) * 2 + len(scenarios)
    print(f"OK: all {total} classify_mode_read_error cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
