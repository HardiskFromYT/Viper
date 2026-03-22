"""Base class for all hot-reloadable server modules."""
from typing import Callable


class BaseModule:
    name = "base"

    def on_load(self, server):
        """Called when the module is loaded or reloaded."""

    def on_unload(self, server):
        """Called before the module is unloaded/reloaded."""

    # Convenience registration helpers (all tag handlers with self.name so they
    # get automatically cleaned up on reload via Server._purge_module)

    def reg_packet(self, server, opcode: int, handler: Callable):
        server.register_packet_handler(opcode, handler, module_name=self.name)

    def reg_gm(self, server, cmd: str, handler: Callable, *,
               help_text: str = "", min_gm: int = 1):
        server.register_gm_command(cmd, handler,
                                   help_text=help_text,
                                   module_name=self.name,
                                   min_gm=min_gm)

    def reg_cli(self, server, cmd: str, handler: Callable, *, help_text: str = ""):
        server.register_cli_command(cmd, handler,
                                    help_text=help_text,
                                    module_name=self.name)
