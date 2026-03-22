"""GM command system: parses in-game chat '.' commands and routes them.
   Also provides teleport, level, speed, announce, kick, info, etc."""
import logging
import struct
import time

from modules.base import BaseModule
from opcodes import (CMSG_MESSAGECHAT, SMSG_MESSAGECHAT, SMSG_UPDATE_OBJECT)
from packets import ByteBuffer, pack_guid
from database import (get_character_by_name, set_gm_level, set_char_level,
                      update_char_position)
from modules.core_world import teleport_player

log = logging.getLogger("gm")

CHAT_MSG_SAY    = 0
CHAT_MSG_YELL   = 5
CHAT_MSG_SYSTEM = 10

# Update field constants — vanilla 1.12.1 (build 5875)
UNIT_FIELD_HEALTH  = 0x0016
UNIT_FIELD_MAXHEALTH = 0x001C
UNIT_FIELD_LEVEL   = 0x0022


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
                    help_text=".teleport <x> <y> <z> [map]  — teleport", min_gm=1)
        self.reg_gm(server, "tel",       self._cmd_teleport,
                    help_text=".tel <x> <y> <z> [map]", min_gm=1)
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
            # Normal chat — broadcast back as SAY to self (simple echo)
            # A full impl would broadcast to nearby players; for now just echo
            if session.char:
                _broadcast_say(session, msg, msg_type)

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
        if len(args) < 3:
            session.send_sys_msg("Usage: .teleport <x> <y> <z> [map]")
            return
        try:
            x, y, z = float(args[0]), float(args[1]), float(args[2])
            map_id = int(args[3]) if len(args) > 3 else (session.char["map"] if session.char else 0)
        except ValueError:
            session.send_sys_msg("Arguments must be numbers.")
            return
        buf = ByteBuffer()
        buf.uint32(map_id)
        buf.float32(x); buf.float32(y); buf.float32(z); buf.float32(0.0)
        session._send(SMSG_NEW_WORLD, buf.bytes())
        if session.char:
            update_char_position(session.db_path, session.char["id"],
                                 map_id, x, y, z, 0.0)
            # Update local cache
            session.char = dict(session.char)
            session.char.update({"map": map_id, "pos_x": x, "pos_y": y,
                                  "pos_z": z, "orientation": 0.0})
        session.send_sys_msg(f"Teleported to {x:.1f}, {y:.1f}, {z:.1f} (map {map_id}).")

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
        speed = 7.0 * mult
        # SMSG_FORCE_RUN_SPEED_CHANGE (0x0E2)
        buf = ByteBuffer()
        buf.raw(pack_guid(session.char["id"] if session.char else 0))
        buf.uint32(0)           # move counter
        buf.float32(speed)
        session._send(0x0E2, buf.bytes())
        session.send_sys_msg(f"Speed set to {mult}x ({speed:.1f}).")

    def _cmd_announce(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .announce <message>")
            return
        msg = " ".join(args)
        msg_bytes = msg.encode("utf-8")
        buf = ByteBuffer()
        buf.uint8(CHAT_MSG_SYSTEM)
        buf.uint32(0)                     # language
        buf.uint64(0)                     # sender guid (system)
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


def _broadcast_say(session, msg: str, msg_type: int):
    """Echo player's own message back as SAY (basic self-echo).
    Vanilla 1.12 SAY format: type(u8) + lang(u32) + senderGUID(u64) + msglen(u32) + msg + tag(u8)
    """
    guid = session.char["id"] if session.char else 0
    msg_bytes = msg.encode("utf-8")
    buf = ByteBuffer()
    buf.uint8(CHAT_MSG_SAY)
    buf.uint32(0)                     # language
    buf.uint64(guid)                  # sender guid
    buf.uint32(len(msg_bytes) + 1)   # message length
    buf.raw(msg_bytes + b"\x00")     # message + null
    buf.uint8(0)                      # chat tag
    session._send(SMSG_MESSAGECHAT, buf.bytes())
