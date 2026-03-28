#!/usr/bin/env python3
"""
extract_dbc.py — Extract DBC data from the WoW 1.12.1 client MPQ into dbc.db (SQLite).

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
import string

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


def open_mpq_archives(data_path):
    """Open all relevant MPQ archives and return them in priority order."""
    import mpyq
    archives = []
    for mpq_name in ["patch-2.MPQ", "patch.MPQ", "dbc.MPQ"]:
        mpq_path = os.path.join(data_path, mpq_name)
        if os.path.exists(mpq_path):
            try:
                archives.append(mpyq.MPQArchive(mpq_path))
            except Exception:
                pass
    return archives


def read_dbc_from_mpq(data_path, dbc_name, archives=None):
    """Read a DBC file from the client's MPQ archives."""
    import mpyq
    if archives is None:
        archives = open_mpq_archives(data_path)
    key = f"DBFilesClient\\{dbc_name}"
    for archive in archives:
        try:
            data = archive.read_file(key)
            if data:
                return data
        except Exception:
            continue
    return None


def list_dbc_files_in_mpq(data_path):
    """List all DBC files available in the MPQ archives."""
    import mpyq
    dbc_files = set()
    for mpq_name in ["dbc.MPQ", "patch.MPQ", "patch-2.MPQ"]:
        mpq_path = os.path.join(data_path, mpq_name)
        if not os.path.exists(mpq_path):
            continue
        try:
            archive = mpyq.MPQArchive(mpq_path)
            if hasattr(archive, 'files'):
                for f in archive.files:
                    if isinstance(f, bytes):
                        f = f.decode("utf-8", errors="replace")
                    if f.lower().endswith(".dbc"):
                        # Extract just the filename
                        name = f.split("\\")[-1]
                        dbc_files.add(name)
        except Exception:
            pass
    return sorted(dbc_files)


def parse_dbc_header(raw):
    """Parse DBC header. Returns (n_records, n_fields, record_size, string_size, data_offset)."""
    if raw[:4] != b"WDBC":
        raise ValueError("Not a valid DBC file")
    n_records, n_fields, record_size, string_size = struct.unpack_from("<4I", raw, 4)
    return n_records, n_fields, record_size, string_size, 20


def get_dbc_string(raw, string_block_offset, str_offset):
    """Read a null-terminated string from the DBC string block."""
    start = string_block_offset + str_offset
    if start >= len(raw):
        return ""
    end = raw.index(b"\x00", start)
    return raw[start:end].decode("utf-8", errors="replace")


# ── Generic DBC extractor ─────────────────────────────────────────────────────

def detect_string_fields(raw, n_records, n_fields, record_size, data_off, string_block_off, string_size):
    """Detect which fields are string offsets by checking if values point to valid strings."""
    if n_records == 0 or string_size <= 1:
        return set()

    printable = set(string.printable.encode("ascii"))
    string_fields = set()
    file_len = len(raw)

    for field_idx in range(n_fields):
        byte_offset = field_idx * 4
        if byte_offset + 4 > record_size:
            break

        valid_string = 0
        nonzero = 0
        sample_size = min(n_records, 500)  # sample for performance

        for i in range(sample_size):
            rec_off = data_off + i * record_size + byte_offset
            val = struct.unpack_from("<I", raw, rec_off)[0]
            if val == 0:
                continue
            nonzero += 1
            # Check if it looks like a valid string offset
            if val < string_size:
                abs_off = string_block_off + val
                if abs_off < file_len:
                    # Check first few bytes are printable or common control chars
                    try:
                        end = raw.index(b"\x00", abs_off)
                        s = raw[abs_off:end]
                        if len(s) > 0 and len(s) < 500:
                            # Check if mostly printable
                            good = sum(1 for b in s if b in printable)
                            if good >= len(s) * 0.8:
                                valid_string += 1
                    except (ValueError, IndexError):
                        pass

        # If >50% of non-zero values are valid string offsets, treat as string
        if nonzero > 0 and valid_string / nonzero > 0.5:
            string_fields.add(field_idx)

    return string_fields


