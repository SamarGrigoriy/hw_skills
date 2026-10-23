#!/usr/bin/env python3
"""
emccheck.py — линтер платы: ЭМС (reference/emc.md), монтаж и корпус
(reference/assembly.md). То, чего нет в DRC KiCad: путь обратного тока, щели в
земле, развязка у вывода, защита порта первой от разъёма, посадка в корпус.

Работает по плате ПОСЛЕ заливки KiCad (check.py запускает DRC с --refill-zones
--save-board): геометрия меди берётся из filled_polygon — ровно та, что увидит
завод. Роли цепей, зоны, пары развязки, порты и корпус — из описания платы
<плата>.desc.json, которое пишет gen_pcb.py.

Каждое правило объявляет код из справочника, стадию, с которой проверяется, и
что именно меряет. Список реализованных правил: `emccheck.py --list`. Правило из
справочника, которого нет в списке, этим линтером НЕ проверяется — его
проверяют глазами по картинкам, и это отмечается в отчёте этапа.

Исключения: в описании платы — rule, object (подстрока сообщения), reason,
agreed, date. Сработавшее исключение не считается ошибкой, но печатается
отдельным списком при каждом прогоне.

Использование:
    python3 emccheck.py плата.kicad_pcb --desc плата.desc.json --stage placement [--json отчёт.json]
    python3 emccheck.py --list
"""

import argparse
import heapq
import json
import math
import os
import sys
from collections import defaultdict, deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcblib  # noqa: E402

from shapely.geometry import Point, LineString, Polygon, box as sbox  # noqa: E402
from shapely.ops import unary_union  # noqa: E402
from shapely.prepared import prep  # noqa: E402
from shapely.strtree import STRtree  # noqa: E402

STAGES = ["outline", "placement", "critical", "full"]
ERR, WARN = "ошибка", "предупреждение"

# Наименьшие расстояния между медью разных ролей, мм (emc.md, раздел 10).
XTK = {
    ("SW", "RF"): 5, ("SW", "CLK"): 3, ("SW", "CABLE"): 3, ("SW", "ANALOG"): 5, ("SW", "RESET"): 3,
    ("CLK", "RF"): 3, ("CLK", "CABLE"): 3, ("CLK", "ANALOG"): 3, ("CLK", "RESET"): 2,
    ("CABLE", "RF"): 5, ("CABLE", "PULSE"): 2, ("CABLE", "ANALOG"): 3, ("CABLE", "RESET"): 3,
    ("PULSE", "RF"): 3, ("PULSE", "ANALOG"): 2, ("PULSE", "RESET"): 1,
    ("RF", "ANALOG"): 3, ("RF", "RESET"): 3,
}
FAST_ROLES = {"RF", "CLK", "SW", "USB", "ETH"}
SMALL_LIBS = ("Resistor_SMD", "Capacitor_SMD", "Capacitor_Tantalum_SMD", "Inductor_SMD", "Diode_SMD",
              "Package_TO_SOT_SMD", "Package_SO", "Package_QFP", "Package_DFN_QFN", "Crystal",
              "LED_SMD", "Sensor_Humidity", "TestPoint", "Connector_Coaxial", "Fuse", "MountingHole")

RULES = []


def rule(code, stage, what):
    def deco(fn):
        RULES.append((code, stage, what, fn))
        return fn
    return deco


def xtk_limit(a, b):
    return XTK.get((a, b)) or XTK.get((b, a))


def lam_eff(f_mhz, eps=3.3):
    return 299792.458 / f_mhz / math.sqrt(eps)


def ipc2221_width(current, dT=10.0, copper_um=35.0):
    """Ширина наружной дорожки, мм, по IPC-2221 для тока current, А."""
    if current <= 0:
        return 0.0
    area_mil2 = (current / (0.048 * dT ** 0.44)) ** (1 / 0.725)
    thick_mil = copper_um / 25.4
    return area_mil2 / thick_mil * 0.0254


class Ctx:
    def __init__(self, board, desc, stage):
        self.b = board
        self.d = desc
        self.stage = stage
        self.v = []
        self.outline = board.outline_polygon()
        self.h = float(desc.get("h", 1.5))
        self.eps = float(desc.get("eps_eff", 3.3))
        self.roles = dict(desc.get("roles", {}))
        self.gnd = set(desc.get("gnd_nets", ["GND"]))
        self.power = set(desc.get("power_nets", []))
        self.unplaced = set(desc.get("unplaced", []))
        self.fps = {r: f for r, f in board.footprints.items() if not f.board_only and r not in self.unplaced}
        self.pads = [p for f in self.fps.values() for p in f.pads()]
        self.pads_by_net = defaultdict(list)
        for p in self.pads:
            if p.net:
                self.pads_by_net[p.net].append(p)
        self._court = {}
        self._padpoly = {}

    # --- справка --------------------------------------------------------------
    def role(self, net):
        if not net:
            return ""
        if net in self.gnd:
            return "GND"
        return self.roles.get(net, "POWER" if net in self.power else "SLOW")

    def court(self, ref):
        if ref not in self._court:
            self._court[ref] = self.b.footprints[ref].courtyard()
        return self._court[ref]

    def padpoly(self, p):
        key = (p.fp.ref, p.num, p.lx, p.ly)
        if key not in self._padpoly:
            self._padpoly[key] = p.polygon()
        return self._padpoly[key]

    def fmax_at(self, pt):
        f = float(self.d.get("fmax_board_mhz", 175))
        for z in self.d.get("zones", []):
            if z.get("fmax_mhz") and Polygon(z["poly"]).contains(Point(pt)):
                f = max(f, float(z["fmax_mhz"]))
        return f

    def add(self, code, level, msg, where=None, value=None, limit=None):
        self.v.append(dict(rule=code, level=level, msg=msg,
                           where=[round(where[0], 2), round(where[1], 2)] if where else None,
                           value=value, limit=limit))

    def gnd_fill(self, layer):
        polys = []
        for z in self.b.zones:
            if z.keepout is None and z.net in self.gnd:
                polys += z.filled.get(layer, [])
        return unary_union(polys) if polys else Polygon()

    def tracks(self, layer=None, net=None):
        return [t for t in self.b.tracks if (layer is None or t.layer == layer) and (net is None or t.net == net)]


# =============================================================================
# Корпус и крепёж
# =============================================================================

@rule("ENC-1", "outline", "контур платы внутри полости корпуса с зазором до стенок")
def enc1(c):
    enc = c.d.get("enclosure")
    if not enc or c.outline is None:
        return
    cav = Polygon(enc["cavity"]).buffer(-float(enc.get("wall_clearance", 1.0)))
    for tongue in enc.get("pockets", []):
        cav = cav.difference(Polygon(tongue).buffer(float(enc.get("wall_clearance", 1.0))))
    out = c.outline.difference(cav)
    if out.area > 0.01:
        p = out.representative_point()
        c.add("ENC-1", ERR, f"контур выходит за полость корпуса с зазором: {out.area:.2f} мм2",
              (p.x, p.y), round(out.area, 2), 0)


