extends RefCounted
## Shared constants and helpers: terrain ids, H3 object classes -> categories, masks, player colors.

enum { DIRT, SAND, GRASS, SNOW, SWAMP, ROUGH, SUBTER, LAVA, WATER, ROCK, HIGHLANDS, WASTELAND }

const TERRAIN_NAMES := ["dirt", "sand", "grass", "snow", "swamp", "rough", "subterranean", "lava", "water",
	"rock", "highlands", "wasteland"]
const RES_NAMES := ["wood", "mercury", "ore", "sulfur", "crystal", "gems", "gold"]
const RES_RU := ["Дерево", "Ртуть", "Руда", "Сера", "Кристаллы", "Самоцветы", "Золото"]
const RES_COLORS := [Color(0.55, 0.36, 0.18), Color(0.75, 0.78, 0.82), Color(0.42, 0.42, 0.44),
	Color(0.92, 0.8, 0.2), Color(0.85, 0.12, 0.15), Color(0.25, 0.75, 0.45), Color(1.0, 0.8, 0.25)]

const PLAYER_COLORS := {
	"red": Color(0.86, 0.12, 0.1), "blue": Color(0.15, 0.32, 0.9), "tan": Color(0.78, 0.62, 0.42),
	"green": Color(0.2, 0.62, 0.18), "orange": Color(0.95, 0.5, 0.1), "purple": Color(0.58, 0.22, 0.72),
	"teal": Color(0.1, 0.62, 0.62), "pink": Color(0.92, 0.5, 0.68)}
const PLAYER_ORDER := ["red", "blue", "tan", "green", "orange", "purple", "teal", "pink"]
const NEUTRAL := Color(0.62, 0.6, 0.55)

const FACTION_NAMES := ["Замок", "Оплот", "Башня", "Инферно", "Некрополис", "Темница", "Цитадель", "Крепость",
	"Сопряжение", "Причал", "Фабрика"]
## stone, roof, accent, glow per faction (Castle .. Factory)
const FACTION_PALETTE := [
	[Color(0.5, 0.5, 0.51), Color(0.16, 0.24, 0.5), Color(0.75, 0.62, 0.3), Color(0, 0, 0)],
	[Color(0.66, 0.62, 0.52), Color(0.2, 0.42, 0.2), Color(0.8, 0.7, 0.4), Color(0, 0, 0)],
	[Color(0.85, 0.85, 0.88), Color(0.3, 0.5, 0.75), Color(0.9, 0.8, 0.5), Color(0.3, 0.6, 1.0)],
	[Color(0.35, 0.22, 0.18), Color(0.5, 0.1, 0.06), Color(0.9, 0.4, 0.1), Color(1.0, 0.4, 0.05)],
	[Color(0.3, 0.3, 0.3), Color(0.16, 0.17, 0.2), Color(0.45, 0.5, 0.4), Color(0.2, 1.0, 0.45)],
	[Color(0.33, 0.28, 0.33), Color(0.32, 0.15, 0.38), Color(0.6, 0.4, 0.7), Color(0.7, 0.3, 1.0)],
	[Color(0.5, 0.38, 0.26), Color(0.42, 0.25, 0.12), Color(0.7, 0.5, 0.3), Color(0, 0, 0)],
	[Color(0.38, 0.4, 0.3), Color(0.28, 0.33, 0.18), Color(0.6, 0.6, 0.3), Color(0, 0, 0)],
	[Color(0.7, 0.78, 0.85), Color(0.25, 0.55, 0.8), Color(0.9, 0.8, 0.5), Color(0.4, 0.8, 1.0)],
	[Color(0.55, 0.55, 0.5), Color(0.1, 0.4, 0.42), Color(0.7, 0.6, 0.4), Color(0, 0, 0)],
	[Color(0.5, 0.42, 0.32), Color(0.45, 0.32, 0.12), Color(0.8, 0.6, 0.25), Color(1.0, 0.7, 0.3)],
]

