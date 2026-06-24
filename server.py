"""
Render deployment entry point for Agentjon Telegram Bot.

Runs TWO things simultaneously:
1. Health check HTTP server on $PORT (for UptimeRobot to ping)
2. Telegram bot in long-polling mode (no timeout limits!)
"""
from __future__ import annotations

import os
import sys
import json
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

# Force UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, OSError):
    pass

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("render-server")


# ── Health Check Server ──────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for UptimeRobot / Render health checks."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        info = {
            "status": "alive",
            "bot": "Agentjon",
            "mode": "polling",
            "python": sys.version.split()[0],
        }
        try:
            import bot_core
            info["model"] = bot_core.GEMINI_MODEL
            info["emoji_count"] = len(bot_core._load_emoji_map())
            info["sessions"] = len(bot_core._chat_sessions)
            info["max_sessions"] = bot_core._MAX_SESSIONS
        except Exception:
            pass
        # Memory stats
        try:
            import resource
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            info["memory_mb"] = round(mem_mb, 1)
        except Exception:
            try:
                import psutil
                proc = psutil.Process()
                info["memory_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
            except Exception:
                pass
        self.wfile.write(json.dumps(info, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        """Suppress noisy health check access logs."""
        pass


def start_health_server():
    """Start the health check HTTP server in a daemon thread."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("🌐 Health check server started on port %d", port)
    server.serve_forever()


# ── Telegram Bot ─────────────────────────────────────────────────────────────

def start_bot():
    """Start the Telegram bot in long-polling mode (NO timeout limits!)."""
    from dotenv import load_dotenv
    load_dotenv()

    # Re-read env vars after loading .env
    import bot_core
    bot_core.TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    bot_core.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    bot_core._genai_client = None  # Reset so it re-reads env

    from telegram import Update
    app = bot_core.build_application()

    logger.info("🤖 Agentjon is starting in POLLING mode (Render)...")
    logger.info("   Model: %s", bot_core.GEMINI_MODEL)
    logger.info("   Token: %s", "SET" if bot_core.TELEGRAM_TOKEN else "MISSING")
    logger.info("   Gemini: %s", "SET" if bot_core.GEMINI_API_KEY else "MISSING")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Start health check server in background (for UptimeRobot)
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    # 2. Start bot in main thread (blocking)
    start_bot()
