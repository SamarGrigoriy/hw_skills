#!/usr/bin/env python3
"""
place.py — размещение деталей: разрешённые места и выбор лучшего по стоимости.

Почему не «всё автоматом». Где стоит микроконтроллер, радиомодуль или
преобразователь, решает разработчик по плану этажей: порты у кромок, ВЧ у
антенны, импульсные узлы вдали от аналоговых входов (reference/placement.md).
Эти решения записываются в описание платы явными координатами — их видно в
diff, их можно обсудить. Автоматика нужна там, где решений сотни и все мелкие:
куда встать конденсатору развязки, резистору подтяжки, супрессору. Для них
размещатель перебирает места вокруг нужного вывода и берёт самое дешёвое из
разрешённых.

Разрешено место, если courtyard детали:
  - целиком внутри контура платы (MNT-5);
  - не заходит в зоны запрета крепежа и запретные зоны описания (ENC-3, EMC-RF-5);
  - отстоит от courtyard уже размещённых деталей не меньше зазора монтажа (MNT-1).

Стоимость места (веса по приоритетам placement.md: ЭМС >> монтаж >> длина):
  - расстояние от площадки детали до целевого вывода — главное слагаемое;
  - длина остальных связей детали до уже размещённых площадок тех же цепей;
  - штраф ЭМС: площадки ролей из таблицы emc.md ближе порога к чужим ролям;
  - предпочтение ориентации блока (MNT-6) — мелкий штраф за «не ту» ориентацию.

Детерминизм: перебор мест и поворотов в фиксированном порядке, при равной
стоимости берётся первое — одинаковое описание даёт одинаковую плату.
"""

import math
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcblib  # noqa: E402

from shapely.geometry import Point, Polygon  # noqa: E402
from shapely.ops import unary_union  # noqa: E402
from shapely.prepared import prep  # noqa: E402

XTK = {
    ("SW", "RF"): 5, ("SW", "CLK"): 3, ("SW", "CABLE"): 3, ("SW", "ANALOG"): 5, ("SW", "RESET"): 3,
    ("CLK", "RF"): 3, ("CLK", "CABLE"): 3, ("CLK", "ANALOG"): 3, ("CLK", "RESET"): 2,
    ("CABLE", "RF"): 5, ("CABLE", "PULSE"): 2, ("CABLE", "ANALOG"): 3, ("CABLE", "RESET"): 3,
    ("PULSE", "RF"): 3, ("PULSE", "ANALOG"): 2, ("PULSE", "RESET"): 1,
    ("RF", "ANALOG"): 3, ("RF", "RESET"): 3,
}


class PlacementError(RuntimeError):
    pass


