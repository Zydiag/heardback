import pytest

from jv.verb import _readback


@pytest.mark.parametrize("sentence, spoken", [
    ("Remember this: my code is blue.?", "Remember this: my code is blue?"),
    ("Remember this: wow!?", "Remember this: wow?"),
    ("Capture this note: a b?", "Capture this note: a b?"),
])
def test_readback_has_one_end_mark(sentence, spoken):
    assert _readback(sentence) == spoken
