"""DB admin module — runs migrations on every load and exposes full DB admin CLI.

Reloading this module (via  reload db  or  reload all) will:
  • Re-import database.py (picks up any code changes to queries/functions)
  • Run all pending migrations against the live database
  • Re-register all CLI commands

To add a new schema change without restarting:
  1. Append a new entry to database.MIGRATIONS
  2. Type  reload db  — done.
"""
import importlib
import logging
import sqlite3
import sys

from modules.base import BaseModule

log = logging.getLogger("db")


class Module(BaseModule):
    name = "db"

    def on_load(self, server):
        self._server = server

        # Always reload database.py itself so any function changes take effect
        if "database" in sys.modules:
            importlib.reload(sys.modules["database"])
        import database as db_mod
        self._db = db_mod

        # Run all pending migrations immediately
        results = db_mod.run_migrations(server.db_path)
        pending = [(v, d) for v, d, s in results if s == "applied"]
        errors  = [(v, d, s) for v, d, s in results if s.startswith("ERROR")]
        if pending:
            for v, d in pending:
                log.info(f"  [migration {v}] {d} — applied.")
        if errors:
            for v, d, s in errors:
                log.error(f"  [migration {v}] {d} — {s}")
        if not pending and not errors:
            log.info("db: all migrations already up to date.")

        # ── CLI commands ──────────────────────────────────────────────

        self.reg_cli(server, "migrate",
                     self._cli_migrate,
                     help_text="migrate  — apply any pending DB migrations")

        self.reg_cli(server, "dbstatus",
                     self._cli_dbstatus,
                     help_text="dbstatus  — show migration history")

        self.reg_cli(server, "dbtables",
                     self._cli_dbtables,
                     help_text="dbtables  — list all tables")

        self.reg_cli(server, "dbdesc",
                     self._cli_dbdesc,
                     help_text="dbdesc <table>  — describe a table's columns")

        self.reg_cli(server, "dbquery",
                     self._cli_dbquery,
                     help_text="dbquery <sql>  — run a read-only SELECT query")

        self.reg_cli(server, "dbexec",
                     self._cli_dbexec,
                     help_text="dbexec <sql>  — execute any SQL statement (dangerous!)")

        self.reg_cli(server, "dbsize",
                     self._cli_dbsize,
                     help_text="dbsize  — show database file size and row counts")

        self.reg_cli(server, "testlogin",
                     self._cli_testlogin,
                     help_text="testlogin <user> <pass>  — verify stored SRP6 verifier matches password")

        log.info("db module loaded.")

    def _cli_testlogin(self, args):
        """Verify that the stored SRP6 verifier matches the given password (offline check)."""
        if len(args) < 2:
            return "Usage: testlogin <username> <password>"
        import config
        from srp6 import make_verifier
        user   = args[0].upper()
        passwd = args[1]
        account = self._db.get_account(config.DB_PATH, user)
        if not account:
            return f"  Account '{user}' not found."
        stored   = bytes(account["verifier"])
        salt     = bytes(account["salt"])
        computed = make_verifier(user, passwd, salt)
        lines = [f"\n  testlogin: {user}"]
        lines.append(f"  salt     : {salt.hex()}")
        lines.append(f"  stored v : {stored.hex()}")
        lines.append(f"  computed : {computed.hex()}")
        if computed == stored:
            lines.append("  RESULT   : OK — verifier matches, credentials are correct.")
        else:
            lines.append("  RESULT   : MISMATCH — stored verifier does not match that password.")
            lines.append("  Hint: re-create the account with the correct password:")
            lines.append(f"    delaccount {user}")
            lines.append(f"    addaccount {user} <correct_password>")
        return "\n".join(lines)

    def on_unload(self, server):
        log.info("db module unloaded.")

    # ── helpers ───────────────────────────────────────────────────────

    def _open(self):
        conn = sqlite3.connect(self._server.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── CLI ───────────────────────────────────────────────────────────

    def _cli_migrate(self, _args):
        results = self._db.run_migrations(self._server.db_path)
        lines = ["\n  Migration results:"]
        for v, desc, status in results:
            icon = "✓" if status == "applied" else ("✗" if status.startswith("ERROR") else "·")
            lines.append(f"  {icon}  [{v:02d}] {desc}  —  {status}")
        return "\n".join(lines)

    def _cli_dbstatus(self, _args):
        rows = self._db.migration_status(self._server.db_path)
        lines = [f"\n  {'Ver':<5} {'Status':<10} {'Applied At':<22} Description"]
        lines.append("  " + "─" * 75)
        for v, desc, status, applied_at in rows:
            icon = "✓" if status == "applied" else "⚠"
            lines.append(f"  {icon} {v:<4} {status:<10} {applied_at:<22} {desc}")
        return "\n".join(lines)

    def _cli_dbtables(self, _args):
        conn = self._open()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        conn.close()
        if not tables:
            return "  No tables found."
        lines = ["\n  Tables:"]
        for t in tables:
            conn2 = self._open()
            count = conn2.execute(f"SELECT COUNT(*) FROM [{t[0]}]").fetchone()[0]
            conn2.close()
            lines.append(f"    {t[0]:<30} {count} rows")
        return "\n".join(lines)

    def _cli_dbdesc(self, args):
        if not args:
            return "Usage: dbdesc <table>"
        table = args[0]
        conn = self._open()
        try:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except Exception as e:
            conn.close()
            return f"  Error: {e}"
        conn.close()
        if not cols:
            return f"  Table '{table}' not found or has no columns."
        lines = [f"\n  Table: {table}"]
        lines.append(f"  {'#':<4} {'Name':<20} {'Type':<15} {'NotNull':<8} {'Default':<15} PK")
        lines.append("  " + "─" * 70)
        for c in cols:
            pk   = "✓" if c["pk"] else ""
            nn   = "✓" if c["notnull"] else ""
            dflt = str(c["dflt_value"]) if c["dflt_value"] is not None else ""
            lines.append(f"  {c['cid']:<4} {c['name']:<20} {c['type']:<15} {nn:<8} {dflt:<15} {pk}")
        return "\n".join(lines)

    def _cli_dbquery(self, args):
        if not args:
            return "Usage: dbquery <sql>"
        sql = " ".join(args)
        if not sql.strip().upper().startswith("SELECT"):
            return "  dbquery only allows SELECT statements. Use dbexec for writes."
        conn = self._open()
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(50)
            conn.close()
        except Exception as e:
            conn.close()
            return f"  SQL error: {e}"
        if not rows:
            return "  (no rows)"
        keys = rows[0].keys()
        col_w = {k: max(len(k), max(len(str(r[k])) for r in rows)) for k in keys}
        header = "  " + "  ".join(k.ljust(col_w[k]) for k in keys)
        sep    = "  " + "  ".join("─" * col_w[k] for k in keys)
        lines  = ["\n" + header, sep]
        for r in rows:
            lines.append("  " + "  ".join(str(r[k]).ljust(col_w[k]) for k in keys))
        if len(rows) == 50:
            lines.append("  (showing first 50 rows)")
        return "\n".join(lines)

    def _cli_dbexec(self, args):
        if not args:
            return "Usage: dbexec <sql>"
        sql = " ".join(args)
        conn = self._open()
        try:
            cur = conn.execute(sql)
            conn.commit()
            affected = cur.rowcount
            conn.close()
        except Exception as e:
            conn.close()
            return f"  SQL error: {e}"
        return f"  OK — {affected} row(s) affected."

    def _cli_dbsize(self, _args):
        import os
        db_path = self._server.db_path
        size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        conn = self._open()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        lines = [f"\n  Database: {db_path}  ({size:,} bytes)"]
        lines.append(f"  {'Table':<30} Rows")
        lines.append("  " + "─" * 40)
        total = 0
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM [{t[0]}]").fetchone()[0]
            lines.append(f"  {t[0]:<30} {count:,}")
            total += count
        conn.close()
        lines.append("  " + "─" * 40)
        lines.append(f"  {'Total rows':<30} {total:,}")
        return "\n".join(lines)
