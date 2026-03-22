#!/usr/bin/env python3
"""TestEmu — WoW 1.12 server emulator entry point."""
import asyncio
import logging
import sys

from database import init_db, create_account
from server import Server
from auth_server import start_auth_server
from world_server import start_world_server
from cli import run_cli
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Show debug details for auth diagnostics
logging.getLogger("srp6").setLevel(logging.DEBUG)
logging.getLogger("world").setLevel(logging.DEBUG)
log = logging.getLogger("main")


def bootstrap():
    init_db(config.DB_PATH)
    create_account(config.DB_PATH, "test", "test")   # default account
    # Always reset password so verifier matches current SRP6 code
    from database import set_account_password
    set_account_password(config.DB_PATH, "test", "test")


async def run():
    server = Server(config.DB_PATH)

    # Load all modules
    server.load_all_modules()

    # Start network servers
    auth_srv  = await start_auth_server(config.DB_PATH)
    world_srv = await start_world_server(server)
    server._auth_server  = auth_srv
    server._world_server = world_srv

    log.info("=" * 52)
    log.info(f"  TestEmu ready!  Realm: {config.REALM_NAME}")
    log.info(f"  Auth  {config.AUTH_HOST}:{config.AUTH_PORT}   "
             f"World {config.REALM_IP}:{config.WORLD_PORT}")
    log.info(f"  Default login: test / test  (GM level 0)")
    log.info(f"  Tip: use  setgm test 3  to make test an admin")
    log.info("=" * 52)

    async with auth_srv, world_srv:
        await asyncio.gather(
            auth_srv.serve_forever(),
            world_srv.serve_forever(),
            run_cli(server),
        )


if __name__ == "__main__":
    # addaccount shortcut
    if len(sys.argv) == 4 and sys.argv[1] == "addaccount":
        init_db(config.DB_PATH)
        create_account(config.DB_PATH, sys.argv[2], sys.argv[3])
        sys.exit(0)

    bootstrap()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down.")