@rule("ENC-3", "outline", "в зоне запрета крепежа нет меди, площадок и деталей")
def enc3(c):
    for hole in c.d.get("holes", []):
        zone = Point(hole["at"]).buffer(float(hole.get("keepout_d", 8.5)) / 2, quad_segs=16)
        pz = prep(zone)
        for ref, fp in c.fps.items():
            if pz.intersects(c.court(ref)):
                c.add("ENC-3", ERR, f"деталь {ref} заходит в зону запрета крепежа {hole.get('name', '')}",
                      hole["at"])
        for t in c.b.tracks:
            if pz.intersects(t.geom()):
                c.add("ENC-3", ERR, f"дорожка {t.net} в зоне запрета крепежа {hole.get('name', '')}", hole["at"])
                break
        for v in c.b.vias:
            if pz.intersects(v.geom()):
                c.add("ENC-3", ERR, f"переходное {v.net} в зоне запрета крепежа {hole.get('name', '')}", v.at)
        for z in c.b.zones:
            if z.keepout is not None:
                continue
            for layer, polys in z.filled.items():
                for poly in polys:
                    if poly.intersection(zone.buffer(-0.2)).area > 0.05:   # зона запрета — многоугольник
                        c.add("ENC-3", ERR, f"заливка {z.net} {layer} в зоне запрета крепежа "
                                            f"{hole.get('name', '')}", hole["at"])


@rule("MNT-5", "outline", "courtyard деталей внутри контура платы")
def mnt5(c):
    if c.outline is None:
        return
    allowed = {p["ref"] for p in c.d.get("ports", []) if p.get("overhang")}
    for ref in c.fps:
        if ref in allowed:
            continue
        out = c.court(ref).difference(c.outline)
        if out.area > 0.01:
            p = out.representative_point()
            c.add("MNT-5", ERR, f"{ref}: courtyard выходит за контур на {out.area:.2f} мм2", (p.x, p.y))


@rule("ENC-2", "outline", "высота деталей не больше разрешённой под крышкой")
def enc2(c):
    enc = c.d.get("enclosure", {})
    hmax = float(enc.get("height_max", 99))
    heights = c.d.get("heights", {})
    for ref, fp in c.fps.items():
        h = heights.get(ref)
        lib = fp.lib_id.split(":")[0]
        x, y = fp.x, fp.y
        if h is None:
            if lib.startswith(SMALL_LIBS) or any(lib.startswith(s) for s in SMALL_LIBS):
                continue
            c.add("ENC-2", WARN, f"{ref} ({fp.lib_id}): высота неизвестна — внести в описание платы", (x, y))
            continue
        limit = hmax
        for z in enc.get("height_zones", []):
            if Polygon(z["poly"]).intersects(c.court(ref)):
                limit = min(limit, float(z["max"]))
        if h > limit:
            c.add("ENC-2", ERR, f"{ref}: высота {h} мм больше разрешённой {limit} мм", (x, y), h, limit)


@rule("EMC-MECH-1", "placement", "крепёж не ближе 5 мм к кварцу, ВЧ-зоне и сбросу")
def mech1(c):
    sens = [p for p in c.pads if c.role(p.net) in ("CLK", "RF", "RESET")]
    for hole in c.d.get("holes", []):
        hp = Point(hole["at"])
        for p in sens:
            d = c.padpoly(p).distance(hp) - float(hole.get("drill", 3.2)) / 2
            if d < 5:
                c.add("EMC-MECH-1", WARN, f"крепёж {hole.get('name', '')} в {d:.1f} мм от {p.fp.ref}.{p.num} "
                                          f"({p.net}, {c.role(p.net)})", hole["at"], round(d, 2), 5)
                break


# =============================================================================
# Монтаж
# =============================================================================

@rule("MNT-1", "placement", "зазор между courtyard соседних деталей по способу монтажа")
def mnt1(c):
    gap = 0.5 if c.d.get("assembly", "manual") == "manual" else 0.25
    refs = sorted(c.fps)
    geoms = [c.court(r) for r in refs]
    sides = [c.b.footprints[r].side for r in refs]
    tree = STRtree(geoms)
    seen = set()
    for i, g in enumerate(geoms):
        for j in tree.query(g.buffer(gap)):
            j = int(j)
            if j <= i or (i, j) in seen:
                continue
            seen.add((i, j))
            if sides[i] != sides[j]:
                continue                      # детали на разных сторонах платы не мешают друг другу
            d = g.distance(geoms[j])
            if d < gap - 1e-6:
                inter = g.intersection(geoms[j])
                p = inter.representative_point() if not inter.is_empty else g.centroid
                what = "пересекаются" if d == 0 else f"зазор {d:.2f} мм"
                c.add("MNT-1", ERR, f"courtyard {refs[i]} и {refs[j]}: {what} (нужно {gap} мм)",
                      (p.x, p.y), round(d, 3), gap)


