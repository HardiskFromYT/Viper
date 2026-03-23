"""SQLite database — accounts, characters, inventory, quests.

Schema changes are handled through the MIGRATIONS list.  Every entry is:
    (version: int, description: str, up: str | callable(conn))

To add a new schema change while the server is running:
  1. Append an entry to MIGRATIONS below.
  2. Type  reload db  in the CLI  (or  reload all).
  The modules/db.py module calls run_migrations() on every load.
"""
import sqlite3
from srp6 import make_verifier, make_salt


# ── Migration definitions ──────────────────────────────────────────────────────
# Each entry: (version, description, SQL-string or callable(conn))
# NEVER edit or remove past entries — only append new ones.

MIGRATIONS = [
    (1, "create accounts table", """
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT UNIQUE NOT NULL COLLATE NOCASE,
            verifier    BLOB NOT NULL,
            salt        BLOB NOT NULL,
            session_key BLOB
        )
    """),
    (2, "create characters table", """
        CREATE TABLE IF NOT EXISTS characters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL,
            name        TEXT NOT NULL COLLATE NOCASE,
            race        INTEGER NOT NULL DEFAULT 1,
            class       INTEGER NOT NULL DEFAULT 1,
            gender      INTEGER NOT NULL DEFAULT 0,
            skin        INTEGER NOT NULL DEFAULT 0,
            face        INTEGER NOT NULL DEFAULT 0,
            hair_style  INTEGER NOT NULL DEFAULT 0,
            hair_color  INTEGER NOT NULL DEFAULT 0,
            facial      INTEGER NOT NULL DEFAULT 0,
            level       INTEGER NOT NULL DEFAULT 1,
            map         INTEGER NOT NULL DEFAULT 0,
            zone        INTEGER NOT NULL DEFAULT 12,
            pos_x       REAL NOT NULL DEFAULT -8949.95,
            pos_y       REAL NOT NULL DEFAULT -132.493,
            pos_z       REAL NOT NULL DEFAULT 83.5312,
            orientation REAL NOT NULL DEFAULT 0.0,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
    """),
    (3, "create inventory table", """
        CREATE TABLE IF NOT EXISTS inventory (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            char_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            count   INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(char_id) REFERENCES characters(id)
        )
    """),
    (4, "create quest_status table", """
        CREATE TABLE IF NOT EXISTS quest_status (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            char_id  INTEGER NOT NULL,
            quest_id INTEGER NOT NULL,
            status   TEXT NOT NULL DEFAULT 'accepted',
            progress TEXT NOT NULL DEFAULT '{}',
            UNIQUE(char_id, quest_id),
            FOREIGN KEY(char_id) REFERENCES characters(id)
        )
    """),
    (5, "add gm_level to accounts",
     lambda conn: _safe_add_column(conn, "accounts", "gm_level",
                                   "INTEGER NOT NULL DEFAULT 0")),
    (6, "auto-promote first account to GM 3 if no GMs exist",
     lambda conn: _auto_promote_gm(conn)),
]


def _auto_promote_gm(conn):
    """If no accounts have gm_level > 0, set the first account to gm_level=3."""
    gm_count = conn.execute("SELECT COUNT(*) FROM accounts WHERE gm_level > 0").fetchone()[0]
    if gm_count == 0:
        first = conn.execute("SELECT id, username FROM accounts ORDER BY id LIMIT 1").fetchone()
        if first:
            conn.execute("UPDATE accounts SET gm_level=3 WHERE id=?", (first[0],))
            conn.commit()


# ── Internal helpers ───────────────────────────────────────────────────────────

