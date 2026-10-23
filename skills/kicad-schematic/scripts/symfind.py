#!/usr/bin/env python3
"""
symfind.py — поиск символов и посадочных мест в библиотеках KiCad.

Зачем отдельный инструмент: имена символов угадать нельзя. «Стабилизатор на
3,3 В» может лежать как Regulator_Linear:AP2112K-3.3, а может не лежать вовсе,
и попытка поставить несуществующий символ кончается тем, что файл схемы
открывается с дырой. Дешевле сначала спросить библиотеку, чем потом искать,
почему пропал компонент.

Использование:
    python3 symfind.py sym  <подстрока> [--pins]   поиск символа
    python3 symfind.py fp   <подстрока>            поиск посадочного места
    python3 symfind.py pins <Lib:Symbol>           карта выводов символа
"""

import os
import re
import sys

SYMBOL_DIRS = [
    "/usr/share/kicad/symbols",
    os.path.expanduser("~/.local/share/kicad/10.0/symbols"),
]
FOOTPRINT_DIRS = [
    "/usr/share/kicad/footprints",
    os.path.expanduser("~/.local/share/kicad/10.0/footprints"),
]


def _iter_symbol_files():
    for d in SYMBOL_DIRS:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".kicad_sym"):
                yield os.path.join(d, f)


def find_symbols(pattern, limit=40):
    """Ищет по имени, описанию и ключевым словам."""
    rx = re.compile(re.escape(pattern), re.I)
    out = []
    for path in _iter_symbol_files():
        lib = os.path.basename(path)[:-10]
        text = open(path, encoding="utf-8", errors="replace").read()
        # Символы верхнего уровня: отступ ровно один таб
        for m in re.finditer(r'\n\t\(symbol "([^"]+)"', text):
            name = m.group(1)
            if "_" in name and re.search(r"_\d+_\d+$", name):
                continue
            chunk = text[m.start():m.start() + 2000]
            desc = re.search(r'\(property "Description" "([^"]*)"', chunk)
            keyw = re.search(r'\(property "ki_keywords" "([^"]*)"', chunk)
            desc = desc.group(1) if desc else ""
            keyw = keyw.group(1) if keyw else ""
            hay = f"{lib}:{name} {desc} {keyw}"
            if rx.search(hay):
                out.append((f"{lib}:{name}", desc, keyw))
                if len(out) >= limit:
                    return out
    return out


def find_footprints(pattern, limit=40):
    rx = re.compile(re.escape(pattern), re.I)
    out = []
    for d in FOOTPRINT_DIRS:
        if not os.path.isdir(d):
            continue
        for lib in sorted(os.listdir(d)):
            if not lib.endswith(".pretty"):
                continue
            libname = lib[:-7]
            p = os.path.join(d, lib)
            for f in sorted(os.listdir(p)):
                if not f.endswith(".kicad_mod"):
                    continue
                full = f"{libname}:{f[:-10]}"
                if rx.search(full):
                    out.append(full)
                    if len(out) >= limit:
                        return out
    return out


def symbol_pins(lib_id):
    """Карта выводов: номер, имя, тип, координаты в системе символа."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from schlib import load_symbol
    block = load_symbol(lib_id)
    pins = []
    for m in re.finditer(
            r'\(pin\s+(\w+)\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
            r'.*?\(name\s+"([^"]*)".*?\(number\s+"([^"]+)"', block, re.S):
        etype, x, y, ang, name, num = m.groups()
        pins.append((num, name, etype, float(x), float(y), float(ang)))
    pins.sort(key=lambda p: (len(p[0]), p[0]))
    return pins


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, arg = sys.argv[1], sys.argv[2]
    if cmd == "sym":
        res = find_symbols(arg)
        if not res:
            print(f"символов по «{arg}» не найдено")
            return 1
        for lib_id, desc, keyw in res:
            print(f"{lib_id:48} {desc[:70]}")
        if "--pins" in sys.argv and len(res) == 1:
            print()
            main_pins(res[0][0])
    elif cmd == "fp":
        res = find_footprints(arg)
        if not res:
            print(f"посадочных мест по «{arg}» не найдено")
            return 1
        for f in res:
            print(f)
    elif cmd == "pins":
        main_pins(arg)
    else:
        print(__doc__)
        return 1
    return 0


def main_pins(lib_id):
    print(f"{lib_id}:")
    print(f"  {'№':>4} {'имя':<16} {'тип':<14} {'x':>8} {'y':>8} {'угол':>5}")
    for num, name, etype, x, y, ang in symbol_pins(lib_id):
        print(f"  {num:>4} {name:<16} {etype:<14} {x:8.2f} {y:8.2f} {ang:5.0f}")


if __name__ == "__main__":
    sys.exit(main())
