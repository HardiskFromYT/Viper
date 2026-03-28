"""NPC Interaction module — gossip, vendors, quest givers, trainers.

Handles the full NPC interaction protocol for vanilla 1.12.1:
  - Gossip menus (CMSG_GOSSIP_HELLO → SMSG_GOSSIP_MESSAGE)
  - NPC text queries (CMSG_NPC_TEXT_QUERY → SMSG_NPC_TEXT_UPDATE)
  - Vendors (CMSG_LIST_INVENTORY → SMSG_LIST_INVENTORY, buy/sell)
  - Quest givers (hello, status, details, accept, complete, reward)
  - Trainers (list, buy spell)
"""
import logging
import struct

from modules.base import BaseModule
from packets import ByteBuffer, pack_guid

log = logging.getLogger("npc_interact")

# ── Opcodes ──────────────────────────────────────────────────────────────────

# Gossip
CMSG_GOSSIP_HELLO          = 0x17B
CMSG_GOSSIP_SELECT_OPTION  = 0x17C
SMSG_GOSSIP_MESSAGE        = 0x17D
SMSG_GOSSIP_COMPLETE       = 0x17E
CMSG_NPC_TEXT_QUERY        = 0x17F
SMSG_NPC_TEXT_UPDATE        = 0x180

# Quest
CMSG_QUESTGIVER_STATUS_QUERY = 0x182
SMSG_QUESTGIVER_STATUS       = 0x183
CMSG_QUESTGIVER_HELLO        = 0x184
SMSG_QUESTGIVER_QUEST_LIST   = 0x185
CMSG_QUESTGIVER_QUERY_QUEST  = 0x186
SMSG_QUESTGIVER_QUEST_DETAILS = 0x188
CMSG_QUESTGIVER_ACCEPT_QUEST = 0x189
CMSG_QUESTGIVER_COMPLETE_QUEST = 0x18A
SMSG_QUESTGIVER_REQUEST_ITEMS = 0x18B
SMSG_QUESTGIVER_OFFER_REWARD  = 0x18D
CMSG_QUESTGIVER_CHOOSE_REWARD = 0x18E
SMSG_QUESTGIVER_QUEST_COMPLETE = 0x191
SMSG_QUEST_CONFIRM_ACCEPT     = 0x19C

# Vendor
CMSG_LIST_INVENTORY        = 0x19E
SMSG_LIST_INVENTORY        = 0x19F
CMSG_BUY_ITEM              = 0x1A2
CMSG_SELL_ITEM             = 0x1A3
SMSG_BUY_ITEM              = 0x1A4
SMSG_BUY_FAILED            = 0x1A5
SMSG_SELL_ITEM             = 0x1A6

# Trainer
CMSG_TRAINER_LIST          = 0x1B0
SMSG_TRAINER_LIST          = 0x1B1
CMSG_TRAINER_BUY_SPELL     = 0x1B2
SMSG_TRAINER_BUY_SUCCEEDED = 0x1B3

# Binder (innkeeper)
CMSG_BINDER_ACTIVATE       = 0x1B5
SMSG_BINDER_CONFIRM        = 0x19B

SMSG_UPDATE_OBJECT = 0x0A9

# ── NpcFlags ─────────────────────────────────────────────────────────────────

NPC_FLAG_GOSSIP       = 0x00000001
NPC_FLAG_QUESTGIVER   = 0x00000002
NPC_FLAG_VENDOR       = 0x00000004
NPC_FLAG_FLIGHTMASTER = 0x00000008
NPC_FLAG_TRAINER      = 0x00000010
NPC_FLAG_SPIRITHEALER = 0x00000020
NPC_FLAG_SPIRITGUIDE  = 0x00000040
NPC_FLAG_INNKEEPER    = 0x00000080
NPC_FLAG_BANKER       = 0x00000100
NPC_FLAG_PETITIONER   = 0x00000200
NPC_FLAG_TABARDDESIGN = 0x00000400
NPC_FLAG_BATTLEMASTER = 0x00000800
NPC_FLAG_AUCTIONEER   = 0x00001000
NPC_FLAG_STABLEMASTER = 0x00002000
NPC_FLAG_REPAIR       = 0x00004000

# Quest dialog status icons
DIALOG_STATUS_NONE        = 0
DIALOG_STATUS_UNAVAILABLE = 1
DIALOG_STATUS_CHAT        = 2
DIALOG_STATUS_INCOMPLETE  = 3
DIALOG_STATUS_REWARD_REP  = 4
DIALOG_STATUS_AVAILABLE   = 5
DIALOG_STATUS_REWARD      = 6  # completable (yellow ?)

# Gossip option icons
GOSSIP_ICON_CHAT     = 0
GOSSIP_ICON_VENDOR   = 1
GOSSIP_ICON_TAXI     = 2
GOSSIP_ICON_TRAINER  = 3
GOSSIP_ICON_INTERACT = 4
GOSSIP_ICON_MONEY    = 6
GOSSIP_ICON_TALK     = 7
GOSSIP_ICON_TABARD   = 8
GOSSIP_ICON_BATTLE   = 9

CREATURE_GUID_OFFSET = 0x100000000

# ── DB helpers ───────────────────────────────────────────────────────────────

def _wdb():
    from modules.world_data import wdb
    return wdb()


