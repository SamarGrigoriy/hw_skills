"""
schlib.py — генератор схем KiCad из описания на Python.

Почему генератор, а не рисование мышью: схема, собранная скриптом, ложится на
сетку сама, не разъезжается при правках, даёт осмысленный git diff и
пересобирается за секунду после смены номинала. Человек читает исходник
описания, KiCad читает результат.

Почему без внешних зависимостей: ни kiutils, ни sexpdata в системе нет, а
ставить их ради вставки текста в текстовый файл — лишний класс отказов.
Формат .kicad_sch — обычные S-выражения, и пишутся они обычным str.

ВАЖНО, проверено на KiCad 10.0.4: под-юниты внутри определения символа
(`C_0_1`, `R_1_1`) остаются БЕЗ префикса библиотеки. Если переименовать их в
`Device:C_0_1`, KiCad молча откажется открывать файл со словами
«Не удалось загрузить схему», не указав строки. Это первое, что надо проверять,
если сгенерированный файл не грузится.
"""

import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kfont   # noqa: E402
import sexp    # noqa: E402

# ---------------------------------------------------------------------------
# Константы формата

SCH_VERSION = 20260306        # формат KiCad 10.0.x
GEN_VERSION = "10.0"
GRID = 1.27                   # мм; вся схема обязана лежать на этой сетке
SYMBOL_DIRS = [
    "/usr/share/kicad/symbols",
    os.path.expanduser("~/.local/share/kicad/10.0/symbols"),
]


def add_symbol_dir(path):
    """Подключает библиотеку символов проекта.

    Своя библиотека нужна почти всегда: половины компонентов реального
    устройства в стандартных нет. Её каталог добавляется ПЕРВЫМ, чтобы
    одноимённый символ проекта побеждал системный, а не наоборот.
    """
    p = os.path.abspath(path)
    if p not in SYMBOL_DIRS:
        SYMBOL_DIRS.insert(0, p)
    _lib_cache.clear()
    return p

# Размеры листа, мм (ширина, высота)
PAPER = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
}

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def uid(key):
    """Детерминированный UUID из строки.

    Случайные UUID сделали бы каждую перегенерацию новым файлом целиком —
    git-история превратилась бы в шум. Здесь один и тот же ключ всегда даёт
    один и тот же идентификатор, и в diff видно только то, что менялось.
    """
    return str(uuid.uuid5(_NS, key))


def snap(v):
    """Притягивает координату к сетке 1.27 мм."""
    return round(round(v / GRID) * GRID, 4)


# ---------------------------------------------------------------------------
# Работа с библиотеками символов

def _find_lib(libname):
    for d in SYMBOL_DIRS:
        p = os.path.join(d, libname + ".kicad_sym")
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        f"библиотека {libname}.kicad_sym не найдена в {SYMBOL_DIRS}")


