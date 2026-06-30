from __future__ import annotations

import os
import sys
import asyncio
import re
import random
import json
import time
import datetime
import logging
from html.parser import HTMLParser

from telegram import (
    Update, Message, ReactionTypeEmoji, ReactionTypeCustomEmoji, ReplyParameters,
    InlineQueryResultArticle, InputTextMessageContent,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from telegram.error import BadRequest, RetryAfter, Forbidden
from google import genai
from google.genai import types

import httpx
from supabase import create_client, Client

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
logger = logging.getLogger("agentjon")
logger.setLevel(logging.DEBUG)

# File handler — faqat lokal muhitda ishlaydi (Vercel serverless'da read-only filesystem)
try:
    _log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentjon.log")
    _file_handler = logging.FileHandler(_log_file, encoding="utf-8")
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(_file_handler)
except Exception:
    pass  # Vercel — file logging disabled

# ─── Environment ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ─── Supabase Client ─────────────────────────────────────────────────────────
supabase_client: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error("Failed to initialize Supabase client: %s", e)

# ─── Gemini Client (lazy init) ───────────────────────────────────────────────
_genai_client = None


def _get_genai_client():
    """Lazily initialize and return the Gemini client."""
    global _genai_client, GEMINI_API_KEY
    if _genai_client is None:
        GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY environment variable not set.")
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client

# ─── Global Bot Info ─────────────────────────────────────────────────────────
BOT_USERNAME = None
BOT_ID = None


async def init_bot_info(bot):
    global BOT_USERNAME, BOT_ID
    if BOT_USERNAME is None:
        try:
            info = await bot.get_me()
            BOT_USERNAME = info.username
            BOT_ID = info.id
        except Exception as e:
            logger.error("Error fetching bot info: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  PREMIUM EMOJI MAP
# ══════════════════════════════════════════════════════════════════════════════

_EMOJI_MAP = {}  # emoji -> [custom_emoji_id, ...]

# ─── Pre-compiled regex patterns (module-level constants) ────────────────────
_RE_HTML_TAG_SPLIT = re.compile(r'(<[^>]+>)')
_RE_TG_EMOJI = re.compile(r'<tg-emoji\s+emoji-id="[^"]+">.*?</tg-emoji>', re.DOTALL)
_RE_CODE_BLOCK = re.compile(r'```(\w*)\n?(.*?)\n?```', re.DOTALL)
_RE_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_RE_MATH_BLOCK = re.compile(r'\$\$([^\$]+?)\$\$', re.DOTALL)
_RE_MATH_INLINE = re.compile(r'\$([^\$\s\n][^\$]*?[^\$\s\n]|[^\$\s\n])\$')
_RE_MATH_BLOCK_BRACKET = re.compile(r'\\\[(.*?)\\\]', re.DOTALL)
_RE_MATH_INLINE_PAREN = re.compile(r'\\\((.*?)\\\)', re.DOTALL)
_RE_SPOILER = re.compile(r'\|\|(.*?)\|\|', re.DOTALL)
_RE_BULLETS = re.compile(r'(?:^|\n)\s*[\*\-]\s+(.+?)(?=\n|$)')
_RE_HEADINGS = re.compile(r'(?:^|\n)(?:#{1,6})\s+(.+?)(?=\n|$)')
_RE_BOLD_STARS = re.compile(r'\*\*(.*?)\*\*', re.DOTALL)
_RE_BOLD_UNDERSCORES = re.compile(r'(?<!_)__(?!_)(?!CODEBLOCK|INLINE|TGEMOJI)(.*?)(?<!_)__(?!_)', re.DOTALL)
_RE_ITALIC_STARS = re.compile(r'\*(.*?)\*', re.DOTALL)
_RE_ITALIC_UNDERSCORES = re.compile(r'_(.*?)_', re.DOTALL)
_RE_STRIKETHROUGH = re.compile(r'~~(.*?)~~', re.DOTALL)
_RE_LINKS = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_RE_BLOCKQUOTE = re.compile(r'(?:^|\n)(?:&gt;[^\n]*\n?)+')
_RE_CLEAN_TAG = re.compile(r'<(/?)([\w-]+)([^>]*)>')
_RE_REACTION = re.compile(r'\[REACTION:(.+?)\]')
_RE_GUEST_REACTION = re.compile(r'\[REACTION:.*?\]')
_RE_HEADING_LINE = re.compile(r'^(#{1,6})\s+(.+)$')
_RE_A_TAG = re.compile(r'(<a\s[^>]*>.*?</a>)', re.DOTALL)

# ─── Emoji fallback map (module-level constant) ─────────────────────────────
_EMOJI_FALLBACK = {
    "⏳": "🤔", "⌛": "🤔", "💡": "⚡", "✅": "👍", "❌": "👎",
    "🤖": "👾", "😊": "😁", "🥳": "🎉", "💪": "🔥", "🎯": "🏆",
    "📝": "✍", "🫶": "❤", "💕": "💘", "😂": "🤣", "😀": "😁",
    "🙂": "😁", "😃": "😁", "🤞": "🙏", "👋": "🤗", "😤": "😡",
    "🥺": "😢", "😳": "🤯", "🤭": "🙈", "😬": "😐", "🫠": "🥴",
    "🤝🏻": "🤝", "☺": "😁", "💀": "👻", "✨": "🎉", "🔥👍": "🔥",
}

# ─── Single-pass emoji regex (built once from all emoji keys) ────────────────
_emoji_regex = None

def _get_emoji_regex():
    """Build and cache a single compiled regex from all emoji map keys."""
    global _emoji_regex
    if _emoji_regex is not None:
        return _emoji_regex
    emap = _load_emoji_map()
    if not emap:
        return None
    # Sort by length descending so longer emojis match first
    keys = sorted(emap.keys(), key=len, reverse=True)
    pattern = '|'.join(re.escape(k) for k in keys)
    _emoji_regex = re.compile(pattern)
    return _emoji_regex

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB (Render free = 512MB RAM, protect from OOM)

def _load_emoji_map():
    """Load emoji -> [custom_emoji_id, ...] from JSON and Supabase."""
    global _EMOJI_MAP
    if _EMOJI_MAP:
        return _EMOJI_MAP
    map_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emoji_multi_map.json')
    try:
        with open(map_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        for k, v in raw.items():
            if isinstance(v, list):
                _EMOJI_MAP[k] = v
            else:
                _EMOJI_MAP[k] = [v]
        logger.info("Loaded %d premium emojis from JSON", len(_EMOJI_MAP))
    except Exception as e:
        logger.warning("Could not load emoji_multi_map.json: %s", e)
        _EMOJI_MAP = {}

    # Merge from Supabase
    if supabase_client:
        try:
            res = supabase_client.table('emoji_cache').select('*').execute()
            if res.data:
                for row in res.data:
                    emoji = row['emoji']
                    custom_ids = row.get('custom_ids', [])
                    if emoji not in _EMOJI_MAP:
                        _EMOJI_MAP[emoji] = custom_ids
                    else:
                        _EMOJI_MAP[emoji] = list(set(_EMOJI_MAP[emoji] + custom_ids))
                    _dynamic_search_done.add(emoji)
                logger.info("Merged %d emojis from Supabase", len(res.data))
        except Exception as e:
            logger.error("Failed to load emoji cache from Supabase: %s", e)

    return _EMOJI_MAP


# ─── Dynamic emoji search via Telegram API ───────────────────────────────────
_dynamic_search_done = set()  # track searched emojis to avoid repeat API calls
_dynamic_emoji_warmed = False  # track if bulk warm-up has been done

# Regex to find ALL Unicode emojis in text
_RE_ALL_EMOJIS = re.compile(
    "(?:"
    "[\U0001F600-\U0001F64F]"  # emoticons
    "|[\U0001F300-\U0001F5FF]"  # symbols & pictographs
    "|[\U0001F680-\U0001F6FF]"  # transport & map
    "|[\U0001F1E0-\U0001F1FF]"  # flags
    "|[\U00002702-\U000027B0]"  # dingbats
    "|[\U0001F900-\U0001F9FF]"  # supplemental symbols
    "|[\U0001FA00-\U0001FA6F]"  # chess symbols
    "|[\U0001FA70-\U0001FAFF]"  # symbols extended
    "|[\U00002600-\U000026FF]"  # misc symbols
    "|[\U00002B50-\U00002B55]"  # stars
    "|[\U0000231A-\U0000231B]"  # watch/hourglass
    "|[\U000023E9-\U000023F3]"  # media controls
    "|[\U000023F8-\U000023FA]"  # media controls
    "|[\U0000200D]"             # ZWJ
    "|[\U0000FE0F]"             # variation selector
    ")+"
)


async def _search_single_emoji(bot_token: str, emoji: str):
    """Search Telegram API for a single custom emoji and add to _EMOJI_MAP."""
    global _EMOJI_MAP, _emoji_regex
    if emoji in _EMOJI_MAP or emoji in _dynamic_search_done:
        return
    _dynamic_search_done.add(emoji)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/searchStickers",
                json={"emoji": emoji, "sticker_type": "custom_emoji", "limit": 10}
            )
            data = r.json()
            ids = []
            if data.get("ok") and data.get("result"):
                for sticker in data["result"]:
                    cid = sticker.get("custom_emoji_id")
                    if cid and cid not in ids:
                        ids.append(cid)
                if ids:
                    _EMOJI_MAP[emoji] = ids
                    _emoji_regex = None  # Force regex rebuild with new emojis
                    logger.debug("Dynamic emoji: %s -> %d custom IDs", emoji, len(ids))

            # Save to Supabase (even if empty, to remember we searched it)
            if supabase_client:
                def _save_emoji():
                    try:
                        supabase_client.table('emoji_cache').upsert({
                            'emoji': emoji,
                            'custom_ids': ids
                        }).execute()
                    except Exception as e:
                        logger.error("Failed to save emoji to Supabase in thread: %s", e)
                asyncio.create_task(asyncio.to_thread(_save_emoji))
    except Exception:
        pass  # Silent fail — emoji just won't be premium


async def ensure_emoji_coverage(bot_token: str, text: str):
    """Search for custom emoji IDs for any emojis in text not already in the map.
    Runs BEFORE emojis_to_premium so new emojis are available for replacement."""
    _load_emoji_map()
    # Find all emojis in text
    found = set(_RE_ALL_EMOJIS.findall(text))
    # Filter to unknown emojis
    unknown = [e for e in found if e not in _EMOJI_MAP and e not in _dynamic_search_done]
    if not unknown:
        return
    # Search up to 20 in parallel (rate limit safe)
    tasks = [_search_single_emoji(bot_token, e) for e in unknown[:20]]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Dynamic emoji search: %d new, total map: %d", len(unknown), len(_EMOJI_MAP))


async def warm_up_emojis(bot_token: str):
    """Bulk search common emojis on first message. Runs once."""
    global _dynamic_emoji_warmed
    if _dynamic_emoji_warmed:
        return
    _dynamic_emoji_warmed = True
    common = [
        "😀", "😁", "😂", "🤣", "😃", "😄", "😅", "😆", "😉", "😊",
        "😋", "😎", "😍", "🥰", "😘", "😗", "😙", "😚", "🙂", "🤗",
        "🤩", "🤔", "🤨", "😐", "😑", "😶", "🙄", "😏", "😣", "😥",
        "😮", "🤐", "😯", "😪", "😫", "🥱", "😴", "😌", "😛", "😜",
        "😝", "🤤", "😒", "😓", "😔", "😕", "🙃", "🤑", "😲", "🙁",
        "😖", "😞", "😟", "😤", "😢", "😭", "😦", "😧", "😨", "😩",
        "🤯", "😬", "😰", "😱", "🥵", "🥶", "😳", "🤪", "😵", "🥴",
        "😠", "😡", "🤬", "😈", "👿", "💀", "☠", "💩", "🤡", "👹",
        "👺", "👻", "👽", "👾", "🤖", "😺", "😸", "😹", "😻", "😼",
        "😽", "🙀", "😿", "😾", "🙈", "🙉", "🙊", "💋", "💌", "💘",
        "💝", "💖", "💗", "💓", "💞", "💕", "💟", "❣", "💔", "❤",
        "🧡", "💛", "💚", "💙", "💜", "🤎", "🖤", "🤍", "💯", "💢",
        "💥", "💫", "💦", "💨", "🕳", "💣", "💬", "👋", "🤚", "🖐",
        "✋", "🖖", "👌", "🤌", "🤏", "✌", "🤞", "🤟", "🤘", "🤙",
        "👈", "👉", "👆", "🖕", "👇", "☝", "👍", "👎", "✊", "👊",
        "🤛", "🤜", "👏", "🙌", "👐", "🤲", "🤝", "🙏", "✍", "💅",
        "🤳", "💪", "🦾", "🦿", "🦵", "🦶", "👂", "🦻", "👃", "🧠",
        "🫀", "🫁", "🦷", "🦴", "👀", "👁", "👅", "👄", "👶", "🧒",
        "👦", "👧", "🧑", "👱", "👨", "🧔", "👩", "🧓", "👴", "👵",
        "🔥", "⭐", "🌟", "✨", "⚡", "💡", "🎯", "🏆", "🎉", "🎊",
        "🎈", "🎁", "🎀", "🎗", "🎄", "🎃", "👑", "💎", "🔑", "🗝",
        "🔒", "🔓", "❤️‍🔥", "🫶", "🩷", "🩵", "🩶",
        "🚀", "🛸", "🌍", "🌎", "🌏", "🌈", "☀", "🌤", "⛅", "🌥",
        "☁", "🌦", "🌧", "⛈", "🌩", "🌪", "🌫", "🌬", "🌀", "🌊",
        "📱", "💻", "🖥", "🖨", "📷", "📸", "📹", "🎥", "📡", "🔭",
        "🔬", "📚", "📖", "📝", "✏", "🖊", "🖋", "📌", "📎", "🔗",
    ]
    # Filter out ones already in map
    to_search = [e for e in common if e not in _EMOJI_MAP and e not in _dynamic_search_done]
    if not to_search:
        return
    logger.info("Warming up emojis: searching %d common emojis...", len(to_search))
    # Search in batches of 30
    for i in range(0, len(to_search), 30):
        batch = to_search[i:i+30]
        tasks = [_search_single_emoji(bot_token, e) for e in batch]
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Emoji warm-up done. Total map: %d emojis", len(_EMOJI_MAP))


def emojis_to_premium(text: str) -> str:
    """Replace regular emojis in text with premium tg-emoji tags.
    Uses a single compiled regex pass. Picks IDs based on text context hash
    for variety that is consistent within one message but varies across messages."""
    emap = _load_emoji_map()
    if not emap:
        return text
    regex = _get_emoji_regex()
    if regex is None:
        return text

    # Context seed: hash of entire text gives consistent selection per response
    ctx_hash = hash(text)
    _counter = [0]  # mutable counter for position-based variation

    def _replace_emoji(m):
        ids = emap[m.group(0)]
        # Mix context hash with position for per-emoji variety
        idx = (ctx_hash + _counter[0]) % len(ids)
        _counter[0] += 1
        return f'<tg-emoji emoji-id="{ids[idx]}">{m.group(0)}</tg-emoji>'

    # Don't replace emojis inside existing tg-emoji tags or HTML tags
    parts = _RE_HTML_TAG_SPLIT.split(text)
    result = []
    inside_tg_emoji = False
    for part in parts:
        if part.startswith('<'):
            result.append(part)
            if part.startswith('<tg-emoji'):
                inside_tg_emoji = True
            elif part == '</tg-emoji>':
                inside_tg_emoji = False
        else:
            if not inside_tg_emoji:
                part = regex.sub(_replace_emoji, part)
            result.append(part)
    return ''.join(result)


# Regex to extract tg-emoji tags: <tg-emoji emoji-id="12345">😊</tg-emoji>
_RE_TG_EMOJI_EXTRACT = re.compile(
    r'<tg-emoji\s+emoji-id="(\d+)">(.*?)</tg-emoji>', re.DOTALL
)
# Regex to strip all remaining HTML tags
_RE_STRIP_HTML = re.compile(r'<[^>]+>')


class TelegramHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.plain_text = ""
        self.entities = []
        self.entity_stack = []

    def _get_current_utf16_len(self):
        return len(self.plain_text.encode('utf-16-le')) // 2

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        start_offset = self._get_current_utf16_len()
        
        entity_type = None
        custom_emoji_id = None
        url = None
        
        if tag in ('b', 'strong'):
            entity_type = "bold"
        elif tag in ('i', 'em'):
            entity_type = "italic"
        elif tag in ('u', 'ins'):
            entity_type = "underline"
        elif tag in ('s', 'strike', 'del'):
            entity_type = "strikethrough"
        elif tag == 'code':
            entity_type = "code"
        elif tag == 'pre':
            entity_type = "pre"
        elif tag in ('tg-spoiler', 'spoiler') or (tag == 'span' and attr_dict.get('class') == 'tg-spoiler'):
            entity_type = "spoiler"
        elif tag == 'tg-emoji':
            entity_type = "custom_emoji"
            custom_emoji_id = attr_dict.get('emoji-id') or attr_dict.get('id')
        elif tag == 'a':
            entity_type = "text_link"
            url = attr_dict.get('href')
        elif tag == 'blockquote':
            if 'expandable' in attr_dict:
                entity_type = "expandable_blockquote"
            else:
                entity_type = "blockquote"

        if entity_type:
            self.entity_stack.append({
                "type": entity_type,
                "start": start_offset,
                "custom_emoji_id": custom_emoji_id,
                "url": url
            })

    def handle_endtag(self, tag):
        expected_types = []
        if tag in ('b', 'strong'): expected_types = ["bold"]
        elif tag in ('i', 'em'): expected_types = ["italic"]
        elif tag in ('u', 'ins'): expected_types = ["underline"]
        elif tag in ('s', 'strike', 'del'): expected_types = ["strikethrough"]
        elif tag == 'code': expected_types = ["code"]
        elif tag == 'pre': expected_types = ["pre"]
        elif tag in ('tg-spoiler', 'spoiler', 'span'): expected_types = ["spoiler"]
        elif tag == 'tg-emoji': expected_types = ["custom_emoji"]
        elif tag == 'a': expected_types = ["text_link"]
        elif tag == 'blockquote': expected_types = ["blockquote", "expandable_blockquote"]
        
        for i in range(len(self.entity_stack) - 1, -1, -1):
            item = self.entity_stack[i]
            if item["type"] in expected_types:
                self.entity_stack.pop(i)
                end_offset = self._get_current_utf16_len()
                length = end_offset - item["start"]
                if length > 0:
                    entity = {
                        "type": item["type"],
                        "offset": item["start"],
                        "length": length
                    }
                    if item["custom_emoji_id"]:
                        entity["custom_emoji_id"] = item["custom_emoji_id"]
                    if item["url"]:
                        entity["url"] = item["url"]
                    self.entities.append(entity)
                break

    def handle_data(self, data):
        self.plain_text += data

    def handle_entityref(self, name):
        import html
        char = html.unescape(f"&{name};")
        self.plain_text += char

    def handle_charref(self, name):
        import html
        char = html.unescape(f"&#{name};")
        self.plain_text += char


def parse_html_to_entities(html_text: str):
    parser = TelegramHTMLParser()
    parser.feed(html_text)
    return parser.plain_text, parser.entities


# All standard Telegram reaction emojis (verified working — Bot API 10.0+)
STANDARD_REACTIONS = [
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷",
    "🤷‍♂", "🤷‍♀", "😡",
]

# Quick lookup set for validation
_STANDARD_SET = set(STANDARD_REACTIONS)


async def set_premium_reaction(message, emoji_str: str):
    """Set reaction on message. Tries: 1) custom emoji, 2) standard emoji, 3) safe fallback.
    Custom emoji works in groups where admin allows it. Standard works everywhere."""
    emap = _load_emoji_map()
    ids = emap.get(emoji_str)

    # Try 1: Premium custom emoji reaction (random pick from available IDs)
    if ids:
        eid = random.choice(ids)
        try:
            await message.set_reaction(reaction=ReactionTypeCustomEmoji(custom_emoji_id=eid))
            return
        except Exception:
            pass

    # Try 2: Standard emoji reaction (must be in Telegram's allowed list)
    if emoji_str in _STANDARD_SET:
        try:
            await message.set_reaction(reaction=ReactionTypeEmoji(emoji=emoji_str))
            return
        except Exception:
            pass

    # Try 3: Find closest standard emoji (uses module-level _EMOJI_FALLBACK)
    fallback = _EMOJI_FALLBACK.get(emoji_str, "👍")
    try:
        await message.set_reaction(reaction=ReactionTypeEmoji(emoji=fallback))
    except Exception as e:
        logger.debug("Final fallback reaction failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  HTML UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def clean_and_balance_html(html_str: str) -> str:
    """Balance HTML tags for Telegram-compatible output."""
    tag_regex = _RE_CLEAN_TAG
    stack = []
    result = []
    last_idx = 0

    SUPPORTED = {
        'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
        'tg-spoiler', 'a', 'code', 'pre', 'blockquote', 'tg-emoji',
    }

    for match in tag_regex.finditer(html_str):
        result.append(html_str[last_idx:match.start()])
        last_idx = match.end()

        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        full_tag = match.group(0)

        if tag_name not in SUPPORTED:
            escaped = full_tag.replace('<', '&lt;').replace('>', '&gt;')
            result.append(escaped)
            continue

        if not is_closing:
            stack.append((tag_name, full_tag))
            result.append(full_tag)
        else:
            match_idx = -1
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tag_name:
                    match_idx = idx
                    break

            if match_idx != -1:
                reopen_tags = []
                while len(stack) - 1 > match_idx:
                    t_name, t_open = stack.pop()
                    result.append(f"</{t_name}>")
                    remaining = html_str[match.end():]
                    if re.search(rf'</\s*{re.escape(t_name)}\s*>', remaining, re.IGNORECASE):
                        reopen_tags.append((t_name, t_open))

                result.append(f"</{tag_name}>")
                stack.pop()

                for t_name, t_open in reversed(reopen_tags):
                    result.append(t_open)
                    stack.append((t_name, t_open))

    result.append(html_str[last_idx:])

    while stack:
        t_name, _ = stack.pop()
        result.append(f"</{t_name}>")

    return "".join(result)


def markdown_to_html(text: str) -> str:
    """Convert Gemini markdown to Telegram-safe HTML."""
    # Preserve tg-emoji tags before escaping
    tg_emojis = []
    def save_tg_emoji(m):
        tg_emojis.append(m.group(0))
        return f"__TGEMOJI{len(tg_emojis)-1}__"
    text = _RE_TG_EMOJI.sub(save_tg_emoji, text)

    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    code_blocks = []
    inline_codes = []

    def save_code_block(m):
        code_blocks.append((m.group(1) or "", m.group(2)))
        return f"__CODEBLOCK{len(code_blocks)-1}__"

    def save_inline_code(m):
        inline_codes.append(m.group(1))
        return f"__INLINE{len(inline_codes)-1}__"

    text = _RE_CODE_BLOCK.sub(save_code_block, text)
    text = _RE_INLINE_CODE.sub(save_inline_code, text)

    # Math
    text = _RE_MATH_BLOCK.sub(r'<pre>\1</pre>', text)
    text = _RE_MATH_INLINE.sub(r'<code>\1</code>', text)
    text = _RE_MATH_BLOCK_BRACKET.sub(r'<pre>\1</pre>', text)
    text = _RE_MATH_INLINE_PAREN.sub(r'<code>\1</code>', text)

    # Spoilers
    text = _RE_SPOILER.sub(r'<tg-spoiler>\1</tg-spoiler>', text)

    # Bullets
    text = _RE_BULLETS.sub(r'\n• \1', text)

    # Headings
    text = _RE_HEADINGS.sub(r'\n<b>\1</b>', text)

    # Bold
    text = _RE_BOLD_STARS.sub(r'<b>\1</b>', text)
    # Bold — only match __ that are NOT part of placeholders
    text = _RE_BOLD_UNDERSCORES.sub(r'<b>\1</b>', text)

    # Italic
    text = _RE_ITALIC_STARS.sub(r'<i>\1</i>', text)

    # Italic underscore — protect <a> tags first to prevent URL corruption
    a_tags = []
    def save_a_tag(m):
        a_tags.append(m.group(0))
        return f"__ATAG{len(a_tags)-1}__"
    text = _RE_A_TAG.sub(save_a_tag, text)
    text = _RE_ITALIC_UNDERSCORES.sub(r'<i>\1</i>', text)
    for i, tag in enumerate(a_tags):
        text = text.replace(f"__ATAG{i}__", tag)

    # Strikethrough
    text = _RE_STRIKETHROUGH.sub(r'<s>\1</s>', text)

    # Links
    text = _RE_LINKS.sub(r'<a href="\2">\1</a>', text)

    # Blockquotes
    def save_blockquote(m):
        lines = m.group(0).strip().split('\n')
        cleaned = [l[4:].lstrip() if l.startswith('&gt;') else l.lstrip() for l in lines]
        return f"\n<blockquote expandable>{''.join(cleaned)}</blockquote>\n"

    text = _RE_BLOCKQUOTE.sub(save_blockquote, text)

    text = clean_and_balance_html(text)

    for i, (lang, code) in enumerate(code_blocks):
        if lang:
            r = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            r = f'<pre><code>{code}</code></pre>'
        text = text.replace(f"__CODEBLOCK{i}__", r)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"__INLINE{i}__", f'<code>{code}</code>')

    # Restore premium tg-emoji tags
    for i, emoji_tag in enumerate(tg_emojis):
        text = text.replace(f"__TGEMOJI{i}__", emoji_tag)

    # Convert remaining regular emojis to premium — DOIM, hamma joyda
    text = emojis_to_premium(text)

    return text


def _guest_markdown_to_html(text: str) -> str:
    """Like markdown_to_html but WITHOUT premium emoji conversion.
    Used for guest mode where custom emojis don't work."""
    # Run the same conversion pipeline but skip emojis_to_premium at the end
    # Quick approach: call markdown_to_html then strip tg-emoji tags
    html = markdown_to_html(text)
    # Remove <tg-emoji emoji-id="...">X</tg-emoji> -> X (keep the inner emoji)
    html = _RE_TG_EMOJI.sub(lambda m: m.group(0).split('>')[1].split('<')[0] if '>' in m.group(0) else m.group(0), html)
    return html

# ══════════════════════════════════════════════════════════════════════════════
#  TOOLS (Gemini Function Calling)
# ══════════════════════════════════════════════════════════════════════════════

async def get_current_time() -> str:
    """Get the current date and time for Uzbekistan (Asia/Tashkent, UTC+5)."""
    tz = datetime.timezone(datetime.timedelta(hours=5))
    return datetime.datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S UZT')


TOOLS = [get_current_time]


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM MANAGEMENT TOOLS (Gemini function calling)
# ══════════════════════════════════════════════════════════════════════════════

# These are declared separately and added at runtime when bot is available
# because they need the bot instance

async def tg_get_chat_info(chat_id: str) -> str:
    """Get information about a Telegram chat/channel/group by its @username or numeric ID.
    Returns title, type, member count, description, and permissions.
    Example: tg_get_chat_info("@channel_username") or tg_get_chat_info("-1001234567890")
    """
    return f"TOOL_NEEDS_BOT:tg_get_chat_info:{chat_id}"


async def tg_get_chat_member_count(chat_id: str) -> str:
    """Get the number of members in a Telegram chat/channel/group.
    Example: tg_get_chat_member_count("@channel_username")
    """
    return f"TOOL_NEEDS_BOT:tg_get_chat_member_count:{chat_id}"


async def tg_send_to_channel(chat_id: str, text: str) -> str:
    """Send a message/post to a Telegram channel or group where the bot is admin.
    The text supports Markdown formatting. Bot MUST be admin in the target chat.
    Example: tg_send_to_channel("@my_channel", "Hello from Agentjon! 🔥")
    """
    return f"TOOL_NEEDS_BOT:tg_send_to_channel:{chat_id}:{text}"


async def tg_get_admins(chat_id: str) -> str:
    """Get the list of administrators in a Telegram chat/channel/group.
    Example: tg_get_admins("@channel_username")
    """
    return f"TOOL_NEEDS_BOT:tg_get_admins:{chat_id}"


# ─── Barcha Tool larni Birlashtirish (Google Built-in + Custom) ────────────────
TOOLS = [
    # Custom Tools
    get_current_time,
    
    # Telegram Tools
    tg_get_chat_info, 
    tg_get_chat_member_count, 
    tg_send_to_channel, 
    tg_get_admins,
]


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_INSTRUCTION = """Sen Agentjon - Mutlaq mukammal Telegram AI agentisan. O'zbek tilida gaplash.
Aqlli, hazilkash, samimiy do'st. Hamma narsani mukammal tahlil qil.
Oddiy savol = aniq qisqa javob. Murakkab savol = batafsil, chuqur, mukammal dizaynlashtirilgan javob. Ilmiy/texnik mavzu = manbalar va jadvallar ko'rsat!

**Bot Qobiliyatlari (TOOLS):**
- **Google Search (Native):** Eng so'nggi real vaqt yangiliklarini topish uchun.
- **Code Execution (Native):** Python kod yozib natijasini olish!
- **Telegram Tools:** Kanallarga post yozish, chat ma'lumotlarini olish.

**Formatlash va Dizayn (API 10.1 qoidasi):**
Sen eng premium darajadagi formatsiyadan foydalanasan:
1. Sarlavhalar oldidan doim Emoji qo'y. Ro'yxatlarda doim Emoji bo'lsin.
2. Murakkab uzun javoblarda jadvallar (Markdown Tables) va > (Blockquote) ishlat.
3. Katta kod bloklarida ````python ... ```` ishlat.
4. Har bir abzats qisqa va aniq o'qiladigan bo'lsin. Emojilarni gap oralariga tabiiy qo'shib yoz (eng asosiysi 3-5 ta emoji kifoya, lekin o'ta o'rinli bo'lsin).
5. Har xabarga [REACTION:emoji] qo'y(👍 👎 ❤️ 🔥 🎉 👏).
Haqorat/spam=[DELETE_MSG]. Guruhda foydali ma'lumotsiz shunchaki salom/hayr=[IGNORE].

=== TELEGRAM EKSPERT BILIMLAR (2026 Maksimal daraja) ===
Sen Telegram'ning ENG KUCHLI ekspertisan, Bot API 10.1 (Iyun 2026) senga to'liq tanish.
- 9.0: Telegram Business, Mini App Storage
- 9.4: Custom emoji
- 10.0: AI Bot Revolution - bot-to-bot aloqa, Guest Mode, Streaming
- 10.1 (Eng so'nggi): Rich Messages - sendRichMessage (32768 belgi, jadvallar, expandable blockquotes)

Biz sendRichMessage va Guest Mode'dan maksimal foydalanamiz!
- Guest Mode'da yozayotganda, xabarlar yanada toza va aniq bo'lishi kerak.
- Agar foydalanuvchi qisqa savol bersa, guruh bo'lsa ham unga maksimal qimmatli bilim ber.
"""


# ══════════════════════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

# In-memory sessions (works for polling; stateless per invocation on Vercel)
_chat_sessions: dict[str, object] = {}
_session_last_used: dict[str, float] = {}  # key -> last used timestamp
_MAX_SESSIONS = 30  # Render free = 512MB, keep sessions small
_MAX_HISTORY_TURNS = 20  # Max conversation turns per session (prevents OOM)


def _prune_session_history(session):
    """Trim old conversation turns to prevent unbounded memory growth.
    Keeps only the last _MAX_HISTORY_TURNS turns (user+model pairs).
    This is the KEY fix for the OOM/memory-full crash."""
    try:
        history = getattr(session, '_history', None) or getattr(session, 'history', None)
        if history and hasattr(history, '__len__') and len(history) > _MAX_HISTORY_TURNS * 2:
            # Keep system instruction (first) + last N turns
            excess = len(history) - _MAX_HISTORY_TURNS * 2
            if excess > 0:
                del history[:excess]
                logger.debug("Pruned %d old history entries", excess)
    except Exception as e:
        logger.debug("History prune skipped: %s", e)


def _serialize_history(session) -> list[dict]:
    """Serialize Gemini history to JSON, omitting raw media to save space."""
    history = getattr(session, '_history', None) or getattr(session, 'history', None)
    if not history:
        return []
    res = []
    for turn in history:
        role = turn.role
        parts = []
        for part in turn.parts:
            if hasattr(part, 'text') and part.text:
                parts.append({"text": part.text})
            else:
                parts.append({"text": "[Media omitted]"})
        res.append({"role": role, "parts": parts})
    return res


def _deserialize_history(history_data: list[dict]) -> list[types.Content]:
    """Deserialize JSON history back to Gemini Content objects."""
    res = []
    for turn in history_data:
        parts = []
        for p in turn.get("parts", []):
            parts.append(types.Part.from_text(text=p.get("text", "")))
        res.append(types.Content(role=turn.get("role", "user"), parts=parts))
    return res


# ─── User Global Memory (Cross-Chat Context) ─────────────────────────────────

def _get_user_global_memory(user_id: int) -> list[dict]:
    """Fetch space-saving global memory for a user across all chats."""
    if not supabase_client: return []
    try:
        res = supabase_client.table('user_global_memory').select('history').eq('user_id', user_id).execute()
        if res.data and res.data[0].get('history'):
            return res.data[0]['history']
    except Exception as e:
        logger.error("Failed to fetch global memory: %s", e)
    return []

def _append_user_global_memory(user_id: int, chat_id: int, text: str, role: str):
    """Append a message to user's global memory. Optimized for 10k users."""
    if not supabase_client or not text or len(text) < 2: return
    # Max 1000 characters per message (enough for very rich context)
    text = text[:1000] + "..." if len(text) > 1000 else text
    def _save():
        try:
            history = _get_user_global_memory(user_id)
            history.append({"c": chat_id, "t": text, "r": role})
            # Keep the last 50 items for deeper memory
            if len(history) > 50:
                history = history[-50:]
            supabase_client.table('user_global_memory').upsert({
                'user_id': user_id,
                'history': history
            }).execute()
        except Exception as e:
            logger.error("Failed to save global memory: %s", e)
    asyncio.create_task(asyncio.to_thread(_save))


def get_chat_session(chat_id: int, message_thread_id: int | None = None):
    """Return an existing or new Gemini chat session with true LRU eviction + Supabase load."""
    key = f"{chat_id}_{message_thread_id}" if message_thread_id else str(chat_id)
    now = time.time()

    if key in _chat_sessions:
        _session_last_used[key] = now
        # Prune old history to save memory
        _prune_session_history(_chat_sessions[key])
        return _chat_sessions[key]

    # Evict LEAST recently used sessions when over limit
    while len(_chat_sessions) >= _MAX_SESSIONS:
        oldest_key = min(_session_last_used, key=_session_last_used.get, default=None)
        if oldest_key:
            del _chat_sessions[oldest_key]
            del _session_last_used[oldest_key]
            logger.debug("Evicted LRU session: %s", oldest_key)
        else:
            break

    # Load from Supabase
    loaded_history = None
    if supabase_client:
        try:
            res = supabase_client.table('chat_sessions').select('history').eq('id', key).execute()
            if res.data and res.data[0].get('history'):
                loaded_history = _deserialize_history(res.data[0]['history'])
                logger.info("Loaded session %s from Supabase with %d turns", key, len(loaded_history))
        except Exception as e:
            logger.error("Failed to load session from Supabase: %s", e)

    _chat_sessions[key] = _get_genai_client().aio.chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=TOOLS,
            temperature=0.7,
            max_output_tokens=4096,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
        history=loaded_history,
    )
    _session_last_used[key] = now
    return _chat_sessions[key]




# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def safe_edit_message(bot, chat_id, message_id, text, reply_markup=None):
    """Edit message with HTML, auto-retry on flood control."""
    html_text = markdown_to_html(text)
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=html_text, parse_mode='HTML', reply_markup=reply_markup,
        )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=html_text, parse_mode='HTML', reply_markup=reply_markup,
            )
        except Exception:
            pass
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        logger.warning("HTML edit failed: %s", e)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=text, reply_markup=reply_markup,
            )
        except Exception as fb:
            if "Message is not modified" not in str(fb):
                logger.error("Fallback edit failed: %s", fb)
    except Exception as e:
        logger.warning("Unexpected edit error: %s", e)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=text, reply_markup=reply_markup,
            )
        except Exception:
            pass


