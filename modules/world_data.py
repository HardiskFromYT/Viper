"""World data module — queries world.db and sends live world state to players.

On load:
  • Opens world.db (read-only connection pool)
  • Hooks player login to send nearby creatures + game objects
  • Registers CLI commands: creature, item, quest, tele, npc, loot, spawn
  • Registers GM commands: .tele, .npc info, .lookup
  • Updates playercreateinfo so new characters get correct starting gear/spells

Reload this module (reload world_data) after re-importing world.db to pick up
new data without restarting the server.
"""
import logging
import os
import sqlite3
import struct
import time

from modules.base import BaseModule
from opcodes import (SMSG_UPDATE_OBJECT, SMSG_DESTROY_OBJECT, SMSG_MOTD,
                     MSG_MOVE_WORLDPORT_ACK, MSG_MOVE_TELEPORT_ACK)
from packets import ByteBuffer, pack_guid

log = logging.getLogger("world_data")

WORLD_DB = os.path.join(os.path.dirname(__file__), "..", "world.db")

# ── World DB connection (read-only) ───────────────────────────────────────────

_wconn: sqlite3.Connection | None = None


def wdb() -> sqlite3.Connection:
    global _wconn
    if _wconn is None:
        if not os.path.exists(WORLD_DB):
            raise RuntimeError(f"world.db not found at {WORLD_DB}. Run: python3 import_world.py")
        _wconn = sqlite3.connect(f"file:{WORLD_DB}?mode=ro", uri=True,
                                 check_same_thread=False)
        _wconn.row_factory = sqlite3.Row
    return _wconn


def close_wdb():
    global _wconn
    if _wconn:
        _wconn.close()
        _wconn = None


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_item_template(entry: int):
    return wdb().execute("SELECT * FROM item_template WHERE entry=?", (entry,)).fetchone()


def search_items(name: str, limit=20):
    return wdb().execute(
        "SELECT entry,name,Quality,ItemLevel,RequiredLevel,class,subclass "
        "FROM item_template WHERE name LIKE ? LIMIT ?",
        (f"%{name}%", limit)
    ).fetchall()


def get_creature_template(entry: int):
    return wdb().execute("SELECT * FROM creature_template WHERE Entry=?", (entry,)).fetchone()


def search_creatures(name: str, limit=20):
    return wdb().execute(
        "SELECT Entry,Name,MinLevel,MaxLevel,CreatureType FROM creature_template WHERE Name LIKE ? LIMIT ?",
        (f"%{name}%", limit)
    ).fetchall()


def get_creatures_near(map_id: int, x: float, y: float, radius: float = 200.0):
    """Get creature spawns within radius on a given map."""
    return wdb().execute(
        """SELECT c.guid, c.id, c.position_x, c.position_y, c.position_z,
                  c.orientation, t.Name, t.MinLevel, t.MaxLevel,
                  t.ModelId1, t.Scale,
                  t.MinLevelHealth, t.MaxLevelHealth,
                  t.MinMeleeDmg, t.MaxMeleeDmg, t.MeleeBaseAttackTime,
                  t.MeleeAttackPower, t.Armor, t.DamageMultiplier,
                  t.DamageVariance, t.FactionAlliance, t.NpcFlags,
                  t.HealthMultiplier, t.UnitFlags
           FROM creature c
           JOIN creature_template t ON c.id = t.Entry
           WHERE c.map = ?
             AND c.position_x BETWEEN ? AND ?
             AND c.position_y BETWEEN ? AND ?
           LIMIT 100""",
        (map_id, x - radius, x + radius, y - radius, y + radius)
    ).fetchall()


def get_gameobjects_near(map_id: int, x: float, y: float, radius: float = 200.0):
    return wdb().execute(
        """SELECT g.guid, g.id, g.position_x, g.position_y, g.position_z,
                  g.orientation, g.rotation0, g.rotation1, g.rotation2, g.rotation3,
                  t.name, t.type, t.displayId
           FROM gameobject g
           JOIN gameobject_template t ON g.id = t.entry
           WHERE g.map = ?
             AND g.position_x BETWEEN ? AND ?
             AND g.position_y BETWEEN ? AND ?
           LIMIT 200""",
        (map_id, x - radius, x + radius, y - radius, y + radius)
    ).fetchall()


def get_quest(entry: int):
    return wdb().execute("SELECT * FROM quest_template WHERE entry=?", (entry,)).fetchone()


def search_quests(name: str, limit=20):
    return wdb().execute(
        "SELECT entry,Title,MinLevel,QuestLevel FROM quest_template WHERE Title LIKE ? LIMIT ?",
        (f"%{name}%", limit)
    ).fetchall()


