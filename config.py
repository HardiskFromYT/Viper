AUTH_HOST  = "0.0.0.0"
AUTH_PORT  = 3724

WORLD_HOST = "0.0.0.0"
WORLD_PORT = 8085

REALM_NAME = "TestEmu"
REALM_IP   = "127.0.0.1"

DB_PATH = "testemu.db"

# Modules loaded on startup (and available for hot-reload).
# Order matters: core_world must come before gm so chat is registered after core.
MODULES = [
    "db",          # must be first — runs migrations before other modules load
    "core_world",
    "items",
    "movement",    # movement packet handlers — silences log spam
    "world_data",  # MaNGOS world DB: creatures, quests, NPCs, teleports, loot
    "combat",      # melee combat system
    "gm",
    "news",        # login-screen breaking-news HTTP server
]
