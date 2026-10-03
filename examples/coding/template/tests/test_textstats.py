import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from textstats import word_count, average_word_length


def test_word_count_handles_whitespace():
    assert word_count("hello   world\nagain") == 3


def test_empty_text():
    assert word_count("") == 0
    assert average_word_length("") == 0.0


def test_average_is_exact():
    assert average_word_length("ab abc") == 2.5