@rule("MNT-6", "placement", "пассивы одного блока одной ориентации")
def mnt6(c):
    groups = defaultdict(list)
    for ref, fp in c.fps.items():
        if ref[:1] in "RCL" and ref[1:2].isdigit() and len(fp.pads()) == 2 and fp.lib_id.split(":")[0].endswith("_SMD"):
            groups[fp.group].append(fp)
    for g, fps in groups.items():
        hor = [f for f in fps if round(f.rot) % 180 == 0]
        ver = [f for f in fps if round(f.rot) % 180 == 90]
        if hor and ver and min(len(hor), len(ver)) > max(2, len(fps) // 3):
            f = min(hor, ver, key=len)[0]
            c.add("MNT-6", WARN, f"блок «{g}»: пассивов горизонтально {len(hor)}, вертикально {len(ver)}",
                  (f.x, f.y))


@rule("MNT-13", "placement", "ввод провода клеммника к кромке порта, клеммник у кромки")
def mnt13(c):
    if c.outline is None:
        return
    x0, y0, x1, y1 = c.outline.bounds
    dirs = {"bottom": (0, 1), "top": (0, -1), "left": (-1, 0), "right": (1, 0)}
    loc = {"+y": (0, 1), "-y": (0, -1), "+x": (1, 0), "-x": (-1, 0)}
    for port in c.d.get("ports", []):
        ref = port["ref"]
        fp = c.b.footprints.get(ref)
        if fp is None:
            continue
        edge = port.get("edge")
        if port.get("entry_local") and edge in dirs:
            lx, ly = loc[port["entry_local"]]
            vx, vy = pcblib.rotate(lx, ly, fp.rot)
            ex, ey = dirs[edge]
            if vx * ex + vy * ey < 0.9:
                c.add("MNT-13", ERR, f"{ref}: ввод провода смотрит не к кромке «{edge}»", (fp.x, fp.y))
        if edge in dirs:
            bx0, by0, bx1, by1 = c.court(ref).bounds
            dist = {"bottom": y1 - by1, "top": by0 - y0, "left": bx0 - x0, "right": x1 - bx1}[edge]
            if dist > float(port.get("edge_max", 2.0)):
                c.add("MNT-13", WARN, f"{ref}: до кромки «{edge}» {dist:.1f} мм", (fp.x, fp.y),
                      round(dist, 2), port.get("edge_max", 2.0))


@rule("ENC-4", "placement", "порт напротив своего ввода корпуса")
def enc4(c):
    for port in c.d.get("ports", []):
        fp = c.b.footprints.get(port["ref"])
        if fp is None or "gland" not in port:
            continue
        gx, gy = port["gland"]
        cx, cy = c.court(port["ref"]).centroid.coords[0]
        along = abs(cx - gx) if port.get("edge") in ("top", "bottom") else abs(cy - gy)
        tol = float(port.get("tol", 5.0))
        if along > tol:
            c.add("ENC-4", ERR, f"{port['ref']}: смещение от ввода {along:.1f} мм (допуск {tol})",
                  (cx, cy), round(along, 2), tol)


@rule("ENC-6", "placement", "датчик воздуха не ближе 15 мм к греющимся деталям")
def enc6(c):
    for s in c.d.get("air_sensors", []):
        if s["ref"] not in c.fps:
            continue
        sp = c.court(s["ref"])
        for h in c.d.get("heaters", []):
            if h in c.fps:
                d = sp.distance(c.court(h))
                if d < 15:
                    x, y = sp.centroid.coords[0]
                    c.add("ENC-6", WARN, f"{s['ref']} в {d:.1f} мм от греющейся {h}", (x, y), round(d, 1), 15)


@rule("ENC-7", "placement", "датчик влажности не дальше 20 мм от мембраны")
def enc7(c):
    for s in c.d.get("air_sensors", []):
        if s["ref"] not in c.fps or "membrane" not in s:
            continue
        d = c.court(s["ref"]).distance(Point(s["membrane"]))
        if d > 20:
            x, y = c.court(s["ref"]).centroid.coords[0]
            c.add("ENC-7", WARN, f"{s['ref']} в {d:.1f} мм от мембраны", (x, y), round(d, 1), 20)


# =============================================================================
# ЭМС: размещение
# =============================================================================

@rule("EMC-XTK-1", "placement", "расстояние между площадками цепей разных ролей (таблица emc.md, 10)")
def xtk1(c):
    items = [(p, c.role(p.net)) for p in c.pads]
    items = [(p, r) for p, r in items if r in {"SW", "CLK", "CABLE", "PULSE", "RF", "ANALOG", "RESET"}]
    polys = [c.padpoly(p) for p, _ in items]
    tree = STRtree(polys)
    signal_nets = defaultdict(set)          # деталь -> её сигнальные цепи (без земли и питания)
    for p in c.pads:
        if p.net and c.role(p.net) not in ("GND", "POWER"):
            signal_nets[p.fp.ref].add(p.net)
    reported = set()
    for i, (p, r) in enumerate(items):
        for j in tree.query(polys[i].buffer(5.0)):
            j = int(j)
            if j <= i:
                continue
            q, s = items[j]
            if p.fp.ref == q.fp.ref or r == s or p.net == q.net:
                continue
            lim = xtk_limit(r, s)
            if not lim:
                continue
            if signal_nets[p.fp.ref] & signal_nets[q.fp.ref]:
                continue                      # детали одной цепочки: фильтр, делитель, согласование
            d = polys[i].distance(polys[j])
            key = tuple(sorted((p.fp.ref, q.fp.ref)))
            if d < lim and key not in reported:
                reported.add(key)
                x, y = polys[i].centroid.coords[0]
                c.add("EMC-XTK-1", ERR, f"{p.fp.ref}.{p.num} ({p.net}, {r}) и {q.fp.ref}.{q.num} "
                                        f"({q.net}, {s}): {d:.2f} мм < {lim}", (x, y), round(d, 2), lim)


def _nearest_same_net(c, pad, ref):
    best = None
    fp = c.b.footprints.get(ref)
    if fp is None:
        return None
    for q in fp.pads():
        if q.net == pad.net:
            d = c.padpoly(pad).distance(c.padpoly(q))
            if best is None or d < best[0]:
                best = (d, q)
    return best


@rule("EMC-DEC-1", "placement", "конденсатор развязки у своего вывода питания (по меди — на стадии full)")
def dec1(c):
    for pair in c.d.get("decoupling", []):
        ref, pin = pair["pin"]
        cap = pair["cap"]
        if ref not in c.fps or cap not in c.fps:
            continue
        pad = c.b.footprints[ref].pad(pin)
        near = _nearest_same_net(c, pad, cap)
        if near is None:
            c.add("EMC-DEC-1", ERR, f"{cap} не на цепи {pad.net} вывода {ref}.{pin}", pad.xy)
            continue
        d = near[0]
        if c.stage == "full":
            cd = copper_distance(c, pad, near[1])
            if cd is not None:
                d = cd
        warn, err = pair.get("warn", 2.0), pair.get("err", 4.0)
        if d > err:
            c.add("EMC-DEC-1", ERR, f"{cap} в {d:.2f} мм от {ref}.{pin} ({pad.net})", pad.xy, round(d, 2), err)
        elif d > warn:
            c.add("EMC-DEC-1", WARN, f"{cap} в {d:.2f} мм от {ref}.{pin} ({pad.net})", pad.xy, round(d, 2), warn)


@rule("EMC-DEC-5", "placement", "буферный конденсатор модуля у выводов питания")
def dec5(c):
    for bulk in c.d.get("bulk", []):
        cap = bulk["cap"]
        if cap not in c.fps:
            continue
        for ref, pin in bulk["pins"]:
            if ref not in c.fps:
                continue
            pad = c.b.footprints[ref].pad(pin)
            near = _nearest_same_net(c, pad, cap)
            if near is None:
                continue
            d = near[0]
            mx = float(bulk.get("max_mm", 5.0))
            if d > 2 * mx:
                c.add("EMC-DEC-5", ERR, f"{cap} в {d:.1f} мм от {ref}.{pin}", pad.xy, round(d, 1), 2 * mx)
            elif d > mx:
                c.add("EMC-DEC-5", WARN, f"{cap} в {d:.1f} мм от {ref}.{pin}", pad.xy, round(d, 1), mx)


@rule("EMC-XTAL-1", "placement", "кварц у МК: расстояние площадка — вывод")
def xtal1(c):
    for x in c.d.get("crystals", []):
        if x["ref"] not in c.fps or x["mcu"] not in c.fps:
            continue
        for p in c.b.footprints[x["ref"]].pads():
            if not p.net or p.net in c.gnd:
                continue
            near = _nearest_same_net(c, p, x["mcu"])
            if near is None:
                continue
            d = near[0]
            if c.stage == "full":
                cd = copper_distance(c, p, near[1])
                d = cd if cd is not None else d
            if d > 10:
                c.add("EMC-XTAL-1", ERR, f"{x['ref']}.{p.num} в {d:.1f} мм от {x['mcu']}", p.xy, round(d, 1), 10)
            elif d > 5:
                c.add("EMC-XTAL-1", WARN, f"{x['ref']}.{p.num} в {d:.1f} мм от {x['mcu']}", p.xy, round(d, 1), 5)


@rule("EMC-XTAL-2", "placement", "чужие площадки не ближе 1 мм к площадкам кварца")
def xtal2(c):
    for x in c.d.get("crystals", []):
        if x["ref"] not in c.fps:
            continue
        nets = {p.net for p in c.b.footprints[x["ref"]].pads() if p.net}
        own = {x["ref"], x.get("mcu")} | set(x.get("caps", []))
        zone = unary_union([c.padpoly(p) for p in c.b.footprints[x["ref"]].pads()]).buffer(1.0)
        for p in c.pads:
            if p.fp.ref in own or p.net in nets or p.net in c.gnd:
                continue
            if zone.intersects(c.padpoly(p)):
                c.add("EMC-XTAL-2", ERR, f"{p.fp.ref}.{p.num} ({p.net}) ближе 1 мм к кварцу {x['ref']}", p.xy)
        if c.stage in ("critical", "full"):
            for t in c.tracks("F.Cu"):
                if t.net not in nets and t.net not in c.gnd and zone.intersects(t.geom()):
                    c.add("EMC-XTAL-2", ERR, f"дорожка {t.net} ближе 1 мм к кварцу {x['ref']}", t.start)


@rule("EMC-XTAL-5", "placement", "кварц не у кромки платы")
def xtal5(c):
    if c.outline is None:
        return
    for x in c.d.get("crystals", []):
        if x["ref"] not in c.fps:
            continue
        d = c.court(x["ref"]).distance(c.outline.exterior)
        if d < 1.5:
            c.add("EMC-XTAL-5", ERR, f"{x['ref']} в {d:.1f} мм от кромки", c.court(x["ref"]).centroid.coords[0], round(d, 2), 1.5)
        elif d < 3:
            c.add("EMC-XTAL-5", WARN, f"{x['ref']} в {d:.1f} мм от кромки", c.court(x["ref"]).centroid.coords[0], round(d, 2), 3)


@rule("EMC-RF-5", "placement", "в запретной зоне антенны нет деталей, площадок и меди")
def rf5(c):
    for z in c.d.get("zones", []):
        if z.get("kind") != "antenna_keepout":
            continue
        zp = prep(Polygon(z["poly"]))
        allowed = set(z.get("allow", []))
        for ref in c.fps:
            if ref not in allowed and zp.intersects(c.court(ref)):
                c.add("EMC-RF-5", ERR, f"{ref} в запретной зоне антенны {z['name']}", c.court(ref).centroid.coords[0])
        for t in c.b.tracks:
            if zp.intersects(t.geom()):
                c.add("EMC-RF-5", ERR, f"дорожка {t.net} в запретной зоне {z['name']}", t.start)
        for zz in c.b.zones:
            if zz.keepout is None:
                for layer, polys in zz.filled.items():
                    if any(zp.intersects(pp) for pp in polys):
                        c.add("EMC-RF-5", ERR, f"заливка {zz.net} {layer} в запретной зоне {z['name']}", z["poly"][0])


@rule("EMC-RF-9", "placement", "антенные разъёмы разных радио не ближе 20 мм")
def rf9(c):
    ants = [r for r in c.d.get("antenna_connectors", []) if r in c.fps]
    for i in range(len(ants)):
        for j in range(i + 1, len(ants)):
            d = c.court(ants[i]).distance(c.court(ants[j]))
            if d < 20:
                c.add("EMC-RF-9", WARN, f"{ants[i]} и {ants[j]}: {d:.1f} мм", c.court(ants[i]).centroid.coords[0], round(d, 1), 20)


def _port_pins(c):
    """Контакты кабельных разъёмов с сигнальными цепями (не земля)."""
    for port in c.d.get("ports", []):
        if not port.get("cable"):
            continue
        fp = c.b.footprints.get(port["ref"])
        if fp is None:
            continue
        for p in fp.pads():
            if p.net and p.net not in c.gnd:
                yield port, p


@rule("EMC-PORT-1", "placement", "защита — первая деталь по цепи от контакта разъёма")
def port1(c):
    prot = set(c.d.get("protection", []))
    for port, p in _port_pins(c):
        others = [q for q in c.pads_by_net[p.net] if q.fp.ref != p.fp.ref]
        if not others:
            continue
        if c.stage == "full":
            order = copper_order(c, p)
            first = order[0] if order else None
        else:
            first = min(others, key=lambda q: c.padpoly(p).distance(c.padpoly(q)))
        if first is None:
            continue
        if first.fp.ref not in prot:
            has = any(q.fp.ref in prot for q in others)
            c.add("EMC-PORT-1", ERR, f"{p.fp.ref}.{p.num} ({p.net}): первой по цепи стоит {first.fp.ref}, а не защита"
                  + ("" if has else " — защиты на цепи нет вовсе"), p.xy)


@rule("EMC-PORT-2", "placement", "защита в пределах 5–10 мм от контакта разъёма")
def port2(c):
    prot = set(c.d.get("protection", []))
    for port, p in _port_pins(c):
        cands = [q for q in c.pads_by_net[p.net] if q.fp.ref in prot]
        if not cands:
            continue
        d = min(c.padpoly(p).distance(c.padpoly(q)) for q in cands)
        if d > 10:
            c.add("EMC-PORT-2", ERR, f"{p.fp.ref}.{p.num} ({p.net}): защита в {d:.1f} мм", p.xy, round(d, 1), 10)
        elif d > 5:
            c.add("EMC-PORT-2", WARN, f"{p.fp.ref}.{p.num} ({p.net}): защита в {d:.1f} мм", p.xy, round(d, 1), 5)


@rule("EMC-PORT-4", "placement", "кабельные разъёмы не на противоположных кромках")
def port4(c):
    edges = {p.get("edge") for p in c.d.get("ports", []) if p.get("cable")}
    for a, b in (("top", "bottom"), ("left", "right")):
        if a in edges and b in edges:
            c.add("EMC-PORT-4", WARN, f"кабельные разъёмы на противоположных кромках: {a} и {b}")


@rule("EMC-PORT-6", "placement", "кабельный разъём не ближе 10 мм к кварцу, ВЧ и МК")
def port6(c):
    sens = set(c.d.get("sensitive_parts", []))
    for x in c.d.get("crystals", []):
        sens.add(x["ref"])
    for port in c.d.get("ports", []):
        if not port.get("cable") or port["ref"] not in c.fps:
            continue
        for s in sens:
            if s not in c.fps:
                continue
            d = c.court(port["ref"]).distance(c.court(s))
            if d < 5:
                c.add("EMC-PORT-6", ERR, f"{port['ref']} в {d:.1f} мм от {s}", c.court(port["ref"]).centroid.coords[0], round(d, 1), 5)
            elif d < 10:
                c.add("EMC-PORT-6", WARN, f"{port['ref']} в {d:.1f} мм от {s}", c.court(port["ref"]).centroid.coords[0], round(d, 1), 10)


@rule("EMC-SMPS-1", "placement", "площадь горячей петли: Cвх — выводы VIN и GND ключа")
def smps1(c):
    for s in c.d.get("smps", []):
        pts = []
        for ref, pin in s.get("loop_pins", []):
            if ref in c.fps:
                pts.append(c.b.footprints[ref].pad(pin).xy)
        if len(pts) < 3:
            continue
        area = Polygon(pts).convex_hull.area
        if area > 30:
            c.add("EMC-SMPS-1", ERR, f"{s['name']}: горячая петля {area:.1f} мм2", pts[0], round(area, 1), 30)
        elif area > 15:
            c.add("EMC-SMPS-1", WARN, f"{s['name']}: горячая петля {area:.1f} мм2", pts[0], round(area, 1), 15)


@rule("EMC-SMPS-5", "placement", "делитель обратной связи у вывода FB")
def smps5(c):
    for s in c.d.get("smps", []):
        if not s.get("fb_pin"):
            continue
        ref, pin = s["fb_pin"]
        if ref not in c.fps:
            continue
        pad = c.b.footprints[ref].pad(pin)
        for r in s.get("fb_divider", []):
            near = _nearest_same_net(c, pad, r)
            if near and near[0] > 6:
                c.add("EMC-SMPS-5", ERR, f"{r} в {near[0]:.1f} мм от FB {ref}", pad.xy, round(near[0], 1), 6)
            elif near and near[0] > 3:
                c.add("EMC-SMPS-5", WARN, f"{r} в {near[0]:.1f} мм от FB {ref}", pad.xy, round(near[0], 1), 3)


@rule("EMC-SMPS-6", "placement", "под дросселем преобразователя нет чужих площадок и меди")
def smps6(c):
    for s in c.d.get("smps", []):
        ind = s.get("inductor")
        if ind not in c.fps:
            continue
        own = {p.net for p in c.b.footprints[ind].pads()}
        zone = prep(c.court(ind))
        for p in c.pads:
            if p.fp.ref != ind and p.net not in own and p.net not in c.gnd and zone.intersects(c.padpoly(p)):
                c.add("EMC-SMPS-6", ERR, f"{p.fp.ref}.{p.num} ({p.net}) под дросселем {ind}", p.xy)
        for t in c.b.tracks:
            if t.net not in own and t.net not in c.gnd and zone.intersects(t.geom()):
                c.add("EMC-SMPS-6", ERR, f"дорожка {t.net} {t.layer} под дросселем {ind}", t.start)


# =============================================================================
# ЭМС: медь (критичные цепи и полная трассировка)
# =============================================================================

def copper_graph(c, net):
    """Элементы меди цепи и их связность: площадки, дорожки, переходные."""
    items = []
    for p in c.pads_by_net.get(net, []):
        items.append(("pad", p, c.padpoly(p), set(p.copper_layers())))
    for t in c.b.tracks:
        if t.net == net:
            items.append(("trk", t, t.geom(), {t.layer}))
    for v in c.b.vias:
        if v.net == net:
            items.append(("via", v, v.geom(), {"F.Cu", "B.Cu"}))
    adj = defaultdict(list)
    geoms = [it[2] for it in items]
    tree = STRtree(geoms) if geoms else None
    for i, it in enumerate(items):
        if tree is None:
            break
        for j in tree.query(it[2]):
            j = int(j)
            if j <= i or not (it[3] & items[j][3]):
                continue
            if it[2].intersects(items[j][2]):
                a = _center(items[i])
                b = _center(items[j])
                w = math.dist(a, b)
                adj[i].append((j, w))
                adj[j].append((i, w))
    return items, adj


def _center(it):
    kind, obj = it[0], it[1]
    if kind == "pad":
        return obj.xy
    if kind == "via":
        return obj.at
    return ((obj.start[0] + obj.end[0]) / 2, (obj.start[1] + obj.end[1]) / 2)


def _dijkstra(adj, src):
    dist = {src: 0.0}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18):
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def copper_distance(c, a, b):
    items, adj = copper_graph(c, a.net)
    ia = next((i for i, it in enumerate(items) if it[0] == "pad" and it[1].fp.ref == a.fp.ref and it[1].num == a.num), None)
    ib = next((i for i, it in enumerate(items) if it[0] == "pad" and it[1].fp.ref == b.fp.ref and it[1].num == b.num), None)
    if ia is None or ib is None:
        return None
    return _dijkstra(adj, ia).get(ib)