def generic_extract(raw, db, table_name):
    """Generic extractor: auto-detect fields and dump everything."""
    n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
    string_block_off = data_off + n_records * record_size

    # Handle DBCs where record_size != n_fields * 4 (e.g. CharBaseInfo with byte-packed fields)
    if n_fields > 0 and record_size != n_fields * 4:
        actual_field_size = record_size // n_fields
        fmt_map = {1: "<B", 2: "<H", 4: "<I"}
        fmt = fmt_map.get(actual_field_size)
        if fmt is None:
            # Unsupported field size — skip
            db.execute(f"DROP TABLE IF EXISTS [{table_name}]")
            db.execute(f"CREATE TABLE [{table_name}] (field_0 INTEGER)")
            return n_records, 0
        columns = [f"field_{i} INTEGER" for i in range(n_fields)]
        db.execute(f"DROP TABLE IF EXISTS [{table_name}]")
        db.execute(f"CREATE TABLE [{table_name}] ({', '.join(columns)})")
        placeholders = ", ".join(["?"] * n_fields)
        rows = []
        for i in range(n_records):
            rec_off = data_off + i * record_size
            row = []
            for f in range(n_fields):
                row.append(struct.unpack_from(fmt, raw, rec_off + f * actual_field_size)[0])
            rows.append(tuple(row))
        db.executemany(f"INSERT INTO [{table_name}] VALUES ({placeholders})", rows)
        return n_records, len(rows)

    string_fields = detect_string_fields(
        raw, n_records, n_fields, record_size, data_off, string_block_off, string_size
    )

    # Create table
    columns = []
    for i in range(n_fields):
        col_type = "TEXT" if i in string_fields else "INTEGER"
        columns.append(f"field_{i} {col_type}")

    db.execute(f"DROP TABLE IF EXISTS [{table_name}]")
    col_defs = ", ".join(columns)
    db.execute(f"CREATE TABLE [{table_name}] ({col_defs})")

    # Extract records
    placeholders = ", ".join(["?"] * n_fields)
    rows = []
    for i in range(n_records):
        rec_off = data_off + i * record_size
        row = []
        for f in range(n_fields):
            val = struct.unpack_from("<I", raw, rec_off + f * 4)[0]
            if f in string_fields:
                try:
                    row.append(get_dbc_string(raw, string_block_off, val) if val else "")
                except (ValueError, IndexError):
                    row.append("")
            else:
                row.append(val)
        rows.append(tuple(row))

    db.executemany(f"INSERT INTO [{table_name}] VALUES ({placeholders})", rows)
    return n_records, len(rows)


# ── Specific extractor helper ─────────────────────────────────────────────────

def _make_specific_extractor(table_name, field_defs):
    """
    Build an extractor function from a list of (col_name, col_type) pairs.
    col_type is "int", "str", "float", or "str_loc" (8 locale strings + flags).

    Each "int" consumes 1 uint32 field.
    Each "str" consumes 1 uint32 field (string offset).
    Each "float" consumes 1 field (IEEE float).
    Each "str_loc" consumes 9 fields (8 locale offsets + 1 flags), only enUS (first) is kept.
    """
    def extractor(raw, db):
        n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
        string_block_off = data_off + n_records * record_size

        # Build column list and field consumption plan
        col_names = []
        # plan: list of (col_index_or_None, field_type, n_fields_consumed)
        plan = []
        for col_name, col_type in field_defs:
            if col_type == "str_loc":
                # 8 locale strings + 1 flags = 9 fields, keep only enUS (first)
                col_names.append(col_name)
                plan.append((len(col_names) - 1, "str", 1))
                for _ in range(8):  # 7 other locales + 1 flags
                    plan.append((None, "skip", 1))
            elif col_type == "skip":
                plan.append((None, "skip", 1))
            else:
                col_names.append(col_name)
                plan.append((len(col_names) - 1, col_type, 1))

        # SQL types
        sql_types = []
        for col_name, col_type in field_defs:
            if col_type in ("str", "str_loc"):
                sql_types.append("TEXT")
            elif col_type == "float":
                sql_types.append("REAL")
            elif col_type == "skip":
                continue
            else:
                sql_types.append("INTEGER")

        db.execute(f"DROP TABLE IF EXISTS [{table_name}]")
        col_defs_sql = ", ".join(
            f"[{col_names[i]}] {sql_types[i]}" for i in range(len(col_names))
        )
        db.execute(f"CREATE TABLE [{table_name}] ({col_defs_sql})")

        placeholders = ", ".join(["?"] * len(col_names))
        rows = []
        for i in range(n_records):
            rec_off = data_off + i * record_size
            row = [None] * len(col_names)
            field_idx = 0
            for col_idx, ftype, n_consumed in plan:
                if field_idx >= n_fields:
                    break
                byte_off = rec_off + field_idx * 4
                if ftype == "skip":
                    field_idx += 1
                    continue
                elif ftype == "str":
                    val = struct.unpack_from("<I", raw, byte_off)[0]
                    try:
                        row[col_idx] = get_dbc_string(raw, string_block_off, val) if val else ""
                    except (ValueError, IndexError):
                        row[col_idx] = ""
                elif ftype == "float":
                    row[col_idx] = struct.unpack_from("<f", raw, byte_off)[0]
                else:  # int
                    row[col_idx] = struct.unpack_from("<I", raw, byte_off)[0]
                field_idx += 1
            rows.append(tuple(row))

        db.executemany(f"INSERT INTO [{table_name}] VALUES ({placeholders})", rows)
        return n_records, len(rows)

    return extractor


def _int(name):
    return (name, "int")

def _str(name):
    return (name, "str")

def _float(name):
    return (name, "float")

def _str_loc(name):
    return (name, "str_loc")

