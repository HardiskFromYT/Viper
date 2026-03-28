"""Movement + misc packet handler — consumes movement opcodes, updates position,
handles logout, creature queries, zone updates, and other client opcodes that
the vanilla 1.12.1 client sends frequently."""
import logging
import struct

from modules.base import BaseModule
from database import update_char_position, update_char_zone
from packets import ByteBuffer

log = logging.getLogger("movement")

# All movement opcodes the vanilla 1.12.1 client sends (0x0B5–0x0EE range)
_MOVE_OPCODES = [
    0x0B5, 0x0B6, 0x0B7, 0x0B8, 0x0B9, 0x0BA, 0x0BB, 0x0BC, 0x0BD, 0x0BE,
    0x0BF, 0x0C0, 0x0C1, 0x0C2, 0x0C3, 0x0C5,
    0x0C9, 0x0CA, 0x0CB,
    0x0DA, 0x0DB,
    0x0EC, 0x0ED, 0x0EE, 0x0F1, 0x0F7,
    0x319,  # MSG_MOVE_TIME_SKIPPED
]

# MSG_MOVE_TIME_SKIPPED is client-internal timing, never broadcast
_NO_BROADCAST = {0x319}

# Opcodes we silently consume (no processing needed)
_SILENT_OPCODES = [
    0x0B4,  # CMSG_AREATRIGGER
    0x097,  # CMSG_JOIN_CHANNEL
    0x098,  # CMSG_LEAVE_CHANNEL
    0x101,  # CMSG_STANDSTATECHANGE
    0x391,  # CMSG_TIME_SYNC_RESP
]

# How often to persist position to DB (every N movement packets)
# High value = less disk IO blocking the event loop, smoother movement
_SAVE_INTERVAL = 500

# Visibility: check every N movement packets, re-query if moved > threshold
_VIS_CHECK_INTERVAL = 10
_VIS_MOVE_THRESHOLD = 75.0   # yards

# Opcodes (import here to avoid circular imports)
_CMSG_LOGOUT_REQUEST      = 0x04B
_CMSG_PLAYER_LOGOUT       = 0x04A
_CMSG_LOGOUT_CANCEL       = 0x04E
_SMSG_LOGOUT_RESPONSE     = 0x04C
_SMSG_LOGOUT_COMPLETE     = 0x04D
_CMSG_SET_SELECTION        = 0x13D
_CMSG_ZONEUPDATE           = 0x1F4
_CMSG_SET_ACTIVE_MOVER     = 0x26A
_CMSG_CREATURE_QUERY       = 0x060
_SMSG_CREATURE_QUERY_RESP  = 0x061
_CMSG_GAMEOBJECT_QUERY     = 0x05E
_SMSG_GAMEOBJECT_QUERY_RESP = 0x05F
_CMSG_ITEM_QUERY_SINGLE    = 0x056
_SMSG_ITEM_QUERY_SINGLE_RESP = 0x058
_CMSG_QUESTGIVER_STATUS_QUERY = 0x182
_SMSG_QUESTGIVER_STATUS    = 0x183
_SMSG_TIME_SYNC_REQ        = 0x390


def _parse_movement(payload: bytes):
    """Parse movement info from a movement packet.
    Format: move_flags(4) + timestamp(4) + x(4) + y(4) + z(4) + o(4) ...
    Returns (x, y, z, o) or None."""
    try:
        if len(payload) < 24:
            return None
        x = struct.unpack_from("<f", payload, 8)[0]
        y = struct.unpack_from("<f", payload, 12)[0]
        z = struct.unpack_from("<f", payload, 16)[0]
        o = struct.unpack_from("<f", payload, 20)[0]
        return x, y, z, o
    except Exception:
        return None



