#!/usr/bin/env python3
"""
pcblib.py — модель печатной платы KiCad 10: сборка .kicad_pcb из нетлиста и
чтение готовой платы для линтера и трассировщика.

Почему своё, а не pcbnew. Python-модуль pcbnew в KiCad 10.0.4 падает с
ошибкой сегментации на LoadBoard (reference/format.md), kiutils в системе нет,
а формат — S-выражения, которые читаются и пишутся за пару сотен строк. KiCad
при этом вызывается только через kicad-cli: заливка, DRC, сверка со схемой,
картинки.

Почему кавычки хранятся. Разбор из скилла схем (sexp.py) намеренно теряет
разницу между `yes` и `"yes"` — для чтения этого хватает. Для записи — нет:
KiCad различает имя слоя "F.Cu" и ключевое слово smd, и файл с перепутанными
кавычками не откроется. Поэтому здесь строка в кавычках — отдельный тип Q.

Что делает модуль:
  - read_netlist()   — компоненты и цепи из `kicad-cli sch export netlist`;
  - FootprintLib     — посадочные места из fp-lib-table проекта и библиотек KiCad;
  - Board            — контур, детали, дорожки, переходные, зоны; save() и read();
  - геометрия        — абсолютные многоугольники площадок и courtyard (shapely).

Соглашение об углах (сверено с демо-платой KiCad): координаты графики и
площадок внутри посадочного места локальные, без поворота; угол площадок и
надписей в файле платы полный — угол в библиотеке плюс поворот детали. Ось Y
направлена вниз, положительный угол — против часовой стрелки на экране.
"""

import copy
import json
import math
import os
import re
import sys
import uuid

from shapely.geometry import Point, Polygon, LineString, box as sbox
from shapely.ops import polygonize, unary_union
from shapely import affinity

KICAD_FP_DIR = "/usr/share/kicad/footprints"
BOARD_VERSION = "20260206"
NS = uuid.UUID("5b0f1c7e-2a41-4d8e-9b3a-7d51c0a1e2f4")


# =============================================================================
# S-выражения с сохранением кавычек
# =============================================================================

class Q(str):
    """Строка, которая пишется в кавычках."""
    __slots__ = ()


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def parse(text):
    """Текст -> вложенные списки. Атом без кавычек — str, в кавычках — Q."""
    pos, n = 0, len(text)
    stack = [[]]
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"разбор S-выражения: неожиданный символ в позиции {pos}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            node = stack.pop()
            stack[-1].append(node)
        elif m.group(3) is not None:
            s = m.group(3)
            if "\\" in s:
                s = re.sub(r"\\(.)", lambda mm: {"n": "\n", "t": "\t"}.get(mm.group(1), mm.group(1)), s)
            stack[-1].append(Q(s))
        elif m.group(4) is not None:
            stack[-1].append(m.group(4))
    if len(stack) != 1:
        raise ValueError("разбор S-выражения: несбалансированные скобки")
    return stack[0][0] if len(stack[0]) == 1 else stack[0]


def load_sexp(path):
    return parse(open(path, encoding="utf-8").read())


def fmt(v):
    """Число для файла: не больше 6 знаков после точки, без хвостовых нулей."""
    if isinstance(v, str):
        return v
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _atom(a):
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, (int, float)):
        return fmt(a)
    return str(a)


def dump(node, level=0):
    """Список -> текст в манере KiCad: вложенные списки с новой строки, табуляцией."""
    if not isinstance(node, list):
        return _atom(node)
    ind = "\t" * level
    heads = []
    kids = []
    for x in node:
        if isinstance(x, list):
            kids.append(x)
        elif kids:
            kids.append(x)          # атом после вложенного списка — редкость, пишем как есть
        else:
            heads.append(_atom(x))
    if not kids:
        return ind + "(" + " ".join(heads) + ")"
    # короткие списки точек (xy) пишем по нескольку в строке — файл не раздувается
    if node and node[0] == "pts":
        parts = [dump(k, 0) if isinstance(k, list) else _atom(k) for k in kids]
        lines = [" ".join(parts[i:i + 6]) for i in range(0, len(parts), 6)]
        return ind + "(pts\n" + "\n".join(ind + "\t" + ln for ln in lines) + "\n" + ind + ")"
    out = [ind + "(" + " ".join(heads)]
    for k in kids:
        out.append(dump(k, level + 1) if isinstance(k, list) else ind + "\t" + _atom(k))
    out.append(ind + ")")
    return "\n".join(out)


def kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def kid(node, name):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def num(node, i, default=0.0):
    try:
        return float(node[i])
    except (IndexError, TypeError, ValueError):
        return default


def det_uuid(*parts):
    """uuid от смысла объекта: пересборка без изменений даёт тот же файл."""
    return str(uuid.uuid5(NS, "|".join(str(p) for p in parts)))


# =============================================================================
# Геометрия
# =============================================================================

def rotate(x, y, deg):
    """Поворот точки на угол KiCad (ось Y вниз, угол против часовой на экране)."""
    if not deg:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def norm_angle(a):
    a = float(a) % 360.0
    return 0.0 if abs(a - 360.0) < 1e-9 else round(a, 6)


# =============================================================================
# Нетлист
# =============================================================================

class Comp:
    def __init__(self):
        self.ref = ""
        self.value = ""
        self.footprint = ""
        self.datasheet = ""
        self.description = ""
        self.fields = {}
        self.sheetname = ""
        self.sheetfile = ""
        self.sheet_names = "/"
        self.sheet_tstamps = "/"
        self.tstamp = ""
        self.dnp = False
        self.exclude_from_bom = False
        self.exclude_from_board = False

    @property
    def path(self):
        return self.sheet_tstamps + self.tstamp


def read_netlist(path):
    """Возвращает (компоненты {ref: Comp}, цепи {имя: [(ref, вывод, тип)]})."""
    t = load_sexp(path)
    comps, nets = {}, {}
    for c in kids(kid(t, "components") or [], "comp"):
        k = Comp()
        k.ref = str(kid(c, "ref")[1])
        k.value = str(kid(c, "value")[1]) if kid(c, "value") else ""
        k.footprint = str(kid(c, "footprint")[1]) if kid(c, "footprint") and len(kid(c, "footprint")) > 1 else ""
        k.datasheet = str(kid(c, "datasheet")[1]) if kid(c, "datasheet") and len(kid(c, "datasheet")) > 1 else ""
        for f in kids(kid(c, "fields") or [], "field"):
            name = str(kid(f, "name")[1])
            val = next((str(x) for x in f[1:] if isinstance(x, Q)), "")
            if name not in ("Footprint", "Datasheet", "Reference", "Value"):
                k.fields[name] = val
        k.description = k.fields.pop("Description", "")
        for p in kids(c, "property"):
            name = str(kid(p, "name")[1])
            val = str(kid(p, "value")[1]) if kid(p, "value") else ""
            if name == "Sheetname":
                k.sheetname = val
            elif name == "Sheetfile":
                k.sheetfile = val
            elif name == "dnp":
                k.dnp = True
            elif name == "exclude_from_bom":
                k.exclude_from_bom = True
            elif name == "exclude_from_board":
                k.exclude_from_board = True
        sp = kid(c, "sheetpath")
        if sp:
            k.sheet_names = str(kid(sp, "names")[1])
            k.sheet_tstamps = str(kid(sp, "tstamps")[1])
        ts = kid(c, "tstamps")
        k.tstamp = str(ts[1]) if ts else ""
        comps[k.ref] = k
    for n in kids(kid(t, "nets") or [], "net"):
        name = str(kid(n, "name")[1])
        nodes = []
        for nd in kids(n, "node"):
            pt = kid(nd, "pintype")
            nodes.append((str(kid(nd, "ref")[1]), str(kid(nd, "pin")[1]), str(pt[1]) if pt else ""))
        nets[name] = nodes
    return comps, nets


