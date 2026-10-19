# Модели для клиента: соглашения и manifest.json

Файлы: `godot/client/assets/models/*.glb` и `godot/client/assets/models/manifest.json`.
Пишет конвейер `assets3d/`, читает клиент (`client/`).

## Система координат

- 1 единица = 1 клетка карты. Y вверх.
- Клетка движка `(x, y)` → Godot `(x + 0.5, 0, y + 0.5)`: ось `y` карты (вниз) = +Z в Godot.
- Камера смотрит с юга (+Z) на север (−Z), как в игре. **Лицевая сторона модели (вход, ворота) — +Z.**
- Начало координат модели — центр низа прямоугольника её клеток (block ∪ visit маски объекта).
- Размер модели: по X и Z укладывается в свой прямоугольник клеток; высота свободная (город ~3–4 клетки,
  дерево ~1–1.5, ресурс ~0.4).

## manifest.json

```jsonc
{
  "version": 1,
  "models": {
    "def:avtgold0":   {"glb": "res://assets/models/res_gold.glb", "footprint": [1, 1]},
    "obj:79:6":       {"glb": "res://assets/models/res_gold.glb", "footprint": [1, 1]},
    "obj:98:5":       {"glb": "res://assets/models/town_necropolis.glb", "footprint": [5, 3],
                       "scale": 1.0, "offset": [0, 0, 0], "rot_y": 0},
    "hero:class:0":   {"glb": "res://assets/models/hero_knight.glb", "footprint": [1, 1]},
    "creature:33":    {"glb": "res://assets/models/cr_033_zombie.glb", "footprint": [1, 1]}
  },
  "variants": {                      // необязательно: несколько моделей на ключ, клиент берёт по id объекта
    "obj:155:0": ["res://assets/models/pine_a.glb", "res://assets/models/pine_b.glb"]
  }
}
```

Поиск модели клиентом: `def:<имя def без расширения, lowercase>` → `obj:<type>:<subtype>` → `obj:<type>` →
процедурная заглушка по категории. Монстры на карте (класс 54): `creature:<id>`. Герои: `hero:<id>` →
`hero:class:<класс>` → общий `hero`.

`footprint` — справочно (клиент берёт клетки из масок объекта); `scale`, `offset`, `rot_y` — поправки, по
умолчанию 1, 0, 0.

## Требования к GLB

- Текстуры JPEG/PNG (не WebP), до 2048 px; до ~30k треугольников на модель (деревья ~3k — их много).
- Материалы с именем, начинающимся на `PlayerColor`, клиент перекрашивает в цвет владельца (флаги, знамёна).
