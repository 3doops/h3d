# 3D-ассеты карты для Godot-клиента

Модели всех объектов карты SoD + HotA, бродячих монстров, героев и городов + текстуры почв. Модели делаются
**по текстовым описаниям** (составлены по оригинальным спрайтам), спрайты игры в генерацию не подаются.
Формат и система координат — `MANIFEST.md`, стиль и формат эскиза — `STYLE.md`.

## Конвейер

```
pipeline/models/*.tsv (описания, руками) + pipeline/sources/*.tsv (выгрузка из игры)
  → build_catalog.py → catalog.tsv (модель, категория, DEF с клетками, ключи манифеста, приоритет)
  → Codex image_gen: prompts/concept.txt + categories.json, стиль style_refs/*.jpg   → work/<key>/concept_N.png
  → check_concept.py: ровный фон, один объект, не обрезан (иначе новый эскиз, до 3 раз)
  → TRELLIS.2 (trellis.py; 512, decimation 20000/деревья 10000, текстура 1024)        → work/<key>/raw_sN.glb
  → Blender fit_asset.py: фасад по эскизу (orient), выравнивание по осям, удаление висящих кусков,
    масштаб под клетки (origin = центр низа, фасад +Z), децимация, base color (+PBR у золота/камней),
    JPEG, флаг PlayerColor у владеемых, 5 рендеров                                    → work/<key>/fit_WxH/
  → автопроверка модели (треугольники, не плоская) → godot/client/assets/models/<key>.glb (+ <key>__WxH.glb)
  → godot/client/assets/models/manifest.json (def:/obj:/creature:/hero: — все ключи модели)
леса (kind=clump): assemble_clump.py ставит 2–3 готовых дерева на каждую занятую клетку маски DEF
                                                                                   → <key>__<def>.glb
```

Всё делает `pipeline/runner.py`: 3 параллельных Codex, 1 TRELLIS (один GPU), 1 Blender; порядок по
`priority`: **0 — объекты тестовой карты «The Newcomers»** (Некрополис первым; берутся из
`godot/bridge/testdata/objects_newcomers.tsv` + `snapshot_newcomers.json`, иначе из скана .h3m),
1 — партия 1, 2 — частые SoD, 3 — HotA и редкие, 4 — артефакты и мелочь. Каталог пересобирается сам, когда
меняются описания, выгрузка или данные тестовой карты. Листы `sheets/concepts_NN.jpg`, `sheets/models_NN.jpg`
(вид камеры игры + спереди) обновляются каждые ~5 мин.

## Запуск, остановка, докачка

```powershell
powershell -File godot\assets3d\pipeline\start_runner.ps1 --lookahead 40   # фоном (supervise.ps1 + runner.py)
powershell -File godot\assets3d\pipeline\stop_runner.ps1                    # стоп (и сторож не перезапустит)
Get-Content godot\assets3d\work\runner.log -Tail 30 -Wait                   # журнал
uv run --no-project --python 3.12 python -I godot\assets3d\pipeline\status.py   # счётчики по приоритетам
```

- **Докачка:** состояние каждой модели — `work/<key>/state.json` (`new → concept_ok → raw → fitted →
  published`); прерванное просто делается заново. Перезапуск — тот же `start_runner.ps1` (повторный вызов
  ничего не делает, если всё живо).
- **Живучесть:** `supervise.ps1` перезапускает `runner.py` через минуту после любого падения; задача
  планировщика `HotA3D-AssetRunner` (каждые 15 мин, `watchdog.ps1`) поднимает всё после перезагрузки/входа,
  если не было `stop_runner.ps1` (файл `work/runner.stop`). Удалить задачу:
  `schtasks /Delete /TN HotA3D-AssetRunner /F`.