def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_migration_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _safe_add_column(conn, table: str, column: str, typedef: str):
    """Add a column only if it doesn't already exist (SQLite has no IF NOT EXISTS for ALTER)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
        conn.commit()


# ── Public migration API ───────────────────────────────────────────────────────

def run_migrations(db_path: str) -> list[tuple[int, str, str]]:
    """Apply all pending migrations. Returns list of (version, desc, status)."""
    conn = _conn(db_path)
    _ensure_migration_table(conn)

    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    results = []

    for version, description, up in MIGRATIONS:
        if version in applied:
            results.append((version, description, "already applied"))
            continue
        try:
            if callable(up):
                up(conn)
            else:
                conn.executescript(up)
                conn.commit()
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?,?)",
                (version, description),
            )
            conn.commit()
            results.append((version, description, "applied"))
        except Exception as e:
            results.append((version, description, f"ERROR: {e}"))

    conn.close()
    return results


def migration_status(db_path: str) -> list[tuple[int, str, str, str]]:
    """Return (version, description, status, applied_at) for all migrations."""
    conn = _conn(db_path)
    _ensure_migration_table(conn)
    applied = {
        row[0]: row[1]
        for row in conn.execute("SELECT version, applied_at FROM schema_migrations")
    }
    conn.close()
    rows = []
    for version, description, _ in MIGRATIONS:
        if version in applied:
            rows.append((version, description, "applied", applied[version]))
        else:
            rows.append((version, description, "PENDING", ""))
    return rows


def init_db(db_path: str):
    """Bootstrap: run all migrations (safe to call on existing DB)."""
    run_migrations(db_path)


# ── Accounts ──────────────────────────────────────────────────────────────────

def create_account(db_path: str, username: str, password: str):
    salt     = make_salt()
    verifier = make_verifier(username, password, salt)
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO accounts (username, verifier, salt) VALUES (?,?,?)",
            (username.upper(), verifier, salt),
        )
        conn.commit()
        print(f"[DB] Account '{username.upper()}' created.")
    except sqlite3.IntegrityError:
        print(f"[DB] Account '{username.upper()}' already exists.")
    finally:
        conn.close()


def delete_account(db_path: str, username: str):
    conn = _conn(db_path)
    conn.execute("DELETE FROM accounts WHERE username = ?", (username.upper(),))
    conn.commit()
    conn.close()


def get_account(db_path: str, username: str):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM accounts WHERE username = ?", (username.upper(),)
    ).fetchone()
    conn.close()
    return row


def get_all_accounts(db_path: str):
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT id, username, gm_level FROM accounts ORDER BY username"
    ).fetchall()
    conn.close()
    return rows


def set_session_key(db_path: str, username: str, session_key: bytes):
    conn = _conn(db_path)
    conn.execute("UPDATE accounts SET session_key=? WHERE username=?",
                 (session_key, username.upper()))
    conn.commit()
    conn.close()


def get_session_key(db_path: str, username: str) -> bytes | None:
    row = get_account(db_path, username)
    return bytes(row["session_key"]) if row and row["session_key"] else None


def set_gm_level(db_path: str, username: str, level: int):
    conn = _conn(db_path)
    conn.execute("UPDATE accounts SET gm_level=? WHERE username=?",
                 (level, username.upper()))
    conn.commit()
    conn.close()


def set_account_password(db_path: str, username: str, new_password: str):
    salt     = make_salt()
    verifier = make_verifier(username, new_password, salt)
    conn = _conn(db_path)
    conn.execute("UPDATE accounts SET verifier=?, salt=? WHERE username=?",
                 (verifier, salt, username.upper()))
    conn.commit()
    conn.close()


# ── Characters ────────────────────────────────────────────────────────────────

# Fallback starting positions — used when world.db is unavailable
_RACE_START_FALLBACK = {
    1: (0,  12, -8949.95,  -132.493, 83.5312,  0.0),     # Human - Northshire
    2: (1,  14,  -618.518, -4251.67, 38.718,   0.0),     # Orc - Valley of Trials
    3: (0,   1, -6240.32,   331.033, 382.758,  6.17),    # Dwarf - Coldridge Valley
    4: (1, 141, 10311.3,    832.463, 1326.41,  5.69),    # Night Elf - Shadowglen
    5: (0,  85,  1676.71,  1678.31,  121.67,   2.71),    # Undead - Deathknell
    6: (1, 215, -2917.58,  -257.98,  52.9968,  0.0),     # Tauren - Camp Narache
    7: (0,   1, -6240.32,   331.033, 382.758,  0.0),     # Gnome - Coldridge Valley
    8: (1,  14,  -618.518, -4251.67, 38.718,   0.0),     # Troll - Valley of Trials
}


def _get_start_position(race: int, cls: int):
    """Get starting position from world.db if available, else fallback."""
    import os
    world_db = os.path.join(os.path.dirname(__file__), "world.db")
    if os.path.exists(world_db):
        try:
            import sqlite3
            conn = sqlite3.connect(world_db)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT map, zone, position_x, position_y, position_z, orientation "
                "FROM playercreateinfo WHERE race=? AND class=?", (race, cls)
            ).fetchone()
            conn.close()
            if row:
                return (int(row["map"]), int(row["zone"]),
                        float(row["position_x"]), float(row["position_y"]),
                        float(row["position_z"]), float(row["orientation"]))
        except Exception:
            pass
    fb = _RACE_START_FALLBACK.get(race, (0, 12, -8949.95, -132.493, 83.5312, 0.0))
    return fb


def create_character(db_path: str, account_id: int, name: str, race: int,
                     cls: int, gender: int, skin: int, face: int,
                     hair_style: int, hair_color: int, facial: int) -> int:
    mp, zone, x, y, z, o = _get_start_position(race, cls)
    conn = _conn(db_path)
    cur = conn.execute(
        """INSERT INTO characters
           (account_id,name,race,class,gender,skin,face,hair_style,hair_color,
            facial,map,zone,pos_x,pos_y,pos_z,orientation)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, name, race, cls, gender, skin, face, hair_style, hair_color,
         facial, mp, zone, x, y, z, o),
    )
    conn.commit()
    char_id = cur.lastrowid
    conn.close()
    return char_id