def _extract_block(text, start):
    """Вырезает S-выражение целиком по балансу скобок, игнорируя скобки в строках."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("несбалансированные скобки в библиотеке")


_lib_cache = {}


def _raw_symbol(libname, symname):
    text = open(_find_lib(libname), encoding="utf-8").read()
    m = re.search(r'\(symbol "' + re.escape(symname) + r'"[\s\n]', text)
    if not m:
        raise KeyError(f"символ {libname}:{symname} не найден в библиотеке")
    return _extract_block(text, m.start())


def _subunits(block, symname):
    """Под-юниты символа: графика и выводы. Их имена вида NAME_0_1."""
    out = []
    for m in re.finditer(r'\n\t\t\(symbol "' + re.escape(symname) + r'_(\d+_\d+)"',
                         block):
        out.append(_extract_block(block, m.start() + 1))
    return out


def load_symbol(lib_id):
    """Определение символа, готовое для вставки в lib_symbols.

    Разворачивает наследование. Половина символов в библиотеках KiCad объявлена
    через `(extends "ДРУГОЙ")`: у них есть свои свойства, но ни одного вывода и
    ни одной линии — всё берётся у родителя. Вставить такой символ как есть —
    получить на схеме деталь без выводов, к которой ничего нельзя подключить,
    причём KiCad об этом не скажет. Поэтому графика и выводы берутся у предка,
    а свойства остаются свои.
    """
    if lib_id in _lib_cache:
        return _lib_cache[lib_id]
    libname, symname = lib_id.split(":", 1)
    block = _raw_symbol(libname, symname)

    m = re.search(r'\(extends "([^"]+)"\)', block)
    if m:
        parent_name = m.group(1)
        parent = _raw_symbol(libname, parent_name)
        # свойства и заголовок — от потомка, тело — от предка
        head = re.sub(r'\n\t\t\(extends "[^"]+"\)', '', block)
        units = _subunits(parent, parent_name)
        units = [u.replace(f'(symbol "{parent_name}_', f'(symbol "{symname}_', 1)
                 for u in units]
        # атрибуты предка, которых нет у потомка (pin_names, pin_numbers и т.п.)
        attrs = []
        for tag in ("pin_numbers", "pin_names", "exclude_from_sim", "in_bom",
                    "on_board", "duplicate_pin_numbers_are_jumpers"):
            if f"({tag}" not in head:
                a = re.search(r'\n\t\t\(' + tag + r'[\s\n)]', parent)
                if a:
                    attrs.append(_extract_block(parent, a.start() + 1))
        # Поля предка, которых потомок не переопределил, KiCad тоже наследует:
        # BC817 получает от Q_NPN_BEC «Sim.Device» и «Sim.Pins». Без них копия
        # в lib_symbols расходится с библиотекой, и ERC на каждый такой символ
        # пишет lib_symbol_mismatch.
        own = set(re.findall(r'\n\t\t\(property "([^"]+)"', head))
        for p in re.finditer(r'\n\t\t\(property "([^"]+)"', parent):
            if p.group(1) not in own:
                attrs.append(_extract_block(parent, p.start() + 1))
        body = "".join("\n\t\t" + a for a in attrs)
        inner = "".join("\n\t\t" + u.replace("\n", "\n") for u in units)
        block = head[:-1].rstrip() + body + inner + "\n\t)"

    block = block.replace(f'(symbol "{symname}"', f'(symbol "{lib_id}"', 1)
    _lib_cache[lib_id] = block
    return block


def symbol_pins(lib_id):
    """Координаты выводов символа в его собственной системе координат.

    Возвращает {номер: (x, y)}. Нужно, чтобы провода попадали ровно в вывод, а
    не «рядом»: провод, не дотянутый до вывода на 0.01 мм, выглядит соединённым
    и не соединён — ERC поймает, глаз нет.
    """
    block = load_symbol(lib_id)
    pins = {}
    for m in re.finditer(
            r'\(pin\s+\w+\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
            r'.*?\(number\s+"([^"]+)"', block, re.S):
        x, y, _ang, num = m.groups()
        pins[num] = (float(x), float(y))
    return pins


def symbol_units(lib_id):
    """Номера юнитов символа: {1} у обычного, {1..7} у 40106.

    Многоюнитный символ — это один корпус, нарисованный несколькими частями:
    шесть инверторов 40106 плюс их общее питание. Части одного корпуса
    стоят на разных листах под одной ссылкой, и у каждой — свои выводы.
    """
    units = {int(u) for u in re.findall(r'\(symbol "[^"]*_(\d+)_\d+"', load_symbol(lib_id))}
    units.discard(0)
    return units or {1}


def xform(px, py, at, angle=0, mirror=None):
    """Точка из системы координат символа — в координаты листа.

    Ось Y в символе направлена вверх, на листе — вниз, поэтому знак Y меняется.
    Сначала поворот против часовой стрелки в системе символа с инверсией Y:
      0   : ( px, -py)
      90  : (-py, -px)
      180 : (-px,  py)
      270 : ( py,  px)
    затем зеркало уже на листе: mirror x отражает по вертикали (dy -> -dy),
    mirror y — по горизонтали (dx -> -dx). Порядок важен: при повороте на
    90/270 зеркало до поворота и после даёт разные точки.
    Проверено сверкой с самим KiCad — scripts/selftest.py сравнивает расчёт
    с координатами, которые KiCad печатает в отчёте ERC, для всех углов и
    зеркал. До этой сверки 90 и 270 были перепутаны местами, а зеркало
    применялось до поворота, и провода молча не попадали в выводы.
    """
    a = int(angle) % 360
    if a == 0:
        dx, dy = px, -py
    elif a == 90:
        dx, dy = -py, -px
    elif a == 180:
        dx, dy = -px, py
    elif a == 270:
        dx, dy = py, px
    else:
        raise ValueError("угол только 0/90/180/270")
    if mirror == "x":
        dy = -dy
    elif mirror == "y":
        dx = -dx
    return (round(at[0] + dx, 4), round(at[1] + dy, 4))


def pin_xy(lib_id, number, at, angle=0, mirror=None):
    """Абсолютные координаты вывода на листе."""
    px, py = symbol_pins(lib_id)[str(number)]
    return xform(px, py, at, angle, mirror)


_geom_cache = {}


def symbol_geometry(lib_id, unit=1):
    """Геометрия символа (одного его юнита) из подключённых библиотек, с кэшем."""
    key = (lib_id, unit)
    if key not in _geom_cache:
        _geom_cache[key] = geometry_from_tree(sexp.parse(load_symbol(lib_id)), unit)
    return _geom_cache[key]


def geometry_from_tree(tree, unit=1):
    """Геометрия символа в его координатах: корпус, выводы, скрытость подписей.

    Нужна, чтобы подписи ставились туда, где их не на что положить: не на
    корпус, не на выводы и не на номера выводов. Принимает разобранное
    определение символа — из библиотеки или из lib_symbols внутри .kicad_sch
    (так линтер не зависит от путей к библиотекам). unit — какой юнит
    многоюнитного символа нужен: у 40106 юнит 3 — это выводы 5 и 6, и провод к
    выводу 5 не должен считаться висящим из-за того, что взяты выводы юнита 1.
    Возвращает словарь:
      body   — (minx, miny, maxx, maxy) графики без выводов или None;
      pins   — [(номер, имя, x, y, угол, длина)];
      texts  — надписи, нарисованные внутри символа (например «V_DD»);
      names_hidden, numbers_hidden, names_offset;
      fields — {"Reference"|"Value": (x, y, угол)} из библиотеки.
    """
    xs, ys, pins, texts = [], [], [], []
    names_hidden = numbers_hidden = False
    names_offset = 0.508
    fields = {}

    def flag_hidden(node):
        return "hide" in node or any(isinstance(c, list) and c and c[0] == "hide"
                                     and (len(c) < 2 or c[1] == "yes") for c in node)

    for ch in tree:
        if not isinstance(ch, list) or not ch:
            continue
        if ch[0] == "pin_names":
            names_hidden = flag_hidden(ch)
            off = sexp.get(ch, "offset")
            if off:
                names_offset = float(off[1])
        elif ch[0] == "pin_numbers":
            numbers_hidden = flag_hidden(ch)
        elif ch[0] == "property" and len(ch) > 2 and ch[1] in ("Reference", "Value"):
            at = sexp.get(ch, "at")
            if at:
                fields[ch[1]] = (float(at[1]), float(at[2]),
                                 float(at[3]) if len(at) > 3 else 0)
        elif ch[0] == "symbol":
            m = re.search(r"_(\d+)_(\d+)$", ch[1])
            if m and (m.group(1) not in ("0", str(unit)) or m.group(2) not in ("0", "1")):
                continue            # другие юниты и альтернативный вид не рисуются
            for g in ch[2:]:
                if not isinstance(g, list) or not g:
                    continue
                if g[0] == "pin":
                    at = sexp.get(g, "at")
                    ln = sexp.get(g, "length")
                    nm = sexp.get(g, "name")
                    nu = sexp.get(g, "number")
                    pins.append((nu[1] if nu else "", nm[1] if nm else "",
                                 float(at[1]), float(at[2]),
                                 float(at[3]) if len(at) > 3 else 0.0,
                                 float(ln[1]) if ln else 2.54))
                elif g[0] == "text" and len(g) > 1:
                    texts.append(g[1])
                elif g[0] == "circle":
                    c, r = sexp.get(g, "center"), sexp.get(g, "radius")
                    if c and r:
                        cx, cy, rr = float(c[1]), float(c[2]), float(r[1])
                        xs += [cx - rr, cx + rr]
                        ys += [cy - rr, cy + rr]
                elif g[0] in ("rectangle", "polyline", "arc", "bezier"):
                    for node in sexp.find_all(g, "xy") + [sexp.get(g, t) for t in
                                                         ("start", "end", "mid")]:
                        if node:
                            xs.append(float(node[1]))
                            ys.append(float(node[2]))
    body = (min(xs), min(ys), max(xs), max(ys)) if xs else None
    return dict(body=body, pins=pins, texts=texts, names_hidden=names_hidden,
                numbers_hidden=numbers_hidden, names_offset=names_offset,
                fields=fields)


def _dir(angle):
    a = int(round(angle)) % 360
    return {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[a]


def field_orientation(angle=0, mirror=None, justify=None):
    """Как записать поле, чтобы на листе оно было ГОРИЗОНТАЛЬНЫМ и с нужным
    выравниванием: возвращает (угол поля в файле, justify в файле).

    Угол поля в .kicad_sch задан в системе координат символа, а KiCad
    складывает его с поворотом и зеркалом символа. Если в итоге направление
    строки смотрит влево, KiCad рисует текст читаемым, но зеркалит
    выравнивание: left становится right. Углы 180 и 270 писать нельзя —
    их KiCad рисует вверх ногами. Проверено по SVG KiCad 10.0.4 на всех
    сочетаниях поворота, зеркала и выравнивания (scripts/selftest.py).
    """
    a = int(angle) % 360
    fang = 90 if a in (90, 270) else 0
    dx, _dy = xform(*((1, 0) if fang == 0 else (0, 1)), (0, 0), a, mirror)
    if justify in ("left", "right") and dx < 0:
        justify = "right" if justify == "left" else "left"
    return fang, justify


def symbol_shapes(lib_id, at, angle=0, mirror=None, geo=None, unit=1):
    """Корпус и выводы символа (одного юнита) в координатах листа.

    Возвращает (body_rect | None, [(номер, имя, точка_подключения, конец_вывода)]).
    geo — готовая геометрия (линтер берёт её из lib_symbols файла схемы).
    """
    geo = geo or symbol_geometry(lib_id, unit)
    body = None
    if geo["body"]:
        x0, y0, x1, y1 = geo["body"]
        pts = [xform(x, y, at, angle, mirror) for x, y in
               ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
        body = (min(p[0] for p in pts), min(p[1] for p in pts),
                max(p[0] for p in pts), max(p[1] for p in pts))
    pins = []
    for num, name, px, py, pa, ln in geo["pins"]:
        dx, dy = _dir(pa)
        a = xform(px, py, at, angle, mirror)
        b = xform(px + dx * ln, py + dy * ln, at, angle, mirror)
        pins.append((num, name, a, b))
    return body, pins


def symbol_bbox(lib_id):
    """Габарит графики символа в его координатах: (minx, miny, maxx, maxy).

    Нужен, чтобы ссылка и номинал ложились снаружи корпуса. Ставить их по
    фиксированному смещению от точки привязки нельзя: у резистора тело 5 мм,
    у микросхемы — 20, и подпись оказывается поверх выводов. На печати это
    сливается в кашу, а найти виновника потом трудно — на экране при большом
    увеличении всё выглядит прилично.
    """
    block = load_symbol(lib_id)
    xs, ys = [], []
    for m in re.finditer(r'\((start|end|center|mid|xy)\s+([-\d.]+)\s+([-\d.]+)\)', block):
        xs.append(float(m.group(2)))
        ys.append(float(m.group(3)))
    for m in re.finditer(r'\(pin\s+\w+\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)', block):
        xs.append(float(m.group(1)))
        ys.append(float(m.group(2)))
    if not xs:
        return (-2.54, -2.54, 2.54, 2.54)
    return (min(xs), min(ys), max(xs), max(ys))



# ---------------------------------------------------------------------------
# Геометрия листа и расстановка подписей

PAGE_MARGIN = 12.0          # внутренняя рамка стандартного оформления KiCad, мм


def page_zones(paper):
    """Внутренняя рамка чертежа и штамп стандартного оформления KiCad.

    Сняты с SVG-экспорта KiCad 10: для A4 рамка (12, 12)-(285, 198),
    штамп (177, 166)-(285, 198). Штамп привязан к правому нижнему углу.
    """
    W, H = PAPER.get(paper, PAPER["A4"])
    border = (PAGE_MARGIN, PAGE_MARGIN, W - PAGE_MARGIN, H - PAGE_MARGIN)
    tblock = (W - 120.0, H - 44.0, W - PAGE_MARGIN, H - PAGE_MARGIN)
    return border, tblock


def rect_inflate(r, d):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def rect_overlap(a, b):
    """Площадь пересечения прямоугольников (0, если не пересекаются)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def rect_dist(a, b):
    """Зазор между прямоугольниками (0, если касаются или пересекаются)."""
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


