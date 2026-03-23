"""Interactive async CLI for TestEmu.
Runs in its own executor so it never blocks the asyncio event loop.
"""
import asyncio
import logging
import time

log = logging.getLogger("cli")

_BANNER = """
╔══════════════════════════════════════════════════╗
║          TestEmu  •  WoW 1.12 Emulator           ║
║  Type  help  for available commands               ║
╚══════════════════════════════════════════════════╝
"""


async def run_cli(server):
    loop = asyncio.get_running_loop()
    print(_BANNER)
    _register_builtin_commands(server)
    while True:
        try:
            raw = await loop.run_in_executor(None, _read_line)
        except (EOFError, KeyboardInterrupt):
            break
        if raw is None:
            break
        raw = raw.strip()
        if not raw:
            continue
        result = await server.dispatch_cli_command(raw)
        if result:
            print(result)


def _read_line() -> str | None:
    try:
        return input("\033[36m>\033[0m ")
    except EOFError:
        return None


# ── Built-in CLI commands (registered on the server) ────────────────────────

def _register_builtin_commands(server):
    from database import (get_account, create_account, delete_account,
                          set_gm_level, set_account_password,
                          get_all_accounts, get_characters,
                          get_character_by_name)

    def _help(args):
        lines = ["", "  Available commands:", "  " + "─" * 60]
        # Group by module
        by_module: dict = {}
        for name, entry in sorted(server._cli_commands.items()):
            mod = entry["module"] or "builtin"
            by_module.setdefault(mod, []).append((name, entry["help"] or name))
        for mod in sorted(by_module):
            lines.append(f"\n  [{mod}]")
            for _, help_text in sorted(by_module[mod]):
                lines.append(f"    {help_text}")
        return "\n".join(lines)

    def _status(args):
        from config import AUTH_HOST, AUTH_PORT, WORLD_HOST, WORLD_PORT, REALM_NAME
        online = server.get_online_players()
        modules_ok = sum(1 for _, s in server.module_list() if s == "loaded")
        return (
            f"\n  ── Server Status ──────────────────────────────\n"
            f"  Uptime   : {server.uptime_str}\n"
            f"  Realm    : {REALM_NAME}\n"
            f"  Auth     : {AUTH_HOST}:{AUTH_PORT}\n"
            f"  World    : {WORLD_HOST}:{WORLD_PORT}\n"
            f"  Online   : {len(online)} player(s)\n"
            f"  Modules  : {modules_ok}/{len(server.module_list())} loaded\n"
        )

    def _players(args):
        online = server.get_online_players()
        if not online:
            return "  No players online."
        lines = [f"\n  Online players ({len(online)}):"]
        lines.append(f"  {'Account':<20} {'Char':<16} {'Lvl':<5} {'Map':<5} {'X':>10} {'Y':>10}")
        lines.append("  " + "─" * 70)
        for s in online:
            c = s.char
            if c:
                lines.append(f"  {s.account:<20} {c['name']:<16} {c['level']:<5} "
                              f"{c['map']:<5} {c['pos_x']:>10.1f} {c['pos_y']:>10.1f}")
            else:
                lines.append(f"  {s.account:<20} (char select)")
        return "\n".join(lines)

    def _player(args):
        if not args:
            return "Usage: player <name>"
        from database import get_character_by_name, get_inventory
        import config
        c = get_character_by_name(config.DB_PATH, args[0])
        if not c:
            return f"  Character '{args[0]}' not found."
        inv = get_inventory(config.DB_PATH, c["id"])
        online = server.get_session(c["name"])  # sessions keyed by account
        lines = [
            f"\n  ── Character: {c['name']} ─────────────────────────",
            f"  GUID    : {c['id']}",
            f"  Level   : {c['level']}",
            f"  Race    : {c['race']}  Class: {c['class']}  Gender: {c['gender']}",
            f"  Map     : {c['map']}  Zone: {c['zone']}",
            f"  Pos     : X={c['pos_x']:.2f}  Y={c['pos_y']:.2f}  Z={c['pos_z']:.2f}",
            f"  Orient  : {c['orientation']:.4f}",
            f"  Items   : {len(inv)} in inventory",
            f"  Online  : {'yes' if online else 'no'}",
        ]
        return "\n".join(lines)

    def _accounts(args):
        import config
        rows = get_all_accounts(config.DB_PATH)
        if not rows:
            return "  No accounts."
        lines = [f"\n  {'ID':<5} {'Username':<20} {'GM':<5}"]
        lines.append("  " + "─" * 35)
        for r in rows:
            lines.append(f"  {r['id']:<5} {r['username']:<20} {r['gm_level']:<5}")
        return "\n".join(lines)

    def _account_cmd(args):
        import config
        if not args:
            return "Usage: account <create|delete|setgm|setpass|info> ..."
        sub = args[0].lower()
        rest = args[1:]

        if sub == "create":
            if len(rest) < 2:
                return "Usage: account create <username> <password>"
            create_account(config.DB_PATH, rest[0], rest[1])
            return f"Account '{rest[0]}' created."

        if sub == "delete":
            if not rest:
                return "Usage: account delete <username>"
            delete_account(config.DB_PATH, rest[0])
            return f"Account '{rest[0]}' deleted."

        if sub == "setgm":
            if len(rest) < 2:
                return "Usage: account setgm <username> <level 0-3>"
            try:
                lvl = int(rest[1])
            except ValueError:
                return "Level must be 0-3."
            set_gm_level(config.DB_PATH, rest[0], lvl)
            s = server.get_session(rest[0])
            if s:
                s.gm_level = lvl
            return f"GM level of '{rest[0]}' set to {lvl}."

        if sub == "setpass":
            if len(rest) < 2:
                return "Usage: account setpass <username> <newpassword>"
            set_account_password(config.DB_PATH, rest[0], rest[1])
            return f"Password for '{rest[0]}' updated."

        if sub == "info":
            if not rest:
                return "Usage: account info <username>"
            import config as cfg
            row = get_account(cfg.DB_PATH, rest[0])
            if not row:
                return f"Account '{rest[0]}' not found."
            chars = get_characters(cfg.DB_PATH, row["id"])
            lines = [
                f"\n  Account: {row['username']}",
                f"  ID     : {row['id']}",
                f"  GM Lvl : {row['gm_level']}",
                f"  Chars  : {len(chars)}",
            ]
            for c in chars:
                lines.append(f"    - {c['name']} (lvl {c['level']}, "
                              f"race {c['race']}, class {c['class']})")
            return "\n".join(lines)

        return f"Unknown account subcommand '{sub}'."

    def _chars(args):
        import config
        lines = []
        if args:
            row = get_account(config.DB_PATH, args[0])
            if not row:
                return f"Account '{args[0]}' not found."
            accounts = [row]
        else:
            accounts = get_all_accounts(config.DB_PATH)

        for acc in accounts:
            chars = get_characters(config.DB_PATH, acc["id"])
            if chars:
                lines.append(f"\n  {acc['username']}:")
                for c in chars:
                    lines.append(f"    [{c['id']}] {c['name']}  lvl {c['level']}  "
                                  f"race {c['race']} class {c['class']}  "
                                  f"map {c['map']} ({c['pos_x']:.0f},{c['pos_y']:.0f})")
        return "\n".join(lines) if lines else "  No characters found."

    def _modules_cmd(args):
        ml = server.module_list()
        lines = [f"\n  {'Module':<20} Status"]
        lines.append("  " + "─" * 35)
        for name, status in ml:
            icon = "✓" if status == "loaded" else "✗"
            lines.append(f"  {icon}  {name:<20} {status}")
        return "\n".join(lines)

    def _reload(args):
        if not args:
            return ("Usage:\n"
                    "  reload <module>       — hot-reload a module from modules/\n"
                    "  reload all            — reload every module in config.MODULES\n"
                    "  reload core <name>    — reload a top-level file (e.g. database)")
        if args[0].lower() == "core":
            if len(args) < 2:
                return "Usage: reload core <filename_without_.py>"
            return server.reload_core(args[1])
        if args[0].lower() == "all":
            results = []
            import config
            for name in config.MODULES:
                results.append(server.load_module(name))
            return "\n".join(f"  {r}" for r in results)
        return server.load_module(args[0])

    def _kick(args):
        if not args:
            return "Usage: kick <account_name>"
        s = server.get_session(args[0])
        if not s:
            return f"  '{args[0]}' not online."
        s.transport.close()
        return f"  Kicked {args[0]}."

    def _announce(args):
        if not args:
            return "Usage: announce <message>"
        from opcodes import SMSG_MESSAGECHAT
        from packets import ByteBuffer
        msg = " ".join(args)
        buf = ByteBuffer()
        buf.uint8(10)    # CHAT_MSG_SYSTEM
        buf.uint32(0); buf.uint64(0); buf.uint32(0); buf.uint64(0)
        buf.uint32(len(msg) + 1); buf.cstring(msg); buf.uint8(0)
        server.broadcast(SMSG_MESSAGECHAT, buf.bytes())
        return f"  Announced: {msg}"

    def _setgm(args):
        import config
        if len(args) < 2:
            return "Usage: setgm <account> <level 0-3>"
        try:
            lvl = int(args[1])
        except ValueError:
            return "Level must be 0-3."
        set_gm_level(config.DB_PATH, args[0], lvl)
        s = server.get_session(args[0])
        if s:
            s.gm_level = lvl
        return f"  GM level of '{args[0]}' set to {lvl}."

    def _addaccount(args):
        import config
        if len(args) < 2:
            return "Usage: addaccount <username> <password>"
        create_account(config.DB_PATH, args[0], args[1])
        return f"  Account '{args[0]}' created."

    def _delaccount(args):
        import config
        if not args:
            return "Usage: delaccount <username>"
        delete_account(config.DB_PATH, args[0])
        return f"  Account '{args[0]}' deleted."

    def _setpass(args):
        import config
        if len(args) < 2:
            return "Usage: setpass <username> <newpass>"
        set_account_password(config.DB_PATH, args[0], args[1])
        return f"  Password updated for '{args[0]}'."

    def _char_info(args):
        if not args:
            return "Usage: char <name>"
        return _player(args)  # same output

    def _logs(args):
        import logbuffer
        n = 100
        if args:
            try:
                n = int(args[0])
            except ValueError:
                return "Usage: logs [n]  — show last n log lines (default 100)"
        entries = list(logbuffer.buffer)
        tail = entries[-n:] if len(entries) >= n else entries
        if not tail:
            return "  No log entries yet."
        return "\n".join(tail)

    def _shutdown(_args):
        import os, signal
        os.kill(os.getpid(), signal.SIGINT)
        return "  Shutting down..."

    # Register all
    cmds = [
        ("help",       _help,       "help  — list all commands"),
        ("status",     _status,     "status  — server status & uptime"),
        ("players",    _players,    "players  — list online players"),
        ("player",     _player,     "player <name>  — detailed player/char info"),
        ("char",       _char_info,  "char <name>  — detailed character info"),
        ("accounts",   _accounts,   "accounts  — list all accounts"),
        ("account",    _account_cmd,"account <create|delete|setgm|setpass|info> ..."),
        ("chars",      _chars,      "chars [account]  — list characters"),
        ("modules",    _modules_cmd,"modules  — list loaded modules and status"),
        ("reload",     _reload,     "reload <module|all|core <name>>  — hot-reload module(s) or core files"),
        ("kick",       _kick,       "kick <account>  — disconnect a player"),
        ("announce",   _announce,   "announce <msg>  — server-wide chat announcement"),
        ("setgm",      _setgm,      "setgm <account> <level>  — set GM level (0-3)"),
        ("addaccount", _addaccount, "addaccount <user> <pass>  — create account"),
        ("delaccount", _delaccount, "delaccount <user>  — delete account"),
        ("setpass",    _setpass,    "setpass <user> <pass>  — change password"),
        ("logs",       _logs,       "logs [n]  — show last n log lines (default 100)"),
        ("shutdown",   _shutdown,   "shutdown  — stop the server"),
        ("quit",       _shutdown,   "quit  — alias for shutdown"),
    ]
    for name, fn, help_text in cmds:
        server.register_cli_command(name, fn, help_text=help_text, module_name="builtin")
