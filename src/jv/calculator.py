"""One-argument qalc fast path."""
import re
import subprocess

from jv.verb import JvError, Verb

NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
UNIT = r"(?:miles?|kilometers?|kilometres?|km|meters?|metres?|m|centimeters?|cm|millimeters?|mm|feet|foot|ft|inches?|in|yards?|grams?|g|kilograms?|kg|pounds?|lb|lbs|ounces?|oz|liters?|litres?|gallons?|seconds?|minutes?|hours?|days?|celsius|fahrenheit|kelvin)"


def math_expression(expr: str) -> bool:
    """Admit only clear numeric arithmetic or familiar unit conversions to the fast path."""
    text = expr.strip().casefold()
    if len(text) > 300:
        return False
    if re.fullmatch(r"[\d\s().+*/^%−-]+", text) and re.search(r"\d", text):
        return True
    return bool(re.fullmatch(rf"[+-]?{NUMBER}(?:\s*(?:plus|minus|times|multiplied by|divided by|percent of|% of|[+*/^%-])\s*[+-]?{NUMBER})+", text)
                or re.fullmatch(rf"[+-]?{NUMBER}\s+{UNIT}\s+(?:to|in)\s+{UNIT}", text))


def _calculate(a):
    expr = a["expr"].strip()
    if not expr or not expr.isprintable():
        raise JvError(1, "calculator input is empty or invalid", say="I couldn't work that out.")
    try:
        p = subprocess.run(["qalc", "-t", expr], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise JvError(1, "qalc is unavailable", say="I couldn't work that out.")
    result = p.stdout.strip()
    if p.returncode or not result or "error" in result.casefold() or "unknown" in result.casefold():
        raise JvError(1, "qalc could not evaluate the expression", say="I couldn't work that out.")
    return {"say": result}


VERBS = [Verb("calculate", _calculate, "Calculate or convert one expression with qalc.",
              {"type": "object", "properties": {"expr": {"type": "string", "maxLength": 300}}, "required": ["expr"]},
              ["expr"], args={"expr": {"type": "text"}},
              phrases=["calculate {expr:text}", "what is {expr:text}", "convert {expr:text}"])]
