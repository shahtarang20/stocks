"""scripts/annotate_failures.py: put a failed test run where it can be read without a login."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("annotate_failures", Path(__file__).resolve().parents[2] / "scripts" / "annotate_failures.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def test_failed_tests_are_listed_and_the_end_of_the_report_is_included():
    report = "....F\n=== short test summary info ===\nFAILED tests/a.py::test_one - assert 1 == 2\nFAILED tests/b.py::test_two\n2 failed\n"
    lines = mod.build(report, "Agent tests")
    assert lines[0].startswith("::error title=Agent tests: 2 failing::")
    assert "tests/a.py::test_one" in lines[0] and "tests/b.py::test_two" in lines[0]
    assert lines[1].startswith("::error title=Agent tests: end of the report::") and "2 failed" in lines[1]


def test_newlines_and_percent_signs_are_escaped_the_way_github_wants():
    assert mod.escape("a\nb%c\r") == "a%0Ab%25c%0D"
    assert all("\n" not in line for line in mod.build("FAILED x\nmore\n"))  # every annotation is one line


def test_a_long_report_is_cut_to_its_end_and_a_long_list_of_failures_is_summarised():
    long_report = "x" * 10_000 + "\nTHE END"
    assert len(mod.build(long_report)[-1]) < 4_300 and "THE END" in mod.build(long_report)[-1]
    many = "\n".join(f"FAILED t{i}" for i in range(100))
    first = mod.build(many)[0]
    assert "and 60 more" in first and first.count("%0A") <= 41


def test_it_never_fails_even_without_a_file(capsys):
    assert mod.main(["annotate_failures.py", "/no/such/file"]) == 0
    assert "could not read the report" in capsys.readouterr().out
    assert mod.main(["annotate_failures.py"]) == 0