def get_tele(name: str):
    return wdb().execute(
        "SELECT * FROM game_tele WHERE name LIKE ? LIMIT 1", (f"%{name}%",)
    ).fetchone()


def search_tele(name: str, limit=15):
    return wdb().execute(
        "SELECT id,name,map,position_x,position_y,position_z FROM game_tele "
        "WHERE name LIKE ? LIMIT ?",
        (f"%{name}%", limit)
    ).fetchall()


def get_vendor_items(npc_entry: int):
    return wdb().execute(
        """SELECT v.item, v.maxcount, v.incrtime, v.ExtendedCost,
                  i.name, i.Quality, i.BuyPrice
           FROM npc_vendor v
           LEFT JOIN item_template i ON v.item = i.entry
           WHERE v.entry = ? ORDER BY i.name""",
        (npc_entry,)
    ).fetchall()


def get_npc_trainer(npc_entry: int, limit=30):
    return wdb().execute(
        "SELECT * FROM npc_trainer WHERE entry=? LIMIT ?",
        (npc_entry, limit)
    ).fetchall()


def get_creature_loot(creature_entry: int):
    return wdb().execute(
        """SELECT cl.item, cl.ChanceOrQuestChance, cl.groupid, cl.mincountOrRef, cl.maxcount,
                  i.name, i.Quality
           FROM creature_loot_template cl
           LEFT JOIN item_template i ON cl.item = i.entry
           WHERE cl.entry = ?
           ORDER BY cl.ChanceOrQuestChance DESC LIMIT 30""",
        (creature_entry,)
    ).fetchall()


def get_playercreateinfo(race: int, cls: int):
    return wdb().execute(
        "SELECT * FROM playercreateinfo WHERE race=? AND class=?", (race, cls)
    ).fetchone()


def get_player_xp_for_level(level: int):
    row = wdb().execute(
        "SELECT xp_for_next_level FROM player_xp_for_level WHERE lvl=?", (level,)
    ).fetchone()
    return row[0] if row else 0


def get_player_levelstats(race: int, cls: int, level: int):
    row = wdb().execute(
        "SELECT * FROM player_levelstats WHERE race=? AND class=? AND level=?",
        (race, cls, level)
    ).fetchone()
    return row


def get_class_levelstats(cls: int, level: int):
    row = wdb().execute(
        "SELECT * FROM player_classlevelstats WHERE class=? AND level=?",
        (cls, level)
    ).fetchone()
    return row


# ── Creature SMSG_UPDATE_OBJECT builder ───────────────────────────────────────

# Update field indices for creatures — vanilla 1.12.1 (build 5875)
_UNIT_GUID        = 0x0000
_UNIT_TYPE        = 0x0002
_UNIT_ENTRY       = 0x0003   # OBJECT_FIELD_ENTRY — creature template ID
_UNIT_SCALE_X     = 0x0004
_UNIT_HEALTH      = 0x0016
_UNIT_MAXHEALTH   = 0x001C
_UNIT_LEVEL       = 0x0022
_UNIT_FACTION_T   = 0x0023
_UNIT_BYTES_0     = 0x0024
_UNIT_NPC_FLAGS   = 0x0093
_UNIT_FLAGS       = 0x002E
_UNIT_BOUNDING    = 0x0081
_UNIT_COMBATREACH = 0x0082
_UNIT_DISPLAYID   = 0x0083
_UNIT_NATIVEDID   = 0x0084
_UNIT_END         = 0x00C0  # creature fields end before player fields


