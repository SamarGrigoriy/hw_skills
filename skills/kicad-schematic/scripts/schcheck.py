#!/usr/bin/env python3
"""
schcheck.py — линтер читаемости схемы поверх ERC.

ERC проверяет электрику: не забыт ли вывод, не столкнулись ли два выхода.
Он ничего не говорит о том, можно ли схему прочитать. А читают её люди —
и тот, кто будет паять, и тот, кто через год станет искать, почему устройство
перезагружается. Здесь проверяется именно это.

Две группы проверок.

Топология: провод сквозь чужой вывод (KiCad считает это соединением, человек —
нет), пересечение проводов без точки (человек считает соединением, KiCad — нет),
висящие метки, провода вне сетки и нулевой длины, пустые поля закупки.

Геометрия — то, из-за чего схема превращается в кашу, хотя ERC чист:
надпись на надписи, на проводе, на корпусе или выводе чужого символа; элемент,
вылезающий из рамки своего блока; пересекающиеся рамки; залезание в штамп и за
рамку чертежа; символы, которых нет в шрифте KiCad («₽» печатается квадратом);
дубликаты элементов в одной точке.

Геометрию текста линтер берёт у САМОГО KiCad: лист экспортируется в SVG, где
каждая надпись лежит группой штрихов со своим текстом. Модель шрифта (kfont.py)
используется только если kicad-cli недоступен — тогда проверка приблизительна,
о чём линтер предупреждает. Раньше этой группы проверок не было, и лист с
подписями поверх проводов проходил с «0 ошибок»; ловушка в selftest.py следит,
чтобы так больше не случилось.

Использование:
    python3 schcheck.py файл.kicad_sch [...]     проверить листы
    python3 schcheck.py --strict ...             предупреждения = ошибки
    python3 schcheck.py --no-render ...          без kicad-cli (приблизительно)
"""

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kfont   # noqa: E402
import sexp    # noqa: E402
from schlib import (GRID, geometry_from_tree, symbol_shapes, page_zones, xform,  # noqa: E402
                    rect_overlap, rect_inside, rect_inflate, label_box,
                    frames_conflicts, label_ambiguous, rect_dist)

LONG_WIRE_MM = 76.2      # 60 клеток: длиннее — просится метка вместо провода
PEN = 0.08               # половина толщины штриха текста, мм
OVERLAP_MIN = 0.15       # пересечение надписей меньше этого по любой оси — касание
TEXT_GAP = 0.5           # надписи ближе этого читаются слитно («+3V3R202»)


class Report:
    def __init__(self):
        self.errors = []
        self.warns = []
        self.notes = []

    def err(self, where, msg):
        if (where, msg) not in self.errors:
            self.errors.append((where, msg))

    def warn(self, where, msg):
        if (where, msg) not in self.warns:
            self.warns.append((where, msg))

    def note(self, where, msg):
        self.notes.append((where, msg))

    def dump(self, strict=False):
        for tag, items in (("ОШИБКА", self.errors), ("ПРЕДУПРЕЖДЕНИЕ", self.warns),
                           ("ЗАМЕЧАНИЕ", self.notes)):
            for where, msg in items:
                print(f"{tag}: {where}: {msg}")
        bad = len(self.errors) + (len(self.warns) if strict else 0)
        print(f"\nитого: ошибок {len(self.errors)}, предупреждений {len(self.warns)}, "
              f"замечаний {len(self.notes)}")
        return 1 if bad else 0


# ---------------------------------------------------------------------------
# Разбор листа

def _xy(node):
    a = sexp.get(node, "at")
    return (float(a[1]), float(a[2])) if a else None


def _on_grid(v):
    return abs(v / GRID - round(v / GRID)) < 1e-6


def _wire_pts(w):
    pts = sexp.get(w, "pts")
    out = []
    for x in pts[1:]:
        if isinstance(x, list) and x[0] == "xy":
            out.append((float(x[1]), float(x[2])))
    return out


def _hidden(prop):
    if sexp.get(prop, "hide") and sexp.val(prop, "hide", "yes") != "no":
        return True
    eff = sexp.get(prop, "effects")
    return bool(eff and ("hide" in eff or (sexp.get(eff, "hide")
                                          and sexp.val(eff, "hide", "yes") != "no")))


def _justify(node):
    eff = sexp.get(node, "effects")
    j = sexp.get(eff, "justify") if eff else None
    if not j:
        return None
    for v in j[1:]:
        if v in ("left", "right"):
            return v
    return None


