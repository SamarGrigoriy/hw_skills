"""
sexp.py — минимальный разбор S-выражений KiCad.

Своё, а не библиотека, по той же причине, что и в остальном проекте: формат
простой, зависимость лишняя. Разбор нужен для чтения уже готовых схем — своих
и нарисованных руками в KiCad, — поэтому он терпим к неизвестным узлам и не
пытается ничего валидировать.
"""


def parse(text):
    """Возвращает вложенные списки; атомы — строки. Кавычки снимаются,
    а сам факт «это была строка в кавычках» теряется намеренно: для наших
    задач различие между `no` и `"no"` не значимо."""
    pos = 0
    n = len(text)

    def skip():
        nonlocal pos
        while pos < n:
            c = text[pos]
            if c in " \t\r\n":
                pos += 1
            elif c == ";":                     # комментарий до конца строки
                while pos < n and text[pos] != "\n":
                    pos += 1
            else:
                return

    def node():
        nonlocal pos
        skip()
        if pos >= n:
            return None
        if text[pos] == "(":
            pos += 1
            out = []
            while True:
                skip()
                if pos >= n:
                    raise ValueError("неожиданный конец файла")
                if text[pos] == ")":
                    pos += 1
                    return out
                out.append(node())
        if text[pos] == '"':
            pos += 1
            buf = []
            while pos < n:
                c = text[pos]
                if c == "\\":
                    buf.append(text[pos + 1])
                    pos += 2
                    continue
                if c == '"':
                    pos += 1
                    return "".join(buf)
                buf.append(c)
                pos += 1
            raise ValueError("незакрытая строка")
        start = pos
        while pos < n and text[pos] not in ' \t\r\n()"':
            pos += 1
        return text[start:pos]

    return node()


def find_all(tree, tag):
    """Все узлы с данным тегом на любом уровне."""
    out = []
    if isinstance(tree, list):
        if tree and tree[0] == tag:
            out.append(tree)
        for x in tree:
            out.extend(find_all(x, tag))
    return out


def get(node, tag):
    """Первый прямой потомок с данным тегом."""
    for x in node:
        if isinstance(x, list) and x and x[0] == tag:
            return x
    return None


def val(node, tag, default=None):
    """Значение простого узла вида (tag value)."""
    x = get(node, tag)
    return x[1] if x and len(x) > 1 else default


def props(node):
    """Свойства символа: {имя: значение}."""
    out = {}
    for p in node:
        if isinstance(p, list) and p and p[0] == "property" and len(p) >= 3:
            out[p[1]] = p[2]
    return out


def load(path):
    return parse(open(path, encoding="utf-8").read())
