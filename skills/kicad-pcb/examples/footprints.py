#!/usr/bin/env python3
"""
Пример генерации своих посадочных мест (.kicad_mod) кодом.

Вынесено из библиотеки проекта Smart Pole, чтобы скилл не ссылался на чужой
проект. Показано два приёма:

- заглушка — только габарит, без площадок: честнее придуманных площадок,
  которые никто не заметит до первой платы;
- параметрическое место по чертежу производителя: число полюсов — параметр,
  все размеры — из чертежа, с пояснением, чем штатное место KiCad не подошло.

    python3 footprints.py <каталог.pretty>
"""

import os
import sys


def placeholder_footprint(path, name, w, h, descr):
    """Заглушка посадочного места: только габарит, без площадок.

    Площадки без чертежа производителя не рисуются — придуманная площадка
    хуже честной заглушки: её не заметят до первой платы.
    """
    text = f'''(footprint "{name}"
	(version 20240108)
	(generator "footprints")
	(layer "F.Cu")
	(descr "{descr}")
	(tags "placeholder")
	(attr through_hole)
	(fp_text reference "REF**" (at 0 {-h / 2 - 1.5}) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
	(fp_text value "{name}" (at 0 {h / 2 + 1.5}) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
	(fp_text user "ЗАГЛУШКА — нарисовать по чертежу" (at 0 0) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
	(fp_rect (start {-w / 2} {-h / 2}) (end {w / 2} {h / 2}) (stroke (width 0.12) (type solid)) (fill none) (layer "F.SilkS"))
	(fp_rect (start {-w / 2} {-h / 2}) (end {w / 2} {h / 2}) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
)
'''
    open(path, "w", encoding="utf-8").write(text)
    return path


def _pad(num, kind, shape, x, y, w, h, layers, drill=""):
    return (f'\t(pad "{num}" {kind} {shape} (at {round(x, 3)} {round(y, 3)}) '
            f'(size {w} {h}){drill} (layers {layers}))')


def golten_gt301_footprint(path, poles=2):
    """Винтовой клеммник Ningbo Golten GT301-5.0 (вид сверху).

    По чертежу GT301-5.0-XXP-XX-02AH: шаг 5,0 мм, вывод Ø1,0±0,05,
    рекомендованное отверстие Ø1,2; корпус 5,0xN на 7,6 мм, ряд выводов
    в 4,9 мм от стороны ввода провода и в 2,7 мм от задней стенки.
    Штатный MX126-5.0 из библиотеки KiCad совпадает шагом, но у него отверстие
    1,3 мм и корпус 10,5x7,8 мм симметрично выводам — контур на плате и
    двор уезжали бы на 0,9 мм в сторону ввода провода.
    Ввод провода — в сторону +Y (к краю платы).
    """
    tht = '"*.Cu" "*.Mask"'
    pads = []
    for k in range(poles):
        shape = "rect" if k == 0 else "circle"
        pads.append(_pad(str(k + 1), "thru_hole", shape, 5.0 * k, 0, 2.4, 2.4, tht, " (drill 1.2)"))
    x0, x1 = -2.5, 5.0 * poles - 2.5
    y0, y1 = -2.7, 4.9

    def r(v):
        return round(v, 3)
    name = f"TerminalBlock_Golten_GT301-5.0-{poles:02d}P_1x{poles:02d}_P5.00mm"
    text = f'''(footprint "{name}"
	(version 20240108)
	(generator "footprints")
	(layer "F.Cu")
	(descr "Клеммник винтовой Ningbo Golten GT301-5.0-{poles:02d}P, шаг 5,0 мм, отверстие 1,2 мм. Чертёж GT301-5.0-XXP-XX-02AH rev 1")
	(tags "terminal block screw Golten GT301 KF301 DG301 pitch 5.0")
	(attr through_hole)
	(fp_text reference "REF**" (at {r((x0 + x1) / 2)} {r(y0 - 1.6)}) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
	(fp_text value "{name}" (at {r((x0 + x1) / 2)} {r(y1 + 1.6)}) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
	(fp_text user "ввод провода" (at {r((x0 + x1) / 2)} {r(y1 - 1.0)}) (layer "F.Fab") (effects (font (size 0.7 0.7) (thickness 0.1))))
	(fp_rect (start {r(x0)} {r(y0)}) (end {r(x1)} {r(y1)}) (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab"))
	(fp_rect (start {r(x0 - 0.12)} {r(y0 - 0.12)}) (end {r(x1 + 0.12)} {r(y1 + 0.12)}) (stroke (width 0.12) (type solid)) (fill none) (layer "F.SilkS"))
	(fp_line (start {r(x0 - 0.6)} -1.2) (end {r(x0 - 0.6)} 1.2) (stroke (width 0.3) (type solid)) (layer "F.SilkS"))
	(fp_rect (start {r(x0 - 1.0)} {r(y0 - 0.5)}) (end {r(x1 + 0.5)} {r(y1 + 0.5)}) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
{chr(10).join(pads)}
)
'''
    open(path, "w", encoding="utf-8").write(text)
    return path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    pretty = sys.argv[1]
    os.makedirs(pretty, exist_ok=True)
    for poles in (2, 3):
        print("записано:", golten_gt301_footprint(os.path.join(
            pretty, f"TerminalBlock_Golten_GT301-5.0-{poles:02d}P_1x{poles:02d}_P5.00mm.kicad_mod"), poles))
    print("записано:", placeholder_footprint(os.path.join(pretty, "Placeholder_20x10.kicad_mod"),
                                             "Placeholder_20x10", 20, 10, "пример заглушки"))