def copper_order(c, pad):
    """Площадки других деталей цепи в порядке расстояния по меди от pad."""
    items, adj = copper_graph(c, pad.net)
    src = next((i for i, it in enumerate(items) if it[0] == "pad" and it[1].fp.ref == pad.fp.ref and it[1].num == pad.num), None)
    if src is None:
        return []
    dist = _dijkstra(adj, src)
    out = [(d, items[i][1]) for i, d in dist.items() if items[i][0] == "pad" and items[i][1].fp.ref != pad.fp.ref]
    return [p for _, p in sorted(out, key=lambda x: x[0])]


@rule("EMC-RF-2", "critical", "на ВЧ-цепи нет переходных")
def rf2(c):
    rfnets = {n for n, r in c.roles.items() if r == "RF"}
    for v in c.b.vias:
        if v.net in rfnets:
            c.add("EMC-RF-2", ERR, f"переходное на ВЧ-цепи {v.net}", v.at)


@rule("EMC-RF-3", "critical", "излом ВЧ-линии не больше 45°")
def rf3(c):
    rfnets = {n for n, r in c.roles.items() if r == "RF"}
    for net in rfnets:
        segs = [t for t in c.b.tracks if t.net == net and t.kind == "segment"]
        for i, a in enumerate(segs):
            for b in segs[i + 1:]:
                for pa, qa in ((a.start, a.end), (a.end, a.start)):
                    for pb, qb in ((b.start, b.end), (b.end, b.start)):
                        if math.dist(pa, pb) < 0.01:
                            v1 = (qa[0] - pa[0], qa[1] - pa[1])
                            v2 = (qb[0] - pb[0], qb[1] - pb[1])
                            n1, n2 = math.hypot(*v1), math.hypot(*v2)
                            if n1 < 1e-6 or n2 < 1e-6:
                                continue
                            turn = 180 - math.degrees(math.acos(max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / n1 / n2))))
                            if turn >= 89:
                                c.add("EMC-RF-3", ERR, f"{net}: излом {turn:.0f}°", pa, round(turn), 45)
                            elif turn > 46:
                                c.add("EMC-RF-3", WARN, f"{net}: излом {turn:.0f}°", pa, round(turn), 45)


