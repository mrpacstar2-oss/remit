"""Tiny text statistics module used as a coding-workflow target. It contains deliberate bugs."""


def words(text):
    return text.split(" ")


def word_count(text):
    return len(words(text))


def average_word_length(text):
    ws = words(text)
    if not ws:
        return 0.0
    return sum(len(w) for w in ws) // len(ws)
