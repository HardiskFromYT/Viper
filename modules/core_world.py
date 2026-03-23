"""Core world packet handlers: char enum/create, player login, teleport, ping."""
import logging
import struct
import time

from modules.base import BaseModule
from opcodes import (CMSG_CHAR_ENUM, SMSG_CHAR_ENUM,
                     CMSG_CHAR_CREATE, SMSG_CHAR_CREATE,
                     CMSG_PLAYER_LOGIN,
                     SMSG_LOGIN_VERIFY_WORLD, SMSG_ACCOUNT_DATA_TIMES,
                     SMSG_TUTORIAL_FLAGS, SMSG_INITIAL_SPELLS,
                     SMSG_ACTION_BUTTONS, SMSG_FACTION_LIST,
                     SMSG_LOGIN_SETTIMESPEED, SMSG_UPDATE_OBJECT, SMSG_MOTD,
                     SMSG_ITEM_QUERY_SINGLE_RESPONSE,
                     CMSG_PING, SMSG_PONG,
                     CMSG_NAME_QUERY, SMSG_NAME_QUERY_RESPONSE,
                     SMSG_NEW_WORLD, SMSG_TRANSFER_PENDING,
                     MSG_MOVE_WORLDPORT_ACK, MSG_MOVE_TELEPORT_ACK)
from packets import ByteBuffer, pack_guid, build_server_packet
from database import (get_account, get_characters, create_character,
                      get_character_by_guid, add_inventory_item, get_inventory,
                      update_char_position, update_char_zone)

log = logging.getLogger("core_world")

# ── display IDs per race/gender ──────────────────────────────────────────────
RACE_DISPLAY = {
    1: (49,   50),   4: (55,  56),   7: (1563, 1564),
    2: (51,   52),   5: (57,  58),   8: (1478, 1479),
    3: (53,   54),   6: (59,  60),
}

RACE_START = {
    1: (0,  -8949.95,  -132.493,  83.5312, 0.0),
    2: (1,   -618.518, -4251.67,  38.718,  0.0),
    3: (0,  -6240.32,   331.033, 382.758,  6.17),
    4: (1,  10311.3,    832.463, 1326.41,  5.69),
    5: (0,   -284.81,  1686.41,   89.45,   2.28),
    6: (1,  -2917.58,  -257.98,   52.9968, 0.0),
    7: (0, -11916.1,  -1206.3,    92.33,   2.08),
    8: (1,  10089.0,   2006.27, 1328.07,   1.56),
}

# Update field indices — vanilla 1.12.1 (build 5875)
OBJECT_FIELD_GUID          = 0x0000
OBJECT_FIELD_TYPE          = 0x0002
OBJECT_FIELD_SCALE_X       = 0x0004
UNIT_FIELD_HEALTH          = 0x0016
UNIT_FIELD_POWER1          = 0x0017
UNIT_FIELD_MAXHEALTH       = 0x001C
UNIT_FIELD_MAXPOWER1       = 0x001D
UNIT_FIELD_LEVEL           = 0x0022
UNIT_FIELD_FACTIONTEMPLATE = 0x0023
UNIT_FIELD_BYTES_0         = 0x0024
UNIT_FIELD_FLAGS           = 0x002E
UNIT_FIELD_BOUNDINGRADIUS  = 0x0081
UNIT_FIELD_COMBATREACH     = 0x0082
UNIT_FIELD_DISPLAYID       = 0x0083
UNIT_FIELD_NATIVEDISPLAYID = 0x0084
PLAYER_BYTES               = 0x00C1
PLAYER_BYTES_2             = 0x00C2
PLAYER_BYTES_3             = 0x00C3
# Visible item fields — one entry per equipment slot (0=head … 18=tabard)
# Vanilla 1.12.1 (build 5875) layout from MaNGOS Zero UpdateFields.h:
#   OBJECT_END = 0x06, UNIT_END = 0xBC, quest log = 60 fields (0xC6..0x101)
#   Per slot (stride 12): creator GUID (2) + item data (8) + properties (1) + pad (1)
#   PLAYER_VISIBLE_ITEM_1_0 takes the **item entry ID** (client looks up display
#   from its own DBC cache; if missing it sends CMSG_ITEM_QUERY_SINGLE).
PLAYER_VISIBLE_ITEM_1_0    = 0x0104   # item-entry field for equipment slot 0 (head)
_VISIBLE_ITEM_STRIDE       = 12       # fields per visible-item slot (build 5875)
PLAYER_FIELD_INV_SLOT_HEAD = 0x01E6   # 23 equipment slot GUIDs (46 fields)
PLAYER_FIELD_PACK_SLOT_1   = 0x0214   # 16 backpack slot GUIDs (32 fields)
PLAYER_END                 = 0x0502