def get_characters(db_path: str, account_id: int):
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM characters WHERE account_id=?", (account_id,)
    ).fetchall()
    conn.close()
    return rows


def get_character_by_guid(db_path: str, guid: int):
    conn = _conn(db_path)
    row = conn.execute("SELECT * FROM characters WHERE id=?", (guid,)).fetchone()
    conn.close()
    return row


def get_character_by_name(db_path: str, name: str):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM characters WHERE name=? COLLATE NOCASE", (name,)
    ).fetchone()
    conn.close()
    return row


def set_char_level(db_path: str, char_id: int, level: int):
    conn = _conn(db_path)
    conn.execute("UPDATE characters SET level=? WHERE id=?", (level, char_id))
    conn.commit()
    conn.close()


def update_char_position(db_path: str, char_id: int, map_id: int,
                         x: float, y: float, z: float, o: float):
    conn = _conn(db_path)
    conn.execute(
        "UPDATE characters SET map=?,pos_x=?,pos_y=?,pos_z=?,orientation=? WHERE id=?",
        (map_id, x, y, z, o, char_id),
    )
    conn.commit()
    conn.close()


def update_char_zone(db_path: str, char_id: int, zone_id: int):
    conn = _conn(db_path)
    conn.execute("UPDATE characters SET zone=? WHERE id=?", (zone_id, char_id))
    conn.commit()
    conn.close()


# ── Inventory ─────────────────────────────────────────────────────────────────

def get_inventory(db_path: str, char_id: int):
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM inventory WHERE char_id=? ORDER BY item_id", (char_id,)
    ).fetchall()
    conn.close()
    return rows


def add_inventory_item(db_path: str, char_id: int, item_id: int, count: int = 1):
    conn = _conn(db_path)
    existing = conn.execute(
        "SELECT id, count FROM inventory WHERE char_id=? AND item_id=?",
        (char_id, item_id)
    ).fetchone()
    if existing:
        conn.execute("UPDATE inventory SET count=? WHERE id=?",
                     (existing["count"] + count, existing["id"]))
    else:
        conn.execute("INSERT INTO inventory (char_id,item_id,count) VALUES (?,?,?)",
                     (char_id, item_id, count))
    conn.commit()
    conn.close()


def remove_inventory_item(db_path: str, char_id: int, item_id: int) -> bool:
    conn = _conn(db_path)
    existing = conn.execute(
        "SELECT id FROM inventory WHERE char_id=? AND item_id=?",
        (char_id, item_id)
    ).fetchone()
    if not existing:
        conn.close()
        return False
    conn.execute("DELETE FROM inventory WHERE id=?", (existing["id"],))
    conn.commit()
    conn.close()
    return True


# ── Quests ────────────────────────────────────────────────────────────────────

def get_quest_status(db_path: str, char_id: int):
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT * FROM quest_status WHERE char_id=?", (char_id,)
    ).fetchall()
    conn.close()
    return rows


def set_quest_status(db_path: str, char_id: int, quest_id: int,
                     status: str, progress: str = "{}"):
    conn = _conn(db_path)
    conn.execute(
        """INSERT INTO quest_status (char_id,quest_id,status,progress)
           VALUES (?,?,?,?)
           ON CONFLICT(char_id,quest_id) DO UPDATE
           SET status=excluded.status, progress=excluded.progress""",
        (char_id, quest_id, status, progress),
    )
    conn.commit()
    conn.close()