@rule("EMC-SMPS-3", "critical", "узел SW только на F.Cu и без переходных")
def smps3(c):
    for s in c.d.get("smps", []):
        sw = s.get("sw_net")
        if not sw:
            continue
        for t in c.b.tracks:
            if t.net == sw and t.layer != "F.Cu":
                c.add("EMC-SMPS-3", ERR, f"{sw} на {t.layer}", t.start)
        for v in c.b.vias:
            if v.net == sw:
                c.add("EMC-SMPS-3", ERR, f"переходное на {sw}", v.at)


@rule("EMC-PWR-1", "critical", "ширина дорожки не меньше расчётной по току (IPC-2221)")
def pwr1(c):
    cur = c.d.get("currents", {})
    for t in c.b.tracks:
        if t.net in cur:
            need = ipc2221_width(float(cur[t.net]))
            if t.width + 1e-6 < need:
                c.add("EMC-PWR-1", ERR, f"{t.net}: дорожка {t.width} мм, нужно {need:.2f} мм на {cur[t.net]} А",
                      t.start, t.width, round(need, 2))


@rule("EMC-GND-1", "full", "доля B.Cu под сплошной землёй")
def gnd1(c):
    if c.outline is None:
        return
    keep = unary_union([Polygon(z["poly"]) for z in c.d.get("zones", []) if z.get("kind") == "antenna_keepout"] +
                       [Point(h["at"]).buffer(h.get("keepout_d", 8.5) / 2) for h in c.d.get("holes", [])])
    area = c.outline.difference(keep).area
    fill = c.gnd_fill("B.Cu").intersection(c.outline).area
    share = fill / area if area else 0
    if share < 0.70:
        c.add("EMC-GND-1", ERR, f"земля на B.Cu покрывает {share:.0%}", c.outline.centroid.coords[0], round(share, 3), 0.70)
    elif share < 0.85:
        c.add("EMC-GND-1", WARN, f"земля на B.Cu покрывает {share:.0%}", c.outline.centroid.coords[0], round(share, 3), 0.85)