# Подпись «ничья»: чужой корпус почти так же близко, как свой. Так подписи двух
# резисторов в ряд оказывались обе в промежутке между ними, одна сверху, другая
# снизу, и «R22» читалась как подпись R35. Ни на что не наложено, ERC и прочие
# проверки молчат, а читатель ошибается. Коэффициент 1,5, а не 1,25: при 1,25 подписи
# двух стабилитронов в ряд (3,2 мм до своего, 4,5 мм до соседа) проходили, хотя
# на листе обе стояли в промежутке между ними.
AMBIG_K, AMBIG_ADD = 1.5, 0.3


def label_ambiguous(box, own, others):
    """Чужой корпус из others, к которому подпись box не дальше, чем к своему own; иначе None."""
    d_own = rect_dist(box, own)
    for name, r in others:
        if rect_dist(box, r) <= AMBIG_K * d_own + AMBIG_ADD:
            return name
    return None


def rect_inside(a, b):
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def seg_rect(a, b, d=0.0):
    return (min(a[0], b[0]) - d, min(a[1], b[1]) - d,
            max(a[0], b[0]) + d, max(a[1], b[1]) + d)


def label_box(kind, text, at, angle=0, size=1.27):
    """Габарит надписи метки. У меток, в отличие от полей, угол задаёт
    сторону: 0 — текст вправо от точки, 180 — влево, 90 — вверх, 270 — вниз."""
    a = int(angle) % 360
    eff, just = {0: (0, "left"), 180: (0, "right"),
                 90: (90, "left"), 270: (90, "right")}[a]
    if kind == "glabel":
        return kfont.glabel_outline(text, at, size, a)
    return kfont.text_box(kind, text, at, size, eff, just)


def _segs_cross(p1, p2, q1, q2):
    """Пересекаются ли отрезки p1-p2 и q1-q2 (касание концами не считается)."""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 * o2 < 0 and o3 * o4 < 0


def _along_pin(pins, a, b):
    """Провод продолжает вывод наружу по его оси (свой провод детали)."""
    for _n, _nm, q, e in pins:
        dx, dy = q[0] - e[0], q[1] - e[1]
        for p0, p1 in ((a, b), (b, a)):
            if p0 == q:
                vx, vy = p1[0] - q[0], p1[1] - q[1]
                if abs(vx * dy - vy * dx) < 1e-6 and vx * dx + vy * dy > 0:
                    return True
    return False