def _size(node, default=1.27):
    eff = sexp.get(node, "effects")
    f = sexp.get(eff, "font") if eff else None
    s = sexp.get(f, "size") if f else None
    return float(s[1]) if s else default


def load_sheet(path):
    """Всё, что нужно проверкам, из одного .kicad_sch."""
    tree = sexp.load(path)
    lib, units = {}, {}
    ls = sexp.get(tree, "lib_symbols")
    for sym in (ls[1:] if ls else []):
        if isinstance(sym, list) and sym and sym[0] == "symbol":
            lib[sym[1]] = sym
            units[sym[1]] = {int(m.group(1)) for c in sym if isinstance(c, list) and c
                             and c[0] == "symbol" for m in [re.search(r"_(\d+)_\d+$", c[1])]
                             if m and m.group(1) != "0"}
    geos = {}

    def geo_of(lib_id, unit):
        # Геометрия берётся у СВОЕГО юнита: у U1C выводы 5 и 6, а не 1 и 2.
        if lib_id not in lib:
            return None
        if (lib_id, unit) not in geos:
            geos[(lib_id, unit)] = geometry_from_tree(lib[lib_id], unit)
        return geos[(lib_id, unit)]
    d = dict(paper=sexp.val(tree, "paper", "A4"), symbols=[], wires=[], juncs=set(),
             frames=[], labels=[], texts=[], tree=tree)
    for node in tree[1:]:
        if not isinstance(node, list) or not node:
            continue
        tag = node[0]
        if tag == "symbol" and sexp.get(node, "lib_id"):
            at = sexp.get(node, "at")
            props = {}
            for p in node:
                if isinstance(p, list) and p and p[0] == "property" and len(p) > 2:
                    pa = sexp.get(p, "at")
                    props[p[1]] = dict(value=p[2], hidden=_hidden(p), justify=_justify(p),
                                       at=(float(pa[1]), float(pa[2])) if pa else None,
                                       angle=float(pa[3]) if pa and len(pa) > 3 else 0.0,
                                       size=_size(p))
            lib_id = sexp.val(node, "lib_id")
            unit = int(sexp.val(node, "unit", "1"))
            # Часть многоюнитного корпуса KiCad печатает с буквой: «U1C». Под этим
            # именем подпись и ищется среди надписей SVG, и называется в отчёте.
            if len(units.get(lib_id, ())) > 1 and "Reference" in props:
                props["Reference"]["value"] += chr(ord("A") + unit - 1)
            d["symbols"].append(dict(
                lib_id=lib_id, at=(float(at[1]), float(at[2])),
                angle=float(at[3]) if len(at) > 3 else 0.0,
                mirror=sexp.val(node, "mirror"), props=props, unit=unit,
                ref=props.get("Reference", {}).get("value", "?"),
                geo=geo_of(lib_id, unit), on_board=sexp.val(node, "on_board") != "no",
                in_bom=sexp.val(node, "in_bom") != "no"))
        elif tag == "wire":
            pts = _wire_pts(node)
            for i in range(len(pts) - 1):
                d["wires"].append((pts[i], pts[i + 1]))
        elif tag == "junction":
            d["juncs"].add(_xy(node))
        elif tag == "rectangle":
            a, b = sexp.get(node, "start"), sexp.get(node, "end")
            x0, y0, x1, y1 = float(a[1]), float(a[2]), float(b[1]), float(b[2])
            d["frames"].append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        elif tag in ("label", "global_label", "hierarchical_label"):
            at = sexp.get(node, "at")
            d["labels"].append(dict(kind=tag, text=node[1], at=(float(at[1]), float(at[2])),
                                    angle=float(at[3]) if len(at) > 3 else 0.0))
        elif tag == "text":
            at = sexp.get(node, "at")
            d["texts"].append(dict(text=node[1], at=(float(at[1]), float(at[2])),
                                   angle=float(at[3]) if len(at) > 3 else 0.0,
                                   size=_size(node), justify=_justify(node) or "left"))
    return d


# ---------------------------------------------------------------------------
# Геометрия

