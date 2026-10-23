#!/usr/bin/env python3
"""
route.py — трассировщик двухслойной платы с ЭМС-стоимостью пути (reference/router.md).

Почему свой, а не Freerouting. Чужой трассировщик не знает, что B.Cu — единственная
земля: он охотно пустит по ней длинную дорожку, разрежет плоскость и получит
«разведено 100 %» при плате, которая излучает. Здесь каждая клетка B.Cu дорогая,
а под кварцем, модулем и в ВЧ-зоне запрещена совсем — это часть стоимости пути,
а не проверка после.

Модель:
  - сетка шагом grid по обоим слоям; клетка — положение осевой линии дорожки;
  - занятость: для каждой клетки номер цепи меди, которая закрывает её с учётом
    половины ширины дорожки, зазора и запаса на округление сетки; клетка в зазоре
    двух разных цепей закрыта для обеих;
  - отдельная карта для переходных: радиус площадки переходного плюс зазор;
  - зоны запрета платы и зоны внутри посадочных мест (антенна, датчик) — запрет;
  - ход в восемь сторон; поворот на 45° — малый штраф, на 90° и больше — запрет;
  - переходное — смена слоя в той же клетке со своей стоимостью;
  - многоточечная цепь растёт деревом: к меди цепи подключается ближайшая
    неподключённая площадка;
  - земляные SMD-площадки не трассируются: у каждой ставится переходное на
    плоскость B.Cu вплотную к площадке (EMC-DEC-2, EMC-PORT-3).

Перетрассировка: связь, которой не нашлось пути, называет цепи, чья медь лежит в
её коридоре; они снимаются (кроме закреплённых и критичных), неудачная связь
разводится первой, снятые — за ней. Проходов не больше max_passes. Неразведённое
не маскируется: отчёт называет связи и место, код возврата ненулевой.

Детерминизм: порядок цепей, связей и ходов фиксирован — одинаковый вход даёт
одинаковые дорожки.
"""

import heapq
import json
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pcblib  # noqa: E402

import shapely  # noqa: E402
from shapely.geometry import LineString, Point, Polygon  # noqa: E402

LAYERS = ("F.Cu", "B.Cu")
_CORE = None


def load_core():
    """Ядро A* на C (astar_core.c): собирается gcc в ~/.cache/kicad-pcb при первом вызове.

    Нет компилятора или сборка не удалась — возвращает None, и трассировщик работает
    на Python-версии поиска: так же, но в десятки раз медленнее.
    """
    global _CORE
    if _CORE is not None:
        return _CORE or None
    import ctypes
    import hashlib
    import subprocess
    src = os.path.join(HERE, "astar_core.c")
    try:
        digest = hashlib.sha1(open(src, "rb").read()).hexdigest()[:12]
        cache = os.path.join(os.path.expanduser("~"), ".cache", "kicad-pcb")
        os.makedirs(cache, exist_ok=True)
        so = os.path.join(cache, f"astar_core-{digest}.so")
        if not os.path.exists(so):
            r = subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", so, src, "-lm"],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stderr)
        lib = ctypes.CDLL(so)
        P = ctypes.c_void_p
        lib.astar.argtypes = [ctypes.c_int, ctypes.c_int, P, P, P, P, P, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_long,
                              P, ctypes.c_int, P, P, P]
        lib.astar.restype = ctypes.c_int
        _CORE = lib
    except Exception as e:                       # noqa: BLE001 — любая причина ведёт к Python-версии
        print("route.py: ядро A* на C недоступно, работает Python-версия:", str(e)[:200])
        _CORE = False
    return _CORE or None
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
FREE, HARD = -1, -2
INF = float("inf")


def _dilate(mask, r):
    """Двоичное расширение на r клеток (numpy, без scipy)."""
    out = mask.copy()
    for _ in range(r):
        m = out.copy()
        m[1:, :] |= out[:-1, :]
        m[:-1, :] |= out[1:, :]
        m[:, 1:] |= out[:, :-1]
        m[:, :-1] |= out[:, 1:]
        out = m
    return out


