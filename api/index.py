"""
Vercel Serverless Webhook Endpoint for Agentjon Telegram Bot.
"""
from __future__ import annotations

import os
import sys
import json
import logging
import traceback
from http.server import BaseHTTPRequestHandler

# Ensure project root is in Python path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook")


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        api_key = os.getenv("GEMINI_API_KEY", "")
        info = {
            "status": "ok",
            "bot": "Agentjon",
            "env_token": "SET" if token else "MISSING",
            "env_gemini": "SET" if api_key else "MISSING",
            "python": sys.version,
        }
        # Debug: try importing bot_core
        try:
            import bot_core
            info["bot_core"] = "OK"
            info["emoji_count"] = len(bot_core._load_emoji_map())
            info["model"] = bot_core.GEMINI_MODEL
        except Exception as e:
            info["bot_core"] = f"ERROR: {e}"
        # Debug: try building app
        try:
            global _cached_app, _app_initialized
            if _cached_app is None:
                from bot_core import build_application
                _cached_app = build_application()
            info["app_build"] = "OK"
        except Exception as e:
            info["app_build"] = f"ERROR: {e}"
        self.wfile.write(json.dumps(info, ensure_ascii=False).encode())

    def do_POST(self):
        # --- Webhook secret verification ---
        webhook_secret = os.getenv("WEBHOOK_SECRET")
        if webhook_secret:
            incoming_token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if incoming_token != webhook_secret:
                logger.warning("Webhook secret mismatch – rejecting request")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"forbidden"}')
                return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            logger.info("Received update_id=%s", data.get("update_id", "?"))

            # Always create a fresh event loop (safe for serverless)
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._handle_update(data))
            finally:
                loop.close()

        except Exception as e:
            logger.error("Webhook error: %s\n%s", e, traceback.format_exc())

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    async def _handle_update(self, data):
        """Process a single Telegram update via Application with timeout."""
        import asyncio
        try:
            await asyncio.wait_for(self._run_with_app(data), timeout=55)
        except asyncio.TimeoutError:
            logger.warning("Update processing timed out (55s) for update_id=%s", data.get("update_id"))
            # Try to notify the user
            try:
                chat_id = None
                if "message" in data and "chat" in data["message"]:
                    chat_id = data["message"]["chat"]["id"]
                elif "callback_query" in data and "message" in data["callback_query"]:
                    chat_id = data["callback_query"]["message"]["chat"]["id"]
                if chat_id and _cached_app:
                    await _cached_app.bot.send_message(
                        chat_id=chat_id,
                        text="⏱ Javob vaqti tugadi (60s Vercel limit). Iltimos qayta urinib ko'ring yoki savolingizni qisqartiring."
                    )
            except Exception:
                pass

    async def _run_with_app(self, data):
        """Use python-telegram-bot Application to process the update."""
        from telegram import Update
        from bot_core import build_application

        # Use a module-level cached app
        global _cached_app, _app_initialized

        if _cached_app is None:
            _cached_app = build_application()

        if not _app_initialized:
            await _cached_app.initialize()
            _app_initialized = True

        update = Update.de_json(data, _cached_app.bot)
        await _cached_app.process_update(update)


_cached_app = None
_app_initialized = False