## H3 object class -> procedural category (fallback when manifest has no model).
const CLASS_CATEGORY := {
	# vegetation
	135: "tree_oak", 186: "tree_oak", 137: "tree_pine", 188: "tree_pine", 155: "tree_mixed", 199: "tree_mixed",
	159: "tree_willow", 203: "tree_willow", 160: "tree_mixed", 204: "tree_mixed", 211: "plant",
	119: "tree_dead", 170: "tree_dead", 153: "stump", 197: "stump", 130: "log", 181: "log",
	114: "plant", 115: "plant", 116: "plant", 120: "flowers", 129: "mushrooms", 131: "plant", 132: "plant",
	138: "plant", 150: "plant", 156: "plant", 165: "plant", 166: "plant", 167: "plant", 171: "flowers",
	180: "mushrooms", 182: "plant", 183: "plant", 189: "plant", 194: "plant", 200: "plant", 125: "plant", 176: "plant",
	# relief
	134: "mountain", 185: "mountain", 206: "hill", 207: "hill", 208: "hill", 209: "hill", 123: "hill", 174: "hill",
	147: "rock", 191: "rock", 136: "rock", 187: "rock", 210: "rock", 152: "stalagmite", 196: "stalagmite",
	133: "rock", 184: "rock", 151: "skull", 195: "skull", 117: "crater", 168: "crater", 118: "crater", 169: "crater",
	124: "crater", 175: "crater", 158: "volcano", 202: "volcano", 157: "volcano", 201: "volcano",
	126: "lake", 177: "lake", 121: "frozen_lake", 172: "frozen_lake", 128: "lava_lake", 179: "lava_lake",
	127: "lava_lake", 178: "lava_lake", 149: "crater", 193: "crater", 154: "tar", 198: "tar", 148: "hill", 192: "hill",
	143: "plant", 190: "plant", 161: "rock", 205: "rock",
	21: "overlay", 46: "overlay", 222: "overlay", 223: "overlay", 224: "overlay", 225: "overlay", 226: "overlay",
	227: "overlay", 228: "overlay", 229: "overlay", 230: "overlay", 231: "overlay",
	# pickups
	79: "resource", 76: "resource", 29: "resource", 82: "chest", 101: "chest", 86: "chest", 59: "sign",
	5: "artifact", 65: "artifact", 66: "artifact", 67: "artifact", 68: "artifact", 69: "artifact", 93: "artifact",
	6: "pandora", 12: "campfire", 22: "skull", 105: "wagon", 39: "tent",
	# buildings
	98: "town", 77: "town", 53: "mine", 220: "mine",
	17: "dwelling", 18: "dwelling", 19: "dwelling", 20: "dwelling", 216: "dwelling", 217: "dwelling", 218: "dwelling",
	54: "monster", 71: "monster", 72: "monster", 73: "monster", 74: "monster", 75: "monster", 162: "monster",
	163: "monster", 164: "monster",
	112: "windmill", 109: "watermill", 57: "obelisk", 103: "gate", 43: "portal", 44: "portal", 45: "portal",
	111: "portal", 88: "shrine", 89: "shrine", 90: "shrine", 91: "sign", 8: "boat", 87: "shipyard",
	16: "ruins", 24: "ruins", 25: "ruins", 84: "ruins", 85: "ruins", 63: "pyramid", 108: "ruins",
	33: "garrison", 219: "garrison", 9: "gatehouse", 10: "tent", 212: "gatehouse", 215: "gatehouse",
	49: "well", 48: "well", 31: "well", 30: "well", 14: "well", 110: "well", 56: "well",
	55: "garden", 32: "shrine", 61: "tower", 58: "tower", 60: "tower", 42: "tower", 23: "tower", 27: "obelisk",
	62: "tower", 37: "tent", 13: "tent", 83: "tent", 113: "tent", 81: "tent", 94: "tent", 95: "tent",
	41: "shrine", 47: "shrine", 107: "shrine", 102: "garden", 100: "obelisk", 97: "tent", 99: "tent",
	221: "tent", 7: "tent", 2: "shrine", 104: "shrine", 106: "tent", 64: "obelisk", 96: "shrine", 80: "shrine",
	4: "shrine", 35: "tent", 51: "tent", 78: "tent", 213: "tent", 50: "shrine", 92: "ruins", 11: "obelisk", 52: "well",
	38: "obelisk", 28: "well", 26: "none", 36: "none", 0: "none", 34: "none", 70: "none", 214: "none",
}

const BUILDING_CATS := ["town", "mine", "dwelling", "windmill", "watermill", "shrine", "tower", "tent", "well",
	"garden", "gatehouse", "garrison", "ruins", "pyramid", "obelisk", "gate", "portal", "shipyard"]

## creature id -> faction index (for monster fallback colors)
static func creature_faction(cid: int) -> int:
	if cid < 0:
		return -1
	if cid <= 13: return 0
	if cid <= 27: return 1
	if cid <= 41: return 2
	if cid <= 55: return 3
	if cid <= 69: return 4
	if cid <= 83: return 5
	if cid <= 97: return 6
	if cid <= 111: return 7
	if cid <= 131 and cid >= 112: return 8
	return -1


static func category(obj: Dictionary) -> String:
	var t := int(obj.get("type", -1))
	if CLASS_CATEGORY.has(t):
		return CLASS_CATEGORY[t]
	var vis := String(obj.get("visit", "0"))
	if vis.hex_to_int() != 0:
		return "tent"
	return "rock"


static func owner_color(owner: int, players: Array) -> Color:
	if owner < 0:
		return NEUTRAL
	for p in players:
		if int(p.get("id", -1)) == owner:
			return PLAYER_COLORS.get(String(p.get("color", "")), NEUTRAL)
	if owner < PLAYER_ORDER.size():
		return PLAYER_COLORS[PLAYER_ORDER[owner]]
	return NEUTRAL


## Mask (12 hex digits, bit i = 8*row+col, anchor = bit 47) -> list of cells.
static func mask_cells(hex: String, ax: int, ay: int) -> Array[Vector2i]:
	var out: Array[Vector2i] = []
	var v := hex.hex_to_int() if hex != "" else 0
	if v == 0:
		return out
	for b in 48:
		if (v >> b) & 1:
			out.append(Vector2i(ax - 7 + b % 8, ay - 5 + b / 8))
	return out


static func obj_cells(obj: Dictionary) -> Array[Vector2i]:
	var ax := int(obj.x)
	var ay := int(obj.y)
	var cells := mask_cells(String(obj.get("block", "")), ax, ay)
	for c in mask_cells(String(obj.get("visit", "")), ax, ay):
		if not cells.has(c):
			cells.append(c)
	if cells.is_empty():
		cells.append(Vector2i(ax, ay))
	return cells


static func cell_center(x: float, y: float) -> Vector2:
	return Vector2(x + 0.5, y + 0.5)
