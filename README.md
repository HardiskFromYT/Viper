# Viper — WoW 1.12.1 Server Emulator

A World of Warcraft 1.12.1 (vanilla, build 5875) private server emulator written in pure Python 3.10+. No external dependencies — stdlib only.

## Features

**Working**
- SRP6 authentication and session encryption
- Realm list (single configurable realm)
- Character creation, enumeration, and deletion (all races/classes)
- World entry with race-appropriate starting locations and gear
- Starter spells per class
- Movement tracking and persistence
- Continuous creature/NPC visibility updates as player moves
- Chat (say/yell) with multiplayer support
- Inventory and gold management
- Creature/NPC/item queries from MaNGOS Zero world database
- GM command system (`.help`, `.teleport`, `.level`, `.speed`, `.fly`, `.gold`, `.announce`, `.kick`, `.setgm`, ...)
- GM flight (swim-in-air technique)
- Login screen news feed with WoW markup support
- Interactive server CLI with hot-reloadable modules
- Ping/pong keepalive

**Not yet implemented**
- Combat
- Quests and quest chains
- NPC interactions (vendors, quest givers, etc.)
- Groups, raids, guilds
- Spell casting and cooldowns

## Requirements

- Python 3.10+
- WoW 1.12.1 client (build 5875)

## Setup

**1. Point your WoW client at this server.**

Edit `WoW/Data/enUS/realmlist.wtf` (adjust for your client's locale folder):
```
set realmlist 127.0.0.1
set patchlist 127.0.0.1
```

**2. Start the server.**
```bash
python3 main.py
```

The auth server listens on `0.0.0.0:3724` and the world server on `0.0.0.0:8085`. A default account `test` / `test` is created automatically on first run.

**3. Log in with your WoW 1.12 client.**
- Username: `test`
- Password: `test`

## Account Management

```bash
# Create an account
python3 main.py addaccount <username> <password>
```

## Server CLI

While the server is running, an interactive prompt lets you run admin commands live:

| Command | Description |
|---|---|
| `accounts` | List all accounts |
| `players` | List connected players |
| `reload <module>` | Hot-reload a module without restarting |
| `kick <name>` | Disconnect a player |
| `announce <msg>` | Broadcast a message to all players |

## GM Commands (in-game)

Type these in the chat box while logged in with a GM account:

| Command | Description |
|---|---|
| `.help` | List available GM commands |
| `.teleport <zone>` | Teleport to a named zone or coordinates |
| `.level <n>` | Set your character's level |
| `.speed <n>` | Set movement speed multiplier (run + swim) |
| `.fly <on\|off>` | Toggle GM flight (swim in air) |
| `.gold <amount\|cap\|reset>` | Add gold (e.g. `1g27s19c`, `50`), set to cap, or reset |
| `.heal` | Restore full HP |
| `.info` | Show position, zone, and account info |
| `.players` | List online players |
| `.announce <msg>` | Server-wide announcement |
| `.kick <name>` | Kick a player |
| `.setgm <name> <level>` | Set another account's GM level (0–3) |
| `.setpos <char> <x> <y> <z>` | Set an offline character's position |

## Project Structure

```
main.py           — Entry point
server.py         — Module/packet/command registry
auth_server.py    — WoW 1.12 auth protocol (SRP6)
world_server.py   — World packet dispatch and session management
database.py       — SQLite schema, migrations, account/character queries
config.py         — Ports, realm name, active modules
opcodes.py        — WoW 1.12 opcode definitions
packets.py        — Packet building helpers
crypto.py         — RC4-like session encryption
srp6.py           — SRP6 implementation
import_world.py   — Import MaNGOS Zero MySQL dumps into world.db
cli.py            — Interactive admin CLI

modules/
  core_world.py   — Character/login/teleport handlers, starter gear
  movement.py     — Movement, logout, zone, creature/item queries
  gm.py           — GM command parser and handlers
  items.py        — Item definitions and inventory
  world_data.py   — MaNGOS world DB integration, creature spawning/visibility
  news.py         — Login screen news feed
  db.py           — Database migrations and admin commands
```

## World Data

The server uses a SQLite database (`world.db`) derived from MaNGOS Zero for authentic creature, item, and zone data. Source SQL files are in `MangosZero Source DB/`. To rebuild `world.db` from scratch:

```bash
python3 import_world.py
```

## License

GPLv3 — see [LICENSE](LICENSE).