@rule("EMC-GND-2", "full", "под дорожкой F.Cu в полосе w+2h сплошная земля B.Cu")
def gnd2(c):
    fill = c.gnd_fill("B.Cu")
    pf = prep(fill)
    step = 0.25
    for t in c.tracks("F.Cu"):
        if t.net in c.gnd:
            continue
        r = t.width / 2 + c.h
        own = [c.padpoly(p) for p in c.pads_by_net.get(t.net, []) if "B.Cu" in p.copper_layers()]
        own += [v.geom() for v in c.b.vias if v.net == t.net]
        exempt = unary_union(own).buffer(c.h + 0.5) if own else Polygon()
        L = t.length()
        n = max(1, int(L / step))
        bad = 0.0
        where = None
        for k in range(n + 1):
            x = t.start[0] + (t.end[0] - t.start[0]) * k / n
            y = t.start[1] + (t.end[1] - t.start[1]) * k / n
            pt = Point(x, y)
            if not exempt.is_empty and exempt.contains(pt):
                continue
            if not pf.contains(pt.buffer(r, quad_segs=2)):
                bad += L / n
                where = where or (x, y)
        if bad <= 0:
            continue
        fast = c.role(t.net) in FAST_ROLES
        if fast and bad > 1.0:
            c.add("EMC-GND-2", ERR, f"{t.net} ({c.role(t.net)}): {bad:.1f} мм без земли под дорожкой", where, round(bad, 2), 1.0)
        elif not fast and bad > 10:
            c.add("EMC-GND-2", ERR, f"{t.net}: {bad:.1f} мм без земли под дорожкой", where, round(bad, 2), 10)
        elif not fast and bad > 3:
            c.add("EMC-GND-2", WARN, f"{t.net}: {bad:.1f} мм без земли под дорожкой", where, round(bad, 2), 3)