# =============================================================================
# Библиотеки посадочных мест
# =============================================================================

class FootprintLib:
    """Ищет посадочное место по «библиотека:имя» в fp-lib-table проекта, затем в KiCad."""

    def __init__(self, project_dir=None):
        self.dirs = {}
        self.cache = {}
        if project_dir:
            table = os.path.join(project_dir, "fp-lib-table")
            if os.path.exists(table):
                for lib in kids(load_sexp(table), "lib"):
                    name = str(kid(lib, "name")[1])
                    uri = str(kid(lib, "uri")[1]).replace("${KIPRJMOD}", project_dir)
                    self.dirs[name] = os.path.normpath(uri)

    def path(self, lib_id):
        lib, name = lib_id.split(":", 1)
        d = self.dirs.get(lib, os.path.join(KICAD_FP_DIR, lib + ".pretty"))
        return os.path.join(d, name + ".kicad_mod")

    def load(self, lib_id):
        if lib_id not in self.cache:
            p = self.path(lib_id)
            if not os.path.exists(p):
                raise FileNotFoundError(f"посадочное место {lib_id} не найдено: {p}")
            self.cache[lib_id] = load_sexp(p)
        return copy.deepcopy(self.cache[lib_id])


# =============================================================================
# Площадка и посадочное место
# =============================================================================

class Pad:
    """Площадка в абсолютных координатах платы."""

    def __init__(self, fp, node):
        self.fp = fp
        self.node = node                      # узел в библиотечном виде (угол локальный)
        self.num = str(node[1])
        self.kind = str(node[2])              # smd, thru_hole, np_thru_hole, connect
        self.shape = str(node[3])             # rect, roundrect, circle, oval, trapezoid, custom
        at = kid(node, "at")
        self.lx, self.ly, self.la = num(at, 1), num(at, 2), num(at, 3)
        size = kid(node, "size")
        self.w, self.h = num(size, 1), num(size, 2)
        drill = kid(node, "drill")
        self.drill = 0.0
        if drill:
            vals = [x for x in drill[1:] if not isinstance(x, list) and x != "oval"]
            self.drill = float(vals[0]) if vals else 0.0
        lay = kid(node, "layers")
        self.layers = [str(x) for x in lay[1:]] if lay else []
        rr = kid(node, "roundrect_rratio")
        self.rratio = num(rr, 1) if rr else 0.0
        self.net = fp.pad_nets.get(self.num, "")
        self.pintype = fp.pad_types.get(self.num, "")

    @property
    def xy(self):
        dx, dy = rotate(self.lx, self.ly, self.fp.rot)
        return self.fp.x + dx, self.fp.y + dy

    @property
    def angle(self):
        return norm_angle(self.la + self.fp.rot)

    def copper_layers(self):
        out = []
        for l in self.layers:
            if l in ("*.Cu", "F&B.Cu"):
                out += ["F.Cu", "B.Cu"]
            elif l.endswith(".Cu"):
                out.append(l)
        if self.kind == "np_thru_hole":
            return []
        return sorted(set(out))

    def polygon(self, grow=0.0):
        """Многоугольник площадки на плате (shapely), с раздувом grow."""
        w, h = self.w, self.h
        if self.shape == "custom":
            shp = self._custom_local()
        elif self.shape == "circle":
            shp = Point(0, 0).buffer(w / 2, quad_segs=8)
        elif self.shape == "oval":
            r = min(w, h) / 2
            L = max(w, h) / 2 - r
            line = LineString([(-L, 0), (L, 0)]) if w >= h else LineString([(0, -L), (0, L)])
            shp = line.buffer(r, quad_segs=8)
        elif self.shape == "roundrect" and self.rratio > 0:
            r = min(w, h) * self.rratio
            shp = sbox(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r).buffer(r, quad_segs=4)
        else:
            shp = sbox(-w / 2, -h / 2, w / 2, h / 2)
        if grow:
            shp = shp.buffer(grow, quad_segs=4)
        return _place(shp, self.angle, *self.xy)

    def _custom_local(self):
        parts = [sbox(-self.w / 2, -self.h / 2, self.w / 2, self.h / 2)]
        prims = kid(self.node, "primitives") or []
        for g in prims[1:]:
            if not isinstance(g, list):
                continue
            wd = num(kid(g, "width") or ["", 0], 1)
            if g[0] == "gr_poly":
                pts = [(num(p, 1), num(p, 2)) for p in kids(kid(g, "pts") or [], "xy")]
                if len(pts) >= 3:
                    parts.append(Polygon(pts).buffer(wd / 2))
            elif g[0] == "gr_rect":
                s, e = kid(g, "start"), kid(g, "end")
                parts.append(sbox(min(num(s, 1), num(e, 1)), min(num(s, 2), num(e, 2)),
                                  max(num(s, 1), num(e, 1)), max(num(s, 2), num(e, 2))).buffer(wd / 2))
            elif g[0] == "gr_circle":
                c, e = kid(g, "center"), kid(g, "end")
                r = math.hypot(num(e, 1) - num(c, 1), num(e, 2) - num(c, 2))
                parts.append(Point(num(c, 1), num(c, 2)).buffer(r + wd / 2))
            elif g[0] == "gr_line":
                s, e = kid(g, "start"), kid(g, "end")
                parts.append(LineString([(num(s, 1), num(s, 2)), (num(e, 1), num(e, 2))]).buffer(wd / 2))
        # примитивы заданы в системе площадки без её угла; поворот — общий в polygon()
        return unary_union(parts)


def _place(shape, angle, x, y):
    """Локальная фигура -> плата: поворот на угол KiCad (по часовой в математике) и сдвиг."""
    if angle:
        shape = affinity.rotate(shape, -angle, origin=(0, 0))
    return affinity.translate(shape, x, y)