def frames_conflicts(boxes):
    """Пары рамок, которые пересекаются частично (вложение допустимо)."""
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if rect_overlap(a, b) > 0 and not rect_inside(a, b) and not rect_inside(b, a):
                out.append((i, j))
    return out

# ---------------------------------------------------------------------------
# Элементы схемы

class _Item:
    def render(self, ctx=None):
        raise NotImplementedError


class Symbol(_Item):
    def __init__(self, ref, lib_id, at, value=None, footprint="", angle=0,
                 mirror=None, unit=1, dnp=False, fields=None, in_bom=True,
                 on_board=True, note=None):
        self.ref = ref
        self.lib_id = lib_id
        self.at = (snap(at[0]), snap(at[1]))
        self.value = value if value is not None else lib_id.split(":")[1]
        self.footprint = footprint
        self.angle = int(angle) % 360
        self.mirror = mirror
        self.unit = unit
        self.dnp = dnp
        self.in_bom = in_bom
        # on_board=False — деталь есть в изделии, но не на плате: выносной зонд,
        # щуп на кабеле, антенна. Посадочного места у неё нет и быть не должно,
        # а в перечне элементов она обязана остаться — это покупная позиция.
        self.on_board = on_board
        self.fields = fields or {}      # доп. поля: MPN, Manufacturer, Цена…
        self.note = note                # комментарий, выводится текстом рядом
        self.field_pos = None           # {"Reference"|"Value": (x, y, justify)} — от автопостановки

    def pin(self, number):
        return pin_xy(self.lib_id, number, self.at, self.angle, self.mirror)

    @property
    def multi(self):
        return len(symbol_units(self.lib_id)) > 1

    @property
    def display_ref(self):
        """Ссылка так, как её печатает KiCad: у части корпуса — с буквой (U1C)."""
        return self.ref + chr(ord("A") + self.unit - 1) if self.multi else self.ref

    def render(self, ctx):
        x, y = self.at
        paths = ctx.instance_paths(self.ref, self.unit)
        # UUID части корпуса зависит от юнита: шесть инверторов U1 — шесть
        # разных объектов листа. У первого юнита ключ прежний, чтобы
        # перегенерация уже существующих схем не меняла их идентификаторы.
        key = "sym:" + self.ref + (f":{self.unit}" if self.unit != 1 else "")
        # Экземпляр перечисляет только выводы своего юнита: выводы соседних
        # частей корпуса с теми же UUID в нескольких экземплярах KiCad считает
        # повреждением файла.
        nums = ({p[0] for p in symbol_geometry(self.lib_id, self.unit)["pins"]}
                if self.multi else set(symbol_pins(self.lib_id)))
        mir = f"\n\t\t(mirror {self.mirror})" if self.mirror else ""
        # Позиции подписей рассчитывает Sheet._autoplace() по карте занятости
        # листа: снаружи корпуса, мимо выводов, проводов, меток и чужих подписей.
        # Угол поля в KiCad складывается с углом символа, поэтому пишется
        # компенсирующий угол — подпись всегда горизонтальна и читается без
        # поворота головы.
        fp = self.field_pos or {}
        rx, ry, rj = fp.get("Reference", (x, y, None))
        vx, vy, vj = fp.get("Value", (x, y, None))
        fa, rj = field_orientation(self.angle, self.mirror, rj)
        _fa, vj = field_orientation(self.angle, self.mirror, vj)
        # У символов питания ссылка (#PWR003) не несёт смысла для читателя и
        # только засоряет лист: важно имя цепи, а оно и так написано в номинале.
        hide_ref = self.lib_id.startswith("power:")
        props = [
            _prop("Reference", self.ref, rx, ry, hide=hide_ref, justify=rj, angle=fa),
            _prop("Value", self.value, vx, vy, justify=vj, angle=fa),
            _prop("Footprint", self.footprint, x, y, hide=True),
            _prop("Datasheet", self.fields.get("Datasheet", ""), x, y, hide=True),
        ]
        for k, v in self.fields.items():
            if k == "Datasheet":
                continue
            props.append(_prop(k, v, x, y, hide=True))
        pins = "".join(
            f'\n\t\t(pin "{n}"\n\t\t\t(uuid "{uid(f"pin:{self.ref}:{n}")}")\n\t\t)'
            for n in sorted(nums))
        return f'''	(symbol
		(lib_id "{self.lib_id}")
		(at {x} {y} {self.angle}){mir}
		(unit {self.unit})
		(exclude_from_sim no)
		(in_bom {'yes' if self.in_bom else 'no'})
		(on_board {'yes' if self.on_board else 'no'})
		(dnp {'yes' if self.dnp else 'no'})
		(uuid "{uid(key)}")
{chr(10).join(props)}{pins}
		(instances
			(project "{ctx.project}"
{paths}
			)
		)
	)'''


def _prop(name, value, x, y, hide=False, justify=None, angle=0):
    # justify: только left/right; центрирование — это отсутствие justify.
    # Слово "center" ломает файл без указания места.
    esc = str(value).replace("\\", "\\\\").replace('"', '\\"')
    h = "\n\t\t\t(hide yes)" if hide else ""
    j = f"\n\t\t\t\t(justify {justify})" if justify in ("left", "right") else ""
    return f'''		(property "{name}" "{esc}"
			(at {round(x, 4)} {round(y, 4)} {int(angle) % 360}){h}
			(effects
				(font
					(size 1.27 1.27)
				){j}
			)
		)'''


class Wire(_Item):
    def __init__(self, a, b, key=None):
        self.a = (snap(a[0]), snap(a[1]))
        self.b = (snap(b[0]), snap(b[1]))
        self.key = key or f"wire:{self.a}:{self.b}"

    def render(self, ctx=None):
        return f'''	(wire
		(pts
			(xy {self.a[0]} {self.a[1]}) (xy {self.b[0]} {self.b[1]})
		)
		(stroke
			(width 0)
			(type default)
		)
		(uuid "{uid(self.key)}")
	)'''


class Junction(_Item):
    def __init__(self, at):
        self.at = (snap(at[0]), snap(at[1]))

    def render(self, ctx=None):
        return f'''	(junction
		(at {self.at[0]} {self.at[1]})
		(diameter 0)
		(color 0 0 0 0)
		(uuid "{uid(f'junction:{self.at}')}")
	)'''