class Router:
    def __init__(self, board, desc=None, grid=0.1, clearance=0.2, track=0.2, via=(0.6, 0.3),
                 cost_bottom=6.0, cost_via=30.0, cost_turn45=0.2, edge=0.5, margin=0.04, bot_max=8.0):
        """cost_bottom — цена клетки на B.Cu в клетках F.Cu, bot_max — предел длины одной
        перемычки. Слой земли берегут, но запретить его нельзя: шину двух радио на одной
        шине SPI на двух слоях без перемычек не развести. При 6 перемычка в 2 мм (два
        переходных и 20 клеток) стоит как крюк в 15 мм по F.Cu — трассировщик берёт её
        только там, где обхода нет; при 6 он начинал использовать B.Cu как второй слой
        сигналов и резал землю дорожками по 20 мм. Длину и щели проверяет линтер
        (EMC-BOT-1..4), а bot_max отсекает длинные перемычки ещё при поиске."""
        self.b = board
        self.d = desc or {}
        self.g = grid
        self.cl = clearance
        self.w0 = track
        self.via_d, self.via_drill = via
        self.cost_bottom = cost_bottom
        self.cost_via = cost_via
        self.cost_turn45 = cost_turn45
        self.edge = edge
        self.margin = margin
        self.bot_max = bot_max
        self.h2h = float((self.d.get("profile") or {}).get("hole_to_hole", 0.5))
        self.outline = board.outline_polygon()
        x0, y0, x1, y1 = self.outline.bounds
        self.ox, self.oy = self._grid_origin(x0, y0)
        self.nx = int(math.ceil((x1 - self.ox) / grid)) + 1
        self.ny = int(math.ceil((y1 - self.oy) / grid)) + 1
        xs = self.ox + np.arange(self.nx) * grid
        ys = self.oy + np.arange(self.ny) * grid
        self.X, self.Y = np.meshgrid(xs, ys)
        self.net_ids, self.net_names = {}, []
        self.gnd = set(self.d.get("gnd_nets", ["GND"]))
        self.roles = dict(self.d.get("roles", {}))
        self.fixed_nets = set(self.d.get("fixed_nets", []))
        self.base_tracks = list(board.tracks)          # закреплённое заранее — не снимается
        self.base_vias = list(board.vias)
        self.routes = {}                                # цепь -> ([Track], [Via])
        self.report = dict(failed=[], routed=0, vias=0, passes=0, ripped=[])

    # --- сетка ---------------------------------------------------------------------------
    def _grid_origin(self, x0, y0):
        """Начало сетки, при котором центры площадок мелкого шага лежат на узлах.

        У вывода с шагом 0,5 мм между зазорами соседей остаётся полоса шириной в одну
        клетку. Если центр площадки попал между узлами (179,75 при сетке от 104,0), края
        полосы совпадают с узлами, и от округления площадка остаётся без свободной клетки
        вовсе — к ней не подойти. Сдвиг сетки выбирается перебором по 1/20 шага: больше
        центров узких площадок на узлах, при равенстве — меньший сдвиг.
        """
        xs, ys = [], []
        for fp in self.b.footprints.values():
            for p in fp.pads():
                if p.net and p.copper_layers() and min(p.w, p.h) <= 0.45:
                    xs.append(p.xy[0])
                    ys.append(p.xy[1])

        def best(vals, base):
            if not vals:
                return base
            v = np.asarray(vals)
            top = None
            for k in range(20):
                o = base - k * self.g / 20
                fr = (v - o) / self.g
                score = int((np.abs(fr - np.round(fr)) < 0.02).sum())
                if top is None or score > top[0]:
                    top = (score, o)
            return round(top[1], 6)
        return best(xs, x0), best(ys, y0)

    def nid(self, net):
        if net not in self.net_ids:
            self.net_ids[net] = len(self.net_names)
            self.net_names.append(net)
        return self.net_ids[net]

    def cell(self, x, y):
        return int(round((x - self.ox) / self.g)), int(round((y - self.oy) / self.g))

    def xy(self, i, j):
        return round(self.ox + i * self.g, 4), round(self.oy + j * self.g, 4)

    def _stamp(self, grid2d, geom, value):
        x0, y0, x1, y1 = geom.bounds
        i0 = max(0, int((x0 - self.ox) / self.g) - 1)
        j0 = max(0, int((y0 - self.oy) / self.g) - 1)
        i1 = min(self.nx - 1, int((x1 - self.ox) / self.g) + 1)
        j1 = min(self.ny - 1, int((y1 - self.oy) / self.g) + 1)
        if i1 < i0 or j1 < j0:
            return
        mask = shapely.contains_xy(geom, self.X[j0:j1 + 1, i0:i1 + 1], self.Y[j0:j1 + 1, i0:i1 + 1])
        sub = grid2d[j0:j1 + 1, i0:i1 + 1]
        if value == HARD:
            sub[mask] = HARD
            return
        # клетка в зазоре двух разных цепей закрыта для обеих: иначе поздняя
        # разметка стёрла бы раннюю, и дорожка прошла бы вплотную к чужой меди
        free = mask & (sub == FREE)
        clash = mask & (sub >= 0) & (sub != value)
        sub[free] = value
        sub[clash] = HARD

    def block_copper(self, geom, net, layers, margin=None, hole=None):
        """margin — запас на округление сетки: для дорожек и переходных он нужен (отрезок
        между клетками проходит ближе, чем сами клетки), для площадок — нет: у выводов с
        шагом 0,5 мм запас закрыл бы клетки внутри самой площадки."""
        m = self.margin if margin is None else margin
        v = self.nid(net) if net else HARD
        g_trk = geom.buffer(self.cl + self.w0 / 2 + m, quad_segs=4)
        for L, name in enumerate(LAYERS):
            if name in layers:
                self._stamp(self.occ[L], g_trk, v)
        if layers:
            self._stamp(self.occ_via, geom.buffer(self.cl + self.via_d / 2 + m, quad_segs=4), v)
        if hole is not None:
            # отверстия сверлятся с зазором друг от друга независимо от цепи: иначе два
            # переходных одной цепи встают в 0,5 мм и завод выдаёт hole_to_hole
            self._stamp(self.occ_via, Point(hole).buffer(self.h2h + self.via_drill + self.margin, quad_segs=8),
                        HARD)

    def keepout(self, poly, layers=LAYERS, vias=True):
        for L, name in enumerate(LAYERS):
            if name in layers:
                self._stamp(self.occ[L], poly.buffer(self.w0 / 2 + self.margin), HARD)
        if vias:
            self._stamp(self.occ_via, poly.buffer(self.via_d / 2 + self.margin), HARD)

    # --- карты -----------------------------------------------------------------------------
    def build_maps(self):
        self.occ = np.full((2, self.ny, self.nx), FREE, dtype=np.int32)
        self.occ_via = np.full((self.ny, self.nx), FREE, dtype=np.int32)
        self.bottom_forbid = np.zeros((self.ny, self.nx), dtype=bool)
        inner_t = self.outline.buffer(-(self.edge + self.w0 / 2 + self.margin))
        inner_v = self.outline.buffer(-(self.edge + self.via_d / 2 + self.margin))
        for L in range(2):
            self.occ[L][~shapely.contains_xy(inner_t, self.X, self.Y)] = HARD
        self.occ_via[~shapely.contains_xy(inner_v, self.X, self.Y)] = HARD
        for fp in self.b.footprints.values():
            for p in fp.pads():
                layers = p.copper_layers()
                if p.kind == "np_thru_hole" or (not layers and p.drill):
                    hole = Point(p.xy).buffer(max(p.w, p.drill) / 2)
                    self.keepout(hole.buffer(self.cl), LAYERS, vias=True)
                    continue
                self.block_copper(p.polygon(), p.net, layers, margin=0.0)
            for z in pcblib.kids(fp.node, "zone"):
                ko = pcblib.kid(z, "keepout")
                if not ko:
                    continue
                rules = {str(k[0]): str(k[1]) == "not_allowed" for k in ko[1:] if isinstance(k, list)}
                poly = pcblib.kid(z, "polygon")
                pts = [(pcblib.num(q, 1), pcblib.num(q, 2)) for q in pcblib.kids(pcblib.kid(poly, "pts") or [], "xy")]
                if len(pts) < 3:
                    continue
                shp = pcblib._place(Polygon(pts), fp.rot, fp.x, fp.y)
                lay = pcblib.kid(z, "layer") or pcblib.kid(z, "layers")
                lays = [str(x) for x in lay[1:]] if lay else list(LAYERS)
                lays = list(LAYERS) if any(l in ("*.Cu", "F&B.Cu") for l in lays) else lays
                # запреты дорожек и переходных независимы: у посадочного места может быть
                # зона, где дорожка можно, а переходное нельзя (тепловая развязка SHT40)
                if rules.get("tracks"):
                    self.keepout(shp, lays, vias=rules.get("vias", False))
                elif rules.get("vias"):
                    self._stamp(self.occ_via, shp.buffer(self.via_d / 2 + self.margin), HARD)
        for z in self.b.zones:
            if z.keepout is not None and z.keepout.get("tracks"):
                self.keepout(z.polygon(), z.layers, vias=bool(z.keepout.get("vias")))
        for zz in self.d.get("zones", []):
            if zz.get("kind") in ("rf", "no_bottom"):
                self.bottom_forbid |= shapely.contains_xy(Polygon(zz["poly"]), self.X, self.Y)
        for ref in self.d.get("no_bottom_under", []):
            if ref in self.b.footprints:
                self.bottom_forbid |= shapely.contains_xy(self.b.footprints[ref].courtyard().buffer(0.5), self.X, self.Y)
        for t in self.base_tracks:
            self.block_copper(t.geom(), t.net, [t.layer])
        for v in self.base_vias:
            self.block_copper(v.geom(), v.net, list(LAYERS), hole=v.at)
        for net, (ts, vs) in self.routes.items():
            for t in ts:
                self.block_copper(t.geom(), t.net, [t.layer])
            for v in vs:
                self.block_copper(v.geom(), v.net, list(LAYERS), hole=v.at)

    # --- поиск пути ------------------------------------------------------------------------------
    def astar(self, starts, targets, nid, window, width, target_xy, allow_bottom=True, limit=600000, soft=None,
              soft_cost=4.0, forbid=None):
        core = load_core()
        if core is None:
            return self._astar_py(starts, targets, nid, window, width, target_xy, allow_bottom, limit, soft, soft_cost)
        i0, j0, i1, j1 = window
        W, H = i1 - i0 + 1, j1 - j0 + 1
        WH = W * H
        occF = self.occ[0, j0:j1 + 1, i0:i1 + 1]
        occB = self.occ[1, j0:j1 + 1, i0:i1 + 1]
        occV = self.occ_via[j0:j1 + 1, i0:i1 + 1]
        bF = (occF != FREE) & (occF != nid)
        bB = (occB != FREE) & (occB != nid)
        if not allow_bottom:
            bB[:] = True
        bf = self.bottom_forbid[j0:j1 + 1, i0:i1 + 1]
        bB |= bf
        if forbid is not None:                      # ВЧ-зона чужой цепи — запрет, а не цена (EMC-RF-6)
            fb = forbid[j0:j1 + 1, i0:i1 + 1]
            bF |= fb
            bB |= fb
        r = max(0, int(math.ceil(((width or self.w0) - self.w0) / 2 / self.g - 1e-9)))
        if r:
            bF, bB = _dilate(bF, r), _dilate(bB, r)
        bV = ((occV != FREE) & (occV != nid)) | bF | bB | bf
        if not allow_bottom:
            bV[:] = True
        blocked = np.ascontiguousarray(np.concatenate([bF.ravel(), bB.ravel()]).astype(np.uint8))
        vblock = np.ascontiguousarray(bV.ravel().astype(np.uint8))
        extra = None
        if soft is not None:
            sw = soft[j0:j1 + 1, i0:i1 + 1].ravel().astype(np.float32) * np.float32(soft_cost)
            extra = np.ascontiguousarray(np.concatenate([sw, sw]))
        if getattr(self, "negotiated", False):
            # согласованная перетрассировка: чужие дорожки не запрет, а цена, растущая от прохода к проходу
            others = self.usage[:, j0:j1 + 1, i0:i1 + 1]
            busy = others > 0
            if r:
                busy = np.stack([_dilate(busy[0], r), _dilate(busy[1], r)])
            pf = (self.pres_fac * busy * np.maximum(others, 1)).astype(np.float32) + self.hist[:, j0:j1 + 1, i0:i1 + 1]
            add = np.ascontiguousarray(pf.reshape(-1))
            extra = add if extra is None else np.ascontiguousarray(extra + add)
            vblock = np.ascontiguousarray(vblock | (self.uvia[j0:j1 + 1, i0:i1 + 1].ravel() > 0).astype(np.uint8))
        tmask = np.zeros(2 * WH, dtype=np.uint8)
        for (L, i, j) in targets:
            if i0 <= i <= i1 and j0 <= j <= j1:
                tmask[L * WH + (j - j0) * W + (i - i0)] = 1
        st = np.array([L * WH + (j - j0) * W + (i - i0) for (L, i, j) in starts
                       if i0 <= i <= i1 and j0 <= j <= j1], dtype=np.int32)
        if not len(st) or not tmask.any():
            return None
        out = np.zeros(4 * (W + H) + 20000, dtype=np.int32)
        best = np.empty(2 * WH, dtype=np.float64)
        parent = np.empty(2 * WH, dtype=np.int32)
        pdir = np.empty(2 * WH, dtype=np.uint8)
        ptr = lambda a: a.ctypes.data if a is not None else None
        n = core.astar(W, H, ptr(blocked), ptr(vblock), ptr(extra), ptr(tmask), ptr(st), len(st),
                       target_xy[0] - i0, target_xy[1] - j0, self.cost_bottom, self.cost_via, self.cost_turn45,
                       limit, ptr(out), len(out), ptr(best), ptr(parent), ptr(pdir))
        if n <= 0:
            return None
        path = []
        for k in out[:n]:
            k = int(k)
            L = k // WH
            j, i = divmod(k - L * WH, W)
            path.append((L, i + i0, j + j0))
        return path

    def _astar_py(self, starts, targets, nid, window, width, target_xy, allow_bottom=True, limit=600000, soft=None,
                  soft_cost=4.0):
        i0, j0, i1, j1 = window
        W, H = i1 - i0 + 1, j1 - j0 + 1
        WH = W * H
        occF = self.occ[0, j0:j1 + 1, i0:i1 + 1]
        occB = self.occ[1, j0:j1 + 1, i0:i1 + 1]
        occV = self.occ_via[j0:j1 + 1, i0:i1 + 1]
        bF = (occF != FREE) & (occF != nid)
        bB = (occB != FREE) & (occB != nid)
        if not allow_bottom:
            bB[:] = True
        bB |= self.bottom_forbid[j0:j1 + 1, i0:i1 + 1]
        r = max(0, int(math.ceil(((width or self.w0) - self.w0) / 2 / self.g - 1e-9)))
        if r:
            bF, bB = _dilate(bF, r), _dilate(bB, r)
        bV = ((occV != FREE) & (occV != nid)) | bF | bB | self.bottom_forbid[j0:j1 + 1, i0:i1 + 1]
        blocked = bytearray(np.concatenate([bF.ravel(), bB.ravel()]).astype(np.uint8).tobytes())
        vblock = bytearray(bV.ravel().astype(np.uint8).tobytes())
        if soft is not None:
            sw = soft[j0:j1 + 1, i0:i1 + 1].ravel()
            smask = bytearray(np.concatenate([sw, sw]).astype(np.uint8).tobytes())
        else:
            smask = None
        tmask = bytearray(2 * WH)
        for (L, i, j) in targets:
            if i0 <= i <= i1 and j0 <= j <= j1:
                tmask[L * WH + (j - j0) * W + (i - i0)] = 1
        best = [INF] * (2 * WH)
        parent = [-1] * (2 * WH)
        pdir = bytearray(b"\x08" * (2 * WH))
        tx, ty = target_xy[0] - i0, target_xy[1] - j0
        heap = []
        for (L, i, j) in starts:
            if not (i0 <= i <= i1 and j0 <= j <= j1):
                continue
            idx = L * WH + (j - j0) * W + (i - i0)
            best[idx] = 0.0
            heapq.heappush(heap, (0.0, 0.0, idx))
        steps = [(di, dj, (1.41421356 if di and dj else 1.0)) for di, dj in DIRS]
        cb, cv, c45 = self.cost_bottom, self.cost_via, self.cost_turn45
        visited = 0
        found = -1
        while heap:
            f, gc, idx = heapq.heappop(heap)
            if gc > best[idx] + 1e-9:
                continue
            if tmask[idx]:
                found = idx
                break
            visited += 1
            if visited > limit:
                break
            L = idx // WH
            rem = idx - L * WH
            j, i = divmod(rem, W)
            pd = pdir[idx]
            lc = cb if L else 1.0
            for nd in range(8):
                di, dj, st = steps[nd]
                if pd != 8:
                    turn = (nd - pd) % 8
                    turn = min(turn, 8 - turn)
                    if turn >= 2:
                        continue
                else:
                    turn = 0
                ni, nj = i + di, j + dj
                if ni < 0 or nj < 0 or ni >= W or nj >= H:
                    continue
                nidx = L * WH + nj * W + ni
                if blocked[nidx]:
                    continue
                ng = gc + st * lc + (c45 if turn == 1 else 0.0)
                if smask is not None and smask[nidx]:
                    ng += st * soft_cost
                if ng < best[nidx] - 1e-9:
                    best[nidx] = ng
                    parent[nidx] = idx
                    pdir[nidx] = nd
                    dx, dy = abs(ni - tx), abs(nj - ty)
                    heapq.heappush(heap, (ng + max(dx, dy) + 0.41421356 * min(dx, dy), ng, nidx))
            if allow_bottom and not vblock[rem]:
                nidx = (1 - L) * WH + rem
                ng = gc + cv
                if ng < best[nidx] - 1e-9:
                    best[nidx] = ng
                    parent[nidx] = idx
                    pdir[nidx] = 8
                    dx, dy = abs(i - tx), abs(j - ty)
                    heapq.heappush(heap, (ng + max(dx, dy) + 0.41421356 * min(dx, dy), ng, nidx))
        if found < 0:
            return None
        path = []
        k = found
        while k >= 0:
            L = k // WH
            j, i = divmod(k - L * WH, W)
            path.append((L, i + i0, j + j0))
            k = parent[k]
        path.reverse()
        return path

    # --- путь в дорожки -----------------------------------------------------------------------
    def to_copper(self, path, net, width):
        tracks, vias = [], []
        seg_start, prev, prev_dir = path[0], path[0], None
        for cur in path[1:]:
            if cur[0] != prev[0]:
                if seg_start[1:] != prev[1:]:
                    tracks.append((seg_start, prev))
                vias.append(prev[1:])
                seg_start, prev, prev_dir = cur, cur, None
                continue
            d = (cur[1] - prev[1], cur[2] - prev[2])
            if prev_dir is not None and d != prev_dir:
                tracks.append((seg_start, prev))
                seg_start = prev
            prev_dir, prev = d, cur
        if seg_start[1:] != prev[1:]:
            tracks.append((seg_start, prev))
        ts = [pcblib.Track(self.xy(a[1], a[2]), self.xy(b[1], b[2]), width, LAYERS[a[0]], net) for a, b in tracks]
        vs = [pcblib.Via(self.xy(i, j), self.via_d, self.via_drill, net) for i, j in vias]
        return ts, vs

    def pad_cells(self, pad, nid):
        poly = pad.polygon()
        x0, y0, x1, y1 = poly.bounds
        cells = set()
        for L, name in enumerate(LAYERS):
            if name not in pad.copper_layers():
                continue
            i0, j0 = max(0, int(math.floor((x0 - self.ox) / self.g))), max(0, int(math.floor((y0 - self.oy) / self.g)))
            i1 = min(self.nx - 1, int(math.ceil((x1 - self.ox) / self.g)))
            j1 = min(self.ny - 1, int(math.ceil((y1 - self.oy) / self.g)))
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    x, y = self.xy(i, j)
                    o = self.occ[L, j, i]
                    if (o == FREE or o == nid) and poly.contains(Point(x, y)):
                        cells.add((L, i, j))
        return cells

    # --- земля SMD ------------------------------------------------------------------------------
    def gnd_via(self, pad, net, max_r=3.0):
        """Переходное на плоскость у земляной SMD-площадки.

        Место ищется поиском пути по F.Cu от площадки до ближайшей клетки, где переходное
        разрешено: у выводов с шагом 0,5 мм прямой отвод почти всегда задевает соседа.
        Переходное в самой площадке не ставится (MNT-10). Запрет B.Cu в ВЧ-зоне и под
        кварцем на земляные переходные не действует: сшивка с плоскостью там и нужна.
        """
        nid = self.nid(net)
        starts = {c for c in self.pad_cells(pad, nid) if c[0] == 0}
        if not starts:
            return None
        cx, cy = pad.xy
        a, bb = self.cell(cx - max_r, cy - max_r), self.cell(cx + max_r, cy + max_r)
        i0, j0 = max(0, a[0]), max(0, a[1])
        i1, j1 = min(self.nx - 1, bb[0]), min(self.ny - 1, bb[1])
        ov = self.occ_via[j0:j1 + 1, i0:i1 + 1]
        oF = self.occ[0, j0:j1 + 1, i0:i1 + 1]
        oB = self.occ[1, j0:j1 + 1, i0:i1 + 1]
        ok = ((ov == FREE) | (ov == nid)) & ((oF == FREE) | (oF == nid)) & ((oB == FREE) | (oB == nid))
        far = shapely.distance(pad.polygon(), shapely.points(self.X[j0:j1 + 1, i0:i1 + 1],
                                                              self.Y[j0:j1 + 1, i0:i1 + 1])) >= self.via_d / 2 + 0.05
        js, is_ = np.nonzero(ok & far)
        targets = {(0, int(i) + i0, int(j) + j0) for i, j in zip(is_, js)}
        if not targets:
            return None
        path = self.astar(starts, targets, nid, (i0, j0, i1, j1), self.w0, self.cell(cx, cy),
                          allow_bottom=False, limit=80000)
        if path is None:
            return None
        _, i, j = path[-1]
        x, y = self.xy(i, j)
        ts = self.to_copper(path, net, self.w0)[0] if len(path) > 1 else []
        return ts, [pcblib.Via((x, y), self.via_d, self.via_drill, net)]

    # --- цепь -------------------------------------------------------------------------------------
    def path_length(self, path):
        L = 0.0
        for a, b in zip(path, path[1:]):
            if a[0] != b[0]:
                continue
            L += math.hypot(a[1] - b[1], a[2] - b[2]) * self.g
        return L

    @staticmethod
    def _closest(pa, pb):
        A = np.asarray(pa, dtype=float)
        B = np.asarray(pb, dtype=float)
        d = ((A[:, None, :] - B[None, :, :]) ** 2).sum(-1)
        k = int(np.argmin(d))
        i, j = divmod(k, len(B))
        return tuple(A[i]), tuple(B[j]), math.sqrt(d.flat[k])

    def _bot_run(self, path):
        """Длина самого длинного непрерывного участка на B.Cu, мм."""
        best = run = 0.0
        for a, b in zip(path, path[1:]):
            if a[0] == 1 and b[0] == 1:
                run += math.hypot(a[1] - b[1], a[2] - b[2]) * self.g
                best = max(best, run)
            else:
                run = 0.0
        return best

    def _overlap(self, path, own):
        """Сколько клеток пути легло на чужую медь (без своих площадок и своих же дорожек)."""
        n = 0
        for c in path:
            if c not in own and self.usage[c[0], c[2], c[1]] > 0:
                n += 1
        return n

    def _connect(self, net, nid, width, src, dst, soft, forbid=None):
        """Путь от скопления меди src к скоплению dst. None — пути нет или крюк слишком длинный.

        Окно поиска расширяется, пока путь идёт по чужой меди: в узком окне обход просто не
        помещается, и поиск возвращает дорогой путь поверх соседей, считая его единственным.
        """
        p0, p1, straight = self._closest(src["pts"], dst["pts"])
        sp = (p0, p1)
        own = src["cells"] | dst["cells"]
        best = None
        for margin in (4.0, 12.0, 40.0):
            x0 = min(sp[0][0], sp[1][0]) - margin
            y0 = min(sp[0][1], sp[1][1]) - margin
            x1 = max(sp[0][0], sp[1][0]) + margin
            y1 = max(sp[0][1], sp[1][1]) + margin
            a, bb = self.cell(x0, y0), self.cell(x1, y1)
            win = (max(0, a[0]), max(0, a[1]), min(self.nx - 1, bb[0]), min(self.ny - 1, bb[1]))
            inside = lambda c: win[0] <= c[1] <= win[2] and win[1] <= c[2] <= win[3]
            starts = {c for c in src["cells"] if inside(c)}
            targets = {c for c in dst["cells"] if inside(c)}
            if not starts or not targets:
                continue
            path = self.astar(starts, targets, nid, win, width, self.cell(*sp[1]), soft=soft, forbid=forbid)
            if path is None:
                continue
            L = self.path_length(path)
            if L > max(3.0 * straight, straight + 15.0):
                self.report.setdefault("detours", []).append(
                    dict(net=net, straight=round(straight, 1), path=round(L, 1)))
                continue
            over = self._overlap(path, own) if getattr(self, "negotiated", False) else 0
            long_bot = max(0.0, self._bot_run(path) - self.bot_max)
            key = (over, round(long_bot, 1), L)
            if best is None or key < best[0]:
                best = (key, path)
            if not over and not long_bot:
                break
        if best is not None and best[0][1]:
            self.report.setdefault("long_jumpers", []).append(
                dict(net=net, run=round(best[0][1] + self.bot_max, 1)))
        return best[1] if best is not None else None

    def route_net(self, net, pads, width, pairs=(), soft=None, forbid=None):
        """Разводит цепь целиком. Возвращает неудачи [(площадка, ближайшая точка)].

        Площадки — скопления меди. Сначала соединяются пары «вывод — его конденсатор
        развязки» (EMC-DEC-1: питание приходит на конденсатор, а не мимо него), затем
        скопления растут деревом: ближайшее к дереву подключается следующим.
        """
        nid = self.nid(net)
        pads = [p for p in pads if p.copper_layers()]
        ts_all, vs_all, fails = [], [], []
        self._centers = []
        if len(pads) < 2:
            self.routes[net] = (ts_all, vs_all)
            return fails
        clusters = [dict(cells=self.pad_cells(p, nid), pads=[p], pts=[p.xy]) for p in pads]
        clusters = [c for c in clusters if c["cells"]] + [c for c in clusters if not c["cells"]]
        for c in clusters:
            if not c["cells"]:
                fails.append((c["pads"][0], c["pads"][0].xy))
        clusters = [c for c in clusters if c["cells"]]

        def merge(a, b):
            a["cells"] |= b["cells"]
            a["pads"] += b["pads"]
            a["pts"] += b["pts"]
            clusters.remove(b)

        changed = True
        while changed:                              # площадки, уже касающиеся друг друга
            changed = False
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    if clusters[i]["cells"] & clusters[j]["cells"]:
                        merge(clusters[i], clusters[j])
                        changed = True
                        break
                if changed:
                    break

        def commit(path, a, b):
            ts, vs = self.to_copper(path, net, width)
            if not getattr(self, "negotiated", False) or net in self.gnd:
                for t in ts:
                    self.block_copper(t.geom(), net, [t.layer])
                for v in vs:
                    self.block_copper(v.geom(), net, list(LAYERS), hole=v.at)
            ts_all.extend(ts)
            vs_all.extend(vs)
            self._centers.extend(path)
            a["cells"].update(path)
            a["pts"] += [self.xy(c[1], c[2]) for c in path[::20]]
            merge(a, b)

        def owner(pad):
            key = (pad.fp.ref, pad.num, pad.lx, pad.ly)
            return next((c for c in clusters if any((q.fp.ref, q.num, q.lx, q.ly) == key for q in c["pads"])), None)

        for pin_pad, cap_pad in pairs:
            a, b = owner(cap_pad), owner(pin_pad)
            if a is None or b is None or a is b:
                continue
            path = self._connect(net, nid, width, a, b, soft, forbid)
            if path is not None:
                commit(path, a, b)
        tree = clusters[0]
        while len(clusters) > 1:
            others = [c for c in clusters if c is not tree]
            nxt = min(others, key=lambda c: (self._closest(c["pts"], tree["pts"])[2],
                                             c["pads"][0].fp.ref, c["pads"][0].num))
            path = self._connect(net, nid, width, nxt, tree, soft, forbid)
            if path is None:
                near = min(tree["pts"], key=lambda q: math.dist(nxt["pads"][0].xy, q))
                fails.append((nxt["pads"][0], near))
                clusters.remove(nxt)
                continue
            commit(path, nxt, tree)            # nxt поглощает дерево
            tree = nxt
        self.routes[net] = (ts_all, vs_all)
        return fails

    def rip_candidates(self, p, nearest, net):
        corridor = LineString([p.xy, nearest]).buffer(1.2) if math.dist(p.xy, nearest) > 1e-6 \
            else Point(p.xy).buffer(1.5)
        corridor = corridor.union(Point(p.xy).buffer(1.0))
        hits = []
        for other, (ts, vs) in sorted(self.routes.items()):
            if other == net or other in self.fixed_nets or other in self.gnd:
                continue
            d = min([t.geom().distance(corridor) for t in ts] + [v.geom().distance(corridor) for v in vs] + [INF])
            if d < 0.05:
                hits.append(other)
        return hits

    # --- согласованная перетрассировка --------------------------------------------------------------
    def _cells_of(self, geom, radius, layers):
        """Плоские индексы клеток (слой L: L*ny*nx + j*nx + i), закрытых фигурой с раздувом."""
        g = geom.buffer(radius, quad_segs=4)
        x0, y0, x1, y1 = g.bounds
        i0, j0 = max(0, int((x0 - self.ox) / self.g) - 1), max(0, int((y0 - self.oy) / self.g) - 1)
        i1 = min(self.nx - 1, int((x1 - self.ox) / self.g) + 1)
        j1 = min(self.ny - 1, int((y1 - self.oy) / self.g) + 1)
        m = shapely.contains_xy(g, self.X[j0:j1 + 1, i0:i1 + 1], self.Y[j0:j1 + 1, i0:i1 + 1])
        jj, ii = np.nonzero(m)
        base = (jj + j0) * self.nx + (ii + i0)
        return [L * self.ny * self.nx + base for L in layers]

    def _coverage(self, ts, vs):
        rt = self.cl + self.w0 / 2 + self.margin
        rv = self.cl + self.via_d / 2 + self.margin
        trk, via = [], []
        for tr in ts:
            L = LAYERS.index(tr.layer)
            trk += self._cells_of(tr.geom(), rt, [L])
            via += self._cells_of(tr.geom(), rv, [0])
        for v in vs:
            trk += self._cells_of(v.geom(), rt, [0, 1])
            via += self._cells_of(v.geom(), rv, [0])
        trk = np.unique(np.concatenate(trk)) if trk else np.zeros(0, dtype=np.int64)
        via = np.unique(np.concatenate(via)) if via else np.zeros(0, dtype=np.int64)
        return trk, via

    def _add(self, net, sign):
        trk, via, centers, vcells = self.cov[net]
        np.add.at(self.usage.reshape(-1), trk, sign)
        np.add.at(self.uvia.reshape(-1), via, sign)

    def _conflicted(self, net):
        trk, via, centers, vcells = self.cov[net]
        if len(centers) and (self.usage.reshape(-1)[centers] >= 2).any():
            return True
        if len(vcells) and (self.uvia.reshape(-1)[vcells] >= 2).any():
            return True
        return False

    def _conflict_list(self, queue):
        """Кто с кем не разошёлся: по дорожкам и по переходным — для отчёта и разбора."""
        us, uv = self.usage.reshape(-1), self.uvia.reshape(-1)
        out = []
        for n in queue:
            if n not in self.cov:
                continue
            trk, via, centers, vcells = self.cov[n]
            for kind, cells, cnt, idx in (("дорожка", centers, us, 0), ("переходное", vcells, uv, 1)):
                if not len(cells):
                    continue
                bad = [int(k) for k in cells if cnt[k] >= 2]
                if not bad:
                    continue
                k = bad[len(bad) // 2]
                L, rem = divmod(k, self.ny * self.nx) if idx == 0 else (0, k)
                j, i = divmod(rem, self.nx)
                who = sorted(m for m in self.cov if m != n and
                             len(np.intersect1d(self.cov[m][idx], np.array([k]), assume_unique=False)))
                out.append(dict(net=n, kind=kind, at=[round(v, 1) for v in self.xy(i, j)],
                                cells=len(bad), with_=who[:4]))
        return out

    def _hotspots(self, queue):
        """Узкие места: где и какие цепи делят клетки после последнего прохода (для отчёта и раздвижки)."""
        flat = self.usage.reshape(-1)
        spots = {}
        for n in queue:
            if n not in self.cov:
                continue
            trk, via, centers, vcells = self.cov[n]
            bad = centers[flat[centers] >= 2]
            for idx in bad[::5]:
                L, rem = divmod(int(idx), self.ny * self.nx)
                j, i = divmod(rem, self.nx)
                x, y = self.xy(i, j)
                key = (round(x), round(y))
                with_ = [m for m in queue if m != n and m in self.cov and
                         np.searchsorted(self.cov[m][0], idx) < len(self.cov[m][0]) and
                         self.cov[m][0][np.searchsorted(self.cov[m][0], idx)] == idx]
                s = spots.setdefault(key, set())
                s.add(n)
                s.update(with_)
        return [dict(at=list(k), nets=sorted(v)) for k, v in sorted(spots.items())]

    def run(self, order=None, widths=None, max_passes=30):
        widths = widths or {}
        order = order or []
        by_net = {}
        for fp in self.b.footprints.values():
            for p in fp.pads():
                if p.net:
                    by_net.setdefault(p.net, []).append(p)
        by_net = {n: sorted(ps, key=lambda p: (p.fp.ref, p.num, p.lx, p.ly)) for n, ps in by_net.items()
                  if len(ps) >= 2 or n in self.gnd}
        t0 = time.time()
        self.negotiated = False
        self.build_maps()
        gnd_fail = []
        for net in sorted(n for n in by_net if n in self.gnd):
            ts_all, vs_all = [], []
            for p in by_net[net]:
                if p.kind == "smd" and "F.Cu" in p.copper_layers():
                    res = self.gnd_via(p, net)
                    if res is None:
                        gnd_fail.append(p)
                        continue
                    ts, vs = res
                    for tr in ts:
                        self.block_copper(tr.geom(), net, ["F.Cu"])
                    for v in vs:
                        self.block_copper(v.geom(), net, list(LAYERS), hole=v.at)
                    ts_all += ts
                    vs_all += vs
            self.routes[net] = (ts_all, vs_all)
        rank = {n: i for i, n in enumerate(order)}
        pairs = {}
        for pr in self.d.get("decoupling", []):
            ref, pin = pr["pin"]
            if ref in self.b.footprints and pr["cap"] in self.b.footprints:
                pp = self.b.footprints[ref].pad(pin)
                cp = next((q for q in self.b.footprints[pr["cap"]].pads() if q.net == pp.net), None)
                if cp is not None and pp.net:
                    pairs.setdefault(pp.net, []).append((pp, cp))
        # ВЧ-зона: цепи со своими площадками внутри входят (это обвязка радио), остальным
        # зона закрыта — чужая дорожка рядом с трактом это ошибка EMC-RF-6, а не дорогая клетка.
        self.rf_mask = np.zeros((self.ny, self.nx), dtype=bool)
        rf_zones = []
        for zz in self.d.get("zones", []):
            if zz.get("kind") == "rf":
                poly = Polygon(zz["poly"])
                m = shapely.contains_xy(poly, self.X, self.Y)
                self.rf_mask |= m
                rf_zones.append((poly, m))
        self.forbid_net = {}
        for n, ps in by_net.items():
            if n in self.gnd:
                continue
            m = None
            for poly, mask in rf_zones:
                if any(poly.intersects(p.polygon()) for p in ps):
                    continue                        # своя обвязка — вход разрешён
                m = mask if m is None else (m | mask)
            if m is not None:
                self.forbid_net[n] = m

        def span(n):
            xs = [p.xy[0] for p in by_net[n]]
            ys = [p.xy[1] for p in by_net[n]]
            return (max(xs) - min(xs)) + (max(ys) - min(ys))
        queue = sorted((n for n in by_net if n not in self.gnd), key=lambda n: (rank.get(n, len(order)), span(n), n))

        self.negotiated = True
        self.usage = np.zeros((2, self.ny, self.nx), dtype=np.int32)
        self.uvia = np.zeros((self.ny, self.nx), dtype=np.int32)
        self.hist = np.zeros((2, self.ny, self.nx), dtype=np.float32)
        self.pres_fac = 1.0
        self.cov = {}
        best = None
        fails_by_net = {}
        to_route = list(queue)
        history = []
        for ps in range(1, max_passes + 1):
            self.report["passes"] = ps
            for net in to_route:
                if net in self.cov:
                    self._add(net, -1)
                    del self.cov[net]
                soft = None if self.roles.get(net) == "RF" else self.rf_mask
                fails = self.route_net(net, by_net[net], widths.get(net, self.w0), pairs.get(net, ()), soft,
                                       self.forbid_net.get(net))
                ts, vs = self.routes[net]
                trk, via = self._coverage(ts, vs)
                centers = np.array(sorted({L * self.ny * self.nx + j * self.nx + i for L, i, j in self._centers}),
                                   dtype=np.int64)
                vcells = np.array(sorted({self.cell(*v.at)[1] * self.nx + self.cell(*v.at)[0] for v in vs}),
                                  dtype=np.int64)
                self.cov[net] = (trk, via, centers, vcells)
                self._add(net, +1)
                if fails:
                    fails_by_net[net] = fails
                else:
                    fails_by_net.pop(net, None)
            conflicted = [n for n in queue if n in self.cov and self._conflicted(n)]
            history.append((len(conflicted), len(fails_by_net)))
            if best is None or (len(conflicted) + len(fails_by_net)) < best[0]:
                # лучший снимок, а не последний: цена перегрузки растёт, и поздние проходы
                # иногда расходятся хуже ранних
                best = (len(conflicted) + len(fails_by_net),
                        {n: (list(ts), list(vs)) for n, (ts, vs) in self.routes.items()},
                        dict(self.cov), self.usage.copy(), self.uvia.copy(), dict(fails_by_net))
            if not conflicted and not fails_by_net:
                break
            for n in conflicted:
                trk, via, centers, vcells = self.cov[n]
                flat = self.usage.reshape(-1)
                bad = centers[flat[centers] >= 2]
                np.add.at(self.hist.reshape(-1), bad, 2.0)
            self.pres_fac = min(self.pres_fac * 1.8, 3000.0)
            to_route = sorted(set(conflicted) | set(fails_by_net), key=lambda n: (rank.get(n, len(order)), span(n), n))
        if best is not None and best[0] < len([n for n in queue if n in self.cov and self._conflicted(n)]) \
                + len(fails_by_net):
            self.routes.update(best[1])
            self.cov, self.usage, self.uvia = best[2], best[3], best[4]
            fails_by_net = dict(best[5])
        self.report["history"] = history
        # починка: оставшиеся цепи разводятся заново по жёсткой карте — соседи уже не
        # двигаются, и близкий промах в одну клетку уходит сам. Не вышло — цепь снимается.
        stuck = [n for n in queue if n in self.cov and self._conflicted(n)] + \
                [n for n in queue if n in fails_by_net and n not in self.cov]
        if stuck:
            self.negotiated = False
            order_key = lambda n: (-len(by_net[n]), -span(n), n)

            def hard_map(free):
                """Жёсткая карта: всё разведённое — препятствие, кроме цепей из free."""
                self.build_maps()
                for n in sorted(self.routes):
                    if n in free:
                        continue
                    for tr in self.routes[n][0]:
                        self.block_copper(tr.geom(), n, [tr.layer])
                    for v in self.routes[n][1]:
                        self.block_copper(v.geom(), n, list(LAYERS), hole=v.at)

            def lay(net):
                for tr in self.routes[net][0]:
                    self.block_copper(tr.geom(), net, [tr.layer])
                for v in self.routes[net][1]:
                    self.block_copper(v.geom(), net, list(LAYERS), hole=v.at)

            def attempt(net):
                soft = None if self.roles.get(net) == "RF" else self.rf_mask
                return self.route_net(net, by_net[net], widths.get(net, self.w0), pairs.get(net, ()), soft,
                                      self.forbid_net.get(net))
            for n in stuck:
                if n in self.cov:
                    self._add(n, -1)
                    del self.cov[n]
            repaired, lost, pending = [], [], list(stuck)
            for _ in range(6):
                if not pending:
                    break
                hard_map(set(pending))
                nxt = []
                for net in sorted(pending, key=order_key):
                    fails, blockers = attempt(net), []
                    if fails:
                        # снимаем 1-3 цепи, лежащие в коридоре, и пробуем ещё раз; сами
                        # снятые разводятся следующим кругом (rip-up & reroute)
                        for p, near in fails:
                            for bnet in self.rip_candidates(p, near, net):
                                if bnet not in blockers and bnet not in pending and bnet in self.routes:
                                    blockers.append(bnet)
                        blockers = blockers[:3]
                        if blockers:
                            for bnet in blockers:
                                self.routes.pop(bnet, None)
                                self.report.setdefault("ripped", []).append(bnet)
                            hard_map({net})
                            fails = attempt(net)
                        if fails:
                            for bnet in blockers:            # не помогло — вернуть как было
                                blockers = []
                                break
                    if fails:
                        # частичная разводка остаётся: у цепи вроде +3V3 с тремя десятками
                        # площадок один неудавшийся участок не должен отменять остальные
                        fails_by_net[net] = fails
                        if net not in lost:
                            lost.append(net)
                        lay(net)
                    else:
                        fails_by_net.pop(net, None)
                        if net not in repaired:
                            repaired.append(net)
                        if net in lost:
                            lost.remove(net)
                        lay(net)
                    for bnet in blockers:
                        if bnet not in nxt:
                            nxt.append(bnet)
                # снятые цепи возвращаются в очередь всегда: раньше отсев по «уже
                # починенным» оставлял цепь снятой навсегда, и она молча исчезала с платы
                pending = list(nxt)
            for net in pending:                      # снятые на последнем круге — развести хоть как
                if net not in self.routes:
                    fails = attempt(net)
                    if fails:
                        fails_by_net[net] = fails
                        if net not in lost:
                            lost.append(net)
                    lay(net)
            self.report["repaired"] = repaired
            self.report["dropped_after_repair"] = [n for n in lost if n not in self.routes]
            # страховка: ни одна цепь не должна остаться без меди незаметно
            missing = [n for n in queue if n not in self.routes]
            for net in sorted(missing, key=order_key):
                hard_map({net})
                fails = attempt(net)
                if fails:
                    fails_by_net[net] = fails
                lay(net)
            if missing:
                self.report["restored"] = missing
        self.report["hotspots"] = self._hotspots(queue)
        self.report["conflicts"] = self._conflict_list(queue)
        if getattr(self, "keep_conflicts", False):
            self.b.tracks = list(self.base_tracks)
            self.b.vias = list(self.base_vias)
            for net in sorted(self.routes):
                self.b.tracks += self.routes[net][0]
                self.b.vias += self.routes[net][1]
            self.b.save(self.keep_conflicts)
        # не разошлись — снимаются поздние по очереди, пока не останется ни одного пересечения
        dropped = list(self.report.get("dropped_after_repair", []))
        for net in reversed(queue):
            if net in self.cov and self._conflicted(net):
                self._add(net, -1)
                del self.cov[net]
                self.routes.pop(net, None)
                dropped.append(net)
        self.report["failed"] = [dict(net=n, pad=f"{p.fp.ref}.{p.num}", at=[round(v, 2) for v in p.xy])
                                 for n, fl in sorted(fails_by_net.items()) for p, _ in fl] + \
                                [dict(net=n, pad="", why="не разошлась с соседними цепями, снята") for n in dropped] + \
                                [dict(net=p.net, pad=f"{p.fp.ref}.{p.num}", at=[round(v, 2) for v in p.xy],
                                      why="нет места для переходного на землю") for p in gnd_fail]
        self.b.tracks = list(self.base_tracks)
        self.b.vias = list(self.base_vias)
        for net in sorted(self.routes):
            ts, vs = self.routes[net]
            self.b.tracks += ts
            self.b.vias += vs
        self.report["nets"] = len(by_net)
        self.report["routed"] = len([n for n in by_net if n in self.routes and n not in fails_by_net])
        self.report["vias"] = len(self.b.vias)
        self.report["seconds"] = round(time.time() - t0, 1)
        bot = sum(tr.length() for tr in self.b.tracks if tr.layer == "B.Cu")
        tot = sum(tr.length() for tr in self.b.tracks) or 1
        self.report["bottom_share"] = round(bot / tot, 3)
        return self.report


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--desc")
    ap.add_argument("--out")
    ap.add_argument("--max-passes", type=int, default=30)
    ap.add_argument("--report")
    ap.add_argument("--keep-conflicts", help="отладка: плата со всеми маршрутами до снятия конфликтных")
    a = ap.parse_args(argv[1:])
    b = pcblib.Board.read(a.board)
    desc = json.load(open(a.desc, encoding="utf-8")) if a.desc else {}
    r = Router(b, desc)
    if a.keep_conflicts:
        r.keep_conflicts = a.keep_conflicts
    rep = r.run(order=desc.get("route_order", []), widths=desc.get("track_widths", {}), max_passes=a.max_passes)
    b.save(a.out or a.board)
    text = json.dumps(rep, ensure_ascii=False, indent=1)
    if a.report:
        open(a.report, "w", encoding="utf-8").write(text)
    print(f"цепей {rep['nets']}, разведено {rep['routed']}, переходных {rep['vias']}, "
          f"доля B.Cu {rep['bottom_share']:.1%}, проходов {rep['passes']}, {rep['seconds']} с")
    for f in rep["failed"]:
        print("НЕ РАЗВЕДЕНО:", f["net"], f["pad"], f.get("why", ""))
    return 1 if rep["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
