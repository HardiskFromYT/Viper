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
_HOSTS_ENTRY = "launcher.worldofwarcraft.com"

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


def _check_hosts_file() -> bool:
    """Return True if the hosts file already redirects the news domain."""
    path = _hosts_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if _HOSTS_ENTRY in stripped:
                    return True
    except (OSError, PermissionError):
        pass
    return False


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


# ── HTML builder ─────────────────────────────────────────────────────────────

def _build_alert_html():
    """Build the SERVERALERT response with latest commits."""
    commits = _get_commits()
    if not commits:
        return "SERVERALERT:<html><body><p>Welcome to Viper!</p></body></html>\n"

    lines = []
    lines.append('<h1 style="color:#FFD100;">Viper — Latest Changes</h1>')
    lines.append("<br/>")
    for i, (sha, subject, author, date) in enumerate(commits):
        colour = _HASH_COLOURS[i % len(_HASH_COLOURS)]
        lines.append(
            f'<p><font color="{colour}">[{sha}]</font> '
            f"{subject} "
            f'<font color="#888888">— {author}, {date}</font></p>'
        )
        if i < len(commits) - 1:
            lines.append('<p><font color="#333333">─────────────────────────</font></p>')

    body = "\n".join(lines)
    # Trailing newline is REQUIRED or the client errors out
    return f"SERVERALERT:<html><body>{body}</body></html>\n"


# ── HTTP handler ─────────────────────────────────────────────────────────────

class _NewsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _build_alert_html().encode("utf-8")
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
    try:
        _server_instance = HTTPServer(("0.0.0.0", NEWS_PORT), _NewsHandler)
    except OSError as e:
        log.warning(f"Could not start news server on port {NEWS_PORT}: {e}")
        return
    _server_thread = threading.Thread(
        target=_server_instance.serve_forever, daemon=True, name="news-http",
    )
    _server_thread.start()
    log.info(f"News HTTP server listening on port {NEWS_PORT}")


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
        # Check hosts file
        if not _check_hosts_file():
            sys_name = platform.system()
            hosts = _hosts_path()
            log.warning("=" * 60)
            log.warning("  NEWS REDIRECT NOT CONFIGURED")
            log.warning(f"  The WoW client needs this line in {hosts}:")
            log.warning(f"    127.0.0.1  {_HOSTS_ENTRY}")
            if sys_name == "Darwin":
                log.warning("  Quick setup:  sudo ./setup_news.sh")
            elif sys_name == "Windows":
                log.warning("  Run as Administrator and edit the file,")
                log.warning("  or run:  setup_news.bat")
            else:
                log.warning("  Add it with:  echo '127.0.0.1  "
                            f"{_HOSTS_ENTRY}' | sudo tee -a {hosts}")
            log.warning("  Without this, the login screen won't show news.")
            log.warning("=" * 60)
        else:
            log.info(f"Hosts file OK — {_HOSTS_ENTRY} redirected to localhost.")

        _start_http()
        log.info("News module loaded.")

    def on_unload(self, server):
        _stop_http()
        log.info("News module unloaded.")