async def _safe_typing(bot, chat_id, action='typing'):
    """Fire-and-forget typing indicator."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception:
        pass


# ── Streaming API (Bot API 9.3+ — sendMessageDraft) ──
# Bot API 10.1: sendRichMessage (32,768 chars) + sendRichMessageDraft
# PTB v22.8 = Bot API 10.0, so Rich Messages use raw do_api_request

_draft_counter = 0


def _next_draft_id():
    """Generate a unique non-zero draft ID (required by API)."""
    global _draft_counter
    _draft_counter += 1
    return _draft_counter


def _markdown_to_rich_blocks(text: str) -> list:
    """Convert markdown text to Bot API 10.1 RichBlock list."""
    blocks = []
    lines = text.split("\n")
    current_para = []

    for line in lines:
        # Heading
        heading_match = _RE_HEADING_LINE.match(line)
        if heading_match:
            if current_para:
                blocks.append({"type": "paragraph", "text": "\n".join(current_para)})
                current_para = []
            blocks.append({"type": "section_heading", "text": heading_match.group(2)})
            continue

        # Code block markers
        if line.strip().startswith("```"):
            if current_para:
                blocks.append({"type": "paragraph", "text": "\n".join(current_para)})
                current_para = []
            continue

        # Divider
        if line.strip() in ("---", "***", "___"):
            if current_para:
                blocks.append({"type": "paragraph", "text": "\n".join(current_para)})
                current_para = []
            blocks.append({"type": "divider"})
            continue

        # Empty line = paragraph break
        if not line.strip():
            if current_para:
                blocks.append({"type": "paragraph", "text": "\n".join(current_para)})
                current_para = []
            continue

        current_para.append(line)

    if current_para:
        blocks.append({"type": "paragraph", "text": "\n".join(current_para)})

    return blocks if blocks else [{"type": "paragraph", "text": text or "⏳"}]


async def send_draft(bot, chat_id, text, draft_id, message_thread_id=None):
    """Stream a draft message using native PTB send_message_draft.
    Fire-and-forget, 0 delay. Ephemeral 30s preview.
    """
    kwargs = dict(chat_id=chat_id, draft_id=draft_id, text=text or "⏳")
    if message_thread_id:
        kwargs["message_thread_id"] = message_thread_id
    try:
        await bot.send_message_draft(**kwargs)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message_draft(**kwargs)
        except Exception as e2:
            logger.error("send_draft retry failed: %s", e2)
    except Exception as e:
        logger.debug("send_draft failed (chat=%s): %s", chat_id, e)


async def send_final(bot, chat_id, text, reply_to_message_id=None, message_thread_id=None):
    """Send the final permanent message.
    Priority: 1) sendRichMessage (long text), 2) entities (premium emoji), 3) HTML, 4) plain text.
    Returns Message object.
    """
    reply_params = ReplyParameters(message_id=reply_to_message_id) if reply_to_message_id else None
    thread_kwargs = {"message_thread_id": message_thread_id} if message_thread_id else {}

    # Try sendRichMessage for long text (Bot API 10.1 — 32,768 char limit)
    if len(text) > 4000:
        try:
            blocks = _markdown_to_rich_blocks(text)
            payload = {
                "chat_id": chat_id,
                "rich_message": {"blocks": blocks},
            }
            if reply_to_message_id:
                payload["reply_parameters"] = {"message_id": reply_to_message_id}
            if message_thread_id:
                payload["message_thread_id"] = message_thread_id
            result = await bot.do_api_request(
                endpoint="sendRichMessage",
                api_kwargs=payload,
            )
            return result
        except Exception as e:
            logger.debug("sendRichMessage failed, falling back: %s", e)

    # Convert markdown to HTML with premium emojis
    html_text = markdown_to_html(text)

    # Try entities approach FIRST (works in DM AND groups for premium emoji)
    from telegram import MessageEntity
    tg_entities = []
    plain_text = ""
    try:
        plain_text, entities = parse_html_to_entities(html_text)
        if entities:
            # Convert dict entities to MessageEntity objects
            for e in entities:
                kwargs = {"type": e["type"], "offset": e["offset"], "length": e["length"]}
                if e.get("custom_emoji_id"):
                    kwargs["custom_emoji_id"] = str(e["custom_emoji_id"])
                if e.get("url"):
                    kwargs["url"] = e["url"]
                tg_entities.append(MessageEntity(**kwargs))
            return await bot.send_message(
                chat_id=chat_id, text=plain_text, entities=tg_entities,
                reply_parameters=reply_params, **thread_kwargs,
            )
    except Exception as e:
        logger.warning("send_final entities failed (chat=%s): %s", chat_id, e)
        # Retry without custom_emoji entities (keep bold, italic etc.)
        if tg_entities:
            try:
                non_emoji_entities = [
                    ent for ent in tg_entities if ent.type != MessageEntity.CUSTOM_EMOJI
                ]
                if non_emoji_entities:
                    return await bot.send_message(
                        chat_id=chat_id, text=plain_text, entities=non_emoji_entities,
                        reply_parameters=reply_params, **thread_kwargs,
                    )
            except Exception:
                pass

    # Standard HTML path (fallback)
    try:
        return await bot.send_message(
            chat_id=chat_id, text=html_text, parse_mode='HTML',
            reply_parameters=reply_params, **thread_kwargs,
        )
    except Exception as e:
        logger.warning("send_final HTML failed (chat=%s): %s", chat_id, e)
    # Plain text fallback
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text[:4096],
            reply_parameters=reply_params, **thread_kwargs,
        )
    except Exception as e:
        logger.error("send_final failed: %s", e)
        return None


async def safe_send_message(bot, chat_id, text, reply_to_message_id=None, message_thread_id=None):
    """Send a new message with HTML, falling back to plain text."""
    html_text = markdown_to_html(text)
    thread_kwargs = {"message_thread_id": message_thread_id} if message_thread_id else {}
    try:
        return await bot.send_message(
            chat_id=chat_id, text=html_text, parse_mode='HTML',
            reply_to_message_id=reply_to_message_id, **thread_kwargs,
        )
    except Exception:
        return await bot.send_message(
            chat_id=chat_id, text=text,
            reply_to_message_id=reply_to_message_id, **thread_kwargs,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  TOOL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

async def execute_tool_calls(function_calls, bot=None, chat_id=None, reply_msg=None):
    """Execute tool calls and return function response parts."""
    responses = []
    for call in function_calls:
        if call.name == "get_current_time":
            result = await get_current_time()

        # ── Telegram Management Tools ──
        elif call.name == "tg_get_chat_info":
            target = call.args.get("chat_id", "")
            result = await _exec_tg_get_chat_info(bot, target)
        elif call.name == "tg_get_chat_member_count":
            target = call.args.get("chat_id", "")
            result = await _exec_tg_get_member_count(bot, target)
        elif call.name == "tg_send_to_channel":
            target = call.args.get("chat_id", "")
            text = call.args.get("text", "")
            result = await _exec_tg_send_to_channel(bot, target, text)
        elif call.name == "tg_get_admins":
            target = call.args.get("chat_id", "")
            result = await _exec_tg_get_admins(bot, target)
        else:
            result = f"Unknown tool: {call.name}"

        responses.append(
            types.Part.from_function_response(
                name=call.name, response={"result": result}
            )
        )
    return responses


# ── Telegram tool implementations ────────────────────────────────────────────

async def _exec_tg_get_chat_info(bot, target):
    """Get chat info via Telegram API."""
    if not bot or not target:
        return "Error: bot yoki chat_id yo'q"
    try:
        chat = await bot.get_chat(chat_id=target)
        info = {
            "id": chat.id,
            "title": chat.title or chat.first_name or "N/A",
            "type": chat.type,
            "username": f"@{chat.username}" if chat.username else "N/A",
            "description": (chat.description or "")[:200],
            "member_count": getattr(chat, 'member_count', None),
            "invite_link": chat.invite_link or "N/A",
        }
        # Check if bot is admin
        try:
            me = await bot.get_chat_member(chat_id=target, user_id=bot.id)
            info["bot_status"] = me.status
            info["bot_can_post"] = getattr(me, 'can_post_messages', False)
            info["bot_can_edit"] = getattr(me, 'can_edit_messages', False)
            info["bot_can_delete"] = getattr(me, 'can_delete_messages', False)
        except Exception:
            info["bot_status"] = "not_member"
        return json.dumps(info, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


async def _exec_tg_get_member_count(bot, target):
    """Get member count via Telegram API."""
    if not bot or not target:
        return "Error: bot yoki chat_id yo'q"
    try:
        count = await bot.get_chat_member_count(chat_id=target)
        return f"A'zolar soni: {count}"
    except Exception as e:
        return f"Error: {e}"


async def _exec_tg_send_to_channel(bot, target, text):
    """Send a message to a channel where bot is admin."""
    if not bot or not target:
        return "Error: bot yoki chat_id yo'q"
    if not text:
        return "Error: text bo'sh"
    try:
        # Guest mode: convert to HTML WITHOUT premium emoji conversion
        # (premium emojis don't work in guest mode, regular ones look ugly)
        html_text = _guest_markdown_to_html(text)
        msg = await bot.send_message(
            chat_id=target,
            text=html_text,
            parse_mode='HTML',
        )
        return f"Post muvaffaqiyatli yuborildi! Message ID: {msg.message_id}"
    except Forbidden:
        return "Error: Bot bu kanalda admin emas yoki post yuborish ruxsati yo'q. Iltimos botni admin qilib qo'ying."
    except Exception as e:
        return f"Error: {e}"


async def _exec_tg_get_admins(bot, target):
    """Get list of admins via Telegram API."""
    if not bot or not target:
        return "Error: bot yoki chat_id yo'q"
    try:
        admins = await bot.get_chat_administrators(chat_id=target)
        result = []
        for a in admins:
            name = a.user.full_name or a.user.username or str(a.user.id)
            username = f"@{a.user.username}" if a.user.username else ""
            is_bot = " [BOT]" if a.user.is_bot else ""
            result.append(f"- {name} {username}{is_bot} ({a.status})")
        return f"Adminlar ({len(admins)}):\n" + "\n".join(result)
    except Exception as e:
        return f"Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  PREMIUM REPLY HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def premium_reply(message, text: str):
    """Reply with premium emojis — converts ALL emojis to custom premium versions.
    Uses entities API for custom_emoji support, falls back to HTML, then plain text."""
    # Step 1: Convert emojis to premium tg-emoji tags
    premium_text = emojis_to_premium(text)
    # Step 2: Parse to plain text + entities (handles bold, italic, custom_emoji etc.)
    plain_text, entities = parse_html_to_entities(premium_text)
    
    if entities:
        try:
            await message.reply_text(plain_text, entities=entities)
            return
        except Exception:
            pass
    
    # Fallback: HTML with premium tags
    try:
        await message.reply_text(premium_text, parse_mode='HTML')
    except Exception:
        # Final fallback: plain text
        try:
            await message.reply_text(text)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    text = (
        "<b>Assalomu alaykum! Mening ismim Agentjon.</b> 🤖\n\n"
        "Men Gemini modeli va zamonaviy web-tahlil imkoniyatlari asosida "
        "ishlaydigan aqlli sun'iy intellekt yordamchisiman.\n\n"
        "💬 Menga ixtiyoriy savol bering, kerak bo'lsa internetdan qidirib, "
        "javob topib beraman.\n\n"
        "📝 <b>Komandalar:</b>\n"
        "/clear - Suhbat tarixini tozalash\n"
        "/help - Bot haqida ma'lumot"
    )
    await premium_reply(update.message, text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    text = (
        "🤖 <b>Agentjon Bot Yo'riqnomasi</b>\n\n"
        "1. <b>Jonli Yozish (Streaming):</b> Bot javobni qismlab ko'rsata boshlaydi.\n"
        "2. <b>Internet Qidiruv:</b> Yangilik va faktlarga oid savollarda bot "
        "avtomatik internetdan qidiradi.\n"
        "3. <b>Multimodal imkoniyatlar:</b>\n"
        "   🎤 <i>Ovozli xabar</i> — bot ovozingizni tinglab javob beradi.\n"
        "   🖼 <i>Rasm</i> — rasmdagi narsalarni tahlil qiladi.\n"
        "   📄 <i>Hujjatlar</i> — yuborilgan fayllarni o'rganadi.\n"
        "4. <b>Suhbat Tarixi:</b> Bot suhbatning oldingi qismlarini eslab qoladi.\n\n"
        "📡 <b>Telegram Boshqaruv:</b>\n"
        "• Kanalga post yozish — botni admin qiling va kanal @username yuboring\n"
        "• Kanal/guruh ma'lumoti — @username yoki chat_id yuboring\n"
        "• Adminlar ro'yxati — guruh haqida so'rang\n\n"
        "✨ <b>Maxsus buyruqlar:</b>\n"
        "📝 /post <i>mavzu</i> — premium Telegram post yaratish\n"
        "🧹 /clear — suhbat tarixini tozalash"
    )
    await premium_reply(update.message, text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear command."""
    chat_id = update.effective_chat.id
    msg = update.effective_message
    thread_id = msg.message_thread_id if msg else None
    key = f"{chat_id}_{thread_id}" if thread_id else str(chat_id)
    _chat_sessions.pop(key, None)
    _session_last_used.pop(key, None)
    _chat_sessions.pop(f"{chat_id}_channel", None)
    _session_last_used.pop(f"{chat_id}_channel", None)
    await premium_reply(msg,
        "🧹 <b>Suhbat tarixi tozalandi!</b> Yangi mavzudan boshlashimiz mumkin.")