def _owners(d, items):
    """Кому принадлежит каждая надпись: полю символа, пояснению или метке.

    Сопоставление по тексту и близости к точке привязки. Нужно, чтобы отличать
    строки одного блока (ссылка и номинал одной детали, строки одного абзаца)
    от подписей РАЗНЫХ деталей, случайно вставших столбиком: «Y401 / 26MHz /
    C414 / 24p» выровнены так же, как строки одного блока, но читаются как
    одна подпись из четырёх строк.
    """
    cands = []
    for s in d["symbols"]:
        for pn, p in s["props"].items():
            if not p["hidden"] and p["at"] and pn in ("Reference", "Value"):
                cands.append((p["value"], p["at"], "sym:" + s["ref"]))
    for t in d["texts"]:
        cands.append((t["text"], t["at"], f"note:{t['at'][0]:.2f}:{t['size']}"))
    for lb in d["labels"]:
        cands.append((lb["text"], lb["at"], "label:" + lb["text"]))
    out = []
    for t, r in items:
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        best = None
        for text, (ax, ay), owner in cands:
            if text != t:
                continue
            dist = abs(ax - cx) + abs(ay - cy)
            if best is None or dist < best[0]:
                best = (dist, owner)
        out.append(best[1] if best and best[0] < 25 else None)
    return out


def _same_run(ta, ra, oa, tb, rb, ob):
    """Строки одного блока подписей или номер вывода — не слипание.

    Ссылка и номинал одной детали, строки одного пояснения — строки одного
    блока: стоят друг под другом и выровнены по краю или по центру. Номера
    выводов KiCad ставит над линией вывода вплотную к имени — это его
    оформление, а не ошибка раскладки. Надписи, не сопоставленные ни с чем
    (имена выводов, оформление символа), не проверяются на слипание.
    """
    if ta.isdigit() or tb.isdigit() or oa is None or ob is None:
        return True
    stacked = min(ra[2], rb[2]) - max(ra[0], rb[0]) > 0
    aligned = (abs(ra[0] - rb[0]) < 0.35 or abs(ra[2] - rb[2]) < 0.35
               or abs((ra[0] + ra[2]) / 2 - (rb[0] + rb[2]) / 2) < 0.35)
    return stacked and aligned and oa == ob


def seg_hits_rect(a, b, r):
    """Пересекает ли отрезок a-b внутренность прямоугольника r."""
    (x0, y0, x1, y1) = r
    if x1 <= x0 or y1 <= y0:
        return False
    # Лян-Барски
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q <= 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return t1 - t0 > 1e-9


def _cross(p1, p2, q1, q2):
    """Пересекаются ли отрезки p1-p2 и q1-q2 (касание концами не считается)."""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    return (orient(p1, p2, q1) * orient(p1, p2, q2) < 0 and
            orient(q1, q2, p1) * orient(q1, q2, p2) < 0)


def _norm_pin_text(t):
    return t.replace("~{", "").replace("}", "").replace("~", "").strip()


def svg_texts(svg_path):
    """Надписи листа в том виде, как их нарисовал KiCad: [(текст, габарит)]."""
    svg = open(svg_path, encoding="utf-8").read()
    out, seen = [], set()
    for m in re.finditer(r'<g class="stroked-text"><desc>(.*?)</desc>(.*?)</g>', svg, re.S):
        nums = [float(v) for v in re.findall(
            r"-?\d+(?:\.\d+)?", " ".join(re.findall(r'd="([^"]*)"', m.group(2))))]
        if len(nums) < 2:
            continue
        xs, ys = nums[0::2], nums[1::2]
        item = (html.unescape(m.group(1)),
                (round(min(xs) - PEN, 2), round(min(ys) - PEN, 2),
                 round(max(xs) + PEN, 2), round(max(ys) + PEN, 2)))
        # KiCad рисует часть надписей вторым проходом (фон и передний план) —
        # одинаковые группы в одной точке. Это не дубликат элемента: настоящие
        # дубликаты ловятся по самому .kicad_sch.
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def find_root(path):
    """Корневой лист проекта, в который входит данный лист."""
    d = os.path.dirname(os.path.abspath(path))
    base = os.path.basename(path)
    for f in sorted(os.listdir(d)):
        if not f.endswith(".kicad_sch"):
            continue
        p = os.path.join(d, f)
        try:
            t = open(p, encoding="utf-8").read()
        except OSError:
            continue
        if "(sheet_instances" in t and (f == base or f'"Sheetfile" "{base}"' in t):
            return p
    return os.path.abspath(path)


