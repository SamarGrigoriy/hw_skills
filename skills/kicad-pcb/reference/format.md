# Формат платы KiCad 10 и его ловушки

Проверено на KiCad **10.0.4**: формат `.kicad_pcb` — `version 20260206`,
примеры сверены с демо-платой `/usr/share/kicad/demos/pic_programmer`.
Всё ниже — S-выражения; разбор — своим парсером, как в скилле схем
(`skills/kicad-schematic/scripts/sexp.py`).

## Python-модуль pcbnew не использовать

`import pcbnew` работает, но `pcbnew.LoadBoard(...)` в standalone Python
**падает с ошибкой сегментации** — и на плате проекта, и на демо-плате KiCad.
`PCB_IO_MGR.Load` в этой сборке не принимает аргументы из Python. IPC API
(`kipy`) не установлен и требует запущенного KiCad.

Поэтому инструмент читает и пишет `.kicad_pcb` сам, а KiCad вызывает только
через `kicad-cli`: заливка, DRC, сверка со схемой, картинки, 3D. Если в новой
версии KiCad `LoadBoard` заработает, это не повод переходить на него: файл,
собранный своим писателем, воспроизводим и даёт чистый `git diff`.

## Цепи — по имени

В формате 10 у площадки, дорожки, переходного и зоны цепь указана **именем**:

```
(pad "1" smd roundrect
    (at -0.51 0)
    (size 0.54 0.64)
    (layers "F.Cu" "F.Mask" "F.Paste")
    (roundrect_rratio 0.25)
    (net "SOIL_ADC")
    (pintype "passive")
    (uuid "…"))
```

Таблицы `(net 0 "") (net 1 "GND") …` в начале файла **нет**. У KiCad 9
(`version 20241229`) цепи — номерами `(net 1)` плюс таблица в начале.
Читатель понимает оба варианта; писатель пишет только формат 10.

Имена иерархических цепей — с путём листа: `/Датчики/SOIL_AOUT`. Безымянные —
`Net-(C413-Pad1)`: такое имя меняется при переаннотации, поэтому в описании
платы на цепь ссылаются по глобальному имени или по паре «деталь — вывод».

## Структура файла

```
(kicad_pcb
  (version 20260206) (generator "…") (generator_version "10.0")
  (general (thickness 1.6) …)
  (paper "A4")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) … (25 "Edge.Cuts" user) …)
  (setup (pad_to_mask_clearance 0) … (pcbplotparams …))
  (property …)                      ; переменные проекта, если есть
  (footprint …) …                   ; детали
  (gr_line …) (gr_rect …) (gr_arc …) ; графика, контур на Edge.Cuts
  (segment …) (arc …) (via …) …     ; медь
  (zone …) …                        ; полигоны и запретные зоны
  (group …) …
)
```

Номера слоёв в формате 10: F.Cu — 0, B.Cu — 2, внутренние — чётные дальше;
пользовательские слои — нечётные. Слои пишутся именем в кавычках везде, кроме
таблицы `layers`.

### Посадочное место

```
(footprint "Resistor_SMD:R_0402_1005Metric"
  (layer "F.Cu")
  (uuid "…")
  (at 163.375 137.035 90)           ; угол необязателен
  (descr "…") (tags "…")
  (property "Reference" "R310" (at 0 -1.17 0) (layer "F.SilkS") (uuid "…") (effects …))
  (property "Value" "10k" …)
  (path "/<uuid листа>/<uuid символа>")   ; связь со схемой
  (sheetname "…") (sheetfile "…")
  (attr smd)                        ; smd | through_hole, dnp, exclude_from_bom
  (fp_line …) (fp_rect …) …         ; графика на F.SilkS, F.CrtYd, F.Fab
  (pad …) …
  (embedded_fonts no)
  (model "${KICAD10_3DMODEL_DIR}/….step" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0))))
```

- Посадочное место **встраивается целиком** из библиотеки: графика, площадки,
  3D-модель. Ссылки на библиотеку при открытии KiCad не разрешает.
- Координаты площадок внутри — относительно `at` детали **без** поворота;
  поворот детали применяется ко всему. Угол площадки в `(at x y a)` — полный,
  уже включающий поворот детали: у детали с `(at … 90)` площадка `(at 0 0 90)`.