async def cmd_addemoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addemoji <pack_name> — add custom emoji pack to bot."""
    msg = update.effective_message
    if os.getenv('VERCEL'):
        return await premium_reply(msg, 'Bu buyruq serverless muhitda ishlamaydi.')
    if not context.args:
        await premium_reply(msg,
            "📦 <b>Custom emoji pack qo'shish</b>\n\n"
            "Foydalanish: <code>/addemoji pack_nomi</code>\n\n"
            "Pack nomini <code>t.me/addemoji/PACK_NOMI</code> havolasidan oling.\n"
            "Masalan: <code>/addemoji MyEmojiPack</code>")
        return

    pack_name = context.args[0].strip()
    # Clean up if user sends full URL
    if "t.me/addemoji/" in pack_name:
        pack_name = pack_name.split("t.me/addemoji/")[-1].strip("/")

    await premium_reply(msg, f"⏳ <code>{pack_name}</code> yuklanmoqda...")

    try:
        sticker_set = await context.bot.get_sticker_set(pack_name)
    except Exception as e:
        await premium_reply(msg, f"❌ Pack topilmadi: <code>{pack_name}</code>\n{e}")
        return

    # Load existing multi-map
    map_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emoji_multi_map.json')
    try:
        with open(map_path, 'r', encoding='utf-8') as f:
            emap = json.load(f)
    except Exception:
        emap = {}

    added = 0
    total = 0
    for sticker in sticker_set.stickers:
        if sticker.custom_emoji_id and sticker.emoji:
            total += 1
            emoji = sticker.emoji
            eid = sticker.custom_emoji_id
            if emoji not in emap:
                emap[emoji] = [eid]
                added += 1
            elif eid not in emap[emoji]:
                emap[emoji].append(eid)
                added += 1

    # Save
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(emap, f, ensure_ascii=False, indent=2)

    # Save to Supabase
    if supabase_client and added > 0:
        def _sync_supabase():
            try:
                for sticker in sticker_set.stickers:
                    if sticker.custom_emoji_id and sticker.emoji:
                        supabase_client.table('emoji_cache').upsert({
                            'emoji': sticker.emoji,
                            'custom_ids': emap.get(sticker.emoji, [])
                        }).execute()
            except Exception as e:
                logger.error("Failed to sync emoji pack to Supabase: %s", e)
        asyncio.create_task(asyncio.to_thread(_sync_supabase))

    # Reload cache (reset both map and regex)
    global _EMOJI_MAP, _emoji_regex
    _EMOJI_MAP = {}
    _emoji_regex = None
    _load_emoji_map()

    total_ids = sum(len(v) if isinstance(v, list) else 1 for v in emap.values())
    await premium_reply(msg,
        f"✅ <b>{pack_name}</b>\n"
        f"📊 Jami: {total} ta emoji\n"
        f"🆕 Yangi qo'shildi: <b>{added}</b> ta\n"
        f"📦 Umumiy: <b>{len(emap)}</b> emoji, <b>{total_ids}</b> variant")


async def cmd_emojicount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /emojicount — show how many premium emojis are loaded."""
    emap = _load_emoji_map()
    await premium_reply(update.message,
        f"📦 <b>{len(emap)}</b> ta premium custom emoji yuklangan.")


