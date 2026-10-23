#!/usr/bin/env python3
"""
Пример: блок питания датчикового узла. Один корневой лист и один дочерний.

Это одновременно и образец для копирования, и проверка скилла: пример обязан
проходить schcheck без единой ошибки и ERC без нарушений, кроме тех, что
объяснены ниже. Если после правки ядра он перестал проходить — сломано ядро,
а не пример.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from schlib import Project

FP_R = "Resistor_SMD:R_0402_1005Metric"
FP_C = "Capacitor_SMD:C_0603_1608Metric"

pr = Project("example", outdir=HERE, title="Пример: питание узла", rev="A",
             company="Smart Pole")

# ---------------------------------------------------------------------------
# Корневой лист: только блоки и связи. Ни одной пассивной детали.

root = pr.root
root.note((25.4, 25.4), [
    "Верхний уровень: состав устройства и связи между блоками.",
    "Пассивных элементов здесь нет — они внутри листов.",
    "Разъёмы — исключение: это внешние интерфейсы изделия, и видеть их",
    "надо сразу, не проваливаясь в листы.",
])

pw = pr.sheet("example_power.kicad_sch", "Питание", (50.8, 50.8), (50.8, 20.32),
              pins=[("VBAT", "input"), ("+3V3", "output")],
              title="Питание: 18650 -> LDO -> +3V3")

ld = pr.sheet("example_load.kicad_sch", "Нагрузка", (137.16, 50.8), (45.72, 20.32),
              pins=[("+3V3", "input")], title="Потребитель (место микроконтроллера)")

vbat = pr.sheets[0][1].pin_xy("VBAT")
v33 = pr.sheets[0][1].pin_xy("+3V3")
# Разъём банки 18650 — источник VBAT для всего изделия.
j101 = root.sym("J101", "Connector:Conn_01x02_Pin", (25.4, vbat[1]),
                value="18650",
                footprint="Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
                fields={"MPN": "B2B-PH-K-S", "Manufacturer": "JST",
                        "Supplier": "LCSC", "Price": "12",
                        "Alt": "клеммник 5.08 мм; держатель 18650 с проводами",
                        "Datasheet": "https://www.jst-mfg.com/product/pdf/eng/ePH.pdf",
                        "Note": "плюс банки; минус — на GND"})
# Метку рядом с выводом листа не ставим: вывод уже подписан своим именем,
# вторая надпись в том же месте только засоряет лист.
root.wire(j101.pin("1"), vbat)
root.wire(j101.pin("2"), (j101.pin("2")[0], j101.pin("2")[1] + 7.62))
root.power("GND", (j101.pin("2")[0], j101.pin("2")[1] + 7.62))
ld_in = pr.sheets[1][1].pin_xy("+3V3")
root.wire(v33, ld_in)

# ---------------------------------------------------------------------------
# Лист питания

pw.box((30.48, 45.72), (139.7, 92.71), "Стабилизатор +3,3 В")
pw.note((43.18, 100.33), [
    "Ток покоя стабилизатора попадает прямо в срок автономии: при среднем",
    "потреблении узла 25 мкА разница между 1 мкА и 50 мкА — это разница",
    "между сезоном работы и месяцем. Отсюда TPS7A0533 (Iq 25 нА), а не AMS1117.",
    "",
    "C2 намеренно 22 мкФ, а не 1 мкФ: на морозе внутреннее сопротивление",
    "банки растёт втрое, и импульс тока радиопередатчика проседает питание",
    "ниже порога сброса микроконтроллера.",
])

# Входная и выходная цепи на одной высоте — поток слева направо.
Y = 63.5

pw.hlabel((48.26, Y), "VBAT", shape="input", angle=180, justify="right")

u1 = pw.sym("U101", "Regulator_Linear:TPS7A0533PDBZ", (88.9, Y),
            value="TPS7A0533PDBZ", footprint="Package_TO_SOT_SMD:SOT-23",
            fields={"MPN": "TPS7A0533PDBZR", "Manufacturer": "Texas Instruments",
                    "Supplier": "LCSC", "Price": "50",
                    "Alt": "XC6206P332MR (Iq 1 мкА), HT7333",
                    "Datasheet": "https://www.ti.com/lit/ds/symlink/tps7a05.pdf",
                    "Note": "Iq 25 нА — определяет автономию узла"})

c1 = pw.sym("C101", "Device:C", (68.58, 73.66), value="1u", footprint=FP_C,
            fields={"MPN": "CL10B105KO8NNNC", "Manufacturer": "Samsung",
                    "Supplier": "LCSC", "Price": "3", "Alt": "любой X7R 1u 16V",
                    "Datasheet": "https://product.samsungsem.com/mlcc/CL10B105KO8NNN.do",
                    "Note": "входной, у вывода VI"})

c2 = pw.sym("C102", "Device:C", (109.22, 73.66), value="22u", footprint=FP_C,
            fields={"MPN": "CL10A226MQ8NRNC", "Manufacturer": "Samsung",
                    "Supplier": "LCSC", "Price": "8", "Alt": "любой X5R 22u 10V",
                    "Datasheet": "https://product.samsungsem.com/mlcc/CL10A226MQ8NRN.do",
                    "Note": "выходной; держит импульс тока радио на морозе"})

# Вход: VBAT -> C101 -> VI
pw.wire((48.26, Y), (68.58, Y))
pw.wire((68.58, Y), u1.pin("3"))
pw.junction((68.58, Y))
pw.wire((68.58, Y), c1.pin("1"))
pw.wire(c1.pin("2"), (68.58, 82.55))
pw.power("GND", (68.58, 82.55))

# Выход: VO -> C102 -> +3V3
pw.wire(u1.pin("2"), (109.22, Y))
pw.junction((109.22, Y))
pw.wire((109.22, Y), (129.54, Y))
pw.wire((109.22, Y), c2.pin("1"))
pw.wire(c2.pin("2"), (109.22, 82.55))
pw.power("GND", (109.22, 82.55))

pw.hlabel((129.54, Y), "+3V3", shape="output", angle=0, justify="left")

# PWR_FLAG сообщает ERC, что питание в эту цепь приходит извне (от банки),
# а не берётся из воздуха. Без него ERC считает выводы питания «не запитанными».
fl1 = pw.sym("#FLG101", "power:PWR_FLAG", (58.42, 55.88), value="PWR_FLAG",
             in_bom=False)
pw.wire(fl1.pin("1"), (58.42, Y))
pw.junction((58.42, Y))
fl2 = pw.sym("#FLG102", "power:PWR_FLAG", (78.74, 82.55), value="PWR_FLAG",
             in_bom=False)
pw.wire(fl2.pin("1"), (78.74, 86.36))
pw.wire((78.74, 86.36), (88.9, 86.36))
pw.junction((88.9, 86.36))

# Земля самого стабилизатора
pw.wire(u1.pin("1"), (88.9, 88.9))
pw.power("GND", (88.9, 88.9), angle=0)

# ---------------------------------------------------------------------------
# Лист нагрузки: здесь будет микроконтроллер, пока — эквивалент потребления.

ld.note((43.18, 88.9), [
    "Заглушка вместо микроконтроллера: R201 задаёт ток, на котором",
    "проверяется просадка стабилизатора. Реальный МК ставится сюда же,",
    "цепь +3V3 приходит выводом листа с верхнего уровня.",
])
ld.box((30.48, 50.8), (101.6, 76.2), "Эквивалент нагрузки")
ld.hlabel((48.26, Y), "+3V3", shape="input", angle=180, justify="right")
r201 = ld.sym("R201", "Device:R", (68.58, Y), value="33k", angle=90,
              footprint=FP_R,
              fields={"MPN": "RC0402FR-0733KL", "Manufacturer": "Yageo",
                      "Supplier": "LCSC", "Price": "1",
                      "Alt": "любой 1% 0402",
                      "Datasheet": "https://www.yageo.com/upload/media/product/products/datasheet/rchip/PYu-RC_Group_51_RoHS_L_12.pdf",
                      "Note": "эквивалент потребления МК, 100 мкА"})
# Резистор повёрнут на 90°, поэтому вывод 1 у него слева, а вывод 2 справа.
# Перепутать их — значит протянуть провод СКВОЗЬ корпус: KiCad соединит,
# читатель увидит пересечение. Координаты берём у символа (r201.pin("1")),
# а не на глаз — и проверяем линтером, потому что глаз этого не видит.
ld.wire((48.26, Y), r201.pin("1"))
ld.wire(r201.pin("2"), (88.9, Y))
ld.power("GND", (88.9, Y), angle=0)

# ---------------------------------------------------------------------------

problems = pr.check()
if problems:
    print("ПРОБЛЕМЫ ИЕРАРХИИ:")
    for p in problems:
        print("  -", p)
    sys.exit(1)

for f in pr.save():
    print("записано:", f)
