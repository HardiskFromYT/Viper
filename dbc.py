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