# ══════════════════════════════════════════════════════════════════════════════
#  PREMIUM POST CREATOR
# ══════════════════════════════════════════════════════════════════════════════

POST_CREATOR_PROMPT = """Sen professional Telegram Content Creator san.
Vazifa: Berilgan mavzu va ko'rsatmalarga asosan MUKAMMAL Telegram post yarat.

QOIDALAR:
1. Post Telegram formatlashiga mos bo'lsin — **qalin**, *kursiv*, emojilar
2. Foydalanuvchi ko'rsatgan emojilarni ALBATTA ishlat va ko'p joyda ishlat
3. Post vizual jihatdan JUDA jozibador bo'lsin — ko'zni quvnantirsin
4. Sarlavha KUCHLI va e'tibor tortuvchi bo'lsin
5. Call-to-action bo'lsin (agar kerak bo'lsa)
6. Post uzunligi: 150-500 so'z (mavzuga qarab moslash)
7. Har bir paragraf orasida bo'sh qator bo'lsin
8. Emojilarni sarlavha, muhim nuqtalar va yakunida ishlat

USLUBLAR (agar ko'rsatilgan bo'lsa):
• motivatsion — ilhomlantiruvchi, kuchli so'zlar, energiya
• yangilik — rasmiy, faktga asoslangan, aniq
• ta'limiy — tushuntiruvchi, qadamma-qadam, foydali
• e'lon — e'tibor tortuvchi, CTA bor, shoshilinch
• marketing — sotuvga yo'naltirilgan, FOMO, qadriyat
• shaxsiy — samimiy, do'stona, ochiq

MUHIM: Javobingda FAQAT postning o'zini yoz. Hech qanday izoh, tushuntirish yoki qo'shimcha gap YO'Q.
Boshlash yoki tugatish uchun "Mana post:" kabi gaplar YOZMA — FAQAT postni ber."""


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /post — generate premium Telegram post."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    thread_id = msg.message_thread_id if msg else None

    # Argumentlarni olish
    user_input = ' '.join(context.args) if context.args else ''
    if not user_input:
        await premium_reply(msg,
            "📝 <b>Premium Post Creator</b>\n\n"
            "Foydalanish: <code>/post mavzu va ko'rsatmalar</code>\n\n"
            "<b>Misollar:</b>\n"
            "• <code>/post Dasturlashni o'rganish haqida motivatsion post, 🔥💡🚀 emoji</code>\n"
            "• <code>/post IT yangiliklar: GPT-5 chiqqani, rasmiy uslubda</code>\n"
            "• <code>/post Kursingizga reklama, marketing uslubida, ✨🎯💰 emoji</code>\n"
            "• <code>/post Telegram kanalim uchun salom post</code>\n\n"
            "💡 Qaysi emojilardan foydalanishni ham yozishingiz mumkin!")
        return

    # Thinking animation
    draft_id = _next_draft_id()
    _thinking = [True]

    async def _anim():
        frames = [
            "📡 Ma'lumot qabul qilinmoqda...",
            "🧠 Mantiqiy tahlil boshlandi...",
            "🔍 Kontekst o'rganilmoqda...",
            "💡 Yechim topilmoqda...",
            "✍️ Eng mukammal javob shakllantirilmoqda...",
            "🚀 Javob tayyor, yuborilmoqda..."
        ]
        random.shuffle(frames)
        i = 0
        while _thinking[0]:
            try:
                await safe_edit_message(context.bot, chat_id, draft_id, frames[i % len(frames)])
            except Exception:
                pass
            i += 1
            await asyncio.sleep(0.4)

    anim_task = asyncio.create_task(_anim())

    try:
        # Maxsus post yaratish sessiyasi (alohida, asosiy chatga aralashmasin)
        client = _get_genai_client()
        post_session = client.aio.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=POST_CREATOR_PROMPT,
                temperature=0.9,  # Kreativlik yuqori
                max_output_tokens=8192,
            ),
        )

        prompt = f"Quyidagi mavzu va ko'rsatmalar asosida Telegram post yarat:\n\n{user_input}"

        response_text = ""
        stream = await post_session.send_message_stream(prompt)
        last_draft_len = 0

        async for chunk in stream:
            try:
                if chunk.text:
                    response_text += chunk.text
                    # Stop animation on first text
                    if _thinking[0]:
                        _thinking[0] = False
                        anim_task.cancel()
                    # Live draft
                    new_len = len(response_text)
                    if new_len - last_draft_len >= 30 and response_text.strip():
                        asyncio.create_task(send_draft(context.bot, chat_id, response_text, draft_id, message_thread_id=thread_id))
                        last_draft_len = new_len
            except (AttributeError, IndexError, ValueError):
                continue

        _thinking[0] = False
        anim_task.cancel()

        if not response_text.strip():
            response_text = "Post yaratishda xatolik. Iltimos qaytadan urinib ko'ring."

        # Post DOIM premium emoji bilan chiqadi (bu maxsus feature)
        await send_final(
            context.bot, chat_id, response_text,
            reply_to_message_id=msg.message_id,
            message_thread_id=thread_id,
        )

        # Success reaction
        asyncio.create_task(set_premium_reaction(msg, "🔥"))

    except Exception as e:
        _thinking[0] = False
        anim_task.cancel()
        logger.error("cmd_post error: %s", e)
        try:
            await premium_reply(msg, f"❌ Post yaratishda xatolik: {e}")
        except Exception:
            await premium_reply(msg, f"Xatolik: {e}")