# ── Starter gear per race/class ──────────────────────────────────────────────
# item_id -> (displayid, inventory_type)
# inventory_type: 4=shirt, 7=legs, 8=feet, 13=1H weapon, 17=2H weapon, 20=robe, 21=main-hand
# For SMSG_CHAR_ENUM: equip slot order is:
#   0=head,1=neck,2=shoulders,3=shirt,4=chest,5=waist,6=legs,7=feet,8=wrists,
#   9=hands,10=ring1,11=ring2,12=trinket1,13=trinket2,14=back,15=mainhand,16=offhand,
#   17=ranged,18=tabard

# (race, class) -> list of (item_id, equip_slot)
# equip_slot maps to inventory slot for display
_STARTER_GEAR = {
    # Human Warrior
    (1, 1): [(38, 3), (39, 6), (40, 7), (25, 15), (6948, -1)],
    # Human Paladin
    (1, 2): [(45, 3), (44, 6), (43, 7), (36, 15), (6948, -1)],
    # Human Rogue
    (1, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Human Priest
    (1, 5): [(53, 3), (52, 6), (51, 7), (36, 15), (6948, -1)],
    # Human Mage
    (1, 8): [(56, 4), (1822, 15), (6948, -1)],
    # Human Warlock
    (1, 9): [(56, 4), (35, 15), (6948, -1)],
    # Orc Warrior
    (2, 1): [(6125, 3), (6126, 6), (6127, 7), (12282, 15), (6948, -1)],
    # Orc Hunter
    (2, 3): [(148, 3), (147, 6), (129, 7), (12282, 15), (6948, -1)],
    # Orc Rogue
    (2, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Orc Shaman
    (2, 7): [(154, 3), (153, 6), (36, 15), (6948, -1)],
    # Orc Warlock
    (2, 9): [(56, 4), (35, 15), (6948, -1)],
    # Dwarf Warrior
    (3, 1): [(38, 3), (39, 6), (40, 7), (12282, 15), (6948, -1)],
    # Dwarf Paladin
    (3, 2): [(45, 3), (44, 6), (43, 7), (36, 15), (6948, -1)],
    # Dwarf Hunter
    (3, 3): [(148, 3), (147, 6), (129, 7), (12282, 15), (6948, -1)],
    # Dwarf Rogue
    (3, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Dwarf Priest
    (3, 5): [(53, 3), (52, 6), (51, 7), (36, 15), (6948, -1)],
    # Night Elf Warrior
    (4, 1): [(38, 3), (39, 6), (40, 7), (25, 15), (6948, -1)],
    # Night Elf Hunter
    (4, 3): [(148, 3), (147, 6), (129, 7), (25, 15), (6948, -1)],
    # Night Elf Rogue
    (4, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Night Elf Priest
    (4, 5): [(53, 3), (52, 6), (51, 7), (36, 15), (6948, -1)],
    # Night Elf Druid
    (4, 11): [(56, 4), (35, 15), (6948, -1)],
    # Undead Warrior
    (5, 1): [(6125, 3), (6126, 6), (6127, 7), (25, 15), (6948, -1)],
    # Undead Rogue
    (5, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Undead Priest
    (5, 5): [(53, 3), (52, 6), (51, 7), (36, 15), (6948, -1)],
    # Undead Mage
    (5, 8): [(56, 4), (35, 15), (6948, -1)],
    # Undead Warlock
    (5, 9): [(56, 4), (35, 15), (6948, -1)],
    # Tauren Warrior
    (6, 1): [(154, 3), (153, 6), (2361, 15), (6948, -1)],
    # Tauren Hunter
    (6, 3): [(148, 3), (147, 6), (129, 7), (2361, 15), (6948, -1)],
    # Tauren Shaman
    (6, 7): [(154, 3), (153, 6), (36, 15), (6948, -1)],
    # Tauren Druid
    (6, 11): [(56, 4), (35, 15), (6948, -1)],
    # Gnome Warrior
    (7, 1): [(38, 3), (39, 6), (40, 7), (25, 15), (6948, -1)],
    # Gnome Rogue
    (7, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Gnome Mage
    (7, 8): [(56, 4), (35, 15), (6948, -1)],
    # Gnome Warlock
    (7, 9): [(56, 4), (35, 15), (6948, -1)],
    # Troll Warrior
    (8, 1): [(6125, 3), (6126, 6), (6127, 7), (12282, 15), (6948, -1)],
    # Troll Hunter
    (8, 3): [(148, 3), (147, 6), (129, 7), (12282, 15), (6948, -1)],
    # Troll Rogue
    (8, 4): [(2105, 3), (120, 6), (121, 7), (2092, 15), (6948, -1)],
    # Troll Priest
    (8, 5): [(53, 3), (52, 6), (51, 7), (36, 15), (6948, -1)],
    # Troll Mage
    (8, 8): [(56, 4), (35, 15), (6948, -1)],
    # Troll Shaman
    (8, 7): [(154, 3), (153, 6), (36, 15), (6948, -1)],
}

# Item cache: item_id -> (displayid, inventory_type)
_item_cache = {}


def _get_item_display(item_id):
    """Lookup item display info from world DB, with cache."""
    if item_id in _item_cache:
        return _item_cache[item_id]
    try:
        from modules.world_data import get_item_template
        tpl = get_item_template(item_id)
        if tpl:
            result = (int(tpl["displayid"] or 0), int(tpl["InventoryType"] or 0))
            _item_cache[item_id] = result
            return result
    except Exception:
        pass
    _item_cache[item_id] = (0, 0)
    return (0, 0)


def _give_starter_gear(db_path, char_id, race, cls):
    """Give starting items to a newly created character."""
    gear = _STARTER_GEAR.get((race, cls))
    if not gear:
        # Fallback: give basic shirt + pants + weapon + hearthstone
        gear = [(38, 3), (39, 6), (40, 7), (25, 15), (6948, -1)]
    for item_id, _slot in gear:
        add_inventory_item(db_path, char_id, item_id, 1)


# ── helpers ──────────────────────────────────────────────────────────────────

def _char_enum_packet(chars, db_path=None) -> bytes:
    buf = ByteBuffer()
    buf.uint8(len(chars))
    for c in chars:
        buf.uint64(c["id"])
        buf.cstring(c["name"])
        buf.uint8(c["race"]);  buf.uint8(c["class"]); buf.uint8(c["gender"])
        buf.uint8(c["skin"]);  buf.uint8(c["face"])
        buf.uint8(c["hair_style"]); buf.uint8(c["hair_color"]); buf.uint8(c["facial"])
        buf.uint8(c["level"])
        buf.uint32(c["zone"]); buf.uint32(c["map"])
        buf.float32(c["pos_x"]); buf.float32(c["pos_y"]); buf.float32(c["pos_z"])
        buf.uint32(0)   # guild_id
        buf.uint32(0)   # char_flags
        buf.uint8(1)    # first_login
        buf.uint32(0)   # pet_display_id
        buf.uint32(0)   # pet_level
        buf.uint32(0)   # pet_family

        # Equipment display: 20 slots (19 equip + 1 bag), each = displayid(u32) + invtype(u8)
        equip_display = [None] * 20  # slot -> (displayid, invtype) or None
        gear = _STARTER_GEAR.get((c["race"], c["class"]))
        if gear:
            for item_id, slot in gear:
                if 0 <= slot < 20:
                    equip_display[slot] = _get_item_display(item_id)
        for slot in range(20):
            if equip_display[slot]:
                buf.uint32(equip_display[slot][0])  # display id
                buf.uint8(equip_display[slot][1])    # inventory type
            else:
                buf.uint32(0); buf.uint8(0)
    return buf.bytes()


def _build_update_object(char, extra_fields=None) -> bytes:
    guid = char["id"]
    race, cls, gender = char["race"], char["class"], char["gender"]
    display = RACE_DISPLAY.get(race, (49, 50))[gender & 1]

    def _f2i(f):
        return struct.unpack("<I", struct.pack("<f", f))[0]

    fields = {
        OBJECT_FIELD_GUID:          guid & 0xFFFFFFFF,
        OBJECT_FIELD_GUID + 1:      (guid >> 32) & 0xFFFFFFFF,
        OBJECT_FIELD_TYPE:          0x19,           # OBJECT | UNIT | PLAYER
        OBJECT_FIELD_SCALE_X:       _f2i(1.0),
        UNIT_FIELD_HEALTH:          100,
        UNIT_FIELD_POWER1:          100,            # mana
        UNIT_FIELD_MAXHEALTH:       100,
        UNIT_FIELD_MAXPOWER1:       100,            # max mana
        UNIT_FIELD_LEVEL:           char["level"],
        UNIT_FIELD_FACTIONTEMPLATE: 1,
        UNIT_FIELD_BYTES_0:         race | (cls << 8) | (gender << 16) | (1 << 24),  # power type=mana
        UNIT_FIELD_FLAGS:           0x00000008,     # UNIT_FLAG_PLAYER_CONTROLLED
        UNIT_FIELD_BOUNDINGRADIUS:  _f2i(0.389),
        UNIT_FIELD_COMBATREACH:     _f2i(1.5),
        UNIT_FIELD_DISPLAYID:       display,
        UNIT_FIELD_NATIVEDISPLAYID: display,
        PLAYER_BYTES:               (char["skin"] | (char["face"] << 8) |
                                     (char["hair_style"] << 16) | (char["hair_color"] << 24)),
        PLAYER_BYTES_2:             char["facial"],
        PLAYER_BYTES_3:             gender,         # gender byte
    }

    # Visible equipment — set PLAYER_VISIBLE_ITEM_<n>_0 to the item's **entry ID**.
    # The client looks up the display model from its DBC cache; if unknown it
    # sends CMSG_ITEM_QUERY_SINGLE which movement.py handles.
    gear = _STARTER_GEAR.get((race, cls)) or [(38, 3), (39, 6), (40, 7), (25, 15)]
    for item_id, equip_slot in gear:
        if 0 <= equip_slot <= 18:
            fields[PLAYER_VISIBLE_ITEM_1_0 + equip_slot * _VISIBLE_ITEM_STRIDE] = item_id

    # Merge caller-supplied fields (e.g. pack slot GUIDs for inventory)
    if extra_fields:
        fields.update(extra_fields)

    # Movement block
    mv = ByteBuffer()
    mv.uint32(0)                                     # move flags
    mv.uint32(int(time.time() * 1000) & 0xFFFFFFFF)  # timestamp
    mv.float32(char["pos_x"]); mv.float32(char["pos_y"])
    mv.float32(char["pos_z"]); mv.float32(char["orientation"])
    mv.uint32(0)          # fall time
    mv.float32(2.5)       # walk speed
    mv.float32(7.0)       # run speed
    mv.float32(4.5)       # run back
    mv.float32(4.722222)  # swim
    mv.float32(2.5)       # swim back
    mv.float32(3.141593)  # turn rate

    # Bitmask + field data
    max_field = max(fields.keys()) + 1
    num_blocks = (max_field + 31) // 32
    mask_words = [0] * num_blocks
    field_data = bytearray()
    for idx in sorted(fields):
        mask_words[idx // 32] |= (1 << (idx % 32))
    for idx in sorted(fields):
        field_data += struct.pack("<I", fields[idx] & 0xFFFFFFFF)

    obj = ByteBuffer()
    obj.raw(pack_guid(guid))   # packed guid
    obj.uint8(4)               # object type: PLAYER
    obj.uint8(0x71)            # update flags: SELF|ALL|LIVING|HAS_POSITION
    obj.raw(mv.bytes())
    obj.uint32(1)              # UPDATEFLAG_ALL data
    obj.uint8(num_blocks)
    for w in mask_words:
        obj.uint32(w)
    obj.raw(field_data)

    pkt = ByteBuffer()
    pkt.uint32(1)   # count
    pkt.uint8(0)    # has_transport
    pkt.uint8(3)    # update_type: CREATE_OBJECT2
    pkt.raw(obj.bytes())
    return pkt.bytes()


def _presend_item_cache(session, char):
    """Proactively send SMSG_ITEM_QUERY_SINGLE_RESPONSE for all visible
    equipment items.  This populates (or replaces) the client's WDB cache
    so it can render gear immediately without needing to query."""
    race, cls = char["race"], char["class"]
    gear = _STARTER_GEAR.get((race, cls)) or [(38, 3), (39, 6), (40, 7), (25, 15)]

    # Also include inventory items
    try:
        inv = get_inventory(session.db_path, char["id"])
        inv_ids = {row["item_id"] for row in inv} if inv else set()
    except Exception:
        inv_ids = set()

    all_ids = {item_id for item_id, _ in gear if item_id} | inv_ids
    sent = 0
    for item_id in all_ids:
        try:
            from modules.world_data import get_item_template
            tpl = get_item_template(item_id)
            if not tpl:
                continue
            tpl = dict(tpl)
            pkt = _build_item_query_response(item_id, tpl)
            if pkt:
                session._send(SMSG_ITEM_QUERY_SINGLE_RESPONSE, pkt)
                sent += 1
        except Exception:
            pass
    if sent:
        log.debug(f"Pre-sent {sent} item cache entries for {char['name']}")


def _build_item_query_response(item_id, tpl):
    """Build an SMSG_ITEM_QUERY_SINGLE_RESPONSE payload from an item template dict."""
    buf = ByteBuffer()
    buf.uint32(item_id)
    buf.uint32(int(tpl.get("class") or 0))
    buf.uint32(int(tpl.get("subclass") or 0))
    buf.cstring(str(tpl.get("name") or "Item"))
    buf.uint8(0); buf.uint8(0); buf.uint8(0)  # names 2-4
    buf.uint32(int(tpl.get("displayid") or 0))
    buf.uint32(int(tpl.get("Quality") or 0))
    buf.uint32(int(tpl.get("Flags") or 0))
    buf.uint32(int(tpl.get("BuyPrice") or 0))
    buf.uint32(int(tpl.get("SellPrice") or 0))
    buf.uint32(int(tpl.get("InventoryType") or 0))
    buf.uint32(int(tpl.get("AllowableClass") or -1) & 0xFFFFFFFF)
    buf.uint32(int(tpl.get("AllowableRace") or -1) & 0xFFFFFFFF)
    buf.uint32(int(tpl.get("ItemLevel") or 1))
    buf.uint32(int(tpl.get("RequiredLevel") or 0))
    buf.uint32(int(tpl.get("RequiredSkill") or 0))
    buf.uint32(int(tpl.get("RequiredSkillRank") or 0))
    buf.uint32(int(tpl.get("requiredspell") or 0))
    buf.uint32(int(tpl.get("requiredhonorrank") or 0))
    buf.uint32(0)  # required_city_rank
    buf.uint32(int(tpl.get("RequiredReputationFaction") or 0))
    buf.uint32(int(tpl.get("RequiredReputationRank") or 0))
    buf.uint32(int(tpl.get("maxcount") or 0))
    buf.uint32(int(tpl.get("stackable") or 1))
    buf.uint32(int(tpl.get("ContainerSlots") or 0))
    for i in range(1, 11):
        buf.uint32(int(tpl.get(f"stat_type{i}") or 0))
        buf.uint32(int(tpl.get(f"stat_value{i}") or 0) & 0xFFFFFFFF)
    for i in range(1, 6):
        buf.float32(float(tpl.get(f"dmg_min{i}") or 0))
        buf.float32(float(tpl.get(f"dmg_max{i}") or 0))
        buf.uint32(int(tpl.get(f"dmg_type{i}") or 0))
    for key in ("armor", "holy_res", "fire_res", "nature_res",
                "frost_res", "shadow_res", "arcane_res"):
        buf.uint32(int(tpl.get(key) or 0) & 0xFFFFFFFF)
    buf.uint32(int(tpl.get("delay") or 0))
    buf.uint32(int(tpl.get("ammo_type") or 0))
    buf.float32(float(tpl.get("RangedModRange") or 0))
    for i in range(1, 6):
        buf.uint32(int(tpl.get(f"spellid_{i}") or 0))
        buf.uint32(int(tpl.get(f"spelltrigger_{i}") or 0))
        buf.uint32(int(tpl.get(f"spellcharges_{i}") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get(f"spellcooldown_{i}") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get(f"spellcategory_{i}") or 0))
        buf.uint32(int(tpl.get(f"spellcategorycooldown_{i}") or 0) & 0xFFFFFFFF)
    buf.uint32(int(tpl.get("bonding") or 0))
    buf.cstring(str(tpl.get("description") or ""))
    buf.uint32(int(tpl.get("PageText") or 0))
    buf.uint32(int(tpl.get("LanguageID") or 0))
    buf.uint32(int(tpl.get("PageMaterial") or 0))
    buf.uint32(int(tpl.get("startquest") or 0))
    buf.uint32(int(tpl.get("lockid") or 0))
    buf.uint32(int(tpl.get("Material") or 0))
    buf.uint32(int(tpl.get("sheath") or 0))
    buf.uint32(int(tpl.get("RandomProperty") or 0))
    buf.uint32(int(tpl.get("block") or 0))
    buf.uint32(int(tpl.get("itemset") or 0))
    buf.uint32(int(tpl.get("MaxDurability") or 0))
    buf.uint32(int(tpl.get("area") or 0))
    buf.uint32(int(tpl.get("Map") or 0))
    buf.uint32(int(tpl.get("BagFamily") or 0))
    return buf.bytes()


# ── Inventory item objects ────────────────────────────────────────────────────

# Item GUIDs: use a range that won't collide with characters or creatures
_ITEM_GUID_BASE = 0x200000000

# Item update-field offsets (from UpdateFields.h, OBJECT_END = 0x06)
_ITEM_FIELD_OWNER          = 0x06   # Size:2 (GUID)
_ITEM_FIELD_CONTAINED      = 0x08   # Size:2 (GUID)
_ITEM_FIELD_STACK_COUNT    = 0x0E   # Size:1
_ITEM_FIELD_DURABILITY     = 0x2E   # Size:1
_ITEM_FIELD_MAXDURABILITY  = 0x2F   # Size:1
OBJECT_FIELD_ENTRY         = 0x03   # Size:1


def _build_inventory_objects(char, db_path):
    """Build pack-slot fields (for the player object) and a CREATE_OBJECT packet
    containing all the character's inventory items (backpack slots 0-15).

    Returns (pack_slot_fields: dict, items_packet: bytes|None).
    """
    inv = get_inventory(db_path, char["id"])
    if not inv:
        return {}, None

    player_guid = char["id"]
    pack_fields = {}
    item_objects = []

    def _f2i(f):
        return struct.unpack("<I", struct.pack("<f", f))[0]

    for slot_idx, row in enumerate(inv[:16]):  # backpack = 16 slots
        item_guid = _ITEM_GUID_BASE + row["id"]

        # Tell the player object which GUID sits in each pack slot
        pack_fields[PLAYER_FIELD_PACK_SLOT_1 + slot_idx * 2] = item_guid & 0xFFFFFFFF
        pack_fields[PLAYER_FIELD_PACK_SLOT_1 + slot_idx * 2 + 1] = (item_guid >> 32) & 0xFFFFFFFF

        # Item object fields
        fields = {
            OBJECT_FIELD_GUID:         item_guid & 0xFFFFFFFF,
            OBJECT_FIELD_GUID + 1:     (item_guid >> 32) & 0xFFFFFFFF,
            OBJECT_FIELD_TYPE:         0x03,         # TYPEMASK_OBJECT | TYPEMASK_ITEM
            OBJECT_FIELD_ENTRY:        row["item_id"],
            OBJECT_FIELD_SCALE_X:      _f2i(1.0),
            _ITEM_FIELD_OWNER:         player_guid & 0xFFFFFFFF,
            _ITEM_FIELD_OWNER + 1:     (player_guid >> 32) & 0xFFFFFFFF,
            _ITEM_FIELD_CONTAINED:     player_guid & 0xFFFFFFFF,
            _ITEM_FIELD_CONTAINED + 1: (player_guid >> 32) & 0xFFFFFFFF,
            _ITEM_FIELD_STACK_COUNT:   row["count"],
        }

        # Look up max durability from world.db and set both fields
        try:
            from modules.world_data import get_item_template
            tpl = get_item_template(row["item_id"])
            if tpl and int(tpl["MaxDurability"] or 0):
                dur = int(tpl["MaxDurability"])
                fields[_ITEM_FIELD_DURABILITY] = dur
                fields[_ITEM_FIELD_MAXDURABILITY] = dur
        except Exception:
            pass

        max_field = max(fields.keys()) + 1
        num_blocks = (max_field + 31) // 32
        mask_words = [0] * num_blocks
        field_data = bytearray()
        for idx in sorted(fields):
            mask_words[idx // 32] |= 1 << (idx % 32)
        for idx in sorted(fields):
            field_data += struct.pack("<I", int(fields[idx]) & 0xFFFFFFFF)

        obj = ByteBuffer()
        obj.raw(pack_guid(item_guid))
        obj.uint8(1)       # object type: ITEM
        obj.uint8(0x10)    # update flags: UPDATEFLAG_ALL
        obj.uint32(0)      # high GUID data
        obj.uint8(num_blocks)
        for w in mask_words:
            obj.uint32(w)
        obj.raw(field_data)
        item_objects.append(obj.bytes())

    if not item_objects:
        return {}, None

    pkt = ByteBuffer()
    pkt.uint32(len(item_objects))
    pkt.uint8(0)   # has_transport
    for obj_bytes in item_objects:
        pkt.uint8(2)   # UPDATE_TYPE: CREATE_OBJECT
        pkt.raw(obj_bytes)

    return pack_fields, pkt.bytes()


# ── Login sequence ────────────────────────────────────────────────────────────

def _send_login_packets(session, char):
    """Send the full login packet sequence — used for initial login."""
    _send_world_init_packets(session, char, is_login=True)


def _send_world_init_packets(session, char, is_login=False):
    """Send world initialization packets.
    Called on initial login (is_login=True) or after cross-map teleport (is_login=False).
    """
    if is_login:
        # Only on initial login — SMSG_LOGIN_VERIFY_WORLD
        b = ByteBuffer()
        b.uint32(char["map"])
        b.float32(char["pos_x"]); b.float32(char["pos_y"])
        b.float32(char["pos_z"]); b.float32(char["orientation"])
        session._send(SMSG_LOGIN_VERIFY_WORLD, b.bytes())

        # Account data times (8 × uint32 zeros)
        session._send(SMSG_ACCOUNT_DATA_TIMES, b"\x00" * 32)

    # Tutorial flags
    session._send(SMSG_TUTORIAL_FLAGS, b"\xff" * 32)

    # Initial spells (empty)
    b = ByteBuffer(); b.uint8(0); b.uint16(0); b.uint16(0)
    session._send(SMSG_INITIAL_SPELLS, b.bytes())

    # Action buttons
    session._send(SMSG_ACTION_BUTTONS, b"\x00" * 480)

    # Faction list
    session._send(SMSG_FACTION_LIST, struct.pack("<I", 0))

    # Time / speed
    t = time.gmtime()
    packed = (((t.tm_year - 2000) << 24) | ((t.tm_mon - 1) << 20) |
              ((t.tm_mday - 1) << 14) | (t.tm_hour << 6) | t.tm_min)
    b = ByteBuffer(); b.uint32(packed); b.float32(0.01666667)
    session._send(SMSG_LOGIN_SETTIMESPEED, b.bytes())

    # Build inventory items and pack-slot fields for the player object
    pack_fields = {}
    items_pkt = None
    try:
        pack_fields, items_pkt = _build_inventory_objects(char, session.db_path)
    except Exception as e:
        log.warning(f"Failed to build inventory for {char.get('name', '?')}: {e}")

    # Pre-send item cache data for all visible equipment so the client
    # doesn't need to query (avoids stale WDB cache issues).
    _presend_item_cache(session, char)

    # Player create object (with pack-slot GUIDs if inventory exists)
    session._send(SMSG_UPDATE_OBJECT, _build_update_object(char, extra_fields=pack_fields))

    # Send inventory item objects so they appear in the backpack
    if items_pkt:
        try:
            session._send(SMSG_UPDATE_OBJECT, items_pkt)
            log.debug(f"Sent {len(pack_fields)//2} inventory items for {char['name']}")
        except Exception as e:
            log.warning(f"Failed to send inventory items: {e}")

    if is_login:
        b = ByteBuffer(); b.uint32(1); b.cstring("Welcome to TestEmu!")
        session._send(SMSG_MOTD, b.bytes())
        log.info(f"World entry complete for {char['name']}")
    else:
        log.info(f"Teleport re-init complete for {char['name']}")


def teleport_player(session, map_id: int, x: float, y: float, z: float, o: float,
                    zone_id: int = 0):
    """Teleport a player. Handles same-map (near) and cross-map (far) teleports.
    zone_id: if non-zero, immediately saves the destination zone so the
    character select screen reflects it without waiting for CMSG_ZONEUPDATE.
    """
    if not session.char:
        return

    old_map = session.char["map"]

    # Update position (and zone if known) in memory and DB
    session.char = dict(session.char)
    session.char.update({"map": map_id, "pos_x": x, "pos_y": y, "pos_z": z, "orientation": o})
    if zone_id:
        session.char["zone"] = zone_id
        update_char_zone(session.db_path, session.char["id"], zone_id)
    update_char_position(session.db_path, session.char["id"], map_id, x, y, z, o)

    if map_id == old_map:
        # ── Same-map teleport: send MSG_MOVE_TELEPORT_ACK ──
        guid = session.char["id"]
        buf = ByteBuffer()
        buf.raw(pack_guid(guid))
        buf.uint32(0)                                       # counter
        buf.uint32(0)                                       # move flags
        buf.uint32(int(time.time() * 1000) & 0xFFFFFFFF)   # timestamp
        buf.float32(x)
        buf.float32(y)
        buf.float32(z)
        buf.float32(o)
        buf.uint32(0)                                       # fall time
        session._send(MSG_MOVE_TELEPORT_ACK, buf.bytes())
        log.info(f"Near-teleport {session.char['name']} to {x:.1f},{y:.1f},{z:.1f} map {map_id}")
    else:
        # ── Cross-map teleport: TRANSFER_PENDING → NEW_WORLD → wait for WORLDPORT_ACK ──
        # Mark session as pending teleport so WORLDPORT_ACK handler knows to re-init
        session._pending_teleport = True

        # Step 1: SMSG_TRANSFER_PENDING
        buf = ByteBuffer()
        buf.uint32(map_id)
        session._send(SMSG_TRANSFER_PENDING, buf.bytes())

        # Step 2: SMSG_NEW_WORLD
        buf = ByteBuffer()
        buf.uint32(map_id)
        buf.float32(x); buf.float32(y); buf.float32(z); buf.float32(o)
        session._send(SMSG_NEW_WORLD, buf.bytes())

        log.info(f"Far-teleport {session.char['name']} to {x:.1f},{y:.1f},{z:.1f} map {map_id} "
                 f"(waiting for WORLDPORT_ACK)")


# ── Module ────────────────────────────────────────────────────────────────────

class Module(BaseModule):
    name = "core_world"

    def on_load(self, server):
        self._server = server
        self.reg_packet(server, CMSG_CHAR_ENUM,        self._char_enum)
        self.reg_packet(server, CMSG_CHAR_CREATE,      self._char_create)
        self.reg_packet(server, CMSG_PLAYER_LOGIN,     self._player_login)
        self.reg_packet(server, CMSG_PING,             self._ping)
        self.reg_packet(server, CMSG_NAME_QUERY,       self._name_query)
        self.reg_packet(server, MSG_MOVE_WORLDPORT_ACK, self._worldport_ack)
        self.reg_packet(server, MSG_MOVE_TELEPORT_ACK,  self._teleport_ack)
        log.info("core_world loaded.")

    def on_unload(self, server):
        log.info("core_world unloaded.")

    # ── packet handlers ───────────────────────────────────────────────

    def _char_enum(self, session, _payload):
        acct = get_account(session.db_path, session.account)
        if not acct:
            session._send(SMSG_CHAR_ENUM, bytes([0]))
            return
        chars = get_characters(session.db_path, acct["id"])
        session._send(SMSG_CHAR_ENUM, _char_enum_packet(chars, session.db_path))

    def _char_create(self, session, payload: bytes):
        try:
            end  = payload.index(b"\x00")
            name = payload[:end].decode()
            i    = end + 1
            race, cls, gender = payload[i], payload[i+1], payload[i+2]; i += 3
            skin, face        = payload[i], payload[i+1]; i += 2
            hair_s, hair_c    = payload[i], payload[i+1]; i += 2
            facial            = payload[i]
        except Exception as e:
            log.error(f"Char create parse: {e}")
            session._send(SMSG_CHAR_CREATE, bytes([0x2F]))  # error
            return

        acct = get_account(session.db_path, session.account)
        if not acct:
            session._send(SMSG_CHAR_CREATE, bytes([0x2F]))
            return

        char_id = create_character(session.db_path, acct["id"], name, race, cls, gender,
                                   skin, face, hair_s, hair_c, facial)
        _give_starter_gear(session.db_path, char_id, race, cls)
        log.info(f"Created char '{name}' (id={char_id}) for {session.account} with starter gear")
        session._send(SMSG_CHAR_CREATE, bytes([0x2E]))  # success

    def _player_login(self, session, payload: bytes):
        guid = struct.unpack_from("<Q", payload, 0)[0]
        char = get_character_by_guid(session.db_path, guid)
        if not char:
            log.warning(f"Login to unknown GUID {guid}")
            return
        session.char = char
        session.char_guid = guid
        log.info(f"Player login: {char['name']} (GUID {guid})")
        _send_login_packets(session, char)

    def _ping(self, session, payload: bytes):
        # CMSG_PING: uint32 ping_id + uint32 latency
        ping_id = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
        session._send(SMSG_PONG, struct.pack("<I", ping_id))

    def _name_query(self, session, payload: bytes):
        """Respond to CMSG_NAME_QUERY with character info."""
        if len(payload) < 8:
            return
        guid = struct.unpack_from("<Q", payload, 0)[0]
        log.info(f"NAME_QUERY for guid={guid}")
        # Check if it's one of our online players
        char = None
        for s in self._server.get_online_players():
            if s.char and s.char["id"] == guid:
                char = s.char
                break
        if not char:
            char = get_character_by_guid(session.db_path, guid)
        if not char:
            log.warning(f"NAME_QUERY: no char found for guid={guid}")
            return
        buf = ByteBuffer()
        buf.uint64(guid)
        buf.cstring(char["name"])
        buf.uint8(0)                # realm name (empty for same realm)
        buf.uint32(char["race"])
        buf.uint32(char["gender"])
        buf.uint32(char["class"])
        data = buf.bytes()
        log.info(f"NAME_QUERY_RESPONSE guid={guid} name='{char['name']}' "
                 f"race={char['race']} gender={char['gender']} class={char['class']} "
                 f"payload_hex={data.hex()}")
        session._send(SMSG_NAME_QUERY_RESPONSE, data)

    def _worldport_ack(self, session, payload: bytes):
        """Handle MSG_MOVE_WORLDPORT_ACK — client finished loading after cross-map teleport.
        Always re-send the full login sequence so the client can move and interact."""
        if not session.char:
            return
        session._pending_teleport = False
        log.info(f"WORLDPORT_ACK from {session.char['name']} — re-sending world init")
        _send_world_init_packets(session, session.char, is_login=True)

    def _teleport_ack(self, session, payload: bytes):
        """Handle MSG_MOVE_TELEPORT_ACK — client confirms same-map teleport."""
        # Nothing critical to do — the position is already updated server-side
        if session.char:
            log.debug(f"TELEPORT_ACK from {session.char['name']}")
