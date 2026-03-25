#!/usr/bin/env python3
"""
extract_dbc.py — Extract DBC data from the WoW 1.12.1 client into dbc.db (SQLite).

Usage:
    python3 extract_dbc.py                          # auto-detect client
    python3 extract_dbc.py /path/to/WoW/Data        # explicit path
    python3 extract_dbc.py status                   # show what's extracted

This reads .dbc files from the client's MPQ archives and stores the parsed
data in dbc.db. Run once after setting up the client; re-run to update.

Requires: pip install mpyq
"""

import os
import sqlite3
import struct
import sys
import time

BASE   = os.path.dirname(os.path.abspath(__file__))
DBC_DB = os.path.join(BASE, "dbc.db")

# ── MPQ / DBC helpers ────────────────────────────────────────────────────────

def find_client_data(explicit_path=None):
    """Find the WoW 1.12 client Data directory."""
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates += [
        os.path.join(BASE, "Client", "SoloCraft 1.12.1", "Data"),
        os.path.join(BASE, "Client", "Data"),
        os.path.expanduser("~/Games/WoW 1.12/Data"),
    ]
    for p in candidates:
        if os.path.isdir(p) and any(f.endswith(".MPQ") for f in os.listdir(p)):
            return p
    return None


def read_dbc_from_mpq(data_path, dbc_name):
    """Read a DBC file from the client's MPQ archives."""
    import mpyq
    # DBC files live in dbc.MPQ, but can also be in patch.MPQ
    for mpq_name in ["dbc.MPQ", "patch.MPQ", "patch-2.MPQ"]:
        mpq_path = os.path.join(data_path, mpq_name)
        if not os.path.exists(mpq_path):
            continue
        try:
            archive = mpyq.MPQArchive(mpq_path)
            key = f"DBFilesClient\\{dbc_name}".encode()
            data = archive.read_file(key)
            if data:
                return data
        except Exception:
            continue
    return None


def parse_dbc_header(raw):
    """Parse DBC header. Returns (n_records, n_fields, record_size, string_size, data_offset)."""
    if raw[:4] != b"WDBC":
        raise ValueError("Not a valid DBC file")
    n_records, n_fields, record_size, string_size = struct.unpack_from("<4I", raw, 4)
    return n_records, n_fields, record_size, string_size, 20


def get_dbc_string(raw, string_block_offset, str_offset):
    """Read a null-terminated string from the DBC string block."""
    start = string_block_offset + str_offset
    end = raw.index(b"\x00", start)
    return raw[start:end].decode("utf-8", errors="replace")


# ── CharStartOutfit.dbc ─────────────────────────────────────────────────────
# Layout (vanilla 1.12.1, 152 bytes per record):
#   uint32  ID                    (offset 0)
#   uint8   Race,Class,Gender,_   (offset 4, packed as 4 bytes)
#   int32   ItemID[12]            (offset 8)
#   int32   DisplayID[12]         (offset 56)
#   int32   InvType[12]           (offset 104)

def extract_char_start_outfit(raw, db):
    """Parse CharStartOutfit.dbc and insert into dbc.db."""
    n_records, _, record_size, _, data_off = parse_dbc_header(raw)

    db.execute("DROP TABLE IF EXISTS char_start_outfit")
    db.execute("""
        CREATE TABLE char_start_outfit (
            race        INTEGER NOT NULL,
            class       INTEGER NOT NULL,
            gender      INTEGER NOT NULL,
            slot_index  INTEGER NOT NULL,
            item_id     INTEGER NOT NULL,
            display_id  INTEGER NOT NULL,
            inventory_type INTEGER NOT NULL,
            PRIMARY KEY (race, class, gender, slot_index)
        )
    """)

    rows = []
    for i in range(n_records):
        rec = raw[data_off + i * record_size : data_off + (i + 1) * record_size]
        race, cls, gender, _ = struct.unpack_from("<4B", rec, 4)
        items    = struct.unpack_from("<12i", rec, 8)
        displays = struct.unpack_from("<12i", rec, 56)
        inv_types = struct.unpack_from("<12i", rec, 104)

        for j, (item_id, disp, inv) in enumerate(zip(items, displays, inv_types)):
            if item_id > 0:
                rows.append((race, cls, gender, j, item_id, disp, inv))

    db.executemany(
        "INSERT INTO char_start_outfit VALUES (?,?,?,?,?,?,?)", rows
    )
    # Count unique race/class combos
    unique = len({(r[0], r[1]) for r in rows})
    return n_records, unique, len(rows)


