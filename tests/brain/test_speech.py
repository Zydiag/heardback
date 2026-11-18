import pytest

from heardback.brain.speech import SentenceSplitter, asks, unquoted


def test_split_sentences():
    s = SentenceSplitter()
    assert s.feed("Hello there. How") == ["Hello there."]
    assert s.feed(" are you?\nFine") == ["How are you?"]
    assert s.flush() == "Fine"


def test_strip_markdown():
    s = SentenceSplitter()
    assert s.feed("**Done** with `x`. ") == ["Done with x."]


def test_long_run_cut_at_200():
    s = SentenceSplitter()
    out = s.feed("word " * 60)
    assert out and all(len(x) <= 200 for x in out)
    assert " ".join(out + [s.flush()]) == " ".join(["word"] * 60)


def test_long_word_hard_cut():
    s = SentenceSplitter()
    out = s.feed("x" * 450)
    assert [len(x) for x in out] == [200, 200] and s.flush() == "x" * 50


def test_sentence_end_waits_for_space():
    s = SentenceSplitter()
    assert s.feed("Pi is 3.14") == [] and s.feed(" today! ") == ["Pi is 3.14 today!"]


def test_late_sentence_end_still_capped():   # M2 review: a "." past 200 chars must not make a 400-char say
    s = SentenceSplitter()
    out = s.feed("word " * 80 + "end. next ")
    assert out and all(len(x) <= 200 for x in out) and s.flush() == "next"


def test_split_whole_text():
    parts = SentenceSplitter().split("One. " + "word " * 70)
    assert parts[0] == "One." and all(len(x) <= 200 for x in parts) and len(parts) == 3


def test_asks():
    assert asks("Which window should I type into: the new terminal I just opened, or a Claude session? "
                "Tell me the name and I'll send it.")
    assert asks("Which one?")
    assert not asks("Opened Zen. Switched to WhatsApp.")
    assert not asks("Did you mean the wallpaper? I changed it anyway. It is the blue one now. All set.")


@pytest.mark.parametrize("left,right", [('"', '"'), ('“', '”')])
def test_quoted_question_and_claim_are_only_reference_text(left, right):
    quote = f"{left}I have saved this note, why didn't it appear?{right}"
    text = f"Sent {quote} to window 3."
    assert unquoted(text) == "Sent   to window 3."
    assert not asks(text)
    assert asks(text + " Anything else?")


@pytest.mark.parametrize("text,says", [   # live 20261031-225734-1: split at "..." into two says
    ('Playing "Just Thinking... Retro Jazz" on YouTube in Zen.', ['Playing "Just Thinking... Retro Jazz" on YouTube in Zen.']),
    ("Playing Just Thinking...Retro Jazz now. Enjoy.", ["Playing Just Thinking...Retro Jazz now.", "Enjoy."]),
    ("Hmm… let me check. Done.", ["Hmm… let me check.", "Done."]),
    ("Wait... it worked. Done.", ["Wait... it worked.", "Done."]),
    ('He said "Stop. Now." Then left.', ['He said "Stop. Now."', "Then left."]),
    ("Opened “A. B” for you. Next?", ["Opened “A. B” for you.", "Next?"]),
])
def test_no_cut_at_ellipsis_or_in_quotes(text, says):
    assert SentenceSplitter().split(text) == says
    s = SentenceSplitter()   # streamed in small deltas, like Claude's text_delta
    out = [x for i in range(0, len(text), 3) for x in s.feed(text[i:i + 3])]
    assert out + [s.flush()] == says
    from heardback.voice.text import split_sentences
    assert split_sentences(text) == says
