# ENGINE_API: SoD 3.2 engine + HotA + HD, reference for the bridge agent

Plan item 1.2 (static research, 2026-10-17); checked on the running game in 1.3–1.8 (2026-10-18, h3hota.exe +
HotA.dll 1.8.0, no HD): items tagged **[R]**, results in section 13. All addresses are VAs in
`h3hota.exe` / `h3hota HD.exe`, which are the same SoD 3.2 image (see `analysis/NOTES.md`). Sources:
`analysis/export/h3hota.exe/decomp.c` (Ghidra C with NH3API, H3API and Dreamcast names), `analysis/nh3api_types.json`,
`_ref/community/nh3api`, `_ref/H3API-master`, `analysis/runtime/hooks.tsv` (HotA/HD patch map),
`analysis/runtime/mem` (memory snapshot of HotA 1.7.x + HD taken at the main menu), and `vcmi-src` plus the hota VCMI mod
(h3m ids).

## 0. Conventions

**How sure we are**

| Tag | Meaning |
|---|---|
| **[V]** | Checked in the decompiled or disassembled SoD code. Where NH3API/H3API cover the item, they agree. |
| **[S]** | Comes from one source (NH3API, H3API or a Dreamcast match). It fits how the decompiled code uses it, but was not traced in full. |
| **[?]** | Inferred or guessed. Check it at runtime before relying on it. |
| **[R]** | Checked at runtime by the bridge (godot/bridge, test_client.py). |

**Calling conventions (Frida)**

- MSVC6 `thiscall`: `this` is passed in `ecx` and the callee pops the stack arguments. In Frida, declare it as
  `new NativeFunction(p, ret, ['pointer', ...], 'thiscall')`.
- `fastcall`: the first two arguments go in `ecx` and `edx`, the rest on the stack, and the callee pops.
- With `Interceptor.attach` on x86, `args[0]` is the first **stack** argument. For thiscall and fastcall, read
  `this.context.ecx` and `this.context.edx` yourself.
- The "ret" values given below were checked against `ret N` in the disassembly.

**Packed point (`type_point`, u32)**

- Decode: `x = v & 0x3FF`, `y = (v >> 16) & 0x3FF`, `z = (v >> 26) & 0xF`.
- Encode: `x | (y << 16) | (z << 26)`.
- The engine treats `x = 0x3FF` as "invalid". [V]

**Strings and vectors**

- `exe_string` (0x10 bytes): `+4 char*`, `+8 length`, `+0xC capacity`.
- `exe_vector<T>` (0x10 bytes): `+4 first`, `+8 last`, `+0xC end`. Element count = `(last - first) / sizeof(T)`.
- Text is cp1251. [V]

**Directions** (from `normalDirTable` @0x678150, `tilePoint[8]`, stride 4) [V, read from the snapshot]:
`0 N(0,-1) 1 NE(1,-1) 2 E(1,0) 3 SE(1,1) 4 S(0,1) 5 SW(-1,1) 6 W(-1,0) 7 NW(-1,-1)`.

**Hooks already placed by HotA/HD**

Most entry points below already carry a HotA/HD HiHook, which shows up as `jmp bridge` in the first bytes.
`Interceptor.attach` and `Interceptor.replace` relocate that `jmp`, so the existing hook chain keeps working as long as
you call through. `Interceptor.replace` *without* calling the original skips every HotA/HD hook on that function. Each
section lists the hooks found at the place (from `hooks.tsv`).

**HotA relocations: read offsets from code, never hard-code them**

HotA keeps the SoD struct layouts but moves some arrays out of `game`. The bridge should read these displacements from
the patched instructions at startup. The table shows the values found in the snapshot.

| What | Read u32 at | SoD value | HotA (snapshot) |
|---|---|---|---|
| hero array offset from `*gpGame` | `0x4317EE` (in `game::GetHero`), or `0x402480` | 0x21620 | **recomputed at every launch** (0x6AFFB8, 0x70FFB8 … seen; the snapshot had 0x6140000): always read it [R] |
| hero count | `0x4BF620` (also `0x4BB1EE`) | 156 | 215 [R] |
| heroOwner[] offset (not needed) | `0x412F1C` | 0x4DF18 | 0x79BDC |

Instead of reading the offset, you can call `game::GetHero` @0x4317D0, `thiscall(gpGame, id) -> hero*`. HotA patches it,
so it returns the relocated hero. [V] [R: GetHero(0) == `*gpGame + [0x402480]`, both sites hold the same value]

`HotA.dll` loads at a relocated base (0x6FDC0000 in the snapshot). Any HotA address is
`static - 0x10000000 + Module.findBaseAddress('hota.dll')`.

**Globals** (all [V] in the decompiled code unless marked)

| Address | Name | Type and meaning |
|---|---|---|
| 0x699538 | gpGame | `game*` (0x4E7D0 in SoD; HotA allocates 0x7A06D) |
| 0x6992B8 | gpAdvManager | `advManager*` |
| 0x699420 | gpCombatManager | `combatManager*` |
| 0x699530 | gpInputManager | `inputManager*` |
| 0x6992D0 | gpWindowManager | `heroWindowManager*` |
| 0x699550 | gpExec | `executive*` |
| 0x6992D4 | gpSearchArray | `searchArray*` (pathfinder) |
| 0x69CCF4 | giCurPlayer | int: whose turn it is |
| 0x69CCFC | gpCurPlayer | `playerData*` of giCurPlayer |
| 0x6977DC | giCurWatchPlayer | int: the player whose view is on screen (the local human). See note below. |
| 0x6995A4 | giThisGamePos | int: local player slot (network); 0 in single player [S] |
| 0x6783C8 / 0x6783CC | MAP_WIDTH / MAP_HEIGHT | int; both equal the map size |
| 0x698A48 | mapExtra | `u16*`: fog bits per tile (section 6) |
| 0x6987A8 | gConfig | `configStruct` (options, section 10) |
| 0x698A10 | bShowIt | bool32: animate adventure-map movement |
| 0x697308 | gbGameOver | bool32 |
| 0x699590 | gbInCombat | bool |
| 0x6977D4 | giDialogTimeout | u32 `timeGetTime` deadline for the current modal; 0 = no deadline (section 8) |
| 0x697728 | gGameCommand | int: main-menu loop command (section 11) |
| 0x6AAD88 | DoDialog nesting depth | int, greater than 0 while any `DoDialog` modal runs [V] |
| 0x699570 / 0x6995E0 / 0x699424 | current NormalDialog style / its window / its selected icon id | [V] |

Note on giCurWatchPlayer: HotA redirects 18 UI reads of `gpCurPlayer` (in ShowRoute, ProcessDeSelect, QuickInfo) to its
own pointer `&game->player[giCurWatchPlayer]`, set in `hook_004bd8c4`. Issue commands only when
`giCurPlayer == giCurWatchPlayer` and that player is a local human.

## 1. Map

**Where it lives.** The map is `game.worldMap` (`NewfullMap`, 0xF5C bytes) at **gpGame+0x1FB70**.
`advManager+0x5C` points to it as well. [V]