# ── AreaTable.dbc (zone names) ───────────────────────────────────────────────
# Layout: uint32 ID, uint32 MapID, uint32 ParentAreaID, ... str_offset Name, ...
# Record size varies; Name is at a known field offset.

def extract_area_table(raw, db):
    """Parse AreaTable.dbc — zone ID to name mapping."""
    n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
    string_block_off = data_off + n_records * record_size

    db.execute("DROP TABLE IF EXISTS area_table")
    db.execute("""
        CREATE TABLE area_table (
            id          INTEGER PRIMARY KEY,
            map_id      INTEGER NOT NULL,
            parent_id   INTEGER NOT NULL,
            name        TEXT NOT NULL
        )
    """)

    # In vanilla 1.12.1, AreaTable has many fields. The name string offset
    # is at field index 11 (byte offset 44 in the record).
    rows = []
    for i in range(n_records):
        off = data_off + i * record_size
        area_id = struct.unpack_from("<I", raw, off)[0]
        map_id  = struct.unpack_from("<I", raw, off + 4)[0]
        parent  = struct.unpack_from("<I", raw, off + 8)[0]
        name_off = struct.unpack_from("<I", raw, off + 44)[0]
        try:
            name = get_dbc_string(raw, string_block_off, name_off)
        except (ValueError, IndexError):
            name = f"Zone {area_id}"
        rows.append((area_id, map_id, parent, name))

    db.executemany("INSERT INTO area_table VALUES (?,?,?,?)", rows)
    return n_records, len(rows)


# ── ChrRaces.dbc (race info) ────────────────────────────────────────────────

def extract_chr_races(raw, db):
    """Parse ChrRaces.dbc — race ID, name, faction."""
    n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
    string_block_off = data_off + n_records * record_size

    db.execute("DROP TABLE IF EXISTS chr_races")
    db.execute("""
        CREATE TABLE chr_races (
            id              INTEGER PRIMARY KEY,
            faction_id      INTEGER,
            model_male      INTEGER,
            model_female    INTEGER,
            name            TEXT
        )
    """)

    # ChrRaces.dbc vanilla layout (29 fields, 116 bytes):
    #   field[0]=ID, field[2]=FactionID, field[4]=MaleModel, field[5]=FemaleModel
    #   field[15]=Name (string offset at byte 60)
    rows = []
    for i in range(n_records):
        off = data_off + i * record_size
        race_id    = struct.unpack_from("<I", raw, off)[0]
        faction_id = struct.unpack_from("<I", raw, off + 8)[0]
        model_m    = struct.unpack_from("<I", raw, off + 16)[0]
        model_f    = struct.unpack_from("<I", raw, off + 20)[0]
        name_off   = struct.unpack_from("<I", raw, off + 60)[0]
        try:
            name = get_dbc_string(raw, string_block_off, name_off)
        except (ValueError, IndexError):
            name = f"Race {race_id}"
        rows.append((race_id, faction_id, model_m, model_f, name))

    db.executemany("INSERT INTO chr_races VALUES (?,?,?,?,?)", rows)
    return n_records, len(rows)


# ── ChrClasses.dbc ──────────────────────────────────────────────────────────

def extract_chr_classes(raw, db):
    """Parse ChrClasses.dbc — class ID and name."""
    n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
    string_block_off = data_off + n_records * record_size

    db.execute("DROP TABLE IF EXISTS chr_classes")
    db.execute("""
        CREATE TABLE chr_classes (
            id          INTEGER PRIMARY KEY,
            power_type  INTEGER,
            name        TEXT
        )
    """)

    # ChrClasses.dbc vanilla layout (16 fields, 64 bytes):
    #   field[0]=ID, field[2]=PowerType, field[5]=Name (string offset at byte 20)
    rows = []
    for i in range(n_records):
        off = data_off + i * record_size
        class_id   = struct.unpack_from("<I", raw, off)[0]
        power_type = struct.unpack_from("<I", raw, off + 8)[0]
        name_off   = struct.unpack_from("<I", raw, off + 20)[0]
        try:
            name = get_dbc_string(raw, string_block_off, name_off)
        except (ValueError, IndexError):
            name = f"Class {class_id}"
        rows.append((class_id, power_type, name))

    db.executemany("INSERT INTO chr_classes VALUES (?,?,?)", rows)
    return n_records, len(rows)