- Деталь на нижней стороне: `(layer "B.Cu")`, слои площадок и графики
  зеркальные (`B.Cu`, `B.Mask`, `B.SilkS`), локальные координаты записаны уже
  зеркальными. **Какую ось зеркалит KiCad 10 при записи — не проверено**:
  на этапе 0 перевернуть деталь в KiCad, сохранить и сравнить с библиотекой,
  результат вписать сюда. Нижняя сторона по умолчанию не используется.
- `path` связывает деталь с символом схемы; без него `--schematic-parity`
  считает деталь лишней.

### Медь

```
(segment (start 110.49 124.155) (end 110.49 119.38) (width 0.8)
         (layer "F.Cu") (net "VCC") (uuid "…"))
(arc (start …) (mid …) (end …) (width 0.25) (layer "F.Cu") (net "…") (uuid "…"))
(via (at 189.865 110.49) (size 1.6) (drill 0.6) (layers "F.Cu" "B.Cu")
     (capping no) (covering (front no) (back no)) (plugging (front no) (back no))
     (filling no) (net "/CLOCK-RB6") (uuid "…"))
```

### Зона

```
(zone
  (net "GND")
  (layer "B.Cu")
  (uuid "…")
  (hatch edge 0.508)
  (connect_pads (clearance 0.508))   ; yes | no | thru_hole_only перед clearance
  (min_thickness 0.3)
  (fill yes (thermal_gap 0.508) (thermal_bridge_width 0.635) (island_removal_mode 0))
  (polygon (pts (xy …) …))           ; контур, заданный человеком или скриптом
  (filled_polygon (layer "B.Cu") (pts (xy …) …))   ; результат заливки KiCad
  …)
```

Зона запрета — `zone` без `net` с блоком
`(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))`.

`filled_polygon` писатель **не** считает: он пишет `polygon`, а заливку делает
KiCad (см. команды ниже). Линтер читает `filled_polygon` из файла, сохранённого
после заливки, — это геометрия, которую увидит завод.

### Контур

Замкнутый контур из `gr_line`, `gr_arc` или `gr_rect` на слое `Edge.Cuts`
с `(stroke (width 0.1) (type solid))`. Концы отрезков совпадают точно: зазор в
0,001 мм — незамкнутый контур и отказ заливки.

## Проект: классы цепей и правила

`.kicad_pro` — JSON. Правила платы и классы цепей лежат здесь, не в `.kicad_pcb`:

| Путь в JSON | Что |
|---|---|
| `board.design_settings.rules` | `min_track_width`, `min_clearance`, `min_via_diameter`, `min_via_annular_width`, `min_through_hole_diameter`, `min_hole_to_hole`, `min_copper_edge_clearance`, `min_silk_clearance`, `min_text_height` … |
| `net_settings.classes[]` | `name`, `clearance`, `track_width`, `via_diameter`, `via_drill`, `diff_pair_width`, `diff_pair_gap`, `priority` |
| `net_settings.netclass_patterns[]` | `{"netclass": "POWER", "pattern": "GND"}` — привязка цепей по шаблону имени |
| `net_settings.netclass_assignments` | привязка отдельных цепей к классу, сделанная в интерфейсе |

Писатель меняет только эти ключи и сохраняет остальной JSON как есть: в нём
живут настройки, которые человек менял в интерфейсе.

## Свои правила DRC: `.kicad_dru`

Файл рядом с проектом, то же имя. Пример синтаксиса из демо KiCad:

```
(version 1)

(rule "rf_50ohm_outer"
    (layer outer)
    (constraint track_width (min 1.0mm) (opt 1.1mm) (max 1.2mm))
    (condition "A.NetClass == 'RF'"))

(rule "no_tracks_under_xtal"
    (layer "B.Cu")
    (constraint disallow track via)
    (condition "A.intersectsArea('XTAL_KEEPOUT')"))

(rule "mains_clearance"
    (constraint clearance (min 2.5mm))
    (condition "A.NetClass == 'HV' && B.NetClass != 'HV'"))
```

