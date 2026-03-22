#!/usr/bin/env python3
"""
import_world.py  —  Import MaNGOS Zero world database into world.db (SQLite).

Usage:
    python3 import_world.py            # full import
    python3 import_world.py status     # show what's already imported
    python3 import_world.py <table>    # re-import a single table

The importer:
  1. Reads the MySQL schema from mangosdLoadDB.sql (CREATE TABLE statements)
  2. Converts MySQL types/syntax → SQLite
  3. Parses every INSERT file in World/Setup/FullDB/
  4. Imports data in bulk using executemany

This can be re-run safely — it drops and recreates each table.
"""

import os
import re
import sqlite3
import sys
import time

BASE    = os.path.dirname(os.path.abspath(__file__))
MANGOS  = os.path.join(BASE, "MangosZero Source DB", "database-master")
FULLDB  = os.path.join(MANGOS, "World", "Setup", "FullDB")
SCHEMA  = os.path.join(MANGOS, "World", "Setup", "mangosdLoadDB.sql")
WORLDDB = os.path.join(BASE, "world.db")

# ── MySQL → SQLite type map ───────────────────────────────────────────────────

_TYPE_RE = re.compile(
    r'\b(tiny|small|medium|big)?int\s*\(\s*\d+\s*\)\s*(unsigned\s*)?(zerofill\s*)?'
    r'|int\b'
    r'|\bfloat\b|\bdouble\b|\bdecimal\s*\([^)]+\)'
    r'|\bvarchar\s*\(\s*\d+\s*\)|\bchar\s*\(\s*\d+\s*\)'
    r'|\b(long|medium|tiny)?text\b'
    r'|\b(long|medium|tiny)?blob\b'
    r'|\benum\s*\([^)]+\)'
    r'|\bset\s*\([^)]+\)',
    re.I,
)

def _mysql_type_to_sqlite(mysql_type: str) -> str:
    t = mysql_type.strip().lower()
    if re.match(r'(tiny|small|medium|big)?int|^int$', t):
        return "INTEGER"
    if re.match(r'float|double|decimal', t):
        return "REAL"
    if re.match(r'(long|medium|tiny)?blob', t):
        return "BLOB"
    return "TEXT"  # varchar, char, text, enum, set, etc.


# ── Schema extraction ─────────────────────────────────────────────────────────

def _extract_create_body(sql: str, start: int) -> tuple[str, int]:
    """Given the position of the opening '(' of a CREATE TABLE, return (body, end_pos)
    by counting parentheses so nested enum(...) are handled correctly."""
    depth = 0
    i = start
    n = len(sql)
    body_start = -1
    while i < n:
        c = sql[i]
        if c == '(':
            depth += 1
            if depth == 1:
                body_start = i + 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return sql[body_start:i], i + 1
        elif c == "'":
            i += 1
            while i < n:
                if sql[i] == '\\':
                    i += 2
                    continue
                if sql[i] == "'":
                    break
                i += 1
        i += 1
    return "", start


def extract_schemas(schema_sql: str) -> dict[str, str]:
    """Parse CREATE TABLE blocks from MySQL DDL → SQLite CREATE TABLE strings."""
    schemas = {}
    header_re = re.compile(r'CREATE\s+TABLE\s+`?(\w+)`?\s*\(', re.I)
    for m in header_re.finditer(schema_sql):
        table = m.group(1)
        body, _ = _extract_create_body(schema_sql, m.end() - 1)
        cols = _parse_columns(body)
        if not cols:
            continue
        col_defs = ",\n  ".join(f"{name} {typ}" for name, typ in cols)
        schemas[table] = f"CREATE TABLE IF NOT EXISTS {table} (\n  {col_defs}\n)"
    return schemas


def _parse_columns(body: str) -> list[tuple[str, str]]:
    """Extract (name, sqlite_type) from the column body of a CREATE TABLE."""
    cols = []
    for line in body.split("\n"):
        line = line.strip().rstrip(",")
        # Skip constraint/index lines
        if re.match(r'(PRIMARY|UNIQUE|KEY|INDEX|CONSTRAINT|FULLTEXT)', line, re.I):
            continue
        # Column definition: `name` type [options...]
        m = re.match(r'`(\w+)`\s+(\S+(?:\s*\([^)]*\))?)', line)
        if not m:
            continue
        col_name = m.group(1)
        mysql_type = m.group(2)
        # Strip size spec off the matched type word for mapping
        base_type = re.sub(r'\s*\([^)]*\)', '', mysql_type)
        sqlite_type = _mysql_type_to_sqlite(base_type)
        cols.append((col_name, sqlite_type))
    return cols


