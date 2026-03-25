"""GM command system: parses in-game chat '.' commands and routes them.
   Also provides teleport, level, speed, announce, kick, info, etc."""
import logging
import struct
import time

import re

from modules.base import BaseModule
from opcodes import (CMSG_MESSAGECHAT, SMSG_MESSAGECHAT, SMSG_UPDATE_OBJECT,
                     SMSG_FORCE_RUN_SPEED_CHANGE, SMSG_FORCE_SWIM_SPEED_CHANGE)
from packets import ByteBuffer, pack_guid
from database import (get_character_by_name, set_gm_level, set_char_level,
                      update_char_position, get_char_money, set_char_money)
from modules.core_world import teleport_player

log = logging.getLogger("gm")


_SYS_MSG_MAX = 200   # WoW 1.12 silently drops chat packets > ~255 chars


def _send_sys_line(session, line: str):
    """Send a single short system-chat line. Internal — do not call directly.
    1.12 SYSTEM layout differs from SAY: only one GUID, then msgLen directly.
      type(u8) + lang(u32) + guid(u64) + msgLen(u32) + msg + NUL + tag(u8)
    Any extra bytes before msgLen shift the msgLen field to a zero, so the
    client reads msgLen=0 and displays nothing.
    """
    line = line[:_SYS_MSG_MAX]
    msg_bytes = line.encode("utf-8")
    buf = ByteBuffer()
    buf.uint8(10)                          # CHAT_MSG_SYSTEM
    buf.uint32(0)                          # LANG_UNIVERSAL
    buf.uint64(0)                          # sender GUID = 0 (system message)
    buf.uint32(len(msg_bytes) + 1)         # msgLen (includes NUL)
    buf.raw(msg_bytes + b"\x00")           # message + NUL
    buf.uint8(0)                           # chat tag
    data = buf.bytes()
    log.debug(f"sys_line ({len(data)}B): {data.hex()}")
    session._send(SMSG_MESSAGECHAT, data)


def send_sys_msg(session, msg: str):
    """Send a CHAT_MSG_SYSTEM message, splitting on newlines so each packet
    stays well under the 255-char client limit.
    Vanilla 1.12 SYSTEM layout (differs from SAY — only one GUID, no second GUID):
      type(u8) + lang(u32) + guid(u64) + msglen(u32) + msg + NUL + tag(u8)
    """
    for line in msg.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        # If a single line is still too long, chunk it
        while len(line.encode("utf-8")) > _SYS_MSG_MAX:
            _send_sys_line(session, line[:_SYS_MSG_MAX])
            line = line[_SYS_MSG_MAX:]
        if line:
            _send_sys_line(session, line)

# Named teleport locations: name -> (map_id, x, y, z, zone_id)
# Zone IDs from vanilla 1.12.1 AreaTable.dbc
_NAMED_LOCATIONS = {
    # Alliance cities
    "stormwind":    (0, -8833.37,  628.62,   94.00,  1519),
    "ironforge":    (0, -4981.25, -881.54,  501.76,  1537),
    "darnassus":    (1,  9952.07, 2280.32,  1341.39, 1657),
    "exodar":       (530, -3961.64, -13931.2, 100.61, 3557),
    # Horde cities
    "orgrimmar":    (1,  1526.00, -4421.00,   6.00,  1637),
    "undercity":    (0,  1596.01,  240.44,  -65.00,  1497),
    "thunderbluff": (1, -1282.73,  141.56,  131.33,  1638),
    "silvermoon":   (530, 9369.81, -7368.74, 14.23,  3487),
    # Shattrath
    "shattrath":    (530, -1836.21, 5301.35, -12.43, 3703),
    # Neutral
    "gadgetzan":    (1, -7178.24, -3804.52,   8.91,   440),
    "booty":        (0, -14353.9,  532.1,     23.0,    33),
    "bootybay":     (0, -14353.9,  532.1,     23.0,    33),
    "mudsprocket":  (1,  -4425.0, -1134.0,    26.0,    15),
    # Instances / special
    "gmisland":     (1, 16222.0,  16265.0,   14.0,   876),
    "gm":           (1, 16222.0,  16265.0,   14.0,   876),
    # Starting zones (Alliance)
    "northshire":   (0,  -8913.0,  -117.0,   80.0,    12),
    "coldridge":    (0,  -6240.0,   331.0,  382.0,     1),
    "teldrassil":   (1,  10311.0,   832.0, 1326.0,   141),
    # Starting zones (Horde)
    "durotar":      (1,   -618.0, -4251.0,   38.7,    14),
    "tirisfal":     (0,   -284.0,  1687.0,   89.0,    85),
    "mulgore":      (1,  -2918.0,  -258.0,   53.0,   215),
    # Other
    "dalaran":      (0,    534.0,  -804.0,   96.0,    36),
}

