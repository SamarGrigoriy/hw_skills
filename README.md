# hw-skills — скиллы Claude Code для разработки электроники

Самостоятельный проект: скиллы не ссылаются ни на один проект платы. Проекты
плат пользуются скиллами, а не наоборот.

| Скилл | Что делает |
|---|---|
| `skills/kicad-schematic/` | схема KiCad 10 как код: генерация, линтер читаемости, ERC, подбор деталей из каталога Промэлектроники, BOM |
| `skills/kicad-pcb/` | разводка платы KiCad 10 как код: размещение, трассировщик, ЭМС-линтер, нормы заводов, DRC |
| `skills/pcb-review/` | сторонний, для справки; написан под чужой проект, не подключается |
| `skills/freecad-skills/` | справочник по скриптам FreeCAD; не подключается |

## Установка

```bash
./install.sh        # симлинки kicad-schematic и kicad-pcb в ~/.claude/skills
```

## Внешние зависимости

- KiCad 10.0.x (`kicad-cli`, библиотеки символов и посадочных мест в `/usr/share/kicad`);
- `pdftoppm` (poppler-utils) и Python 3 с PIL — картинки листов в воротах `check.py`;
- каталог Промэлектроники `~/Proj/3.Resourses/components/promelec/catalog.db`
  и парсер `~/Proj/3.Resourses/parser/` — это общий ресурс, а не проект;
  путь меняется ключом `--db` у `partfind.py`.

## Проверка после правки скриптов

```bash
python3 skills/kicad-schematic/scripts/selftest.py
cd skills/kicad-schematic/template && python3 ../scripts/check.py example.kicad_sch --gen gen_sch.py
```

## Проект платы, которому нужны скрипты напрямую

Генераторы схем вызывают `schlib.py` по пути. В проекте платы либо путь
до `hw-skills/skills/...`, либо симлинк `skills -> ~/Proj/1.Project/hw-skills/skills`
(так подключён Smart_Pole).