`intersectsArea('имя')` ссылается на зону с этим именем (`(name "…")` в `zone`).
DRC ловит то, что выражается в этих правилах; всё, что требует геометрии
обратного тока, площади петли или порядка деталей по цепи, — ЭМС-линтер.

## Детерминированные uuid

uuid каждого объекта — `uuid5` от пространства имён проекта и смысловой
идентичности объекта: для детали — её ссылка, для дорожки — цепь, слой и
координаты концов, для зоны — цепь, слой и имя. Случайный uuid превращает
каждую пересборку в diff на весь файл. Приём тот же, что в скилле схем
(`schlib.uid`); здесь — `pcblib.det_uuid`.

## Команды kicad-cli

Сверены с `--help` KiCad 10.0.4.

```bash
# нетлист из схемы — вход сборки платы
kicad-cli sch export netlist --format kicadsexpr -o build/net.net проект.kicad_sch

# заливка зон и сохранение платы, затем DRC с отчётом в JSON
kicad-cli pcb drc --refill-zones --save-board --format json --severity-all \
          --exit-code-violations -o build/drc.json плата.kicad_pcb

# сверка платы со схемой
kicad-cli pcb drc --schematic-parity --format json -o build/parity.json плата.kicad_pcb

# картинки слоёв для просмотра
kicad-cli pcb export pdf --mode-separate --layers F.Cu,B.Cu,F.SilkS,F.Fab,Edge.Cuts \
          --check-zones -o build/layers/ плата.kicad_pcb
kicad-cli pcb export svg --mode-single --layers B.Cu,Edge.Cuts --fit-page-to-board \
          --exclude-drawing-sheet -o build/B_Cu.svg плата.kicad_pcb

# 3D
kicad-cli pcb render --side top -o build/3d_top.png плата.kicad_pcb
kicad-cli pcb export step --no-dnp -o build/board.step плата.kicad_pcb

# статистика: площадь, число деталей и переходных
kicad-cli pcb export stats --format json -o build/stats.json плата.kicad_pcb
```

PDF переводится в PNG через `pdftoppm -r 300 -png`, вырезки зон — через PIL,
как в `skills/kicad-schematic/scripts/check.py`.

## Ловушки

1. **`--save-board` перезаписывает плату.** Заливка идёт по собранному файлу в
   `build/` или сразу после сборки; ручные правки в GUI между сборкой и
   заливкой пропадут, и это ожидаемо: плата — результат сборки.
2. **Незаполненная зона** в только что собранном файле: DRC без
   `--refill-zones` сообщит о неразведённых земляных связях, которых после
   заливки не будет.
3. **Площадки с цепью `""`** — не подключено; у детали, чей символ не
   попал в нетлист, все площадки такие.
4. **Посадочное место из `smartpole.pretty`** ищется через `fp-lib-table`
   проекта, стандартные — в `/usr/share/kicad/footprints`. Имя библиотеки в
   `footprint "lib:name"` должно совпадать с таблицей, иначе обновление из
   схемы в GUI потеряет деталь.
5. **Несуществующая 3D-модель** не ломает файл, но рендер и проверка высот
   корпуса молча её пропускают — линтер ENC предупреждает о детали без модели.
6. **Устаревшая плата проекта**: если `.kicad_pcb` собран по старой схеме,
   сверка со схемой покажет лишние и недостающие детали. Плата не
   «обновляется» — собирается заново из нетлиста.
7. **Число без единиц в `.kicad_dru` — нанометры.** `(min 6.0)` — это 6 нм:
   правило загружается без ошибки и молча ничего не проверяет. Всегда писать
   `(min 6.0mm)`. После правки правил — проверка, что правило живое: в копии
   проекта ужесточить его выше фактического зазора и убедиться, что DRC
   выдаёт нарушения по этому правилу.
8. **`creepage` не видит исключений по области.** Условие
   `!(A.intersectsArea('X') && B.intersectsArea('X'))` KiCad 10 учитывает для
   `clearance`, но проверка пути утечки его игнорирует. Для правил с
   исключением (например, поле выводов TO-220 с шагом 2,54) — только
   `clearance`: на плате без прорезей он совпадает с путём утечки.
