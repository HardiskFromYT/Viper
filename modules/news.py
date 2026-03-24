"""Login-screen 'Breaking News' module.

The vanilla 1.12.1 client fetches news from a hardcoded URL:
  http://launcher.worldofwarcraft.com/Alert

We serve it on port 8080 (no root needed).  A one-time hosts-file entry
redirects the domain to localhost, and port-forwarding (or the setup_news.sh
script) bridges port 80 → 8080.

On load this module checks the system hosts file and warns if the redirect
is missing.
"""
import logging
import os
import platform
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from modules.base import BaseModule

log = logging.getLogger("news")

NEWS_PORT = 8080
# The vanilla 1.12.1 client may use different domains depending on the build.
# Common ones: www.worldofwarcraft.com, launcher.worldofwarcraft.com
_HOSTS_DOMAINS = [
    "www.worldofwarcraft.com",
    "launcher.worldofwarcraft.com",
]

# Cycle through these colours for commit hashes
_HASH_COLOURS = [
    "#F48CBA",  # pink
    "#FF7C0A",  # orange
    "#AAD372",  # green
    "#3FC7EB",  # cyan
    "#FFF468",  # yellow
    "#C69B6D",  # tan
    "#A330C9",  # purple
    "#00FF98",  # mint
]

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Hosts-file check ────────────────────────────────────────────────────────

def _hosts_path() -> str:
    """Return the hosts-file path for the current OS."""
    if platform.system() == "Windows":
        return r"C:\Windows\System32\drivers\etc\hosts"
    return "/etc/hosts"  # macOS, Linux


def _check_hosts_file() -> list:
    """Return list of missing domains not yet in the hosts file."""
    path = _hosts_path()
    found = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for domain in _HOSTS_DOMAINS:
                    if domain in stripped:
                        found.add(domain)
    except (OSError, PermissionError):
        pass
    return [d for d in _HOSTS_DOMAINS if d not in found]


# ── Git commit fetcher ───────────────────────────────────────────────────────

def _get_commits(count=12):
    """Return list of (short_hash, subject, author, date) tuples."""
    try:
        out = subprocess.check_output(
            ["git", "log", f"--max-count={count}",
             "--pretty=format:%h\x1f%s\x1f%an\x1f%ar"],
            cwd=_REPO_DIR, text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    commits = []
    for line in out.strip().split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append(tuple(parts))
    return commits


# ── Alert builder ────────────────────────────────────────────────────────────
# The WoW client uses its own markup, NOT HTML:
#   |cAARRGGBB  — start colour (AA=alpha, usually FF)
#   |r          — reset colour
#   |n          — newline

def _wow_color(hex_color: str) -> str:
    """Convert '#RRGGBB' to WoW '|cFFRRGGBB'."""
    return f"|cFF{hex_color.lstrip('#')}"

def _build_alert():
    """Build the SERVERALERT response with latest commits in WoW markup."""
    commits = _get_commits()
    if not commits:
        return "SERVERALERT:Welcome to Viper!\n"

    parts = []
    parts.append(f"{_wow_color('#FFD100')}Viper — Latest Changes|r|n|n")
    for i, (sha, subject, author, date) in enumerate(commits):
        colour = _HASH_COLOURS[i % len(_HASH_COLOURS)]
        parts.append(
            f"{_wow_color(colour)}[{sha}]|r "
            f"{subject} "
            f"{_wow_color('#888888')}— {author}, {date}|r"
        )
        if i < len(commits) - 1:
            parts.append(f"|n{_wow_color('#444444')}————————————————————|r|n")
        else:
            parts.append("|n")

    # Trailing newline is REQUIRED or the client errors out
    return "SERVERALERT:" + "".join(parts) + "\n"


# ── HTTP handler ─────────────────────────────────────────────────────────────

class _NewsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _build_alert().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug(f"News HTTP: {fmt % args}")


# ── HTTP server lifecycle ────────────────────────────────────────────────────

_server_instance = None
_server_thread = None


def _start_http():
    global _server_instance, _server_thread
    if _server_instance is not None:
        return  # already running
    # Try port 80 first (works if launched with sudo or has cap_net_bind),
    # then fall back to NEWS_PORT (8080).
    for port in (80, NEWS_PORT):
        try:
            _server_instance = HTTPServer(("0.0.0.0", port), _NewsHandler)
            break
        except OSError:
            continue
    else:
        log.warning("Could not start news server on port 80 or 8080.")
        print("  \033[91m✗\033[0m News: could not bind port 80 or 8080")
        return
    _server_thread = threading.Thread(
        target=_server_instance.serve_forever, daemon=True, name="news-http",
    )
    _server_thread.start()
    actual = _server_instance.server_address[1]
    log.info(f"News HTTP server listening on port {actual}")
    if actual != 80:
        print(f"  \033[93m!\033[0m News: listening on port {actual} "
              f"(port 80 needs root — run with sudo for login-screen news)")


def _stop_http():
    global _server_instance, _server_thread
    if _server_instance is not None:
        _server_instance.shutdown()
        _server_instance = None
        _server_thread = None
        log.info("News HTTP server stopped.")


# ── Module ───────────────────────────────────────────────────────────────────

class Module(BaseModule):
    name = "news"

    def on_load(self, server):
        # Check hosts file — print to CONSOLE (logging goes to file only)
        missing = _check_hosts_file()
        if missing:
            sys_name = platform.system()
            hosts = _hosts_path()
            domains_str = "  ".join(missing)
            hosts_lines = "\n".join(f"    127.0.0.1  {d}" for d in missing)
            msg = [
                "",
                "  \033[93m⚠  NEWS REDIRECT NOT CONFIGURED\033[0m",
                f"  Add these lines to \033[1m{hosts}\033[0m:",
                f"\033[92m{hosts_lines}\033[0m",
            ]
            if sys_name == "Darwin":
                msg.append("  Quick setup:  \033[96msudo ./setup_news.sh\033[0m")
            elif sys_name == "Windows":
                msg.append("  Run Notepad as Administrator, edit the hosts file.")
            else:
                for d in missing:
                    msg.append(f"  echo '127.0.0.1  {d}' | sudo tee -a {hosts}")
            msg.append("  Without this, the login screen won't show news.")
            msg.append("")
            for line in msg:
                print(line)
                log.warning(line)
        else:
            log.info("Hosts file OK — news domains redirected to localhost.")
            print("  \033[92m✓\033[0m News: hosts redirect OK")

        _start_http()
        log.info("News module loaded.")

    def on_unload(self, server):
        _stop_http()
        log.info("News module unloaded.")
