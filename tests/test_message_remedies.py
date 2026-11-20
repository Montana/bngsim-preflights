"""A message that hands the user code to run is listed, to be run as written (#1723)."""

from __future__ import annotations

from _helpers import pc, result

BASE = {"python/bngsim/_simulator.py": "def scan():\n    pass\n"}


def test_a_message_with_a_hook_to_run_warns(make_branch):
    # #1723's message, as first written: the hook set the wrong value for a derived initial condition.
    b = make_branch(BASE, {"python/bngsim/_simulator.py": (
        "def scan():\n"
        "    warnings.warn(\n"
        "        \"To dose them, set them in on_point: \"\n"
        "        \"on_point=lambda model, value: model.set_concentration(name, value).\"\n"
        "    )\n")})
    pc.check_message_remedies(b)
    status, detail = result("message remedies run as written")
    assert status == "WARN" and "model.set_concentration(name, value)" in detail


def test_a_call_in_code_is_not_a_message(make_branch):
    b = make_branch(BASE, {"python/bngsim/_simulator.py": (
        "def scan(model):\n"
        "    model.set_concentration('A', 1.0)  # not a message\n"
        "    hook = lambda m: m\n")})
    pc.check_message_remedies(b)
    assert result("message remedies run as written")[0] == "PASS"


def test_a_message_with_no_code_passes(make_branch):
    b = make_branch(BASE, {"python/bngsim/_simulator.py": (
        "def scan():\n"
        "    raise ValueError(\"the scan's values must be finite\")\n")})
    pc.check_message_remedies(b)
    assert result("message remedies run as written")[0] == "PASS"


def test_a_test_file_s_strings_are_not_read(make_branch):
    b = make_branch(BASE, {"python/tests/test_x.py": "S = \"on_point=lambda model, value: model.reset()\"\n"})
    pc.check_message_remedies(b)
    assert result("message remedies run as written")[0] == "PASS"