async def _handle_guest_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              message, guest_query_id: str):
    """Handle messages from chats where bot is NOT a member (Guest Mode).
    Uses answerGuestQuery API to send response with premium emojis."""
    await init_bot_info(context.bot)

    user_name = message.from_user.first_name if message.from_user else "Foydalanuvchi"
    text = message.text or message.caption or "Salom!"

    # Remove @bot mention
    if BOT_USERNAME:
        text = text.replace(f"@{BOT_USERNAME}", "").strip()
    if not text:
        text = "Salom!"

    logger.info("Guest flow from %s (chat=%s): %s", user_name, update.effective_chat.id, repr(text)[:80])

    try:
        # AI response with streaming
        user_id = update.effective_user.id if update.effective_user else 0
        session = get_chat_session(user_id, None)
        prompt = f"[{user_name}] (Guest so'rov - bot a'zo bo'lmagan chatdan. MUHIM: Guest mode da premium emoji ishlamaydi, shuning uchun JUDA KAM emoji ishlat - faqat 1-2 ta oddiy Unicode emoji, matn sifatiga qattiq e'tibor ber!): {text}"

        response_text = ""
        stream = await session.send_message_stream(prompt)
        async for chunk in stream:
            try:
                for part in chunk.candidates[0].content.parts:
                    if hasattr(part, 'text') and part.text:
                        response_text += part.text
                    elif hasattr(part, 'executable_code') and part.executable_code:
                        response_text += f"\n\n```python\n{part.executable_code.code}\n```\n"
                    elif hasattr(part, 'code_execution_result') and part.code_execution_result:
                        response_text += f"\n`Natija: {part.code_execution_result.output}`\n"
            except Exception:
                try:
                    if chunk.text:
                        response_text += chunk.text
                except Exception:
                    pass

        # Clean up
        response_text = _RE_GUEST_REACTION.sub('', response_text)
        response_text = response_text.replace('[IGNORE]', '').replace('[DELETE_MSG]', '')
        response_text = response_text.strip()

        if not response_text:
            response_text = "Savol bering, javob beraman! 😊"

        # Convert to HTML without premium emojis
        html_text = _guest_markdown_to_html(response_text)

        # Parse HTML to plain text and entities array (supporting ALL formatting tags like bold, custom_emoji, links, blockquotes)
        # httpx imported at top level
        plain_text, entities = parse_html_to_entities(html_text)

        token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
        api_url = f"https://api.telegram.org/bot{token}/answerGuestQuery"

        input_content = {"message_text": plain_text}
        if entities:
            input_content["entities"] = entities
        else:
            # No custom emojis — use parse_mode for other formatting
            input_content = {"message_text": html_text, "parse_mode": "HTML"}

        payload = {
            "guest_query_id": guest_query_id,
            "result": {
                "type": "article",
                "id": "guest_response",
                "title": "Agentjon javobi",
                "input_message_content": input_content,
            },
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(api_url, json=payload, timeout=30)
            result_data = resp.json()

        if result_data.get("ok"):
            logger.info("Guest query answered (entities) for %s. Payload: %s", user_name, json.dumps(payload))
        else:
            logger.warning("Guest API error: %s — falling back. Payload: %s", result_data, json.dumps(payload))
            # Fallback: plain HTML without custom emojis
            plain_html = _guest_markdown_to_html(response_text)
            # InlineQueryResultArticle, InputTextMessageContent imported at top level
            result = InlineQueryResultArticle(
                id="guest_fallback",
                title="Agentjon javobi",
                input_message_content=InputTextMessageContent(
                    message_text=plain_html,
                    parse_mode='HTML',
                ),
            )
            await context.bot.answer_guest_query(
                guest_query_id=guest_query_id,
                result=result,
            )
            logger.info("Guest query answered (fallback) for %s", user_name)

    except Exception as e:
        logger.error("Guest flow error: %s", e)
        try:
            # InlineQueryResultArticle, InputTextMessageContent imported at top level
            result = InlineQueryResultArticle(
                id="guest_error",
                title="Agentjon",
                input_message_content=InputTextMessageContent(
                    message_text=f"🚀 Salom! Men Agentjon (Premium AI yordamchi). Guruhda bo'lmasam ham savollaringizga shunday chiroyli javob bera olaman! Marhamat, so'rayvering!",
                ),
            )
            await context.bot.answer_guest_query(
                guest_query_id=guest_query_id,
                result=result,
            )
        except Exception as e2:
            logger.warning("Guest error fallback also failed: %s", e2)


# ── Smart Auto-Reply: guruhda savolga o'zi javob berish ──
_group_last_auto: dict[int, float] = {}  # chat_id -> last auto-reply timestamp
_AUTO_REPLY_COOLDOWN = 30  # 30 soniya - faol muloqot uchun


def _should_auto_reply(chat_id: int, text: str) -> bool:
    """Guruhda avtomatik javob berish kerakmi - aqlli filtr (Agentjon 2.0)."""
    now = time.time()

    # Cooldown tekshirish
    last = _group_last_auto.get(chat_id, 0)
    if now - last < _AUTO_REPLY_COOLDOWN:
        return False

    # Juda qisqa xabarlar - javob bermaslik
    if len(text) < 5:
        return False

    text_lower = text.lower()
    has_question_mark = '?' in text
    
    # Kengaytirilgan savol va yordam so'zlari
    question_words = ['qanday', 'qanaqa', 'nimaga', 'nega', 'kim', 'qachon', 'qayerda', 'yordam', 'xato', 'error', 'bug']
    has_question_word = any(w in text_lower for w in question_words)
    
    # Kod yoki dasturlash elementlari
    is_code = 'def ' in text or 'function' in text or 'console.log' in text or 'print(' in text or 'import ' in text

    should_reply = has_question_mark or has_question_word or is_code

    if should_reply:
        _group_last_auto[chat_id] = now
        logger.info("Smart auto-reply triggered for chat=%s text=%s", chat_id, repr(text)[:60])

    return should_reply


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all incoming messages: text, photo, voice, document, location."""
    message = update.effective_message
    if message is None:
        return
    if message.text and message.text.startswith('/'):
        return
    # Skip own messages (prevent infinite loop)
    if message.from_user and message.from_user.id == BOT_ID:
        return

    # ── Guest Mode: bot is NOT a member of this chat ──
    guest_query_id = getattr(message, 'guest_query_id', None)
    if guest_query_id:
        await _handle_guest_flow(update, context, message, guest_query_id)
        return

    chat_id = update.effective_chat.id
    thread_id = message.message_thread_id
    await init_bot_info(context.bot)
    await warm_up_emojis(context.bot.token)  # Preload 260+ common custom emojis (runs once)

    # ── Determine if bot should respond ──
    is_private = message.chat.type == "private"
    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == BOT_ID
    )
    text_content = message.text or message.caption or ""
    is_mentioned = BOT_USERNAME and f"@{BOT_USERNAME}" in text_content
    is_direct = is_private or is_reply_to_bot or is_mentioned

    # ── Smart auto-reply: guruhda savolga o'zi javob beradi ──
    is_smart_reply = False
    if not is_direct and not is_private and text_content:
        is_smart_reply = _should_auto_reply(chat_id, text_content)

    # ── Content extraction ──
    user_text = text_content
    photo = message.photo
    voice = message.voice
    video = message.video
    video_note = message.video_note
    audio = message.audio
    document = message.document
    location = message.location
    contact = message.contact

    if not (user_text or photo or voice or video or video_note or audio or document or location or contact):
        return

    logger.info("Message from chat=%s direct=%s smart=%s text=%s", chat_id, is_direct, is_smart_reply, repr(text_content)[:80])

    # ── React + typing (fire-and-forget — 0 blocking) ──
    should_respond = is_direct or is_smart_reply
    if not should_respond:
        return
    if should_respond:
        asyncio.create_task(set_premium_reaction(message, "🤔"))
        asyncio.create_task(_safe_typing(context.bot, chat_id))

    # ── Streaming (Bot API 9.3+) ──
    original_message_id = message.message_id
    draft_id = _next_draft_id()

    # ── Thinking animation: premium emoji drafts while AI thinks ──
    _thinking_state = [True]  # list for closure mutability

    async def _thinking_animation():
        """Animated status updates for user engagement."""
        frames = [
            "🧠 Neyronlar ishlamoqda...", "🤔 Javob topilmoqda...", "🔍 Internetdan izlanyapti...",
            "⚗️ Tahlil qilinmoqda...", "✨ Sehrli javob tayyorlanyapti...", "🚀 Tez orada tayyor!"
        ]
        i = 0
        while _thinking_state[0]:
            try:
                await safe_edit_message(context.bot, chat_id, draft_id, frames[i % len(frames)])
            except Exception:
                pass
            i += 1
            await asyncio.sleep(1.5)

    thinking_task = None
    if should_respond:
        thinking_task = asyncio.create_task(_thinking_animation())

    # ── Assemble multimodal content ──
    contents: list = []
    reply_message = None
    try:
        # ── Fast parallel media download — 0 wasted time ──
        async def _dl(file_id):
            f = await context.bot.get_file(file_id)
            return bytes(await f.download_as_bytearray())

        if photo:
            file_size = photo[-1].file_size or 0
            if file_size > _MAX_FILE_SIZE:
                await premium_reply(message, "⚠️ Rasm hajmi 20MB dan oshib ketdi.")
                return
            asyncio.create_task(_safe_typing(context.bot, chat_id, 'upload_photo'))
            photo_bytes = await _dl(photo[-1].file_id)
            contents.append(types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg"))
            contents.append(message.caption or "Ushbu rasmni batafsil tahlil qilib javob ber.")

        elif voice:
            file_size = voice.file_size or 0
            if file_size > _MAX_FILE_SIZE:
                await premium_reply(message, "⚠️ Ovozli xabar hajmi 20MB dan oshib ketdi.")
                return
            asyncio.create_task(_safe_typing(context.bot, chat_id, 'record_voice'))
            voice_bytes = await _dl(voice.file_id)
            contents.append(types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg"))
            contents.append(message.caption or "Ushbu ovozli xabarni eshitib, unga o'zbek tilida javob ber.")

        elif document:
            file_size = document.file_size or 0
            if file_size > _MAX_FILE_SIZE:
                await premium_reply(message, "⚠️ Fayl hajmi 20MB dan oshib ketdi.")
                return
            asyncio.create_task(_safe_typing(context.bot, chat_id, 'upload_document'))
            doc_bytes = await _dl(document.file_id)
            fname = document.file_name or "document"
            mime = document.mime_type or "application/octet-stream"
            code_ext = ('.py', '.js', '.json', '.html', '.css', '.sh', '.java',
                        '.cpp', '.c', '.txt', '.md', '.xml', '.yml', '.yaml')
            if fname.lower().endswith(code_ext):
                mime = "text/plain"
            contents.append(types.Part.from_bytes(data=doc_bytes, mime_type=mime))
            contents.append(message.caption or f"Ushbu '{fname}' faylini tahlil qil va unga javob ber.")

        elif video:
            file_size = video.file_size or 0
            if file_size > _MAX_FILE_SIZE:
                await premium_reply(message, "⚠️ Video hajmi 20MB dan oshib ketdi.")
                return
            asyncio.create_task(_safe_typing(context.bot, chat_id, 'upload_video'))
            video_bytes = await _dl(video.file_id)
            contents.append(types.Part.from_bytes(data=video_bytes, mime_type=video.mime_type or "video/mp4"))
            contents.append(message.caption or "Ushbu videoni ko'rib, tahlil qilib javob ber.")

        elif video_note:
            file_size = video_note.file_size or 0
            if file_size > _MAX_FILE_SIZE:
                await premium_reply(message, "⚠️ Video xabar hajmi 20MB dan oshib ketdi.")
                return
            asyncio.create_task(_safe_typing(context.bot, chat_id, 'record_video_note'))
            vn_bytes = await _dl(video_note.file_id)
            contents.append(types.Part.from_bytes(data=vn_bytes, mime_type="video/mp4"))
            contents.append("Ushbu video xabarni ko'rib, unga javob ber.")

        elif audio:
            file_size = audio.file_size or 0
            if file_size > _MAX_FILE_SIZE:
                await premium_reply(message, "⚠️ Audio hajmi 20MB dan oshib ketdi.")
                return
            asyncio.create_task(_safe_typing(context.bot, chat_id, 'upload_voice'))
            audio_bytes = await _dl(audio.file_id)
            contents.append(types.Part.from_bytes(data=audio_bytes, mime_type=audio.mime_type or "audio/mpeg"))
            contents.append(message.caption or "Ushbu audio faylni eshitib, unga javob ber.")

        # Replied Message Context
        if message.reply_to_message:
            replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""
            if replied_text:
                contents.append(f"[Foydalanuvchi quyidagi xabarga javob (reply) yozmoqda:\n\"{replied_text}\"]")

        # User identification + context note
        user_name = message.from_user.first_name if message.from_user else "Foydalanuvchi"
        user_id = message.from_user.id if message.from_user else chat_id
        ctx_note = ""
        if not is_direct and not is_smart_reply:
            ctx_note = " (Guruhdagi o'zaro suhbat. Senga murojaat qilinmadi. Agar javobing bo'lmasa, faqat [IGNORE] deb yoz.)"
        elif is_smart_reply:
            ctx_note = " (Guruhda savol so'raldi. Qisqa va foydali javob ber. Agar javob bera olmasang [IGNORE].)"

        # Save user message to global memory
        if user_text:
            _append_user_global_memory(user_id, chat_id, user_text, "u")

        # Cross-Chat Context Injection (for private DMs)
        if is_private:
            global_mem = _get_user_global_memory(user_id)
            if global_mem:
                mem_str = "\n".join([f"[{m.get('r','u')} in chat {m.get('c','?')}]: {m.get('t','')}" for m in global_mem])
                contents.append(
                    f"[Maxfiy tizim eslatmasi: Bu foydalanuvchining boshqa guruhlar yoki chatlardagi oxirgi xotirasi:\n{mem_str}\nShularni yodda tutgan holda uning quyidagi xabariga munosabat bildir!]"
                )

        if contact:
            contents.append(
                f"[{user_name}]{ctx_note}: Kontakt yubordi — "
                f"Ism: {contact.first_name or ''} {contact.last_name or ''}, "
                f"Tel: {contact.phone_number or '?'}"
            )
        elif location:
            contents.append(
                f"[{user_name} ning joylashuvi]{ctx_note}: "
                f"Kenglik {location.latitude}, Uzunlik {location.longitude}."
            )
            contents.append(
                "Iltimos ushbu joylashuvga e'tibor berib javob ber "
                "(qayerdaligim, ob-havo va h.k). Kerak bo'lsa internetdan izla."
            )
        elif user_text:
            contents.append(f"[{user_name}]{ctx_note}: {user_text}")
        else:
            contents.append(f"[{user_name}]{ctx_note} rasm/audio/video/fayl yubordi.")

        # ── AI Processing with native streaming ──
        session = get_chat_session(chat_id, thread_id)

        response_text = ""
        current_prompt = contents
        last_draft_len = 0
        tool_round = 0

        while True:
            tool_round += 1
            if tool_round > 5:
                logger.warning("Tool loop exceeded max iterations (5) for chat=%s", chat_id)
                break

            try:
                stream = await session.send_message_stream(current_prompt)
                tool_calls = []

                async for chunk in stream:
                    # Tool calls
                    if chunk.function_calls:
                        tool_calls.extend(chunk.function_calls)
                        continue

                    # Extract text - fastest possible path
                    txt = ""
                    try:
                        for part in chunk.candidates[0].content.parts:
                            if hasattr(part, 'text') and part.text:
                                txt += part.text
                            elif hasattr(part, 'executable_code') and part.executable_code:
                                txt += f"

```python
{part.executable_code.code}
```
"
                            elif hasattr(part, 'code_execution_result') and part.code_execution_result:
                                txt += f"
`Natija: {part.code_execution_result.output}`
"
                    except Exception:
                        try:
                            txt = chunk.text
                        except Exception:
                            pass
                    
                    if not txt:
                        continue

                    response_text += txt

                    # Stop thinking animation on first real text
                    if _thinking_state[0]:
                        _thinking_state[0] = False
                        if thinking_task:
                            thinking_task.cancel()
                            
                        # Ensure draft is clean
                        try:
                            await safe_edit_message(context.bot, chat_id, draft_id, " ")
                        except Exception:
                            pass

                    # Skip special commands instantly
                    s = response_text.lstrip()
                    if s[:8] == "[IGNORE]" or s[:11] == "[DELETE_MSG":
                        continue

                    # Reaction - instant fire-and-forget
                    if "[REACTION:" in txt:
                        rmatch = _RE_REACTION.search(response_text)
                        if rmatch:
                            asyncio.create_task(set_premium_reaction(message, rmatch.group(1).strip()))
                            response_text = response_text.replace(rmatch.group(0), "")

                    # 100% LIVE DRAFT - every chunk, 0 delay
                    new_len = len(response_text)
                    if new_len - last_draft_len >= 30 and response_text.strip():
                        asyncio.create_task(send_draft(context.bot, chat_id, response_text, draft_id, message_thread_id=thread_id))
                        last_draft_len = new_len
            except Exception as e:
                logger.error("Gemini API Error: %s", e)
                if should_respond:
                    await premium_reply(message, f"❌ Uzr, AI xizmatida xatolik yuz berdi: `{e}`")
                break
            finally:
                if _thinking_state[0]:
                    _thinking_state[0] = False
                    if thinking_task:
                        thinking_task.cancel()
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=draft_id)
                    except Exception:
                        pass

            if not tool_calls:
                break

            # Execute tools
            current_prompt = await execute_tool_calls(
                tool_calls, context.bot, chat_id, reply_message,
            )

        # ── Finalize: commit draft to permanent message (ONCE, after tool loop) ──
        if (response_text
                and not response_text.strip().startswith("[DELETE_MSG]")
                and not response_text.strip().startswith("[IGNORE]")):
            # Search for custom emojis for any new emojis in response
            await ensure_emoji_coverage(context.bot.token, response_text)
            reply_message = await send_final(
                context.bot, chat_id, response_text,
                reply_to_message_id=original_message_id,
                message_thread_id=thread_id,
            )

        # ── Post-processing ──
        stripped = response_text.strip()
        if stripped.startswith("[IGNORE]"):
            pass  # silently ignore
        elif stripped.startswith("[DELETE_MSG]"):
            try:
                await message.delete()
            except Exception as e:
                logger.warning("Failed to delete message: %s", e)
            if reply_message and isinstance(reply_message, Message):
                try:
                    await reply_message.delete()
                except Exception as e:
                    logger.debug("Failed to delete reply_message in moderation: %s", e)
            try:
                uname = (
                    f"@{message.from_user.username}"
                    if message.from_user and message.from_user.username
                    else (message.from_user.first_name if message.from_user else "Foydalanuvchi")
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Guruhda tartibni saqlash maqsadida {uname} yuborgan nojo'ya xabar o'chirildi.",
                )
            except Exception as e:
                logger.debug("Failed to send moderation notice: %s", e)
        elif response_text:
            # For very long messages (over RichMessage 32768 limit), send additional chunks
            max_len = 32768
            if len(response_text) > max_len and reply_message:
                for i in range(max_len, len(response_text), 4000):
                    chunk_text = response_text[i:i + 4000]
                    try:
                        await safe_send_message(context.bot, chat_id, chunk_text,
                                                reply_to_message_id=None)
                    except Exception as e:
                        logger.warning("Failed to send overflow chunk: %s", e)

        # Success reaction (fire-and-forget — 0 delay)
        if not stripped.startswith("[IGNORE]"):
            asyncio.create_task(set_premium_reaction(message, "👍"))

        # ── Save Session to Supabase ──
        if supabase_client:
            user_id = message.from_user.id if message.from_user else chat_id
            if response_text:
                _append_user_global_memory(user_id, chat_id, response_text, "b")
                
            try:
                history_data = _serialize_history(session)
                key = f"{chat_id}_{thread_id}" if thread_id else str(chat_id)
                def _save_session_sync():
                    try:
                        supabase_client.table('chat_sessions').upsert({
                            'id': key,
                            'history': history_data
                        }).execute()
                    except Exception as e:
                        logger.error("Failed to save session to Supabase in thread: %s", e)
                asyncio.create_task(asyncio.to_thread(_save_session_sync))
            except Exception as e:
                logger.error("Failed to start Supabase save task: %s", e)

    except Forbidden as e:
        _thinking_state[0] = False
        if thinking_task: thinking_task.cancel()
        logger.warning("Forbidden in chat=%s: %s", chat_id, e)
        return
    except Exception as e:
        _thinking_state[0] = False
        if thinking_task: thinking_task.cancel()
        logger.exception("Error in handle_message: %s", e)
        if reply_message and isinstance(reply_message, Message):
            try:
                await safe_edit_message(
                    context.bot, chat_id, reply_message.message_id,
                    "Xatolik yuz berdi. Iltimos, birozdan so'ng qayta urinib ko'ring.",
                )
            except Exception:
                pass
        asyncio.create_task(set_premium_reaction(message, "👎"))


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet new group members."""
    if not update.message or not update.message.new_chat_members:
        return
    chat_id = update.effective_chat.id
    await init_bot_info(context.bot)

    for member in update.message.new_chat_members:
        if member.id == BOT_ID:
            text = (
                "<b>Assalomu alaykum! Mening ismim Agentjon.</b> 🤖\n\n"
                "Guruhga muvaffaqiyatli qo'shildim. A'zolar menga murojaat "
                "qilishlari yoki savollar yuborishlari mumkin."
            )
            try:
                await premium_reply(update.message, text)
            except Exception:
                pass
            continue

        prompt = (
            f"Guruhga yangi a'zo qo'shildi. Uning ismi: {member.full_name}. "
            "Agentjon nomidan unga juda samimiy, qisqa (1-2 gap) va o'zbekona tabrik yoz."
        )
        welcome_draft_id = _next_draft_id()
        await send_draft(context.bot, chat_id, "⏳", welcome_draft_id)

        try:
            session = get_chat_session(chat_id, update.message.message_thread_id)
            response_text = ""
            try:
                stream = await session.send_message_stream(current_prompt)
                tool_calls = []

                async for chunk in stream:
                    # Tool calls
                    if chunk.function_calls:
                        tool_calls.extend(chunk.function_calls)
                        continue

                    # Extract text - fastest possible path
                    txt = ""
                    try:
                        for part in chunk.candidates[0].content.parts:
                            if hasattr(part, 'text') and part.text:
                                txt += part.text
                            elif hasattr(part, 'executable_code') and part.executable_code:
                                txt += f"

```python
{part.executable_code.code}
```
"
                            elif hasattr(part, 'code_execution_result') and part.code_execution_result:
                                txt += f"
`Natija: {part.code_execution_result.output}`
"
                    except Exception:
                        try:
                            txt = chunk.text
                        except Exception:
                            pass
                    
                    if not txt:
                        continue

                    response_text += txt

                    # Stop thinking animation on first real text
                    if _thinking_state[0]:
                        _thinking_state[0] = False
                        if thinking_task:
                            thinking_task.cancel()
                            
                        # Ensure draft is clean
                        try:
                            await safe_edit_message(context.bot, chat_id, draft_id, " ")
                        except Exception:
                            pass

                    # Skip special commands instantly
                    s = response_text.lstrip()
                    if s[:8] == "[IGNORE]" or s[:11] == "[DELETE_MSG":
                        continue

                    # Reaction - instant fire-and-forget
                    if "[REACTION:" in txt:
                        rmatch = _RE_REACTION.search(response_text)
                        if rmatch:
                            asyncio.create_task(set_premium_reaction(message, rmatch.group(1).strip()))
                            response_text = response_text.replace(rmatch.group(0), "")

                    # 100% LIVE DRAFT - every chunk, 0 delay
                    new_len = len(response_text)
                    if new_len - last_draft_len >= 30 and response_text.strip():
                        asyncio.create_task(send_draft(context.bot, chat_id, response_text, draft_id, message_thread_id=thread_id))
                        last_draft_len = new_len
            except Exception as e:
                logger.error("Gemini API Error: %s", e)
                if should_respond:
                    await premium_reply(message, f"❌ Uzr, AI xizmatida xatolik yuz berdi: `{e}`")
                break
            finally:
                if _thinking_state[0]:
                    _thinking_state[0] = False
                    if thinking_task:
                        thinking_task.cancel()
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=draft_id)
                    except Exception:
                        pass

            if response_text:
                await send_final(context.bot, chat_id, response_text,
                                reply_to_message_id=update.message.message_id)
        except Exception as e:
            logger.error("Error greeting user: %s", e)
            try:
                await safe_send_message(context.bot, chat_id, f"Xush kelibsiz, {member.full_name}! 😊")
            except Exception:
                pass