class Footprint:
    """Деталь на плате. Узел хранится в библиотечном виде, поворот — отдельно."""

    def __init__(self, ref, lib_id, node, x=0.0, y=0.0, rot=0.0, side="F"):
        self.ref = ref
        self.lib_id = lib_id
        self.node = node
        self.x, self.y, self.rot = float(x), float(y), norm_angle(rot)
        self.side = side
        self.value = ""
        self.fields = {}
        self.datasheet = ""
        self.description = ""
        self.path = ""
        self.sheetname = ""
        self.sheetfile = ""
        self.dnp = False
        self.exclude_from_bom = False
        self.board_only = False
        self.locked = False
        self.pad_nets = {}
        self.pad_types = {}
        self.group = ""                  # функциональный блок (лист схемы)
        self.height = None               # мм над платой, если известна

    # --- геометрия --------------------------------------------------------
    def pads(self):
        return [Pad(self, p) for p in kids(self.node, "pad")]

    def pad(self, num_):
        for p in self.pads():
            if p.num == str(num_):
                return p
        raise KeyError(f"{self.ref}: нет площадки {num_}")

    def flip(self):
        """Перевернуть деталь на нижнюю сторону (как «F» в pcbnew).

        Зеркалит геометрию относительно оси X детали (y -> -y, углы со знаком минус) и
        меняет слои местами: F.Cu <-> B.Cu, F.SilkS <-> B.SilkS и так далее. Сквозные
        площадки остаются на обоих слоях. Деталь снизу видна сквозь плату, поэтому без
        зеркала она встала бы наоборот и монтаж получился бы неверным.
        """
        def swap_layer(s):
            s = str(s)
            if s.startswith("F."):
                return "B." + s[2:]
            if s.startswith("B."):
                return "F." + s[2:]
            return s

        def walk(node):
            if not isinstance(node, list) or not node:
                return
            head = node[0] if isinstance(node[0], str) else None
            if head in ("at", "start", "end", "center", "mid", "xy", "offset") and len(node) >= 3:
                try:
                    node[2] = -float(node[2]) + 0.0
                except (TypeError, ValueError):
                    pass
                if head == "at" and len(node) >= 4:
                    try:
                        node[3] = norm_angle(-float(node[3]))
                    except (TypeError, ValueError):
                        pass
            if head in ("layer", "layers"):
                for i in range(1, len(node)):
                    node[i] = Q(swap_layer(node[i]))
            if head == "effects":
                # текст на нижней стороне пишется зеркально, иначе KiCad выдаёт
                # nonmirrored_text_on_back_layer
                j = next((k for k, x in enumerate(node)
                          if isinstance(x, list) and x and x[0] == "justify"), None)
                if j is None:
                    node.append(["justify", "mirror"])
                elif "mirror" in node[j][1:]:
                    node[j] = [x for x in node[j] if x != "mirror"]
                    if len(node[j]) == 1:
                        node.pop(j)
                else:
                    node[j].append("mirror")
            for x in node:
                if isinstance(x, list):
                    walk(x)
        walk(self.node)
        self.side = "B" if self.side == "F" else "F"
        self._lc = self._lp = None
        return self

    def _layer_shapes(self, layer):
        shapes = []
        for g in self.node:
            if not (isinstance(g, list) and g and g[0] in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")):
                continue
            l = kid(g, "layer")
            if not l or str(l[1]) != layer:
                continue
            if g[0] == "fp_line":
                s, e = kid(g, "start"), kid(g, "end")
                shapes.append(LineString([(num(s, 1), num(s, 2)), (num(e, 1), num(e, 2))]))
            elif g[0] == "fp_arc":
                s, m_, e = kid(g, "start"), kid(g, "mid"), kid(g, "end")
                shapes.append(LineString(_arc_points((num(s, 1), num(s, 2)), (num(m_, 1), num(m_, 2)),
                                                     (num(e, 1), num(e, 2)))))
            elif g[0] == "fp_rect":
                s, e = kid(g, "start"), kid(g, "end")
                shapes.append(sbox(min(num(s, 1), num(e, 1)), min(num(s, 2), num(e, 2)),
                                   max(num(s, 1), num(e, 1)), max(num(s, 2), num(e, 2))))
            elif g[0] == "fp_circle":
                c, e = kid(g, "center"), kid(g, "end")
                r = math.hypot(num(e, 1) - num(c, 1), num(e, 2) - num(c, 2))
                shapes.append(Point(num(c, 1), num(c, 2)).buffer(r, quad_segs=12))
            elif g[0] == "fp_poly":
                pts = [(num(p, 1), num(p, 2)) for p in kids(kid(g, "pts") or [], "xy")]
                if len(pts) >= 3:
                    shapes.append(Polygon(pts))
        return shapes

    def local_courtyard(self):
        """Courtyard в координатах детали (кэш: узел детали при размещении не меняется)."""
        if getattr(self, "_lc", None) is None:
            self._lc = self._compute_local_courtyard()
        return self._lc

    def local_pads(self):
        """[(номер, цепь, x, y, фигура в системе площадки без поворота детали, угол в библиотеке)]."""
        if getattr(self, "_lp", None) is None:
            out = []
            for p in self.pads():
                saved = (self.x, self.y, self.rot)
                self.x, self.y, self.rot = 0.0, 0.0, 0.0
                shp = p.polygon()                  # при нулевом положении — локальная фигура с углом площадки
                self.x, self.y, self.rot = saved
                out.append((p.num, p.net, p.lx, p.ly, shp, p))
            self._lp = out
        return self._lp

    def courtyard(self):
        """Многоугольник courtyard на плате; без courtyard — площадки плюс 0,25 мм."""
        return _place(self.local_courtyard(), self.rot, self.x, self.y)

    def _compute_local_courtyard(self):
        shapes = self._layer_shapes("F.CrtYd" if self.side == "F" else "B.CrtYd")
        polys = [s for s in shapes if s.geom_type == "Polygon"]
        lines = [s for s in shapes if s.geom_type == "LineString"]
        if lines:
            polys += list(polygonize(unary_union(lines)))
        if polys:
            local = unary_union(polys)
            if local.geom_type != "Polygon":
                local = local.convex_hull
        else:
            local = None
        if local is None:
            saved = (self.x, self.y, self.rot)
            self.x, self.y, self.rot = 0.0, 0.0, 0.0
            pads = [p.polygon(0.25) for p in self.pads()]
            self.x, self.y, self.rot = saved
            return unary_union(pads).convex_hull if pads else Point(0, 0).buffer(0.5)
        return local

    def bbox(self):
        return self.courtyard().bounds

    # --- запись -------------------------------------------------------------
    def to_node(self):
        n = copy.deepcopy(self.node)
        name = Q(self.lib_id)
        body = [x for x in n[2:] if not (isinstance(x, list) and x and x[0] in
                                         ("version", "generator", "generator_version", "layer",
                                          "uuid", "at", "path", "sheetname", "sheetfile", "attr",
                                          "locked"))]
        out = ["footprint", name]
        if self.locked:
            out.append(["locked", "yes"])
        out += [["layer", Q("F.Cu" if self.side == "F" else "B.Cu")],
                ["uuid", Q(det_uuid("fp", self.ref))],
                ["at", fmt(self.x), fmt(self.y)] + ([fmt(self.rot)] if self.rot else [])]
        props = []
        rest = []
        for x in body:
            if isinstance(x, list) and x and x[0] == "fp_text" and len(x) > 2 and str(x[1]) in ("reference", "value"):
                x = _fp_text_to_property(x)          # старый вид (KiCad 7 и самодельные библиотеки)
            if isinstance(x, list) and x and x[0] == "property":
                props.append(x)
            else:
                rest.append(x)
        seen = set()
        fixed = []
        for p in props:
            pname = str(p[1])
            seen.add(pname)
            if pname == "Reference":
                p[2] = Q(self.ref)
            elif pname == "Value":
                p[2] = Q(self.value or str(p[2]))
            elif pname == "Datasheet":
                p[2] = Q(self.datasheet)
            elif pname == "Description":
                p[2] = Q(self.description)
            elif pname in self.fields:
                p[2] = Q(self.fields[pname])
            self._fix_text(p, "prop:" + pname)
            fixed.append(p)
        for pname, val in (("Datasheet", self.datasheet), ("Description", self.description)):
            if pname not in seen:
                fixed.append(self._hidden_prop(pname, val))
                seen.add(pname)
        for pname, val in self.fields.items():
            if pname not in seen:
                fixed.append(self._hidden_prop(pname, val))
        out += fixed
        if self.path:
            out.append(["path", Q(self.path)])
        if self.sheetname:
            out.append(["sheetname", Q(self.sheetname)])
        if self.sheetfile:
            out.append(["sheetfile", Q(self.sheetfile)])
        attr = kid(self.node, "attr")
        flags = [str(x) for x in attr[1:]] if attr else []
        flags = [f for f in flags if f not in ("dnp", "exclude_from_bom", "board_only")]
        if self.board_only:
            flags.append("board_only")
        if self.exclude_from_bom:
            flags.append("exclude_from_bom")
        if self.dnp:
            flags.append("dnp")
        if flags:
            out.append(["attr"] + flags)
        k = 0
        for x in rest:
            if isinstance(x, list) and x and x[0] in ("fp_line", "fp_rect", "fp_circle", "fp_arc",
                                                      "fp_poly", "fp_text", "fp_text_box", "zone"):
                x = [y for y in x if not (isinstance(y, list) and y and y[0] == "uuid")]
                if x[0] == "fp_text":
                    self._fix_text(x, f"text{k}")
                x.append(["uuid", Q(det_uuid("fpg", self.ref, k))])
                k += 1
            elif isinstance(x, list) and x and x[0] == "pad":
                x = self._pad_node(x)
            if isinstance(x, list) and x and x[0] == "zone":
                x = self._zone_to_board(x)
            out.append(x)
        return out

    def _zone_to_board(self, z):
        """Зона внутри посадочного места хранится в файле платы в координатах платы."""
        z = copy.deepcopy(z)
        for poly in kids(z, "polygon") + kids(z, "filled_polygon"):
            for xy in kids(kid(poly, "pts") or [], "xy"):
                gx, gy = rotate(num(xy, 1), num(xy, 2), self.rot)
                xy[1], xy[2] = fmt(self.x + gx), fmt(self.y + gy)
        return z

    def _hidden_prop(self, pname, val):
        return ["property", Q(pname), Q(val), ["at", "0", "0", fmt(self.rot)],
                ["layer", Q("F.Fab")], ["hide", "yes"],
                ["uuid", Q(det_uuid("txt", self.ref, "prop:" + pname))],
                ["effects", ["font", ["size", "1", "1"], ["thickness", "0.15"]]]]

    def _fix_text(self, node, key):
        at = kid(node, "at")
        if at is not None:
            local = num(at, 3)
            del at[3:]
            a = norm_angle(local + self.rot)
            at.append(fmt(a))
        node[:] = [y for y in node if not (isinstance(y, list) and y and y[0] == "uuid")]
        # uuid ставится после layer/hide, перед effects — порядок KiCad
        pos = next((i for i, y in enumerate(node) if isinstance(y, list) and y and y[0] == "effects"), len(node))
        node.insert(pos, ["uuid", Q(det_uuid("txt", self.ref, key))])

    def _pad_node(self, p):
        p = copy.deepcopy(p)
        at = kid(p, "at")
        a = norm_angle(num(at, 3) + self.rot)
        del at[3:]
        if a:
            at.append(fmt(a))
        p[:] = [y for y in p if not (isinstance(y, list) and y and y[0] in ("net", "uuid", "pintype", "pinfunction"))]
        net = self.pad_nets.get(str(p[1]), "")
        tail = []
        if net:
            tail.append(["net", Q(net)])
        if self.pad_types.get(str(p[1])):
            tail.append(["pintype", Q(self.pad_types[str(p[1])])])
        tail.append(["uuid", Q(det_uuid("pad", self.ref, p[1], len(p)))])
        # net и uuid — после геометрии, перед вложенными primitives/teardrops нет разницы для KiCad
        return p + tail


def _fp_text_to_property(x):
    """(fp_text reference "REF**" (at ..) (layer ..) [hide] (effects ..)) -> (property "Reference" ...)."""
    name = "Reference" if str(x[1]) == "reference" else "Value"
    p = ["property", Q(name), Q(str(x[2]))]
    hidden = "hide" in [y for y in x if not isinstance(y, list)]
    for y in x[3:]:
        if isinstance(y, list) and y[0] in ("at", "layer", "effects", "unlocked"):
            p.append(copy.deepcopy(y))
        elif isinstance(y, list) and y[0] == "hide":
            hidden = str(y[1]) == "yes"
    if hidden:
        pos = next((i for i, y in enumerate(p) if isinstance(y, list) and y and y[0] == "effects"), len(p))
        p.insert(pos, ["hide", "yes"])
    return p


def _arc_points(s, m, e, n=16):
    """Дуга по трём точкам -> ломаная."""
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [s, e]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def between(a, b, c):
        # лежит ли b на пути от a к c против часовой (в математических координатах)
        return (b - a) % (2 * math.pi) < (c - a) % (2 * math.pi)
    if between(a1, a2, a3):
        span = (a3 - a1) % (2 * math.pi)
    else:
        span = -((a1 - a3) % (2 * math.pi))
    return [(ux + r * math.cos(a1 + span * i / n), uy + r * math.sin(a1 + span * i / n)) for i in range(n + 1)]


# =============================================================================
# Медь и зоны
# =============================================================================

class Track:
    def __init__(self, start, end, width, layer, net, locked=False, kind="segment", mid=None):
        self.start, self.end, self.width = tuple(start), tuple(end), float(width)
        self.layer, self.net, self.locked, self.kind, self.mid = layer, net, locked, kind, mid

    def geom(self):
        if self.kind == "arc" and self.mid:
            return LineString(_arc_points(self.start, self.mid, self.end)).buffer(self.width / 2, quad_segs=4)
        if self.start == self.end:
            return Point(self.start).buffer(self.width / 2, quad_segs=4)
        return LineString([self.start, self.end]).buffer(self.width / 2, quad_segs=4)

    def length(self):
        if self.kind == "arc" and self.mid:
            return LineString(_arc_points(self.start, self.mid, self.end)).length
        return math.dist(self.start, self.end)

    def to_node(self):
        if self.kind == "arc":
            n = ["arc", ["start", fmt(self.start[0]), fmt(self.start[1])],
                 ["mid", fmt(self.mid[0]), fmt(self.mid[1])],
                 ["end", fmt(self.end[0]), fmt(self.end[1])]]
        else:
            n = ["segment", ["start", fmt(self.start[0]), fmt(self.start[1])],
                 ["end", fmt(self.end[0]), fmt(self.end[1])]]
        n += [["width", fmt(self.width)], ["layer", Q(self.layer)]]
        if self.locked:
            n.append(["locked", "yes"])
        n += [["net", Q(self.net)],
              ["uuid", Q(det_uuid("trk", self.layer, self.net, *[fmt(v) for v in self.start + self.end], fmt(self.width)))]]
        return n


class Via:
    def __init__(self, at, size, drill, net, locked=False):
        self.at, self.size, self.drill, self.net, self.locked = tuple(at), float(size), float(drill), net, locked

    def geom(self):
        return Point(self.at).buffer(self.size / 2, quad_segs=8)

    def to_node(self):
        n = ["via", ["at", fmt(self.at[0]), fmt(self.at[1])], ["size", fmt(self.size)],
             ["drill", fmt(self.drill)], ["layers", Q("F.Cu"), Q("B.Cu")]]
        if self.locked:
            n.append(["locked", "yes"])
        n += [["net", Q(self.net)], ["uuid", Q(det_uuid("via", self.net, fmt(self.at[0]), fmt(self.at[1])))]]
        return n


class Zone:
    """Заливка (net задан) или зона запрета (keepout задан)."""

    def __init__(self, pts, layers, net="", name="", priority=0, clearance=0.3,
                 min_thickness=0.25, thermal_gap=0.5, thermal_width=0.5,
                 connect="thermal", keepout=None, island_removal=0):
        self.pts = [tuple(p) for p in pts]
        self.layers = list(layers) if not isinstance(layers, str) else [layers]
        self.net, self.name, self.priority = net, name, priority
        self.clearance, self.min_thickness = clearance, min_thickness
        self.thermal_gap, self.thermal_width = thermal_gap, thermal_width
        self.connect = connect               # thermal | solid | none | thru_hole_only
        self.keepout = keepout               # dict tracks/vias/pads/copperpour/footprints -> bool (запрещено)
        self.island_removal = island_removal
        self.filled = {}                     # слой -> [Polygon] после заливки KiCad

    def polygon(self):
        return Polygon(self.pts)

    def to_node(self):
        n = ["zone"]
        if self.net:
            n.append(["net", Q(self.net)])
        if len(self.layers) == 1:
            n.append(["layer", Q(self.layers[0])])
        else:
            n.append(["layers"] + [Q(l) for l in self.layers])
        n.append(["uuid", Q(det_uuid("zone", self.name, self.net, self.layers, *[fmt(v) for p in self.pts[:2] for v in p]))])
        if self.name:
            n.append(["name", Q(self.name)])
        n.append(["hatch", "edge", "0.5"])
        if self.priority:
            n.append(["priority", str(self.priority)])
        cp = ["connect_pads"]
        if self.connect == "solid":
            cp.append("yes")
        elif self.connect == "none":
            cp.append("no")
        elif self.connect == "thru_hole_only":
            cp.append("thru_hole_only")
        cp.append(["clearance", fmt(self.clearance)])
        n.append(cp)
        n.append(["min_thickness", fmt(self.min_thickness)])
        n.append(["filled_areas_thickness", "no"])
        if self.keepout is not None:
            ko = ["keepout"]
            for key in ("tracks", "vias", "pads", "copperpour", "footprints"):
                ko.append([key, "not_allowed" if self.keepout.get(key) else "allowed"])
            n.append(ko)
            n.append(["placement", ["enabled", "no"], ["sheetname", Q("")]])
        fill = ["fill"]
        if self.keepout is None:
            fill.append("yes")
        fill += [["thermal_gap", fmt(self.thermal_gap)], ["thermal_bridge_width", fmt(self.thermal_width)]]
        if self.keepout is None:
            fill.append(["island_removal_mode", str(self.island_removal)])
        n.append(fill)
        n.append(["polygon", ["pts"] + [["xy", fmt(x), fmt(y)] for x, y in self.pts]])
        return n


# =============================================================================
# Плата
# =============================================================================

_HEADER_LAYERS = [
    (0, "F.Cu", "signal"), (2, "B.Cu", "signal"),
    (9, "F.Adhes", "user", "F.Adhesive"), (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user"), (15, "B.Paste", "user"),
    (5, "F.SilkS", "user", "F.Silkscreen"), (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user"), (3, "B.Mask", "user"),
    (17, "Dwgs.User", "user", "User.Drawings"), (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"), (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user"), (27, "Margin", "user"),
    (31, "F.CrtYd", "user", "F.Courtyard"), (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user"), (33, "B.Fab", "user"),
    (39, "User.1", "user"), (41, "User.2", "user"), (43, "User.3", "user"), (45, "User.4", "user"),
]


class Board:
    def __init__(self, name="board", thickness=1.6):
        self.name = name
        self.thickness = thickness
        self.outline = []            # [("line", p1, p2)] | [("arc", start, mid, end)]
        self.footprints = {}
        self.tracks = []
        self.vias = []
        self.zones = []
        self.graphics = []           # готовые узлы gr_* (надписи, пометки на User-слоях)

    # --- контур -----------------------------------------------------------
    def set_outline_rect(self, x0, y0, x1, y1, r=1.0):
        self.outline = []
        if r <= 0:
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            for i in range(4):
                self.outline.append(("line", pts[i], pts[(i + 1) % 4]))
            return
        c = r * (1 - math.sqrt(0.5))
        self.outline += [
            ("line", (x0 + r, y0), (x1 - r, y0)),
            ("arc", (x1 - r, y0), (x1 - c, y0 + c), (x1, y0 + r)),
            ("line", (x1, y0 + r), (x1, y1 - r)),
            ("arc", (x1, y1 - r), (x1 - c, y1 - c), (x1 - r, y1)),
            ("line", (x1 - r, y1), (x0 + r, y1)),
            ("arc", (x0 + r, y1), (x0 + c, y1 - c), (x0, y1 - r)),
            ("line", (x0, y1 - r), (x0, y0 + r)),
            ("arc", (x0, y0 + r), (x0 + c, y0 + c), (x0 + r, y0)),
        ]

    def set_outline_poly(self, pts):
        self.outline = [("line", tuple(pts[i]), tuple(pts[(i + 1) % len(pts)])) for i in range(len(pts))]

    def outline_polygon(self):
        """Замкнутый контур из отрезков и дуг Edge.Cuts в любом порядке и направлении."""
        pieces = []
        for e in self.outline:
            pieces.append([e[1], e[2]] if e[0] == "line" else _arc_points(e[1], e[2], e[3], 8))
        if not pieces:
            return None
        loop = list(pieces.pop(0))
        tol = 1e-3
        while pieces:
            end = loop[-1]
            k = next((i for i, p in enumerate(pieces) if math.dist(p[0], end) < tol), None)
            if k is not None:
                loop += pieces.pop(k)[1:]
                continue
            k = next((i for i, p in enumerate(pieces) if math.dist(p[-1], end) < tol), None)
            if k is not None:
                loop += list(reversed(pieces.pop(k)))[1:]
                continue
            break                                   # разрыв контура: остаток не сцепился
        if len(loop) < 3:
            return None
        return Polygon(loop).buffer(0)

    # --- детали -------------------------------------------------------------
    def add_footprint(self, ref, lib_id, node, x=0.0, y=0.0, rot=0.0):
        fp = Footprint(ref, lib_id, node, x, y, rot)
        self.footprints[ref] = fp
        return fp

    def pads(self):
        out = []
        for fp in self.footprints.values():
            out += fp.pads()
        return out

    def net_names(self):
        names = set()
        for p in self.pads():
            if p.net:
                names.add(p.net)
        for t in self.tracks:
            names.add(t.net)
        for v in self.vias:
            names.add(v.net)
        for z in self.zones:
            if z.net:
                names.add(z.net)
        return sorted(n for n in names if n)

    # --- запись ---------------------------------------------------------------
    def to_node(self):
        n = ["kicad_pcb", ["version", BOARD_VERSION], ["generator", Q("pcbnew")],
             ["generator_version", Q("10.0")],
             ["general", ["thickness", fmt(self.thickness)], ["legacy_teardrops", "no"]],
             ["paper", Q("A4")]]
        layers = ["layers"]
        for L in _HEADER_LAYERS:
            layers.append([str(L[0]), Q(L[1]), L[2]] + ([Q(L[3])] if len(L) > 3 else []))
        n.append(layers)
        n.append(["setup", ["pad_to_mask_clearance", "0"],
                  ["allow_soldermask_bridges_in_footprints", "no"],
                  ["tenting", ["front", "yes"], ["back", "yes"]],
                  ["covering", ["front", "no"], ["back", "no"]],
                  ["plugging", ["front", "no"], ["back", "no"]],
                  ["capping", "no"], ["filling", "no"],
                  ["pcbplotparams", ["layerselection", "0x00000000_00000000_55555555_5755f5ff"],
                   ["plot_on_all_layers_selection", "0x00000000_00000000_00000000_00000000"],
                   ["disableapertmacros", "no"], ["usegerberextensions", "no"],
                   ["usegerberattributes", "yes"], ["usegerberadvancedattributes", "yes"],
                   ["creategerberjobfile", "yes"], ["dashed_line_dash_ratio", "12"],
                   ["dashed_line_gap_ratio", "3"], ["svgprecision", "4"], ["plotframeref", "no"],
                   ["mode", "1"], ["useauxorigin", "no"], ["pdf_front_fp_property_popups", "yes"],
                   ["pdf_back_fp_property_popups", "yes"], ["pdf_metadata", "yes"],
                   ["pdf_single_document", "no"], ["dxfpolygonmode", "yes"],
                   ["dxfimperialunits", "yes"], ["dxfusepcbnewfont", "yes"], ["psnegative", "no"],
                   ["psa4output", "no"], ["plot_black_and_white", "yes"], ["sketchpadsonfab", "no"],
                   ["plotpadnumbers", "no"], ["hidednponfab", "no"], ["sketchdnponfab", "yes"],
                   ["crossoutdnponfab", "yes"], ["subtractmaskfromsilk", "no"], ["outputformat", "1"],
                   ["mirror", "no"], ["drillshape", "1"], ["scaleselection", "1"],
                   ["outputdirectory", Q("")]]])
        for ref in sorted(self.footprints, key=_ref_key):
            n.append(self.footprints[ref].to_node())
        for i, e in enumerate(self.outline):
            if e[0] == "line":
                g = ["gr_line", ["start", fmt(e[1][0]), fmt(e[1][1])], ["end", fmt(e[2][0]), fmt(e[2][1])]]
            else:
                g = ["gr_arc", ["start", fmt(e[1][0]), fmt(e[1][1])], ["mid", fmt(e[2][0]), fmt(e[2][1])],
                     ["end", fmt(e[3][0]), fmt(e[3][1])]]
            g += [["stroke", ["width", "0.05"], ["type", "solid"]], ["layer", Q("Edge.Cuts")],
                  ["uuid", Q(det_uuid("edge", i, e[0], *[fmt(v) for p in e[1:] for v in p]))]]
            n.append(g)
        n += copy.deepcopy(self.graphics)
        for t in sorted(self.tracks, key=lambda t: (t.net, t.layer, t.start, t.end)):
            n.append(t.to_node())
        for v in sorted(self.vias, key=lambda v: (v.net, v.at)):
            n.append(v.to_node())
        for z in self.zones:
            n.append(z.to_node())
        n.append(["embedded_fonts", "no"])
        return n

    def save(self, path):
        text = dump(self.to_node()) + "\n"
        open(path, "w", encoding="utf-8").write(text)
        return path

    # --- чтение -------------------------------------------------------------
    @classmethod
    def read(cls, path):
        """Читает плату KiCad 9 или 10. Узлы деталей переводятся в библиотечный вид."""
        t = load_sexp(path)
        b = cls(os.path.splitext(os.path.basename(path))[0])
        gen = kid(t, "general")
        if gen and kid(gen, "thickness"):
            b.thickness = num(kid(gen, "thickness"), 1)
        netnames = {}
        for nn in kids(t, "net"):              # KiCad 9: таблица цепей
            if len(nn) >= 3:
                netnames[str(nn[1])] = str(nn[2])

        def netof(node):
            k = kid(node, "net")
            if not k or len(k) < 2:
                return ""
            if len(k) >= 3:
                return str(k[2])
            v = k[1]
            return str(v) if isinstance(v, Q) else netnames.get(str(v), "")

        for f in kids(t, "footprint"):
            lib_id = str(f[1])
            at = kid(f, "at")
            x, y, rot = num(at, 1), num(at, 2), num(at, 3)
            lay = kid(f, "layer")
            side = "B" if lay and str(lay[1]) == "B.Cu" else "F"
            node = copy.deepcopy(f)
            ref = ""
            for p in kids(node, "property"):
                if str(p[1]) == "Reference":
                    ref = str(p[2])
            # KiCad 8/9 могли хранить обозначение в fp_text reference
            for tx in kids(node, "fp_text"):
                if str(tx[1]) == "reference" and not ref:
                    ref = str(tx[2])
            fp = Footprint(ref, lib_id, node, x, y, rot, side)
            for p in kids(node, "property"):
                pname, val = str(p[1]), str(p[2]) if len(p) > 2 else ""
                if pname == "Value":
                    fp.value = val
                elif pname == "Datasheet":
                    fp.datasheet = val
                elif pname == "Description":
                    fp.description = val
                elif pname != "Reference":
                    fp.fields[pname] = val
            for pad in kids(node, "pad"):
                net = netof(pad)
                if net:
                    fp.pad_nets[str(pad[1])] = net
                pt = kid(pad, "pintype")
                if pt:
                    fp.pad_types[str(pad[1])] = str(pt[1])
                pat = kid(pad, "at")
                if pat is not None:           # угол в файле полный -> локальный
                    la = norm_angle(num(pat, 3) - rot)
                    del pat[3:]
                    if la:
                        pat.append(fmt(la))
            for tx in kids(node, "property") + kids(node, "fp_text"):
                tat = kid(tx, "at")
                if tat is not None and len(tat) > 3:
                    la = norm_angle(num(tat, 3) - rot)
                    del tat[3:]
                    tat.append(fmt(la))
            for fz in kids(node, "zone"):
                for poly in kids(fz, "polygon") + kids(fz, "filled_polygon"):
                    for xy in kids(kid(poly, "pts") or [], "xy"):
                        lx, ly = rotate(num(xy, 1) - x, num(xy, 2) - y, -rot)
                        xy[1], xy[2] = fmt(lx), fmt(ly)
            attr = kid(node, "attr")
            flags = [str(a) for a in attr[1:]] if attr else []
            fp.dnp = "dnp" in flags
            fp.exclude_from_bom = "exclude_from_bom" in flags
            fp.board_only = "board_only" in flags
            fp.locked = kid(node, "locked") is not None or (len(f) > 2 and f[2] == "locked")
            pth = kid(node, "path")
            fp.path = str(pth[1]) if pth else ""
            sn, sf = kid(node, "sheetname"), kid(node, "sheetfile")
            fp.sheetname = str(sn[1]) if sn else ""
            fp.group = fp.sheetname.strip("/")
            fp.sheetfile = str(sf[1]) if sf else ""
            b.footprints[ref or f"?{len(b.footprints)}"] = fp
        for s in kids(t, "segment") + kids(t, "arc"):
            st, en = kid(s, "start"), kid(s, "end")
            mid = kid(s, "mid")
            b.tracks.append(Track((num(st, 1), num(st, 2)), (num(en, 1), num(en, 2)),
                                  num(kid(s, "width"), 1), str(kid(s, "layer")[1]), netof(s),
                                  locked=kid(s, "locked") is not None, kind=s[0],
                                  mid=(num(mid, 1), num(mid, 2)) if mid else None))
        for v in kids(t, "via"):
            at = kid(v, "at")
            b.vias.append(Via((num(at, 1), num(at, 2)), num(kid(v, "size"), 1),
                              num(kid(v, "drill"), 1), netof(v), locked=kid(v, "locked") is not None))
        for z in kids(t, "zone"):
            lay = kid(z, "layer")
            lays = kid(z, "layers")
            layers = [str(lay[1])] if lay else [str(x) for x in (lays[1:] if lays else [])]
            poly = kid(z, "polygon")
            pts = [(num(p, 1), num(p, 2)) for p in kids(kid(poly, "pts") or [], "xy")] if poly else []
            ko = kid(z, "keepout")
            keep = None
            if ko:
                keep = {str(k[0]): str(k[1]) == "not_allowed" for k in ko[1:] if isinstance(k, list)}
            nm = kid(z, "name")
            pr = kid(z, "priority")
            zone = Zone(pts, layers, net=netof(z), name=str(nm[1]) if nm else "",
                        priority=int(num(pr, 1)) if pr else 0, keepout=keep)
            cp = kid(z, "connect_pads")
            if cp:
                cl = kid(cp, "clearance")
                zone.clearance = num(cl, 1) if cl else zone.clearance
            for fpoly in kids(z, "filled_polygon"):
                fl = str(kid(fpoly, "layer")[1])
                fpts = [(num(p, 1), num(p, 2)) for p in kids(kid(fpoly, "pts") or [], "xy")]
                if len(fpts) >= 3:
                    zone.filled.setdefault(fl, []).append(Polygon(fpts).buffer(0))
            b.zones.append(zone)
        for g in t:
            if isinstance(g, list) and g and g[0] in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
                lay = kid(g, "layer")
                if lay and str(lay[1]) == "Edge.Cuts":
                    if g[0] == "gr_line":
                        b.outline.append(("line", (num(kid(g, "start"), 1), num(kid(g, "start"), 2)),
                                          (num(kid(g, "end"), 1), num(kid(g, "end"), 2))))
                    elif g[0] == "gr_arc":
                        b.outline.append(("arc", (num(kid(g, "start"), 1), num(kid(g, "start"), 2)),
                                          (num(kid(g, "mid"), 1), num(kid(g, "mid"), 2)),
                                          (num(kid(g, "end"), 1), num(kid(g, "end"), 2))))
                    elif g[0] == "gr_rect":
                        s, e = kid(g, "start"), kid(g, "end")
                        b.set_outline_rect(num(s, 1), num(s, 2), num(e, 1), num(e, 2), r=0)
                else:
                    b.graphics.append(g)
            elif isinstance(g, list) and g and g[0] == "gr_text":
                b.graphics.append(g)
        return b

    def filled(self, layer, net=None):
        """Объединение залитых областей слоя (после заливки KiCad)."""
        polys = []
        for z in self.zones:
            if z.keepout is not None:
                continue
            if net is not None and z.net != net:
                continue
            polys += z.filled.get(layer, [])
        return unary_union(polys) if polys else Polygon()


# =============================================================================
# Шелкография
# =============================================================================

def _prop(fp, name):
    for p in kids(fp.node, "property"):
        if str(p[1]) == name:
            return p
    for x in kids(fp.node, "fp_text"):
        if str(x[1]) == name.lower():
            return x
    return None


def _set_text(node, layer=None, local=None, angle=None, size=None, hide=None):
    if layer is not None:
        l = kid(node, "layer")
        if l is not None:
            l[1] = Q(layer)
    if local is not None or angle is not None:
        at = kid(node, "at")
        if at is not None:
            if local is not None:
                at[1], at[2] = fmt(local[0]), fmt(local[1])
            del at[3:]
            at.append(fmt(angle or 0))
    if size is not None:
        eff = kid(node, "effects")
        font = kid(eff, "font") if eff else None
        if font is not None:
            sz = kid(font, "size")
            if sz is not None:
                sz[1], sz[2] = fmt(size), fmt(size)
            th = kid(font, "thickness")
            if th is not None:
                th[1] = fmt(max(0.1, size * 0.15))
    if hide is not None:
        node[:] = [y for y in node if not (isinstance(y, list) and y and y[0] == "hide") and y != "hide"]
        if hide:
            pos = next((i for i, y in enumerate(node) if isinstance(y, list) and y and y[0] == "effects"), len(node))
            node.insert(pos, ["hide", "yes"])


def text_box(cx, cy, text, size):
    """Приблизительный прямоугольник надписи штриховым шрифтом KiCad."""
    w = max(1, len(text)) * size * 0.72 + size * 0.15
    h = size * 1.25
    return sbox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def tidy_silk(board, small_area=16.0, silk_size=1.0, fab_size=0.5, edge=0.5):
    """Обозначения мелких деталей — на F.Fab, крупных — на шелкографию в свободное место.

    Мелкие детали (courtyard меньше small_area мм²) на шелкографии не подписываются:
    при ручном монтаже их находят по сборочному чертежу (F.Fab), а подпись 1 мм
    на плате 0402 неизбежно ложится на площадки соседей. Номинал всегда только
    на F.Fab. Возвращает список деталей, для которых место не нашлось.
    """
    outline = board.outline_polygon()
    inner = outline.buffer(-edge) if outline is not None else None
    pads = []
    silk = []
    fps = [f for f in board.footprints.values()]
    for fp in fps:
        for p in fp.pads():
            if p.copper_layers() or p.kind == "np_thru_hole":
                pads.append(p.polygon(0.2))
        for g in fp._layer_shapes("F.SilkS" if fp.side == "F" else "B.SilkS"):
            silk.append(_place(g, fp.rot, fp.x, fp.y).buffer(0.2))
    for fp in fps:
        if fp.board_only:
            continue
        for x in list(kids(fp.node, "fp_text")):
            if str(x[1]) in ("reference", "value"):
                fp.node[fp.node.index(x)] = _fp_text_to_property(x)
    from shapely.strtree import STRtree
    failed = []
    placed_text = []
    order = sorted(fps, key=lambda f: -f.courtyard().area)
    for fp in order:
        ref = _prop(fp, "Reference")
        val = _prop(fp, "Value")
        if ref is None:
            continue
        court = fp.courtyard()
        fab = "F.Fab" if fp.side == "F" else "B.Fab"      # деталь снизу подписывается снизу
        silk_layer = "F.SilkS" if fp.side == "F" else "B.SilkS"
        if val is not None:
            _set_text(val, layer=fab, size=fab_size, hide=True)
        cx, cy = court.centroid.coords[0]
        lx, ly = rotate(cx - fp.x, cy - fp.y, -fp.rot)
        if fp.board_only or court.area < small_area:
            x0, y0, x1, y1 = court.bounds
            size = max(0.3, min(fab_size, (x1 - x0) / max(1, len(fp.ref)) / 0.8, (y1 - y0) / 1.4))
            _set_text(ref, layer=fab, local=(lx, ly), angle=norm_angle(-fp.rot), size=size, hide=False)
            continue
        x0, y0, x1, y1 = court.bounds
        h = silk_size * 1.25
        cands = [((x0 + x1) / 2, y0 - h / 2 - 0.25), ((x0 + x1) / 2, y1 + h / 2 + 0.25),
                 (x0 - 0.3 - len(fp.ref) * silk_size * 0.36, (y0 + y1) / 2),
                 (x1 + 0.3 + len(fp.ref) * silk_size * 0.36, (y0 + y1) / 2),
                 ((x0 + x1) / 2, (y0 + y1) / 2)]
        obstacles = pads + [s for s in silk] + placed_text
        tree = STRtree(obstacles) if obstacles else None
        chosen = None
        for tx, ty in cands:
            box_ = text_box(tx, ty, fp.ref, silk_size)
            if inner is not None and not inner.contains(box_):
                continue
            if tree is not None and any(obstacles[int(i)].intersects(box_) for i in tree.query(box_)):
                continue
            chosen = (tx, ty, box_)
            break
        if chosen is None:
            _set_text(ref, layer=fab, local=(lx, ly), angle=norm_angle(-fp.rot), size=fab_size, hide=False)
            failed.append(fp.ref)
            continue
        tx, ty, box_ = chosen
        llx, lly = rotate(tx - fp.x, ty - fp.y, -fp.rot)
        _set_text(ref, layer=silk_layer, local=(llx, lly), angle=norm_angle(-fp.rot), size=silk_size, hide=False)
        placed_text.append(box_.buffer(0.2))
    return failed


def _ref_key(ref):
    m = re.match(r"([A-Za-z#]*)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0), ref)


# =============================================================================
# Сборка платы из нетлиста
# =============================================================================

def board_from_netlist(netlist_path, project_dir, name, placements=None, thickness=1.6,
                       default_pos=(0.0, 0.0)):
    """Создаёт плату: все компоненты нетлиста с посадочным местом, цепи на площадках.

    placements: {ref: (x, y, rot)} или (x, y, rot, "B") для нижней стороны;
    не указанные детали ставятся в default_pos.
    Компоненты без посадочного места (выносные датчики, банка) на плату не попадают.
    """
    comps, nets = read_netlist(netlist_path)
    lib = FootprintLib(project_dir)
    b = Board(name, thickness)
    pinmap = {}
    for net, nodes in nets.items():
        for ref, pin, ptype in nodes:
            pinmap.setdefault(ref, {})[pin] = (net, ptype)
    placements = placements or {}
    for ref in sorted(comps, key=_ref_key):
        c = comps[ref]
        if not c.footprint or c.exclude_from_board:
            continue
        node = lib.load(c.footprint)
        pl = placements.get(ref, (default_pos[0], default_pos[1], 0.0))
        x, y, rot = pl[0], pl[1], pl[2]
        fp = b.add_footprint(ref, c.footprint, node, x, y, rot)
        if len(pl) > 3 and str(pl[3]).upper().startswith("B"):
            fp.flip()                       # деталь на нижней стороне — по согласованию с человеком
        fp.value = c.value
        fp.datasheet = c.datasheet
        fp.description = c.description
        fp.fields = dict(c.fields)
        fp.path = c.path
        fp.sheetname = c.sheet_names
        fp.sheetfile = c.sheetfile
        fp.dnp = c.dnp
        fp.exclude_from_bom = c.exclude_from_bom
        fp.group = c.sheetname
        padnums = {str(p[1]) for p in kids(node, "pad")}
        for pin, (net, ptype) in pinmap.get(ref, {}).items():
            if pin in padnums:
                fp.pad_nets[pin] = net
                fp.pad_types[pin] = ptype
    return b, comps, nets


# =============================================================================
# Проект: правила и классы цепей в .kicad_pro
# =============================================================================

def update_project(pro_path, rules=None, classes=None, patterns=None):
    """Меняет только правила и классы цепей; остальной JSON сохраняется как есть."""
    p = json.load(open(pro_path, encoding="utf-8"))
    if rules:
        p.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {}).update(rules)
    if classes is not None:
        ns = p.setdefault("net_settings", {})
        base = next((c for c in ns.get("classes", []) if c.get("name") == "Default"), {})
        out = []
        for c in classes:
            d = dict(base)
            d.update(c)
            out.append(d)
        ns["classes"] = out
    if patterns is not None:
        p.setdefault("net_settings", {})["netclass_patterns"] = patterns
    json.dump(p, open(pro_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


# =============================================================================
# Командная строка
# =============================================================================

def main(argv):
    if len(argv) >= 3 and argv[1] == "info":
        b = Board.read(argv[2])
        op = b.outline_polygon()
        print(f"плата: {argv[2]}")
        if op is not None:
            x0, y0, x1, y1 = op.bounds
            print(f"контур: {x1 - x0:.2f} x {y1 - y0:.2f} мм, площадь {op.area:.0f} мм2")
        print(f"деталей: {len(b.footprints)}, цепей: {len(b.net_names())}, "
              f"дорожек: {len(b.tracks)}, переходных: {len(b.vias)}, зон: {len(b.zones)}")
        return 0
    print(__doc__)
    print("использование: pcblib.py info плата.kicad_pcb")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