@rule("EMC-GND-3", "full", "обход обратного тока по земле B.Cu не длиннее прямого пути")
def gnd3(c):
    fill = c.gnd_fill("B.Cu")
    if fill.is_empty:
        return
    res = 0.25
    x0, y0, x1, y1 = c.outline.bounds
    W, H = int((x1 - x0) / res) + 2, int((y1 - y0) / res) + 2
    pf = prep(fill)
    grid = bytearray(W * H)
    for j in range(H):
        yy = y0 + j * res
        for i in range(W):
            if pf.contains(Point(x0 + i * res, yy)):
                grid[j * W + i] = 1

    def cell(x, y):
        return int(round((x - x0) / res)), int(round((y - y0) / res))

    def nearest_free(i, j):
        for rr in range(0, 12):
            for dj in range(-rr, rr + 1):
                for di in range(-rr, rr + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < W and 0 <= jj < H and grid[jj * W + ii]:
                        return ii, jj
        return None

    for t in c.tracks("F.Cu"):
        if t.net in c.gnd or t.length() < 2.0:
            continue
        a = nearest_free(*cell(*t.start))
        b = nearest_free(*cell(*t.end))
        if a is None or b is None:
            continue
        straight = t.length()
        limit_cells = int(straight * 3.5 / res) + 20
        dist = {a: 0}
        dq = deque([a])
        found = None
        while dq:
            u = dq.popleft()
            if u == b:
                found = dist[u]
                break
            du = dist[u]
            if du > limit_cells:
                break
            ui, uj = u
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ii, jj = ui + di, uj + dj
                if 0 <= ii < W and 0 <= jj < H and grid[jj * W + ii] and (ii, jj) not in dist:
                    dist[(ii, jj)] = du + 1
                    dq.append((ii, jj))
        detour = (found * res / 1.27) if found is not None else float("inf")   # манхэттен -> ~евклид
        ratio = detour / straight
        fast = c.role(t.net) in FAST_ROLES
        e, w = (1.5, 1.2) if fast else (3.0, 2.0)
        mid = ((t.start[0] + t.end[0]) / 2, (t.start[1] + t.end[1]) / 2)
        if ratio > e:
            c.add("EMC-GND-3", ERR, f"{t.net}: обход по земле в {ratio:.1f} раза длиннее дорожки", mid,
                  round(ratio, 2) if ratio != float("inf") else None, e)
        elif ratio > w:
            c.add("EMC-GND-3", WARN, f"{t.net}: обход по земле в {ratio:.1f} раза длиннее дорожки", mid, round(ratio, 2), w)


def _chains(tracks):
    """Связные цепочки дорожек одного слоя и цепи -> [(длина, [дорожки])]."""
    by = defaultdict(list)
    for t in tracks:
        by[(t.net, t.layer)].append(t)
    out = []
    for key, ts in by.items():
        parent = list(range(len(ts)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                a, b = ts[i], ts[j]
                if min(math.dist(p, q) for p in (a.start, a.end) for q in (b.start, b.end)) < 0.01:
                    parent[find(i)] = find(j)
        groups = defaultdict(list)
        for i, t in enumerate(ts):
            groups[find(i)].append(t)
        for g in groups.values():
            out.append((sum(t.length() for t in g), g))
    return out


@rule("EMC-BOT-1", "full", "перемычка на B.Cu короче 5–10 мм и λэфф/20")
def bot1(c):
    for L, g in _chains(c.tracks("B.Cu")):
        if g[0].net in c.gnd:
            continue
        lim = min(10.0, lam_eff(c.fmax_at(g[0].start), c.eps) / 20)
        if L > lim:
            c.add("EMC-BOT-1", ERR, f"перемычка {g[0].net} на B.Cu {L:.1f} мм", g[0].start, round(L, 1), round(lim, 1))
        elif L > 5:
            c.add("EMC-BOT-1", WARN, f"перемычка {g[0].net} на B.Cu {L:.1f} мм", g[0].start, round(L, 1), 5)


@rule("EMC-BOT-2", "full", "щель в земле B.Cu короче λэфф/20 своей зоны")
def bot2(c):
    if c.outline is None:
        return
    edge = float(c.d.get("profile", {}).get("edge", 0.5)) + 0.1
    inner = c.outline.buffer(-edge)
    keep = unary_union([Polygon(z["poly"]) for z in c.d.get("zones", []) if z.get("kind") == "antenna_keepout"] +
                       [Point(h["at"]).buffer(h.get("keepout_d", 8.5) / 2 + 0.3) for h in c.d.get("holes", [])])
    gaps = inner.difference(c.gnd_fill("B.Cu")).difference(keep)
    geoms = getattr(gaps, "geoms", [gaps])
    for g in geoms:
        if g.is_empty or g.area < 0.5:
            continue
        mrr = g.minimum_rotated_rectangle
        xs = list(mrr.exterior.coords)
        sides = [math.dist(xs[i], xs[i + 1]) for i in range(4)] if len(xs) >= 5 else [0]
        L = max(sides)
        if L < 2.0:
            continue
        p = g.representative_point()
        lam = lam_eff(c.fmax_at((p.x, p.y)), c.eps)
        if L > lam / 20:
            c.add("EMC-BOT-2", ERR, f"щель в земле B.Cu {L:.1f} мм", (p.x, p.y), round(L, 1), round(lam / 20, 1))
        elif L > lam / 40:
            c.add("EMC-BOT-2", WARN, f"щель в земле B.Cu {L:.1f} мм", (p.x, p.y), round(L, 1), round(lam / 40, 1))


@rule("EMC-BOT-3", "full", "нет перемычек B.Cu в ВЧ-зоне, под кварцем, модулем, дросселем")
def bot3(c):
    forbid = [Polygon(z["poly"]) for z in c.d.get("zones", []) if z.get("kind") in ("rf", "no_bottom")]
    for ref in c.d.get("no_bottom_under", []):
        if ref in c.b.footprints:
            forbid.append(c.court(ref))
    if not forbid:
        return
    area = prep(unary_union(forbid))
    for t in c.tracks("B.Cu"):
        if t.net not in c.gnd and area.intersects(t.geom()):
            c.add("EMC-BOT-3", ERR, f"перемычка {t.net} на B.Cu в запретной области", t.start)


@rule("EMC-BOT-4", "full", "суммарная длина B.Cu на цепь не больше 10 мм")
def bot4(c):
    tot = defaultdict(float)
    where = {}
    for t in c.tracks("B.Cu"):
        if t.net not in c.gnd:
            tot[t.net] += t.length()
            where.setdefault(t.net, t.start)
    for net, L in tot.items():
        if L > 10:
            c.add("EMC-BOT-4", WARN, f"{net}: на B.Cu {L:.1f} мм", where[net], round(L, 1), 10)


def _gnd_vias_near(c, geom, radius):
    pts = [v for v in c.b.vias if v.net in c.gnd]
    return [v for v in pts if geom.distance(Point(v.at)) - v.size / 2 <= radius]


@rule("EMC-DEC-2", "full", "переходное на землю у земляной площадки конденсатора развязки")
def dec2(c):
    for pair in c.d.get("decoupling", []):
        cap = pair["cap"]
        if cap not in c.fps:
            continue
        for p in c.b.footprints[cap].pads():
            if p.net in c.gnd:
                g = c.padpoly(p)
                vias = [v for v in c.b.vias if v.net in c.gnd]
                thru = [q for q in c.pads_by_net.get(p.net, []) if "B.Cu" in q.copper_layers() and q is not p]
                ds = [g.distance(Point(v.at)) - v.size / 2 for v in vias] + [g.distance(c.padpoly(q)) for q in thru]
                d = min(ds) if ds else float("inf")
                if d > 1.5:
                    c.add("EMC-DEC-2", ERR, f"{cap}.{p.num}: переходное на землю в {d:.1f} мм", p.xy, round(d, 2) if d != float('inf') else None, 1.5)
                elif d > 0.7:
                    c.add("EMC-DEC-2", WARN, f"{cap}.{p.num}: переходное на землю в {d:.1f} мм", p.xy, round(d, 2), 0.7)


@rule("EMC-XTAL-3", "full", "под кварцем и его конденсаторами на B.Cu только земля")
def xtal3(c):
    for x in c.d.get("crystals", []):
        area = unary_union([c.court(r) for r in [x["ref"]] + x.get("caps", []) if r in c.b.footprints])
        pa = prep(area)
        for t in c.tracks("B.Cu"):
            if t.net not in c.gnd and pa.intersects(t.geom()):
                c.add("EMC-XTAL-3", ERR, f"дорожка {t.net} на B.Cu под кварцем {x['ref']}", t.start)
        for v in c.b.vias:
            if v.net not in c.gnd and v.net not in {p.net for p in c.b.footprints[x["ref"]].pads()} and pa.intersects(v.geom()):
                c.add("EMC-XTAL-3", ERR, f"переходное {v.net} под кварцем {x['ref']}", v.at)


@rule("EMC-RF-6", "full", "чужая медь не ближе зазора линии плюс 1 мм к ВЧ-дорожке")
def rf6(c):
    rf = c.d.get("rf_lines", [])
    for line in rf:
        net = line["net"]
        gap = float(line.get("gap", 0.3))
        segs = [t.geom() for t in c.b.tracks if t.net == net and t.layer == "F.Cu"]
        if not segs:
            continue
        zone = prep(unary_union(segs).buffer(gap + 1.0))
        for t in c.tracks("F.Cu"):
            if t.net != net and t.net not in c.gnd and zone.intersects(t.geom()):
                c.add("EMC-RF-6", ERR, f"дорожка {t.net} рядом с ВЧ-линией {net}", t.start)


@rule("EMC-RF-8", "full", "два переходных на землю у каждой земляной площадки антенного разъёма")
def rf8(c):
    for ref in c.d.get("antenna_connectors", []):
        if ref not in c.fps:
            continue
        for p in c.b.footprints[ref].pads():
            if p.net in c.gnd:
                n = len(_gnd_vias_near(c, c.padpoly(p), 1.5))
                if n == 0:
                    c.add("EMC-RF-8", ERR, f"{ref}.{p.num}: нет переходных на землю", p.xy, 0, 2)
                elif n < 2:
                    c.add("EMC-RF-8", WARN, f"{ref}.{p.num}: одно переходное на землю", p.xy, n, 2)


@rule("EMC-PORT-3", "full", "переходные на землю у земляной площадки защиты")
def port3(c):
    for ref in c.d.get("protection", []):
        if ref not in c.fps:
            continue
        for p in c.b.footprints[ref].pads():
            if p.net in c.gnd:
                g = c.padpoly(p)
                near = _gnd_vias_near(c, g, 1.0)
                vias = [v for v in c.b.vias if v.net in c.gnd]
                d = min((g.distance(Point(v.at)) - v.size / 2 for v in vias), default=float("inf"))
                if not near and d > 2:
                    c.add("EMC-PORT-3", ERR, f"{ref}.{p.num}: переходное на землю в {d:.1f} мм", p.xy)
                elif len(near) < 2:
                    c.add("EMC-PORT-3", WARN, f"{ref}.{p.num}: переходных на землю рядом {len(near)}", p.xy, len(near), 2)


@rule("EMC-EDGE-1", "full", "быстрые цепи не ближе 2–3 мм к кромке, остальные 1–1,5 мм")
def edge1(c):
    if c.outline is None:
        return
    ring = c.outline.exterior
    for t in c.b.tracks:
        if t.net in c.gnd:
            continue
        d = t.geom().distance(ring)
        fast = c.role(t.net) in FAST_ROLES
        e, w = (2.0, 3.0) if fast else (1.0, 1.5)
        code = "EMC-EDGE-1" if fast else "EMC-EDGE-2"
        if d < e:
            c.add(code, ERR, f"{t.net} в {d:.2f} мм от кромки", t.start, round(d, 2), e)
        elif d < w:
            c.add(code, WARN, f"{t.net} в {d:.2f} мм от кромки", t.start, round(d, 2), w)


@rule("EMC-EDGE-3", "full", "шаг переходных, сшивающих землю по периметру")
def edge3(c):
    if c.outline is None:
        return
    ring = c.outline.exterior
    L = ring.length
    pos = sorted(ring.project(Point(v.at)) for v in c.b.vias
                 if v.net in c.gnd and ring.distance(Point(v.at)) < 4.0)
    lam = lam_eff(float(c.d.get("fmax_board_mhz", 175)), c.eps)
    if not pos:
        c.add("EMC-EDGE-3", ERR, "по периметру нет переходных сшивки земли", ring.coords[0])
        return
    gaps = [(pos[i + 1] - pos[i], pos[i]) for i in range(len(pos) - 1)] + [(L - pos[-1] + pos[0], pos[-1])]
    g, at = max(gaps)
    p = ring.interpolate(at + g / 2)
    lim_w = min(lam / 20, float(c.d.get("stitch_edge_mm", 10.0)))
    if g > lam / 10:
        c.add("EMC-EDGE-3", ERR, f"разрыв сшивки по периметру {g:.1f} мм", (p.x, p.y), round(g, 1), round(lam / 10, 1))
    elif g > lim_w:
        c.add("EMC-EDGE-3", WARN, f"разрыв сшивки по периметру {g:.1f} мм", (p.x, p.y), round(g, 1), round(lim_w, 1))


@rule("EMC-ISL-1", "full", "нет островов заливки без связи с цепью")
def isl1(c):
    for z in c.b.zones:
        if z.keepout is not None or not z.net:
            continue
        conn = [c.padpoly(p) for p in c.pads_by_net.get(z.net, [])] + [v.geom() for v in c.b.vias if v.net == z.net]
        conn += [t.geom() for t in c.b.tracks if t.net == z.net]
        for layer, polys in z.filled.items():
            lconn = [g for g in conn]
            tree = STRtree(lconn) if lconn else None
            for poly in polys:
                hit = tree is not None and any(poly.intersects(lconn[int(i)]) for i in tree.query(poly))
                if not hit and poly.area > 0.2:
                    p = poly.representative_point()
                    c.add("EMC-ISL-1", ERR, f"остров заливки {z.net} на {layer}, {poly.area:.1f} мм2", (p.x, p.y))


@rule("EMC-ISL-2", "full", "остров заливки земли F.Cu связан с B.Cu двумя переходными")
def isl2(c):
    vias = [v for v in c.b.vias if v.net in c.gnd]
    thru = [p for n in c.gnd for p in c.pads_by_net.get(n, []) if "B.Cu" in p.copper_layers() and "F.Cu" in p.copper_layers()]
    for poly in getattr(c.gnd_fill("F.Cu"), "geoms", [c.gnd_fill("F.Cu")]):
        if poly.is_empty or poly.area < 2:
            continue
        n = sum(1 for v in vias if poly.intersects(Point(v.at))) + sum(1 for p in thru if poly.intersects(c.padpoly(p)))
        p = poly.representative_point()
        if n == 0:
            c.add("EMC-ISL-2", ERR, f"заливка земли F.Cu {poly.area:.0f} мм2 без переходных на B.Cu", (p.x, p.y), 0, 2)
        elif n == 1:
            c.add("EMC-ISL-2", WARN, f"заливка земли F.Cu {poly.area:.0f} мм2 с одним переходным", (p.x, p.y), 1, 2)


@rule("EMC-XTK-2", "full", "расстояние между дорожками цепей разных ролей")
def xtk2(c):
    ts = [(t, c.role(t.net)) for t in c.b.tracks]
    ts = [(t, r) for t, r in ts if r in {"SW", "CLK", "CABLE", "PULSE", "RF", "ANALOG", "RESET"}]
    geoms = [t.geom() for t, _ in ts]
    if not geoms:
        return
    tree = STRtree(geoms)
    seen = set()
    for i, (t, r) in enumerate(ts):
        for j in tree.query(geoms[i].buffer(5.0)):
            j = int(j)
            if j <= i:
                continue
            u, s = ts[j]
            if t.layer != u.layer or r == s or t.net == u.net:
                continue
            lim = xtk_limit(r, s)
            if not lim:
                continue
            d = geoms[i].distance(geoms[j])
            key = tuple(sorted((t.net, u.net)))
            if d < lim and key not in seen:
                seen.add(key)
                c.add("EMC-XTK-2", ERR, f"дорожки {t.net} ({r}) и {u.net} ({s}): {d:.2f} мм < {lim}",
                      t.start, round(d, 2), lim)


@rule("EMC-PWR-3", "full", "на цепи с током больше 1 А переходных не меньше тока в амперах")
def pwr3(c):
    cur = c.d.get("currents", {})
    for net, i in cur.items():
        if float(i) <= 1:
            continue
        if any(t.layer == "B.Cu" for t in c.b.tracks if t.net == net):
            vias = [v for v in c.b.vias if v.net == net]
            need = math.ceil(float(i))
            if len(vias) < need:
                c.add("EMC-PWR-3", WARN, f"{net}: переходных {len(vias)}, нужно не меньше {need} на смену слоя",
                      vias[0].at if vias else None, len(vias), need)


# =============================================================================

def run(board, desc, stage):
    c = Ctx(board, desc, stage)
    si = STAGES.index(stage)
    for code, st, what, fn in RULES:
        if STAGES.index(st) <= si:
            fn(c)
    exc = desc.get("exceptions", [])
    active, used = [], []
    for v in c.v:
        hit = next((e for e in exc if e.get("rule") == v["rule"] and e.get("object", "") in v["msg"]), None)
        if hit:
            v["exception"] = hit
            used.append(v)
        else:
            active.append(v)
    return active, used


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?")
    ap.add_argument("--desc")
    ap.add_argument("--stage", choices=STAGES, default="full")
    ap.add_argument("--json")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for code, st, what, _ in RULES:
            print(f"{code:12} с этапа {st:9} {what}")
        return 0
    if not a.board:
        ap.error("нужен файл платы")
    board = pcblib.Board.read(a.board)
    desc = json.load(open(a.desc, encoding="utf-8")) if a.desc else {}
    if not a.desc:
        print("ПРЕДУПРЕЖДЕНИЕ: без описания платы роли, зоны, порты и корпус неизвестны — "
              "большинство правил не проверяется")
    active, used = run(board, desc, a.stage)
    order = {ERR: 0, WARN: 1}
    for v in sorted(active, key=lambda v: (order[v["level"]], v["rule"])):
        w = f" @ {v['where'][0]:.1f},{v['where'][1]:.1f}" if v["where"] else ""
        print(f"{v['level'].upper()}: {v['rule']}: {v['msg']}{w}")
    for v in used:
        e = v["exception"]
        print(f"ИСКЛЮЧЕНИЕ: {v['rule']}: {v['msg']} — {e.get('reason', '')} "
              f"(согласовал {e.get('agreed', '?')}, {e.get('date', '?')})")
    n_err = sum(1 for v in active if v["level"] == ERR)
    n_warn = len(active) - n_err
    print(f"\nлинтер ({a.stage}): ошибок {n_err}, предупреждений {n_warn}, исключений {len(used)}")
    if a.json:
        json.dump(dict(stage=a.stage, violations=active, exceptions=used), open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