def _build_creature_update(spawn_guid: int, template) -> bytes:
    """Build a CREATE_OBJECT block for a single creature (unused helper)."""
    level  = max(1, (template["MinLevel"] + template["MaxLevel"]) // 2)
    hp     = int(template["MinLevelHealth"] or level * 10) or level * 10
    model  = template["ModelId1"] or 1
    scale  = float(template["Scale"] or 1.0)

    full_guid = spawn_guid

    entry = int(template["Entry"])
    fields = {
        _UNIT_GUID:        full_guid & 0xFFFFFFFF,
        _UNIT_GUID + 1:    (full_guid >> 32) & 0xFFFFFFFF,
        _UNIT_TYPE:        0x09,
        _UNIT_ENTRY:       entry,
        _UNIT_SCALE_X:     _f2i(scale),
        _UNIT_HEALTH:      hp,
        _UNIT_MAXHEALTH:   hp,
        _UNIT_LEVEL:       level,
        _UNIT_FACTION_T:   template.get("FactionAlliance") or 14,
        _UNIT_NPC_FLAGS:   int(template["NpcFlags"] or 0) if "NpcFlags" in template.keys() else 0,
        _UNIT_BOUNDING:    _f2i(0.389),
        _UNIT_COMBATREACH: _f2i(1.5),
        _UNIT_DISPLAYID:   model,
        _UNIT_NATIVEDID:   model,
    }

    max_field = max(fields.keys()) + 1
    num_blocks = (max_field + 31) // 32
    mask_words = [0] * num_blocks
    field_data = bytearray()
    for idx in sorted(fields):
        mask_words[idx // 32] |= 1 << (idx % 32)
    for idx in sorted(fields):
        field_data += struct.pack("<I", int(fields[idx]) & 0xFFFFFFFF)

    return full_guid, fields, mask_words, num_blocks, field_data


def _f2i(f):
    """Float to uint32 bits."""
    return struct.unpack("<I", struct.pack("<f", f))[0]


def build_creatures_packet(spawns, session=None) -> bytes | None:
    """Build a single SMSG_UPDATE_OBJECT containing all nearby creatures.
    If session is provided, filters out creatures the player shouldn't see
    (e.g. Spirit Healers are only visible to ghost players)."""
    if not spawns:
        return None

    objects = []
    for spawn in spawns:
        # Spirit Healer visibility filter
        if session:
            try:
                from modules.combat import should_see_creature
                if not should_see_creature(session, int(spawn["id"])):
                    continue
            except ImportError:
                pass
        guid = spawn["guid"] + 0x100000000  # shift into creature GUID range
        tpl  = get_creature_template(spawn["id"])
        if not tpl:
            continue

        level  = max(1, (int(tpl["MinLevel"] or 1) + int(tpl["MaxLevel"] or 1)) // 2)
        model  = int(tpl["ModelId1"] or 1)
        if model == 0:
            model = int(tpl["ModelId2"] or 0) or 1
        scale  = float(tpl["Scale"] or 1.0)
        faction = int(tpl["FactionAlliance"] or 14)
        hp     = int(tpl["MinLevelHealth"] or level * 10) or level * 10

        entry = int(spawn["id"])
        fields = {
            _UNIT_GUID:        guid & 0xFFFFFFFF,
            _UNIT_GUID + 1:    (guid >> 32) & 0xFFFFFFFF,
            _UNIT_TYPE:        0x09,         # OBJECT | UNIT
            _UNIT_ENTRY:       entry,        # creature template ID — client queries name from this
            _UNIT_SCALE_X:     _f2i(scale),
            _UNIT_HEALTH:      hp,
            _UNIT_MAXHEALTH:   hp,
            _UNIT_LEVEL:       level,
            _UNIT_FACTION_T:   faction,
            _UNIT_NPC_FLAGS:   int(tpl["NpcFlags"] or 0),
            _UNIT_BYTES_0:     0,            # race/class/gender/power — 0 for NPCs
            _UNIT_BOUNDING:    _f2i(0.389),
            _UNIT_COMBATREACH: _f2i(1.5),
            _UNIT_DISPLAYID:   model,
            _UNIT_NATIVEDID:   model,
        }

        max_field = max(fields.keys()) + 1
        num_blocks = (max_field + 31) // 32
        mask_words = [0] * num_blocks
        field_data = bytearray()
        for idx in sorted(fields):
            mask_words[idx // 32] |= 1 << (idx % 32)
        for idx in sorted(fields):
            field_data += struct.pack("<I", int(fields[idx]) & 0xFFFFFFFF)

        # Movement block
        mv = ByteBuffer()
        mv.uint32(0)   # move flags
        mv.uint32(0)  # timestamp (creatures are static, no prior movement)
        mv.float32(float(spawn["position_x"]))
        mv.float32(float(spawn["position_y"]))
        mv.float32(float(spawn["position_z"]))
        mv.float32(float(spawn["orientation"] or 0.0))
        mv.uint32(0)          # fall time
        mv.float32(2.5)       # walk speed
        mv.float32(7.0)       # run speed
        mv.float32(4.5)       # run back
        mv.float32(4.722222)  # swim
        mv.float32(2.5)       # swim back
        mv.float32(3.141593)  # turn speed

        obj = ByteBuffer()
        obj.raw(pack_guid(guid))
        obj.uint8(3)     # object type: UNIT
        obj.uint8(0x60)  # UPDATEFLAG_LIVING(0x20) | HAS_POSITION(0x40)
        obj.raw(mv.bytes())
        obj.uint8(num_blocks)
        for w in mask_words:
            obj.uint32(w)
        obj.raw(field_data)
        objects.append(obj.bytes())

    if not objects:
        return None

    pkt = ByteBuffer()
    pkt.uint32(len(objects))
    pkt.uint8(0)   # has_transport
    for o_bytes in objects:
        pkt.uint8(2)   # update_type: CREATE_OBJECT (not 3/CREATE_OBJECT2)
        pkt.raw(o_bytes)

    return pkt.bytes()


# ── Visibility system ─────────────────────────────────────────────────────────
# Spawn creatures within this radius; despawn only past the larger radius.
# The gap ensures the player never sees creatures pop in/out.
_VIS_SPAWN_RADIUS  = 150.0
_VIS_DESPAWN_RADIUS = 300.0

SMSG_DESTROY_OBJECT = 0x00AA


def _init_session_visibility(session):
    """Initialise per-session visibility tracking if not already set."""
    if not hasattr(session, "_known_creatures"):
        session._known_creatures = set()       # creature GUIDs sent to client
        session._known_positions = {}           # guid -> (x, y) for distance check
        session._spawn_center = (None, 0, 0)   # (map, x, y) of last query


def _send_destroy(session, guid: int):
    """Send SMSG_DESTROY_OBJECT for one creature GUID."""
    session._send(SMSG_DESTROY_OBJECT, struct.pack("<Q", guid))


def update_visibility(session):
    """Re-query creatures near the player; send new ones, despawn far ones.

    Called periodically from the movement handler when the player has moved
    far enough from the last spawn center.
    """
    if not session.char:
        return
    _init_session_visibility(session)

    char   = session.char
    map_id = char["map"]
    px, py = float(char["pos_x"]), float(char["pos_y"])

    # Query creatures within the spawn radius
    try:
        spawns = get_creatures_near(map_id, px, py, radius=_VIS_SPAWN_RADIUS)
    except Exception as e:
        log.error(f"Visibility query error: {e}")
        return

    near_guids = set()
    new_spawns = []
    for s in (spawns or []):
        guid = s["guid"] + 0x100000000
        # Filter out creatures the player shouldn't see (e.g. Spirit Healers)
        try:
            from modules.combat import should_see_creature
            if not should_see_creature(session, int(s["id"])):
                continue
        except ImportError:
            pass
        near_guids.add(guid)
        if guid not in session._known_creatures:
            new_spawns.append(s)
        # Always refresh position for distance checks
        session._known_positions[guid] = (float(s["position_x"]), float(s["position_y"]))

    # Send new creatures
    if new_spawns:
        try:
            pkt = build_creatures_packet(new_spawns, session=session)
            if pkt:
                session._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception as e:
            log.error(f"Visibility spawn error: {e}")

    # Despawn creatures that are beyond the despawn radius
    to_remove = []
    for guid in session._known_creatures:
        pos = session._known_positions.get(guid)
        if not pos:
            continue
        dx = px - pos[0]
        dy = py - pos[1]
        if (dx * dx + dy * dy) > _VIS_DESPAWN_RADIUS * _VIS_DESPAWN_RADIUS:
            to_remove.append(guid)

    for guid in to_remove:
        try:
            _send_destroy(session, guid)
        except Exception:
            pass
        session._known_creatures.discard(guid)
        session._known_positions.pop(guid, None)

    # Update known set (add new, keep existing that aren't despawned)
    session._known_creatures |= near_guids
    session._spawn_center = (map_id, px, py)


# ── Player-to-player visibility ──────────────────────────────────────────────

def _init_known_players(session):
    """Initialise per-session player visibility tracking."""
    if not hasattr(session, "_known_players"):
        session._known_players = set()   # player GUIDs we've sent CREATE for


def send_nearby_players(session, server):
    """Send CREATE_OBJECT for all other online players to this session,
    and send this session's CREATE_OBJECT to all other players."""
    from modules.core_world import build_other_player_object
    _init_known_players(session)
    char = session.char
    if not char:
        return

    for other in server.get_online_players():
        if other is session or not other.char:
            continue
        other_char = other.char
        _init_known_players(other)

        # Send the other player to us
        if other_char["id"] not in session._known_players:
            try:
                session._send(SMSG_UPDATE_OBJECT,
                              build_other_player_object(other_char))
                session._known_players.add(other_char["id"])
            except Exception as e:
                log.debug(f"Error sending player {other_char['name']} to {char['name']}: {e}")

        # Send us to the other player
        if char["id"] not in other._known_players:
            try:
                other._send(SMSG_UPDATE_OBJECT,
                            build_other_player_object(char))
                other._known_players.add(char["id"])
            except Exception as e:
                log.debug(f"Error sending player {char['name']} to {other_char['name']}: {e}")


def destroy_player_for_others(session, server):
    """Send SMSG_DESTROY_OBJECT for this player to all other sessions."""
    if not session.char:
        return
    guid = session.char["id"]
    data = struct.pack("<Q", guid)
    for other in server.get_online_players():
        if other is session or not other.char:
            continue
        try:
            other._send(SMSG_DESTROY_OBJECT, data)
            if hasattr(other, "_known_players"):
                other._known_players.discard(guid)
        except Exception:
            pass


def broadcast_movement(session, opcode, payload):
    """Forward a movement packet from one player to all other nearby players.
    Format: PackedGUID + raw MovementInfo (client payload forwarded as-is)."""
    if not session.char:
        return
    guid = session.char["id"]
    data = pack_guid(guid) + payload
    server = getattr(session, "server", None)
    if not server:
        return
    for other in server.get_online_players():
        if other is session or not other.char:
            continue
        try:
            other._send(opcode, data)
        except Exception:
            pass


# ── Module ────────────────────────────────────────────────────────────────────

class Module(BaseModule):
    name = "world_data"

    def on_load(self, server):
        self._server = server
        close_wdb()   # force fresh connection on reload

        if not os.path.exists(WORLD_DB):
            log.warning("world.db not found — run: python3 import_world.py")
            self._register_cli(server)
            return

        # Quick sanity check
        try:
            n = wdb().execute("SELECT COUNT(*) FROM creature_template").fetchone()[0]
            log.info(f"world_data loaded: {n:,} creature templates.")
        except Exception as e:
            log.error(f"world.db open error: {e}")
            return

        self._register_cli(server)
        self._register_gm(server)

        # Hook into player login via packet handler override
        # We register an ADDITIONAL handler for CMSG_PLAYER_LOGIN that
        # fires AFTER core_world already sent the base packets
        from opcodes import CMSG_PLAYER_LOGIN
        self.reg_packet(server, CMSG_PLAYER_LOGIN, self._on_player_login_hook)

        # Also re-send nearby creatures after every teleport so NPCs appear
        # immediately without requiring a relog.
        self.reg_packet(server, MSG_MOVE_WORLDPORT_ACK,  self._on_teleport_hook)
        self.reg_packet(server, MSG_MOVE_TELEPORT_ACK,   self._on_teleport_hook)

        log.info("world_data module loaded.")

    def on_unload(self, server):
        close_wdb()
        log.info("world_data unloaded.")

    # ── player login hook ─────────────────────────────────────────────

    def _on_player_login_hook(self, session, payload: bytes):
        """Send nearby creatures/objects after core_world already handled login."""
        if not session.char:
            return
        char = session.char
        map_id = char["map"]
        x, y   = float(char["pos_x"]), float(char["pos_y"])

        # Initialise visibility tracking
        _init_session_visibility(session)
        session._known_creatures.clear()
        session._known_positions.clear()
        session._spawn_center = (map_id, x, y)

        # Send nearby creatures and populate known set
        try:
            spawns = get_creatures_near(map_id, x, y, radius=_VIS_SPAWN_RADIUS)
            if spawns:
                pkt = build_creatures_packet(spawns, session=session)
                if pkt:
                    session._send(SMSG_UPDATE_OBJECT, pkt)
                for s in spawns:
                    guid = s["guid"] + 0x100000000
                    # Track known creatures (skip spirit healers if alive)
                    try:
                        from modules.combat import should_see_creature
                        if not should_see_creature(session, int(s["id"])):
                            continue
                    except ImportError:
                        pass
                    session._known_creatures.add(guid)
                    session._known_positions[guid] = (
                        float(s["position_x"]), float(s["position_y"]))
                log.debug(f"Sent {len(spawns)} creatures to {char['name']}")
        except Exception as e:
            log.error(f"Error sending creatures: {e}")

        # Send other online players and announce ourselves to them
        try:
            send_nearby_players(session, self._server)
        except Exception as e:
            log.debug(f"Error sending nearby players: {e}")

        # Send MOTD with world info
        try:
            count_c = wdb().execute("SELECT COUNT(*) FROM creature").fetchone()[0]
            count_q = wdb().execute("SELECT COUNT(*) FROM quest_template").fetchone()[0]
            motd = (f"World loaded: {count_c:,} creature spawns, "
                    f"{count_q:,} quests. Type .help for GM commands.")
            buf = ByteBuffer(); buf.uint32(1); buf.cstring(motd)
            session._send(SMSG_MOTD, buf.bytes())
        except Exception:
            pass

    def _on_teleport_hook(self, session, _payload: bytes):
        """Re-send nearby creatures after any teleport (same-map or cross-map)."""
        if not session.char:
            return
        _init_session_visibility(session)

        # Destroy all previously known creatures (player is in a new area)
        for guid in list(session._known_creatures):
            try:
                _send_destroy(session, guid)
            except Exception:
                pass
        session._known_creatures.clear()
        session._known_positions.clear()

        char   = session.char
        map_id = char["map"]
        x, y   = float(char["pos_x"]), float(char["pos_y"])
        session._spawn_center = (map_id, x, y)

        try:
            spawns = get_creatures_near(map_id, x, y, radius=_VIS_SPAWN_RADIUS)
            if spawns:
                pkt = build_creatures_packet(spawns, session=session)
                if pkt:
                    session._send(SMSG_UPDATE_OBJECT, pkt)
                for s in spawns:
                    guid = s["guid"] + 0x100000000
                    try:
                        from modules.combat import should_see_creature
                        if not should_see_creature(session, int(s["id"])):
                            continue
                    except ImportError:
                        pass
                    session._known_creatures.add(guid)
                    session._known_positions[guid] = (
                        float(s["position_x"]), float(s["position_y"]))
                log.debug(f"Post-teleport: sent {len(spawns)} creatures to "
                          f"{char['name']} at map={map_id} ({x:.0f},{y:.0f})")
        except Exception as e:
            log.error(f"Error sending creatures after teleport: {e}")

        # Refresh player visibility after teleport
        try:
            _init_known_players(session)
            session._known_players.clear()
            send_nearby_players(session, self._server)
        except Exception as e:
            log.debug(f"Error refreshing player visibility after teleport: {e}")

    # ── CLI ───────────────────────────────────────────────────────────

    def _register_cli(self, server):
        self.reg_cli(server, "witem",    self._cli_item,
                     help_text="witem <name|id>  — look up world item template")
        self.reg_cli(server, "wcreature",self._cli_creature,
                     help_text="wcreature <name|id>  — look up creature template")
        self.reg_cli(server, "wquest",   self._cli_quest,
                     help_text="wquest <name|id>  — look up quest")
        self.reg_cli(server, "wtele",    self._cli_tele,
                     help_text="wtele <name>  — search teleport locations")
        self.reg_cli(server, "wnpc",     self._cli_npc,
                     help_text="wnpc <id>  — show NPC vendor/trainer/loot info")
        self.reg_cli(server, "wloot",    self._cli_loot,
                     help_text="wloot <creature_id>  — show creature loot table")
        self.reg_cli(server, "wspawns",  self._cli_spawns,
                     help_text="wspawns <map> <x> <y> [radius]  — list spawns near position")
        self.reg_cli(server, "reimport", self._cli_reimport,
                     help_text="reimport [table]  — re-run world DB import")

    def _register_gm(self, server):
        self.reg_gm(server, "tele",     self._gm_tele,
                    help_text=".tele <location_name>  — teleport to a named location", min_gm=1)
        self.reg_gm(server, "npcinfo",  self._gm_npcinfo,
                    help_text=".npcinfo <id>  — show full NPC template info", min_gm=1)
        self.reg_gm(server, "lookup",   self._gm_lookup,
                    help_text=".lookup item|creature|quest <name>  — search world DB", min_gm=1)
        self.reg_gm(server, "loot",     self._gm_loot,
                    help_text=".loot <creature_id>  — show loot table in chat", min_gm=1)

    # ── CLI handlers ──────────────────────────────────────────────────

    def _cli_item(self, args):
        if not args:
            return "Usage: witem <name or id>"
        try:
            entry = int(args[0])
            rows  = [get_item_template(entry)] if get_item_template(entry) else []
        except ValueError:
            rows = search_items(" ".join(args))
        if not rows or rows == [None]:
            return "  No items found."
        QUALITY = ["Poor","Common","Uncommon","Rare","Epic","Legendary","Artifact"]
        lines = [f"\n  {'ID':<7} {'Name':<40} {'Qual':<10} {'ilvl':<5} {'Req':<5}"]
        lines.append("  " + "─" * 70)
        for r in rows:
            if r is None: continue
            q = QUALITY[int(r["Quality"] or 0)] if int(r["Quality"] or 0) < len(QUALITY) else "?"
            lines.append(f"  {r['entry']:<7} {r['name']:<40} {q:<10} {r['ItemLevel']:<5} {r['RequiredLevel']:<5}")
        return "\n".join(lines)

    def _cli_creature(self, args):
        if not args:
            return "Usage: wcreature <name or id>"
        try:
            entry = int(args[0])
            t = get_creature_template(entry)
            rows = [t] if t else []
        except ValueError:
            rows = search_creatures(" ".join(args))
        if not rows or rows == [None]:
            return "  No creatures found."
        lines = [f"\n  {'ID':<7} {'Name':<30} {'Level':<12} {'Type':<5}"]
        lines.append("  " + "─" * 58)
        TYPES = {1:"Beast",2:"Dragon",3:"Demon",4:"Elemental",5:"Ghost",6:"Undead",7:"Human",10:"Mechanical",11:"Not specified"}
        for r in rows:
            if r is None: continue
            lvl   = f"{r['MinLevel']}-{r['MaxLevel']}"
            tname = TYPES.get(int(r.get("CreatureType") or 0), "?")
            lines.append(f"  {r['Entry']:<7} {r['Name']:<30} {lvl:<12} {tname}")
        return "\n".join(lines)

    def _cli_quest(self, args):
        if not args:
            return "Usage: wquest <name or id>"
        try:
            entry = int(args[0])
            q = get_quest(entry)
            rows = [q] if q else []
        except ValueError:
            rows = search_quests(" ".join(args))
        if not rows or rows == [None]:
            return "  No quests found."
        lines = [f"\n  {'ID':<7} {'Title':<45} {'lvl':<10}"]
        lines.append("  " + "─" * 65)
        for r in rows:
            if r is None: continue
            lvl = f"{r.get('MinLevel') or '?'}-{r.get('QuestLevel') or '?'}"
            lines.append(f"  {r['entry']:<7} {r['Title']:<45} {lvl}")
        return "\n".join(lines)

    def _cli_tele(self, args):
        if not args:
            return "Usage: wtele <location name>"
        rows = search_tele(" ".join(args))
        if not rows:
            return "  No teleport locations found."
        lines = [f"\n  {'Name':<30} {'Map':<6} {'X':>10} {'Y':>10} {'Z':>8}"]
        lines.append("  " + "─" * 68)
        for r in rows:
            lines.append(f"  {r['name']:<30} {r['map']:<6} "
                         f"{r['position_x']:>10.1f} {r['position_y']:>10.1f} {r['position_z']:>8.1f}")
        return "\n".join(lines)

    def _cli_npc(self, args):
        if not args:
            return "Usage: wnpc <creature_entry_id>"
        try:
            entry = int(args[0])
        except ValueError:
            return "Entry must be a number."
        tpl = get_creature_template(entry)
        if not tpl:
            return f"  No creature with entry {entry}."
        vendor = get_vendor_items(entry)
        trainer = get_npc_trainer(entry)
        lines = [f"\n  NPC #{entry}: {tpl['Name']} (lvl {tpl['MinLevel']}-{tpl['MaxLevel']})"]
        if vendor:
            lines.append(f"\n  Vendor ({len(vendor)} items):")
            for v in vendor[:15]:
                lines.append(f"    [{v['item']}] {v['name'] or '?'}  price: {v['BuyPrice'] or 0}c")
        if trainer:
            lines.append(f"\n  Trainer ({len(trainer)} spells):")
            for t in trainer[:15]:
                lines.append(f"    spell {t['spell']}  req lvl {t['reqlevel']}  cost {t['spellcost']}c")
        if not vendor and not trainer:
            lines.append("  (no vendor/trainer data)")
        return "\n".join(lines)

    def _cli_loot(self, args):
        if not args:
            return "Usage: wloot <creature_entry_id>"
        try:
            entry = int(args[0])
        except ValueError:
            return "Entry must be a number."
        rows = get_creature_loot(entry)
        if not rows:
            return f"  No loot defined for creature {entry}."
        lines = [f"\n  Loot for creature #{entry}:"]
        lines.append(f"  {'Item ID':<10} {'Name':<35} {'Chance %':<10} Count")
        lines.append("  " + "─" * 65)
        for r in rows:
            chance = float(r["ChanceOrQuestChance"] or 0)
            lines.append(f"  {r['item']:<10} {r['name'] or '?':<35} {chance:<10.1f} {r['mincountOrRef']}-{r['maxcount']}")
        return "\n".join(lines)

    def _cli_spawns(self, args):
        if len(args) < 3:
            return "Usage: wspawns <map_id> <x> <y> [radius=150]"
        try:
            map_id = int(args[0]); x = float(args[1]); y = float(args[2])
            radius = float(args[3]) if len(args) > 3 else 150.0
        except ValueError:
            return "Arguments must be numbers."
        spawns = get_creatures_near(map_id, x, y, radius)
        if not spawns:
            return f"  No creature spawns within {radius}y of ({x:.0f},{y:.0f}) on map {map_id}."
        lines = [f"\n  Creature spawns near ({x:.0f},{y:.0f}) map {map_id} within {radius:.0f}y:"]
        lines.append(f"  {'GUID':<8} {'ID':<7} {'Name':<28} {'lvl':<8} {'X':>10} {'Y':>10}")
        lines.append("  " + "─" * 75)
        for s in spawns[:40]:
            lvl = f"{s['MinLevel']}-{s['MaxLevel']}"
            lines.append(f"  {s['guid']:<8} {s['id']:<7} {s['Name']:<28} {lvl:<8} "
                         f"{s['position_x']:>10.1f} {s['position_y']:>10.1f}")
        if len(spawns) > 40:
            lines.append(f"  ... ({len(spawns)-40} more, use smaller radius)")
        return "\n".join(lines)

    def _cli_reimport(self, args):
        import subprocess, sys
        cmd = [sys.executable, "import_world.py"] + args
        log.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=os.path.dirname(WORLD_DB))
        close_wdb()
        return (result.stdout[-3000:] if result.stdout else "") + \
               (result.stderr[-1000:] if result.stderr else "")

    # ── GM handlers ───────────────────────────────────────────────────

    def _gm_tele(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .tele <location name>")
            return
        name  = " ".join(args)
        row   = get_tele(name)
        if not row:
            # Show suggestions
            rows = search_tele(name, limit=8)
            if rows:
                suggestions = ", ".join(r["name"] for r in rows)
                session.send_sys_msg(f"No exact match. Suggestions: {suggestions}")
            else:
                session.send_sys_msg(f"Location '{name}' not found.")
            return
        from opcodes import SMSG_NEW_WORLD
        from packets import ByteBuffer as BB
        from database import update_char_position
        x, y, z, map_id = (float(row["position_x"]), float(row["position_y"]),
                           float(row["position_z"]), int(row["map"]))
        buf = BB(); buf.uint32(map_id); buf.float32(x); buf.float32(y); buf.float32(z); buf.float32(0.0)
        session._send(SMSG_NEW_WORLD, buf.bytes())
        if session.char:
            update_char_position(session.db_path, session.char["id"], map_id, x, y, z, 0.0)
            session.char = dict(session.char)
            session.char.update({"map": map_id, "pos_x": x, "pos_y": y, "pos_z": z})
        session.send_sys_msg(f"Teleporting to {row['name']} (map {map_id}).")

    def _gm_npcinfo(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .npcinfo <creature_id>")
            return
        try: entry = int(args[0])
        except ValueError:
            session.send_sys_msg("ID must be a number.")
            return
        t = get_creature_template(entry)
        if not t:
            session.send_sys_msg(f"No creature with ID {entry}.")
            return
        lines = [
            f"NPC #{entry}: {t['Name']}",
            f"Level: {t['MinLevel']}-{t['MaxLevel']}  Scale: {t['scale']}",
            f"Model: {t['ModelId1']}  Faction A:{t['FactionAlliance']} H:{t['FactionHorde']}",
            f"HP mult: {t.get('BaseHealthMultiplier')}  Speed: {t.get('SpeedWalk')}",
        ]
        vendor = get_vendor_items(entry)
        if vendor:
            lines.append(f"Vendor: {len(vendor)} items")
        session.send_sys_msg("\n".join(lines))

    def _gm_lookup(self, session, args):
        if len(args) < 2:
            session.send_sys_msg("Usage: .lookup item|creature|quest <name>")
            return
        kind = args[0].lower()
        name = " ".join(args[1:])
        if kind == "item":
            rows = search_items(name, 8)
            if not rows:
                session.send_sys_msg(f"No items matching '{name}'.")
                return
            lines = [f"Items matching '{name}':"]
            for r in rows:
                lines.append(f"  [{r['entry']}] {r['name']} (ilvl {r['ItemLevel']})")
            session.send_sys_msg("\n".join(lines))
        elif kind in ("creature", "npc", "mob"):
            rows = search_creatures(name, 8)
            if not rows:
                session.send_sys_msg(f"No creatures matching '{name}'.")
                return
            lines = [f"Creatures matching '{name}':"]
            for r in rows:
                lines.append(f"  [{r['Entry']}] {r['Name']} lvl {r['MinLevel']}-{r['MaxLevel']}")
            session.send_sys_msg("\n".join(lines))
        elif kind == "quest":
            rows = search_quests(name, 8)
            if not rows:
                session.send_sys_msg(f"No quests matching '{name}'.")
                return
            lines = [f"Quests matching '{name}':"]
            for r in rows:
                lines.append(f"  [{r['entry']}] {r['Title']}")
            session.send_sys_msg("\n".join(lines))
        else:
            session.send_sys_msg("Type must be: item, creature, or quest")

    def _gm_loot(self, session, args):
        if not args:
            session.send_sys_msg("Usage: .loot <creature_id>")
            return
        try: entry = int(args[0])
        except ValueError:
            session.send_sys_msg("ID must be a number."); return
        rows = get_creature_loot(entry)
        if not rows:
            session.send_sys_msg(f"No loot defined for creature {entry}.")
            return
        lines = [f"Loot for creature #{entry} ({len(rows)} entries):"]
        for r in rows[:10]:
            chance = float(r["ChanceOrQuestChance"] or 0)
            lines.append(f"  [{r['item']}] {r['name'] or '?'}  {chance:.1f}%  x{r['mincountOrRef']}-{r['maxcount']}")
        session.send_sys_msg("\n".join(lines))