class Module(BaseModule):
    name = "movement"

    def on_load(self, server):
        self._server = server
        self._heartbeat_count = {}

        # Movement opcodes — each gets a closure so we can broadcast with the opcode
        for opcode in _MOVE_OPCODES:
            self.reg_packet(server, opcode, self._make_move_handler(opcode))

        # Silent consumers
        for opcode in _SILENT_OPCODES:
            self.reg_packet(server, opcode, self._on_ignore)

        # Logout
        self.reg_packet(server, _CMSG_LOGOUT_REQUEST, self._on_logout_request)
        self.reg_packet(server, _CMSG_PLAYER_LOGOUT,  self._on_logout_request)
        self.reg_packet(server, _CMSG_LOGOUT_CANCEL,  self._on_ignore)

        # Selection / zone / active mover
        self.reg_packet(server, _CMSG_SET_SELECTION,    self._on_set_selection)
        self.reg_packet(server, _CMSG_ZONEUPDATE,       self._on_zone_update)
        self.reg_packet(server, _CMSG_SET_ACTIVE_MOVER, self._on_ignore)

        # Creature / gameobject / item queries
        self.reg_packet(server, _CMSG_CREATURE_QUERY,   self._on_creature_query)
        self.reg_packet(server, _CMSG_GAMEOBJECT_QUERY, self._on_gameobject_query)
        self.reg_packet(server, _CMSG_ITEM_QUERY_SINGLE, self._on_item_query)

        # Questgiver status
        self.reg_packet(server, _CMSG_QUESTGIVER_STATUS_QUERY, self._on_questgiver_status)

        log.info(f"movement loaded: {len(_MOVE_OPCODES)} movement + misc opcodes.")

    def on_unload(self, server):
        log.info("movement unloaded.")

    # ── Movement ──────────────────────────────────────────────────────

    def _make_move_handler(self, opcode):
        """Create a movement handler closure that knows its opcode for broadcasting."""
        def handler(session, payload):
            self._on_move(session, opcode, payload)
        return handler

    def _on_move(self, session, opcode, payload: bytes):
        if not session.char:
            return
        # During fly mode activation, briefly reject client movement to prevent
        # the client's old flags from overwriting the new swim-in-air state
        import time as _time
        reject_until = getattr(session, "_fly_reject_until", 0)
        if reject_until and _time.time() < reject_until:
            return

        # Store the last client timestamp for other modules to use
        if len(payload) >= 8:
            session._last_move_time = struct.unpack_from("<I", payload, 4)[0]

        # Broadcast to other players (all movement opcodes, as MaNGOS does)
        if opcode not in _NO_BROADCAST:
            try:
                from modules.world_data import broadcast_movement
                broadcast_movement(session, opcode, payload)
            except Exception as e:
                log.error(f"broadcast_movement FAILED: {e}", exc_info=True)

        pos = _parse_movement(payload)
        if not pos:
            return
        x, y, z, o = pos

        session.char = dict(session.char)
        session.char["pos_x"] = x
        session.char["pos_y"] = y
        session.char["pos_z"] = z
        session.char["orientation"] = o

        char_id = session.char["id"]
        count = self._heartbeat_count.get(char_id, 0) + 1
        self._heartbeat_count[char_id] = count

        # Persist position to DB periodically
        if count % _SAVE_INTERVAL == 0:
            try:
                update_char_position(session.db_path, char_id,
                                     session.char["map"], x, y, z, o)
            except Exception as e:
                log.debug(f"Position save error: {e}")

        # Visibility update: check if player moved far enough to re-query
        if count % _VIS_CHECK_INTERVAL == 0:
            self._check_visibility(session, x, y)

    # ── Visibility ──────────────────────────────────────────────────────

    def _check_visibility(self, session, x, y):
        """Re-query creatures if the player moved far enough from last spawn center."""
        center = getattr(session, "_spawn_center", None)
        if not center:
            return
        _, cx, cy = center
        dx = x - cx
        dy = y - cy
        if (dx * dx + dy * dy) < _VIS_MOVE_THRESHOLD * _VIS_MOVE_THRESHOLD:
            return  # haven't moved far enough
        try:
            from modules.world_data import update_visibility
            update_visibility(session)
        except Exception as e:
            log.debug(f"Visibility update error: {e}")

    # ── Logout ────────────────────────────────────────────────────────

    def _on_logout_request(self, session, payload: bytes):
        """Handle logout request — instant logout for GM, otherwise 20s timer."""
        # Save position
        if session.char:
            c = session.char
            try:
                update_char_position(session.db_path, c["id"],
                                     c["map"], c["pos_x"], c["pos_y"],
                                     c["pos_z"], c["orientation"])
            except Exception:
                pass

        # Remove this player's model from all other clients
        try:
            from modules.world_data import destroy_player_for_others
            destroy_player_for_others(session, self._server)
        except Exception:
            pass

        # Send SMSG_LOGOUT_RESPONSE: reason=0 (success), instant=1
        buf = ByteBuffer()
        buf.uint32(0)  # reason: 0 = success
        buf.uint8(1)   # instant_logout: 1 = immediate
        session._send(_SMSG_LOGOUT_RESPONSE, buf.bytes())

        # Send SMSG_LOGOUT_COMPLETE (empty body)
        session._send(_SMSG_LOGOUT_COMPLETE, b"")
        log.info(f"Player {session.account} logged out.")

    # ── Zone update ───────────────────────────────────────────────────

    def _on_zone_update(self, session, payload: bytes):
        """CMSG_ZONEUPDATE — client tells us which zone it entered.
        Persist to DB so the character select screen shows the right zone.
        """
        if len(payload) < 4 or not session.char:
            return
        zone_id = struct.unpack_from("<I", payload, 0)[0]
        session.char = dict(session.char)
        session.char["zone"] = zone_id
        try:
            update_char_zone(session.db_path, session.char["id"], zone_id)
            log.debug(f"Zone update: {session.char['name']} → zone {zone_id}")
        except Exception as e:
            log.warning(f"Zone save error: {e}")

    # ── Selection ─────────────────────────────────────────────────────

    def _on_set_selection(self, session, payload: bytes):
        """Track what the player has selected (target)."""
        if len(payload) >= 8:
            session.target_guid = struct.unpack_from("<Q", payload, 0)[0]

    # ── Creature query ────────────────────────────────────────────────

    def _on_creature_query(self, session, payload: bytes):
        """Respond to CMSG_CREATURE_QUERY with creature template data."""
        if len(payload) < 4:
            return
        entry = struct.unpack_from("<I", payload, 0)[0]

        try:
            from modules.world_data import get_creature_template
            tpl = get_creature_template(entry)
        except Exception:
            tpl = None

        if not tpl:
            # Not found: send entry with high bit set
            session._send(_SMSG_CREATURE_QUERY_RESP,
                          struct.pack("<I", entry | 0x80000000))
            return

        buf = ByteBuffer()
        buf.uint32(entry)
        buf.cstring(str(tpl["Name"] or "Creature"))   # name1
        buf.uint8(0)                                    # name2 (empty)
        buf.uint8(0)                                    # name3 (empty)
        buf.uint8(0)                                    # name4 (empty)
        buf.cstring(str(tpl["SubName"] or ""))          # sub_name / guild
        buf.uint32(int(tpl["CreatureTypeFlags"] or 0))  # type_flags
        buf.uint32(int(tpl["CreatureType"] or 0))       # creature_type
        buf.uint32(int(tpl["Family"] or 0))             # creature_family
        buf.uint32(int(tpl["Rank"] or 0))               # creature_rank
        buf.uint32(0)                                    # unknown
        buf.uint32(int(tpl["PetSpellDataId"] or 0))     # spell_data_id
        buf.uint32(int(tpl["ModelId1"] or 0))           # display_id
        buf.uint8(int(tpl["Civilian"] or 0))            # civilian
        buf.uint8(int(tpl["RacialLeader"] or 0))        # racial_leader
        session._send(_SMSG_CREATURE_QUERY_RESP, buf.bytes())

    # ── Gameobject query ──────────────────────────────────────────────

    def _on_gameobject_query(self, session, payload: bytes):
        """Respond to CMSG_GAMEOBJECT_QUERY."""
        if len(payload) < 4:
            return
        entry = struct.unpack_from("<I", payload, 0)[0]
        # Not found response
        session._send(_SMSG_GAMEOBJECT_QUERY_RESP,
                      struct.pack("<I", entry | 0x80000000))

    # ── Item query ────────────────────────────────────────────────────

    def _on_item_query(self, session, payload: bytes):
        """Respond to CMSG_ITEM_QUERY_SINGLE with item template data."""
        if len(payload) < 4:
            return
        item_id = struct.unpack_from("<I", payload, 0)[0]

        try:
            from modules.world_data import get_item_template
            tpl = get_item_template(item_id)
        except Exception:
            tpl = None

        if not tpl:
            session._send(_SMSG_ITEM_QUERY_SINGLE_RESP,
                          struct.pack("<I", item_id | 0x80000000))
            return

        # sqlite3.Row doesn't support .get() — convert to dict
        tpl = dict(tpl)

        # Build the massive item query response
        buf = ByteBuffer()
        buf.uint32(item_id)
        buf.uint32(int(tpl["class"] or 0))         # item_class
        buf.uint32(int(tpl["subclass"] or 0))       # item_sub_class
        buf.cstring(str(tpl["name"] or "Item"))     # name1
        buf.uint8(0)                                 # name2
        buf.uint8(0)                                 # name3
        buf.uint8(0)                                 # name4
        buf.uint32(int(tpl["displayid"] or 0))      # display_id
        buf.uint32(int(tpl["Quality"] or 0))         # quality
        buf.uint32(int(tpl["Flags"] or 0))           # flags
        buf.uint32(int(tpl["BuyPrice"] or 0))        # buy_price
        buf.uint32(int(tpl["SellPrice"] or 0))       # sell_price
        buf.uint32(int(tpl["InventoryType"] or 0))   # inventory_type
        buf.uint32(int(tpl["AllowableClass"] or -1) & 0xFFFFFFFF)  # allowable_class
        buf.uint32(int(tpl["AllowableRace"] or -1) & 0xFFFFFFFF)   # allowable_race
        buf.uint32(int(tpl["ItemLevel"] or 1))       # item_level
        buf.uint32(int(tpl["RequiredLevel"] or 0))   # required_level
        buf.uint32(int(tpl["RequiredSkill"] or 0))
        buf.uint32(int(tpl["RequiredSkillRank"] or 0))
        buf.uint32(int(tpl["requiredspell"] or 0))
        buf.uint32(int(tpl["requiredhonorrank"] or 0))
        buf.uint32(0)  # required_city_rank
        buf.uint32(int(tpl["RequiredReputationFaction"] or 0))
        buf.uint32(int(tpl["RequiredReputationRank"] or 0))
        buf.uint32(int(tpl["maxcount"] or 0))
        buf.uint32(int(tpl["stackable"] or 1))
        buf.uint32(int(tpl["ContainerSlots"] or 0))

        # 10 stats
        for i in range(1, 11):
            buf.uint32(int(tpl.get(f"stat_type{i}") or 0))
            buf.uint32(int(tpl.get(f"stat_value{i}") or 0) & 0xFFFFFFFF)

        # 5 damage types
        for i in range(1, 6):
            buf.float32(float(tpl.get(f"dmg_min{i}") or 0))
            buf.float32(float(tpl.get(f"dmg_max{i}") or 0))
            buf.uint32(int(tpl.get(f"dmg_type{i}") or 0))

        # 7 resistances
        buf.uint32(int(tpl.get("armor") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get("holy_res") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get("fire_res") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get("nature_res") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get("frost_res") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get("shadow_res") or 0) & 0xFFFFFFFF)
        buf.uint32(int(tpl.get("arcane_res") or 0) & 0xFFFFFFFF)

        buf.uint32(int(tpl.get("delay") or 0))
        buf.uint32(int(tpl.get("ammo_type") or 0))
        buf.float32(float(tpl.get("RangedModRange") or 0))

        # 5 spells
        for i in range(1, 6):
            buf.uint32(int(tpl.get(f"spellid_{i}") or 0))
            buf.uint32(int(tpl.get(f"spelltrigger_{i}") or 0))
            buf.uint32(int(tpl.get(f"spellcharges_{i}") or 0) & 0xFFFFFFFF)
            buf.uint32(int(tpl.get(f"spellcooldown_{i}") or 0) & 0xFFFFFFFF)
            buf.uint32(int(tpl.get(f"spellcategory_{i}") or 0))
            buf.uint32(int(tpl.get(f"spellcategorycooldown_{i}") or 0) & 0xFFFFFFFF)

        buf.uint32(int(tpl.get("bonding") or 0))
        buf.cstring(str(tpl.get("description") or ""))
        buf.uint32(int(tpl.get("PageText") or 0))
        buf.uint32(int(tpl.get("LanguageID") or 0))
        buf.uint32(int(tpl.get("PageMaterial") or 0))
        buf.uint32(int(tpl.get("startquest") or 0))
        buf.uint32(int(tpl.get("lockid") or 0))
        buf.uint32(int(tpl.get("Material") or 0))
        buf.uint32(int(tpl.get("sheath") or 0))
        buf.uint32(int(tpl.get("RandomProperty") or 0))
        buf.uint32(int(tpl.get("block") or 0))
        buf.uint32(int(tpl.get("itemset") or 0))
        buf.uint32(int(tpl.get("MaxDurability") or 0))
        buf.uint32(int(tpl.get("area") or 0))
        buf.uint32(int(tpl.get("Map") or 0))
        buf.uint32(int(tpl.get("BagFamily") or 0))
        session._send(_SMSG_ITEM_QUERY_SINGLE_RESP, buf.bytes())

    # ── Questgiver status ─────────────────────────────────────────────

    def _on_questgiver_status(self, session, payload: bytes):
        """Respond to questgiver status query — for now, always say 'no quests'."""
        if len(payload) < 8:
            return
        guid = struct.unpack_from("<Q", payload, 0)[0]
        buf = ByteBuffer()
        buf.uint64(guid)
        buf.uint32(0)  # QUEST_STATUS_NONE
        session._send(_SMSG_QUESTGIVER_STATUS, buf.bytes())

    # ── Ignore ────────────────────────────────────────────────────────

    def _on_ignore(self, session, payload: bytes):
        pass