async def handle_guest_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages in chats where bot is NOT a member (Guest Mode, Bot API 10.0)."""
    guest_msg = update.guest_message
    if not guest_msg or not guest_msg.guest_query_id:
        return

    await init_bot_info(context.bot)

    # Extract text and user info
    text = guest_msg.text or guest_msg.caption or ""
    user_name = "Foydalanuvchi"
    if hasattr(guest_msg, 'guest_bot_caller_user') and guest_msg.guest_bot_caller_user:
        user_name = guest_msg.guest_bot_caller_user.first_name
    elif guest_msg.from_user:
        user_name = guest_msg.from_user.first_name

    # Remove @bot mention from text
    if BOT_USERNAME:
        text = text.replace(f"@{BOT_USERNAME}", "").strip()

    if not text:
        text = "Salom!"

    logger.info("Guest query from %s: %s", user_name, repr(text)[:80])

    try:
        # Generate AI response - tell AI to use FEWER emojis in guest mode
        prompt = f"[{user_name}] (Guest so'rov - bot a'zo bo'lmagan chatdan. MUHIM: Guest mode da premium emoji ishlamaydi, shuning uchun JUDA KAM emoji ishlat - faqat 1-2 ta oddiy Unicode emoji, matn sifatiga qattiq e'tibor ber!): {text}"
        session = get_chat_session(update.effective_user.id if update.effective_user else 0, None)  # Per-user guest session
        response = await session.send_message(prompt)

        response_text = ""
        if response:
            try:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'text') and part.text:
                        response_text += part.text
                    elif hasattr(part, 'executable_code') and part.executable_code:
                        response_text += f"\n\n```python\n{part.executable_code.code}\n```\n"
                    elif hasattr(part, 'code_execution_result') and part.code_execution_result:
                        response_text += f"\n`Natija: {part.code_execution_result.output}`\n"
            except Exception:
                try:
                    if response.text:
                        response_text = response.text
                except Exception:
                    pass

        # Clean up special tags
        response_text = _RE_GUEST_REACTION.sub('', response_text)
        response_text = response_text.replace('[IGNORE]', '').replace('[DELETE_MSG]', '')
        response_text = response_text.strip()

        if not response_text:
            response_text = "Savol bering, javob beraman!"

        # Guest mode: convert to HTML but WITHOUT premium emoji conversion
        html_text = _guest_markdown_to_html(response_text)

        input_content = InputTextMessageContent(
            message_text=html_text,
            parse_mode='HTML',
        )

        result = InlineQueryResultArticle(
            id="guest_response",
            title="Agentjon javobi",
            input_message_content=input_content,
        )
        await context.bot.answer_guest_query(
            guest_query_id=guest_msg.guest_query_id,
            result=result,
        )
        logger.info("Guest query answered for %s", user_name)

    except Exception as e:
        logger.error("Guest query error: %s", e)
        try:
            result = InlineQueryResultArticle(
                id="guest_error",
                title="Agentjon",
                input_message_content=InputTextMessageContent(
                    message_text=f"🚀 Salom! Men Agentjon (Premium AI yordamchi). Guruhda bo'lmasam ham savollaringizga shunday chiroyli javob bera olaman! Marhamat, so'rayvering!",
                ),
            )
            await context.bot.answer_guest_query(
                guest_query_id=guest_msg.guest_query_id,
                result=result,
            )
        except Exception as e2:
            logger.warning("Guest error fallback also failed: %s", e2)


async def handle_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-approve join requests and send private greeting."""
    request = update.chat_join_request
    if not request:
        return
    chat = request.chat
    user = request.from_user
    logger.info("Join request from %s for %s", user.full_name, chat.title)

    try:
        await request.approve()
    except Exception as e:
        logger.error("Failed to approve: %s", e)
        return

    text = (
        f"<b>Assalomu alaykum, {user.full_name}!</b> 😊\n\n"
        f"Sizning <b>{chat.title}</b> guruhiga/kanaliga qo'shilish so'rovingiz "
        "muvaffaqiyatli tasdiqlandi! 🎉\n\n"
        "Men guruh ma'muri — <b>Agentjon</b>. Menga ixtiyoriy savol yuboring!"
    )
    try:
        await context.bot.send_message(chat_id=user.id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.warning("Failed to send private greeting: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION BUILDER
# ══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — logs unhandled exceptions."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


def build_application() -> Application:
    """Build and configure the Telegram Application with all handlers."""
    from telegram.ext import ChatJoinRequestHandler

    token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")

    async def _post_init(app: Application):
        """Called once after app.initialize() — cache bot info immediately."""
        await init_bot_info(app.bot)

    app = Application.builder().token(token).post_init(_post_init).build()

    # Commands — work from both users AND other bots
    bot_ok = filters.ALL
    app.add_handler(CommandHandler("start", cmd_start, filters=bot_ok))
    app.add_handler(CommandHandler("help", cmd_help, filters=bot_ok))
    app.add_handler(CommandHandler("clear", cmd_clear, filters=bot_ok))
    app.add_handler(CommandHandler("addemoji", cmd_addemoji, filters=bot_ok))
    app.add_handler(CommandHandler("emojicount", cmd_emojicount, filters=bot_ok))
    app.add_handler(CommandHandler("post", cmd_post, filters=bot_ok))

    # Accept messages from ALL users including other bots
    msg_filter = (
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.VIDEO
         | filters.VIDEO_NOTE | filters.AUDIO
         | filters.Document.ALL | filters.LOCATION | filters.CONTACT) & ~filters.COMMAND
    )
    app.add_handler(MessageHandler(msg_filter, handle_message))

    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS,
        welcome_new_member,
    ))

    app.add_handler(ChatJoinRequestHandler(handle_chat_join_request))

    # Guest Mode (Bot API 10.0) — bot responds in chats where it's NOT a member
    # Note: _handle_guest_flow() in handle_message() also checks guest_query_id.
    # This handler catches the dedicated GUEST_MESSAGE update type.
    try:
        app.add_handler(MessageHandler(
            filters.UpdateType.GUEST_MESSAGE,
            handle_guest_message,
        ), group=1)  # group=1 prevents duplicate with handle_message
        logger.info("Guest Mode handler registered")
    except Exception as e:
        logger.warning("Guest Mode not available: %s", e)

    # Global error handler
    app.add_error_handler(error_handler)

    return app


# ══════════════════════════════════════════════════════════════════════════════
#  LOCAL POLLING (for development)
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Run the bot locally using long-polling (development mode)."""
    # Force UTF-8 on Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    from dotenv import load_dotenv
    load_dotenv()

    # Re-read env vars after loading .env
    global TELEGRAM_TOKEN, GEMINI_API_KEY, _genai_client
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    _genai_client = None  # Reset so _get_genai_client() re-reads env

    app = build_application()
    logger.info("🤖 Agentjon is starting in POLLING mode...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