CHAT_MSG_SAY    = 0
CHAT_MSG_YELL   = 5
CHAT_MSG_SYSTEM = 10

# Update field constants — vanilla 1.12.1 (build 5875)
UNIT_FIELD_HEALTH      = 0x0016
UNIT_FIELD_MAXHEALTH   = 0x001C
UNIT_FIELD_LEVEL       = 0x0022
PLAYER_FIELD_COINAGE   = 0x0498

# Gold cap in copper: 214748g 36s 47c
_GOLD_CAP = 2147483647


def _build_values_update(guid: int, fields: dict) -> bytes:
    """Build an SMSG_UPDATE_OBJECT UPDATETYPE_VALUES packet for a single object.
    Format: count(u32) + has_transport(u8) + update_type(u8) + packed_guid + mask + data
    """
    max_field = max(fields.keys()) + 1
    num_blocks = (max_field + 31) // 32
    mask_words = [0] * num_blocks
    field_data = bytearray()
    for idx in sorted(fields):
        mask_words[idx // 32] |= (1 << (idx % 32))
    for idx in sorted(fields):
        field_data += struct.pack("<I", fields[idx] & 0xFFFFFFFF)

    pkt = ByteBuffer()
    pkt.uint32(1)              # block count
    pkt.uint8(0)               # has_transport
    pkt.uint8(0)               # update_type: UPDATETYPE_VALUES
    pkt.raw(pack_guid(guid))   # packed guid
    pkt.uint8(num_blocks)
    for w in mask_words:
        pkt.uint32(w)
    pkt.raw(field_data)
    return pkt.bytes()


class Module(BaseModule):
    name = "gm"

    def on_load(self, server):
        self._server = server
        self.reg_packet(server, CMSG_MESSAGECHAT, self._on_chat)

        # ── GM commands ───────────────────────────────────────────────

        self.reg_gm(server, "help",      self._cmd_help,
                    help_text=".help  — list GM commands", min_gm=1)
        self.reg_gm(server, "info",      self._cmd_info,
                    help_text=".info  — show your position / account info", min_gm=1)
        self.reg_gm(server, "teleport",  self._cmd_teleport,
                    help_text=".teleport <name|x y z [map]>  — teleport to location or coords", min_gm=1)
        self.reg_gm(server, "tel",       self._cmd_teleport,
                    help_text=".tel <name|x y z [map]>", min_gm=1)
        self.reg_gm(server, "tele",      self._cmd_teleport,
                    help_text=".tele <name|x y z [map]>", min_gm=1)
        self.reg_gm(server, "level",     self._cmd_level,
                    help_text=".level <n>  — set your level", min_gm=1)
        self.reg_gm(server, "heal",      self._cmd_heal,
                    help_text=".heal  — restore full HP/mana", min_gm=1)
        self.reg_gm(server, "speed",     self._cmd_speed,
                    help_text=".speed <n>  — set run speed multiplier", min_gm=1)
        self.reg_gm(server, "announce",  self._cmd_announce,
                    help_text=".announce <msg>  — server-wide announcement", min_gm=2)
        self.reg_gm(server, "ann",       self._cmd_announce,
                    help_text=".ann <msg>", min_gm=2)
        self.reg_gm(server, "kick",      self._cmd_kick,
                    help_text=".kick <player>  — disconnect a player", min_gm=2)
        self.reg_gm(server, "setgm",     self._cmd_setgm,
                    help_text=".setgm <account> <level>  — set GM level (0-3)", min_gm=3)
        self.reg_gm(server, "players",   self._cmd_players,
                    help_text=".players  — list online players", min_gm=1)
        self.reg_gm(server, "setpos",    self._cmd_setpos,
                    help_text=".setpos <char> <x> <y> <z>  — set offline char position", min_gm=2)
        self.reg_gm(server, "fly",       self._cmd_fly,
                    help_text=".fly <on|off>  — toggle GM flight (swim in air)", min_gm=1)
        self.reg_gm(server, "gold",      self._cmd_gold,
                    help_text=".gold <amount|cap|reset>  — add gold (e.g. 1g27s19c)", min_gm=1)

        log.info("gm loaded.")

    def on_unload(self, server):
        log.info("gm unloaded.")

    # ── CMSG_MESSAGECHAT parser ───────────────────────────────────────

    def _on_chat(self, session, payload: bytes):
        try:
            msg_type = struct.unpack_from("<I", payload, 0)[0]
            _lang    = struct.unpack_from("<I", payload, 4)[0]
            # For SAY/YELL the message follows immediately
            msg_bytes = payload[8:]
            end = msg_bytes.find(b"\x00")
            if end < 0:
                log.debug("Chat: no null terminator found")
                return
            msg = msg_bytes[:end].decode("utf-8", errors="replace")
        except Exception as e:
            log.debug(f"Chat parse error: {e}")
            return

        log.info(f"Chat from {session.account}: type={msg_type} msg='{msg}'")

        if msg.startswith(".") and len(msg) > 1:
            # GM command — don't echo to world
            parts = msg[1:].split()
            cmd, args = parts[0], parts[1:]
            log.info(f"GM command: .{cmd} {args} (gm_level={session.gm_level})")
            if not self._server.dispatch_gm_command(session, cmd, args):
                session.send_sys_msg(
                    f"Unknown command '.{cmd}'. Type .help for a list."
                )
        else:
            # Normal chat — broadcast to all online players
            if session.char:
                log.info(f"SAY from {session.char['name']} (guid={session.char['id']}): '{msg}'")
                _broadcast_say(session, msg, msg_type)
            else:
                log.warning(f"Chat from {session.account} but session.char is None")

    # ── GM command implementations ────────────────────────────────────

    def _cmd_help(self, session, _args):
        cmds = sorted(self._server._gm_commands.items())
        visible = [(k, v) for k, v in cmds if v["min_gm"] <= session.gm_level
                   and v["help"] and not v["help"].startswith(".tel ")
                   and not v["help"].startswith(".ann ")]
        lines = ["Available GM commands:"]
        for k, v in visible:
            lines.append(f"  {v['help']}")
        session.send_sys_msg("\n".join(lines))

    def _cmd_info(self, session, _args):
        c = session.char
        if not c:
            session.send_sys_msg("Not in world.")
            return
        lines = [
            f"Name: {c['name']}  Level: {c['level']}",
            f"Race: {c['race']}  Class: {c['class']}  Gender: {c['gender']}",
            f"Map: {c['map']}  Zone: {c['zone']}",
            f"Pos: X={c['pos_x']:.2f} Y={c['pos_y']:.2f} Z={c['pos_z']:.2f}",
            f"Account: {session.account}  GM Level: {session.gm_level}",
        ]
        session.send_sys_msg("\n".join(lines))

    def _cmd_teleport(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .tele <name>  or  .tele <x> <y> <z> [map]")
            return
        # Named location?
        name = args[0].lower()
        if name in _NAMED_LOCATIONS:
            map_id, x, y, z, zone_id = _NAMED_LOCATIONS[name]
            teleport_player(session, map_id, x, y, z, 0.0, zone_id=zone_id)
            return
        # Coordinate teleport
        if len(args) < 3:
            session.send_sys_msg("Usage: .tele <name>  or  .tele <x> <y> <z> [map]")
            return
        try:
            x, y, z = float(args[0]), float(args[1]), float(args[2])
            map_id = int(args[3]) if len(args) > 3 else (session.char["map"] if session.char else 0)
        except ValueError:
            session.send_sys_msg("Invalid coordinates. Usage: .tele <x> <y> <z> [map]")
            return
        teleport_player(session, map_id, x, y, z, 0.0)

    def _cmd_level(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .level <n>")
            return
        try:
            lvl = max(1, min(60, int(args[0])))
        except ValueError:
            session.send_sys_msg("Level must be a number 1-60.")
            return
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        set_char_level(session.db_path, session.char["id"], lvl)
        session.char = dict(session.char)
        session.char["level"] = lvl
        # Send values update
        session._send(SMSG_UPDATE_OBJECT,
                      _build_values_update(session.char["id"], {UNIT_FIELD_LEVEL: lvl}))
        session.send_sys_msg(f"Level set to {lvl}.")

    def _cmd_heal(self, session, _args):
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        session._send(SMSG_UPDATE_OBJECT,
                      _build_values_update(session.char["id"],
                                           {UNIT_FIELD_HEALTH: 100,
                                            UNIT_FIELD_MAXHEALTH: 100}))
        session.send_sys_msg("Healed.")

    def _cmd_speed(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .speed <multiplier>  (e.g. 2.0)")
            return
        try:
            mult = max(0.1, min(50.0, float(args[0])))
        except ValueError:
            session.send_sys_msg("Speed must be a number.")
            return
        guid = session.char["id"] if session.char else 0
        run_speed = 7.0 * mult
        swim_speed = 4.7222 * mult
        # SMSG_FORCE_RUN_SPEED_CHANGE
        buf = ByteBuffer()
        buf.raw(pack_guid(guid))
        buf.uint32(0)
        buf.float32(run_speed)
        session._send(SMSG_FORCE_RUN_SPEED_CHANGE, buf.bytes())
        # SMSG_FORCE_SWIM_SPEED_CHANGE — affects fly speed in swim-in-air mode
        buf = ByteBuffer()
        buf.raw(pack_guid(guid))
        buf.uint32(0)
        buf.float32(swim_speed)
        session._send(SMSG_FORCE_SWIM_SPEED_CHANGE, buf.bytes())
        session.send_sys_msg(f"Speed set to {mult}x.")

    def _cmd_announce(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .announce <message>")
            return
        msg = " ".join(args)
        msg_bytes = msg.encode("utf-8")
        buf = ByteBuffer()
        buf.uint8(CHAT_MSG_SYSTEM)
        buf.uint32(0)                     # language
        buf.uint64(0)                     # GUID1 = 0 (no sender)
        buf.uint64(0)                     # GUID2 = 0 (no sender)
        buf.uint32(len(msg_bytes) + 1)   # message length
        buf.raw(msg_bytes + b"\x00")     # message + null
        buf.uint8(0)                      # chat tag
        data = buf.bytes()
        self._server.broadcast(SMSG_MESSAGECHAT, data)
        log.info(f"[ANNOUNCE] {msg}")

    def _cmd_kick(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .kick <player_name>")
            return
        target = self._server.get_session(args[0])
        if not target:
            session.send_sys_msg(f"Player '{args[0]}' not found online.")
            return
        target.transport.close()
        session.send_sys_msg(f"Kicked {args[0]}.")

    def _cmd_setgm(self, session, args):
        if len(args) < 2:
            session.send_sys_msg("Usage: .setgm <account_name> <level (0-3)>")
            return
        try:
            lvl = int(args[1])
        except ValueError:
            session.send_sys_msg("Level must be 0-3.")
            return
        set_gm_level(session.db_path, args[0], lvl)
        # Update live session if online
        target = self._server.get_session(args[0])
        if target:
            target.gm_level = lvl
        session.send_sys_msg(f"Set GM level of '{args[0]}' to {lvl}.")

    def _cmd_players(self, session, _args):
        online = self._server.get_online_players()
        if not online:
            session.send_sys_msg("No players online.")
            return
        lines = [f"Online players ({len(online)}):"]
        for s in online:
            char = s.char
            if char:
                lines.append(f"  {char['name']} (lvl {char['level']}) — "
                              f"map {char['map']} {char['pos_x']:.0f},{char['pos_y']:.0f}")
            else:
                lines.append(f"  {s.account} (char select)")
        session.send_sys_msg("\n".join(lines))

    def _cmd_setpos(self, session, args):
        if len(args) < 4:
            session.send_sys_msg("Usage: .setpos <char> <x> <y> <z>")
            return
        try:
            x, y, z = float(args[1]), float(args[2]), float(args[3])
        except ValueError:
            session.send_sys_msg("Coordinates must be numbers.")
            return
        char = get_character_by_name(session.db_path, args[0])
        if not char:
            session.send_sys_msg(f"Character '{args[0]}' not found.")
            return
        update_char_position(session.db_path, char["id"],
                             char["map"], x, y, z, 0.0)
        session.send_sys_msg(f"Set {char['name']} position to {x:.1f},{y:.1f},{z:.1f}.")

    # ── .fly on/off ──────────────────────────────────────────────────

    # Movement flags for "swim in air" fly mode (vanilla 1.12.1 technique)
    _FLY_FLAGS = 0x00000400 | 0x00200000 | 0x01000000  # LEVITATING | SWIMMING | FLYING

    def _send_fly_heartbeat(self, session, move_flags):
        """Send MSG_MOVE_HEARTBEAT with given move flags to self + nearby players.
        This is how MaNGOS/VMaNGOS implement GM fly: overwrite movement flags
        and broadcast a heartbeat so the client enters swimming-in-air mode."""
        from opcodes import MSG_MOVE_HEARTBEAT
        char = session.char
        guid = char["id"]

        buf = ByteBuffer()
        buf.raw(pack_guid(guid))
        buf.uint32(move_flags)
        buf.uint32(int(time.time() * 1000) & 0xFFFFFFFF)  # timestamp
        buf.float32(float(char["pos_x"]))
        buf.float32(float(char["pos_y"]))
        buf.float32(float(char["pos_z"]))
        buf.float32(float(char["orientation"]))
        # When MOVEFLAG_SWIMMING (0x00200000) is set, client expects a pitch float
        if move_flags & 0x00200000:
            buf.float32(0.0)  # pitch
        buf.uint32(0)  # fall time

        data = buf.bytes()
        # Send to self
        session._send(MSG_MOVE_HEARTBEAT, data)
        # Broadcast to other nearby players
        for s in self._server.get_online_players():
            if s is not session and s.char:
                s._send(MSG_MOVE_HEARTBEAT, data)

    def _cmd_fly(self, session, args):
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        if not args:
            session.send_sys_msg("Usage: .fly <on|off>")
            return

        sub = args[0].lower()
        guid = session.char["id"]

        if sub == "on":
            # Boost swim speed for faster flight
            buf = ByteBuffer()
            buf.raw(pack_guid(guid))
            buf.uint32(0)
            buf.float32(4.7222 * 3.0)  # 3x normal swim speed
            session._send(SMSG_FORCE_SWIM_SPEED_CHANGE, buf.bytes())

            # Send heartbeat with fly flags — puts client into swim-in-air mode
            self._send_fly_heartbeat(session, self._FLY_FLAGS)

            # Reject client movement packets briefly to prevent flag overwrite
            session._fly_mode = True
            session._fly_reject_until = time.time() + 0.15
            session.send_sys_msg("Fly mode ON. Do not jump or it will break.")

        elif sub == "off":
            # Send heartbeat with no flags — back to normal
            self._send_fly_heartbeat(session, 0)

            # Reset swim speed
            buf = ByteBuffer()
            buf.raw(pack_guid(guid))
            buf.uint32(0)
            buf.float32(4.7222)  # normal swim speed
            session._send(SMSG_FORCE_SWIM_SPEED_CHANGE, buf.bytes())

            session._fly_mode = False
            session._fly_reject_until = 0
            session.send_sys_msg("Fly mode OFF.")

        else:
            session.send_sys_msg("Usage: .fly <on|off>")

    # ── .gold ────────────────────────────────────────────────────────

    def _cmd_gold(self, session, args):
        if not session.char:
            session.send_sys_msg("Not in world.")
            return
        if not args:
            session.send_sys_msg("Usage: .gold <amount|cap|reset>")
            session.send_sys_msg("  amount: e.g. 10g, 5g30s, 1g27s19c, 50s, 99c")
            return

        # Resolve target: selected player or self
        target_session = session
        target_guid = getattr(session, "target_guid", 0)
        if target_guid and target_guid != session.char["id"]:
            # Check if target is an online player
            for s in self._server.get_online_players():
                if s.char and s.char["id"] == target_guid:
                    target_session = s
                    break

        if not target_session.char:
            session.send_sys_msg("Target not in world.")
            return

        char_id = target_session.char["id"]
        char_name = target_session.char["name"]
        current = get_char_money(session.db_path, char_id)

        sub = args[0].lower()
        if sub == "cap":
            new_money = _GOLD_CAP
        elif sub == "reset":
            new_money = 0
        else:
            # Parse amount like "1g27s19c", "10g", "50s", "99c"
            amount = _parse_gold(args[0])
            if amount is None:
                session.send_sys_msg("Invalid amount. Examples: 10g, 5g30s, 1g27s19c, 50s, 99c")
                return
            new_money = min(current + amount, _GOLD_CAP)

        set_char_money(session.db_path, char_id, new_money)

        # Update the live session's char dict
        target_session.char = dict(target_session.char)
        target_session.char["money"] = new_money

        # Send SMSG_UPDATE_OBJECT to update coinage on the client
        target_session._send(
            SMSG_UPDATE_OBJECT,
            _build_values_update(target_session.char["id"],
                                 {PLAYER_FIELD_COINAGE: new_money & 0xFFFFFFFF}))

        g, s, c = new_money // 10000, (new_money % 10000) // 100, new_money % 100
        if target_session is session:
            session.send_sys_msg(f"Gold set to {g}g {s}s {c}c.")
        else:
            session.send_sys_msg(f"Set {char_name}'s gold to {g}g {s}s {c}c.")


def _parse_gold(text: str) -> int | None:
    """Parse a gold string like '1g27s19c' into copper. Returns None on failure."""
    text = text.lower().strip()
    # Try pattern: optional gold, optional silver, optional copper
    m = re.fullmatch(r"(?:(\d+)g)?(?:(\d+)s)?(?:(\d+)c)?", text)
    if not m or not any(m.groups()):
        # Plain number = treat as gold
        try:
            return int(text) * 10000
        except ValueError:
            return None
    gold = int(m.group(1) or 0)
    silver = int(m.group(2) or 0)
    copper = int(m.group(3) or 0)
    return gold * 10000 + silver * 100 + copper


def _broadcast_say(session, msg: str, msg_type: int):
    """Broadcast player chat to all online players.
    Vanilla 1.12 SMSG_MESSAGECHAT true layout (verified from client name-query behaviour):
      type(u8) + lang(u32) + GUID1(u64) + GUID2(u64) + msglen(u32) + msg + tag(u8)
    The client issues CMSG_NAME_QUERY on GUID2 to resolve the display name.
    For SAY/YELL/EMOTE both GUIDs must be the sender's GUID.
    """
    guid = session.char["id"] if session.char else 0
    msg_bytes = msg.encode("utf-8")
    buf = ByteBuffer()
    buf.uint8(msg_type & 0xFF)        # chat type
    buf.uint32(0)                     # language (LANG_UNIVERSAL)
    buf.uint64(guid)                  # GUID1 – sender (context / target)
    buf.uint64(guid)                  # GUID2 – sender again; client name-queries THIS
    buf.uint32(len(msg_bytes) + 1)   # message length (including null)
    buf.raw(msg_bytes + b"\x00")     # message + null terminator
    buf.uint8(0)                      # chat tag
    data = buf.bytes()
    sessions = session.server.get_online_players()
    log.info(f"SMSG_MESSAGECHAT type={msg_type} guid={guid} msg='{msg}' "
             f"payload_hex={data.hex()} sessions={len(sessions)}")
    for s in sessions:
        s._send(SMSG_MESSAGECHAT, data)
