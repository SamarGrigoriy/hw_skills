#!/usr/bin/env python3
"""
mksym.py — создание собственных символов KiCad.

Половины нужных компонентов в стандартных библиотеках нет: ни CMT2300A, ни
модулей Ebyte на LLCC68, ни китайских LTE-модемов. Рисовать их мышью в
редакторе символов — работа на час на каждый, причём неповторяемая: через
полгода никто не вспомнит, откуда взялся порядок выводов.

Здесь символ описывается таблицей выводов, а рисуется скриптом. Таблица
переносится из даташита один раз, и рядом с ней в комментарии остаётся ссылка
на страницу — тогда сверка занимает минуту, а не полчаса.

Раскладка: выводы группируются по сторонам, питание сверху, земля снизу,
управление слева, данные справа. Корпус считается ПО ДЛИНЕ ИМЁН выводов, а не
только по их числу: имена верхних и нижних выводов пишутся вертикально внутрь
корпуса и без запаса по высоте сталкиваются друг с другом («VCC» и «GND»
сливались в «G/VDC»), а длинные имена слева и справа — посередине.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kfont   # noqa: E402

GRID = 1.27
PIN_LEN = 3.81          # длина вывода, кратна сетке
PITCH = 2.54            # шаг выводов
NAME_OFFSET = 1.016     # отступ имени вывода от края корпуса (pin_names offset)
FONT = 1.27
GAP = 1.27              # зазор между встречными именами


def _up(v):
    """Округление вверх до сетки."""
    n = int(v / GRID)
    return (n if abs(n * GRID - v) < 1e-6 else n + 1) * GRID


def _name_len(name):
    return kfont.ink_width(name.replace("~{", "").replace("}", ""), FONT)


def make_symbol(name, pins, description="", keywords="", footprint="",
                datasheet="", ref_prefix="U", width=None):
    """Собирает определение символа.

    pins — список кортежей (номер, имя, тип, сторона), где сторона это
    "L" (слева), "R" (справа), "T" (сверху), "B" (снизу).
    Типы KiCad: input, output, bidirectional, tri_state, passive, free,
    unspecified, power_in, power_out, open_collector, open_emitter, no_connect.
    width — минимальная ширина корпуса; если имена длиннее, корпус шире.
    """
    # Пятый элемент кортежа True — «стопочный» скрытый вывод: он стоит в той же
    # точке, что и видимый вывод с тем же именем на той же стороне. Так рисуют
    # девять выводов GND модуля одним выводом, а в нетлисте остаются все девять.
    stacked = [p for p in pins if len(p) > 4 and p[4]]
    pins = [p[:4] for p in pins if not (len(p) > 4 and p[4])]
    by = {s: [p for p in pins if p[3] == s] for s in "LRTB"}
    nl, nr = len(by["L"]), len(by["R"])
    nt, nb = len(by["T"]), len(by["B"])

    # Вертикальные зоны: имена сверху и снизу занимают высоту по своей длине.
    top_zone = max([_name_len(p[1]) for p in by["T"]] + [0]) + NAME_OFFSET
    bot_zone = max([_name_len(p[1]) for p in by["B"]] + [0]) + NAME_OFFSET
    side_rows = max(nl, nr, 1)
    side_h = (side_rows - 1) * PITCH
    # Боковые выводы — ниже зоны верхних имён и выше зоны нижних.
    h = _up(top_zone + GAP + side_h + GAP + bot_zone) if (nt or nb) else \
        _up(side_h + 2 * PITCH)
    h = max(h, 2 * PITCH + PITCH)

    # Горизонтальные зоны: левое и правое имя в одной строке не должны встретиться;
    # верхние/нижние выводы должны поместиться с шагом PITCH.
    lw = max([_name_len(p[1]) for p in by["L"]] + [0]) + NAME_OFFSET
    rw = max([_name_len(p[1]) for p in by["R"]] + [0]) + NAME_OFFSET
    w_names = lw + rw + 2 * GAP if (nl and nr) else max(lw, rw) + 2 * GAP
    w_tb = (max(nt, nb, 1) + 1) * PITCH
    w = _up(max(w_names, w_tb, width or 0, 10.16))
    # Выводы кратны сетке 2,54 только если края корпуса кратны 1,27 от центра.
    w = w if abs((w / 2) / GRID - round((w / 2) / GRID)) < 1e-6 else w + GRID
    h = h if abs((h / 2) / GRID - round((h / 2) / GRID)) < 1e-6 else h + GRID
    x0, x1 = -w / 2, w / 2

    # Первый боковой вывод — под зоной верхних имён, на сетке PITCH. Если
    # после привязки к сетке нижний вывод залез в зону нижних имён, корпус
    # удлиняется на две клетки и расчёт повторяется.
    while True:
        y0, y1 = -h / 2, h / 2
        limit = y1 - (top_zone + GAP if nt else PITCH)
        first = int(limit / PITCH) * PITCH
        while first > limit + 1e-6:
            first -= PITCH
        lowest = first - PITCH * (side_rows - 1)
        if (not nb or lowest >= y0 + bot_zone + GAP - 1e-6) and lowest >= y0 + 1e-6:
            break
        h += 2 * GRID

    out = []
    for side, lst in by.items():
        n = len(lst)
        for i, (num, pname, ptype, _s) in enumerate(lst):
            if side == "L":
                out.append((num, pname, ptype, x0 - PIN_LEN, round(first - PITCH * i, 4), 0))
            elif side == "R":
                out.append((num, pname, ptype, x1 + PIN_LEN, round(first - PITCH * i, 4), 180))
            elif side == "T":
                x = -(n - 1) * PITCH / 2 + i * PITCH
                out.append((num, pname, ptype, x, y1 + PIN_LEN, 270))
            else:
                x = -(n - 1) * PITCH / 2 + i * PITCH
                out.append((num, pname, ptype, x, y0 - PIN_LEN, 90))
    # Верхние/нижние выводы при чётном числе попадают на полусетку — сдвиг.
    fixed = []
    for num, pname, ptype, x, y, ang in out:
        if ang in (90, 270) and abs(x / PITCH - round(x / PITCH)) > 1e-6:
            x = round(x + PITCH / 2, 4)
        fixed.append((num, pname, ptype, round(x, 4), round(y, 4), ang))
    for num, pname, ptype, side in [p[:4] for p in stacked]:
        base = next((f for f in fixed if f[1] == pname and
                     {"L": 0, "R": 180, "T": 270, "B": 90}[side] == f[5]), None)
        assert base, f"{name}: для скрытого вывода {num} нет видимого «{pname}» на стороне {side}"
        fixed.append((num, pname, "passive", base[3], base[4], base[5], True))

    pin_txt = "\n".join(f'''			(pin {f[2]} line
				(at {f[3]} {f[4]} {f[5]})
				(length {PIN_LEN}){"" if len(f) < 7 else chr(10) + chr(9) * 4 + "(hide yes)"}
				(name "{f[1]}"
					(effects
						(font
							(size 1.27 1.27)
						)
					)
				)
				(number "{f[0]}"
					(effects
						(font
							(size 1.27 1.27)
						)
					)
				)
			)''' for f in fixed)

    top_pin = y1 + (PIN_LEN if nt else 0)
    bot_pin = y0 - (PIN_LEN if nb else 0)
    return f'''	(symbol "{name}"
		(pin_names
			(offset {NAME_OFFSET})
		)
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(property "Reference" "{ref_prefix}"
			(at {x1 + 1.27} {top_pin + 1.27} 0)
			(effects
				(font
					(size 1.27 1.27)
				)
				(justify left)
			)
		)
		(property "Value" "{name}"
			(at {x1 + 1.27} {bot_pin - 1.27} 0)
			(effects
				(font
					(size 1.27 1.27)
				)
				(justify left)
			)
		)
		(property "Footprint" "{footprint}"
			(at 0 0 0)
			(hide yes)
			(effects
				(font
					(size 1.27 1.27)
				)
			)
		)
		(property "Datasheet" "{datasheet}"
			(at 0 0 0)
			(hide yes)
			(effects
				(font
					(size 1.27 1.27)
				)
			)
		)
		(property "Description" "{description}"
			(at 0 0 0)
			(hide yes)
			(effects
				(font
					(size 1.27 1.27)
				)
			)
		)
		(property "ki_keywords" "{keywords}"
			(at 0 0 0)
			(hide yes)
			(effects
				(font
					(size 1.27 1.27)
				)
			)
		)
		(symbol "{name}_0_1"
			(rectangle
				(start {x0} {y1})
				(end {x1} {y0})
				(stroke
					(width 0.254)
					(type default)
				)
				(fill
					(type background)
				)
			)
		)
		(symbol "{name}_1_1"
{pin_txt}
		)
	)'''


def make_library(path, symbols):
    """Записывает .kicad_sym с перечисленными символами."""
    body = "\n".join(symbols)
    text = f'''(kicad_symbol_lib
	(version 20241209)
	(generator "mksym")
	(generator_version "10.0")
{body}
)
'''
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    open(path, "w", encoding="utf-8").write(text)
    return path
