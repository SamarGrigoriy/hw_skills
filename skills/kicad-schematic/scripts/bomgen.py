#!/usr/bin/env python3
"""
bomgen.py — перечень элементов из схемы, с полями закупки и суммой.

Штатный `kicad-cli sch export bom` даёт ссылку, номинал, посадочное место и
количество. Для заказа этого мало: нужны парт-номер, поставщик, цена и
взаимозаменяемые аналоги — иначе перед каждой закупкой BOM собирается заново
руками, а сумма считается в уме.

Источник истины — сама схема. Поля MPN, Manufacturer, Supplier, Price, Alt,
Note кладутся в свойства символа при генерации, поэтому BOM и схема не могут
разойтись: не бывает «в BOM одно, на плате другое».

Символы с dnp=yes попадают в перечень отдельной группой «не монтировать»:
на плате место есть, в закупку они не идут. Для проекта с одной платой на две
роли это половина смысла BOM.

Использование:
    python3 bomgen.py проект.kicad_sch [-o bom.md] [--csv bom.csv] [--qty 30]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sexp

# URL — страница товара у поставщика: из перечня по ней сразу кладут в корзину.
FIELDS = ["MPN", "Manufacturer", "Supplier", "Price", "Alt", "Note", "URL"]


def sheets_of(root_path):
    """Корневой лист плюс все дочерние (по свойству Sheetfile)."""
    base = os.path.dirname(os.path.abspath(root_path))
    out = [root_path]
    tree = sexp.load(root_path)
    for sh in sexp.find_all(tree, "sheet"):
        pr = sexp.props(sh)
        f = pr.get("Sheetfile")
        if f:
            p = os.path.join(base, f)
            if os.path.exists(p) and p not in out:
                out.append(p)
    return out


def collect(root_path):
    items = []
    seen = set()
    for path in sheets_of(root_path):
        tree = sexp.load(path)
        for s in sexp.find_all(tree, "symbol"):
            if not sexp.get(s, "lib_id"):
                continue
            pr = sexp.props(s)
            ref = pr.get("Reference", "")
            if ref.startswith("#PWR") or ref.startswith("#FLG"):
                continue
            if sexp.val(s, "in_bom") == "no":
                continue
            # Семь частей 40106 на листах — один корпус в закупке.
            if ref in seen:
                continue
            seen.add(ref)
            items.append({
                "ref": ref,
                "value": pr.get("Value", ""),
                "fp": pr.get("Footprint", ""),
                "dnp": sexp.val(s, "dnp") == "yes",
                "sheet": os.path.basename(path),
                **{f: pr.get(f, "") for f in FIELDS},
            })
    return items


def _refkey(r):
    m = re.match(r"([A-Za-z#]+)(\d+)", r)
    return (m.group(1), int(m.group(2))) if m else (r, 0)


def group(items):
    """Одинаковые детали в одну строку. Ключ — то, что реально заказывается."""
    out = {}
    for it in items:
        key = (it["value"], it["fp"], it["MPN"], it["dnp"])
        g = out.setdefault(key, {**it, "refs": [], "notes": []})
        g["refs"].append(it["ref"])
        # Примечание у каждой детали своё («подтяжка NRST», «BOOT0 на землю»):
        # взять примечание первой детали на всю группу — значит соврать про остальные.
        if it["Note"]:
            g["notes"].append((it["ref"], it["Note"]))
    rows = []
    for g in out.values():
        g["refs"].sort(key=_refkey)
        g["qty"] = len(g["refs"])
        rows.append(g)
    rows.sort(key=lambda g: (g["dnp"], -_price(g) * g["qty"], _refkey(g["refs"][0])))
    return rows


def _price(g):
    try:
        return float(str(g.get("Price", "")).replace(",", ".").split()[0])
    except Exception:
        return 0.0


def _money(x):
    """Целые рубли без копеек, дробные — с копейками: резистор за 0,25 руб.
    не должен превращаться в «0», а сумма из таких — терять половину."""
    return f"{x:.0f}" if abs(x - round(x)) < 1e-9 else f"{x:.2f}"


def _mpn(r):
    if r["MPN"] and r.get("URL"):
        return f"[{r['MPN']}]({r['URL']})"
    return r["MPN"] or "—"


def to_markdown(rows, batch=1, title="Перечень элементов"):
    mounted = [r for r in rows if not r["dnp"]]
    dnp = [r for r in rows if r["dnp"]]
    total = sum(_price(r) * r["qty"] for r in mounted)
    lines = [f"# {title}", ""]
    if any(_price(r) == 0 for r in mounted):
        lines += ["> У части позиций не заполнено поле Price — сумма ниже неполная.", ""]
    lines += ["| № | Позиция | Номинал / тип | Корпус | Кол-во | Цена, ₽ | Сумма, ₽ | Парт-номер | Поставщик | Аналоги |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for n, r in enumerate(mounted, 1):
        p = _price(r)
        lines.append(
            f"| {n} | {', '.join(r['refs'])} | {r['value']} | {_short_fp(r['fp'])} | "
            f"{r['qty']} | {_money(p)} | {_money(p * r['qty'])} | {_mpn(r)} | "
            f"{r['Supplier'] or '—'} | {r['Alt'] or '—'} |")
    lines += ["", f"**Итого на одно изделие: {_money(total)} ₽**"]
    if batch > 1:
        lines.append(f"**На партию {batch} шт: {_money(total * batch)} ₽**")
    if dnp:
        lines += ["", "## Не монтируется (место на плате есть, в закупку не идёт)", "",
                  "| Позиция | Номинал / тип | Корпус | Кол-во | Назначение |",
                  "|---|---|---|---|---|"]
        for r in dnp:
            texts = list(dict.fromkeys(t for _r, t in r.get("notes", []))) or ["—"]
            lines.append(f"| {', '.join(r['refs'])} | {r['value']} | "
                         f"{_short_fp(r['fp'])} | {r['qty']} | {'; '.join(texts)} |")
    notes = [r for r in rows if r.get("notes") and not r["dnp"]]
    if notes:
        lines += ["", "## Примечания", ""]
        for r in notes:
            by_text = {}
            for ref, text in r["notes"]:
                by_text.setdefault(text, []).append(ref)
            for text, refs in by_text.items():
                refs.sort(key=_refkey)
                lines.append(f"- **{', '.join(refs)}** — {text}")
    return "\n".join(lines) + "\n"


def _short_fp(fp):
    return fp.split(":")[-1] if fp else "—"


def to_csv(rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Refs", "Value", "Footprint", "Qty", "Price", "Sum",
                "MPN", "Manufacturer", "Supplier", "Alt", "DNP", "Note", "URL"])
    for r in rows:
        p = _price(r)
        w.writerow([" ".join(r["refs"]), r["value"], r["fp"], r["qty"],
                    r.get("Price", ""), f"{p * r['qty']:.2f}" if p else "",
                    r["MPN"], r["Manufacturer"], r["Supplier"], r["Alt"],
                    "DNP" if r["dnp"] else "", r["Note"], r.get("URL", "")])
    return buf.getvalue()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    out = None
    csv_out = None
    batch = 1
    for i, a in enumerate(sys.argv):
        if a == "-o" and i + 1 < len(sys.argv):
            out = sys.argv[i + 1]
        if a == "--csv" and i + 1 < len(sys.argv):
            csv_out = sys.argv[i + 1]
        if a == "--qty" and i + 1 < len(sys.argv):
            batch = int(sys.argv[i + 1])
    rows = group(collect(src))
    md = to_markdown(rows, batch)
    if out:
        open(out, "w", encoding="utf-8").write(md)
        print(f"записано: {out}")
    else:
        print(md)
    if csv_out:
        open(csv_out, "w", encoding="utf-8").write(to_csv(rows))
        print(f"записано: {csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
