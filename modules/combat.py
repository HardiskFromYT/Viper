"""Combat module — vanilla 1.12.1 blizzlike melee combat system.

Handles:
  - CMSG_ATTACKSWING / CMSG_ATTACKSTOP (player auto-attack)
  - Swing timer loop (asyncio)
  - Damage calculation with armor reduction
  - Hit table: miss, dodge, crit, normal hit
  - Creature retaliation (aggro + swing back)
  - Creature AI: chase player with SMSG_MONSTER_MOVE
  - Health updates via partial SMSG_UPDATE_OBJECT
  - Creature death (animation + despawn after 30s)
  - Player death → release spirit → ghost at graveyard → corpse walk → resurrect
"""
import asyncio
import logging
import math
import random
import struct
import time

from modules.base import BaseModule
from opcodes import (
    CMSG_ATTACKSWING, CMSG_ATTACKSTOP,
    SMSG_ATTACKSTART, SMSG_ATTACKSTOP,
    SMSG_ATTACKERSTATEUPDATE,
    SMSG_ATTACKSWING_NOTINRANGE,
    SMSG_ATTACKSWING_BADFACING,
    SMSG_ATTACKSWING_DEADTARGET,
    SMSG_ATTACKSWING_CANT_ATTACK,
    SMSG_UPDATE_OBJECT, SMSG_DESTROY_OBJECT,
    SMSG_MONSTER_MOVE,
    CMSG_REPOP_REQUEST, CMSG_RECLAIM_CORPSE,
    MSG_CORPSE_QUERY, SMSG_CORPSE_RECLAIM_DELAY,
)
from packets import ByteBuffer, pack_guid

log = logging.getLogger("combat")

# ── Constants ────────────────────────────────────────────────────────────────

MELEE_RANGE = 5.0           # yards
CHASE_RANGE = 40.0          # max yards creature will chase before leash reset
DEFAULT_ATTACK_TIME = 2000  # ms
CREATURE_GUID_OFFSET = 0x100000000
CORPSE_DESPAWN_TIME = 30.0  # seconds before dead creature disappears
CREATURE_RUN_SPEED = 7.0    # yards/second (same as player run speed)
CHASE_UPDATE_INTERVAL = 0.5 # seconds between creature position updates
CORPSE_RECLAIM_RANGE = 40.0 # yards — how close player must be to corpse

# Spirit Healer creature template entry
SPIRIT_HEALER_ENTRY = 6491

# Update field indices (vanilla 1.12.1)
UNIT_FIELD_HEALTH    = 0x0016
UNIT_FIELD_MAXHEALTH = 0x001C
UNIT_FIELD_TARGET    = 0x0012  # 2 fields (GUID low + high)
UNIT_FIELD_DISPLAYID = 0x0083
UNIT_FIELD_NATIVEDID = 0x0084
UNIT_FIELD_BYTES_1   = 0x00A1  # stand state / death state
PLAYER_FLAGS         = 0x00BE  # player-specific flags field

# Ghost display model
GHOST_DISPLAY_ID     = 10045   # wisp/ghost model for all races

# HitInfo flags for SMSG_ATTACKERSTATEUPDATE (1.12.1 / post-1.9.4)
HITINFO_NORMALSWING    = 0x00000000
HITINFO_AFFECTS_VICTIM = 0x00000002  # REQUIRED for "being hit" animation
HITINFO_LEFTSWING      = 0x00000004  # offhand
HITINFO_MISS           = 0x00000010
HITINFO_ABSORB         = 0x00000020
HITINFO_RESIST         = 0x00000040
HITINFO_CRITICALHIT    = 0x00000080
HITINFO_GLANCING       = 0x00004000
HITINFO_CRUSHING       = 0x00008000

# VictimState (1.12.1)
VICTIMSTATE_UNAFFECTED = 0  # used with miss
VICTIMSTATE_NORMAL     = 1  # normal hit landed
VICTIMSTATE_DODGE      = 2
VICTIMSTATE_PARRY      = 3
VICTIMSTATE_BLOCKS     = 5

# UnitStandState values packed in UNIT_FIELD_BYTES_1
UNIT_STAND_STATE_DEAD = 0x01  # byte 0 of BYTES_1

# Player flags
PLAYER_FLAG_GHOST = 0x00000010

# ── Creature runtime state ───────────────────────────────────────────────────
# Global dict: creature_guid -> state dict
# Lazily populated when a creature is first attacked
_creature_state: dict[int, dict] = {}

# Spline ID counter for SMSG_MONSTER_MOVE
_spline_id = 0


def _next_spline_id() -> int:
    global _spline_id
    _spline_id += 1
    return _spline_id


def _get_creature_spawn_data(session, creature_guid: int) -> dict | None:
    """Look up creature spawn data from the session's known creatures cache."""
    if not hasattr(session, "_known_creature_data"):
        return None
    return session._known_creature_data.get(creature_guid)


