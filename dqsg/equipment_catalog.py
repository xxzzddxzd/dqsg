"""Equipment metadata helpers.

Armor master_id format: RABCC  (5 digits)
  R = rarity (1★/2★/3★)
  A = slot (0=weapon, 1=shield, 2=head, 3=chest, 4=lower)
  BCC = item index within rarity+slot

Weapon master_id format: R0TBCC  (6 digits)
  R = rarity (1★/2★/3★)
  0 = weapon indicator (always 0)
  T = weapon type (1=sword, 2=?, 3=spear, 4=claw, 5=boomerang, 6=whip)
  BCC = item index
"""

from __future__ import annotations

CONTENT_TYPE_WEAPON = 100
CONTENT_TYPE_ARMOR = 110

SLOT_WEAPON = 0
SLOT_SHIELD = 1
SLOT_HEAD = 2
SLOT_CHEST = 3
SLOT_LOWER = 4

ARMOR_SLOT_NAMES = {
    SLOT_WEAPON: "weapon",
    SLOT_SHIELD: "shield",
    SLOT_HEAD: "head",
    SLOT_CHEST: "chest",
    SLOT_LOWER: "lower",
}

ARMOR_SLOT_NAMES_ZH = {
    SLOT_WEAPON: "武器",
    SLOT_SHIELD: "盾牌",
    SLOT_HEAD: "头部",
    SLOT_CHEST: "衣",
    SLOT_LOWER: "裤",
}

WEAPON_TYPE_SWORD = 1
WEAPON_TYPE_AXE = 2
WEAPON_TYPE_SPEAR = 3
WEAPON_TYPE_STAFF = 4
WEAPON_TYPE_BOW = 5
WEAPON_TYPE_HAMMER = 6

WEAPON_TYPE_NAMES = {
    WEAPON_TYPE_SWORD: "sword",
    WEAPON_TYPE_AXE: "axe",
    WEAPON_TYPE_SPEAR: "spear",
    WEAPON_TYPE_STAFF: "staff",
    WEAPON_TYPE_BOW: "bow",
    WEAPON_TYPE_HAMMER: "hammer",
}

WEAPON_TYPE_NAMES_ZH = {
    WEAPON_TYPE_SWORD: "剑",
    WEAPON_TYPE_AXE: "斧",
    WEAPON_TYPE_SPEAR: "枪",
    WEAPON_TYPE_STAFF: "杖",
    WEAPON_TYPE_BOW: "弓",
    WEAPON_TYPE_HAMMER: "锤",
}

EQUIPMENT_METADATA: dict[int, dict[str, object]] = {
    301001: {"name": "火焰剑", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    301002: {"name": "隼之剑", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    302001: {"name": "冰之法杖", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    303001: {"name": "沙尘之矛", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    305002: {"name": "白金回旋镖", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    306001: {"name": "女王之鞭", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    304001: {"name": "水晶爪", "content_type": CONTENT_TYPE_WEAPON, "series": "normal"},
    32506: {"name": "金属头盔", "content_type": CONTENT_TYPE_ARMOR, "series": "metal"},
    33506: {"name": "金属胸甲", "content_type": CONTENT_TYPE_ARMOR, "series": "metal"},
    34502: {"name": "大魔导长袍下装", "content_type": CONTENT_TYPE_ARMOR, "series": "normal"},
    32504: {"name": "圣者帽", "content_type": CONTENT_TYPE_ARMOR, "series": "normal"},
    31003: {"name": "力量之盾", "content_type": CONTENT_TYPE_ARMOR, "series": "normal"},
}


def equipment_name(master_id: int) -> str | None:
    meta = EQUIPMENT_METADATA.get(master_id)
    if not meta:
        return None
    name = meta.get("name")
    return str(name) if name else None


def equipment_series(master_id: int) -> str | None:
    meta = EQUIPMENT_METADATA.get(master_id)
    if not meta:
        return None
    series = meta.get("series")
    return str(series) if series else None


def equipment_is_metal(master_id: int) -> bool:
    return equipment_series(master_id) == "metal"


def is_weapon_master_id(master_id: int) -> bool:
    return master_id >= 100000


def content_type_for_master_id(master_id: int) -> int:
    return CONTENT_TYPE_WEAPON if is_weapon_master_id(master_id) else CONTENT_TYPE_ARMOR


def armor_slot(master_id: int) -> int:
    return (master_id % 10000) // 1000


def armor_rarity(master_id: int) -> int:
    return master_id // 10000


def armor_slot_name(master_id: int, zh: bool = False) -> str:
    slot = armor_slot(master_id)
    table = ARMOR_SLOT_NAMES_ZH if zh else ARMOR_SLOT_NAMES
    return table.get(slot, f"slot{slot}")


def weapon_rarity(master_id: int) -> int:
    return master_id // 100000


def weapon_type(master_id: int) -> int:
    return (master_id % 100000) // 1000


def weapon_type_name(master_id: int, zh: bool = False) -> str:
    code = weapon_type(master_id)
    table = WEAPON_TYPE_NAMES_ZH if zh else WEAPON_TYPE_NAMES
    return table.get(code, f"type{code}")


def equipment_rarity(master_id: int) -> int:
    return weapon_rarity(master_id) if is_weapon_master_id(master_id) else armor_rarity(master_id)


def equipment_slot_name(master_id: int, zh: bool = False) -> str:
    return weapon_type_name(master_id, zh=zh) if is_weapon_master_id(master_id) else armor_slot_name(master_id, zh=zh)


def equipment_kind_name(master_id: int, zh: bool = False) -> str:
    if is_weapon_master_id(master_id):
        return "武器" if zh else "weapon"
    return "装备" if zh else "armor"


def equipment_display(master_id: int, zh: bool = False) -> str:
    name = equipment_name(master_id)
    if name:
        rarity = equipment_rarity(master_id)
        return f"{rarity}★ {name}"
    rarity = equipment_rarity(master_id)
    slot_name = equipment_slot_name(master_id, zh=zh)
    kind_name = equipment_kind_name(master_id, zh=zh)
    return f"{rarity}★ {slot_name} {kind_name} ({master_id})"