def _get_creature_entry(creature_guid: int) -> int | None:
    """Get creature template entry from spawn guid."""
    spawn_id = creature_guid - CREATURE_GUID_OFFSET
    try:
        row = _wdb().execute("SELECT id FROM creature WHERE guid=?", (spawn_id,)).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def _get_npc_template(entry: int) -> dict | None:
    from modules.world_data import get_creature_template
    tpl = get_creature_template(entry)
    return dict(tpl) if tpl else None


def _get_npc_flags(entry: int) -> int:
    tpl = _get_npc_template(entry)
    return int(tpl.get("NpcFlags") or 0) if tpl else 0


def _get_gossip_menu_id(entry: int) -> int:
    tpl = _get_npc_template(entry)
    return int(tpl.get("GossipMenuId") or 0) if tpl else 0


def _get_npc_text(text_id: int) -> dict | None:
    try:
        row = _wdb().execute("SELECT * FROM npc_text WHERE ID=?", (text_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _get_gossip_options(menu_id: int) -> list:
    try:
        rows = _wdb().execute(
            "SELECT * FROM gossip_menu_option WHERE menu_id=? ORDER BY id",
            (menu_id,)
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def _get_gossip_text_id(menu_id: int) -> int:
    """Get text_id from gossip_menu table."""
    try:
        row = _wdb().execute(
            "SELECT text_id FROM gossip_menu WHERE entry=? LIMIT 1",
            (menu_id,)
        ).fetchone()
        return int(row["text_id"]) if row else 1
    except Exception:
        return 1


def _get_quest_relations(entry: int, role: int) -> list:
    """Get quests for a creature. role=0 = quest starters, role=1 = quest enders."""
    try:
        rows = _wdb().execute(
            "SELECT quest FROM quest_relations WHERE entry=? AND role=? AND actor=0",
            (entry, role)
        ).fetchall()
        return [int(r["quest"]) for r in rows] if rows else []
    except Exception:
        return []


def _get_quest(quest_id: int) -> dict | None:
    try:
        row = _wdb().execute("SELECT * FROM quest_template WHERE entry=?", (quest_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _get_vendor_items(entry: int) -> list:
    from modules.world_data import get_vendor_items
    rows = get_vendor_items(entry)
    return [dict(r) for r in rows] if rows else []


def _get_trainer_spells(entry: int) -> list:
    from modules.world_data import get_npc_trainer
    rows = get_npc_trainer(entry, limit=200)
    return [dict(r) for r in rows] if rows else []


def _get_item_template(item_id: int) -> dict | None:
    from modules.world_data import get_item_template
    row = get_item_template(item_id)
    return dict(row) if row else None


# ── Quest status logic ───────────────────────────────────────────────────────

def _get_quest_status_for_npc(session, entry: int) -> int:
    """Determine the quest icon to show over an NPC.
    Returns highest-priority DIALOG_STATUS value."""
    best = DIALOG_STATUS_NONE

    # Check completable quests (role=1 = quest ender)
    ender_quests = _get_quest_relations(entry, 1)
    for qid in ender_quests:
        # If player has this quest and it's complete, show yellow ?
        if _player_has_quest(session, qid):
            if _is_quest_complete(session, qid):
                best = max(best, DIALOG_STATUS_REWARD)
            else:
                best = max(best, DIALOG_STATUS_INCOMPLETE)

    # Check available quests (role=0 = quest starter)
    starter_quests = _get_quest_relations(entry, 0)
    for qid in starter_quests:
        if _player_has_quest(session, qid):
            continue  # Already on this quest
        if _player_completed_quest(session, qid):
            continue  # Already done
        quest = _get_quest(qid)
        if not quest:
            continue
        min_level = int(quest.get("MinLevel") or 1)
        player_level = session.char["level"] if session.char else 1
        if player_level >= min_level:
            best = max(best, DIALOG_STATUS_AVAILABLE)
        else:
            best = max(best, DIALOG_STATUS_UNAVAILABLE)

    return best


def _player_has_quest(session, quest_id: int) -> bool:
    """Check if player currently has a quest in their log."""
    quest_log = getattr(session, "_quest_log", {})
    return quest_id in quest_log


def _is_quest_complete(session, quest_id: int) -> bool:
    """Check if a quest in the player's log is complete (objectives met)."""
    quest_log = getattr(session, "_quest_log", {})
    entry = quest_log.get(quest_id)
    if not entry:
        return False
    return entry.get("complete", False)


def _player_completed_quest(session, quest_id: int) -> bool:
    """Check if player has already turned in this quest."""
    completed = getattr(session, "_completed_quests", set())
    return quest_id in completed


# ── Gossip packet builders ───────────────────────────────────────────────────

def _build_gossip_message(npc_guid: int, text_id: int, menu_id: int,
                           gossip_options: list, quests: list) -> bytes:
    """Build SMSG_GOSSIP_MESSAGE."""
    buf = ByteBuffer()
    buf.uint64(npc_guid)
    buf.uint32(text_id)
    buf.uint32(menu_id)
    buf.uint32(len(gossip_options))

    for i, opt in enumerate(gossip_options):
        buf.uint32(i)                                          # option index
        buf.uint8(int(opt.get("option_icon") or 0))            # icon
        buf.uint8(int(opt.get("box_coded") or 0))              # coded
        buf.uint32(int(opt.get("box_money") or 0))             # money required
        buf.cstring(str(opt.get("option_text") or ""))         # text
        buf.cstring(str(opt.get("box_text") or ""))            # box text

    buf.uint32(len(quests))
    for q in quests:
        buf.uint32(q["quest_id"])
        buf.uint32(q["icon"])
        buf.uint32(q["level"])
        buf.cstring(q["title"])

    return buf.bytes()


def _build_npc_text_update(text_id: int, npc_text: dict | None) -> bytes:
    """Build SMSG_NPC_TEXT_UPDATE."""
    buf = ByteBuffer()
    buf.uint32(text_id)

    for i in range(8):
        if npc_text:
            prob = float(npc_text.get(f"prob{i}") or 0.0)
            text_m = str(npc_text.get(f"text{i}_0") or "")
            text_f = str(npc_text.get(f"text{i}_1") or "")
            lang = int(npc_text.get(f"lang{i}") or 0)
        else:
            prob = 1.0 if i == 0 else 0.0
            text_m = "Greetings." if i == 0 else ""
            text_f = ""
            lang = 0

        buf.float32(prob)
        buf.cstring(text_m)
        buf.cstring(text_f)
        buf.uint32(lang)
        # 3 emote pairs (delay + id) = 6 uint32s
        for j in range(3):
            if npc_text:
                delay = int(npc_text.get(f"em{i}_{j}_delay") or 0)
                emote = int(npc_text.get(f"em{i}_{j}") or 0)
            else:
                delay = 0
                emote = 0
            buf.uint32(delay)
            buf.uint32(emote)

    return buf.bytes()


def _build_vendor_list(vendor_guid: int, items: list) -> bytes:
    """Build SMSG_LIST_INVENTORY."""
    buf = ByteBuffer()
    buf.uint64(vendor_guid)

    if not items:
        buf.uint8(0)  # item count = 0
        return buf.bytes()

    # Filter items with valid templates and build list
    valid_items = []
    for item in items:
        item_id = int(item.get("item") or 0)
        tpl = _get_item_template(item_id)
        if not tpl:
            continue
        valid_items.append((item, tpl))

    buf.uint8(len(valid_items))
    for idx, (item, tpl) in enumerate(valid_items):
        item_id = int(item.get("item") or 0)
        maxcount = int(item.get("maxcount") or 0)
        buy_price = int(tpl.get("BuyPrice") or 0)
        display_id = int(tpl.get("displayid") or 0)
        max_dur = int(tpl.get("MaxDurability") or 0)
        stackable = int(tpl.get("stackable") or 1)

        buf.uint32(idx + 1)           # muid (1-based slot index)
        buf.uint32(item_id)           # item entry
        buf.uint32(display_id)        # display ID
        buf.uint32(maxcount if maxcount > 0 else 0xFFFFFFFF)  # available count
        buf.uint32(buy_price)         # price
        buf.uint32(max_dur)           # max durability
        buf.uint32(max(1, stackable)) # buy count (stack size)

    return buf.bytes()


def _build_quest_details(npc_guid: int, quest: dict) -> bytes:
    """Build SMSG_QUESTGIVER_QUEST_DETAILS for a quest."""
    buf = ByteBuffer()
    buf.uint64(npc_guid)
    buf.uint32(int(quest["entry"]))
    buf.cstring(str(quest.get("Title") or "Quest"))
    buf.cstring(str(quest.get("Details") or ""))
    buf.cstring(str(quest.get("Objectives") or ""))
    buf.uint32(1)  # auto_accept = no (show Accept button)

    # Reward choice items (choose one)
    choice_items = []
    for i in range(1, 7):
        rid = int(quest.get(f"RewChoiceItemId{i}") or 0)
        rcount = int(quest.get(f"RewChoiceItemCount{i}") or 0)
        if rid:
            tpl = _get_item_template(rid)
            display = int(tpl.get("displayid") or 0) if tpl else 0
            choice_items.append((rid, rcount, display))

    buf.uint32(len(choice_items))
    for rid, rcount, display in choice_items:
        buf.uint32(rid)
        buf.uint32(rcount)
        buf.uint32(display)

    # Reward items (guaranteed)
    reward_items = []
    for i in range(1, 5):
        rid = int(quest.get(f"RewItemId{i}") or 0)
        rcount = int(quest.get(f"RewItemCount{i}") or 0)
        if rid:
            tpl = _get_item_template(rid)
            display = int(tpl.get("displayid") or 0) if tpl else 0
            reward_items.append((rid, rcount, display))

    buf.uint32(len(reward_items))
    for rid, rcount, display in reward_items:
        buf.uint32(rid)
        buf.uint32(rcount)
        buf.uint32(display)

    # Reward money
    buf.uint32(max(0, int(quest.get("RewOrReqMoney") or 0)))

    # Required objectives (4 slots)
    for i in range(1, 5):
        buf.uint32(int(quest.get(f"ReqCreatureOrGOId{i}") or 0) & 0xFFFFFFFF)
        buf.uint32(int(quest.get(f"ReqCreatureOrGOCount{i}") or 0))
        buf.uint32(int(quest.get(f"ReqItemId{i}") or 0))
        buf.uint32(int(quest.get(f"ReqItemCount{i}") or 0))

    # Emotes (4 slots)
    for i in range(1, 5):
        buf.uint32(int(quest.get(f"DetailsEmote{i}") or 0))
        buf.uint32(int(quest.get(f"DetailsEmoteDelay{i}") or 0))

    return buf.bytes()


def _build_quest_list(npc_guid: int, greeting: str, quests: list) -> bytes:
    """Build SMSG_QUESTGIVER_QUEST_LIST."""
    buf = ByteBuffer()
    buf.uint64(npc_guid)
    buf.cstring(greeting)
    buf.uint32(0)   # emote delay
    buf.uint32(0)   # emote id
    buf.uint8(len(quests))

    for q in quests:
        buf.uint32(q["quest_id"])
        buf.uint32(q["icon"])
        buf.uint32(q["level"])
        buf.cstring(q["title"])

    return buf.bytes()


def _build_offer_reward(npc_guid: int, quest: dict) -> bytes:
    """Build SMSG_QUESTGIVER_OFFER_REWARD for quest turn-in."""
    buf = ByteBuffer()
    buf.uint64(npc_guid)
    buf.uint32(int(quest["entry"]))
    buf.cstring(str(quest.get("Title") or "Quest"))
    buf.cstring(str(quest.get("OfferRewardText") or "Well done!"))
    buf.uint32(1)  # auto_finish = no

    # Emotes (4 slots)
    buf.uint32(4)
    for i in range(4):
        buf.uint32(0)  # emote delay
        buf.uint32(0)  # emote id

    # Reward choice items
    choice_items = []
    for i in range(1, 7):
        rid = int(quest.get(f"RewChoiceItemId{i}") or 0)
        rcount = int(quest.get(f"RewChoiceItemCount{i}") or 0)
        if rid:
            tpl = _get_item_template(rid)
            display = int(tpl.get("displayid") or 0) if tpl else 0
            choice_items.append((rid, rcount, display))

    buf.uint32(len(choice_items))
    for rid, rcount, display in choice_items:
        buf.uint32(rid)
        buf.uint32(rcount)
        buf.uint32(display)

    # Reward items (guaranteed)
    reward_items = []
    for i in range(1, 5):
        rid = int(quest.get(f"RewItemId{i}") or 0)
        rcount = int(quest.get(f"RewItemCount{i}") or 0)
        if rid:
            tpl = _get_item_template(rid)
            display = int(tpl.get("displayid") or 0) if tpl else 0
            reward_items.append((rid, rcount, display))

    buf.uint32(len(reward_items))
    for rid, rcount, display in reward_items:
        buf.uint32(rid)
        buf.uint32(rcount)
        buf.uint32(display)

    # Reward money
    buf.uint32(max(0, int(quest.get("RewOrReqMoney") or 0)))

    return buf.bytes()


def _build_quest_complete(quest: dict) -> bytes:
    """Build SMSG_QUESTGIVER_QUEST_COMPLETE."""
    buf = ByteBuffer()
    buf.uint32(int(quest["entry"]))
    buf.uint32(3)   # reward type = 3 (normal complete)
    # XP reward
    xp = int(quest.get("RewXP") or 0)
    if not xp:
        # Estimate from quest level
        qlevel = int(quest.get("QuestLevel") or 1)
        xp = qlevel * 50
    buf.uint32(xp)
    buf.uint32(max(0, int(quest.get("RewOrReqMoney") or 0)))
    # Reward items
    reward_count = 0
    for i in range(1, 5):
        if int(quest.get(f"RewItemId{i}") or 0):
            reward_count += 1
    buf.uint32(reward_count)
    for i in range(1, 5):
        rid = int(quest.get(f"RewItemId{i}") or 0)
        rcount = int(quest.get(f"RewItemCount{i}") or 0)
        if rid:
            buf.uint32(rid)
            buf.uint32(rcount)
    return buf.bytes()


def _build_trainer_list(trainer_guid: int, trainer_type: int,
                         spells: list, greeting: str) -> bytes:
    """Build SMSG_TRAINER_LIST."""
    buf = ByteBuffer()
    buf.uint64(trainer_guid)
    buf.uint32(trainer_type)  # 0=class, 1=mounts, 2=tradeskills, 3=pets
    buf.uint32(len(spells))

    for sp in spells:
        spell_id = int(sp.get("spell") or 0)
        cost = int(sp.get("spellcost") or 0)
        req_level = int(sp.get("reqlevel") or 0)
        req_skill = int(sp.get("reqskill") or 0)
        req_skill_val = int(sp.get("reqskillvalue") or 0)

        buf.uint32(spell_id)
        buf.uint8(0)              # state: 0=available (green)
        buf.uint32(cost)          # spell cost
        buf.uint32(0)             # proficiency points
        buf.uint32(0)             # proficiency max
        buf.uint8(req_level)      # required level
        buf.uint32(req_skill)     # required skill
        buf.uint32(req_skill_val) # required skill value
        buf.uint32(0)             # prerequisite spell
        buf.uint32(0)             # unknown1
        buf.uint32(0)             # unknown2

    buf.cstring(greeting)
    return buf.bytes()


# ── Interaction handlers ─────────────────────────────────────────────────────

def _handle_gossip_hello(session, npc_guid: int):
    """Handle right-click on NPC — build and send gossip menu or direct action."""
    entry = _get_creature_entry(npc_guid)
    if not entry:
        log.warning(f"GOSSIP_HELLO: unknown creature guid {npc_guid:#x}")
        return

    npc_flags = _get_npc_flags(entry)
    log.info(f"GOSSIP_HELLO: entry={entry} flags={npc_flags:#x}")

    # Collect available quests for this NPC
    quest_items = _collect_npc_quests(session, entry)

    # Get gossip menu from DB
    gossip_menu_id = _get_gossip_menu_id(entry)
    text_id = _get_gossip_text_id(gossip_menu_id) if gossip_menu_id else 1

    # Build gossip options based on NPC flags
    gossip_options = []

    if gossip_menu_id:
        # Use DB-defined gossip options
        db_options = _get_gossip_options(gossip_menu_id)
        for opt in db_options:
            # Filter options by NPC flag match
            opt_npcflag = int(opt.get("npc_option_npcflag") or 0)
            if opt_npcflag == 0 or (npc_flags & opt_npcflag):
                gossip_options.append(opt)

    if not gossip_options:
        # Auto-generate gossip options from NPC flags
        if npc_flags & NPC_FLAG_VENDOR:
            gossip_options.append({
                "option_icon": GOSSIP_ICON_VENDOR,
                "option_text": "I want to browse your goods.",
                "option_id": 3,  # GOSSIP_OPTION_VENDOR
                "npc_option_npcflag": NPC_FLAG_VENDOR,
                "box_coded": 0, "box_money": 0, "box_text": "",
            })
        if npc_flags & NPC_FLAG_TRAINER:
            gossip_options.append({
                "option_icon": GOSSIP_ICON_TRAINER,
                "option_text": "Train me.",
                "option_id": 5,  # GOSSIP_OPTION_TRAINER
                "npc_option_npcflag": NPC_FLAG_TRAINER,
                "box_coded": 0, "box_money": 0, "box_text": "",
            })
        if npc_flags & NPC_FLAG_INNKEEPER:
            gossip_options.append({
                "option_icon": GOSSIP_ICON_CHAT,
                "option_text": "Make this inn your home.",
                "option_id": 8,  # GOSSIP_OPTION_INNKEEPER
                "npc_option_npcflag": NPC_FLAG_INNKEEPER,
                "box_coded": 0, "box_money": 0, "box_text": "",
            })

    # If only one action and no quests, go directly
    if not gossip_options and not quest_items:
        # Pure questgiver with no available quests — just show empty gossip
        if npc_flags & NPC_FLAG_QUESTGIVER:
            pass  # Fall through to send empty gossip
        elif npc_flags & NPC_FLAG_VENDOR:
            _send_vendor_list(session, npc_guid, entry)
            return
        elif npc_flags & NPC_FLAG_TRAINER:
            _send_trainer_list(session, npc_guid, entry)
            return

    # If no gossip options and only one quest available, go directly to quest details
    if not gossip_options and len(quest_items) == 1:
        q = quest_items[0]
        quest = _get_quest(q["quest_id"])
        if quest:
            if q["icon"] == DIALOG_STATUS_REWARD:
                # Completable quest — show reward
                session._send(SMSG_QUESTGIVER_OFFER_REWARD,
                              _build_offer_reward(npc_guid, quest))
            else:
                # Available quest — show details
                session._send(SMSG_QUESTGIVER_QUEST_DETAILS,
                              _build_quest_details(npc_guid, quest))
            return

    # Send gossip message
    pkt = _build_gossip_message(npc_guid, text_id, gossip_menu_id,
                                 gossip_options, quest_items)
    session._send(SMSG_GOSSIP_MESSAGE, pkt)

    # Store gossip state on session for handling option selection
    session._gossip_npc_guid = npc_guid
    session._gossip_entry = entry
    session._gossip_options = gossip_options


def _handle_gossip_select(session, npc_guid: int, option_id: int):
    """Handle player selecting a gossip option."""
    entry = getattr(session, "_gossip_entry", None)
    options = getattr(session, "_gossip_options", [])

    if not entry or option_id >= len(options):
        session._send(SMSG_GOSSIP_COMPLETE, b"")
        return

    opt = options[option_id]
    opt_type = int(opt.get("option_id") or 0)
    npc_flags = _get_npc_flags(entry)

    log.info(f"GOSSIP_SELECT: option_id={option_id} type={opt_type}")

    # Route based on option type
    if opt_type == 3 or (int(opt.get("npc_option_npcflag") or 0) & NPC_FLAG_VENDOR):
        # Vendor
        session._send(SMSG_GOSSIP_COMPLETE, b"")
        _send_vendor_list(session, npc_guid, entry)
    elif opt_type == 5 or (int(opt.get("npc_option_npcflag") or 0) & NPC_FLAG_TRAINER):
        # Trainer
        session._send(SMSG_GOSSIP_COMPLETE, b"")
        _send_trainer_list(session, npc_guid, entry)
    elif opt_type == 2 or (int(opt.get("npc_option_npcflag") or 0) & NPC_FLAG_QUESTGIVER):
        # Quest giver — send quest list
        session._send(SMSG_GOSSIP_COMPLETE, b"")
        _send_quest_list(session, npc_guid, entry)
    else:
        # Check if there's a sub-menu
        action_menu = int(opt.get("action_menu_id") or 0)
        if action_menu > 0:
            # Send new gossip for the sub-menu
            text_id = _get_gossip_text_id(action_menu)
            sub_options = _get_gossip_options(action_menu)
            pkt = _build_gossip_message(npc_guid, text_id, action_menu,
                                         sub_options, [])
            session._send(SMSG_GOSSIP_MESSAGE, pkt)
            session._gossip_options = sub_options
        else:
            session._send(SMSG_GOSSIP_COMPLETE, b"")


def _collect_npc_quests(session, entry: int) -> list:
    """Collect quests available from NPC with their status icons."""
    quest_items = []

    # Completable quests (ender)
    for qid in _get_quest_relations(entry, 1):
        if _player_has_quest(session, qid):
            quest = _get_quest(qid)
            if quest:
                complete = _is_quest_complete(session, qid)
                quest_items.append({
                    "quest_id": qid,
                    "icon": DIALOG_STATUS_REWARD if complete else DIALOG_STATUS_INCOMPLETE,
                    "level": int(quest.get("QuestLevel") or 1),
                    "title": str(quest.get("Title") or "Quest"),
                })

    # Available quests (starter)
    for qid in _get_quest_relations(entry, 0):
        if _player_has_quest(session, qid) or _player_completed_quest(session, qid):
            continue
        quest = _get_quest(qid)
        if not quest:
            continue
        min_level = int(quest.get("MinLevel") or 1)
        player_level = session.char["level"] if session.char else 1
        if player_level >= min_level:
            quest_items.append({
                "quest_id": qid,
                "icon": DIALOG_STATUS_AVAILABLE,
                "level": int(quest.get("QuestLevel") or 1),
                "title": str(quest.get("Title") or "Quest"),
            })

    return quest_items


def _send_vendor_list(session, npc_guid: int, entry: int):
    """Send vendor item list to player."""
    items = _get_vendor_items(entry)
    pkt = _build_vendor_list(npc_guid, items)
    session._send(SMSG_LIST_INVENTORY, pkt)
    log.info(f"Sent vendor list: {len(items)} items from entry {entry}")


def _send_trainer_list(session, npc_guid: int, entry: int):
    """Send trainer spell list to player."""
    spells = _get_trainer_spells(entry)
    tpl = _get_npc_template(entry)
    name = tpl.get("Name", "Trainer") if tpl else "Trainer"
    pkt = _build_trainer_list(npc_guid, 0, spells, f"Ready for some training, $N?")
    session._send(SMSG_TRAINER_LIST, pkt)
    log.info(f"Sent trainer list: {len(spells)} spells from {name}")


def _send_quest_list(session, npc_guid: int, entry: int):
    """Send quest list for a questgiver."""
    quest_items = _collect_npc_quests(session, entry)
    tpl = _get_npc_template(entry)
    greeting = "What can I do for you?"
    if tpl:
        gossip_id = int(tpl.get("GossipMenuId") or 0)
        if gossip_id:
            text_id = _get_gossip_text_id(gossip_id)
            npc_text = _get_npc_text(text_id)
            if npc_text:
                greeting = str(npc_text.get("text0_0") or greeting) or greeting

    pkt = _build_quest_list(npc_guid, greeting, quest_items)
    session._send(SMSG_QUESTGIVER_QUEST_LIST, pkt)


def _handle_questgiver_hello(session, npc_guid: int):
    """Handle CMSG_QUESTGIVER_HELLO — direct questgiver interaction."""
    entry = _get_creature_entry(npc_guid)
    if not entry:
        return
    _send_quest_list(session, npc_guid, entry)


def _handle_questgiver_query(session, npc_guid: int, quest_id: int):
    """Handle CMSG_QUESTGIVER_QUERY_QUEST — player wants quest details."""
    quest = _get_quest(quest_id)
    if not quest:
        return

    # Check if this is a completable quest
    if _player_has_quest(session, quest_id):
        if _is_quest_complete(session, quest_id):
            # Show reward screen
            session._send(SMSG_QUESTGIVER_OFFER_REWARD,
                          _build_offer_reward(npc_guid, quest))
        else:
            # Show request items (objectives not met)
            _send_request_items(session, npc_guid, quest, complete=False)
        return

    # New quest — show details
    session._send(SMSG_QUESTGIVER_QUEST_DETAILS,
                  _build_quest_details(npc_guid, quest))


def _handle_questgiver_accept(session, npc_guid: int, quest_id: int):
    """Handle CMSG_QUESTGIVER_ACCEPT_QUEST — player accepts a quest."""
    quest = _get_quest(quest_id)
    if not quest:
        return

    if _player_has_quest(session, quest_id):
        return  # Already have it
    if _player_completed_quest(session, quest_id):
        session.send_sys_msg("You have already completed that quest.")
        return

    # Add to quest log
    if not hasattr(session, "_quest_log"):
        session._quest_log = {}

    session._quest_log[quest_id] = {
        "quest": quest,
        "complete": False,
        "objectives": {},
    }

    log.info(f"{session.char['name']} accepted quest [{quest_id}] {quest.get('Title')}")
    session.send_sys_msg(f"Quest accepted: {quest.get('Title')}")

    # Auto-complete quests that have no objectives (exploration, delivery, etc.)
    has_objectives = False
    for i in range(1, 5):
        if int(quest.get(f"ReqCreatureOrGOId{i}") or 0) or int(quest.get(f"ReqItemId{i}") or 0):
            has_objectives = True
            break
    if not has_objectives:
        session._quest_log[quest_id]["complete"] = True


def _handle_questgiver_complete(session, npc_guid: int, quest_id: int):
    """Handle CMSG_QUESTGIVER_COMPLETE_QUEST — player talks to NPC to turn in."""
    quest = _get_quest(quest_id)
    if not quest:
        return
    if not _player_has_quest(session, quest_id):
        return

    if _is_quest_complete(session, quest_id):
        # Show reward selection
        session._send(SMSG_QUESTGIVER_OFFER_REWARD,
                      _build_offer_reward(npc_guid, quest))
    else:
        # Show request items (not done yet)
        _send_request_items(session, npc_guid, quest, complete=False)


def _handle_questgiver_choose_reward(session, npc_guid: int, quest_id: int,
                                      reward_choice: int):
    """Handle CMSG_QUESTGIVER_CHOOSE_REWARD — player picks reward and completes."""
    quest = _get_quest(quest_id)
    if not quest:
        return
    if not _player_has_quest(session, quest_id):
        return

    # Remove from quest log, add to completed
    session._quest_log.pop(quest_id, None)
    if not hasattr(session, "_completed_quests"):
        session._completed_quests = set()
    session._completed_quests.add(quest_id)

    # Give money reward
    money = max(0, int(quest.get("RewOrReqMoney") or 0))
    if money > 0:
        from database import get_char_money, set_char_money
        current = get_char_money(session.db_path, session.char["id"])
        set_char_money(session.db_path, session.char["id"], current + money)

    # Send quest complete
    pkt = _build_quest_complete(quest)
    session._send(SMSG_QUESTGIVER_QUEST_COMPLETE, pkt)

    log.info(f"{session.char['name']} completed quest [{quest_id}] {quest.get('Title')}"
             f" reward_choice={reward_choice} money={money}")
    session.send_sys_msg(f"Quest completed: {quest.get('Title')}")


def _send_request_items(session, npc_guid: int, quest: dict, complete: bool):
    """Send SMSG_QUESTGIVER_REQUEST_ITEMS — turn-in screen."""
    buf = ByteBuffer()
    buf.uint64(npc_guid)
    buf.uint32(int(quest["entry"]))
    buf.cstring(str(quest.get("Title") or "Quest"))
    buf.cstring(str(quest.get("RequestItemsText") or
                    ("Have you completed the task?" if not complete else "")))
    buf.uint32(0)  # emote delay
    buf.uint32(0)  # emote id
    buf.uint32(0)  # auto_finish
    buf.uint32(0)  # required money

    # Required items
    req_items = []
    for i in range(1, 5):
        rid = int(quest.get(f"ReqItemId{i}") or 0)
        rcount = int(quest.get(f"ReqItemCount{i}") or 0)
        if rid:
            tpl = _get_item_template(rid)
            display = int(tpl.get("displayid") or 0) if tpl else 0
            req_items.append((rid, rcount, display))

    buf.uint32(len(req_items))
    for rid, rcount, display in req_items:
        buf.uint32(rid)
        buf.uint32(rcount)
        buf.uint32(display)

    # completable flag: 0x00 = incomplete, 0x03 = completable
    buf.uint32(0x03 if complete else 0x00)

    session._send(SMSG_QUESTGIVER_REQUEST_ITEMS, buf.bytes())


def _handle_questgiver_status(session, npc_guid: int):
    """Handle CMSG_QUESTGIVER_STATUS_QUERY — what icon to show over NPC."""
    entry = _get_creature_entry(npc_guid)
    status = DIALOG_STATUS_NONE
    if entry:
        status = _get_quest_status_for_npc(session, entry)

    buf = ByteBuffer()
    buf.uint64(npc_guid)
    buf.uint32(status)
    session._send(SMSG_QUESTGIVER_STATUS, buf.bytes())


def _handle_npc_text_query(session, text_id: int, npc_guid: int):
    """Handle CMSG_NPC_TEXT_QUERY — client wants greeting text."""
    npc_text = _get_npc_text(text_id)
    pkt = _build_npc_text_update(text_id, npc_text)
    session._send(SMSG_NPC_TEXT_UPDATE, pkt)


def _handle_buy_item(session, vendor_guid: int, item_id: int, count: int):
    """Handle CMSG_BUY_ITEM — player buys from vendor."""
    if count < 1:
        count = 1

    tpl = _get_item_template(item_id)
    if not tpl:
        buf = ByteBuffer()
        buf.uint64(vendor_guid)
        buf.uint32(item_id)
        buf.uint8(0)  # BUY_ERR_CANT_FIND_ITEM
        session._send(SMSG_BUY_FAILED, buf.bytes())
        return

    price = int(tpl.get("BuyPrice") or 0) * count
    from database import get_char_money, set_char_money
    current_money = get_char_money(session.db_path, session.char["id"])

    if current_money < price:
        buf = ByteBuffer()
        buf.uint64(vendor_guid)
        buf.uint32(item_id)
        buf.uint8(2)  # BUY_ERR_NOT_ENOUGHT_MONEY
        session._send(SMSG_BUY_FAILED, buf.bytes())
        return

    # Deduct money
    set_char_money(session.db_path, session.char["id"], current_money - price)

    # Add item to inventory
    from database import add_inventory_item
    add_inventory_item(session.db_path, session.char["id"], item_id, count)

    log.info(f"{session.char['name']} bought item {item_id} x{count} for {price}c")
    session.send_sys_msg(f"Purchased {tpl.get('name', 'item')} x{count}.")


def _handle_sell_item(session, vendor_guid: int, item_guid: int, count: int):
    """Handle CMSG_SELL_ITEM — player sells to vendor."""
    # For now, just acknowledge — full sell requires inventory system
    buf = ByteBuffer()
    buf.uint64(vendor_guid)
    buf.uint64(item_guid)
    buf.uint8(1)  # SELL_ERR_CANT_SELL_ITEM
    session._send(SMSG_SELL_ITEM, buf.bytes())


# ── NpcFlags on creature_template UPDATE_OBJECT ─────────────────────────────
# The client uses NpcFlags from UNIT_NPC_FLAGS field (0x0025) to decide
# what interactions are available. We need to send this in creature objects.

UNIT_NPC_FLAGS = 0x0093


# ── Module ───────────────────────────────────────────────────────────────────

class Module(BaseModule):
    name = "npc_interact"

    def on_load(self, server):
        self._server = server

        # Gossip
        self.reg_packet(server, CMSG_GOSSIP_HELLO, self._on_gossip_hello)
        self.reg_packet(server, CMSG_GOSSIP_SELECT_OPTION, self._on_gossip_select)
        self.reg_packet(server, CMSG_NPC_TEXT_QUERY, self._on_npc_text_query)

        # Quest
        self.reg_packet(server, CMSG_QUESTGIVER_STATUS_QUERY, self._on_questgiver_status)
        self.reg_packet(server, CMSG_QUESTGIVER_HELLO, self._on_questgiver_hello)
        self.reg_packet(server, CMSG_QUESTGIVER_QUERY_QUEST, self._on_questgiver_query)
        self.reg_packet(server, CMSG_QUESTGIVER_ACCEPT_QUEST, self._on_questgiver_accept)
        self.reg_packet(server, CMSG_QUESTGIVER_COMPLETE_QUEST, self._on_questgiver_complete)
        self.reg_packet(server, CMSG_QUESTGIVER_CHOOSE_REWARD, self._on_questgiver_reward)

        # Vendor
        self.reg_packet(server, CMSG_LIST_INVENTORY, self._on_list_inventory)
        self.reg_packet(server, CMSG_BUY_ITEM, self._on_buy_item)
        self.reg_packet(server, CMSG_SELL_ITEM, self._on_sell_item)

        # Trainer
        self.reg_packet(server, CMSG_TRAINER_LIST, self._on_trainer_list)

        log.info("npc_interact module loaded")

    def on_unload(self, server):
        log.info("npc_interact module unloaded")

    # ── Gossip ───────────────────────────────────────────────────────

    def _on_gossip_hello(self, session, payload: bytes):
        if len(payload) < 8 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        _handle_gossip_hello(session, npc_guid)

    def _on_gossip_select(self, session, payload: bytes):
        if len(payload) < 12 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        option_id = struct.unpack_from("<I", payload, 8)[0]
        _handle_gossip_select(session, npc_guid, option_id)

    def _on_npc_text_query(self, session, payload: bytes):
        if len(payload) < 12 or not session.char:
            return
        text_id = struct.unpack_from("<I", payload, 0)[0]
        npc_guid = struct.unpack_from("<Q", payload, 4)[0]
        _handle_npc_text_query(session, text_id, npc_guid)

    # ── Quest ────────────────────────────────────────────────────────

    def _on_questgiver_status(self, session, payload: bytes):
        if len(payload) < 8 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        _handle_questgiver_status(session, npc_guid)

    def _on_questgiver_hello(self, session, payload: bytes):
        if len(payload) < 8 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        _handle_questgiver_hello(session, npc_guid)

    def _on_questgiver_query(self, session, payload: bytes):
        if len(payload) < 12 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        quest_id = struct.unpack_from("<I", payload, 8)[0]
        _handle_questgiver_query(session, npc_guid, quest_id)

    def _on_questgiver_accept(self, session, payload: bytes):
        if len(payload) < 12 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        quest_id = struct.unpack_from("<I", payload, 8)[0]
        _handle_questgiver_accept(session, npc_guid, quest_id)

    def _on_questgiver_complete(self, session, payload: bytes):
        if len(payload) < 12 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        quest_id = struct.unpack_from("<I", payload, 8)[0]
        _handle_questgiver_complete(session, npc_guid, quest_id)

    def _on_questgiver_reward(self, session, payload: bytes):
        if len(payload) < 16 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        quest_id = struct.unpack_from("<I", payload, 8)[0]
        reward_choice = struct.unpack_from("<I", payload, 12)[0]
        _handle_questgiver_choose_reward(session, npc_guid, quest_id, reward_choice)

    # ── Vendor ───────────────────────────────────────────────────────

    def _on_list_inventory(self, session, payload: bytes):
        if len(payload) < 8 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        entry = _get_creature_entry(npc_guid)
        if entry:
            _send_vendor_list(session, npc_guid, entry)

    def _on_buy_item(self, session, payload: bytes):
        if len(payload) < 14 or not session.char:
            return
        vendor_guid = struct.unpack_from("<Q", payload, 0)[0]
        item_id = struct.unpack_from("<I", payload, 8)[0]
        count = payload[12] if len(payload) > 12 else 1
        _handle_buy_item(session, vendor_guid, item_id, max(1, count))

    def _on_sell_item(self, session, payload: bytes):
        if len(payload) < 17 or not session.char:
            return
        vendor_guid = struct.unpack_from("<Q", payload, 0)[0]
        item_guid = struct.unpack_from("<Q", payload, 8)[0]
        count = payload[16] if len(payload) > 16 else 0
        _handle_sell_item(session, vendor_guid, item_guid, count)

    # ── Trainer ──────────────────────────────────────────────────────

    def _on_trainer_list(self, session, payload: bytes):
        if len(payload) < 8 or not session.char:
            return
        npc_guid = struct.unpack_from("<Q", payload, 0)[0]
        entry = _get_creature_entry(npc_guid)
        if entry:
            _send_trainer_list(session, npc_guid, entry)
