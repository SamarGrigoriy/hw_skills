#!/usr/bin/env python3
"""
check.py — ворота этапа: одна команда, после которой схему можно считать проверенной.

Порядок: сборка (если указан генератор) -> линтер schcheck (топология и
геометрия по SVG самого KiCad) -> ERC -> PDF -> PNG каждого листа и вырезка
каждой рамки. Код возврата ненулевой, если есть хоть одна ошибка линтера или
нарушение ERC.

PNG нужны не для отчёта, а чтобы на них ПОСМОТРЕТЬ. Линтер ловит то, что
умеет, а глаз — остальное: неудачную композицию, неочевидный поток сигнала,
пояснение не на своём месте. «0 ошибок» без просмотра картинок этапа не
закрывает — именно так на листы попали подписи поверх проводов.

Использование:
    python3 check.py проект.kicad_sch [--gen gen_sch.py] [--out build] [--dpi 150]
                     [--strict] [--no-crops]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sexp                      # noqa: E402
from schlib import PAPER         # noqa: E402


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sheet_files(root):
    base = os.path.dirname(os.path.abspath(root))
    out = [os.path.abspath(root)]
    for sh in sexp.find_all(sexp.load(root), "sheet"):
        f = sexp.props(sh).get("Sheetfile")
        p = os.path.join(base, f) if f else None
        if p and os.path.exists(p) and p not in out:
            out.append(p)
    return out


def frames_of(path):
    tree = sexp.load(path)
    paper = sexp.val(tree, "paper", "A4")
    rects = []
    for node in tree[1:]:
        if isinstance(node, list) and node and node[0] == "rectangle":
            a, b = sexp.get(node, "start"), sexp.get(node, "end")
            rects.append((min(float(a[1]), float(b[1])), min(float(a[2]), float(b[2])),
                          max(float(a[1]), float(b[1])), max(float(a[2]), float(b[2]))))
    return paper, rects


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--gen", help="скрипт сборки схемы (запускается первым)")
    ap.add_argument("--out", default=None, help="каталог для PDF/PNG (по умолчанию build/ рядом со схемой)")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--strict", action="store_true", help="предупреждения линтера = ошибки")
    ap.add_argument("--no-crops", action="store_true")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    proj = os.path.dirname(root)
    out = os.path.abspath(a.out or os.path.join(proj, "build"))
    os.makedirs(out, exist_ok=True)
    failed = []

    if a.gen:
        print(f"== сборка: {a.gen}")
        r = run([sys.executable, os.path.abspath(a.gen)], cwd=proj)
        sys.stdout.write(r.stdout + r.stderr)
        if r.returncode:
            print("СБОРКА НЕ ПРОШЛА")
            return 2
        if "ПРЕДУПРЕЖДЕНИЕ" in r.stdout:
            failed.append("сборка: предупреждения генератора")

    sheets = sheet_files(root)
    print(f"== линтер: {len(sheets)} листов")
    cmd = [sys.executable, os.path.join(HERE, "schcheck.py")] + (["--strict"] if a.strict else []) + sheets
    r = run(cmd, cwd=proj)
    lines = [ln for ln in r.stdout.splitlines() if not ln.startswith("ЗАМЕЧАНИЕ")]
    print("\n".join(lines))
    if r.returncode:
        failed.append("линтер")

    print("== ERC")
    rpt = os.path.join(out, "erc.rpt")
    r = run(["kicad-cli", "sch", "erc", "--severity-all", "--output", rpt, root],
            cwd=proj, timeout=600)
    n_erc = -1
    try:
        text = open(rpt, encoding="utf-8").read()
        m = re.search(r"ERC messages:\s*(\d+)", text)
        n_erc = int(m.group(1)) if m else -1
        sheet = ""
        for ln in text.splitlines():
            if ln.startswith("***** Sheet"):
                sheet = ln[len("***** Sheet"):].strip()
            elif ln.startswith("["):
                print(f"ERC {sheet}: {ln}")
            elif ln.strip().startswith("@("):
                print("    " + ln.strip())
    except OSError:
        print("ERC: отчёт не получен\n" + r.stdout + r.stderr)
    print(f"ERC нарушений: {n_erc}")
    if n_erc:
        failed.append("ERC")

    print("== картинки")
    pdf = os.path.join(out, "sch.pdf")
    run(["kicad-cli", "sch", "export", "pdf", "--output", pdf, root], cwd=proj, timeout=600)
    tmp = tempfile.mkdtemp(prefix="check_png_")
    try:
        run(["pdftoppm", "-r", str(a.dpi), "-png", pdf, os.path.join(tmp, "p")])
        pages = sorted(os.listdir(tmp), key=lambda f: int(re.findall(r"\d+", f)[-1]))
        by_file = {}
        for i, f in enumerate(pages, 1):
            txt = run(["pdftotext", "-f", str(i), "-l", str(i), pdf, "-"]).stdout
            m = re.search(r"File:\s*(\S+\.kicad_sch)", txt)
            name = m.group(1) if m else f"page{i}.kicad_sch"
            dst = os.path.join(out, name.replace(".kicad_sch", ".png"))
            shutil.move(os.path.join(tmp, f), dst)
            by_file[name] = dst
            print("  лист:", dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not a.no_crops:
        try:
            from PIL import Image
        except ImportError:
            Image = None
            print("  (PIL нет — вырезки рамок пропущены)")
        if Image:
            k = a.dpi / 25.4
            for sh in sheets:
                name = os.path.basename(sh)
                png = by_file.get(name)
                if not png:
                    continue
                paper, rects = frames_of(sh)
                img = Image.open(png)
                W, _H = PAPER.get(paper, PAPER["A4"])
                s = img.width / W / k if W else 1
                for n, (x0, y0, x1, y1) in enumerate(rects, 1):
                    box = [int((x0 - 6) * k * s), int((y0 - 6) * k * s),
                           int((x1 + 6) * k * s), int((y1 + 6) * k * s)]
                    box = [max(0, box[0]), max(0, box[1]), min(img.width, box[2]), min(img.height, box[3])]
                    dst = png.replace(".png", f"-frame{n}.png")
                    img.crop(box).save(dst)
                    print("  рамка:", dst)

    print()
    if failed:
        print("ВОРОТА НЕ ПРОЙДЕНЫ:", ", ".join(failed))
        print("Исправить и повторить. После прохождения — открыть и просмотреть PNG каждого листа и рамки.")
        return 1
    print("ВОРОТА ПРОЙДЕНЫ автоматически. Теперь ОБЯЗАТЕЛЬНО просмотреть PNG каждого листа и рамки.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
