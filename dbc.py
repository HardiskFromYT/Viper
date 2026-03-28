"""DBC data — extracted from the WoW 1.12.1 client and stored in dbc.db (SQLite).

Extraction (one-time):
    python3 extract_dbc.py [/path/to/WoW/Data]

Runtime (server reads dbc.db):
    from dbc import get_start_outfit
    items = get_start_outfit(race=1, class_id=1)
    # Returns list of (item_id, display_id, inventory_type)
"""
import logging
import os
import sqlite3

log = logging.getLogger("dbc")

DBC_DB = os.path.join(os.path.dirname(__file__), "dbc.db")

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        if not os.path.exists(DBC_DB):
            raise RuntimeError(
                f"dbc.db not found. Run: python3 extract_dbc.py /path/to/WoW/Data"
            )
        _conn = sqlite3.connect(DBC_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


# ── CharStartOutfit ──────────────────────────────────────────────────────────

_outfit_cache: dict[tuple[int, int], list[tuple[int, int, int]]] | None = None


def get_start_outfit(race: int, class_id: int) -> list[tuple[int, int, int]]:
    """Get starter outfit for a race/class combo.
    Returns list of (item_id, display_id, inventory_type) tuples.
    """
    global _outfit_cache
    if _outfit_cache is None:
        _outfit_cache = {}
        try:
            rows = _db().execute(
                "SELECT race, class, item_id, display_id, inventory_type "
                "FROM char_start_outfit WHERE gender=0 "
                "ORDER BY race, class, slot_index"
            ).fetchall()
            for r in rows:
                key = (r["race"], r["class"])
                _outfit_cache.setdefault(key, []).append(
                    (r["item_id"], r["display_id"], r["inventory_type"])
                )
            log.info(f"Loaded {len(_outfit_cache)} race/class starter outfits from dbc.db")
        except Exception as e:
            log.warning(f"Failed to load CharStartOutfit from dbc.db: {e}")
    return _outfit_cache.get((race, class_id), [])


# ── Inventory type → equipment slot mapping ──────────────────────────────────

_INVTYPE_TO_SLOT = {
    1: 0,    # Head
    2: 1,    # Neck
    3: 2,    # Shoulder
    4: 3,    # Shirt
    5: 4,    # Chest
    6: 5,    # Waist
    7: 6,    # Legs
    8: 7,    # Feet
    9: 8,    # Wrists
    10: 9,   # Hands
    11: 10,  # Finger (ring1)
    12: 12,  # Trinket (trinket1)
    13: 15,  # One-Hand → main hand
    14: 16,  # Shield → off hand
    15: 17,  # Ranged (bow)
    16: 14,  # Back (cloak)
    17: 15,  # Two-Hand → main hand
    20: 4,   # Robe → chest
    21: 15,  # Main-Hand
    22: 16,  # Off-Hand
    23: 16,  # Holdable → off hand
    25: 17,  # Thrown → ranged
    26: 17,  # Ranged Right (gun/wand) → ranged
}


def invtype_to_slot(inv_type: int) -> int:
    """Convert DBC inventory type to equipment slot index. Returns -1 for non-equippable."""
    return _INVTYPE_TO_SLOT.get(inv_type, -1)


# ── FactionTemplate ──────────────────────────────────────────────────────────

_ft_cache: dict[int, dict] | None = None


def _load_faction_templates():
    global _ft_cache
    if _ft_cache is not None:
        return
    _ft_cache = {}
    try:
        rows = _db().execute("SELECT * FROM faction_template").fetchall()
        for r in rows:
            _ft_cache[r["id"]] = {
                "id": r["id"],
                "faction": r["faction"],
                "flags": r["flags"],
                "faction_group": r["faction_group"],
                "friend_group": r["friend_group"],
                "enemy_group": r["enemy_group"],
                "enemy_factions": [r["enemy_faction1"], r["enemy_faction2"],
                                   r["enemy_faction3"], r["enemy_faction4"]],
                "friend_factions": [r["friend_faction1"], r["friend_faction2"],
                                    r["friend_faction3"], r["friend_faction4"]],
            }
        log.info(f"Loaded {len(_ft_cache)} faction templates from dbc.db")
    except Exception as e:
        log.warning(f"Failed to load FactionTemplate from dbc.db: {e}")


def get_faction_template(ft_id: int) -> dict | None:
    """Get a faction template by ID."""
    _load_faction_templates()
    return _ft_cache.get(ft_id)


# Player faction template IDs per race
RACE_FACTION_TEMPLATE = {
    1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 115, 8: 116,
}


def is_hostile(ft_a: dict, ft_b: dict) -> bool:
    """Check if faction template A is hostile to faction template B.
    Uses the CMaNGOS hostility algorithm."""
    if ft_b["faction"]:
        if ft_b["faction"] in ft_a["enemy_factions"]:
            return True
        if ft_b["faction"] in ft_a["friend_factions"]:
            return False
    return (ft_a["enemy_group"] & ft_b["faction_group"]) != 0


def is_hostile_to_player(creature_ft_id: int, player_race: int) -> bool:
    """Check if a creature's faction template is hostile to a player of given race."""
    _load_faction_templates()
    creature_ft = _ft_cache.get(creature_ft_id)
    player_ft_id = RACE_FACTION_TEMPLATE.get(player_race, 1)
    player_ft = _ft_cache.get(player_ft_id)
    if not creature_ft or not player_ft:
        return False
    return is_hostile(creature_ft, player_ft)


def is_attackable_by_player(creature_ft_id: int, player_race: int) -> bool:
    """Check if a player can attack a creature (right-click attack).
    This is broader than is_hostile — includes passive mobs like wolves
    that don't aggro but ARE attackable. Only truly friendly/allied units
    (same faction group, or explicitly friendly) are non-attackable."""
    _load_faction_templates()
    creature_ft = _ft_cache.get(creature_ft_id)
    player_ft_id = RACE_FACTION_TEMPLATE.get(player_race, 1)
    player_ft = _ft_cache.get(player_ft_id)
    if not creature_ft or not player_ft:
        return False
    # If explicitly hostile, always attackable
    if is_hostile(creature_ft, player_ft):
        return True
    # If explicitly friendly, NOT attackable
    if is_friendly(creature_ft, player_ft):
        return False
    # Otherwise (neutral) — attackable
    return True


def is_friendly(ft_a: dict, ft_b: dict) -> bool:
    """Check if faction template A is friendly to faction template B."""
    if ft_b["faction"]:
        if ft_b["faction"] in ft_a["enemy_factions"]:
            return False
        if ft_b["faction"] in ft_a["friend_factions"]:
            return True
    return (ft_a["friend_group"] & ft_b["faction_group"]) != 0
