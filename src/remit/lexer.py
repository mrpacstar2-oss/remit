"""Lexer. Newlines are significant (statement separators) except inside () and []."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import RemitError, Diagnostic, Span

KEYWORDS = {
    "program", "uses", "budget", "usd", "type", "fn", "let", "var", "return", "if", "else",
    "for", "in", "limit", "match", "and", "or", "not", "true", "false", "retry", "timeout",
    "capability", "model", "agent", "tags", "cost", "idempotent", "approval", "always",
    "deny", "approve", "clears", "max", "test", "expect", "break", "input", "import", "as",
}

PUNCT = [
    "=>", "->", "==", "!=", "<=", ">=", "??",
    "(", ")", "{", "}", "[", "]", ",", ":", ".", "=", "+", "-", "*", "/", "%", "<", ">", "|", ";",
]


@dataclass
class Token:
    kind: str  # IDENT, KW, INT, FLOAT, STR, DURATION, OP, NEWLINE, COMMENT, EOF
    value: object
    span: Span
    text: str = ""

    def __repr__(self) -> str:
        return f"Token({self.kind},{self.value!r}@{self.span.line}:{self.span.col})"


def lex(src: str, file: str = "<input>") -> list[Token]:
    toks: list[Token] = []
    i, line, col = 0, 1, 1
    depth = 0  # () and [] nesting
    n = len(src)

    def span(l0, c0, l1=None, c1=None):
        return Span(file, l0, c0, l1 if l1 is not None else line, c1 if c1 is not None else col)

    def err(msg, l0, c0, hint=None):
        raise RemitError(Diagnostic("E0001", msg, Span(file, l0, c0, l0, c0 + 1), hint=hint))

    while i < n:
        ch = src[i]
        if ch == "\n":
            if depth == 0:
                toks.append(Token("NEWLINE", None, span(line, col, line, col + 1)))
            i += 1
            line += 1
            col = 1
            continue
        if ch in " \t\r":
            i += 1
            col += 1
            continue
        if ch == "#":
            j = src.find("\n", i)
            j = n if j == -1 else j
            text = src[i:j]
            toks.append(Token("COMMENT", text, span(line, col, line, col + len(text)), text))
            col += j - i
            i = j
            continue
        l0, c0 = line, col
        if ch.isdigit():
            j = i
            while j < n and (src[j].isdigit() or src[j] == "_"):
                j += 1
            is_float = False
            if j < n and src[j] == "." and j + 1 < n and src[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            text = src[i:j]
            # duration suffix
            if j < n and src[j:j + 2] == "ms" and not (j + 2 < n and (src[j + 2].isalnum() or src[j + 2] == "_")):
                val = float(text.replace("_", "")) / 1000.0
                toks.append(Token("DURATION", val, span(l0, c0, line, col + (j + 2 - i)), src[i:j + 2]))
                col += j + 2 - i
                i = j + 2
                continue
            if j < n and src[j] in "sm" and not (j + 1 < n and (src[j + 1].isalnum() or src[j + 1] == "_")):
                mult = 1.0 if src[j] == "s" else 60.0
                val = float(text.replace("_", "")) * mult
                toks.append(Token("DURATION", val, span(l0, c0, line, col + (j + 1 - i)), src[i:j + 1]))
                col += j + 1 - i
                i = j + 1
                continue
            if j < n and (src[j].isalpha() or src[j] == "_"):
                err(f"invalid number literal '{src[i:j + 1]}'", l0, c0)
            val = float(text.replace("_", "")) if is_float else int(text.replace("_", ""))
            toks.append(Token("FLOAT" if is_float else "INT", val, span(l0, c0, line, col + (j - i)), text))
            col += j - i
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            kind = "KW" if word in KEYWORDS else "IDENT"
            toks.append(Token(kind, word, span(l0, c0, line, col + (j - i)), word))
            col += j - i
            i = j
            continue
        if ch == '"':
            # string with {expr} interpolation; we keep raw parts and let the parser handle exprs
            j = i + 1
            buf = []
            parts: list = []  # list of str | ("expr", text, line, col)
            c = col + 1
            ln = line
            while True:
                if j >= n or src[j] == "\n":
                    err("unterminated string literal", l0, c0, hint='close the string with "')
                cj = src[j]
                if cj == '"':
                    j += 1
                    c += 1
                    break
                if cj == "\\":
                    if j + 1 >= n:
                        err("unterminated escape", ln, c)
                    esc = src[j + 1]
                    mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "{": "{", "}": "}"}
                    if esc not in mapping:
                        err(f"unknown escape '\\{esc}'", ln, c, hint='valid escapes: \\n \\t \\" \\\\ \\{ \\}')
                    buf.append(mapping[esc])
                    j += 2
                    c += 2
                    continue
                if cj == "{":
                    if buf:
                        parts.append("".join(buf))
                        buf = []
                    k = j + 1
                    d = 1
                    while k < n and d > 0:
                        if src[k] == "{":
                            d += 1
                        elif src[k] == "}":
                            d -= 1
                        elif src[k] in '"\n':
                            err("string interpolation must be a simple expression on one line without quotes", ln, c)
                        k += 1
                    if d != 0:
                        err("unclosed '{' in string interpolation", ln, c, hint="use \\{ for a literal brace")
                    parts.append(("expr", src[j + 1:k - 1], ln, c + 1))
                    c += k - j
                    j = k
                    continue
                if cj == "}":
                    err("unmatched '}' in string", ln, c, hint="use \\} for a literal brace")
                buf.append(cj)
                j += 1
                c += 1
            if buf or not parts:
                parts.append("".join(buf))
            toks.append(Token("STR", parts, span(l0, c0, line, c), src[i:j]))
            col = c
            i = j
            continue
        for p in PUNCT:
            if src.startswith(p, i):
                if p in "([":
                    depth += 1
                elif p in ")]":
                    depth = max(0, depth - 1)
                toks.append(Token("OP", p, span(l0, c0, line, col + len(p)), p))
                i += len(p)
                col += len(p)
                break
        else:
            err(f"unexpected character {ch!r}", l0, c0)
    toks.append(Token("NEWLINE", None, Span(file, line, col, line, col)))
    toks.append(Token("EOF", None, Span(file, line, col, line, col)))
    return toks