def render_texts(roots):
    """{имя файла листа: [(текст, габарит)]} по SVG KiCad; None без kicad-cli."""
    if not shutil.which("kicad-cli"):
        return None
    result = {}
    for root in roots:
        tmp = tempfile.mkdtemp(prefix="schcheck_svg_")
        try:
            subprocess.run(["kicad-cli", "sch", "export", "svg", "--output", tmp, root],
                           capture_output=True, timeout=300)
            for f in os.listdir(tmp):
                if not f.endswith(".svg"):
                    continue
                texts = svg_texts(os.path.join(tmp, f))
                for t, _r in texts:
                    if t.startswith("File:"):
                        result[t[5:].strip()] = texts
                        break
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return result


def model_texts(d):
    """Приблизительные габариты надписей по модели шрифта (без kicad-cli)."""
    out = []
    for s in d["symbols"]:
        for name, p in s["props"].items():
            if p["hidden"] or not p["at"] or name not in ("Reference", "Value"):
                continue
            j = p["justify"]
            dx, dy = xform(*((1, 0) if int(p["angle"]) % 180 == 0 else (0, 1)), (0, 0),
                           s["angle"], s["mirror"])
            if j in ("left", "right") and (dx < 0 or dy > 0):
                j = "right" if j == "left" else "left"
            out.append((p["value"], kfont.text_box("field", p["value"], p["at"], p["size"],
                                                   0 if dx else 90, j)))
    for lb in d["labels"]:
        kind = "label" if lb["kind"] == "label" else "glabel"
        box = label_box("label" if kind == "label" else "glabel", lb["text"], lb["at"], lb["angle"])
        if kind == "glabel":
            box = rect_inflate(box, -0.6)
        out.append((lb["text"], box))
    for t in d["texts"]:
        out.append((t["text"], kfont.text_box("text", t["text"], t["at"], t["size"], 0,
                                              t["justify"])))
    return out


def _frame_titles(d):
    """Подпись рамки — текст над её левым верхним углом."""
    names = []
    for f in d["frames"]:
        title = None
        for t in d["texts"]:
            if abs(t["at"][0] - f[0]) < 3.2 and -4.5 < t["at"][1] - f[1] < 1.0:
                title = t["text"]
        names.append(title or f"({f[0]:.1f}; {f[1]:.1f})")
    return names