- **Сбои GPU-сервера** (OOM, недоступен) — не брак: TRELLIS-поток ждёт 5 мин, модель откладывается.
- **Лимит Codex** («You've hit your usage limit… try again at 4:47 AM») — все Codex-потоки ждут до
  названного времени, попытки не сгорают; TRELLIS и Blender доделывают уже нарисованное.

## Диск и города повышенной детализации

- Диск C: мал: после каждой публикации `cleanup.compact` оставляет только нужное для повтора (текущий эскиз в
  JPEG, текущий сырой GLB, `state.json`, логи Codex, `stats.json` и два вида для листов), копию картинки в
  `~/.codex/generated_images` удаляет сразу. Разовая чистка: `uv run --no-project --python 3.12 --with pillow
  python -I pipeline/cleanup.py --all --codex-cache`. При < 2 ГБ свободного на C: все стадии ждут
  (`DISK GUARD` в журнале; порог — `A3D_MIN_FREE_GB`).
- Города (`fit`: `upgrade=1024;upgrade_faces=60000`): после обычной публикации runner раз в час
  (`A3D_UPGRADE_EVERY`), когда GPU свободен, пробует TRELLIS 1024; удалось — модель заменяется, OOM —
  остаётся опубликованная 512 (её сырой GLB тоже хранится). Некрополис — модель по скетчу пользователя
  (`necrocastle/ai3d/result/necropolis_ai.glb`, TRELLIS 1024), подогнанная тем же `fit_asset.py`.
- `pipeline/review_sheet.py "<с какого времени>"` — листы `sheets/review_NN.jpg` только из ещё не просмотренных.

## Просмотр и отбраковка

Листы `sheets/models_*.jpg` (какой ключ где — `sheets/index.tsv`). Брак — строка в `review.tsv`
(`key<TAB>verdict<TAB>note`), runner подхватывает за минуту:

| verdict | что сделает runner |
|---|---|
| `reject_concept` | новый эскиз, дальше заново TRELLIS и подгонка |
| `reject_model` | тот же эскиз, TRELLIS с новым seed |
| `turn=90` / `turn=-90` / `turn=180` | фасад не туда: довернуть (+ = против часовой сверху) и подогнать заново |
| `refit` | только Blender (после правки `fit` в каталоге: `max_h=1.8;pbr=1;gain=1.2;res=1024`) |
| `ok` | отметка «просмотрено» |

Правка описания в `pipeline/models/*.tsv` сама запускает новый эскиз этой модели.

## Каталог

`pipeline/models/*.tsv`: `10_batch1` (партия 1), `15_heroes`, `20_creatures`, `30_objects`, `40_decor`;
колонки `model_key, category, kind, priority, name, match, footprint, flag, fit, members, description`;
`match` — правила `def:`, `re:`, `obj:C[:S]`, `creature:ID`, `heroclass:ID`, `hero`, `town:FID`
(подробно — `build_catalog.py`). `pipeline/sources/` — выгрузка `extract_sources.py`: `defs.tsv` (2218 шаблонов
DEF SoD + HotA с классом/подтипом и масками), `creatures.tsv`, `heroclasses.tsv`, `factions.tsv`.
`kind`: `gen` — генерируется, `clump` — лес из готовых деревьев, `skip` — невидимые/случайные объекты,
`decal` — плоские наложения почвы (это текстуры, не модели).

## Текстуры почв

`pipeline/terrain.py` → `godot/client/assets/terrain/<почва>_albedo.jpg` — 12 почв (dirt, sand, grass, snow,
swamp, rough, subterranean, lava, water, rock, highlands, wasteland), 1024 px, бесшовные (картинка Codex
сводится с копией со сдвигом на полкадра по шумовой маске). Проверка — `sheets/terrain_2x2.jpg`;
`--only snow --force` — перегенерировать одну.

## Счётчики (2026-10-18 03:45)

- Каталог: **956 моделей** (892 gen, 33 clump, 9 skip, 22 decal), покрыты все **2186** DEF карты, все
  существа с картинкой на карте (190) и все 24 класса героев, 12 городов.
- Готово: **~270 моделей** + 171 лес-сборка = 440 GLB, 270 МБ, 1030+ ключей в `manifest.json`; тестовая карта —
  ~95% (приоритет 0), партия 1 — 40 из 45.
- В очереди: ~650 моделей (приоритеты 2–4). Темп ≈ 1 модель/мин, пока хватает лимита Codex.

## Известные ограничения

- TRELLIS.2 на 1024 упирается в нехватку памяти GPU (общий RTX 3090) — всё идёт на 512: формы мягче, мелочь
  (монеты, зубцы) сглажена. Когда GPU свободен, важные модели можно переделать с `res=1024` в `fit` + `reject_model`.
- Фасад определяется автоматически в ~85–90% случаев, остальное — `turn=` по листу.
- Огонь (костёр) и тонкие ветки TRELLIS восстанавливает плохо.