def get_or_init_creature(creature_guid: int, session) -> dict | None:
    """Get or lazily initialize runtime combat state for a creature."""
    if creature_guid in _creature_state:
        return _creature_state[creature_guid]

    # Look up template from world.db
    spawn_id = creature_guid - CREATURE_GUID_OFFSET
    from modules.world_data import get_creature_template, wdb
    # Find the creature spawn to get template ID
    try:
        row = wdb().execute(
            "SELECT id FROM creature WHERE guid=?", (spawn_id,)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None

    tpl = get_creature_template(row["id"])
    if not tpl:
        return None

    tpl = dict(tpl)
    level = max(1, (int(tpl.get("MinLevel") or 1) + int(tpl.get("MaxLevel") or 1)) // 2)
    hp_mult = float(tpl.get("HealthMultiplier") or 1.0) or 1.0
    min_hp = int(tpl.get("MinLevelHealth") or level * 10) or level * 10
    max_hp = int(tpl.get("MaxLevelHealth") or min_hp) or min_hp
    base_hp = (min_hp + max_hp) // 2
    health = max(1, int(base_hp * hp_mult))

    state = {
        "guid": creature_guid,
        "spawn_id": spawn_id,
        "entry": row["id"],
        "template": tpl,
        "level": level,
        "health": health,
        "max_health": health,
        "armor": int(tpl.get("Armor") or 0),
        "min_dmg": float(tpl.get("MinMeleeDmg") or 1.0),
        "max_dmg": float(tpl.get("MaxMeleeDmg") or 2.0),
        "attack_time": int(tpl.get("MeleeBaseAttackTime") or DEFAULT_ATTACK_TIME),
        "dmg_mult": float(tpl.get("DamageMultiplier") or 1.0) or 1.0,
        "attack_power": int(tpl.get("MeleeAttackPower") or 0),
        "faction": int(tpl.get("FactionAlliance") or 14),
        "npc_flags": int(tpl.get("NpcFlags") or 0),
        "target": 0,
        "attack_task": None,
        "chase_task": None,     # timer for periodic chase updates
        "threat": {},           # player_guid -> threat value
        "dead": False,
        # Current position (updated as creature moves)
        "pos_x": 0.0,
        "pos_y": 0.0,
        "pos_z": 0.0,
        # Spawn (home) position for leash reset
        "spawn_x": 0.0,
        "spawn_y": 0.0,
        "spawn_z": 0.0,
    }

    # Get spawn position
    try:
        spawn = wdb().execute(
            "SELECT position_x, position_y, position_z FROM creature WHERE guid=?",
            (spawn_id,)
        ).fetchone()
        if spawn:
            state["spawn_x"] = state["pos_x"] = float(spawn["position_x"])
            state["spawn_y"] = state["pos_y"] = float(spawn["position_y"])
            state["spawn_z"] = state["pos_z"] = float(spawn["position_z"])
    except Exception:
        pass

    _creature_state[creature_guid] = state
    return state


# ── Distance / position helpers ──────────────────────────────────────────────

def _dist_2d(x1, y1, x2, y2) -> float:
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def _dist_3d(x1, y1, z1, x2, y2, z2) -> float:
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2 + (z1 - z2) ** 2)


def _get_creature_pos(creature_guid: int) -> tuple[float, float, float]:
    """Get a creature's current position (updated during chase)."""
    cs = _creature_state.get(creature_guid)
    if cs:
        return cs["pos_x"], cs["pos_y"], cs["pos_z"]
    return 0, 0, 0


def _get_player_pos(session) -> tuple[float, float, float]:
    if session.char:
        return float(session.char["pos_x"]), float(session.char["pos_y"]), float(session.char["pos_z"])
    return 0, 0, 0


def _is_in_melee_range(session, creature_guid: int) -> bool:
    px, py, _ = _get_player_pos(session)
    cx, cy, _ = _get_creature_pos(creature_guid)
    return _dist_2d(px, py, cx, cy) <= MELEE_RANGE


# ── SMSG_MONSTER_MOVE builder ───────────────────────────────────────────────

def _build_monster_move(creature_guid: int, start_x: float, start_y: float,
                        start_z: float, dest_x: float, dest_y: float,
                        dest_z: float, duration_ms: int) -> bytes:
    """Build SMSG_MONSTER_MOVE for a straight-line move from start to dest."""
    buf = ByteBuffer()
    buf.raw(pack_guid(creature_guid))
    buf.float32(start_x)
    buf.float32(start_y)
    buf.float32(start_z)
    buf.uint32(_next_spline_id())   # spline ID
    buf.uint8(0)                     # MonsterMoveNormal
    buf.uint32(0x00000100)           # SplineFlags: Runmode
    buf.uint32(duration_ms)          # travel time
    buf.uint32(1)                    # point count = 1 (destination only)
    buf.float32(dest_x)
    buf.float32(dest_y)
    buf.float32(dest_z)
    return buf.bytes()


def _build_monster_move_stop(creature_guid: int, x: float, y: float,
                              z: float) -> bytes:
    """Build SMSG_MONSTER_MOVE stop packet."""
    buf = ByteBuffer()
    buf.raw(pack_guid(creature_guid))
    buf.float32(x)
    buf.float32(y)
    buf.float32(z)
    buf.uint32(_next_spline_id())
    buf.uint8(1)  # MonsterMoveStop
    return buf.bytes()


# ── Hit table ────────────────────────────────────────────────────────────────

HIT_MISS   = "miss"
HIT_DODGE  = "dodge"
HIT_CRIT   = "crit"
HIT_NORMAL = "normal"


def _roll_hit_table(attacker_level: int, victim_level: int) -> str:
    """Single-roll vanilla hit table. Returns hit result string."""
    level_diff = victim_level - attacker_level
    miss_chance = max(0, min(60, 5 + level_diff))
    dodge_chance = 5
    crit_chance = 5

    roll = random.randint(1, 100)
    if roll <= miss_chance:
        return HIT_MISS
    roll -= miss_chance
    if roll <= dodge_chance:
        return HIT_DODGE
    roll -= dodge_chance
    if roll <= crit_chance:
        return HIT_CRIT
    return HIT_NORMAL


# ── Damage calculation ───────────────────────────────────────────────────────

def _calc_armor_reduction(armor: int, attacker_level: int) -> float:
    """Vanilla armor reduction formula. Returns damage multiplier (0.0 to 1.0)."""
    if armor <= 0:
        return 1.0
    reduction = armor / (armor + 400 + 85 * attacker_level)
    return max(0.0, min(1.0, 1.0 - reduction))


def _calc_player_damage(session) -> tuple[float, float]:
    """Calculate player's melee damage range. For now: level-based + basic formula."""
    level = session.char["level"] if session.char else 1
    base_min = 1.0 + level * 0.5
    base_max = 2.0 + level * 1.5
    return base_min, base_max


def _calc_creature_damage(cs: dict) -> float:
    """Roll creature melee damage."""
    min_dmg = cs["min_dmg"]
    max_dmg = max(min_dmg, cs["max_dmg"])
    base = random.uniform(min_dmg, max_dmg)
    ap_bonus = cs["attack_power"] / 14.0 * (cs["attack_time"] / 1000.0)
    return max(1.0, (base + ap_bonus) * cs["dmg_mult"])


# ── Packet builders ──────────────────────────────────────────────────────────

def _build_attack_start(attacker_guid: int, victim_guid: int) -> bytes:
    buf = ByteBuffer()
    buf.uint64(attacker_guid)
    buf.uint64(victim_guid)
    return buf.bytes()


def _build_attack_stop(attacker_guid: int, victim_guid: int) -> bytes:
    buf = ByteBuffer()
    buf.raw(pack_guid(attacker_guid))
    buf.raw(pack_guid(victim_guid))
    buf.uint32(0)  # unk (dead flag)
    return buf.bytes()


def _build_attacker_state_update(
    hit_info: int, attacker_guid: int, victim_guid: int,
    total_damage: int, sub_school: int = 0,
    absorbed: int = 0, resisted: int = 0,
    victim_state: int = VICTIMSTATE_NORMAL,
    blocked: int = 0,
) -> bytes:
    """Build SMSG_ATTACKERSTATEUPDATE payload (vanilla 1.12.1 format)."""
    buf = ByteBuffer()
    buf.uint32(hit_info)
    buf.raw(pack_guid(attacker_guid))
    buf.raw(pack_guid(victim_guid))
    buf.uint32(total_damage)

    buf.uint8(1)  # sub-damage count
    buf.uint32(sub_school)
    buf.float32(float(total_damage))
    buf.uint32(total_damage)
    buf.uint32(absorbed)
    buf.uint32(resisted)

    buf.uint32(victim_state)
    buf.uint32(0)   # unknown1
    buf.uint32(0)   # unknown2
    buf.uint32(blocked)
    return buf.bytes()


def _build_health_update(guid: int, health: int) -> bytes:
    """Build a partial SMSG_UPDATE_OBJECT (UPDATETYPE_VALUES) for health change."""
    buf = ByteBuffer()
    buf.uint32(1)      # count
    buf.uint8(0)       # has_transport
    buf.uint8(0)       # UPDATETYPE_VALUES
    buf.raw(pack_guid(guid))

    num_blocks = (UNIT_FIELD_HEALTH // 32) + 1
    mask_words = [0] * num_blocks
    mask_words[UNIT_FIELD_HEALTH // 32] |= (1 << (UNIT_FIELD_HEALTH % 32))

    buf.uint8(num_blocks)
    for w in mask_words:
        buf.uint32(w)
    buf.uint32(health)

    return buf.bytes()


def _build_death_update(guid: int) -> bytes:
    """Build partial UPDATE_OBJECT setting health=0 and death stand state."""
    buf = ByteBuffer()
    buf.uint32(1)      # count
    buf.uint8(0)       # has_transport
    buf.uint8(0)       # UPDATETYPE_VALUES
    buf.raw(pack_guid(guid))

    max_field = UNIT_FIELD_BYTES_1 + 1
    num_blocks = (max_field + 31) // 32
    mask_words = [0] * num_blocks
    mask_words[UNIT_FIELD_HEALTH // 32] |= (1 << (UNIT_FIELD_HEALTH % 32))
    mask_words[UNIT_FIELD_BYTES_1 // 32] |= (1 << (UNIT_FIELD_BYTES_1 % 32))

    buf.uint8(num_blocks)
    for w in mask_words:
        buf.uint32(w)
    buf.uint32(0)                    # UNIT_FIELD_HEALTH = 0
    buf.uint32(UNIT_STAND_STATE_DEAD)  # UNIT_FIELD_BYTES_1 = dead

    return buf.bytes()


def _build_alive_update(guid: int, health: int) -> bytes:
    """Build partial UPDATE_OBJECT restoring health and clearing death state."""
    buf = ByteBuffer()
    buf.uint32(1)      # count
    buf.uint8(0)       # has_transport
    buf.uint8(0)       # UPDATETYPE_VALUES
    buf.raw(pack_guid(guid))

    max_field = UNIT_FIELD_BYTES_1 + 1
    num_blocks = (max_field + 31) // 32
    mask_words = [0] * num_blocks
    mask_words[UNIT_FIELD_HEALTH // 32] |= (1 << (UNIT_FIELD_HEALTH % 32))
    mask_words[UNIT_FIELD_BYTES_1 // 32] |= (1 << (UNIT_FIELD_BYTES_1 % 32))

    buf.uint8(num_blocks)
    for w in mask_words:
        buf.uint32(w)
    buf.uint32(health)  # UNIT_FIELD_HEALTH = alive
    buf.uint32(0)       # UNIT_FIELD_BYTES_1 = standing (alive)

    return buf.bytes()


def _build_player_flags_update(guid: int, flags: int) -> bytes:
    """Build partial UPDATE_OBJECT setting PLAYER_FLAGS."""
    buf = ByteBuffer()
    buf.uint32(1)      # count
    buf.uint8(0)       # has_transport
    buf.uint8(0)       # UPDATETYPE_VALUES
    buf.raw(pack_guid(guid))

    num_blocks = (PLAYER_FLAGS // 32) + 1
    mask_words = [0] * num_blocks
    mask_words[PLAYER_FLAGS // 32] |= (1 << (PLAYER_FLAGS % 32))

    buf.uint8(num_blocks)
    for w in mask_words:
        buf.uint32(w)
    buf.uint32(flags)

    return buf.bytes()


def _build_displayid_update(guid: int, display_id: int) -> bytes:
    """Build partial UPDATE_OBJECT setting UNIT_FIELD_DISPLAYID."""
    buf = ByteBuffer()
    buf.uint32(1)      # count
    buf.uint8(0)       # has_transport
    buf.uint8(0)       # UPDATETYPE_VALUES
    buf.raw(pack_guid(guid))

    num_blocks = (UNIT_FIELD_DISPLAYID // 32) + 1
    mask_words = [0] * num_blocks
    mask_words[UNIT_FIELD_DISPLAYID // 32] |= (1 << (UNIT_FIELD_DISPLAYID % 32))

    buf.uint8(num_blocks)
    for w in mask_words:
        buf.uint32(w)
    buf.uint32(display_id)

    return buf.bytes()


# ── Broadcast helpers ────────────────────────────────────────────────────────

def _get_server(session):
    return getattr(session, "server", None)


def _get_nearby_sessions(session) -> list:
    """Get all online sessions (for now, broadcast to all)."""
    server = _get_server(session)
    if not server:
        return []
    return [s for s in server.get_online_players() if s.char]


def _broadcast_packet(session, opcode: int, data: bytes):
    """Send a packet to the session itself and all nearby players."""
    for s in _get_nearby_sessions(session):
        try:
            s._send(opcode, data)
        except Exception:
            pass


def _send_health_to_all(guid: int, health: int, sessions: list):
    """Send health update for a unit to all given sessions."""
    pkt = _build_health_update(guid, health)
    for s in sessions:
        try:
            s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception:
            pass


def _broadcast_to_all(opcode: int, data: bytes):
    """Send a packet to ALL online sessions."""
    if not _server_ref:
        return
    for s in _server_ref.get_online_players():
        if s.char:
            try:
                s._send(opcode, data)
            except Exception:
                pass


# ── Creature chase AI ───────────────────────────────────────────────────────

def _creature_chase_target(creature_guid: int):
    """Move a creature toward its current target. Called periodically."""
    try:
        _creature_chase_inner(creature_guid)
    except Exception:
        log.exception("Error in creature chase")


def _creature_chase_inner(creature_guid: int):
    cs = _creature_state.get(creature_guid)
    if not cs or cs["dead"] or cs["target"] == 0:
        return

    target_session = _find_session_by_guid(cs["target"])
    if not target_session or not target_session.char:
        return
    if getattr(target_session, "health", 0) <= 0:
        return

    px, py, pz = _get_player_pos(target_session)
    cx, cy, cz = cs["pos_x"], cs["pos_y"], cs["pos_z"]

    dist = _dist_2d(px, py, cx, cy)

    # Leash check — if too far from spawn, reset
    spawn_dist = _dist_2d(cs["spawn_x"], cs["spawn_y"], cx, cy)
    if spawn_dist > CHASE_RANGE:
        _creature_evade(creature_guid)
        return

    # Already in melee range — no need to move
    if dist <= MELEE_RANGE:
        _schedule_chase(creature_guid)
        return

    # Move toward player — stop at melee range
    move_dist = dist - (MELEE_RANGE * 0.8)  # stop slightly inside melee range
    if move_dist <= 0:
        _schedule_chase(creature_guid)
        return

    # Calculate destination point along the line to player
    dx = px - cx
    dy = py - cy
    dz = pz - cz
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if norm < 0.001:
        _schedule_chase(creature_guid)
        return

    # Move at most move_dist toward player
    ratio = move_dist / norm
    dest_x = cx + dx * ratio
    dest_y = cy + dy * ratio
    dest_z = cz + dz * ratio

    # Calculate travel time
    duration_ms = max(100, int((move_dist / CREATURE_RUN_SPEED) * 1000))

    # Send SMSG_MONSTER_MOVE to all players
    pkt = _build_monster_move(creature_guid, cx, cy, cz,
                               dest_x, dest_y, dest_z, duration_ms)
    _broadcast_to_all(SMSG_MONSTER_MOVE, pkt)

    # Update creature's server-side position to destination
    # (In a real server this would interpolate, but for simplicity
    #  we set it immediately — the next chase tick will correct)
    cs["pos_x"] = dest_x
    cs["pos_y"] = dest_y
    cs["pos_z"] = dest_z

    # Schedule next chase update
    _schedule_chase(creature_guid)


def _schedule_chase(creature_guid: int):
    """Schedule the next chase position update."""
    cs = _creature_state.get(creature_guid)
    if not cs or cs["dead"] or cs["target"] == 0:
        return

    if cs["chase_task"] and not cs["chase_task"].cancelled():
        cs["chase_task"].cancel()

    loop = asyncio.get_running_loop()
    cs["chase_task"] = loop.call_later(
        CHASE_UPDATE_INTERVAL, _creature_chase_target, creature_guid)


def _stop_chase(creature_guid: int):
    """Stop creature chase movement."""
    cs = _creature_state.get(creature_guid)
    if not cs:
        return
    if cs["chase_task"] and not cs["chase_task"].cancelled():
        cs["chase_task"].cancel()
    cs["chase_task"] = None


def _creature_evade(creature_guid: int):
    """Creature resets to spawn point — evade (leash)."""
    cs = _creature_state.get(creature_guid)
    if not cs:
        return

    log.info(f"Creature {creature_guid:#x} evading — returning to spawn")

    old_target = cs["target"]
    cs["target"] = 0
    cs["threat"].clear()

    # Cancel attack and chase timers
    if cs["attack_task"] and not cs["attack_task"].cancelled():
        cs["attack_task"].cancel()
    cs["attack_task"] = None
    _stop_chase(creature_guid)

    # Move back to spawn
    cx, cy, cz = cs["pos_x"], cs["pos_y"], cs["pos_z"]
    sx, sy, sz = cs["spawn_x"], cs["spawn_y"], cs["spawn_z"]
    dist = _dist_2d(cx, cy, sx, sy)
    duration_ms = max(100, int((dist / CREATURE_RUN_SPEED) * 1000))

    pkt = _build_monster_move(creature_guid, cx, cy, cz,
                               sx, sy, sz, duration_ms)
    _broadcast_to_all(SMSG_MONSTER_MOVE, pkt)

    cs["pos_x"] = sx
    cs["pos_y"] = sy
    cs["pos_z"] = sz

    # Restore full health on evade
    cs["health"] = cs["max_health"]
    _broadcast_to_all(SMSG_UPDATE_OBJECT,
                      _build_health_update(creature_guid, cs["max_health"]))

    # Send attack stop
    if old_target:
        pkt = _build_attack_stop(creature_guid, old_target)
        _broadcast_to_all(SMSG_ATTACKSTOP, pkt)


# ── Player auto-attack ───────────────────────────────────────────────────────

def _start_player_attack(session, target_guid: int):
    """Start the player's auto-attack loop against a creature."""
    if target_guid < CREATURE_GUID_OFFSET:
        session._send(SMSG_ATTACKSWING_CANT_ATTACK, b"")
        return

    cs = get_or_init_creature(target_guid, session)
    if not cs:
        session._send(SMSG_ATTACKSWING_CANT_ATTACK, b"")
        return

    if cs["dead"]:
        session._send(SMSG_ATTACKSWING_DEADTARGET, b"")
        return

    from dbc import is_attackable_by_player
    if not is_attackable_by_player(cs["faction"], session.char["race"]):
        session._send(SMSG_ATTACKSWING_CANT_ATTACK, b"")
        return

    player_guid = session.char["id"]
    session._attack_target = target_guid
    session._in_combat = True

    _broadcast_packet(session, SMSG_ATTACKSTART,
                      _build_attack_start(player_guid, target_guid))

    # Execute first swing immediately
    _do_player_swing(session, target_guid)


def _schedule_player_swing(session, target_guid: int):
    """Schedule the next player melee swing."""
    if session._attack_timer and not session._attack_timer.cancelled():
        session._attack_timer.cancel()

    attack_time = getattr(session, "_base_attack_time", DEFAULT_ATTACK_TIME)
    delay = attack_time / 1000.0

    loop = asyncio.get_running_loop()
    session._attack_timer = loop.call_later(delay, _do_player_swing, session, target_guid)


def _do_player_swing(session, target_guid: int):
    """Execute one player melee swing."""
    try:
        _do_player_swing_inner(session, target_guid)
    except Exception:
        log.exception("Error in player swing")


def _do_player_swing_inner(session, target_guid: int):
    if not session.char or session._attack_target != target_guid:
        return
    if getattr(session, "health", 0) <= 0:
        _stop_player_attack(session)
        return

    cs = _creature_state.get(target_guid)
    if not cs or cs["dead"]:
        _stop_player_attack(session)
        return

    player_guid = session.char["id"]
    player_level = session.char["level"]
    nearby = _get_nearby_sessions(session)

    # Range check
    if not _is_in_melee_range(session, target_guid):
        session._send(SMSG_ATTACKSWING_NOTINRANGE, b"")
        _schedule_player_swing(session, target_guid)
        return

    # Roll hit table
    hit_result = _roll_hit_table(player_level, cs["level"])

    if hit_result == HIT_MISS:
        pkt = _build_attacker_state_update(
            HITINFO_MISS, player_guid, target_guid, 0,
            victim_state=VICTIMSTATE_UNAFFECTED)
        for s in nearby:
            try: s._send(SMSG_ATTACKERSTATEUPDATE, pkt)
            except Exception: pass
        _schedule_player_swing(session, target_guid)
        _creature_add_threat(target_guid, session, 0)
        return

    if hit_result == HIT_DODGE:
        pkt = _build_attacker_state_update(
            HITINFO_AFFECTS_VICTIM, player_guid, target_guid, 0,
            victim_state=VICTIMSTATE_DODGE)
        for s in nearby:
            try: s._send(SMSG_ATTACKERSTATEUPDATE, pkt)
            except Exception: pass
        _schedule_player_swing(session, target_guid)
        _creature_add_threat(target_guid, session, 0)
        return

    # Calculate damage
    base_min, base_max = _calc_player_damage(session)
    raw_damage = random.uniform(base_min, base_max)
    if hit_result == HIT_CRIT:
        raw_damage *= 2.0

    armor_mult = _calc_armor_reduction(cs["armor"], player_level)
    final_damage = max(1, int(raw_damage * armor_mult))

    hit_info = HITINFO_AFFECTS_VICTIM
    if hit_result == HIT_CRIT:
        hit_info |= HITINFO_CRITICALHIT

    cs["health"] = max(0, cs["health"] - final_damage)

    log.info(f"Swing: {session.char['name']} -> creature {target_guid:#x} "
             f"hit={hit_result} dmg={final_damage} hp={cs['health']}/{cs['max_health']}")

    pkt = _build_attacker_state_update(
        hit_info, player_guid, target_guid, final_damage,
        victim_state=VICTIMSTATE_NORMAL)
    for s in nearby:
        try: s._send(SMSG_ATTACKERSTATEUPDATE, pkt)
        except Exception: pass

    _send_health_to_all(target_guid, cs["health"], nearby)
    _creature_add_threat(target_guid, session, final_damage)

    if cs["health"] <= 0:
        _creature_die(target_guid, nearby)
        _stop_player_attack(session)
        return

    _schedule_player_swing(session, target_guid)


def _stop_player_attack(session):
    """Stop the player's auto-attack."""
    if session._attack_timer and not session._attack_timer.cancelled():
        try: session._attack_timer.cancel()
        except Exception: pass
    session._attack_timer = None

    player_guid = session.char["id"] if session.char else 0
    target_guid = session._attack_target
    session._attack_target = 0
    session._in_combat = False

    if player_guid and target_guid:
        _broadcast_packet(session, SMSG_ATTACKSTOP,
                          _build_attack_stop(player_guid, target_guid))


# ── Creature combat AI ───────────────────────────────────────────────────────

def _creature_add_threat(creature_guid: int, session, damage: int):
    """Add threat from a player to a creature and start retaliation if needed."""
    cs = _creature_state.get(creature_guid)
    if not cs or cs["dead"]:
        return

    player_guid = session.char["id"] if session.char else 0
    if not player_guid:
        return

    cs["threat"][player_guid] = cs["threat"].get(player_guid, 0) + max(1, damage)

    # If creature isn't already attacking, start attacking
    if cs["target"] == 0:
        _creature_start_attack(creature_guid, session)


def _creature_start_attack(creature_guid: int, target_session):
    """Make a creature start attacking a player."""
    cs = _creature_state.get(creature_guid)
    if not cs or cs["dead"]:
        return

    player_guid = target_session.char["id"] if target_session.char else 0
    cs["target"] = player_guid

    # Send SMSG_ATTACKSTART
    nearby = _get_nearby_sessions(target_session)
    pkt = _build_attack_start(creature_guid, player_guid)
    for s in nearby:
        try: s._send(SMSG_ATTACKSTART, pkt)
        except Exception: pass

    # Start chase AI and schedule first swing
    _schedule_chase(creature_guid)
    _schedule_creature_swing(creature_guid)


def _schedule_creature_swing(creature_guid: int):
    """Schedule the next creature melee swing."""
    cs = _creature_state.get(creature_guid)
    if not cs or cs["dead"]:
        return

    if cs["attack_task"] and not cs["attack_task"].cancelled():
        cs["attack_task"].cancel()

    delay = cs["attack_time"] / 1000.0
    loop = asyncio.get_running_loop()
    cs["attack_task"] = loop.call_later(delay, _do_creature_swing, creature_guid)


def _do_creature_swing(creature_guid: int):
    """Execute one creature melee swing."""
    try:
        _do_creature_swing_inner(creature_guid)
    except Exception:
        log.exception("Error in creature swing")


def _do_creature_swing_inner(creature_guid: int):
    cs = _creature_state.get(creature_guid)
    if not cs or cs["dead"] or cs["target"] == 0:
        return

    target_guid = cs["target"]
    target_session = _find_session_by_guid(target_guid)
    if not target_session or not target_session.char:
        _creature_drop_target(creature_guid)
        return

    if getattr(target_session, "health", 0) <= 0:
        _creature_drop_target(creature_guid)
        return

    nearby = _get_nearby_sessions(target_session)

    # Range check — creature must be close enough to hit
    px, py, _ = _get_player_pos(target_session)
    cx, cy = cs["pos_x"], cs["pos_y"]
    if _dist_2d(px, py, cx, cy) > MELEE_RANGE * 1.5:
        # Out of range but chasing — skip this swing, try again later
        _schedule_creature_swing(creature_guid)
        return

    # Roll hit table
    creature_level = cs["level"]
    player_level = target_session.char["level"]
    hit_result = _roll_hit_table(creature_level, player_level)

    if hit_result == HIT_MISS:
        pkt = _build_attacker_state_update(
            HITINFO_MISS, creature_guid, target_guid, 0,
            victim_state=VICTIMSTATE_UNAFFECTED)
        for s in nearby:
            try: s._send(SMSG_ATTACKERSTATEUPDATE, pkt)
            except Exception: pass
        _schedule_creature_swing(creature_guid)
        return

    if hit_result == HIT_DODGE:
        pkt = _build_attacker_state_update(
            HITINFO_AFFECTS_VICTIM, creature_guid, target_guid, 0,
            victim_state=VICTIMSTATE_DODGE)
        for s in nearby:
            try: s._send(SMSG_ATTACKERSTATEUPDATE, pkt)
            except Exception: pass
        _schedule_creature_swing(creature_guid)
        return

    raw_damage = _calc_creature_damage(cs)
    if hit_result == HIT_CRIT:
        raw_damage *= 2.0

    player_armor = player_level * 2
    armor_mult = _calc_armor_reduction(player_armor, creature_level)
    final_damage = max(1, int(raw_damage * armor_mult))

    hit_info = HITINFO_AFFECTS_VICTIM
    if hit_result == HIT_CRIT:
        hit_info |= HITINFO_CRITICALHIT

    target_session.health = max(0, target_session.health - final_damage)

    pkt = _build_attacker_state_update(
        hit_info, creature_guid, target_guid, final_damage,
        victim_state=VICTIMSTATE_NORMAL)
    for s in nearby:
        try: s._send(SMSG_ATTACKERSTATEUPDATE, pkt)
        except Exception: pass

    _send_health_to_all(target_guid, target_session.health, nearby)

    if target_session.health <= 0:
        _player_die(target_session, creature_guid)
        _creature_drop_target(creature_guid)
        return

    _schedule_creature_swing(creature_guid)


def _creature_drop_target(creature_guid: int):
    """Creature stops attacking and looks for next threat target."""
    cs = _creature_state.get(creature_guid)
    if not cs:
        return

    old_target = cs["target"]
    cs["target"] = 0

    # Cancel attack timer
    if cs["attack_task"] and not cs["attack_task"].cancelled():
        cs["attack_task"].cancel()
    cs["attack_task"] = None
    _stop_chase(creature_guid)

    # Try to find next highest-threat target
    if cs["threat"] and not cs["dead"]:
        to_remove = []
        for pg in cs["threat"]:
            sess = _find_session_by_guid(pg)
            if not sess or not sess.char or getattr(sess, "health", 0) <= 0:
                to_remove.append(pg)
        for pg in to_remove:
            del cs["threat"][pg]

        if cs["threat"]:
            next_target_guid = max(cs["threat"], key=cs["threat"].get)
            next_session = _find_session_by_guid(next_target_guid)
            if next_session and next_session.char and getattr(next_session, "health", 0) > 0:
                _creature_start_attack(creature_guid, next_session)
                return

    # No valid targets — evade back to spawn
    if not cs["dead"]:
        _creature_evade(creature_guid)
    else:
        # Dead — just send attack stop
        session = _find_any_session()
        if session and old_target:
            pkt = _build_attack_stop(creature_guid, old_target)
            _broadcast_to_all(SMSG_ATTACKSTOP, pkt)


# ── Creature death ───────────────────────────────────────────────────────────

def _creature_die(creature_guid: int, nearby_sessions: list):
    """Handle creature death."""
    cs = _creature_state.get(creature_guid)
    if not cs:
        return

    cs["dead"] = True
    cs["health"] = 0
    cs["target"] = 0

    # Cancel attack and chase timers
    if cs["attack_task"] and not cs["attack_task"].cancelled():
        cs["attack_task"].cancel()
    cs["attack_task"] = None
    _stop_chase(creature_guid)

    # Send stop movement
    pkt = _build_monster_move_stop(creature_guid, cs["pos_x"], cs["pos_y"], cs["pos_z"])
    for s in nearby_sessions:
        try: s._send(SMSG_MONSTER_MOVE, pkt)
        except Exception: pass

    # Send death update
    pkt = _build_death_update(creature_guid)
    for s in nearby_sessions:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Send SMSG_ATTACKSTOP
    for pg in cs["threat"]:
        pkt = _build_attack_stop(creature_guid, pg)
        for s in nearby_sessions:
            try: s._send(SMSG_ATTACKSTOP, pkt)
            except Exception: pass

    # Stop all players attacking this creature
    for s in nearby_sessions:
        if getattr(s, "_attack_target", 0) == creature_guid:
            _stop_player_attack(s)

    log.info(f"Creature {creature_guid:#x} (entry {cs['entry']}) died")

    # Schedule corpse despawn
    loop = asyncio.get_running_loop()
    loop.call_later(CORPSE_DESPAWN_TIME, _despawn_creature_corpse, creature_guid)


def _despawn_creature_corpse(creature_guid: int):
    """Remove a dead creature's corpse from all clients."""
    cs = _creature_state.pop(creature_guid, None)

    pkt = struct.pack("<Q", creature_guid)
    session = _find_any_session()
    if session:
        for s in _get_nearby_sessions(session):
            try:
                s._send(SMSG_DESTROY_OBJECT, pkt)
                if hasattr(s, "_known_creatures"):
                    s._known_creatures.discard(creature_guid)
            except Exception:
                pass

    log.debug(f"Despawned creature corpse {creature_guid:#x}")


# ── Player death / ghost / resurrection ──────────────────────────────────────

def _find_nearest_graveyard(map_id: int, x: float, y: float, z: float) -> tuple | None:
    """Find nearest spirit healer spawn as graveyard location.
    Spirit Healers (entry 6491) are placed at graveyards in the DB."""
    from modules.world_data import wdb
    try:
        rows = wdb().execute(
            """SELECT c.position_x, c.position_y, c.position_z
               FROM creature c
               WHERE c.id = ? AND c.map = ?""",
            (SPIRIT_HEALER_ENTRY, map_id)
        ).fetchall()
    except Exception:
        return None

    if not rows:
        # No spirit healers on this map — try any map as fallback
        try:
            rows = wdb().execute(
                """SELECT c.position_x, c.position_y, c.position_z, c.map
                   FROM creature c WHERE c.id = ? LIMIT 1""",
                (SPIRIT_HEALER_ENTRY,)
            ).fetchall()
        except Exception:
            return None
        if rows:
            r = rows[0]
            return (float(r["position_x"]), float(r["position_y"]),
                    float(r["position_z"]))
        return None

    # Find closest spirit healer
    best = None
    best_dist = float("inf")
    for r in rows:
        sx, sy, sz = float(r["position_x"]), float(r["position_y"]), float(r["position_z"])
        d = _dist_2d(x, y, sx, sy)
        if d < best_dist:
            best_dist = d
            best = (sx, sy, sz)

    return best


def _player_die(session, killer_guid: int = 0):
    """Handle player death. Shows release spirit dialog."""
    if not session.char:
        return

    player_guid = session.char["id"]
    session.health = 0
    session._in_combat = False

    # Save corpse location for resurrection
    session._corpse_x = float(session.char["pos_x"])
    session._corpse_y = float(session.char["pos_y"])
    session._corpse_z = float(session.char["pos_z"])
    session._corpse_map = session.char["map"]
    session._is_ghost = False
    session._is_dead = True

    # Persist death state to DB
    from database import save_death_state
    save_death_state(session.db_path, player_guid, True, False,
                     session._corpse_x, session._corpse_y,
                     session._corpse_z, session._corpse_map)

    # Stop player's own attack
    _stop_player_attack(session)

    # Send death update to all
    nearby = _get_nearby_sessions(session)
    pkt = _build_death_update(player_guid)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    log.info(f"Player {session.char['name']} died — waiting for release spirit")

    # Client automatically shows the "Release Spirit" button when health=0
    # and UNIT_FIELD_BYTES_1 has death state. Player sends CMSG_REPOP_REQUEST
    # when they click it.


def _send_corpse_query_response(session):
    """Send MSG_CORPSE_QUERY response with corpse location for minimap marker."""
    if not session.char:
        return
    if not getattr(session, "_is_ghost", False):
        # No corpse — send "not found"
        session._send(MSG_CORPSE_QUERY, struct.pack("<B", 0))
        return

    corpse_x = getattr(session, "_corpse_x", 0.0)
    corpse_y = getattr(session, "_corpse_y", 0.0)
    corpse_z = getattr(session, "_corpse_z", 0.0)
    corpse_map = getattr(session, "_corpse_map", 0)

    # Response: found(u8=1) + mapid(i32) + x(f32) + y(f32) + z(f32) + corpsemapid(u32)
    # mapid = map where the marker should show (same as corpse map for open world)
    # corpsemapid = actual map where corpse physically exists
    buf = ByteBuffer()
    buf.uint8(1)                    # found = true
    buf.uint32(corpse_map)          # mapid (where marker shows)
    buf.float32(corpse_x)           # x
    buf.float32(corpse_y)           # y
    buf.float32(corpse_z)           # z
    buf.uint32(corpse_map)          # corpsemapid (actual map)
    session._send(MSG_CORPSE_QUERY, buf.bytes())


def _handle_corpse_query(session, payload: bytes):
    """Handle MSG_CORPSE_QUERY — client asks where the corpse is."""
    _send_corpse_query_response(session)


def _handle_repop_request(session, payload: bytes):
    """Handle CMSG_REPOP_REQUEST — player clicked Release Spirit."""
    if not session.char:
        return
    if not getattr(session, "_is_dead", False):
        return
    if getattr(session, "_is_ghost", False):
        return  # Already a ghost

    player_guid = session.char["id"]
    map_id = session.char["map"]
    x, y, z = float(session.char["pos_x"]), float(session.char["pos_y"]), float(session.char["pos_z"])

    # Find nearest graveyard (spirit healer location)
    gy = _find_nearest_graveyard(map_id, x, y, z)
    if not gy:
        log.warning(f"No graveyard found for {session.char['name']} on map {map_id}")
        # Fallback: resurrect in place
        _player_resurrect(session)
        return

    gx, gy_y, gz = gy

    # Set ghost state
    session._is_ghost = True
    session.health = session.max_health  # Ghosts have full HP bar

    # Persist ghost state to DB
    from database import save_death_state
    save_death_state(session.db_path, player_guid, True, True,
                     session._corpse_x, session._corpse_y,
                     session._corpse_z, session._corpse_map)

    # Save original display ID for restoration on resurrect
    from modules.core_world import RACE_DISPLAY
    race, gender = session.char["race"], session.char["gender"]
    session._original_display_id = RACE_DISPLAY.get(race, (49, 50))[gender & 1]

    # Send alive update (ghost is technically "alive" with ghost flag)
    nearby = _get_nearby_sessions(session)
    pkt = _build_alive_update(player_guid, session.max_health)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Set PLAYER_FLAGS_GHOST
    pkt = _build_player_flags_update(player_guid, PLAYER_FLAG_GHOST)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Set ghost display model (wisp)
    pkt = _build_displayid_update(player_guid, GHOST_DISPLAY_ID)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Teleport to graveyard
    from modules.core_world import teleport_player
    teleport_player(session, map_id, gx, gy_y, gz, 0.0)

    # Send corpse location so the minimap shows the corpse marker + arrow
    _send_corpse_query_response(session)

    log.info(f"Player {session.char['name']} released spirit → graveyard at "
             f"({gx:.0f}, {gy_y:.0f}, {gz:.0f})")

    session.send_sys_msg("You are now a ghost. Walk back to your corpse to resurrect.")


def _handle_reclaim_corpse(session, payload: bytes):
    """Handle CMSG_RECLAIM_CORPSE — player wants to resurrect at corpse."""
    if not session.char:
        log.warning("RECLAIM_CORPSE: no char")
        return
    is_ghost = getattr(session, "_is_ghost", False)
    is_dead = getattr(session, "_is_dead", False)
    log.info(f"RECLAIM_CORPSE: ghost={is_ghost} dead={is_dead}")
    if not is_ghost:
        log.warning("RECLAIM_CORPSE: not a ghost, ignoring")
        return

    # Check distance to corpse
    px, py, pz = _get_player_pos(session)
    cx = getattr(session, "_corpse_x", px)
    cy = getattr(session, "_corpse_y", py)
    cz = getattr(session, "_corpse_z", pz)

    dist = _dist_2d(px, py, cx, cy)
    log.info(f"RECLAIM_CORPSE: player=({px:.0f},{py:.0f}) corpse=({cx:.0f},{cy:.0f}) dist={dist:.0f}")
    if dist > CORPSE_RECLAIM_RANGE:
        session.send_sys_msg(f"You are too far from your corpse ({dist:.0f} yards). Get closer!")
        return

    # Resurrect at current position (player is already near corpse, no teleport needed)
    _player_resurrect_at(session, px, py, pz)


def _player_resurrect_at(session, x: float, y: float, z: float):
    """Resurrect a ghost player at a specific position."""
    if not session.char:
        return

    player_guid = session.char["id"]

    # Clear ghost state
    session._is_ghost = False
    session._is_dead = False

    # Clear death state in DB
    from database import save_death_state
    save_death_state(session.db_path, player_guid, False, False)
    session.health = max(1, session.max_health // 2)  # Resurrect with 50% HP
    session.mana = session.max_mana

    nearby = _get_nearby_sessions(session)

    # Clear PLAYER_FLAGS_GHOST
    pkt = _build_player_flags_update(player_guid, 0)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Restore original display model
    original_display = getattr(session, "_original_display_id", None)
    if original_display:
        pkt = _build_displayid_update(player_guid, original_display)
        for s in nearby:
            try: s._send(SMSG_UPDATE_OBJECT, pkt)
            except Exception: pass

    # Send health update
    pkt = _build_alive_update(player_guid, session.health)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Clear corpse query (no corpse anymore)
    session._send(MSG_CORPSE_QUERY, struct.pack("<B", 0))

    log.info(f"Player {session.char['name']} resurrected at ({x:.0f},{y:.0f},{z:.0f}) "
             f"with {session.health} HP")
    session.send_sys_msg("You have been resurrected!")


def _player_resurrect(session):
    """Resurrect a dead player with full HP (fallback / GM resurrect)."""
    if not session.char:
        return

    player_guid = session.char["id"]
    session._is_ghost = False
    session._is_dead = False

    # Clear death state in DB
    from database import save_death_state
    save_death_state(session.db_path, player_guid, False, False)
    session.health = session.max_health
    session.mana = session.max_mana

    nearby = _get_nearby_sessions(session)

    # Clear ghost flag
    pkt = _build_player_flags_update(player_guid, 0)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Restore original display model
    original_display = getattr(session, "_original_display_id", None)
    if original_display:
        pkt = _build_displayid_update(player_guid, original_display)
        for s in nearby:
            try: s._send(SMSG_UPDATE_OBJECT, pkt)
            except Exception: pass

    # Alive update
    pkt = _build_alive_update(player_guid, session.max_health)
    for s in nearby:
        try: s._send(SMSG_UPDATE_OBJECT, pkt)
        except Exception: pass

    # Clear corpse query
    session._send(MSG_CORPSE_QUERY, struct.pack("<B", 0))

    log.info(f"Player {session.char['name']} resurrected with {session.max_health} HP")
    session.send_sys_msg("You have been resurrected!")


# ── Spirit Healer visibility helper ──────────────────────────────────────────

def is_spirit_healer_entry(entry_id: int) -> bool:
    """Check if a creature entry is a Spirit Healer (should only be visible to ghosts)."""
    return entry_id == SPIRIT_HEALER_ENTRY


def should_see_creature(session, creature_entry: int) -> bool:
    """Check if a session should see a given creature based on state.
    Spirit Healers are only visible to ghost players."""
    if creature_entry == SPIRIT_HEALER_ENTRY:
        return getattr(session, "_is_ghost", False)
    return True


# ── Session lookup helpers ───────────────────────────────────────────────────

_server_ref = None  # Set during module load


def _find_session_by_guid(player_guid: int):
    """Find an online session by character GUID."""
    if not _server_ref:
        return None
    for s in _server_ref.get_online_players():
        if s.char and s.char["id"] == player_guid:
            return s
    return None


def _find_any_session():
    """Find any online session (for broadcasting)."""
    if not _server_ref:
        return None
    players = _server_ref.get_online_players()
    return players[0] if players else None


# ── Module ───────────────────────────────────────────────────────────────────

class Module(BaseModule):
    name = "combat"

    def on_load(self, server):
        global _server_ref
        _server_ref = server

        # Force-reload dbc module so new functions are available after hot-reload
        import importlib, dbc
        importlib.reload(dbc)

        self.reg_packet(server, CMSG_ATTACKSWING, self._on_attack_swing)
        self.reg_packet(server, CMSG_ATTACKSTOP, self._on_attack_stop)
        self.reg_packet(server, CMSG_REPOP_REQUEST, self._on_repop_request)
        self.reg_packet(server, CMSG_RECLAIM_CORPSE, self._on_reclaim_corpse)
        self.reg_packet(server, MSG_CORPSE_QUERY, self._on_corpse_query)

        log.info("combat module loaded")

    def on_unload(self, server):
        global _server_ref
        # Cancel all creature attack and chase timers
        for cs in _creature_state.values():
            if cs["attack_task"] and not cs["attack_task"].cancelled():
                cs["attack_task"].cancel()
            if cs["chase_task"] and not cs["chase_task"].cancelled():
                cs["chase_task"].cancel()
        # Cancel all player attack timers
        for s in server.get_online_players():
            if hasattr(s, "_attack_timer") and s._attack_timer:
                try: s._attack_timer.cancel()
                except Exception: pass
        _server_ref = None
        log.info("combat module unloaded")

    def _on_attack_swing(self, session, payload: bytes):
        """Handle CMSG_ATTACKSWING — player initiates melee attack."""
        if len(payload) < 8:
            return
        target_guid = struct.unpack_from("<Q", payload, 0)[0]
        log.info(f"{session.char['name']} attacks target {target_guid:#x}")

        if getattr(session, "health", 0) <= 0:
            return
        if getattr(session, "_is_ghost", False):
            return  # Ghosts can't attack

        _start_player_attack(session, target_guid)

    def _on_attack_stop(self, session, payload: bytes):
        """Handle CMSG_ATTACKSTOP — player stops attacking."""
        log.debug(f"{session.char['name'] if session.char else '?'} stops attack")
        _stop_player_attack(session)

    def _on_repop_request(self, session, payload: bytes):
        """Handle CMSG_REPOP_REQUEST — player clicked Release Spirit."""
        log.info(f"REPOP_REQUEST from {session.char['name'] if session.char else '?'}")
        _handle_repop_request(session, payload)

    def _on_reclaim_corpse(self, session, payload: bytes):
        """Handle CMSG_RECLAIM_CORPSE — player wants to resurrect at corpse."""
        log.info(f"RECLAIM_CORPSE from {session.char['name'] if session.char else '?'}")
        _handle_reclaim_corpse(session, payload)

    def _on_corpse_query(self, session, payload: bytes):
        """Handle MSG_CORPSE_QUERY — client asks where their corpse is."""
        _handle_corpse_query(session, payload)