def _skip():
    return ("_skip", "skip")

def _ints(prefix, count):
    """Generate count int fields: prefix1, prefix2, ..."""
    return [_int(f"{prefix}{i+1}") for i in range(count)]

def _ints0(prefix, count):
    """Generate count int fields: prefix0, prefix1, ..."""
    return [_int(f"{prefix}{i}") for i in range(count)]


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


# ── FactionTemplate.dbc ────────────────────────────────────────────────────

def extract_faction_template(raw, db):
    """Parse FactionTemplate.dbc and insert into dbc.db."""
    n_records, n_fields, record_size, _, data_off = parse_dbc_header(raw)

    db.execute("DROP TABLE IF EXISTS faction_template")
    db.execute("""
        CREATE TABLE faction_template (
            id              INTEGER PRIMARY KEY,
            faction         INTEGER NOT NULL,
            flags           INTEGER NOT NULL,
            faction_group   INTEGER NOT NULL,
            friend_group    INTEGER NOT NULL,
            enemy_group     INTEGER NOT NULL,
            enemy_faction1  INTEGER NOT NULL DEFAULT 0,
            enemy_faction2  INTEGER NOT NULL DEFAULT 0,
            enemy_faction3  INTEGER NOT NULL DEFAULT 0,
            enemy_faction4  INTEGER NOT NULL DEFAULT 0,
            friend_faction1 INTEGER NOT NULL DEFAULT 0,
            friend_faction2 INTEGER NOT NULL DEFAULT 0,
            friend_faction3 INTEGER NOT NULL DEFAULT 0,
            friend_faction4 INTEGER NOT NULL DEFAULT 0
        )
    """)

    rows = []
    for i in range(n_records):
        off = data_off + i * record_size
        fields = struct.unpack_from("<14I", raw, off)
        rows.append(fields)

    db.executemany(
        "INSERT INTO faction_template VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return n_records, len(rows)


# ── Spell.dbc ────────────────────────────────────────────────────────────────
# Vanilla 1.12 base: 162 fields (112 pre + 4x9 strings + 14 post)
# SoloCraft/patched: 173 fields (120 pre + 4x9 strings + 17 post)
# We auto-detect based on n_fields.

def extract_spell(raw, db):
    """Parse Spell.dbc — auto-detects 162 vs 173 field layout."""
    n_records, n_fields, record_size, string_size, data_off = parse_dbc_header(raw)
    string_block_off = data_off + n_records * record_size

    db.execute("DROP TABLE IF EXISTS spell")

    # Detect layout based on field count: 4 locale string blocks always take 36 fields
    # 162 fields: 112 pre-string + 36 string + 14 post-string
    # 173 fields: 120 pre-string + 36 string + 17 post-string
    n_string_fields = 36  # 4 blocks x 9 fields each
    n_pre = n_fields - n_string_fields - (n_fields - n_string_fields - 112)
    # Simpler: detect by finding SpellName. In all layouts the string block start
    # is at (n_fields - 36 - n_post) where we know n_post from the field count.
    if n_fields == 162:
        name_field_start = 112  # vanilla base
    elif n_fields == 173:
        name_field_start = 120  # SoloCraft / TBC-backport patch
    else:
        # Auto-detect: scan for the name string block pattern (enUS string, then 7 zeros, then flags)
        name_field_start = None
        for candidate in range(100, min(n_fields - 36, 140)):
            # Check if field[candidate] looks like a string and field[candidate+1..+7] are 0 for most records
            hits = 0
            sample = min(n_records, 50)
            for ri in range(sample):
                rec_off = data_off + ri * record_size
                v0 = struct.unpack_from("<I", raw, rec_off + candidate * 4)[0]
                if v0 == 0:
                    continue
                if v0 >= string_size:
                    continue
                # Check locales 1-7 are 0
                all_zero = True
                for loc in range(1, 8):
                    vl = struct.unpack_from("<I", raw, rec_off + (candidate + loc) * 4)[0]
                    if vl != 0:
                        all_zero = False
                        break
                if all_zero:
                    hits += 1
            if hits > sample * 0.3:
                name_field_start = candidate
                break
        if name_field_start is None:
            name_field_start = 112  # fallback

    n_post = n_fields - name_field_start - 36

    # Column names for pre-string fields (named for the common subset, extras get generic names)
    base_pre_names = [
        "ID", "School", "Category", "castUI", "Dispel", "Mechanic",
        "Attributes", "AttributesEx", "AttributesEx2", "AttributesEx3",
        "AttributesEx4",
        "Stances", "StancesNot",
        "Targets", "TargetCreatureType", "RequiresSpellFocus",
        "CasterAuraState", "TargetAuraState",
        "CastingTimeIndex", "RecoveryTime", "CategoryRecoveryTime",
        "InterruptFlags", "AuraInterruptFlags", "ChannelInterruptFlags",
        "ProcFlags", "ProcChance", "ProcCharges",
        "MaxLevel", "BaseLevel", "SpellLevel",
        "DurationIndex", "PowerType", "ManaCost", "ManaCostPerlevel",
        "ManaPerSecond", "ManaPerSecondPerLevel",
        "RangeIndex", "Speed", "ModalNextSpell", "StackAmount",
        "Totem1", "Totem2",
        "Reagent1", "Reagent2", "Reagent3", "Reagent4",
        "Reagent5", "Reagent6", "Reagent7", "Reagent8",
        "ReagentCount1", "ReagentCount2", "ReagentCount3", "ReagentCount4",
        "ReagentCount5", "ReagentCount6", "ReagentCount7", "ReagentCount8",
        "EquippedItemClass", "EquippedItemSubClassMask",
        "EquippedItemInventoryTypeMask",
        "Effect1", "Effect2", "Effect3",
        "EffectDieSides1", "EffectDieSides2", "EffectDieSides3",
        "EffectBaseDice1", "EffectBaseDice2", "EffectBaseDice3",
        "EffectDicePerLevel1", "EffectDicePerLevel2", "EffectDicePerLevel3",
        "EffectRealPointsPerLevel1", "EffectRealPointsPerLevel2",
        "EffectRealPointsPerLevel3",
        "EffectBasePoints1", "EffectBasePoints2", "EffectBasePoints3",
        "EffectMechanic1", "EffectMechanic2", "EffectMechanic3",
        "EffectImplicitTargetA1", "EffectImplicitTargetA2",
        "EffectImplicitTargetA3",
        "EffectImplicitTargetB1", "EffectImplicitTargetB2",
        "EffectImplicitTargetB3",
        "EffectRadiusIndex1", "EffectRadiusIndex2", "EffectRadiusIndex3",
        "EffectApplyAuraName1", "EffectApplyAuraName2",
        "EffectApplyAuraName3",
        "EffectAmplitude1", "EffectAmplitude2", "EffectAmplitude3",
        "EffectMultipleValue1", "EffectMultipleValue2",
        "EffectMultipleValue3",
        "EffectChainTarget1", "EffectChainTarget2", "EffectChainTarget3",
        "EffectItemType1", "EffectItemType2", "EffectItemType3",
        "EffectMiscValue1", "EffectMiscValue2", "EffectMiscValue3",
        "EffectTriggerSpell1", "EffectTriggerSpell2", "EffectTriggerSpell3",
    ]
    # Pad or trim to match actual pre-string count
    pre_string_names = base_pre_names[:name_field_start]
    while len(pre_string_names) < name_field_start:
        pre_string_names.append(f"field_{len(pre_string_names)}")

    string_names = ["SpellName", "Rank", "Description", "ToolTip"]

    # Post-string columns
    base_post_names = [
        "EffectPointsPerComboPoint1", "EffectPointsPerComboPoint2",
        "EffectPointsPerComboPoint3",
        "SpellVisual1", "SpellVisual2",
        "SpellIconID", "ActiveIconID",
        "SpellPriority",
        "ManaCostPercentage", "StartRecoveryCategory", "StartRecoveryTime",
        "MaxTargetLevel", "SpellFamilyName", "SpellFamilyFlags",
    ]
    post_string_names = base_post_names[:n_post]
    while len(post_string_names) < n_post:
        post_string_names.append(f"post_field_{len(post_string_names)}")

    # All column names in output order
    col_names = pre_string_names + string_names + post_string_names

    # Float fields
    float_col_names = {
        "Speed",
        "EffectRealPointsPerLevel1", "EffectRealPointsPerLevel2", "EffectRealPointsPerLevel3",
        "EffectMultipleValue1", "EffectMultipleValue2", "EffectMultipleValue3",
        "EffectPointsPerComboPoint1", "EffectPointsPerComboPoint2", "EffectPointsPerComboPoint3",
    }

    col_defs_sql = []
    for c in col_names:
        if c in string_names:
            col_defs_sql.append(f"[{c}] TEXT")
        elif c in float_col_names:
            col_defs_sql.append(f"[{c}] REAL")
        else:
            col_defs_sql.append(f"[{c}] INTEGER")

    db.execute(f"CREATE TABLE spell ({', '.join(col_defs_sql)})")

    placeholders = ", ".join(["?"] * len(col_names))
    rows = []

    post_field_start = name_field_start + 36

    for i in range(n_records):
        rec_off = data_off + i * record_size
        row = []

        # Pre-string integer/float fields
        for f in range(name_field_start):
            cname = pre_string_names[f]
            byte_off = rec_off + f * 4
            if cname in float_col_names:
                row.append(struct.unpack_from("<f", raw, byte_off)[0])
            else:
                row.append(struct.unpack_from("<I", raw, byte_off)[0])

        # 4 locale string blocks (9 fields each, keep enUS = first)
        for s in range(4):
            base_field = name_field_start + s * 9
            str_off = struct.unpack_from("<I", raw, rec_off + base_field * 4)[0]
            try:
                row.append(get_dbc_string(raw, string_block_off, str_off) if str_off else "")
            except (ValueError, IndexError):
                row.append("")

        # Post-string trailing integer/float fields
        for f in range(post_field_start, n_fields):
            cname = post_string_names[f - post_field_start] if (f - post_field_start) < len(post_string_names) else f"post_{f}"
            byte_off = rec_off + f * 4
            if cname in float_col_names:
                row.append(struct.unpack_from("<f", raw, byte_off)[0])
            else:
                row.append(struct.unpack_from("<I", raw, byte_off)[0])

        rows.append(tuple(row))

    db.executemany(f"INSERT INTO spell VALUES ({placeholders})", rows)
    return n_records, len(rows)


# ── Specific extractors built with the helper ────────────────────────────────

# SkillLine.dbc
extract_skill_line = _make_specific_extractor("skill_line", [
    _int("ID"), _int("CategoryID"), _int("SkillCostsID"),
    _str_loc("Name"), _str_loc("Description"),
    _int("SpellIconID"),
])

# SkillLineAbility.dbc
extract_skill_line_ability = _make_specific_extractor("skill_line_ability", [
    _int("ID"), _int("SkillLine"), _int("SpellID"),
    _int("RaceMask"), _int("ClassMask"),
    _int("ExcludeRace"), _int("ExcludeClass"),
    _int("MinSkillLineRank"), _int("SupercededBySpell"),
    _int("AcquireMethod"),
    _int("TrivialSkillLineRankHigh"), _int("TrivialSkillLineRankLow"),
    _int("NumSkillUps"), _int("UniqueBit"),
])

# Talent.dbc
extract_talent = _make_specific_extractor("talent", [
    _int("ID"), _int("TalentTab"), _int("Row"), _int("Column"),
    *_ints("RankID", 5),
    *_ints("ReqTalent", 3),
    *_ints("ReqTalentRank", 3),
    _int("Flags"), _int("ReqSpell"),
])

# TalentTab.dbc
extract_talent_tab = _make_specific_extractor("talent_tab", [
    _int("ID"), _str_loc("Name"),
    _int("SpellIconID"), _int("RaceMask"), _int("ClassMask"),
    _int("OrderIndex"), _str("BackgroundFile"),
])

# TaxiNodes.dbc
extract_taxi_nodes = _make_specific_extractor("taxi_nodes", [
    _int("ID"), _int("MapID"),
    _float("X"), _float("Y"), _float("Z"),
    _str_loc("Name"),
    _int("MountCreatureID_A"), _int("MountCreatureID_H"),
])

# TaxiPath.dbc
extract_taxi_path = _make_specific_extractor("taxi_path", [
    _int("ID"), _int("FromNode"), _int("ToNode"), _int("Cost"),
])

# TaxiPathNode.dbc
extract_taxi_path_node = _make_specific_extractor("taxi_path_node", [
    _int("ID"), _int("PathID"), _int("NodeIndex"), _int("MapID"),
    _float("X"), _float("Y"), _float("Z"),
    _int("Flags"), _int("Delay"),
    _int("ArrivalEventID"), _int("DepartureEventID"),
])

# Map.dbc — vanilla 1.12 has: ID, InternalName(str), MapType, IsBattleground, Name(str_loc), ...
# Total fields vary; we grab the important ones and skip the rest
extract_map = _make_specific_extractor("map", [
    _int("ID"), _str("InternalName"), _int("MapType"), _int("IsBattleground"),
    _str_loc("Name"),
])

# SpellCastTimes.dbc
extract_spell_cast_times = _make_specific_extractor("spell_cast_times", [
    _int("ID"), _int("CastTime"), _int("CastTimePerLevel"), _int("MinCastTime"),
])

# SpellDuration.dbc
extract_spell_duration = _make_specific_extractor("spell_duration", [
    _int("ID"), _int("Duration"), _int("DurationPerLevel"), _int("MaxDuration"),
])

# SpellRange.dbc
extract_spell_range = _make_specific_extractor("spell_range", [
    _int("ID"), _float("RangeMin"), _float("RangeMax"), _int("Flags"),
    _str_loc("Name"), _str_loc("ShortName"),
])

# SpellRadius.dbc
extract_spell_radius = _make_specific_extractor("spell_radius", [
    _int("ID"), _float("Radius"), _float("RadiusPerLevel"), _float("RadiusMax"),
])

# Faction.dbc
extract_faction = _make_specific_extractor("faction", [
    _int("ID"), _int("ReputationListID"),
    *_ints("ReputationBase", 4),
    *_ints("ReputationFlags", 4),
    _int("ParentFactionID"),
    _str_loc("Name"), _str_loc("Description"),
])

# CreatureFamily.dbc
extract_creature_family = _make_specific_extractor("creature_family", [
    _int("ID"),
    _float("MinScale"), _int("MinScaleLevel"),
    _float("MaxScale"), _int("MaxScaleLevel"),
    _int("SkillLine1"), _int("SkillLine2"),
    _int("PetFoodMask"), _int("PetTalentType"), _int("CategoryEnumID"),
    _str_loc("Name"), _str("IconFile"),
])

# CreatureType.dbc
extract_creature_type = _make_specific_extractor("creature_type", [
    _int("ID"), _str_loc("Name"), _int("Flags"),
])

# ItemClass.dbc
extract_item_class = _make_specific_extractor("item_class", [
    _int("ID"), _int("SubClassMapID"), _int("Flags"),
    _str_loc("Name"),
])

# ItemRandomProperties.dbc
extract_item_random_properties = _make_specific_extractor("item_random_properties", [
    _int("ID"), _str("InternalName"),
    *_ints("Enchantment", 5),
    _str_loc("Name"),
])

# ItemSet.dbc
extract_item_set = _make_specific_extractor("item_set", [
    _int("ID"), _str_loc("Name"),
    *_ints("ItemID", 17),
    *_ints("SetSpellID", 8), *_ints("SetThreshold", 8),
    _int("RequiredSkill"), _int("RequiredSkillRank"),
])

# Lock.dbc
extract_lock = _make_specific_extractor("lock", [
    _int("ID"),
    *_ints("Type", 5), *_ints("Index", 5),
    *_ints("Skill", 5), *_ints("Action", 5),
])

# SpellItemEnchantment.dbc
extract_spell_item_enchantment = _make_specific_extractor("spell_item_enchantment", [
    _int("ID"),
    *_ints("Type", 3), *_ints("Amount", 3), *_ints("SpellID", 3),
    _str_loc("Description"),
    _int("AuraID"), _int("Slot"), _int("GemID"), _int("EnchantCondition"),
])

# WorldSafeLocs.dbc
extract_world_safe_locs = _make_specific_extractor("world_safe_locs", [
    _int("ID"), _int("Map"),
    _float("X"), _float("Y"), _float("Z"),
    _str_loc("Name"),
])

# ChatChannels.dbc
extract_chat_channels = _make_specific_extractor("chat_channels", [
    _int("ID"), _int("Flags"), _int("FactionGroup"),
    _str_loc("Name"), _str_loc("Shortcut"),
])

# QuestSort.dbc
extract_quest_sort = _make_specific_extractor("quest_sort", [
    _int("ID"), _str_loc("Name"),
])

# QuestInfo.dbc
extract_quest_info = _make_specific_extractor("quest_info", [
    _int("ID"), _str_loc("Name"),
])


# ── Main ─────────────────────────────────────────────────────────────────────

DBC_EXTRACTORS = {
    # Original 6 specific extractors
    "CharStartOutfit.dbc": ("CharStartOutfit",    extract_char_start_outfit),
    "AreaTable.dbc":       ("AreaTable",           extract_area_table),
    "ChrRaces.dbc":        ("ChrRaces",            extract_chr_races),
    "ChrClasses.dbc":      ("ChrClasses",          extract_chr_classes),
    "EmotesText.dbc":      ("EmotesText",          extract_emotes_text),
    "FactionTemplate.dbc": ("FactionTemplate",     extract_faction_template),
    # Spell — the big one
    "Spell.dbc":           ("Spell",               extract_spell),
    # Skills & talents
    "SkillLine.dbc":           ("SkillLine",           extract_skill_line),
    "SkillLineAbility.dbc":    ("SkillLineAbility",    extract_skill_line_ability),
    "Talent.dbc":              ("Talent",              extract_talent),
    "TalentTab.dbc":           ("TalentTab",           extract_talent_tab),
    # Travel
    "TaxiNodes.dbc":           ("TaxiNodes",           extract_taxi_nodes),
    "TaxiPath.dbc":            ("TaxiPath",            extract_taxi_path),
    "TaxiPathNode.dbc":        ("TaxiPathNode",        extract_taxi_path_node),
    # World
    "Map.dbc":                 ("Map",                 extract_map),
    "WorldSafeLocs.dbc":       ("WorldSafeLocs",       extract_world_safe_locs),
    # Spell sub-tables
    "SpellCastTimes.dbc":      ("SpellCastTimes",      extract_spell_cast_times),
    "SpellDuration.dbc":       ("SpellDuration",        extract_spell_duration),
    "SpellRange.dbc":          ("SpellRange",           extract_spell_range),
    "SpellRadius.dbc":         ("SpellRadius",          extract_spell_radius),
    "SpellItemEnchantment.dbc":("SpellItemEnchantment", extract_spell_item_enchantment),
    # Factions & creatures
    "Faction.dbc":             ("Faction",             extract_faction),
    "CreatureFamily.dbc":      ("CreatureFamily",      extract_creature_family),
    "CreatureType.dbc":        ("CreatureType",         extract_creature_type),
    # Items
    "ItemClass.dbc":           ("ItemClass",           extract_item_class),
    "ItemRandomProperties.dbc":("ItemRandomProperties", extract_item_random_properties),
    "ItemSet.dbc":             ("ItemSet",             extract_item_set),
    # Misc
    "Lock.dbc":                ("Lock",                extract_lock),
    "ChatChannels.dbc":        ("ChatChannels",        extract_chat_channels),
    "QuestSort.dbc":           ("QuestSort",           extract_quest_sort),
    "QuestInfo.dbc":           ("QuestInfo",           extract_quest_info),
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

    specific = 0
    generic = 0
    total_rows = 0
    print(f"dbc.db tables ({len(tables)}):")
    for t in tables:
        if t.startswith("_"):
            continue
        count = db.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        total_rows += count
        # Check if first column is "field_0" to identify generic tables
        cols = db.execute(f"PRAGMA table_info([{t}])").fetchall()
        if cols and cols[0][1] == "field_0":
            marker = "(generic)"
            generic += 1
        else:
            marker = ""
            specific += 1
        print(f"  {t:40s} {count:7d} rows  {marker}")

    print(f"\nTotal: {specific} specific + {generic} generic = {specific + generic} tables, {total_rows} rows")
    db.close()


# Known DBC files in vanilla 1.12.1 (for when MPQ listing doesn't work)
KNOWN_VANILLA_DBCS = [
    "AnimationData.dbc", "AreaPOI.dbc", "AreaTable.dbc", "AreaTrigger.dbc",
    "AttackAnimKits.dbc", "AttackAnimTypes.dbc", "AuctionHouse.dbc",
    "BankBagSlotPrices.dbc", "BattlemasterList.dbc",
    "CameraShakes.dbc", "Cfg_Categories.dbc", "Cfg_Configs.dbc",
    "CharBaseInfo.dbc", "CharHairGeosets.dbc", "CharHairTextures.dbc",
    "CharSections.dbc", "CharStartOutfit.dbc", "CharVariations.dbc",
    "ChatChannels.dbc", "ChatProfanity.dbc", "ChrClasses.dbc", "ChrRaces.dbc",
    "CinematicCamera.dbc", "CinematicSequences.dbc",
    "CreatureDisplayInfo.dbc", "CreatureDisplayInfoExtra.dbc",
    "CreatureFamily.dbc", "CreatureModelData.dbc", "CreatureSpellData.dbc",
    "CreatureType.dbc",
    "DanceAnimKitRefs.dbc", "DanceMoves.dbc", "DurabilityQuality.dbc", "DurabilityCosts.dbc",
    "Emotes.dbc", "EmotesText.dbc", "EmotesTextData.dbc", "EmotesTextSound.dbc",
    "EnvironmentalDamage.dbc", "Exhaustion.dbc",
    "Faction.dbc", "FactionGroup.dbc", "FactionTemplate.dbc",
    "FootstepTerrainLookup.dbc",
    "GMSurveyCurrentSurvey.dbc", "GMSurveyQuestions.dbc", "GMSurveySurveys.dbc",
    "GMTicketCategory.dbc",
    "GameObjectArtKit.dbc", "GameObjectDisplayInfo.dbc",
    "GameTips.dbc", "GroundEffectDoodad.dbc", "GroundEffectTexture.dbc",
    "HelmetGeosetVisData.dbc",
    "ItemBagFamily.dbc", "ItemClass.dbc", "ItemDisplayInfo.dbc",
    "ItemGroupSounds.dbc", "ItemPetFood.dbc", "ItemRandomProperties.dbc",
    "ItemRandomSuffix.dbc", "ItemSet.dbc", "ItemSubClass.dbc",
    "ItemSubClassMask.dbc", "ItemVisualEffects.dbc", "ItemVisuals.dbc",
    "LFGDungeons.dbc", "LanguageWords.dbc", "Languages.dbc",
    "LightFloatBand.dbc", "LightIntBand.dbc", "LightParams.dbc",
    "LightSkybox.dbc", "Light.dbc", "LiquidType.dbc", "LoadingScreenTaxiSplines.dbc",
    "LoadingScreens.dbc", "Lock.dbc", "LockType.dbc",
    "MailTemplate.dbc", "Map.dbc",
    "Material.dbc", "NameGen.dbc", "NamesProfanity.dbc", "NamesReserved.dbc",
    "NPCSounds.dbc",
    "Package.dbc", "PageTextMaterial.dbc", "PaperDollItemFrame.dbc",
    "PetLoyalty.dbc", "PetPersonality.dbc",
    "QuestInfo.dbc", "QuestSort.dbc",
    "Resistances.dbc",
    "ServerMessages.dbc", "SheatheSoundLookups.dbc",
    "SkillCostsData.dbc", "SkillLine.dbc", "SkillLineAbility.dbc",
    "SkillLineCategory.dbc", "SkillRaceClassInfo.dbc", "SkillTiers.dbc",
    "SoundAmbience.dbc", "SoundEntries.dbc", "SoundProviderPreferences.dbc",
    "SoundWaterType.dbc",
    "SpamMessages.dbc",
    "Spell.dbc", "SpellAuraNames.dbc", "SpellCastTimes.dbc",
    "SpellCategory.dbc", "SpellChainEffects.dbc",
    "SpellDispelType.dbc", "SpellDuration.dbc",
    "SpellEffectCameraShakes.dbc", "SpellFocusObject.dbc",
    "SpellIconName.dbc", "SpellItemEnchantment.dbc",
    "SpellMechanic.dbc", "SpellMissile.dbc", "SpellMissileMotion.dbc",
    "SpellRadius.dbc", "SpellRange.dbc",
    "SpellShapeshiftForm.dbc", "SpellVisual.dbc",
    "SpellVisualEffectName.dbc", "SpellVisualKit.dbc",
    "SpellVisualPrecastTransitions.dbc",
    "StableSlotPrices.dbc", "Startup_Strings.dbc",
    "Stationery.dbc", "StringLookups.dbc",
    "Talent.dbc", "TalentTab.dbc",
    "TaxiNodes.dbc", "TaxiPath.dbc", "TaxiPathNode.dbc",
    "TerrainType.dbc", "TerrainTypeSounds.dbc", "TransportAnimation.dbc",
    "UISoundLookups.dbc", "UnitBlood.dbc", "UnitBloodLevels.dbc",
    "VideoHardware.dbc", "VocalUISounds.dbc",
    "WMOAreaTable.dbc", "WeaponImpactSounds.dbc", "WeaponSwingSounds2.dbc",
    "Weather.dbc", "WorldMapArea.dbc", "WorldMapContinent.dbc",
    "WorldMapOverlay.dbc", "WorldSafeLocs.dbc",
    "ZoneIntroMusicTable.dbc", "ZoneMusic.dbc",
]


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
    db.execute("PRAGMA synchronous=NORMAL")

    # Track metadata
    db.execute("""
        CREATE TABLE IF NOT EXISTS _dbc_meta (
            name        TEXT PRIMARY KEY,
            extracted   TEXT,
            records     INTEGER,
            rows        INTEGER
        )
    """)

    # Pre-open MPQ archives for performance
    archives = open_mpq_archives(data_path)

    # ── Phase 1: Specific extractors ──────────────────────────────────────
    total_specific = 0
    total_generic = 0
    t_start = time.time()

    print(f"\n=== Phase 1: Specific extractors ({len(DBC_EXTRACTORS)} DBCs) ===")
    for dbc_file, (label, extractor) in DBC_EXTRACTORS.items():
        print(f"  {dbc_file:40s}", end=" ", flush=True)
        raw = read_dbc_from_mpq(data_path, dbc_file, archives)
        if not raw:
            print("NOT FOUND")
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
            print(f"OK  {n_records:6d} records -> {n_rows:7d} rows")
            total_specific += 1
        except Exception as e:
            print(f"ERROR: {e}")
            db.rollback()

    # ── Phase 2: Generic extractor for all remaining DBCs ─────────────────
    print(f"\n=== Phase 2: Generic extractor (remaining DBCs) ===")

    # Get list of all DBC files available
    discovered = list_dbc_files_in_mpq(data_path)
    if not discovered:
        # Fall back to known list
        discovered = KNOWN_VANILLA_DBCS

    # Which ones already have specific extractors?
    handled = set(DBC_EXTRACTORS.keys())

    remaining = [f for f in discovered if f not in handled]
    print(f"  Found {len(remaining)} additional DBC files to extract generically")

    for dbc_file in sorted(remaining):
        table_name = dbc_file.replace(".dbc", "").replace(".DBC", "").lower()
        # Sanitize table name
        table_name = table_name.replace("-", "_").replace(" ", "_")

        print(f"  {dbc_file:40s}", end=" ", flush=True)
        raw = read_dbc_from_mpq(data_path, dbc_file, archives)
        if not raw:
            print("NOT FOUND")
            continue

        try:
            if len(raw) < 20 or raw[:4] != b"WDBC":
                print("INVALID")
                continue
            n_records, n_fields = generic_extract(raw, db, table_name)
            db.execute(
                "INSERT OR REPLACE INTO _dbc_meta VALUES (?,datetime('now'),?,?)",
                (table_name, n_records, n_fields)
            )
            db.commit()
            print(f"OK  {n_records:6d} records, {n_fields:3d} fields")
            total_generic += 1
        except Exception as e:
            print(f"ERROR: {e}")
            db.rollback()

    elapsed = time.time() - t_start
    db.close()
    print(f"\nDone in {elapsed:.1f}s! {total_specific} specific + {total_generic} generic = {total_specific + total_generic} tables in {DBC_DB}")


if __name__ == "__main__":
    main()
