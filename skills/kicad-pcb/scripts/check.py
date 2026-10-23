#!/usr/bin/env python3
"""
check.py — ворота этапа разводки: одна команда, после которой этап можно
считать проверенным автоматически.

Порядок: сборка (gen_pcb.py) -> заливка зон и DRC со сверкой со схемой
(kicad-cli) -> линтер ЭМС, монтажа и корпуса (emccheck.py) -> картинки для
просмотра: PNG слоёв по контуру платы, вырезки зон из описания и мест ошибок,
3D-рендеры. Код возврата ненулевой, если ворота не пройдены.

Картинки нужны не для отчёта, а чтобы на них ПОСМОТРЕТЬ. Линтер ловит известные
классы дефектов, глаз — остальное: неудачный поток сигнала, тесноту, дорожку,
огибающую полплаты. «0 ошибок» без просмотра картинок этап не закрывает.

Стадии (reference/tools.md):
  outline   — этап 3: контур, крепёж, неподвижные детали; неразведённые связи не ошибка
  placement — этап 4: размещение; неразведённые связи не ошибка
  critical  — этап 5: критичные цепи; неразведённые связи не ошибка
  full      — этапы 6–8: всё разведено, любое нарушение DRC — ошибка

Файл платы — результат сборки: DRC запускается с --refill-zones --save-board,
и KiCad записывает в него залитые полигоны, которые затем читает линтер.

Использование:
    python3 check.py плата.kicad_pcb --gen gen_pcb.py --stage placement [--out build] [--no-3d]
"""

import argparse
import json
import re
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcblib  # noqa: E402

DPI = 300
MM = DPI / 25.4

# Нарушения, которые до полной трассировки ожидаемы: связи ещё не проложены.
PENDING_UNTIL_FULL = {"unconnected_items", "track_dangling", "via_dangling",
                      "isolated_copper", "zones_intersect"}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


REF_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z]{1,3}[0-9]{1,4})(?![A-Za-z0-9_])")


def drc(board, out, stage, unplaced=()):
    rpt = os.path.join(out, "drc.json")
    r = run(["kicad-cli", "pcb", "drc", "--refill-zones", "--save-board", "--schematic-parity",
             "--severity-all", "--format", "json", "-o", rpt, board], timeout=900)
    if not os.path.exists(rpt):
        print(r.stdout + r.stderr)
        return None, ["DRC не выполнен"]
    d = json.load(open(rpt, encoding="utf-8"))
    errors, warnings, pending = [], [], []

    def item_text(v):
        items = "; ".join(i.get("description", "") for i in v.get("items", [])[:2])
        return f"{v.get('type')}: {v.get('description')} — {items}"

    unplaced = set(unplaced)

    def only_unplaced(v):
        refs = set()
        for i in v.get("items", []):
            refs |= set(REF_RE.findall(i.get("description", "")))
        return bool(refs) and refs <= unplaced

    for v in d.get("violations", []):
        t = v.get("type")
        if unplaced and only_unplaced(v):
            pending.append(v)
        elif t in PENDING_UNTIL_FULL and stage != "full":
            pending.append(v)
        elif v.get("severity") == "error":
            errors.append(item_text(v))
        else:
            warnings.append(item_text(v))
    for v in d.get("schematic_parity", []):
        errors.append("сверка со схемой — " + item_text(v))
    unconnected = d.get("unconnected_items", [])
    if stage == "full":
        errors += ["не разведено — " + item_text(v) for v in unconnected]
    else:
        pending += unconnected
    return dict(errors=errors, warnings=warnings, pending=len(pending)), None


def plot_layers(board, out, bounds, views):
    """PDF слоя -> PNG, обрезанный по контуру платы с полем 3 мм."""
    from PIL import Image
    x0, y0, x1, y1 = bounds
    box = (int((x0 - 3) * MM), int((y0 - 3) * MM), int((x1 + 3) * MM), int((y1 + 3) * MM))
    made = []
    for name, layers in views:
        pdf = os.path.join(out, f"{name}.pdf")
        r = run(["kicad-cli", "pcb", "export", "pdf", "--mode-single", "--layers", layers,
                 "--drill-shape-opt", "2", "-o", pdf, board], timeout=600)
        if not os.path.exists(pdf):
            print(f"  ! {name}: PDF не создан: {r.stderr.strip()[:200]}")
            continue
        stem = os.path.join(out, f"_{name}")
        run(["pdftoppm", "-r", str(DPI), "-png", "-singlefile", pdf, stem], timeout=600)
        png = stem + ".png"
        im = Image.open(png).convert("RGB").crop(box)
        dst = os.path.join(out, f"{name}.png")
        im.save(dst)
        os.remove(png)
        os.remove(pdf)
        made.append(dst)
    return made


def crops(out, bounds, regions, source="top"):
    """Вырезки прямоугольников (мм платы) из готового PNG вида source."""
    from PIL import Image
    src = os.path.join(out, f"{source}.png")
    if not os.path.exists(src) or not regions:
        return []
    im = Image.open(src)
    x0, y0 = bounds[0] - 3, bounds[1] - 3
    made = []
    for name, (a, b, c, d) in regions:
        box = (int((a - x0) * MM), int((b - y0) * MM), int((c - x0) * MM), int((d - y0) * MM))
        dst = os.path.join(out, f"zone-{name}.png")
        im.crop(box).save(dst)
        made.append(dst)
    return made