def check_geometry(path, rep, texts):
    name = os.path.basename(path)
    d = load_sheet(path)
    border, tblock = page_zones(d["paper"])
    frames = d["frames"]
    ftitle = _frame_titles(d)
    at = lambda r: f"({(r[0] + r[2]) / 2:.1f}; {(r[1] + r[3]) / 2:.1f}) мм"   # noqa: E731

    # --- глифы -------------------------------------------------------------
    strings = [t["text"] for t in d["texts"]] + [lb["text"] for lb in d["labels"]]
    for s in d["symbols"]:
        strings += [p["value"] for n, p in s["props"].items()
                    if not p["hidden"] and n in ("Reference", "Value")]
    for st in strings:
        bad = kfont.unsupported_chars(st)
        if bad:
            rep.err(name, f"в надписи «{st}» символы {''.join(bad)} отсутствуют в шрифте "
                          f"KiCad и печатаются квадратом — заменить (₽ -> руб., № -> N)")

    # --- перевёрнутые надписи -------------------------------------------------
    # Угол поля 180/270 KiCad рисует вверх ногами; у пояснений — так же.
    for s in d["symbols"]:
        for pn, p in s["props"].items():
            if not p["hidden"] and int(p["angle"]) % 360 in (180, 270):
                rep.err(name, f"{s['ref']}: подпись {pn} «{p['value']}» с углом "
                              f"{int(p['angle'])} — KiCad рисует её вверх ногами")
    for t in d["texts"]:
        if int(t.get("angle", 0)) % 360 in (180, 270):
            rep.err(name, f"пояснение «{t['text'][:40]}» перевёрнуто (угол {t['angle']})")

    # --- дубликаты -----------------------------------------------------------
    seen = {}
    for s in d["symbols"]:
        k = (s["lib_id"], s["at"], s["angle"])
        if k in seen:
            rep.err(name, f"{s['ref']} и {seen[k]} ({s['lib_id']}) стоят в одной точке "
                          f"{s['at']} — дубликат, на листе не виден")
        seen[k] = s["ref"]
    wseen = set()
    for a, b in d["wires"]:
        k = tuple(sorted((a, b)))
        if k in wseen:
            rep.err(name, f"провод {a}->{b} продублирован")
        wseen.add(k)

    # --- надписи: исключаем оформление листа (штамп, номера зон) -----------
    if texts is None:
        texts = model_texts(d)
    items = []
    for t, r in texts:
        if rect_inside(r, rect_inflate(tblock, 0.5)):
            continue
        # Надпись штампа, вылезшая из его граф: длинный заголовок листа KiCad
        # печатает поверх рамки чертежа и за край листа. Отдельной проверкой,
        # потому что штамп из всех остальных исключён.
        if rect_overlap(r, tblock) and r[2] > tblock[2] + 0.5:
            rep.err(name, f"надпись «{t[:50]}» не помещается в штамп — "
                          f"сократить заголовок листа (title)")
            continue
        if r[2] < border[0] or r[0] > border[2] or r[3] < border[1] or r[1] > border[3]:
            continue
        items.append((t, r))

    # --- надпись x надпись -----------------------------------------------------
    owners = _owners(d, items)
    for i in range(len(items)):
        ta, ra = items[i]
        for j in range(i + 1, len(items)):
            tb, rb = items[j]
            w = min(ra[2], rb[2]) - max(ra[0], rb[0])
            h = min(ra[3], rb[3]) - max(ra[1], rb[1])
            if w > OVERLAP_MIN and h > OVERLAP_MIN:
                if not (ta == tb and all(abs(ra[k] - rb[k]) < 0.02 for k in range(4))):
                    rep.err(name, f"надписи «{ta}» и «{tb}» наложились в {at(ra)}")
            elif w > -TEXT_GAP and h > -TEXT_GAP and (w > 0.3 or h > 0.3) \
                    and not _same_run(ta, ra, owners[i], tb, rb, owners[j]):
                rep.err(name, f"надписи «{ta}» и «{tb}» стоят вплотную в {at(ra)} — "
                              f"читаются слитно, нужен зазор {TEXT_GAP} мм")

    # --- надпись x провод ------------------------------------------------------
    for t, r in items:
        rr = rect_inflate(r, -0.12)
        for a, b in d["wires"]:
            if seg_hits_rect(a, b, rr):
                rep.err(name, f"провод {a}->{b} проходит по надписи «{t}» в {at(r)}")
                break

    # --- надпись x рамка -------------------------------------------------------
    for t, r in items:
        rr = rect_inflate(r, -0.05)
        for k, f in enumerate(frames):
            edges = (((f[0], f[1]), (f[2], f[1])), ((f[2], f[1]), (f[2], f[3])),
                     ((f[0], f[3]), (f[2], f[3])), ((f[0], f[1]), (f[0], f[3])))
            if any(seg_hits_rect(a, b, rr) for a, b in edges):
                rep.err(name, f"надпись «{t}» в {at(r)} пересекает рамку «{ftitle[k]}»")

    # --- символы: корпус, выводы, рамки, лист -------------------------------
    shapes = []
    for s in d["symbols"]:
        geo = s["geo"]
        if not geo:
            continue
        body, pins = symbol_shapes(s["lib_id"], s["at"], s["angle"], s["mirror"], geo)
        allowed = {_norm_pin_text(p[1]) for p in geo["pins"]} | \
                  {p[0] for p in geo["pins"]} | set(geo["texts"])
        full = body or (s["at"][0], s["at"][1], s["at"][0], s["at"][1])
        for _n, _nm, a, b in pins:
            full = (min(full[0], a[0], b[0]), min(full[1], a[1], b[1]),
                    max(full[2], a[0], b[0]), max(full[3], a[1], b[1]))
        shapes.append((s, body, pins, full))
        for t, r in items:
            if _norm_pin_text(t) in allowed:
                continue
            rr = rect_inflate(r, -0.1)
            if body and rect_overlap(rr, body) > 0.02:
                rep.err(name, f"надпись «{t}» лежит на корпусе {s['ref']} в {at(r)}")
                continue
            for _n, _nm, a, b in pins:
                if seg_hits_rect(a, b, rr):
                    rep.err(name, f"надпись «{t}» лежит на выводе {s['ref']}.{_n} в {at(r)}")
                    break

        ref = s["ref"]
        if not rect_inside(full, border):
            rep.err(name, f"{ref}: выходит за рамку чертежа ({full[0]:.1f}; {full[1]:.1f})")
        if rect_overlap(full, tblock) > 0:
            rep.err(name, f"{ref}: заходит в штамп")
        for k, f in enumerate(frames):
            if rect_overlap(full, f) > 0 and not rect_inside(full, f):
                rep.err(name, f"{ref}: выходит за рамку «{ftitle[k]}» — "
                              f"символ ({full[0]:.1f}; {full[1]:.1f})-({full[2]:.1f}; {full[3]:.1f})")
            if rect_inside(full, f):
                for pn, p in s["props"].items():
                    if pn in ("Reference", "Value") and not p["hidden"] and p["at"] \
                            and not (f[0] <= p["at"][0] <= f[2] and f[1] <= p["at"][1] <= f[3]):
                        rep.err(name, f"{ref}: подпись {pn} «{p['value']}» вынесена за "
                                      f"рамку «{ftitle[k]}»")

    # --- подпись отделена от своей детали проводом ----------------------------
    # Подпись по ту сторону провода читается как подпись соседа.
    sym_by_ref = {sh[0]["ref"]: sh for sh in shapes}
    for (t, r), owner in zip(items, owners):
        if not owner or not owner.startswith("sym:"):
            continue
        sh_ = sym_by_ref.get(owner[4:])
        if not sh_ or sh_[0]["lib_id"].startswith("power:"):
            continue
        s_, body, pins, full = sh_
        c0 = ((full[0] + full[2]) / 2, (full[1] + full[3]) / 2)
        c1 = ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
        # Свой — только провод, продолжающий вывод наружу по его оси: шина,
        # к которой деталь подключена сбоку, подпись от детали отделяет.
        own = []
        for _n, _nm, q, e in pins:
            own.append((q, (q[0] - e[0], q[1] - e[1])))

        def is_own(a, b):
            for q, (dx, dy) in own:
                for p0, p1 in ((a, b), (b, a)):
                    if p0 == q:
                        vx, vy = p1[0] - q[0], p1[1] - q[1]
                        if abs(vx * dy - vy * dx) < 1e-6 and vx * dx + vy * dy > 0:
                            return True
            return False

        for a, b in d["wires"]:
            if is_own(a, b):
                continue
            if _cross(c0, c1, a, b):
                rep.err(name, f"подпись «{t}» отделена от {s_['ref']} проводом "
                              f"{a}->{b} — читается как подпись соседа")
                break

    # --- подпись «ничья»: чужой корпус так же близко, как свой -----------------
    bodies = [(sh[0]["ref"], sh[1]) for sh in shapes
              if sh[1] and not sh[0]["lib_id"].startswith("power:")]
    for (t, r), owner in zip(items, owners):
        if not owner or not owner.startswith("sym:"):
            continue
        sh_ = sym_by_ref.get(owner[4:])
        if not sh_ or sh_[0]["lib_id"].startswith("power:"):
            continue
        s_, body, _pins, full = sh_
        # Сопоставление по тексту путает поле «3» (номинал 3 Ом) с номером
        # вывода «3» соседнего транзистора: берём надпись, только если она
        # стоит у точки привязки своего поля.
        anchors = [p["at"] for pn, p in s_["props"].items()
                   if pn in ("Reference", "Value") and p["value"] == t and p["at"]]
        if not any(rect_dist(r, (a[0], a[1], a[0], a[1])) < 1.0 for a in anchors):
            continue
        other = label_ambiguous(r, body or full,
                                [(n, b) for n, b in bodies if n != s_["ref"]])
        if other:
            rep.err(name, f"подпись «{t}» стоит между {s_['ref']} и {other} — "
                          f"неясно, чья; раздвинуть детали")

    # --- корпус x корпус -------------------------------------------------------
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            bi, bj = shapes[i][1], shapes[j][1]
            if bi and bj and rect_overlap(rect_inflate(bi, -0.1), rect_inflate(bj, -0.1)) > 0:
                rep.err(name, f"корпуса {shapes[i][0]['ref']} и {shapes[j][0]['ref']} "
                              f"наложились")

    # --- метки -------------------------------------------------------------------
    for lb in d["labels"]:
        kind = "label" if lb["kind"] == "label" else "glabel"
        box = label_box(kind, lb["text"], lb["at"], lb["angle"])
        if not rect_inside(box, border) or rect_overlap(box, tblock) > 0:
            rep.err(name, f"метка «{lb['text']}» выходит за рамку чертежа или в штамп")
        for k, f in enumerate(frames):
            if rect_overlap(box, f) > 0 and not rect_inside(box, f):
                rep.err(name, f"метка «{lb['text']}» в {lb['at']} выходит за рамку "
                              f"«{ftitle[k]}»")
        if kind == "glabel":
            inner = rect_inflate(box, -0.3)
            for a, b in d["wires"]:
                if not seg_hits_rect(a, b, inner):
                    continue
                attached = lb["at"] in (a, b)
                far = b if a == lb["at"] else a
                if attached and not (inner[0] <= far[0] <= inner[2] and
                                     inner[1] <= far[1] <= inner[3]) and \
                        not seg_hits_rect(lb["at"], far, rect_inflate(inner, -0.5)):
                    continue
                rep.err(name, f"провод {a}->{b} проходит через метку «{lb['text']}» "
                              f"в {lb['at']}")
                break

    # --- рамки -------------------------------------------------------------------
    for i, j in frames_conflicts(frames):
        rep.err(name, f"рамки «{ftitle[i]}» и «{ftitle[j]}» пересекаются")
    for k, f in enumerate(frames):
        if not rect_inside(f, border):
            rep.err(name, f"рамка «{ftitle[k]}» выходит за рамку чертежа")
        if rect_overlap(f, tblock) > 0:
            rep.err(name, f"рамка «{ftitle[k]}» заходит в штамп")

    # --- пояснения -----------------------------------------------------------------
    for t in d["texts"]:
        box = kfont.text_box("text", t["text"], t["at"], t["size"], 0, t["justify"])
        if rect_overlap(box, tblock) > 0:
            rep.err(name, f"текст «{t['text'][:40]}» заходит в штамп")
        if not rect_inside(box, border):
            rep.err(name, f"текст «{t['text'][:40]}» выходит за рамку чертежа")


