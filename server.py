"""Central server object — holds all runtime state and the module manager."""
import asyncio
import importlib
import logging
import sys
import time
from typing import Callable, Dict, Any

log = logging.getLogger("server")


class Server:
    def __init__(self, db_path: str):
        self.db_path   = db_path
        self.start_time = time.time()

        # opcode (int) -> list of {"module": str, "handler": callable}
        self._packet_handlers: Dict[int, list] = {}
        # name -> {"module": str, "handler": callable, "help": str, "min_gm": int}
        self._gm_commands: Dict[str, dict] = {}
        # name -> {"module": str, "handler": callable, "help": str}
        self._cli_commands: Dict[str, dict] = {}

        # username (upper) -> WorldSession
        self.sessions: Dict[str, Any] = {}

        # module name -> Module instance
        self._modules: Dict[str, Any] = {}

        # asyncio server handles set by main
        self._auth_server  = None
        self._world_server = None

    # ------------------------------------------------------------------
    # Packet handler registry
    # ------------------------------------------------------------------

    def register_packet_handler(self, opcode: int, handler: Callable,
                                 module_name: str = ""):
        self._packet_handlers.setdefault(opcode, []).append(
            {"module": module_name, "handler": handler}
        )

    def dispatch_packet(self, session, opcode: int, payload: bytes) -> bool:
        entries = self._packet_handlers.get(opcode)
        if not entries:
            return False
        for e in entries:
            try:
                e["handler"](session, payload)
            except Exception as ex:
                log.error(f"Packet handler 0x{opcode:04X} error: {ex}", exc_info=True)
        return True

    # ------------------------------------------------------------------
    # GM command registry
    # ------------------------------------------------------------------

    def register_gm_command(self, name: str, handler: Callable, *,
                             help_text: str = "", module_name: str = "",
                             min_gm: int = 1):
        self._gm_commands[name.lower()] = {
            "handler": handler, "help": help_text,
            "module": module_name, "min_gm": min_gm,
        }

    def dispatch_gm_command(self, session, cmd: str, args: list) -> bool:
        entry = self._gm_commands.get(cmd.lower())
        if not entry:
            return False
        if session.gm_level < entry["min_gm"]:
            session.send_sys_msg("You don't have permission for that command.")
            return True
        try:
            entry["handler"](session, args)
        except Exception as ex:
            log.error(f"GM command .{cmd} error: {ex}", exc_info=True)
            session.send_sys_msg(f"Command error: {ex}")
        return True

    # ------------------------------------------------------------------
    # CLI command registry
    # ------------------------------------------------------------------

    def register_cli_command(self, name: str, handler: Callable, *,
                              help_text: str = "", module_name: str = ""):
        self._cli_commands[name.lower()] = {
            "handler": handler, "help": help_text, "module": module_name,
        }

    async def dispatch_cli_command(self, raw: str) -> str:
        parts = raw.strip().split()
        if not parts:
            return ""
        cmd = parts[0].lower()
        args = parts[1:]
        entry = self._cli_commands.get(cmd)
        if not entry:
            return f"Unknown command '{cmd}'. Type 'help' for a list."
        try:
            result = entry["handler"](args)
            if asyncio.iscoroutine(result):
                result = await result
            return result or ""
        except Exception as ex:
            log.error(f"CLI command '{cmd}' error: {ex}", exc_info=True)
            return f"Error: {ex}"

    # ------------------------------------------------------------------
    # Module manager
    # ------------------------------------------------------------------

    def load_module(self, name: str) -> str:
        """Load or hot-reload a module. Returns status message."""
        action = "Reloaded" if name in self._modules else "Loaded"

        # Unload existing instance
        if name in self._modules:
            try:
                self._modules[name].on_unload(self)
            except Exception as ex:
                log.warning(f"Error during unload of '{name}': {ex}")
            del self._modules[name]

        # Clean up all registrations from this module
        self._purge_module(name)

        # Import / reload the Python module
        full = f"modules.{name}"
        try:
            if full in sys.modules:
                importlib.reload(sys.modules[full])
                py_mod = sys.modules[full]
            else:
                py_mod = importlib.import_module(full)
        except Exception as ex:
            return f"[modules/{name}] Import error: {ex}"

        cls = getattr(py_mod, "Module", None)
        if cls is None:
            return f"[modules/{name}] No 'Module' class found."

        instance = cls()
        try:
            instance.on_load(self)
        except Exception as ex:
            return f"[modules/{name}] on_load error: {ex}"

        self._modules[name] = instance
        log.info(f"Module '{name}' {action.lower()}.")
        return f"{action} module '{name}'."

    def load_all_modules(self):
        import config
        for name in config.MODULES:
            msg = self.load_module(name)
            log.info(msg)

    def reload_core(self, name: str) -> str:
        """Reload a top-level core Python file (e.g. 'database', 'packets').
        This refreshes the module's functions in sys.modules so any subsequent
        imports (including from modules) call the updated code.
        Note: existing  from X import Y  bindings in already-loaded modules
        won't update — reload those modules too if needed.
        """
        if name in sys.modules:
            try:
                importlib.reload(sys.modules[name])
                return f"Reloaded core module '{name}'."
            except Exception as ex:
                return f"Error reloading '{name}': {ex}"
        # Try to import it fresh
        try:
            importlib.import_module(name)
            return f"Imported core module '{name}'."
        except Exception as ex:
            return f"Cannot import '{name}': {ex}"

    def _purge_module(self, name: str):
        for op in list(self._packet_handlers.keys()):
            self._packet_handlers[op] = [
                e for e in self._packet_handlers[op] if e["module"] != name
            ]
        self._gm_commands  = {k: v for k, v in self._gm_commands.items()  if v["module"] != name}
        self._cli_commands = {k: v for k, v in self._cli_commands.items() if v["module"] != name}

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def add_session(self, username: str, session):
        self.sessions[username.upper()] = session

    def remove_session(self, username: str):
        self.sessions.pop(username.upper(), None)

    def get_session(self, name: str):
        return self.sessions.get(name.upper())

    def get_online_players(self):
        return list(self.sessions.values())

    def broadcast(self, opcode: int, data: bytes = b""):
        for s in list(self.sessions.values()):
            try:
                s._send(opcode, data)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @property
    def uptime_str(self) -> str:
        secs = int(time.time() - self.start_time)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h:02d}h {m:02d}m {s:02d}s"

    def module_list(self) -> list:
        """Return list of (name, status) tuples."""
        import config
        rows = []
        for name in config.MODULES:
            status = "loaded" if name in self._modules else "unloaded"
            rows.append((name, status))
        return rows