| Field | Offset from gpGame | Notes |
|---|---|---|
| ObjectTypes `exe_vector<CObjectType>` | +0x1FB70 (first +0x1FB74, last +0x1FB78) | section 2 |
| Objects `exe_vector<CObject>` | +0x1FB80 (first +0x1FB84, last +0x1FB88) | section 2 |
| cellData `NewmapCell*` | **+0x1FC40** | [V] |
| Size int32 | **+0x1FC44** | width = height; HotA allows up to 252 |
| HasTwoLevels bool | **+0x1FC48** | levels = `HasTwoLevels ? 2 : 1` |

**Tile index.** `cell = cellData + ((z*Size + y)*Size + x) * 0x26`. This is
`NewfullMap::cell` @0x4086D0, `thiscall(map, x, y, z)`. [V]

**Tile struct (`NewmapCell`, 0x26 bytes).** No HotA patches to its size or offsets were found.

| Off | Size | Field | Meaning |
|---|---|---|---|
| +0x00 | u32 | setup (ExtraInfoUnion) | per-object data of the object whose trigger is on this tile (town id, mine id, monster bits…); -1 after erase [V] |
| +0x04 | u8 | GroundSet | terrain id |
| +0x05 | u8 | GroundIndex | terrain sprite frame |
| +0x06 | u8 | RiverSet | 0 = none, 1 clear, 2 icy, 3 muddy, 4 lava. Use `& 7`: VCMI notes high bits in HotA maps [?] |
| +0x07 | u8 | RiverIndex | river frame |
| +0x08 | u8 | RoadSet | 0 = none, 1 dirt, 2 gravel, 3 cobblestone |
| +0x09 | u8 | RoadIndex | road frame |
| +0x0C | u8 | mirror | bit0 terrain flip X, bit1 terrain flip Y, bit2/3 river X/Y, bit4/5 road X/Y [V, 3 sources agree]. Bit6 is "canDig" (H3API) or "passable" (NH3API); bit7 is "animated" [?] |
| +0x0D | u8 | access | **bit0 blocked** [V H3API IsBlocked] [R: equals the union of the objects' block masks, tile for tile], **bit1 coast / water edge** [S], bit3 can build boat [S], **bit4 trigger/visitable entrance** [V H3API IsEntrance] [R; visitable tiles also have bit0], bit6 can dig (H3API) [?] |
| +0x0E | `exe_vector<TObjectCell>` | ObjectCellList | every object covering this tile. Entry (4 bytes): `u16 objIndex` (HotA treats it as unsigned), `u8 cellX:4 / cellY:4`, `i8 drawHeight` [V EraseObj] |
| +0x1E | int32 | type | object type of the top object (34 = hero standing here) [V] |
| +0x22 | i16 | subtype | subtype of the top object |
| +0x24 | u16 | objIndex | index of the top object in `Objects`, or -1/0xFFFF [V] |

**Terrain ids** [V for SoD; HotA from h3m ids, see below]:
`0 dirt, 1 sand, 2 grass, 3 snow, 4 swamp, 5 rough, 6 subterranean, 7 lava, 8 water, 9 rock, 10 highlands (HotA),
11 wasteland (HotA)`.

- HotA patches the terrain sprite count 10→12 in the `advManager` ctor and relocates `giTerrainToMusicTrack`
  (0x678330 → HotA memory).
- HotA does not hook the tile loader `loadMapLayer`@0x4FEAE0. So the bytes in memory are the h3m bytes, and the VCMI
  hota mod maps h3m ids `highlands=10, wasteland=11` (`vcmi/userdata/Mods/hota/mods/mapSupport/mod.json`). [S/?]
  Check this on a map that contains those terrains.

Rivers 0–4 and roads 0–3 are unchanged in HotA. PROTOCOL.md's numbering matches the table.

## 2. Map objects

**`CObject`** (0xC bytes, `Objects[i]`; `i` is the object id) [V EraseObj]:

| Off | Field |
|---|---|
| +0x00 | u32 setup: the initial ExtraInfoUnion. The live value is on the trigger cell, cell+0x00 |
| +0x04 / +0x05 / +0x06 | u8 x, y, z of the **anchor**, the bottom-right tile of the sprite box |
| +0x08 | u16 TypeID: index into `ObjectTypes` |
| +0x0A | u8 frameOffset: random animation phase |

**`CObjectType`** (0x44 bytes, `ObjectTypes[TypeID]`) [V field use; S names]:

| Off | Field |
|---|---|
| +0x00 | exe_string ImageName: the def name, for example `AVTgold0.def` (`char*` at +0x04) |
| +0x10 / +0x11 | i8 Width, Height in tiles, from the `.msk` file |
| +0x14 | PlacementMask (`.msk` draw mask, 8 bytes) |
| +0x1C | **PassableMask** (8 bytes, 48 bits used): 1 = passable, 0 = blocked |
| +0x24 | ShadowMask |
| +0x2C | **TriggerMask** (visit mask): 1 = visitable |
| +0x34 | u32 TerrainMask |
| +0x38 | int32 **Type** (object class) |
| +0x3C | int32 **Subtype** |
| +0x40 | bool IsUnderlay: drawn as ground |

**Mask bit order** [V: `readObjectType`@0x503DD0 copies the 6 h3m bytes bit for bit; VCMI `ObjectTemplate::readMap`]:

- Treat bytes 0..5 as a little-endian 48-bit value. Bit `b = 8*row + col`.
- `row` 0 is the top of the 8×6 box and row 5 is the anchor row. `col` 0 is the left edge and col 7 is the anchor
  column.
- So bit `b` is tile `(ax - 7 + (b & 7), ay - 5 + (b >> 3))`, and **bit 47 is the anchor**. [R: town gate = bit 45
  = (ax-2, ay) = the town record's x,y; mine entrance bit 46 = (ax-1, ay); same strings as OBJECTS.TXT]
- This matches PROTOCOL.md ("бит = 8*row+col от левого верхнего"). Send the 6 bytes as stored.
- H3API's comment ("bit 0 = bottom-right") is **wrong** for these fields.

**Which objects are on the map.** `EraseObj`@0x4AA820 removes the index from every covered tile's `ObjectCellList` and
sets cell setup = -1. It **does not shrink `Objects`**. So the live set is the union of `objIndex` over all
`ObjectCellList`s. [V]

- Removal events: hook `advManager::EraseObj` @0x4AA820 `thiscall(adv, NewmapCell* cell, u32 point, bool record)`.
  The object id is `u16 [cell+0x24]` at entry. Hooks: 1 HotA HiHook.
- Placement: `NewfullMap::PlaceObject` @0x506170 `thiscall(map, objIndex, setExtra)` [S], and
  `game::InsertObject` @0x4C9550 [S].

**Heroes and boats are not normal objects.** They are drawn from `heroPool` and `boatPool`. The tile under a hero has
type 34 and setup = hero id, while the object that was there is saved in hero+0x0C/+0x14. [R] No object of type 34 is on
any tile's `ObjectCellList` in a loaded game (1233 live objects of the 1258 in the .h3m). A picked-up resource
disappears from the lists at once (bridge diff `objects_removed`). [R]

**Trigger tile.** `CObject::FindTrigger` @0x4FF280 `thiscall(obj, int* x, int* y)` [S] [R: gives the visitable tile; its cell+0x00 holds the live setup], or
`NewmapCell::get_trigger_cell` @0x4FD0F0 `thiscall(cell) -> cell*` [S].

**Owner.** Call `GetFlaggedObjectOwner` @0x40FC80 `fastcall(NewmapCell* triggerCell) -> int` (-1 = none). [V] [R: town
and mine owners match the players' town lists]
It handles:

| Types | Pool used |
|---|---|
| 17, 20 dwellings | `generatorPool[setup]` owner at +0x57 |
| 33 garrison | `garrisonPool[setup]` +0 |
| 42 lighthouse, 53 mine | `minePool[setup]` +0 |
| 77, 98 town | `townPool[setup]` +1 |
| 87 shipyard | low byte of setup |
| 34 hero | redirects to the object under the hero |

`hasFlag(type)` @0x40FC00 `fastcall`: true for 17, 20, 33, 42, 53, 77, 87, 98. [V]

Pools (offsets from gpGame; HotA does not relocate them):

| Pool | Offset | Element |
|---|---|---|
| minePool | +0x4E388 | 0x40; +0 owner, +1 type (0–6 resource, 7 abandoned), +0x3C x/y/z |
| generatorPool | +0x4E398 | 0x5C; +0x54 x/y/z, +0x57 owner |
| garrisonPool | +0x4E3A8 | 0x40; +0 owner |
| boatPool | +0x4E3B8 | 0x28; +0 x/+2 y/+4 z i16, +6 valid, +0x1C owner, +0x20 hero |

Owners of HotA-only flaggable objects (warehouses 142, …) are not handled by this function. [?]

**Object classes.** SoD 0–231 is `TAdventureObjectType` in NH3API `core/terrain.hpp`; it matches VCMI. HotA adds
(VCMI hota `mapSupport`):

| Class | Contents |
|---|---|
| 139, 140 | decorations |
| 141 | magic terrains |
| 142 | warehouses 0–6 |
| 144 | visitables 0–12 (temple of loyalty … trapper lodge) |
| 145 | pickups 0–3 (ancient lamp, sea barrel, jetsam, vial of mana) |
| 146 | visitables 0–4 (seafaring academy, observatory, altar of mana, town gate, ancient altar) |
| 212 | border/quest gate 1000, grave 1001 |

HotA also adds new subtypes of 8 (boats, airship 100), 16 (banks 21–32), 17 (dwellings 91–117), 45 (portals 8–24),
83 (seer huts 3–7) and 88–90.

**Common setup bits** (H3API) [S]:

- monster (54): SoD `count = setup & 0xFFF`. **HotA: setup is an index into HotA's monster table**; count =
  `int [ *( *(gpGame + [HotA.dll+0x634AC8]) + 0x1C ) + setup*0x24 ]` (HotA hook_004170DA in MonsterQuickView). [R]
- resource (79): `amount = setup & 0x7FFFF` [R for wood: setup 10 → +10 wood; gold piles hold 5..10 — probably
  hundreds, not checked].
- town, mine, dwelling: setup = pool index.

## 3. Heroes

**Location.** `hero = *gpGame + heroOff + id*0x492`, with `heroOff` from section 0 (HotA: 0x6140000). The count is
`[0x4BF620]` (HotA: 215). HotA keeps the hero size 0x492; only the base moves. [V]

**`hero` struct** (0x492, NH3API names; H3API agrees on every offset used here) [V unless marked]:

| Off | Type | Field |
|---|---|---|
| +0x00 / +0x02 / +0x04 | i16 | mapX, mapY, mapZ. This is the tile the hero **stands on**: the path starts there and its terrain picks the walking sound. In the h3m file the hero object is stored one tile to the right [S]. |
| +0x06 | bool | valid: the hero is on the map |
| +0x0C / +0x14 | | type and setup of the object hidden under the hero (for example, a town gate tile) |
| +0x18 | i16 | mana |
| +0x1A | int32 | id |
| +0x22 | i8 | **playerOwner**, -1 = none (unhired, prison, dead) |
| +0x23 | char[13] | name (cp1251) |
| +0x30 | int32 | hero class (HotA: 0–23) |
| +0x34 | u8 | portrait |
| +0x35 / +0x39 | int32 | targetX, targetY: route destination, -1 = none |
| +0x3D | i16 | targetZ |
| +0x47 | u8 | facing (direction 0–7) [S] |
| +0x49 | int32 | **maxMobility** (movement points at turn start) |
| +0x4D | int32 | **currMobility** (movement points left) |
| +0x51 | int32 | experience |
| +0x55 | i16 | level |
| +0x91 | int32[7] | army creature types, -1 = empty slot |
| +0xAD | int32[7] | army counts |
| +0xC9 | i8[28] | secondary skill levels (HotA has 30 skills; the extra 2 are stored elsewhere [?]) |
| +0x105 | u32 | flags: **0x40000 in boat** [V DoAdvCommand], **0x20000 in tavern** [V NextPlayer]; the other bits are visited-object bonuses |
| +0x11C | bool | IsSleeping |
| +0x476 | i8[4] | primary skills: attack, defense, power, knowledge [V DoVictory] |

**State of a hero**

| State | How to tell |
|---|---|
| On the map | `valid != 0 && owner >= 0` |
| In a tavern | flag 0x20000, or listed in some `playerData.recruits[2]` |
| In a prison | owner -1, not valid; a prison object (62) has `setup == hero id` |
| In a boat | flag 0x40000 |

HotA caveat: `game.heroExtraPool[156]` at +0xA4 and `heroAllocInfo` are also relocated. The bridge only needs the hero
array.

## 4. Towns

`townPool` is an `exe_vector<town>` at **gpGame+0x21610** (first +0x21614, last +0x21618). The element is 0x168 bytes
and HotA does not relocate it. Town id = vector index. `game::GetTown` @0x42BA30 `thiscall(game, id)`.

| Off | Type | Field |
|---|---|---|
| +0x00 | i8 | id |
| +0x01 | i8 | **owner** (-1 neutral) |
| +0x04 | i8 | faction (0–8 SoD, 9 Cove, 10 Factory, 11 Bulwark) |
| +0x05 / +0x06 / +0x07 | u8 | x, y, z: **the gate tile** (= the town object's trigger tile, anchor x-2) [R] |
| +0x0C | int32 | garrison hero id (upper slot), -1 = none |
| +0x10 | int32 | **visiting hero** id, -1 = none. [R] While the visiting hero is the selected ("mobilized") hero of the player whose turn it is, this is -1; it is set again when the turn passes. |
| +0x14 | i8 | mage guild level |
| +0xC4 | exe_string | **name** (`char*` at +0xC8) |
| +0xE0 / +0xFC | int32[7] / int32[7] | garrison types / counts |
| +0x150 | u64 | buildings shown on the town screen (top level only) [S] |
| **+0x158** | u64 | **built buildings** (lower tiers included; the build menu reads it) [V use] |
| +0x160 | u64 | buildings allowed in this town |

Building bits: 7 fort, 8 citadel, 9 castle, 10–13 village/town/city hall/**capitol**, 26 grail.
`fort = bit9 ? 3 : bit8 ? 2 : bit7 ? 1 : 0` from +0x158. `capitol = bit13` [S]. [R for fort: every town drawn with the
`…f0` (fort) def has bit7, the two `…t0` towns have none]

## 5. Players

`player[8]` (`playerData`, 0x168 bytes) at **gpGame+0x20AD0 + i*0x168**; i is the color (0 red … 7 pink). [V]

| Off | Type | Field |
|---|---|---|
| +0x00 | i8 | color |
| +0x01 | i8 | numHeroes |
| +0x04 | int32 | **currHero**: the selected hero, -1 = none |
| +0x08 | int32[8] | **owned hero ids** (-1 = empty) |
| +0x28 | int32[2] | tavern heroes |
| +0x3E / +0x3F | i8 | numTowns / currTown (selected town index) |
| +0x40 | i8[48] | owned town ids |
| +0x9C | int32[7] | **resources**: wood, mercury, ore, sulfur, crystal, gems, gold |
| +0xCC | char[21] | name |
| +0xE1 | bool | **isLocal** [V] |
| +0xE2 | bool | **isHuman** [V] |
| +0xE4 | u32 | quickCombat (network only) |

Alive: `!gpGame->playerDead[i]`, a bool[8] at **gpGame+0x1F636**. Slots that do not take part in the game look the same,
so also require `numHeroes > 0 || numTowns > 0`, or read the map header [?].

Helpers:

- `game::IsHuman` @0x4CE600 `thiscall(game, i) -> bool`
- `game::IsLocalHuman` @0x4CE630
- `playerData::IsLocalHuman` @0x4BAA40 `thiscall(pd)`
- `playerData::HasMobileHero` @0x4BA850

## 6. Date, turn, fog of war

| Item | Where |
|---|---|
| Day of week (1–7), week (1–4), month (1–) | u16 at gpGame **+0x1F63E, +0x1F640, +0x1F642** [V] |
| Whose turn | `giCurPlayer` @0x69CCF4. "Me" is the player with `isHuman && isLocal`; in hotseat it is `giCurWatchPlayer` |
| Fog | `mapExtra` (`u16*` @0x698A48). Index is `(z*MAP_HEIGHT + y)*MAP_WIDTH + x`; **tile is visible to player p if `mapExtra[i] & (1 << p)`** [V GetMapExtra@0x4F8040, GetCloudLookup] [R]. Bit 8 is used for monsters [S]. |

Revealing tiles: `game::SetVisibility` @0x49CDD0 [S].

## 7. Commands: what the original UI calls

### Where to run commands (the game thread)

> **[R] Runtime finding (important).** Frida/Gum does not run *Frida* hooks for calls made from inside an
> `Interceptor.attach` listener on the same thread (the interceptor guard is held while listeners run). An engine call
> made from the `Main` onEnter listener therefore skipped our NormalDialog/DoDialog/NextPlayer/MoveHero hooks: the
> chest dialog ran modally and the game hung. HotA's own inline hooks are not affected. The bridge therefore **rewrites
> the idle message** in the `Main` onEnter listener into the button click `{0x200, 0xD, itemId}` and lets `Main` itself
> run `ProcessDeSelect` (see "Move" / "End turn" below); `Main`'s onLeave of that invocation reports completion.
> Calls from inside an `Interceptor.replace` callback are hooked normally.

**Drain the command queue in `advManager::Main`** @0x408710: `thiscall(adv, message* msg) -> int`. This is
`vtable[2]` of advManager. The executive main loop calls it once per input event and with `msg->command == 0` when
idle. [V]

Run queued commands at entry only when all of the following hold [R, plus `advManager+0x34 status != 2` (2 = another
manager such as the town screen runs)]:

```
msg.command == 0 (idle)
&& !gbGameOver
&& playerData::IsLocalHuman(gpCurPlayer)
&& giCurPlayer == giCurWatchPlayer
&& [0x6AAD88] == 0          (no modal open)
&& !gbInCombat
```

This is the same context in which the original button and click handlers run: `ProcessDeSelect` → `DoAdvCommand` /
`NextPlayer`. Anything they open (NormalDialog, combat, AI turn) runs synchronously inside the call.

- `Main` is not called while the town screen, combat or another manager runs (`executive::CallManager` builds its own
  loop) or while a `DoDialog` modal is open, so the queue simply waits.
- Hooks on `Main`: HD.HotA ×2 + HD+ ×3 HiHooks at entry, a HotA LoHook at 0x40890E. Attaching is fine.
- **The AI turn also runs inside `Main`**: when `!IsHuman(gpCurPlayer)`, Main calls the AI `move_hero(...)` and then
  `game::NextPlayer`. So the guard above also blocks commands during AI turns.

Alternative drain point: `inputManager::GetEvent` @0x4EC660, `thiscall(im, message* out)`, has no HotA/HD hooks. It is
also polled inside `DoDialog` loops and inside the hero movement loop, so check the caller (return address inside
`executive::MainLoop` @0x4B0BA0) before doing anything there.

Every `NativeFunction` engine call must run on the game thread, inside a hook callback. Snapshots should also be read
there, to avoid torn reads.

### Select a hero

`advManager::SetHeroContext` @0x417A80 `thiscall(adv, heroId, bInMove=0, waitingPlayer=0, drawChanges=1)` (ret 0x10).
It sets `gpCurPlayer->currHero = heroId` and centres the view. [V] Hooks: HD.HotA HiHook at entry and 13 more places (HotA and HD.HotA).

### Path to a destination (what the first click does)

`ProcessMapSelect` @0x40A530 does the following:

1. Set the hero's target: `hero+0x35 = x`, `+0x39 = y`, `i16 +0x3D = z`.
2. `adv+0x50 = 0`. This is `seedingValid`, the same as `advManager::Reseed`; it forces a full re-seed.
3. Call `advManager::ShowRoute` @0x418D30 `thiscall(adv, bUpdateScreen, bReseed, bChangeButton)` (ret 0xC). The UI uses
   `(1,1,1)`; use `(0,1,0)` to skip drawing.

`ShowRoute` works on the current hero (`currHero`) of the local human. It calls `SeedTo` @0x419400
`thiscall(adv, u32 target)` (`searchArray::SeedPosition` @0x56B710) and `searchArray::BuildPath` @0x56A3A0
`thiscall(sa, hero*, limit=59999) -> count`. [V]

**Result:**

- `gpSearchArray->result` is an `exe_vector<pathCell*>` at `sa+0x48` (first at **sa+0x4C**, last at sa+0x50).
- It is stored **in reverse**: `result[n-1]` is the first step and `result[0]` is the destination. n = 0 means no path.
- `pathCell` (0x1E bytes): `+0` type_point (tile of this step), `+4` u32 flags (bit0 = reached;
  **direction = (flags >> 12) & 0xF** [V asm 0x418FB7]; NH3API's bit 11 is wrong), `+8` previous point,
  `+0x18` u16 cumulative cost, `+0x1C` u16 move_left.
- The search grid is `sa+0x24 cellData + ((y + (2*z + sublayer)*MAP_HEIGHT)*MAP_WIDTH + x)`: two sub-layers per level.
- A destination is unreachable if bit 0 of its pathCell flags is 0 [V].
- [R] `ShowRoute(adv, 1, 1, 1)` after setting the target and `adv+0x50 = 0`: `result` is reversed as described,
  direction = `(flags >> 12) & 0xF`, `+0x18 cost` is the **cumulative** cost from the hero (it keeps growing across
  days), `+0x1C move_left` restarts from the full movement on the next day. An empty `result` = no path (also for any
  target behind a border guard or a guarded pass).

**Reachable today:**

- After `ShowRoute`, read `adv+0x48 pRouteArray` (`u16[]`, index `(z*MAP_HEIGHT + y)*MAP_WIDTH + x`). A value of 1–25 is
  a step reachable today (the arrow frame); **a value above 25 is beyond today** (+0x19 is the grey/red arrow set).
- Or repeat the UI loop: `mp = hero.currMobility`; for each step, `mp -= H3Hero::GetMovementCost(hero, dir, fromPoint, mp)`;
  steps with `mp >= 0` are reachable today.
- `GetMovementCost` @0x4B1620 is `fastcall(ecx=hero, edx=dir, [stack] u32 fromPoint, int mpLeft) -> int` (ret 8). [V]
- `reachable` in PROTOCOL = the number of leading steps that are reachable today. [R: pRouteArray 1..25 today,
  26/27 (+0x19) beyond]

Hooks: HotA patches SeedTo (7), ShowRoute (15), BuildPath (4 + HD.HotA HiHook) and GetMovementCost (3). Calling the
engine keeps HotA's movement rules.

### Move (the second click on the destination or the "Move hero" button)

ProcessDeSelect case 7 (the Move button) does exactly this:

1. Select the hero (SetHeroContext).
2. Set the target (+0x35/+0x39/+0x3D).
3. `adv+0x40 advCommand = 7`.
4. `void* ev = advManager::DoAdvCommand(adv, u32* triggerPoint)` @0x407AE0 (ret 4). Pass a fresh 4-byte buffer.

`advCommand = 1` does the same but takes the target from `adv.last_map_hover` (+0xE8). [V]

What `DoAdvCommand` does:

- runs `SeedTo` and `BuildPath`;
- loops `advManager::MoveHero` @0x47FF00 one step at a time;
- stops at the end of today's movement, on an event or on a battle;
- calls `DoEvent` @0x4AA710 `thiscall(adv, cell, u32 point)` itself, and returns 0 after doing so;
- if it returns a non-zero cell, call `DoEvent(adv, ev, *triggerPoint)` the way `Main` does.

DoAdvCommand hooks: 2 HD+ HiHooks at entry and patches at 30 more places (27 of them HotA).

`MoveHero` @0x47FF00 `thiscall(adv, dir, standEnd, u32* trigger, int* bNoMove, bool bComputer, int* bFoughtBattle, bool bRemote) -> NewmapCell*`
(ret 0x1C) is the per-step function. The AI calls it too, from 0x42FC50 with bComputer=1. Hook it (onEnter/onLeave on
the current hero's position) for `hero_move` events of **any** hero, AI included. It has 60 HotA/HD patches;
attach-only is fine.

Simpler option for the human's own move: send the path cells from `result` up to the hero's position after the call.

[R] The bridge: SetHeroContext (if needed) + target + `adv+0x50 = 0`, then the idle `Main` message becomes
`{0x200, 0xD, 7}`; `Main` -> `ProcessDeSelect` case 7 -> `DoAdvCommand` (walks, visits, fights) -> `DoEvent`. `MoveHero`
onLeave (position diff of every hero) gives the walked cells, AI heroes included. A resource pile is picked up from the
adjacent tile (the hero does not step on it).

### End turn

The End Turn button is ProcessDeSelect case 0xC. If `!tutorial && HasMobileHero(gpCurPlayer) && gConfig.MoveReminder`,
it first shows `NormalDialog(GameText[0x38], style 2)` and continues only on ACCEPT. Then it calls
**`game::NextPlayer` @0x4C6CA0 `thiscall(gpGame)`**. [V]

The bridge should call `NextPlayer` directly from the drain point. It skips the prompt; alternatively set
`gConfig.MoveReminder` (+0x20) to 0.

[R] The bridge instead lets `Main` process `{0x200, 0xD, 0xC}`: the move reminder comes through NormalDialog (forwarded
to the client as `yesno`), then NextPlayer. The whole AI round (3 AI players, their battles) runs in the next `Main`
call(s) and takes about a second with `Delay`/`DelayTil` replaced; `NextPlayer` onLeave gives every turn change.

What `NextPlayer` does:

- autosaves when the option is on;
- advances giCurPlayer (all humans first, then all AIs, then `PerDay` @0x4C7CA0 and the next day);
- recomputes movement points;
- for an AI, shows the "computer turn" screen and sets bShowIt = 0;
- runs `DoNewTurn` @0x4CC490: new-week and new-month messages come through NormalDialog.

The AI then plays inside later `Main` calls.

Hook NextPlayer onLeave for the `turn` event (`giCurPlayer`). It has 5 HiHooks at entry (HD.HotA, HD+, HotA) and patches
at 17 more places.

## 8. Dialogs and modal windows

### NormalDialog @0x4F6C00 (all yes/no and info questions; 214 callers)

```
void __fastcall NormalDialog(const char* text /*ecx*/, int style /*edx*/,
    int x, int y,                 // -1 = centred
    int pic1Type, int pic1Sub, int pic2Type, int pic2Sub,
    int special /*-1*/, int timeout /*0*/,
    int pic3Type, int pic3Sub)    // ret 0x28 (10 stack args)
```

[V: decomp sets icons from (a5,a6), (a7,a8), (a11,a12); a9 = special; a10 = timeout.]
NH3API has the order right; H3API's `show()` puts pic3 into special/timeout, which is wrong.

**Styles:**

| Style | Meaning | Buttons |
|---|---|---|
| 1 | OK | OK = 30722 |
| 2 | yes/no | 30725 ACCEPT / 30726 DECLINE |
| 4 | right-click popup | none |
| 5, 6 | variants | OK / cancel |
| 7 | choose between pictures | OK only |
| 10 | choose or cancel | Cancel = 30721 |

**Picture types:** 0–6 resource (subtype = amount), 8 artifact, 9 spell, 10 player crest, 11–13 luck,
14–16 morale, 17 experience, 20 secondary skill (`skill*3 + level + 2`), 21 creature, 22–30 building (per faction),
31–34 primary skills, 35 spell points, 36 money, -1 = none. [S H3API]

**Result:** the caller reads `gpWindowManager->dialogReturn` at **wm+0x38** afterwards. [V]

| Answer | Value |
|---|---|
| OK | 30722 (0x7802) |
| yes | 30725 |
| no | 30726 |
| cancel | 30721 |
| choose, left picture | **30729 (0x7809)** |
| choose, right picture | **30730 (0x780A)** |
| timeout | 9999 |

[V EventWindowHandler@0x4F1650: on OK in styles 7/10 it returns the selected icon id held in [0x699424].] H3API swaps
the names SELECT_LEFT/RIGHT. SoD supports only these two selection ids; HotA adds a LoHook in the icon creation
(0x4F788D), so three or more selectable pictures are [?].

Hooks: HD.TE ×3 + HD.HotA HiHooks at entry; a HotA LoHook at 0x4F6D32 (after the icons are set; it may add HotA icons).

[R] HotA's treasure chest calls this NormalDialog (style 7, gold / experience pictures) with a title written as
`{Сундук Сокровищ}` at the start of the text; the replacement returns 30729 / 30730 and the engine applies it (+1500
gold or +1000 exp). The move reminder is style 2; "week of ..." is style 1. Timed map events and other multi-picture
messages come through `extended_dialog` (also replaced).

**Plan for 1.5.** `Interceptor.replace` NormalDialog with a `fastcall` NativeCallback:

- For style 4, or when client mode is off, call the original through `new NativeFunction(0x4F6C00, …)`.
- Otherwise send `{text, style, pics}` to the client and block the game thread on
  `recv('dialog_answer', …).wait()`. Write the mapped value into `[gpWindowManager+0x38]` and return.
- Replacing skips the HD hooks and HotA's extra-icon LoHook. That is acceptable for the prototype.

### extended_dialog @0x4F7D20 (list of pictures, OK only; 20 callers)

`void __fastcall extended_dialog(const char* text, exe_vector<type_dialog_resource{int type; int sub;}>* pics, int x, int y, int timeout)`.
[V/S] It is info-only: forward it to the client and return. Hooks: HD.HotA + HD.TE HiHooks; a HotA LoHook at 0x4F7E4A.
`NormalDialogTimeOut` @0x4F6BC0 (cdecl, 12 args) is the timed variant.

### Other modal windows: identify and answer

**Window modals** go through `heroWindowManager::DoDialog` @0x602AE0
`thiscall(wm, heroWindow* w, handlerFn, bool fade) -> int` (ret 0xC). Most windows reach it via
`heroWindow::DoModal` = `H3Dlg::vShowAndRun` @0x5FFA20 (vtable+0x18). Hooks: HD.TE + 2 HotA HiHooks.

- `DoDialog` sets `dialogReturn = -1`, then loops `GetEvent` → `window->handle_message`.
- It closes when the handler returns 2 with `msg {command 0x200, subType 10, itemId}`; then `dialogReturn = itemId`.
- **Identify the window** in DoDialog onEnter by `u32 [window]` (its vtable), using the table below. A vtable inside
  HotA.dll means a HotA window; log it as `hota:<static VA>`.

**Manager screens** (town screen, hero meeting, combat) run through `executive::CallManager` @0x4B09D0
`thiscall(gpExec, manager*)`. The manager vtables are: advManager 0x63A678, combatManager 0x63D3E8,
townManager 0x643730, swapManager 0x64245C.

Window vtables found from constructors (`pc_sources.tsv`) [V address / S class]:

| Window | vtable | Opened from | How to answer or close |
|---|---|---|---|
| NormalDialog box (TDialogBox) | 0x63DB40 | DoNormalDialog@0x4F7020 (handler 0x4F0F60) | replace NormalDialog (above) |
| Level-up skill choice (TLevelUpWindow) [R: HotA's replacement of CheckLevel still uses it; ctor @0x4F8F10 `thiscall(this, hero, primary, skillPic1, skillPic2)`, skill pic = `(skill+1)*3 + level-1`; the bridge forwards it as `choose` and returns 0x7DA/0x7DB without running the loop] | 0x63FE74 | hero::CheckLevel@0x4DA990 (local human only; HotA ×4 HiHooks) | dialogReturn 0x7DA = left skill, 0x7DB = right, 9999 = let the AI choose. Easiest: in DoDialog onEnter set `giDialogTimeout = 1` (already expired): handler 0x4F9E10 closes with 9999 and the engine calls `AI_choose_secondary_skill`. To pick a skill instead, inject key "1"/"2" (KEY_DOWN, subType 2 or 3), then a click on 0x7802. |
| Combat results (TCombatResultsWindow) [R: the only window after a HotA quick combat, human attacking and human defending; skipped with 0x7802] | 0x63D46C | combatManager::DoVictory@0x476DA0 → 0x4716C0 → DoDialog(handler 0x4716E0) | `giDialogTimeout = 1` closes it as OK (0x7802) [V handler]. HotA's quick-combat "replay battle" option, if present, is [?] |
| Hero screen (THeroScreenWindow) | 0x63EAE8 | H3Hero::ShowDialog@0x4E1A70 (user action only) | not opened by game logic |
| Town screen (TTownScreenWindow inside townManager) | 0x64373C | town::View@0x5BE610 → CallManager(townManager); opens automatically when your hero enters your own town | replace `town::View` with a no-op that logs a `window` event; this skips HotA's LoHook at 0x5BE660 |
| Hero meeting (TSwapWindow) | 0x642438 | advManager::HeroSwap@0x4AAA60 → swapManager | replace with a no-op, or inject Esc [?] |
| Recruit at a dwelling (TRecruitWindow) | 0x640C5C | dwelling events | inject a click on the close button, or use the fallback below [?] |
| Garrison (TGarrisonWindow) | 0x643864 | DoEventGarrison | same |
| Tavern 0x643990, Marketplace 0x643A08, Black market 0x643A80, University 0x643BE8, Hill fort 0x63EB68, Altar (sacrifice_window) 0x641630, Town gate 0x642E40, Dimension door 0x63DB9C, Spellbook 0x641DDC, View world 0x643C64, Split stack 0x63B8F8, Adventure options 0x63A610, Main menu 0x63FF60, Game type 0x63E6D8, Scenario info 0x641720 | | | log them; close with the fallback below |

**Closing techniques** (from safest to bluntest):

1. **Timeout trick.** In DoDialog onEnter write `giDialogTimeout = 1`. Handlers that check it (combat results,
   level-up, NormalDialog's `EventWindowHandler`) close at once with their default. `DoNormalDialog` overwrites the
   value at its own start, so NormalDialog needs the replacement instead.
2. **Inject input.** In `GetEvent` onLeave, when the returned message is idle (`command == 0`), overwrite the 0x20-byte
   message:

   | Off | Field |
   |---|---|
   | +0 | command |
   | +4 | subType |
   | +8 | itemId |
   | +0xC | qualifier |
   | +0x10 / +0x14 | mouse x, y |

   - Button click: `{command 0x200, subType 0xD, itemId <button id>}` (OK = 0x7802, cancel = 0x7801).
   - Key: `{command 1, subType <DIK scancode>}` (Esc = 1, Enter = 0x1C).

   This is exactly how the UI reports clicks; the handlers test `0x200 / 0xD / id`. Alternatively append to the input
   ring buffer: `iBuffer[64]` at im+0x38, `iHead` +0x838, `iTail` +0x83C. [V struct; ? per window]
3. **Skip the modal.** Replace DoDialog for a known vtable: set `dialogReturn = 30721` (cancel) and return 0 without
   running the loop. This also skips HotA's DoDialog hooks [?].

## 9. Combat

**Make the human's battles resolve automatically.** Set **`gConfig.QuickCombat`** (u32 at gConfig+0x24 = **0x6987CC**)
to 1. [R: no battle screen; DoCombat returns 0/1; the attacker/defender `armyGroup` diff gives the losses; `point` is
the defender's (monster's) tile]

- `combatManager::IsQuickCombat` @0x46A080 (not hooked) returns `gConfig.QuickCombat != 0` in single player (not in the
  tutorial). Both sides are then played by the AI and nothing is drawn. [V]
- The snapshot already had 1 (the user's setting). Then only the results window appears (section 8).
- Other options: `combatManager+0x132C4 autoCombatOn` (the in-battle auto-combat button) or the `gConfig.Auto*` flags at
  +0x3C…+0x4C. These are not needed with quick combat.
- AI-vs-AI battles use `AI_quick_combat` @0x4270C0 without a combatManager.

**Start and end of every adventure-map battle:**
`advManager::DoCombat` @0x4AD160 (ret 0x28):

```
int __thiscall DoCombat(adv, u32 point, hero* atkHero, armyGroup* atkArmy,
    int defPlayer /*-1 neutral*/, town* defTown, hero* defHero /*NULL for monsters*/,
    armyGroup* defArmy, int seed /*-1*/, int bFinishHeroes, int altLayout)
    -> winner side: 0 = attacker, 1 = defender      // returns combatManager+0x13D48 'winner'
```

[V] Attach-only: snapshot both `armyGroup`s (`int type[7]` at +0, `int count[7]` at +0x1C) onEnter and diff them
onLeave to get losses. `point` is the battle tile; which tile it is (attacker's or monster's) is [?].
Hooks: 7 HiHooks at entry (HD.HotA, HD.TE ×2, HD+ ×2, HotA ×2) and patches at 25 more places. HotA's combat changes run inside, so
attach only and do not replace it.

Inside a battle drawn by the engine:

| Function / field | Address or offset |
|---|---|
| `combatManager::SetupCombat` | @0x4635D0 |
| `DoVictory` (writes the results) | @0x476DA0 `thiscall(cm, winnerSide)` |
| `combatManager.Heroes[2]` | +0x53CC |
| `iPlayer[2]` | +0x54A8 |
| `IsHuman[2]` | +0x54A4 |
| `Armies[2][21]` | +0x54CC, 0x548 each: +0x34 type, +0x4C count, +0x60 count at start |
| `winner` | +0x13D48 |
| `bCombatShowIt` | +0x13300 |

## 10. Speed

**Delays.** Both pump Windows messages while waiting, and neither is hooked by HotA/HD. [V]

- `GameTime::Delay(ms)` @**0x4F8A50**, `fastcall(ecx=ms)`.
- `GameTime::DelayTil(absTime)` @**0x4F8980**, `fastcall(ecx=timeGetTime deadline)`.
- `GameTime::Get` @0x4F8970 is `jmp [timeGetTime]`.
- The Dreamcast names at 0x4F8DF0/0x4F8E50 are a wrong match.
- To run at full speed: `Interceptor.replace` both with a callback that calls `Process1WindowsMessage` @0x4F8640 once
  (keeps the window alive) and returns.
- Caveat: while the game window is **minimized** (`IsIconic`), Delay blocks in `GetMessageA` until a message arrives.
  Keep the window un-minimized or hidden instead.

**Hero movement animation.** `gConfig.walkSpeed[2]`:

| Field | Address | Meaning |
|---|---|---|
| walkSpeed[1] | **0x6987AC** | own (local human) hero speed |
| walkSpeed[0] | **0x6987A8** | enemy/AI hero speed |

Value **4 = instant**: no step animation, only one redraw per step. Values 0–3 are animated speeds. HotA's default is 4.
[V animate code, DoSystemOptions] The snapshot had `[0]=4, [1]=3`. **Set both to 4.** [R: the bridge sets 4/4 and
replaces Delay/DelayTil; a 4-step move and a full AI round take about a second]

Other knobs:

| Knob | Where | Effect |
|---|---|---|
| `bShowIt` | @0x698A10 | 0 = no map animation or scrolling; NextPlayer sets it to 1 at every human turn |
| `gConfig.ShowRoute` | +0x1C | route display |
| `gConfig.MoveReminder` | +0x20 | end-turn prompt |
| `gbNoSound` | @0x6992E0, or volumes at gConfig+0x08/+0x0C = 0 | sound off |
| `gConfig.iCombatSpeed` | +0x94 | combat speed; irrelevant with quick combat |

Pick-up fizzles, fades and `FadeScreen` @0x603210 all wait through Delay/DelayTil.

`configStruct` field list (gConfig = 0x6987A8): `+0 walkSpeed[2], +8 musicVolume, +0xC soundVolume, +0x18 AutoSave,
+0x1C ShowRoute, +0x20 MoveReminder, +0x24 QuickCombat, +0x3C..+0x4C AutoCreatures/Spells/Catapult/Ballista/FirstAid,
+0x94 iCombatSpeed`. [S NH3API; V for walkSpeed, QuickCombat, MoveReminder, ShowRoute]

## 11. Save, load, new game

**Save** (safe from the drain point; the autosave in NextPlayer does the same) [V]:
`game::SaveGame` @0x4BEB60 `thiscall(gpGame, const char* name, bDetermineSuffix=1, bCampaignWinMode=0, compress=1, xferFile=0)`
(ret 0x14).

- Autosave calls it as `SaveGame(GameText[0x134/4], 1, 0, 1, 0)`.
- Pass the name without an extension; the suffix (.GM1) and the `[GAMES]` folder are added by the engine [S].
- Hooks: HD+ HiHook, HD.TE LoHook.

**Load:** `game::LoadGame` @0x4BEFF0 `thiscall(gpGame, const char* fileName, bIsOrigData=0, bIsQuickLoad=0)` (ret 0xC).
It opens `"[GAMES]" + fileName` (or `[DATA]`), and HD.TE redirects the folder string. [V] Hooks: HotA ×5, HD.TE patch.
`LoadGame` only fills `game`; the main loop then starts the adventure. The original flow is:

1. In-game "L": `gGameCommand = 0x66` and leave advManager.
2. `oldmain` @0x4EEA70 runs `TGameTypeWindow::DoModal` @0x4D5B20 (dialogReturn 100 = single-player load).
3. `PickLoadGame` @0x4F0CA0 (cdecl, returns bool) shows the selection window.
4. On OK, `TSingleSelectionWindow::BeginSavedGame` @0x58C560 runs `game::LoadGame(gpGame, gpGame->sSetup.cFilename, 0, 0)`.
5. Back in `oldmain`, `AddManager(gpAdvManager)` → adventure.

`sSetup.cFilename` = gpGame+0x1F6D9 (char[251]).

**Suggested auto-load chain** [?, verify; this is the 1.1 launcher's job]. Make each of these return at once:

- `TMainMenu::DoModal` @0x4FBD70: set `dialogReturn = 0x66` (Load).
- `TGameTypeWindow::DoModal`: set `dialogReturn = 100`.
- `PickLoadGame` (replace): strcpy the name into `sSetup.cFilename`, call `game::LoadGame(gpGame, name, 0, 0)`, set
  `dialogReturn = 30722`, return 1.

HotA/HD replace parts of the main menu (hooks at 0x4EEEBF, 0x4EF6B4/0x4EF6C8/0x4EF6E0, 0x4EF35F), so check which
`DoModal` actually runs. A new game from an .h3m goes through the scenario-selection window and `DoNewGame` @0x4F0720.
That is not easy; prefer prepared .GM1 saves.

**Game flow / phases [R, PLAN 4.5].** `oldmain` @0x4EEA70 (7068 bytes) is the outer loop: main menu (`TMainMenu`),
game type, scenario / load selection, campaign screens, high scores, credits; then `AddManager(gpAdvManager)` →
`executive::MainLoop` @0x4B0BA0 (the call returns into oldmain) → `RemoveManager`. The adventure map runs exactly
during that MainLoop call; town screen / combat MainLoops come from `CallManager` (other return addresses).
Leaving it: `gGameCommand` 0x6C main menu, 0x66 load (back through the game-type window), 0x6B restart, 0x69 quit,
`gbGameOver`. The scenario intro and the first "player X's turn" message are shown before MainLoop.
- System options: `ProcessDeSelect` item 10 → `advManager::DoSystemOptions` @0x41AB60 (window vtable 0x642478;
  dialogReturn 0x66 load, 0x69 quit, 0x6A save, 0x6B restart, 0x6C main menu → `gGameCommand`); item 9 →
  `DoAdventureOptions`. Main menu / load / quit ask "are you sure" (a `dialogBox` over it) first.
- Multiplayer window `TMultiPlayerWindow::OnWidgetDeselect` @0x50F740 `thiscall(this, itemId, bool* close)`:
  0x65 online (ShellExecute autorun.exe + ShutDown), 0x66 hot-seat, 0x67 IPX, 0x68 TCP/IP, 0x69 modem, 0x6A direct.
  The network ones reach `InitConnection` @0x555AD0: with `CreateDPlayObject` stubbed it calls
  `ShutDown("DirectPlay Error")` → MessageBox (invisible in session 0) → exit. `CDPlay::Create` @0x4968B0 and
  `CDPlayLobby::Init` @0x4985D0 CoCreateInstance CLSID_DirectPlay @0x643E28 / DirectPlayLobby @0x643DC8.
- Hot-seat: `giCurWatchPlayer` = the human whose turn it is; "Ходит …" and that player's intro are NormalDialogs
  inside `NextPlayer` (before its return).

## 12. Screen buffer (debugging)

`gpWindowManager+0x40` is `Bitmap16Bit* screenBitmap`, with `+0x24 Width`, `+0x28 Height`, `+0x2C Pitch` (bytes),
`+0x30 pixels`. [S]

- With HD the size is the configured resolution (1920×1080 here; HD patches 800/600 → 1920/1080).
- Bytes per pixel = Pitch / Width: 2 = RGB565, 4 = BGRX with HD True32 [?].
- Presented by `heroWindowManager::UpdateScreen` @0x603190 `thiscall(wm, x, y, w, h)`; hook it to grab frames.
  It has 3 HD HiHooks.

### 12.1 The game window and real input (PLAN 4.6, visible mode) [R]

- `WinMain` creates `hwndApp` (@0x699650, class `"Heroes III"`) with style 0x10CA0000 (`WS_VISIBLE` included) and
  never calls `ShowWindow` for it; `ShowWindow(hwndApp, SW_RESTORE)` comes later from hot-seat turn changes. The bridge
  hides it by dropping `WS_VISIBLE` in `CreateWindowExA` (hidden mode) and turning those `ShowWindow` calls into
  `SW_HIDE`.
- Present: `FUN_00600430` (from `H3Redraw` @0x603190 and WM_PAINT `FUN_00601DE0`) unlocks the offscreen RGB565
  surface, composites the mouse cursor sprite, `Blt`s the changed rectangle to the primary surface (dest in screen
  coordinates), relocks; `screenBitmap` then points at the relocked memory. `IsIconic(hwndApp)` skips presenting, and
  `Process1WindowsMessage` @0x4F8640 blocks in `GetMessageA` while the window is minimized.
- AppWndProc @0x4F8290: WM_ACTIVATEAPP only pauses video/sound and switches the system cursor (`WeAreActiveWindow`
  is only used for edge scrolling), so an inactive window plays on. Mouse messages go to `inputManager::CheckDown`
  @0x4EC370 (EDX = message, stack wParam, lParam; position from **lParam**), keys to @0x4EC1C0; posted
  `WM_KEYDOWN`/`WM_LBUTTONDOWN` therefore work like real input, also in session 0.
- Adventure map clicks: `advManager::ProcessSelect` @0x409740 item 0/0x25 → `ProcessMapSelect` @0x40A530, which walks
  only if the search array was seeded (`advManager+0x50` seedingValid). Seeding happens on hover
  (`SimulateMouseOver` @0x40E2C0 → `SeedTo` @0x419400), whose path calls `GetCursorPos`; in session 0
  `GetCursorPos` fails, so the bridge answers it with the window's last mouse-message position (visible mode).
- `advManager`: `+0x44` advWindow (TAdventureMapWindow: widgets as heroWindow `+0x28` head, widget `+0x10` id,
  `+0x18..+0x1E` x/y/w/h relative to the window `+0x18/+0x1C`), `+0xE4` map_origin (type_point, 10-bit signed x/y),
  `+0xE8` last_map_hover, `+0xEC/+0xF0` hover column/row; screen x = 32·(tile x − origin x) + 16, same for y.
  Hero list portraits are widgets 0x0F–0x13 (click on the selected hero = hero screen), system options = widget 10.
- NormalDialog buttons (widget ids in the top window): OK 30722, cancel 30721, yes 30725, no 30726, pictures of a
  choice 30729/30730 (style 7: picture then OK; OK is disabled until a picture is chosen). A queued click (rollover 4,
  down 8, up 16 at the widget centre) answers the dialog; `{0x200, 0xD, id}` / `{0x200, 10, id}` put into the input
  queue do not (DoDialog @0x602AE0 leaves its loop on a `{0x200, 10}` returned by the window's own handler).

## 13. Runtime check results (2026-10-18)

Checked by the bridge (`bridge_agent.js`, `bridge_runtime.js`, `test_client.py`) on GODOT_TEST.GM1 (The Newcomers) and
GODOT_BATTLE.GM1 (Too Many Monsters), h3hota.exe + HotA.dll 1.8.0 without HD.

| # | Item | Result |
|---|---|---|
| 1 | Hero base | `[0x4317EE] == [0x402480]`, **different on every launch**; `GetHero(0)` = `*gpGame + disp`; count 215 at 0x4BF620 and 0x4BB1EE. [R] |
| 2 | Terrain 10/11, RiverSet high bits | not on the test maps; the bridge sends `RiverSet & 7`. [?] |
| 3 | Mask orientation | bit 47 = anchor: town gate bit 45 = (ax-2, ay) = town x,y; mine entrance bit 46. access bit0 = union of block masks. [R] |
| 4 | Objects after pickup / hero objects | the id leaves every ObjectCellList (Objects keeps it); no type-34 objects on the lists. [R] |
| 5 | Path | reversed result, dir bits 12–15, cumulative cost at +0x18, pRouteArray > 25 = beyond today. [R] |
| 6 | Move | from the `Main` hook only by rewriting the idle message (Frida listener guard, section 7); then walk, visits, battles, dialogs work, no deadlock. [R] |
| 7 | NormalDialog replacement | chest style 7 -> 30729/30730 applied; style 2 move reminder; style 1 week message; `extended_dialog` for map events. [R] |
| 8 | Level-up / combat results | level-up: TLevelUpWindow via DoDialog, answered 0x7DA/0x7DB (skill learned, primary +1); combat results: skipped with OK, no other HotA window after quick combat. [R] |
| 9 | Town screen / hero meeting | town screen (CallManager townManager) runs and closes on an injected `{0x200, 10}` in the executive loop (townManager::Main 0x5D3640 case 10); hero meeting is skipped (not exercised). [R / ?] |
| 10 | Town fort / capitol, warehouse owners | fort bits match the town defs; capitol and HotA warehouses not on the test maps. [R / ?] |
| 11 | Speed | walkSpeed 4/4 + Delay/DelayTil replaced: AI round about a second (several AI battles); the two-game test runs in 45 s. [R] |
| - | Monster count | HotA table, see section 2. [R] |
| - | Names | `g_H3SecondarySkillText` 0x67DCF0 (0x10/entry), `g_H3ArtifactSetup` 0x660B68 (0x20), `g_H3Spell` 0x687FA8 (0x88, name +0x10), `g_H3CreatureInformation` 0x6747B0 (0x74, names +0x14/+0x18): pointer globals, cp1251 Russian names. [R for skills] |