class Placer:
    def __init__(self, board, desc=None, gap=None, edge=0.0):
        self.b = board
        self.d = desc or {}
        self.gap = gap if gap is not None else (0.5 if self.d.get("assembly", "manual") == "manual" else 0.25)
        outline = board.outline_polygon()
        if outline is None:
            raise PlacementError("у платы нет контура")
        self.area = outline.buffer(-edge) if edge else outline
        forbid = []
        for h in self.d.get("holes", []):
            forbid.append(Point(h["at"]).buffer(float(h.get("keepout_d", 8.5)) / 2, quad_segs=16))
        for z in self.d.get("zones", []):
            if z.get("kind") in ("antenna_keepout", "no_parts"):
                forbid.append(Polygon(z["poly"]))
        self.forbid = unary_union(forbid) if forbid else Polygon()
        self.placed = {}                     # ref -> courtyard
        self.side = {}                       # ref -> "F" или "B": зазор нужен только своей стороне
        self.through = []                    # (ref, фигура) сквозных площадок: они мешают обеим сторонам
        self.roles = dict(self.d.get("roles", {}))
        self.gnd = set(self.d.get("gnd_nets", ["GND"]))
        self.power = set(self.d.get("power_nets", []))
        self.log = []
        self.failed = []

    # --- состояние ---------------------------------------------------------------
    def role(self, net):
        if not net or net in self.gnd:
            return "GND"
        return self.roles.get(net, "POWER" if net in self.power else "SLOW")

    def fix(self, *refs):
        """Отметить уже стоящие детали (из описания) как препятствия."""
        for r in refs:
            self.placed[r] = self.b.footprints[r].courtyard()
            self.side[r] = self.b.footprints[r].side
            self._add_through(r)

    def _add_through(self, ref):
        """Сквозные площадки детали: деталь с другой стороны на них встать не может."""
        for p in self.b.footprints[ref].pads():
            if p.kind in ("thru_hole", "np_thru_hole"):
                self.through.append((ref, p.polygon(0.25)))

    def conflicts(self, ref):
        fp = self.b.footprints[ref]
        c = fp.courtyard()
        out = []
        if not self.area.contains(c):
            out.append("вне контура")
        if not self.forbid.is_empty and c.intersects(self.forbid):
            out.append("в зоне запрета")
        for r, oc in self.placed.items():
            if r == ref or self.side.get(r, "F") != fp.side:
                continue                       # деталь на другой стороне платы не мешает
            if c.distance(oc) < self.gap - 1e-6:
                out.append(f"зазор с {r}")
        for r, g in self.through:
            if r != ref and c.intersects(g):
                out.append(f"сквозная площадка {r}")
        return out

    def put(self, ref, x, y, rot=0.0, strict=True):
        fp = self.b.footprints[ref]
        fp.x, fp.y, fp.rot = float(x), float(y), pcblib.norm_angle(rot)
        bad = self.conflicts(ref)
        if bad and strict:
            raise PlacementError(f"{ref} в ({x:.2f}, {y:.2f}, {rot}): {', '.join(bad)}")
        self.placed[ref] = fp.courtyard()
        self.side[ref] = fp.side
        self._add_through(ref)
        return bad

    # --- поиск места ----------------------------------------------------------------
    def _placed_pads(self, exclude):
        pads = {}
        for r in self.placed:
            if r == exclude or self.b.footprints[r].board_only:
                continue
            for p in self.b.footprints[r].pads():
                if p.net:
                    pads.setdefault(p.net, []).append(p.xy)
        return pads

    def near(self, ref, target, pin=None, rots=(0, 90, 180, 270), rmax=12.0, step=0.25,
             region=None, prefer_rot=None, away=None, weight_other=0.3):
        """Поставить ref как можно ближе к выводу target=(ref, pin).

        pin — площадка ref, которая должна быть у цели (по умолчанию площадка той же цепи).
        region — многоугольник, в котором разрешено стоять (дополнительно к контуру).
        away — [(x, y, радиус, штраф)] мягкое отталкивание от точек.
        Шаг перебора 0,25 мм в пределах 3 мм от цели и 0,5 мм дальше.
        """
        from shapely import affinity
        from shapely.strtree import STRtree
        fp = self.b.footprints[ref]
        tref, tpin = target
        tpad = self.b.footprints[tref].pad(tpin)
        tx, ty = tpad.xy
        lpads = fp.local_pads()
        my = None
        for item in lpads:
            num, net = item[0], item[1]
            if (pin is not None and num == str(pin)) or (pin is None and net and net == tpad.net):
                my = item
                break
        if my is None:
            raise PlacementError(f"{ref}: нет площадки на цепи {tpad.net} вывода {tref}.{tpin}")
        lcourt = fp.local_courtyard()
        others = self._placed_pads(ref)
        reg = prep(region) if region is not None else None
        tpoly = tpad.polygon()
        area = prep(self.area)
        forbid = prep(self.forbid) if not self.forbid.is_empty else None
        obst_refs = [r for r in self.placed if r != ref and self.side.get(r, "F") == fp.side]
        # ровно зазор, без послабления: с запасом внутрь перебор пропускал место,
        # которое потом не проходило put() и деталь оставалась не поставленной
        obst = [self.placed[r].buffer(self.gap, quad_segs=2) for r in obst_refs]
        obst += [g for r, g in self.through if r != ref]     # сквозные площадки с обеих сторон
        otree = STRtree(obst) if obst else None
        emc_items = []
        for r in obst_refs:
            for q in self.b.footprints[r].pads():
                role = self.role(q.net)
                if role not in ("GND", "POWER", "SLOW"):
                    emc_items.append((role, q.net, q.polygon()))
        etree = STRtree([e[2] for e in emc_items]) if emc_items else None

        def place_shape(shp, rot, x, y):
            if rot:
                shp = affinity.rotate(shp, -rot, origin=(0, 0))
            return affinity.translate(shp, x, y)

        offsets = []
        r = 0.0
        while r <= rmax + 1e-9:
            s = step if r < 3.0 else 2 * step
            k = int(round(r / s))
            ring = set()
            for i in range(-k, k + 1):
                for j in (-k, k):
                    ring.add((i * s, j * s))
                    ring.add((j * s, i * s))
            offsets.append((r, sorted(ring, key=lambda d: (math.hypot(*d), d))))
            r += s
        best = None
        cands = []
        seen = set()
        for r, ring in offsets:
            if best is not None and r > best[0] + 2.0:
                break
            for dx, dy in ring:
                cx, cy = round(tx + dx, 4), round(ty + dy, 4)
                for rot in rots:
                    key = (cx, cy, rot)
                    if key in seen:
                        continue
                    seen.add(key)
                    lx, ly = pcblib.rotate(my[2], my[3], rot)
                    x, y = cx - lx, cy - ly
                    c = place_shape(lcourt, rot, x, y)
                    if not area.contains(c):
                        continue
                    if forbid is not None and forbid.intersects(c):
                        continue
                    if reg is not None and not reg.contains(c):
                        continue
                    if otree is not None and any(obst[int(i)].intersects(c) for i in otree.query(c)):
                        continue
                    cost = 0.0
                    for num, net, px_, py_, shp, _p in lpads:
                        ax, ay = pcblib.rotate(px_, py_, rot)
                        apoly = place_shape(shp, rot, x, y) if (num == my[0] or etree is not None) else None
                        if num == my[0]:
                            cost += apoly.distance(tpoly)
                        elif net and net not in self.gnd and others.get(net):
                            cost += weight_other * min(math.hypot(x + ax - qx, y + ay - qy) for qx, qy in others[net])
                        role = self.role(net)
                        if etree is not None and role not in ("GND", "POWER", "SLOW") and apoly is not None:
                            for i in etree.query(apoly.buffer(5.0)):
                                r2, n2, g2 = emc_items[int(i)]
                                lim = XTK.get((role, r2)) or XTK.get((r2, role))
                                if lim and n2 != net:
                                    dd = apoly.distance(g2)
                                    if dd < lim:
                                        cost += 10.0 * (lim - dd)
                    if prefer_rot is not None and round(rot) % 180 != round(prefer_rot) % 180:
                        cost += 0.3
                    if away:
                        for ax_, ay_, ar, pen in away:
                            d = math.hypot(x - ax_, y - ay_)
                            if d < ar:
                                cost += pen * (ar - d) / ar
                    cands.append((cost, x, y, pcblib.norm_angle(rot)))
                    if best is None or cost < best[0] - 1e-9:
                        best = (cost, x, y, pcblib.norm_angle(rot))
        if best is None:
            raise PlacementError(f"{ref}: нет разрешённого места в радиусе {rmax} мм от {tref}.{tpin}")
        # перебор и put() считают зазор чуть по-разному (округление), поэтому лучшее место
        # проверяется строго, а не выйдет — берётся следующее по стоимости
        for cost, x, y, rot in sorted(cands, key=lambda c: c[0])[:40]:
            fp.x, fp.y, fp.rot = x, y, rot
            if not self.conflicts(ref):
                self.put(ref, x, y, rot)
                self.log.append((ref, round(cost, 2)))
                return (cost, x, y, rot)
        raise PlacementError(f"{ref}: место у {tref}.{tpin} не проходит проверку зазоров")

    def try_near(self, ref, target, **kw):
        """Как near, но неудача не прерывает размещение: причина копится в self.failed."""
        try:
            return self.near(ref, target, **kw)
        except PlacementError as e:
            self.failed.append(str(e))
            return None

    def row(self, refs, x, y, dx=0.0, dy=0.0, rot=0.0):
        """Ряд деталей с шагом (dx, dy) от (x, y)."""
        for i, r in enumerate(refs):
            self.put(r, x + i * dx, y + i * dy, rot)


def main(argv):
    if len(argv) >= 3 and argv[1] == "check":
        b = pcblib.Board.read(argv[2])
        pl = Placer(b)
        bad = 0
        for r, fp in b.footprints.items():
            if fp.board_only:
                continue
            pl.placed[r] = fp.courtyard()
            pl.side[r] = fp.side
            pl._add_through(r)
        for r in sorted(pl.placed, key=pcblib._ref_key):
            c = pl.conflicts(r)
            if c:
                bad += 1
                print(r, ", ".join(c))
        print("деталей с конфликтами:", bad)
        return 1 if bad else 0
    print(__doc__)
    print("использование: place.py check плата.kicad_pcb  — конфликты размещения")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