def renders(board, out):
    made = []
    for name, args in (("3d_top", ["--side", "top"]), ("3d_bottom", ["--side", "bottom"]),
                       ("3d_front", ["--side", "front"]),
                       ("3d_iso", ["--rotate", "-45,0,45", "--perspective"])):
        dst = os.path.join(out, f"{name}.png")
        run(["kicad-cli", "pcb", "render", "--quality", "basic", "--width", "1600", "--height", "1200",
             "--background", "opaque", "-o", dst] + args + [board], timeout=900)
        if os.path.exists(dst):
            made.append(dst)
    return made


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--gen", help="скрипт сборки платы (запускается первым)")
    ap.add_argument("--stage", choices=["outline", "placement", "critical", "full"], required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-3d", action="store_true")
    a = ap.parse_args()

    board = os.path.abspath(a.board)
    proj = os.path.dirname(board)
    out = os.path.abspath(a.out or os.path.join(proj, "build"))
    os.makedirs(out, exist_ok=True)
    failed = []

    if a.gen:
        print(f"== сборка: {a.gen}")
        r = run([sys.executable, os.path.abspath(a.gen)], cwd=proj, timeout=3600)
        sys.stdout.write(r.stdout + r.stderr)
        if r.returncode:
            print("СБОРКА НЕ ПРОШЛА")
            return 2

    desc = os.path.splitext(board)[0] + ".desc.json"
    unplaced = []
    if os.path.exists(desc):
        try:
            unplaced = json.load(open(desc, encoding="utf-8")).get("unplaced", [])
        except ValueError:
            pass
    if unplaced and a.stage in ("placement", "critical", "full"):
        print(f"ОШИБКА: на стадии {a.stage} не размещены: {', '.join(unplaced)}")
        failed.append("не размещены детали")
    print("== заливка зон, DRC и сверка со схемой")
    res, err = drc(board, out, a.stage, unplaced if a.stage == "outline" else ())
    if err:
        print("\n".join(err))
        return 2
    for e in res["errors"]:
        print("ОШИБКА DRC:", e)
    for w in res["warnings"]:
        print("ПРЕДУПРЕЖДЕНИЕ DRC:", w)
    print(f"DRC: ошибок {len(res['errors'])}, предупреждений {len(res['warnings'])}, "
          f"отложено до трассировки {res['pending']}")
    if res["errors"]:
        failed.append("DRC")
    if res["warnings"] and a.stage == "full":
        failed.append("предупреждения DRC")

    regions = []
    if os.path.exists(os.path.join(HERE, "emccheck.py")):
        print("== линтер ЭМС, монтажа и корпуса")
        cmd = [sys.executable, os.path.join(HERE, "emccheck.py"), board, "--stage", a.stage,
               "--json", os.path.join(out, "emc.json")]
        if os.path.exists(desc):
            cmd += ["--desc", desc]
        r = run(cmd, timeout=1800)
        sys.stdout.write(r.stdout + r.stderr)
        if r.returncode:
            failed.append("линтер")
        try:
            rep = json.load(open(os.path.join(out, "emc.json"), encoding="utf-8"))
            for i, v in enumerate(x for x in rep.get("violations", []) if x.get("level") == "ошибка"):
                if v.get("where"):
                    cx, cy = v["where"]
                    regions.append((f"err{i + 1:02d}-{v['rule']}", (cx - 6, cy - 6, cx + 6, cy + 6)))
        except (OSError, ValueError):
            pass
    else:
        print("== линтер: emccheck.py ещё не написан — ворота не считаются пройденными")
        failed.append("нет линтера")

    b = pcblib.Board.read(board)
    op = b.outline_polygon()
    if op is None:
        print("ОШИБКА: у платы нет замкнутого контура Edge.Cuts")
        return 2
    bounds = op.bounds
    print("== картинки")
    views = [("top", "F.Cu,F.SilkS,F.Mask,Edge.Cuts"),
             ("F_Cu", "F.Cu,Edge.Cuts"),
             ("B_Cu", "B.Cu,Edge.Cuts"),
             ("assembly", "F.SilkS,F.Fab,F.CrtYd,Edge.Cuts")]
    made = plot_layers(board, out, bounds, views)
    if os.path.exists(desc):
        try:
            d = json.load(open(desc, encoding="utf-8"))
            for name, rect in d.get("views", {}).items():
                regions.append((name, tuple(rect)))
        except ValueError:
            pass
    made += crops(out, bounds, regions)
    if not a.no_3d:
        made += renders(board, out)
    for m in made:
        print("  картинка:", m)

    print()
    if failed:
        print("ВОРОТА НЕ ПРОЙДЕНЫ:", ", ".join(failed))
        print("Исправить и повторить. После прохождения — открыть и просмотреть каждую картинку.")
        return 1
    print("ВОРОТА ПРОЙДЕНЫ автоматически. Теперь ОБЯЗАТЕЛЬНО просмотреть каждую картинку:")
    print("слои, вырезки зон и 3D-рендеры.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