# ── MySQL value parser ────────────────────────────────────────────────────────

def parse_insert(sql_text: str) -> tuple[str, list[str], list[tuple]]:
    """
    Parse a MySQL INSERT statement.
    Returns (table_name, [col_names], [(row_values), ...])
    """
    # Table name and column list
    header = re.search(
        r"INSERT\s+INTO\s+`?(\w+)`?\s*\(([^)]+)\)\s+VALUES\s*",
        sql_text, re.I | re.S,
    )
    if not header:
        return None, [], []

    table = header.group(1)
    cols  = [c.strip().strip("`") for c in header.group(2).split(",")]

    # Start of values block
    values_start = header.end()
    values_text  = sql_text[values_start:]

    rows = _parse_value_tuples(values_text)
    return table, cols, rows


def _parse_value_tuples(text: str) -> list[tuple]:
    """Parse MySQL value tuples: (v1,v2,...),(v1,v2,...) → list of tuples."""
    rows = []
    i = 0
    n = len(text)

    while i < n:
        # Seek next '('
        while i < n and text[i] != '(':
            i += 1
        if i >= n:
            break
        i += 1  # skip '('

        row   = []
        depth = 1  # track nested parens (shouldn't be in values, but be safe)

        while i < n and depth > 0:
            c = text[i]

            if c == "'":
                # String literal
                i += 1
                s = []
                while i < n:
                    ch = text[i]
                    if ch == "\\":
                        nxt = text[i + 1] if i + 1 < n else ''
                        ESC = {"'": "'", "\\": "\\", "n": "\n",
                               "r": "\r", "t": "\t", "0": "\x00",
                               "b": "\b", "z": "\x1a"}
                        s.append(ESC.get(nxt, nxt))
                        i += 2
                    elif ch == "'":
                        i += 1
                        # MySQL allows '' inside strings too
                        if i < n and text[i] == "'":
                            s.append("'")
                            i += 1
                        else:
                            break
                    else:
                        s.append(ch)
                        i += 1
                row.append("".join(s))

            elif text[i:i+4].upper() == "NULL" and (i + 4 >= n or not text[i + 4].isalnum()):
                row.append(None)
                i += 4

            elif c in "-0123456789":
                j = i
                if c == '-':
                    j += 1
                # digits + optional decimal
                while j < n and (text[j].isdigit() or text[j] == '.'):
                    j += 1
                # scientific notation
                if j < n and text[j] in 'eE':
                    j += 1
                    if j < n and text[j] in '+-':
                        j += 1
                    while j < n and text[j].isdigit():
                        j += 1
                raw = text[i:j]
                try:
                    row.append(int(raw))
                except ValueError:
                    try:
                        row.append(float(raw))
                    except ValueError:
                        row.append(raw)
                i = j

            elif c == ',':
                i += 1  # value separator

            elif c == ')':
                depth -= 1
                i += 1
                if depth == 0:
                    break

            elif c == '(':
                depth += 1
                i += 1

            else:
                # Unquoted token (shouldn't happen in well-formed data)
                j = i
                while j < n and text[j] not in ",)":
                    j += 1
                row.append(text[i:j].strip() or None)
                i = j

        rows.append(tuple(row))

    return rows


# ── Importer ──────────────────────────────────────────────────────────────────

def open_world_db() -> sqlite3.Connection:
    conn = sqlite3.connect(WORLDDB)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA cache_size   = -32000")  # 32 MB cache
    return conn


def import_table(conn: sqlite3.Connection, sql_file: str,
                 schemas: dict, force: bool = False) -> dict:
    """Import one SQL file. Returns stats dict."""
    table_name = os.path.splitext(os.path.basename(sql_file))[0]
    t0 = time.time()

    try:
        with open(sql_file, encoding="utf8", errors="replace") as f:
            raw = f.read()
    except Exception as e:
        return {"table": table_name, "status": "read_error", "error": str(e)}

    # Find INSERT statement
    if "INSERT INTO" not in raw.upper():
        return {"table": table_name, "status": "no_data"}

    table, cols, rows = parse_insert(raw)
    if not table or not rows:
        return {"table": table_name, "status": "parse_error"}

    # Create table
    if table in schemas:
        ddl = schemas[table]
    else:
        # Fallback: create with all TEXT columns
        col_defs = ", ".join(f"{c} TEXT" for c in cols)
        ddl = f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})"

    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(ddl)

    # Reconcile column count (INSERT cols vs schema cols)
    placeholders = ", ".join("?" * len(cols))
    # Build column name list for INSERT (schema may have different columns)
    col_list = ", ".join(cols)
    insert_sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"

    # Pad/trim rows to match column count
    ncols = len(cols)
    safe_rows = []
    for row in rows:
        if len(row) == ncols:
            safe_rows.append(row)
        elif len(row) > ncols:
            safe_rows.append(row[:ncols])
        else:
            safe_rows.append(row + (None,) * (ncols - len(row)))

    # Batch insert
    BATCH = 5000
    for i in range(0, len(safe_rows), BATCH):
        conn.executemany(insert_sql, safe_rows[i:i + BATCH])
    conn.commit()

    elapsed = time.time() - t0
    return {
        "table":   table,
        "status":  "ok",
        "rows":    len(safe_rows),
        "elapsed": elapsed,
    }


