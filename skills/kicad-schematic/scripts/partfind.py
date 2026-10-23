#!/usr/bin/env python3
"""
partfind.py — подбор компонентов из локального каталога Промэлектроники.

Зачем: деталь в схеме должна быть покупаемой. Каталог — товары в наличии на
www.promelec.ru по разделам, актуальным для разработки; собирается парсером
~/Proj/3.Resourses/parser и лежит в ~/Proj/3.Resourses/components/promelec.
Искать в нём быстрее и честнее, чем вспоминать парт-номер: сразу видно,
есть ли деталь на складе, сколько штук и почём.

Использование:
    python3 partfind.py stats                         свежесть и объём каталога
    python3 partfind.py cats <подстрока>              найти раздел (все 3 уровня)
    python3 partfind.py params <ключ раздела>         какие параметры есть в разделе
    python3 partfind.py find <текст> [фильтры]        найти товар
    python3 partfind.py show <имя | good_id>          карточка товара целиком

Фильтры find:
    --cat 1/15/108          раздел или ветка (можно повторять)
    -p "Корпус=SOT-23"      параметр содержит значение (можно повторять)
    --brand TI  --package 0402
    --min-stock 100         на складе не меньше
    --max-price 50
    --sort price|stock|name --limit 30

Текст ищется в названии и описании, без учёта регистра; пустая строка ""
— без условия на текст. Значения параметров на сайте свободные
(«2 В ~ 3.6 В»), поэтому -p сравнивает подстроки, а не числа.

Каталог: --db путь или переменная PROMELEC_DB.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.expanduser("~/Proj/3.Resourses/components/promelec/catalog.db")
PARSER_DIR = os.path.expanduser("~/Proj/3.Resourses/parser")
STALE_DAYS = 30


def connect(path):
    if not os.path.exists(path):
        sys.exit(f"Каталог не найден: {path}\n"
                 f"Собрать: cd {PARSER_DIR} && venv/bin/python parse_products.py")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    # SQLite LOWER не знает кириллицу
    db.create_function("ulower", 1, lambda s: s.lower() if isinstance(s, str) else s)
    return db


def _age_days(iso):
    return (datetime.now() - datetime.fromisoformat(iso)).days if iso else None


def _table(rows, cols):
    """rows — список словарей, cols — [(ключ, заголовок, ширина)]."""
    print("  ".join(f"{h:<{w}}"[:w] for _, h, w in cols))
    for r in rows:
        cells = []
        for k, _, w in cols:
            v = r[k]
            v = "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))
            cells.append(f"{v:<{w}}"[:w])
        print("  ".join(cells))


def _cat_where(keys):
    """Условие «товар лежит в ветке» через product_categories."""
    if not keys:
        return "", []
    parts, args = [], []
    for k in keys:
        k = k.strip("/")
        parts.append("(pc.category_key = ? OR pc.category_key LIKE ?)")
        args += [k, k + "/%"]
    sql = ("p.good_id IN (SELECT pc.good_id FROM product_categories pc WHERE "
           + " OR ".join(parts) + ")")
    return sql, args


# ------------------------------------------------------------

def cmd_stats(db, a):
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    print(f"Каталог собран: {meta.get('built_at')}   источник: {meta.get('source')}")
    c = db.execute("""SELECT COUNT(*) n, SUM(parsed_at IS NOT NULL) parsed,
                             SUM(selected) sel, MIN(parsed_at) oldest, MAX(parsed_at) newest
                      FROM categories""").fetchone()
    print(f"Разделов в дереве: {c['n']}, выбрано для разработки: {c['sel']}, "
          f"спарсено: {c['parsed']} (с {c['oldest']} по {c['newest']})")
    print(f"Товаров в наличии: {db.execute('SELECT COUNT(*) FROM products').fetchone()[0]}")
    miss = db.execute("SELECT key, path FROM categories WHERE selected=1 AND parsed_at IS NULL").fetchall()
    if miss:
        print(f"Выбраны, но не спарсены ({len(miss)}): " + ", ".join(r["key"] for r in miss[:15]))
    part = db.execute("SELECT key FROM categories WHERE complete=0").fetchall()
    if part:
        print("Спарсены не полностью: " + ", ".join(r["key"] for r in part))


def cmd_cats(db, a):
    q = f"%{a.text.lower()}%"
    rows = db.execute("""
        SELECT key, path, count_total, count_instock, parsed_at, selected FROM categories
        WHERE ulower(path) LIKE ? OR key = ? OR key LIKE ?
        ORDER BY key""", (q, a.text.strip("/"), a.text.strip("/") + "/%")).fetchall()
    if not rows:
        print("Раздела нет в дереве. Обновить дерево: "
              f"cd {PARSER_DIR} && venv/bin/python parser_category.py")
        return
    out = []
    for r in rows:
        age = _age_days(r["parsed_at"])
        state = ("не спарсен" if r["parsed_at"] is None else
                 f"{age} дн." + (" — устарел" if age > STALE_DAYS else ""))
        out.append({**dict(r), "state": state, "sel": "да" if r["selected"] else ""})
    _table(out, [("key", "ключ", 18), ("path", "раздел", 70), ("count_total", "всего", 6),
                 ("count_instock", "в налич.", 8), ("state", "данные", 18), ("sel", "выбран", 6)])


def cmd_params(db, a):
    sql, args = _cat_where([a.cat])
    rows = db.execute(f"""
        SELECT pr.name, COUNT(*) n, COUNT(DISTINCT pr.value) nv FROM params pr
        JOIN products p ON p.good_id = pr.good_id WHERE {sql}
        GROUP BY pr.name ORDER BY n DESC""", args).fetchall()
    if not rows:
        print("В разделе нет товаров — раздел не спарсен или пуст. Проверить: partfind.py cats " + a.cat)
        return
    for r in rows:
        top = db.execute(f"""
            SELECT pr.value, COUNT(*) c FROM params pr JOIN products p ON p.good_id = pr.good_id
            WHERE pr.name = ? AND {sql} GROUP BY pr.value ORDER BY c DESC LIMIT 8""",
                         [r["name"]] + args).fetchall()
        print(f"{r['name']} ({r['n']} товаров, {r['nv']} значений): "
              + " | ".join(f"{t['value']} ({t['c']})" for t in top))


def cmd_find(db, a):
    where, args = [], []
    if a.text:
        where.append("(ulower(p.name) LIKE ? OR ulower(p.description) LIKE ?)")
        args += [f"%{a.text.lower()}%"] * 2
    sql, cargs = _cat_where(a.cat)
    if sql:
        where.append(sql)
        args += cargs
    for f in a.param:
        if "=" not in f:
            sys.exit(f"-p ожидает ИМЯ=ЗНАЧЕНИЕ, получено: {f}")
        name, value = f.split("=", 1)
        where.append("p.good_id IN (SELECT good_id FROM params "
                     "WHERE ulower(name) LIKE ? AND ulower(value) LIKE ?)")
        args += [f"%{name.strip().lower()}%", f"%{value.strip().lower()}%"]
    if a.brand:
        where.append("ulower(p.brand) LIKE ?")
        args.append(f"%{a.brand.lower()}%")
    if a.package:
        where.append("ulower(p.package) LIKE ?")
        args.append(f"%{a.package.lower()}%")
    if a.min_stock:
        where.append("p.stock >= ?")
        args.append(a.min_stock)
    if a.max_price is not None:
        where.append("p.price <= ?")
        args.append(a.max_price)
    order = {"price": "p.price IS NULL, p.price", "stock": "p.stock DESC",
             "name": "p.name"}[a.sort]
    rows = db.execute(f"""
        SELECT p.* FROM products p {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY {order} LIMIT ?""", args + [a.limit]).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM products p "
                       f"{'WHERE ' + ' AND '.join(where) if where else ''}", args).fetchone()[0]
    if not rows:
        print("Ничего не найдено. Дальше: ослабить фильтры; проверить, спарсен ли раздел "
              "(partfind.py cats ...); если подходящего нет — спросить пользователя.")
        return
    _table(rows, [("name", "наименование", 26), ("brand", "производитель", 18),
                  ("package", "корпус", 12), ("price", "цена ₽", 9), ("stock", "склад", 8),
                  ("description", "описание", 60), ("category_key", "раздел", 16)])
    print(f"— показано {len(rows)} из {total}")
    ages = [_age_days(r["parsed_at"]) for r in rows]
    if ages and max(ages) > STALE_DAYS:
        print(f"Внимание: данные старше {STALE_DAYS} дней — наличие и цены могли измениться")


def cmd_show(db, a):
    rows = db.execute("SELECT * FROM products WHERE good_id = ? OR ulower(name) = ?",
                      (a.name, a.name.lower())).fetchall()
    if not rows:
        rows = db.execute("SELECT * FROM products WHERE ulower(name) LIKE ? LIMIT 5",
                          (f"%{a.name.lower()}%",)).fetchall()
    if not rows:
        print("Нет в каталоге.")
        return
    for r in rows:
        print(f"\n{r['name']} — {r['description'] or ''}")
        for k in ("brand", "package", "price", "stock", "external_stock", "analogs",
                  "good_id", "url", "category_path", "parsed_at"):
            print(f"  {k:<15} {r[k]}")
        cats = [x[0] for x in db.execute(
            "SELECT category_key FROM product_categories WHERE good_id = ?", (r["good_id"],))]
        print(f"  {'разделы':<15} {', '.join(cats)}")
        for k, v in json.loads(r["params_json"] or "{}").items():
            print(f"    {k}: {v}")


def main():
    ap = argparse.ArgumentParser(description="Подбор компонентов из каталога Промэлектроники")
    ap.add_argument("--db", default=os.environ.get("PROMELEC_DB", DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats")
    p = sub.add_parser("cats")
    p.add_argument("text")
    p = sub.add_parser("params")
    p.add_argument("cat")
    p = sub.add_parser("show")
    p.add_argument("name")
    p = sub.add_parser("find")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--cat", action="append", default=[])
    p.add_argument("-p", "--param", action="append", default=[])
    p.add_argument("--brand")
    p.add_argument("--package")
    p.add_argument("--min-stock", type=int)
    p.add_argument("--max-price", type=float)
    p.add_argument("--sort", choices=["price", "stock", "name"], default="stock")
    p.add_argument("--limit", type=int, default=30)

    a = ap.parse_args()
    db = connect(a.db)
    {"stats": cmd_stats, "cats": cmd_cats, "params": cmd_params,
     "find": cmd_find, "show": cmd_show}[a.cmd](db, a)


if __name__ == "__main__":
    main()
