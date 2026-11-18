import subprocess

import pytest

from jv import calculator


@pytest.mark.parametrize("expression", ["18 percent of 2400", "5 miles to kilometers", ";$(touch /tmp/nope)`"])
def test_calculator_passes_expression_as_one_literal_argument(monkeypatch, run_jv, expression):
    seen = []
    def fake(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "432\n", "")
    monkeypatch.setattr(calculator.subprocess, "run", fake)
    code, out, _ = run_jv("calculate", expression, "--json")
    assert code == 0 and out["say"] == "432"
    assert seen[0][0] == ["qalc", "-t", expression]
    assert not seen[0][1].get("shell")


@pytest.mark.parametrize("rc,out", [(1, ""), (0, ""), (0, "error: parse error")])
def test_calculator_odd_results_use_fixed_sentence(monkeypatch, run_jv, rc, out):
    monkeypatch.setattr(calculator.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, ""))
    code, result, _ = run_jv("calculate", "not math", "--json")
    assert code == 1 and result["say"] == "I couldn't work that out."


def test_calculator_missing_program_is_plain(monkeypatch, run_jv):
    monkeypatch.setattr(calculator.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    code, result, _ = run_jv("calculate", "1+1", "--json")
    assert code == 1 and result["say"] == "I couldn't work that out."


@pytest.mark.parametrize("expression", ["2 + 2", "2 plus 2", "18 percent of 2400", "18% of 2400", "(2 + 3) * 4", "-5.2", "5 miles to kilometers", "32 fahrenheit to celsius", "2 hours in minutes", "two times three"])
def test_clear_math_shapes(expression):
    assert calculator.math_expression(expression)


@pytest.mark.parametrize("expression", ["weather", "my volume", "this PDF to text", "2 plus 2 and explain how it works", "5 miles to kilometers and send it to Claude", ";$(touch /tmp/nope)`", "", "2 bananas to apples"])
def test_other_requests_are_not_math(expression):
    assert not calculator.math_expression(expression)