# ---------------------------------------------------------------------------
# Топология и поля (проверки, которые были и раньше)

def _seg_hits_point(a, b, p, eps=1e-6):
    """Лежит ли точка p строго ВНУТРИ отрезка a-b (концы не считаются)."""
    (ax, ay), (bx, by), (px, py) = a, b, p
    if abs(ax - bx) < eps and abs(ay - by) < eps:
        return False
    if (abs(px - ax) < eps and abs(py - ay) < eps) or \
       (abs(px - bx) < eps and abs(py - by) < eps):
        return False
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > 1e-6:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    return 0 <= dot <= (bx - ax) ** 2 + (by - ay) ** 2


def check_sheet(path, rep):
    name = os.path.basename(path)
    d = load_sheet(path)

    for s in d["symbols"]:
        if not (_on_grid(s["at"][0]) and _on_grid(s["at"][1])):
            rep.err(name, f"{s['ref']}: не на сетке {GRID} мм — {s['at']}")
    for a, b in d["wires"]:
        for p in (a, b):
            if not (_on_grid(p[0]) and _on_grid(p[1])):
                rep.err(name, f"провод не на сетке {GRID} мм — точка {p}")
        if a == b:
            rep.err(name, f"провод нулевой длины в {a} — обычно это промах мимо вывода")

    for s in d["symbols"]:
        r = s["ref"]
        if r.startswith("#PWR") or r.startswith("#FLG"):
            continue
        pr = {k: v["value"] for k, v in s["props"].items()}
        if not pr.get("Footprint") and s["on_board"]:
            rep.err(name, f"{r}: не задано посадочное место — плату не развести")
        if not pr.get("Datasheet"):
            rep.note(name, f"{r}: нет ссылки на даташит")
        # Вне перечня (площадки под провод, тестовые точки) покупать нечего —
        # парт-номер у них не нужен, bomgen их тоже пропускает.
        if s["in_bom"] and not pr.get("MPN") and not pr.get("Партномер"):
            rep.warn(name, f"{r}: нет парт-номера (MPN) — BOM будет неполным")

    # Корпуса символов не касаются друг друга. Так на листе оказались два значка
    # земли вплотную (супрессор и контакт клеммника): стрелки слились в одну, и
    # не видно, что земель две. Надписи это правило не проверяют — только графику.
    bodies = []
    for s in d["symbols"]:
        if s["geo"]:
            body, _sp = symbol_shapes(s["lib_id"], s["at"], s["angle"], s["mirror"], s["geo"])
            if body:
                bodies.append((s["ref"], body))
    for i in range(len(bodies)):
        ra, (ax0, ay0, ax1, ay1) = bodies[i]
        for j in range(i + 1, len(bodies)):
            rb, (bx0, by0, bx1, by1) = bodies[j]
            gap = max(bx0 - ax1, ax0 - bx1, by0 - ay1, ay0 - by1)
            if gap < 0.25:
                what = "наложились" if gap < 0 else "стоят вплотную"
                rep.err(name, f"корпуса символов {ra} и {rb} {what} (зазор {gap:.2f} мм) — "
                              f"на печати сливаются в один значок")

    pins = []
    for s in d["symbols"]:
        if not s["geo"]:
            rep.warn(name, f"{s['ref']}: нет определения {s['lib_id']} в lib_symbols")
            continue
        _body, sp = symbol_shapes(s["lib_id"], s["at"], s["angle"], s["mirror"], s["geo"])
        pins += [(s["ref"], n, a) for n, _nm, a, _b in sp]
    for a, b in d["wires"]:
        for r, num, p in pins:
            if _seg_hits_point(a, b, p):
                rep.err(name, f"провод {a}->{b} проходит сквозь вывод {r}.{num} "
                              f"— выглядит как пересечение, а соединяет")

    segs = d["wires"]
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            (a1, b1), (a2, b2) = segs[i], segs[j]
            h1 = abs(a1[1] - b1[1]) < 1e-6
            h2 = abs(a2[1] - b2[1]) < 1e-6
            if h1 == h2:
                continue
            hor, ver = ((a1, b1), (a2, b2)) if h1 else ((a2, b2), (a1, b1))
            y, x = hor[0][1], ver[0][0]
            if min(hor[0][0], hor[1][0]) < x < max(hor[0][0], hor[1][0]) and \
               min(ver[0][1], ver[1][1]) < y < max(ver[0][1], ver[1][1]) and \
               (x, y) not in d["juncs"]:
                rep.note(name, f"провода пересекаются в ({x}, {y}) без точки "
                               f"соединения — читателю неочевидно, что связи нет")

    for a, b in segs:
        dist = abs(b[0] - a[0]) + abs(b[1] - a[1])
        if dist > LONG_WIRE_MM:
            rep.warn(name, f"провод длиной {dist:.0f} мм ({a}->{b}) — читается хуже, "
                           f"чем метка с именем цепи")

    if not d["texts"]:
        rep.warn(name, "на листе нет ни одного пояснения — читателю придётся "
                       "угадывать назначение блока")

    ends = {p for s in segs for p in s}
    for lb in d["labels"]:
        p = lb["at"]
        if not (p in ends or any(_seg_hits_point(a, b, p) for a, b in segs)):
            rep.err(name, f"метка «{lb['text']}» в {p} не лежит на проводе — "
                          f"цепь с таким именем не появится")
    return rep