class NoConnect(_Item):
    def __init__(self, at):
        self.at = (snap(at[0]), snap(at[1]))

    def render(self, ctx=None):
        return f'''	(no_connect
		(at {self.at[0]} {self.at[1]})
		(uuid "{uid(f'nc:{self.at}')}")
	)'''


class Label(_Item):
    """Локальная метка — имя цепи в пределах листа."""
    kind = "label"

    def __init__(self, at, text, angle=0, justify="left"):
        self.at = (snap(at[0]), snap(at[1]))
        self.text = text
        self.angle = int(angle) % 360
        self.justify = justify

    def render(self, ctx=None):
        return f'''	({self.kind}
		"{self.text}"
		(at {self.at[0]} {self.at[1]} {self.angle})
		(effects
			(font
				(size 1.27 1.27)
			)
			(justify {self.justify} bottom)
		)
		(uuid "{uid(f'{self.kind}:{self.text}:{self.at}')}")
	)'''


class GlobalLabel(Label):
    """Глобальная метка — цепь, видимая на всех листах."""
    kind = "global_label"

    def render(self, ctx=None):
        return f'''	(global_label
		"{self.text}"
		(shape bidirectional)
		(at {self.at[0]} {self.at[1]} {self.angle})
		(effects
			(font
				(size 1.27 1.27)
			)
			(justify {self.justify})
		)
		(uuid "{uid(f'glabel:{self.text}:{self.at}')}")
	)'''


class Text(_Item):
    """Пояснение на листе. Ради него всё и затевается: схема должна объяснять себя."""

    def __init__(self, at, text, size=1.27, justify="left"):
        self.at = (snap(at[0]), snap(at[1]))
        self.text = text
        self.size = size
        self.justify = justify

    def render(self, ctx=None):
        esc = self.text.replace("\\", "\\\\").replace('"', '\\"')
        return f'''	(text
		"{esc}"
		(at {self.at[0]} {self.at[1]} 0)
		(effects
			(font
				(size {self.size} {self.size})
			)
			(justify {self.justify})
		)
		(uuid "{uid(f'text:{self.at}')}")
	)'''


class Box(_Item):
    """Рамка вокруг функционального блока — главный инструмент читаемости."""

    def __init__(self, a, b, title=None):
        self.a = (snap(a[0]), snap(a[1]))
        self.b = (snap(b[0]), snap(b[1]))
        self.title = title

    def render(self, ctx=None):
        return f'''	(rectangle
		(start {self.a[0]} {self.a[1]})
		(end {self.b[0]} {self.b[1]})
		(stroke
			(width 0.1)
			(type dash)
		)
		(fill
			(type none)
		)
		(uuid "{uid(f'box:{self.a}:{self.b}')}")
	)'''


# ---------------------------------------------------------------------------
# Иерархические элементы


class HierLabel(_Item):
    """Вывод блока изнутри дочернего листа.

    Пара к SheetPin на родительском листе: имена обязаны совпадать, иначе
    KiCad считает это разными цепями и лист «не подключается». Это самая
    частая ошибка в иерархии, поэтому Project.check() сверяет их сам.
    """

    def __init__(self, at, text, shape="input", angle=0, justify="right"):
        self.at = (snap(at[0]), snap(at[1]))
        self.text = text
        self.shape = shape          # input | output | bidirectional | tri_state | passive
        self.angle = int(angle) % 360
        self.justify = justify

    def render(self, ctx=None):
        return f'''	(hierarchical_label "{self.text}"
		(shape {self.shape})
		(at {self.at[0]} {self.at[1]} {self.angle})
		(effects
			(font
				(size 1.27 1.27)
			)
			(justify {self.justify})
		)
		(uuid "{uid(f'hlabel:{self.text}:{self.at}')}")
	)'''


class SheetRef(_Item):
    """Прямоугольник дочернего листа на родительском листе.

    Выводы (pins) размещаются по левой и правой сторонам автоматически:
    входы слева, выходы справа — тогда поток сигнала на верхнем уровне читается
    слева направо без разглядывания стрелок.
    """

    def __init__(self, name, filename, at, size=(50.8, 25.4), pins=None):
        self.name = name
        self.filename = filename
        self.at = (snap(at[0]), snap(at[1]))
        self.size = (snap(size[0]), snap(size[1]))
        # [(имя, shape), ...] или [(имя, shape, "left"|"right"), ...].
        # Без стороны вход ставится слева, остальное справа. Сторона задаётся
        # явно, когда двунаправленная шина уходит к блоку, стоящему слева, —
        # иначе провод на верхнем листе обходит блок кругом.
        self.pins = [(p[0], p[1], p[2] if len(p) > 2 else
                      ("left" if p[1] == "input" else "right"))
                     for p in (pins or [])]
        self.uuid = uid(f"sheet:{filename}")

    def pin_xy(self, name):
        """Координата вывода листа — чтобы дотянуть до него провод."""
        left = [p for p in self.pins if p[2] == "left"]
        right = [p for p in self.pins if p[2] != "left"]
        for lst, x in ((left, self.at[0]), (right, self.at[0] + self.size[0])):
            for k, (nm, _sh, _side) in enumerate(lst):
                if nm == name:
                    return (x, snap(self.at[1] + GRID * 2 * (k + 1)))
        raise KeyError(f"вывод {name} не объявлен у листа {self.name}")

    def render(self, ctx=None):
        parts = []
        for nm, sh, _side in self.pins:
            x, y = self.pin_xy(nm)
            ang = 180 if x == self.at[0] else 0
            # Имя вывода листа пишется ВНУТРИ прямоугольника: снаружи оно
            # легло бы на провод, подходящий к выводу.
            just = "left" if ang == 180 else "right"
            parts.append(f'''		(pin "{nm}" {sh}
			(at {x} {y} {ang})
			(effects
				(font
					(size 1.27 1.27)
				)
				(justify {just})
			)
			(uuid "{uid(f'spin:{self.filename}:{nm}')}")
		)''')
        pins = ("\n" + "\n".join(parts)) if parts else ""
        page = ctx.page_of(self.filename) if ctx else "2"
        return f'''	(sheet
		(at {self.at[0]} {self.at[1]})
		(size {self.size[0]} {self.size[1]})
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(dnp no)
		(stroke
			(width 0.1524)
			(type solid)
		)
		(fill
			(color 0 0 0 0.0000)
		)
		(uuid "{self.uuid}")
		(property "Sheetname" "{self.name}"
			(at {self.at[0]} {self.at[1] - 0.7} 0)
			(effects
				(font
					(size 1.524 1.524)
				)
				(justify left bottom)
			)
		)
		(property "Sheetfile" "{self.filename}"
			(at {self.at[0]} {self.at[1] + self.size[1] + 1.3} 0)
			(effects
				(font
					(size 1.524 1.524)
				)
				(justify left top)
			)
		){pins}
		(instances
			(project "{ctx.project if ctx else ''}"
				(path "/{ctx.root_uuid if ctx else ''}"
					(page "{page}")
				)
			)
		)
	)'''