# ── Emotes.dbc ───────────────────────────────────────────────────────────────

def extract_emotes_text(raw, db):
    """Parse EmotesText.dbc — emote ID to slash command mapping."""
    n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
    string_block_off = data_off + n_records * record_size

    db.execute("DROP TABLE IF EXISTS emotes_text")
    db.execute("""
        CREATE TABLE emotes_text (
            id      INTEGER PRIMARY KEY,
            name    TEXT
        )
    """)

    rows = []
    for i in range(n_records):
        off = data_off + i * record_size
        emote_id = struct.unpack_from("<I", raw, off)[0]
        name_off = struct.unpack_from("<I", raw, off + 4)[0]
        try:
            name = get_dbc_string(raw, string_block_off, name_off)
        except (ValueError, IndexError):
            name = ""
        if name:
            rows.append((emote_id, name))

    db.executemany("INSERT INTO emotes_text VALUES (?,?)", rows)
    return n_records, len(rows)


# ── Main ─────────────────────────────────────────────────────────────────────

DBC_EXTRACTORS = {
    "CharStartOutfit.dbc": ("CharStartOutfit", extract_char_start_outfit),
    "AreaTable.dbc":       ("AreaTable",       extract_area_table),
    "ChrRaces.dbc":        ("ChrRaces",        extract_chr_races),
    "ChrClasses.dbc":      ("ChrClasses",      extract_chr_classes),
    "EmotesText.dbc":      ("EmotesText",      extract_emotes_text),
}


def show_status():
    """Show what's currently in dbc.db."""
    if not os.path.exists(DBC_DB):
        print("dbc.db does not exist. Run: python3 extract_dbc.py [/path/to/WoW/Data]")
        return
    db = sqlite3.connect(DBC_DB)
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    if not tables:
        print("dbc.db is empty.")
        return
    print(f"dbc.db tables ({len(tables)}):")
    for t in tables:
        count = db.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"  {t:30s} {count:6d} rows")
    db.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
        return

    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    data_path = find_client_data(explicit)
    if not data_path:
        print("ERROR: WoW 1.12 client Data directory not found.")
        print("Usage: python3 extract_dbc.py /path/to/WoW/Data")
        sys.exit(1)

    print(f"Client Data: {data_path}")

    try:
        import mpyq
    except ImportError:
        print("ERROR: mpyq not installed. Run: pip install mpyq")
        sys.exit(1)

    db = sqlite3.connect(DBC_DB)
    db.execute("PRAGMA journal_mode=WAL")

    # Track metadata
    db.execute("""
        CREATE TABLE IF NOT EXISTS _dbc_meta (
            name        TEXT PRIMARY KEY,
            extracted   TEXT,
            records     INTEGER,
            rows        INTEGER
        )
    """)

    total_extracted = 0
    for dbc_file, (label, extractor) in DBC_EXTRACTORS.items():
        print(f"  Extracting {dbc_file}...", end=" ", flush=True)
        raw = read_dbc_from_mpq(data_path, dbc_file)
        if not raw:
            print("NOT FOUND (skipped)")
            continue

        try:
            result = extractor(raw, db)
            n_records = result[0]
            n_rows = result[-1]
            db.execute(
                "INSERT OR REPLACE INTO _dbc_meta VALUES (?,datetime('now'),?,?)",
                (label, n_records, n_rows)
            )
            db.commit()
            print(f"OK ({n_records} records, {n_rows} rows)")
            total_extracted += 1
        except Exception as e:
            print(f"ERROR: {e}")

    db.close()
    print(f"\nDone! Extracted {total_extracted}/{len(DBC_EXTRACTORS)} DBC files into {DBC_DB}")


if __name__ == "__main__":
    main()
