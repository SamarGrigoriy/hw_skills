#!/usr/bin/env python3
"""
view.py — картинка слоёв платы для просмотра между прогонами трассировщика.

check.py рисует всё сразу и долго (DRC, заливка, 3D). Между итерациями
размещения и трассировки нужно быстро посмотреть одно место: вокруг МК, в
ВЧ-зоне, у разъёма. view.py выгружает PDF нужных слоёв через kicad-cli и режет
PNG по прямоугольнику в миллиметрах платы. Координаты PDF совпадают с
координатами платы один к одному (проверено на KiCad 10.0.4), поэтому вырезка
точная.

Использование:
    python3 view.py плата.kicad_pcb out.png [--layers F.Cu,B.Cu,Edge.Cuts] [--rect x0 y0 x1 y1] [--dpi 300]
"""

import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcblib  # noqa: E402


def render(board, out, layers="F.Cu,B.Cu,Edge.Cuts", rect=None, dpi=300):
    from PIL import Image
    tmp = tempfile.mkdtemp(prefix="pcbview_")
    pdf = os.path.join(tmp, "v.pdf")
    subprocess.run(["kicad-cli", "pcb", "export", "pdf", "--mode-single", "--layers", layers,
                    "--drill-shape-opt", "2", "-o", pdf, board], capture_output=True, text=True, timeout=600)
    stem = os.path.join(tmp, "v")
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf, stem], capture_output=True, timeout=600)
    im = Image.open(stem + ".png").convert("RGB")
    if rect is None:
        op = pcblib.Board.read(board).outline_polygon()
        x0, y0, x1, y1 = op.bounds
        rect = (x0 - 2, y0 - 2, x1 + 2, y1 + 2)
    k = dpi / 25.4
    im.crop(tuple(int(v * k) for v in rect)).save(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("out")
    ap.add_argument("--layers", default="F.Cu,B.Cu,Edge.Cuts")
    ap.add_argument("--rect", nargs=4, type=float)
    ap.add_argument("--dpi", type=int, default=300)
    a = ap.parse_args()
    print(render(a.board, a.out, a.layers, a.rect, a.dpi))
    return 0


if __name__ == "__main__":
    sys.exit(main())