# ---------------------------------------------------------------------------
# Лист и проект


class _Ctx:
    """Контекст рендеринга: знает имя проекта и путь листа в иерархии."""

    def __init__(self, project, root_uuid, sheet_paths, pages):
        self.project = project
        self.root_uuid = root_uuid
        self._paths = sheet_paths       # список путей, где этот лист встречается
        self._pages = pages             # {имя файла: номер страницы}

    def instance_paths(self, ref, unit):
        return "\n".join(
            f'''				(path "{p}"
					(reference "{ref}")
					(unit {unit})
				)''' for p in self._paths)

    def page_of(self, filename):
        return self._pages.get(filename, "2")


class Sheet:
    """Один лист схемы = один файл .kicad_sch."""

    def __init__(self, filename, title="", rev="A", company="", paper="A4",
                 comments=None):
        self.filename = filename
        self.title = title
        self.rev = rev
        self.company = company
        self.paper = paper
        self.comments = comments or {}
        self.items = []
        self.uuid = uid(f"sheetfile:{filename}")

    # --- добавление ------------------------------------------------------
    def add(self, item):
        self.items.append(item)
        return item

    def sym(self, ref, lib_id, at, **kw):
        unit = kw.get("unit", 1)
        if any(isinstance(i, Symbol) and i.ref == ref and i.unit == unit for i in self.items):
            raise ValueError(f"ссылка {ref} (юнит {unit}) уже занята на листе {self.filename}")
        return self.add(Symbol(ref, lib_id, at, **kw))

    def wire(self, a, b, key=None):
        return self.add(Wire(a, b, key))

    def bus(self, points):
        for i in range(len(points) - 1):
            self.wire(points[i], points[i + 1], key=f"poly:{self.filename}:{points}:{i}")

    def junction(self, at):
        return self.add(Junction(at))

    def nc(self, at):
        return self.add(NoConnect(at))

    def label(self, at, text, **kw):
        return self.add(Label(at, text, **kw))

    def glabel(self, at, text, **kw):
        return self.add(GlobalLabel(at, text, **kw))

    def hlabel(self, at, text, shape="input", **kw):
        return self.add(HierLabel(at, text, shape=shape, **kw))

    def text(self, at, text, **kw):
        return self.add(Text(at, text, **kw))

    def note(self, at, lines, size=1.27):
        """Многострочное пояснение. Схема обязана объяснять себя сама."""
        for k, line in enumerate(lines):
            self.text((at[0], at[1] + k * 2 * GRID), line, size=size)

    def box(self, a, b, title=None):
        if title:
            # Заголовок стоит над рамкой на 2 клетки: на одной клетке скобки и
            # буквы с выносными элементами («у», «(») задевают её линию.
            self.text((min(a[0], b[0]) + GRID, min(a[1], b[1]) - 2 * GRID), title,
                      size=1.778)
        return self.add(Box(a, b, title))

    def power(self, net, at, angle=0, ref=None):
        """Символ питания. Ссылку (#PWRxxx) присваивает Project.save() сквозной
        нумерацией по всему проекту: одинаковые #PWR на разных листах — это
        ошибка аннотации, из-за которой KiCad отказывается строить netlist."""
        lib = "power:" + net
        n = sum(1 for i in self.items if isinstance(i, Symbol)
                and i.lib_id.startswith("power:"))
        r = ref or f"#PWR?{self.filename}:{n:03d}"
        return self.sym(r, lib, at, value=net, angle=angle, in_bom=False)

    def sheet_ref(self, name, filename, at, size=(50.8, 25.4), pins=None):
        return self.add(SheetRef(name, filename, at, size, pins))


    # --- расстановка подписей ----------------------------------------------
    def box_rects(self):
        return [(min(b.a[0], b.b[0]), min(b.a[1], b.b[1]),
                 max(b.a[0], b.b[0]), max(b.a[1], b.b[1]))
                for b in self.items if isinstance(b, Box)]

    def _obstacles(self):
        """Всё, на что подпись ложиться не должна, — прямоугольниками."""
        obs = []
        for it in self.items:
            if isinstance(it, Wire):
                obs.append(seg_rect(it.a, it.b, 0.2))
            elif isinstance(it, Junction):
                obs.append(rect_inflate((*it.at, *it.at), 0.6))
            elif isinstance(it, NoConnect):
                obs.append(rect_inflate((*it.at, *it.at), 0.8))
            elif isinstance(it, (GlobalLabel, HierLabel)):
                obs.append(label_box("glabel", it.text, it.at, it.angle))
            elif isinstance(it, Label):
                obs.append(label_box("label", it.text, it.at, it.angle))
            elif isinstance(it, Text):
                obs.append(kfont.text_box("text", it.text, it.at, it.size, 0, it.justify))
            elif isinstance(it, Box):
                x0, y0 = it.a
                x1, y1 = it.b
                for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                             ((x0, y1), (x1, y1)), ((x0, y0), (x0, y1))):
                    obs.append(seg_rect(a, b, 0.2))
            elif isinstance(it, SheetRef):
                x, y = it.at
                obs.append((x, y - 3.0, x + max(it.size[0], 40), y + it.size[1] + 3.5))
            elif isinstance(it, Symbol):
                geo = symbol_geometry(it.lib_id, it.unit)
                body, pins = symbol_shapes(it.lib_id, it.at, it.angle, it.mirror, unit=it.unit)
                if body:
                    obs.append(rect_inflate(body, 0.2))
                # Номера выводов стоят над линией вывода: полоса вокруг вывода
                # занята, если номера видны.
                d = 0.3 if geo["numbers_hidden"] else 1.5
                for _n, _nm, a, b in pins:
                    obs.append(seg_rect(a, b, d))
        return obs

    @staticmethod
    def _field_candidates(sym, body, pins, n):
        """Позиции блока подписей по убыванию предпочтения.

        Сначала стороны корпуса без выводов: у вертикального резистора это
        бока, у горизонтального — верх и низ, у символа питания — сторона,
        противоположная выводу. Затем углы и стороны за концами выводов.
        """
        ax, ay = sym.at
        B = body or (ax - 1.27, ay - 1.27, ax + 1.27, ay + 1.27)
        F = B
        sides = set()
        for _n, _nm, a, b in pins:
            F = (min(F[0], a[0], b[0]), min(F[1], a[1], b[1]),
                 max(F[2], a[0], b[0]), max(F[3], a[1], b[1]))
            d = {"left": B[0] - a[0], "right": a[0] - B[2],
                 "top": B[1] - a[1], "bottom": a[1] - B[3]}
            sides.add(max(d, key=d.get))
        P = 1.905
        TOP, BOT = kfont._vert("field", 1.27)
        cx, cy = (B[0] + B[2]) / 2, (B[1] + B[3]) / 2

        def col(x, just, yc):
            return [(x, yc + (i - (n - 1) / 2) * P - (TOP + BOT) / 2, just) for i in range(n)]

        def above(x, just, edge, g, k=n):
            yl = edge - g - BOT
            return [(x, yl - (k - 1 - i) * P, just) for i in range(k)]

        def below(x, just, edge, g, k=n):
            y0 = edge + g - TOP
            return [(x, y0 + i * P, just) for i in range(k)]

        out = []
        for g in (0.635, 1.905, 3.81):
            R = F[2] if "right" in sides else B[2]
            L = F[0] if "left" in sides else B[0]
            T = F[1] if "top" in sides else B[1]
            D = F[3] if "bottom" in sides else B[3]
            shifts = (0, -1.905, 1.905, -3.81, 3.81)
            right = [col(R + g, "left", cy + dy) for dy in shifts]
            left = [col(L - g, "right", cy + dy) for dy in shifts]
            top = [above(cx + dx, None, T, g) for dx in (0, -2.54, 2.54)]
            bottom = [below(cx + dx, None, D, g) for dx in (0, -2.54, 2.54)]
            split = ([above(cx, None, T, g, 1) + below(cx, None, D, g, 1)] if n == 2 else [])
            corners = [above(B[2], "right", B[1], g), above(B[0], "left", B[1], g),
                       below(B[2], "right", B[3], g), below(B[0], "left", B[3], g),
                       above(F[2] + g, "left", F[1], 0), below(F[2] + g, "left", F[3], 0),
                       above(F[0] - g, "right", F[1], 0), below(F[0] - g, "right", F[3], 0)]
            if not sides:
                order = right + top + left + bottom + corners
            elif sides <= {"top", "bottom"}:
                order = right + left + corners + top + bottom
            elif sides <= {"left", "right"}:
                order = split + top + bottom + corners + right + left
            elif len(sides) == 1:
                side = next(iter(sides))
                opp = {"top": bottom, "bottom": top, "left": right, "right": left}[side]
                rest = {"top": right + left + top, "bottom": right + left + bottom,
                        "left": top + bottom + left, "right": top + bottom + right}[side]
                order = opp + corners + rest
            else:
                free = []
                for name, lst in (("right", right), ("top", top), ("left", left), ("bottom", bottom)):
                    if name not in sides:
                        free += lst
                pinned = []
                for name, lst in (("top", top), ("bottom", bottom), ("right", right), ("left", left)):
                    if name in sides:
                        pinned += lst
                order = free + split + corners + pinned
            out += order
        return out

    def _autoplace(self):
        """Ставит Reference и Value каждого символа туда, где они ни на что
        не ложатся: ни на провод, ни на корпус, ни на вывод, ни на метку,
        ни на чужую подпись, ни на рамку, и не выходят из рамки своего блока.

        Порядок детерминирован (питание, затем крупные символы, затем по
        ссылке), поэтому перегенерация даёт тот же файл. Если чистого места
        нет, берётся позиция с наименьшим пересечением и об этом пишется в
        журнал — линтер всё равно покажет её как ошибку.
        """
        border, tblock = page_zones(self.paper)
        obs = self._obstacles()
        wires = [(w.a, w.b) for w in self.items if isinstance(w, Wire)]
        frames = self.box_rects()
        self.place_log = []

        def prio(sym):
            return (0 if sym.lib_id.startswith("power:") else 1,
                    -len(symbol_geometry(sym.lib_id, sym.unit)["pins"]), sym.ref, sym.unit)

        # Корпуса деталей — чтобы подпись не встала «ничьей», между двумя.
        bodies = []
        for it in self.items:
            if isinstance(it, Symbol) and not it.lib_id.startswith("power:"):
                b, _p = symbol_shapes(it.lib_id, it.at, it.angle, it.mirror, unit=it.unit)
                if b:
                    bodies.append((it, b))

        for sym in sorted((i for i in self.items if isinstance(i, Symbol)), key=prio):
            body, pins = symbol_shapes(sym.lib_id, sym.at, sym.angle, sym.mirror, unit=sym.unit)
            power = sym.lib_id.startswith("power:")
            others = [(o.display_ref, b) for o, b in bodies if o is not sym]
            own = body or (sym.at[0], sym.at[1], sym.at[0], sym.at[1])
            # Ширину ссылки считаем по тому, что напечатает KiCad: «U1C», а не «U1».
            lines = [("Value", sym.value)] if power else [("Reference", sym.display_ref),
                                                          ("Value", sym.value)]
            c = ((body[0] + body[2]) / 2, (body[1] + body[3]) / 2) if body else sym.at
            home = [f for f in frames if f[0] <= c[0] <= f[2] and f[1] <= c[1] <= f[3]]
            home = min(home, key=lambda f: (f[2] - f[0]) * (f[3] - f[1])) if home else None
            best = None
            for cand in self._field_candidates(sym, body, pins, len(lines)):
                boxes = [kfont.text_box("field", txt, (x, y), 1.27, 0, j)
                         for (_nm, txt), (x, y, j) in zip(lines, cand)]
                cost = 0.0
                for b in boxes:
                    t = rect_inflate(b, 0.5)
                    if not rect_inside(b, border):
                        cost += 1000
                    cost += 100 * rect_overlap(t, tblock)
                    if home and not rect_inside(b, rect_inflate(home, -0.3)):
                        cost += 500
                    for o in obs:
                        ov = rect_overlap(t, o)
                        if ov:
                            cost += 1 + 10 * ov
                    if not power and label_ambiguous(b, own, others):
                        cost += 5
                # Подпись не должна быть отделена от своей детали проводом:
                # иначе она читается как подпись соседа.
                cx = sum((b[0] + b[2]) / 2 for b in boxes) / len(boxes)
                cy = sum((b[1] + b[3]) / 2 for b in boxes) / len(boxes)
                for wa, wb in wires:
                    if _along_pin(pins, wa, wb):
                        continue
                    if _segs_cross(c, (cx, cy), wa, wb):
                        cost += 3
                if best is None or cost < best[0]:
                    best = (cost, cand, boxes)
                if cost == 0:
                    break
            cost, cand, boxes = best
            if cost > 0:
                self.place_log.append(f"{self.filename}: {sym.ref} — подписи без пересечений "
                                      f"не поместились (штраф {cost:.1f}), нужна раздвижка")
            sym.field_pos = {nm: (round(x, 3), round(y, 3), j)
                             for (nm, _t), (x, y, j) in zip(lines, cand)}
            obs.extend(boxes)

    # --- вывод -----------------------------------------------------------
    def render(self, ctx):
        if not getattr(self, "_placed", False):
            self._autoplace()
            self._placed = True
        libs = sorted({i.lib_id for i in self.items if isinstance(i, Symbol)})
        lib_block = "\n".join(
            "\t\t" + load_symbol(l).replace("\n", "\n\t\t") for l in libs)
        body = "\n".join(i.render(ctx) for i in self.items)
        cm = "".join(f'\n\t\t(comment {k} "{v}")'
                     for k, v in sorted(self.comments.items()))
        tail = ""
        if ctx.is_root:
            paths = "\n".join(
                f'''		(path "{p}"
			(page "{pg}")
		)''' for p, pg in ctx.all_pages)
            tail = f"\n\t(sheet_instances\n{paths}\n\t)"
        return f'''(kicad_sch
	(version {SCH_VERSION})
	(generator "schlib")
	(generator_version "{GEN_VERSION}")
	(uuid "{self.uuid}")
	(paper "{self.paper}")
	(title_block
		(title "{self.title}")
		(rev "{self.rev}")
		(company "{self.company}"){cm}
	)
	(lib_symbols
{lib_block}
	){chr(10) + body if body else ''}{tail}
	(embedded_fonts no)
)
'''


