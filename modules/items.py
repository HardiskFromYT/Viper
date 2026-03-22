"""Item definitions (data/items.json) + inventory CLI/GM commands."""
import json
import logging
import os

from modules.base import BaseModule
from database import get_account, get_characters, get_character_by_name

log = logging.getLogger("items")

ITEMS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "items.json")
_items: dict = {}


def load_items() -> dict:
    try:
        with open(ITEMS_FILE) as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except FileNotFoundError:
        log.warning(f"items.json not found at {ITEMS_FILE}")
        return {}
    except Exception as e:
        log.error(f"Failed to load items.json: {e}")
        return {}


def get_item(item_id: int) -> dict | None:
    return _items.get(int(item_id))


class Module(BaseModule):
    name = "items"

    def on_load(self, server):
        global _items
        _items = load_items()
        log.info(f"items loaded: {len(_items)} item templates.")

        # ── CLI commands ──────────────────────────────────────────────

        self.reg_cli(server, "items",
                     self._cli_items,
                     help_text="items [search]  — list item templates")

        self.reg_cli(server, "item",
                     self._cli_item,
                     help_text="item <id>  — show item template info")

        self.reg_cli(server, "additem",
                     self._cli_additem,
                     help_text="additem <char> <item_id> [count]  — give item to character")

        self.reg_cli(server, "inv",
                     self._cli_inv,
                     help_text="inv <char>  — show character inventory")

        self.reg_cli(server, "reloaditems",
                     self._cli_reload_items,
                     help_text="reloaditems  — reload items.json from disk")

        # ── GM commands ───────────────────────────────────────────────

        self.reg_gm(server, "additem",   self._gm_additem,
                    help_text=".additem <id> [count]  — add item to self", min_gm=1)

        self.reg_gm(server, "delitem",   self._gm_delitem,
                    help_text=".delitem <id>  — remove item from self", min_gm=1)

        self.reg_gm(server, "inv",       self._gm_inv,
                    help_text=".inv  — list your inventory", min_gm=1)

    def on_unload(self, server):
        log.info("items unloaded.")

    # ── CLI ───────────────────────────────────────────────────────────

    def _cli_items(self, args):
        search = args[0].lower() if args else ""
        rows = [(iid, it["name"], it.get("quality_str", "?"), it.get("type", "?"))
                for iid, it in _items.items()
                if not search or search in it["name"].lower()]
        if not rows:
            return "No items found."
        lines = [f"  {'ID':<6} {'Name':<35} {'Quality':<10} Type"]
        lines.append("  " + "-" * 65)
        for iid, name, qual, itype in sorted(rows)[:50]:
            lines.append(f"  {iid:<6} {name:<35} {qual:<10} {itype}")
        if len(rows) > 50:
            lines.append(f"  ... and {len(rows)-50} more (use a search term to filter)")
        return "\n".join(lines)

    def _cli_item(self, args):
        if not args:
            return "Usage: item <id>"
        try:
            iid = int(args[0])
        except ValueError:
            return "Item ID must be a number."
        it = get_item(iid)
        if not it:
            return f"Item {iid} not found."
        lines = [f"  Item #{iid}: {it['name']}"]
        for k, v in it.items():
            if k != "name":
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)

    def _cli_additem(self, args):
        if len(args) < 2:
            return "Usage: additem <char_name> <item_id> [count]"
        char_name = args[0]
        try:
            item_id = int(args[1])
            count   = int(args[2]) if len(args) > 2 else 1
        except ValueError:
            return "item_id and count must be numbers."
        it = get_item(item_id)
        if not it:
            return f"Item {item_id} not found in templates."
        from database import add_inventory_item
        from server import Server  # avoid circular at module level
        char = get_character_by_name(_get_db(), char_name)
        if not char:
            return f"Character '{char_name}' not found."
        add_inventory_item(_get_db(), char["id"], item_id, count)
        return f"Added {count}x [{it['name']}] to {char['name']}."

    def _cli_inv(self, args):
        if not args:
            return "Usage: inv <char_name>"
        from database import get_inventory
        char = get_character_by_name(_get_db(), args[0])
        if not char:
            return f"Character '{args[0]}' not found."
        inv = get_inventory(_get_db(), char["id"])
        if not inv:
            return f"{char['name']} has no items."
        lines = [f"  Inventory of {char['name']}:"]
        lines.append(f"  {'Item ID':<10} {'Name':<35} Count")
        lines.append("  " + "-" * 55)
        for row in inv:
            it = get_item(row["item_id"])
            name = it["name"] if it else f"Unknown({row['item_id']})"
            lines.append(f"  {row['item_id']:<10} {name:<35} {row['count']}")
        return "\n".join(lines)

    def _cli_reload_items(self, _args):
        global _items
        _items = load_items()
        return f"Reloaded {len(_items)} item templates from disk."

    # ── GM commands ───────────────────────────────────────────────────

    def _gm_additem(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .additem <id> [count]")
            return
        try:
            item_id = int(args[0])
            count   = int(args[1]) if len(args) > 1 else 1
        except ValueError:
            session.send_sys_msg("Arguments must be numbers.")
            return
        it = get_item(item_id)
        if not it:
            session.send_sys_msg(f"Unknown item id {item_id}.")
            return
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        from database import add_inventory_item
        add_inventory_item(session.db_path, session.char["id"], item_id, count)
        session.send_sys_msg(f"Added {count}x [{it['name']}] to inventory.")

    def _gm_delitem(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .delitem <id>")
            return
        try:
            item_id = int(args[0])
        except ValueError:
            session.send_sys_msg("Item ID must be a number.")
            return
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        from database import remove_inventory_item
        removed = remove_inventory_item(session.db_path, session.char["id"], item_id)
        if removed:
            session.send_sys_msg(f"Removed item {item_id} from inventory.")
        else:
            session.send_sys_msg(f"Item {item_id} not found in inventory.")

    def _gm_inv(self, session, _args):
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        from database import get_inventory
        inv = get_inventory(session.db_path, session.char["id"])
        if not inv:
            session.send_sys_msg("Your inventory is empty.")
            return
        lines = ["Your inventory:"]
        for row in inv:
            it = get_item(row["item_id"])
            name = it["name"] if it else f"Item#{row['item_id']}"
            lines.append(f"  [{row['item_id']}] {name} x{row['count']}")
        session.send_sys_msg("\n".join(lines))


def _get_db():
    import config
    return config.DB_PATH