def run_import(tables_filter: list[str] | None = None):
    if not os.path.isdir(FULLDB):
        print(f"ERROR: FullDB directory not found: {FULLDB}")
        sys.exit(1)

    print(f"Loading schema from mangosdLoadDB.sql ...")
    schema_sql = open(SCHEMA, encoding="utf8", errors="replace").read()
    schemas    = extract_schemas(schema_sql)
    print(f"  Found {len(schemas)} table schemas.")

    conn = open_world_db()

    sql_files = sorted(f for f in os.listdir(FULLDB) if f.endswith(".sql"))
    if tables_filter:
        sql_files = [f for f in sql_files
                     if os.path.splitext(f)[0] in tables_filter]

    total_rows = 0
    errors     = []

    print(f"\nImporting {len(sql_files)} tables into {WORLDDB} ...\n")
    for i, fname in enumerate(sql_files, 1):
        path   = os.path.join(FULLDB, fname)
        result = import_table(conn, path, schemas)
        status = result["status"]
        if status == "ok":
            rows    = result["rows"]
            elapsed = result["elapsed"]
            total_rows += rows
            print(f"  [{i:3d}/{len(sql_files)}] {result['table']:<45} {rows:>7,} rows  {elapsed:.1f}s")
        elif status == "no_data":
            print(f"  [{i:3d}/{len(sql_files)}] {result['table']:<45} (no data)")
        else:
            err = result.get("error", "")
            print(f"  [{i:3d}/{len(sql_files)}] {result['table']:<45} ERROR: {err}")
            errors.append(result)

    # Create useful indexes
    print("\nCreating indexes ...")
    _create_indexes(conn)

    conn.close()
    print(f"\nDone! {total_rows:,} total rows imported.")
    if errors:
        print(f"  {len(errors)} error(s):")
        for e in errors:
            print(f"    {e}")


def _create_indexes(conn: sqlite3.Connection):
    indexes = [
        # creature spawns
        ("idx_creature_map",        "CREATE INDEX IF NOT EXISTS idx_creature_map ON creature(map)"),
        ("idx_creature_id",         "CREATE INDEX IF NOT EXISTS idx_creature_id  ON creature(id)"),
        # gameobject spawns
        ("idx_go_map",              "CREATE INDEX IF NOT EXISTS idx_go_map ON gameobject(map)"),
        ("idx_go_id",               "CREATE INDEX IF NOT EXISTS idx_go_id  ON gameobject(id)"),
        # items
        ("idx_item_entry",          "CREATE INDEX IF NOT EXISTS idx_item_entry ON item_template(entry)"),
        # quests
        ("idx_quest_entry",         "CREATE INDEX IF NOT EXISTS idx_quest_entry ON quest_template(entry)"),
        # vendors
        ("idx_vendor_entry",        "CREATE INDEX IF NOT EXISTS idx_vendor_entry ON npc_vendor(entry)"),
        # loot
        ("idx_clt_entry",           "CREATE INDEX IF NOT EXISTS idx_clt_entry ON creature_loot_template(entry)"),
        # game_tele
        ("idx_tele_name",           "CREATE INDEX IF NOT EXISTS idx_tele_name ON game_tele(name)"),
    ]
    for name, sql in indexes:
        try:
            conn.execute(sql)
        except Exception as e:
            print(f"  Warning: index {name}: {e}")
    conn.commit()


def show_status():
    if not os.path.exists(WORLDDB):
        print("world.db does not exist yet. Run: python3 import_world.py")
        return
    conn = open_world_db()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    total_rows = 0
    print(f"\n  world.db — {os.path.getsize(WORLDDB):,} bytes\n")
    print(f"  {'Table':<45} Rows")
    print("  " + "─" * 58)
    for (t,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"  {t:<45} {count:>10,}")
        total_rows += count
    print("  " + "─" * 58)
    print(f"  {'TOTAL':<45} {total_rows:>10,}")
    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "status":
        show_status()
    elif args:
        # Import specific tables
        run_import(tables_filter=args)
    else:
        run_import()