class Project:
    """Проект: корневой лист плюс дочерние.

    Имя проекта обязано совпадать с именем .kicad_pro, иначе KiCad не свяжет
    ссылки символов с листами и при открытии переспросит аннотацию.
    """

    def __init__(self, name, outdir=".", title="", rev="A", company="",
                 paper="A4"):
        self.name = name
        self.outdir = outdir
        self.root = Sheet(f"{name}.kicad_sch", title=title or name,
                          rev=rev, company=company, paper=paper)
        self.sheets = []                # [(Sheet, SheetRef)]

    def sheet(self, filename, name, at, size=(50.8, 25.4), pins=None,
              title="", rev=None, company="", paper="A4"):
        """Создаёт дочерний лист и его прямоугольник на корневом листе."""
        sh = Sheet(filename, title=title or name, rev=rev or self.root.rev,
                   company=company or self.root.company, paper=paper)
        ref = self.root.sheet_ref(name, filename, at, size, pins)
        self.sheets.append((sh, ref))
        return sh

    # --- проверка целостности -------------------------------------------
    def check(self):
        """Сверяет выводы листов с иерархическими метками внутри них."""
        problems = []
        for sh, ref in self.sheets:
            declared = {p[0] for p in ref.pins}
            inside = {i.text for i in sh.items if isinstance(i, HierLabel)}
            for n in declared - inside:
                problems.append(
                    f"{ref.name}: вывод «{n}» объявлен на родительском листе, "
                    f"но в {sh.filename} нет hierarchical_label с таким именем")
            for n in inside - declared:
                problems.append(
                    f"{ref.name}: метка «{n}» есть в {sh.filename}, "
                    f"но не объявлена выводом листа — цепь никуда не выйдет")
        # Части одного корпуса (U1A на одном листе, U1D на другом) — не повтор:
        # повтором считается одна и та же пара «ссылка + юнит» дважды.
        refs = {}
        libs = {}
        for sh in [self.root] + [s for s, _ in self.sheets]:
            for i in sh.items:
                if isinstance(i, Symbol) and not i.ref.startswith("#PWR?"):
                    refs.setdefault((i.ref, i.unit), []).append(sh.filename)
                    libs.setdefault(i.ref, set()).add(i.lib_id)
        for (r, u), files in refs.items():
            if len(files) > 1:
                name = f"{r} (юнит {u})" if u != 1 else r
                problems.append(f"ссылка {name} повторяется в {', '.join(files)}")
        for r, ids in libs.items():
            if len(ids) > 1:
                problems.append(f"ссылка {r} у разных символов: {', '.join(sorted(ids))}")
        return problems

    # --- запись ----------------------------------------------------------
    def _renumber_power(self):
        n = 0
        for sh in [self.root] + [s for s, _ in self.sheets]:
            for i in sh.items:
                if isinstance(i, Symbol) and i.ref.startswith("#PWR?"):
                    n += 1
                    i.ref = f"#PWR{n:03d}"

    def save(self):
        self._renumber_power()
        pages = {self.root.filename: "1"}
        for k, (sh, _r) in enumerate(self.sheets, start=2):
            pages[sh.filename] = str(k)
        all_pages = [("/", "1")]
        for sh, ref in self.sheets:
            all_pages.append((f"/{ref.uuid}", pages[sh.filename]))

        written = []
        ctx = _Ctx(self.name, self.root.uuid, [f"/{self.root.uuid}"], pages)
        ctx.is_root = True
        ctx.all_pages = all_pages
        p = os.path.join(self.outdir, self.root.filename)
        open(p, "w", encoding="utf-8").write(self.root.render(ctx))
        written.append(p)

        for sh, ref in self.sheets:
            c = _Ctx(self.name, self.root.uuid,
                     [f"/{self.root.uuid}/{ref.uuid}"], pages)
            c.is_root = False
            c.all_pages = all_pages
            p = os.path.join(self.outdir, sh.filename)
            open(p, "w", encoding="utf-8").write(sh.render(c))
            written.append(p)
        self.warnings = []
        for sh in [self.root] + [s for s, _ in self.sheets]:
            self.warnings += getattr(sh, "place_log", [])
            fr = sh.box_rects()
            for i, j in frames_conflicts(fr):
                self.warnings.append(f"{sh.filename}: рамки {fr[i]} и {fr[j]} пересекаются")
        for w in self.warnings:
            print("ПРЕДУПРЕЖДЕНИЕ:", w)
        return written