def check_global_nets(paths, rep):
    """Глобальная метка, встреченная ровно на одном листе, — почти всегда опечатка.

    Смысл глобальной метки в том, чтобы связать листы. Если имя встретилось
    один раз, связи нет: цепь висит, а выглядит подключённой. Ошибка тихая —
    ERC о ней не скажет, потому что формально метка корректна. Так ловятся
    RF_MISO против RF_MSIO и +3V3 против +3.3V.
    """
    POWER = {"GND", "+3V3", "+5V", "VBAT", "VCC", "VDD", "VSS", "GNDA", "VDDA"}
    seen = {}
    for p in paths:
        for lb in load_sheet(p)["labels"]:
            if lb["kind"] == "global_label":
                seen.setdefault(lb["text"], set()).add(os.path.basename(p))
    for net, files in sorted(seen.items()):
        if net not in POWER and len(files) == 1:
            rep.warn(list(files)[0], f"глобальная метка «{net}» встречается только здесь — "
                                     f"связи с другим листом нет; проверить имя на опечатку")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    strict = "--strict" in sys.argv
    render = "--no-render" not in sys.argv
    if not args:
        print(__doc__)
        return 1
    rep = Report()
    texts = render_texts(sorted({find_root(p) for p in args})) if render else None
    if render and texts is None:
        rep.warn("*", "kicad-cli не найден: геометрия текста по модели шрифта, приблизительно")
    for p in args:
        check_sheet(p, rep)
        sheet_texts = texts.get(os.path.basename(p)) if texts else None
        if texts is not None and sheet_texts is None:
            rep.warn(os.path.basename(p), "лист не найден в SVG-экспорте — геометрия по модели")
        check_geometry(p, rep, sheet_texts)
    if len(args) > 1:
        check_global_nets(args, rep)
    return rep.dump(strict)


if __name__ == "__main__":
    sys.exit(main())
