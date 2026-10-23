#!/usr/bin/env python3
"""
selftest.py — самопроверка скилла: координаты выводов и линтер-ловушка.

Часть 1. Сверка расчёта координат выводов с самим KiCad.

Расчёт положения вывода при повороте символа легко сделать «почти правильно»:
формула выглядит разумной, файл открывается, схема рисуется — и только провода
молча не попадают в выводы. ERC при этом ругается на «не подключено», а глаз
видит соединение. Именно так 90° и 270° однажды оказались перепутаны местами.

Здесь символы ставятся под всеми четырьмя углами БЕЗ единого провода, а затем
из отчёта ERC вынимаются координаты, которые KiCad сам считает положением
выводов («Pin not connected @ (x, y)»), и сравниваются с расчётом.

Часть 2. Ловушка для линтера. Лист, на котором нарочно сделано всё, что уже
однажды попадало на реальную схему и прошло «0 ошибок»: надпись на надписи,
провод по подписи, подпись поверх чужого корпуса, деталь и метка за рамкой,
пересекающиеся рамки, «₽», которого нет в шрифте, перевёрнутая подпись,
подпись по ту сторону провода, подписи двух деталей столбиком вплотную.
Тест падает, если schcheck НЕ нашёл хоть один дефект: линтер, который молчит
на каше, хуже отсутствия линтера — он выдаёт кашу за проверенную схему.

Запуск: python3 selftest.py    (нужен kicad-cli)
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schlib import Project, pin_xy, symbol_pins, symbol_geometry
import schcheck

MULTI = "4xxx:40106"            # многоюнитный символ: 6 инверторов + питание

CASES = [
    ("Device:R", "R"),
    ("Device:C", "C"),
    ("Transistor_FET:AO3401A", "Q"),
    ("Regulator_Linear:TPS7A0533PDBZ", "U"),
    ("Connector:Conn_01x02_Pin", "J"),
    # Производный символ, у предка которого есть собственные поля (Sim.*):
    # копия в lib_symbols обязана совпасть с библиотекой (см. проверку ниже).
    ("Transistor_BJT:BC817", "Q"),
]
ANGLES = (0, 90, 180, 270)
MIRRORS = (None, "x", "y")


# Дефект -> подстрока, по которой его обязан узнать schcheck.
TRAP = [
    ("надпись на надписи", "наложились"),
    ("провод по надписи", "проходит по надписи"),
    ("подпись на чужом корпусе", "лежит на корпусе"),
    ("деталь за рамкой", "выходит за рамку «Рамка А»"),
    ("метка за рамкой", "метка «TRAP_NET»"),
    ("рамки пересекаются", "пересекаются"),
    ("глиф вне шрифта", "отсутствуют в шрифте"),
    ("перевёрнутая подпись", "вверх ногами"),
    ("подпись за проводом", "отделена от"),
    ("подписи двух деталей вплотную", "вплотную"),
    ("два значка земли вплотную", "корпуса символов"),
    ("заголовок длиннее штампа", "не помещается в штамп"),
    ("подпись между двумя деталями", "«R8» стоит между"),
    ("подпись у края промежутка", "«R10» стоит между"),
]


def trap():
    """Лист с дефектами из TRAP; возвращает число НЕ пойманных."""
    tmp = tempfile.mkdtemp(prefix="schcheck_trap_")
    # Заголовок нарочно длиннее графы штампа: KiCad печатает его за край листа.
    pr = Project("trap", outdir=tmp, title="Ловушка для линтера: заголовок листа такой "
                 "длинный, что вылезает из штампа")
    sh = pr.root
    fp = dict(footprint="test:test", fields={"MPN": "x", "Datasheet": "x"})
    sh.box((25.4, 25.4), (101.6, 88.9), "Рамка А")
    sh.box((88.9, 76.2), (139.7, 127.0), "Рамка Б")                  # пересекаются
    sh.text((30.48, 38.1), "НАЛОЖЕНИЕ")
    sh.text((33.02, 38.1), "ТЕКСТ")                                  # на надписи
    sh.text((30.48, 45.72), "ПОДПИСЬ")
    sh.wire((27.94, 45.72), (55.88, 45.72))                          # провод по надписи
    sh.text((30.48, 132.08), "Цена 100 ₽")                           # глиф
    r1 = sh.sym("R1", "Device:R", (45.72, 60.96), value="10k", **fp)
    r2 = sh.sym("R2", "Device:R", (58.42, 60.96), value="10k", **fp)
    r3 = sh.sym("R3", "Device:R", (101.6, 55.88), value="1k", **fp)  # на рамке
    r4 = sh.sym("R4", "Device:R", (35.56, 76.2), value="4.7k", **fp)
    r5 = sh.sym("R5", "Device:R", (71.12, 71.12), value="1M", **fp)
    sh.wire((76.2, 64.77), (76.2, 77.47))                            # между R5 и подписью
    r6 = sh.sym("R6", "Device:R", (86.36, 40.64), value="22k", **fp)
    r7 = sh.sym("R7", "Device:R", (86.36, 53.34), value="33k", **fp)
    sh.wire((93.98, 35.56), (99.06, 35.56))
    sh.glabel((99.06, 35.56), "TRAP_NET")                            # метка через рамку
    # Подписи расставлены вручную, автопостановка отключена: дефекты нужны.
    r1.field_pos = {"Reference": (58.42, 60.96, None), "Value": (47.75, 58.42, "left")}
    r2.field_pos = {"Reference": (60.96, 50.8, "left"), "Value": (60.96, 52.07, "left")}
    r3.field_pos = {"Reference": (104.14, 50.8, "left"), "Value": (104.14, 52.83, "left")}
    r4.field_pos = {"Reference": (38.1, 80.01, "left"), "Value": (38.1, 81.28, "left")}
    r5.field_pos = {"Reference": (80.01, 69.85, "left"), "Value": (80.01, 71.76, "left")}
    # R6 и R7: подписи двух разных деталей столбиком с зазором 0,3 мм.
    r6.field_pos = {"Reference": (66.04, 39.37, "left"), "Value": (66.04, 41.28, "left")}
    r7.field_pos = {"Reference": (66.04, 42.93, "left"), "Value": (66.04, 44.84, "left")}
    # Два значка земли в 2,54 мм: стрелки касаются и читаются как одна земля.
    sh.power("GND", (116.84, 116.84))
    sh.power("GND", (119.38, 116.84))
    # R8 и R9 в ряд, подписи R8 — над промежутком между ними: до обоих корпусов
    # одинаково далеко, и «R8» читается как подпись R9.
    r8 = sh.sym("R8", "Device:R", (35.56, 149.86), value="10k", angle=90, **fp)
    r9 = sh.sym("R9", "Device:R", (48.26, 149.86), value="10k", angle=90, **fp)
    r8.field_pos = {"Reference": (41.91, 146.05, None), "Value": (41.91, 147.96, None)}
    r9.field_pos = {"Reference": (48.26, 144.78, None), "Value": (48.26, 146.69, None)}
    # Пограничный случай: до своего корпуса 2,9 мм, до соседнего 4,0 мм. С
    # коэффициентом 1,25 проходило — так встали подписи стабилитронов D3/D4.
    r10 = sh.sym("R10", "Device:R", (35.56, 160.02), value="10k", angle=90, **fp)
    sh.sym("R11", "Device:R", (50.8, 160.02), value="10k", angle=90, **fp).field_pos = {
        "Reference": (50.8, 155.0, None), "Value": (50.8, 156.9, None)}
    r10.field_pos = {"Reference": (40.62, 158.75, "left"), "Value": (40.62, 160.66, "left")}
    sh._placed = True
    pr.save()
    path = os.path.join(tmp, "trap.kicad_sch")
    text = open(path, encoding="utf-8").read()
    # R4.Reference с углом 180 — KiCad нарисует её вверх ногами.
    i = text.index('(property "Reference" "R4"')
    j = text.index(" 0)", i)
    text = text[:j] + " 180)" + text[j + 3:]
    open(path, "w", encoding="utf-8").write(text)

    rep = schcheck.Report()
    texts = schcheck.render_texts([path]) or {}
    schcheck.check_sheet(path, rep)
    schcheck.check_geometry(path, rep, texts.get("trap.kicad_sch"))
    found = "\n".join(m for _w, m in rep.errors)
    missed = [name for name, key in TRAP if key not in found]
    for name, key in TRAP:
        print(f"  {'поймано ' if name not in missed else 'ПРОПУЩЕНО'}: {name}")
    return len(missed)


def main():
    print("== ловушка для линтера")
    missed = trap()
    if missed:
        print(f"ОШИБКА: линтер пропустил дефектов: {missed}")
    print("== координаты выводов")
    code = coords()
    return 1 if missed or code else 0


def coords():
    tmp = tempfile.mkdtemp(prefix="schlib_selftest_")
    pr = Project("selftest", outdir=tmp, title="Сверка координат выводов", paper="A2")
    sh = pr.root

    expected = {}
    x = 30.48
    for lib_id, prefix in CASES:
        y = 38.1
        for mir in MIRRORS:
            for ang in ANGLES:
                ref = f"{prefix}{len(expected) + 1}"
                sh.sym(ref, lib_id, (x, y), angle=ang, mirror=mir, footprint="test:test")
                for num in symbol_pins(lib_id):
                    expected[(ref, num)] = pin_xy(lib_id, num, (x, y), ang, mir)
                y += 25.4
        x += 50.8
    # Многоюнитный корпус: шесть инверторов 40106 и юнит питания, каждый под
    # четырьмя углами. У каждого юнита свои выводы (у DD1C — 5 и 6), и KiCad
    # обязан найти их там же, где их считает генератор.
    for k, ang in enumerate(ANGLES):
        ref = f"DD{k + 1}"
        for u in range(1, 8):
            at = (x + (u - 1) * 30.48, 38.1 + k * 38.1)
            sh.sym(ref, MULTI, at, angle=ang, unit=u, footprint="test:test")
            for num in {p[0] for p in symbol_geometry(MULTI, u)["pins"]}:
                expected[(ref, num)] = pin_xy(MULTI, num, at, ang)
    sh.text((25.4, 20.32), "Самотест: символы под всеми углами, проводов нет.")
    pr.save()

    # Одинаковый UUID у двух объектов KiCad молча «чинит» при открытии, и
    # файл после первого сохранения уже не совпадает со сгенерированным.
    body = open(os.path.join(tmp, "selftest.kicad_sch"), encoding="utf-8").read()
    uuids = re.findall(r'\(uuid "([^"]+)"\)', body)
    dup = {u for u in uuids if uuids.count(u) > 1}
    if dup:
        print(f"ОШИБКА: повторяющиеся UUID в файле: {len(dup)}")
        return 1

    rpt = os.path.join(tmp, "erc.rpt")
    subprocess.run(["kicad-cli", "sch", "erc", "--severity-all",
                    "--output", rpt,
                    os.path.join(tmp, "selftest.kicad_sch")],
                   capture_output=True, timeout=180)
    if not os.path.exists(rpt):
        print("ОШИБКА: kicad-cli не создал отчёт")
        return 2

    text = open(rpt, encoding="utf-8", errors="replace").read()
    # Копия символа в lib_symbols, разошедшаяся с библиотекой, — ошибка
    # разворачивания наследования в load_symbol(): KiCad видит её на каждом
    # таком символе, а пользователь — десяток предупреждений в ERC.
    mism = text.count("[lib_symbol_mismatch]")
    if mism:
        print(f"ОШИБКА: символов, не совпадающих с библиотекой: {mism}")
        return 1
    # «@(60.96 mm, 66.04 mm): Symbol Q101 Вывод 1 [G, ...]»
    found = {}
    for m in re.finditer(
            r"@\(([-\d.]+) mm, ([-\d.]+) mm\):\s*Symbol (\S+)\s+\S+\s+(\S+)",
            text):
        fx, fy, ref, num = m.groups()
        # Часть корпуса KiCad может назвать с буквой юнита (DD1C).
        if (ref, num) not in expected and ref[-1:].isalpha():
            ref = ref[:-1]
        found[(ref, num)] = (float(fx), float(fy))

    if not found:
        print("ОШИБКА: в отчёте нет координат выводов — нечего сверять")
        return 2

    bad = 0
    checked = 0
    multi = 0
    for key, kicad_xy in sorted(found.items()):
        if key not in expected:
            continue
        checked += 1
        multi += key[0].startswith("DD")
        calc = expected[key]
        if abs(calc[0] - kicad_xy[0]) > 0.01 or abs(calc[1] - kicad_xy[1]) > 0.01:
            bad += 1
            print(f"РАСХОЖДЕНИЕ {key[0]}.{key[1]}: расчёт {calc}, KiCad {kicad_xy}")

    print(f"сверено выводов: {checked} (из них многоюнитных {multi}), расхождений: {bad}")
    # Выводы многоюнитного корпуса, которых KiCad не нашёл, — тоже провал:
    # значит, экземпляр юнита потерял свои выводы.
    want = sum(1 for k in expected if k[0].startswith("DD"))
    if multi < want:
        print(f"ОШИБКА: KiCad нашёл {multi} выводов многоюнитного корпуса из {want}")
        bad += 1
    if not bad:
        print("расчёт координат совпадает с KiCad")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
