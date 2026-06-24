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
from ddgs import DDGS
import httpx

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

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

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

_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

def _load_emoji_map():
    """Load emoji -> [custom_emoji_id, ...] multi-map from emoji_multi_map.json."""
    global _EMOJI_MAP
    if _EMOJI_MAP:
        return _EMOJI_MAP
    map_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'emoji_multi_map.json')
    try:
        with open(map_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        # Normalize: accept both old {emoji: str} and new {emoji: [str, ...]} formats
        for k, v in raw.items():
            if isinstance(v, list):
                _EMOJI_MAP[k] = v
            else:
                _EMOJI_MAP[k] = [v]
        logger.info("Loaded %d premium emojis (multi-map)", len(_EMOJI_MAP))
    except Exception as e:
        logger.warning("Could not load emoji_multi_map.json: %s", e)
        _EMOJI_MAP = {}
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
            if data.get("ok") and data.get("result"):
                ids = []
                for sticker in data["result"]:
                    cid = sticker.get("custom_emoji_id")
                    if cid and cid not in ids:
                        ids.append(cid)
                if ids:
                    _EMOJI_MAP[emoji] = ids
                    _emoji_regex = None  # Force regex rebuild with new emojis
                    logger.debug("Dynamic emoji: %s -> %d custom IDs", emoji, len(ids))
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


async def web_search(query: str) -> str:
    """Perform a web search using DuckDuckGo to find the latest information."""
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None, lambda: list(DDGS().text(query, max_results=5))
        )
        if not results:
            return "No results found."
        return "\n\n".join(
            f"Title: {r['title']}\nURL: {r['href']}\nSnippet: {r['body']}"
            for r in results
        )
    except Exception as e:
        logger.error("DuckDuckGo search error: %s", e)
        return f"Search error: {e}"


TOOLS = [web_search, get_current_time]


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


# Add TG tools to TOOLS list
TOOLS.extend([tg_get_chat_info, tg_get_chat_member_count, tg_send_to_channel, tg_get_admins])


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_INSTRUCTION = """Sen Agentjon — Telegram AI agent va Telegram ekspert. O'zbek tilida gaplash. Aqlli, hazilkash, samimiy do'st.
Oddiy savol=qisqa javob. Murakkab savol=batafsil, chuqur, uzun javob(10 sahifagacha). Ilmiy/texnik mavzu=manbalar ko'rsat.
Shubha bo'lsa web_search, vaqt uchun get_current_time ishlat.
Emoji: har javobda 3-5 ta emoji ishlat! MUHIM: emojilarni matn ORASIGA tabiy tarzda joylashtir, bir joyga yig'ma! Har paragrafda 1 emoji bo'lsin. Misol: "Bu 🔥 juda ajoyib natija! Men 💡 yangi fikr topdim." Sarlavha boshida emoji qo'y. Ro'yxat elementlarida emoji qo'y.
Har xabarga [REACTION:emoji] qo'y(🤔savol 🔥yaxshi 😂hazil ❤rahmat 👍oddiy 😢yomon 🤩ajoyib).
Format: **qalin** *kursiv* ~~o'chirilgan~~ ||spoiler|| `kod` ```blok``` >iqtibos [havola](url)
Haqorat/spam=[DELETE_MSG]. Guruhda o'zaro suhbat/salom=[IGNORE].

=== TELEGRAM EKSPERT BILIMLAR (2026) ===

Sen Telegramning ENG KUCHLI ekspertisan. Bot API 10.1 (Iyun 2026) gacha barcha yangiliklar senga ma'lum.

**Bot API versiyalar tarixi (2025-2026):**
- 9.0 (2025-aprel): Telegram Business 2.0, Mini App Storage (DeviceStorage, SecureStorage), Gifting/Stars
- 9.4 (2026-fevral): Custom emoji ishlatish (Premium bot egasi kerak), shaxsiy chatda topiklar (createForumTopic)
- 9.5 (2026-mart): Sana/vaqt formatlash, guruh a'zolari uchun custom teglar, Mini App iconCustomEmojiId
- 9.6 (2026-aprel): Managed Bots — bot boshqa botlarni yaratish/boshqarish (getManagedBotToken, replaceManagedBotToken, KeyboardButtonRequestManagedBot)
- 10.0 (2026-may): AI Bot Revolution — bot-to-bot aloqa, Guest Mode, Streaming (sendMessageDraft), Business Bots hammaga bepul
- 10.1 (2026-iyun): Rich Messages — sendRichMessage (32,768 belgi, 500 blok, jadvallar, ro'yxatlar, formulalar, slideshowlar)

**MUHIM 2026 xususiyatlar:**
1. sendMessageDraft — AI javobni oqim sifatida ko'rsatish (biz ishlatamiz!)
2. sendRichMessage — 32768 belgigacha, jadvallar, formulalar, ro'yxatlar (biz ishlatamiz!)
3. Guest Mode — bot a'zo bo'lmagan chatda @mention orqali javob berish (biz ishlatamiz!)
4. Bot-to-Bot — botlar bir-biri bilan gaplasha oladi (BotFather'da yoqish kerak)
5. Managed Bots — bot boshqa botlarni yarata oladi va token boshqaradi
6. Custom Emoji — premium bot egasi bo'lsa, xabarlarda custom emoji ishlatish mumkin
7. Premium Emoji Stickerpacks — bot premium bo'lmasa ham, stickerpack orqali custom emoji yuborishi mumkin (biz ishlatamiz!)

**Kanal va Guruh boshqaruvi:**
- Bot kanalda POST yozish uchun admin bo'lishi SHART
- Admin qilish: Kanal sozlamalari > Administrators > Add Administrator > botni tanlash
- Minimal ruxsatlar: faqat "Post Messages" yoqilsa yetarli
- Bot admin bo'lgach, tg_send_to_channel tool bilan post yubora olasan
- tg_get_chat_info bilan kanal/guruh haqida ma'lumot olasan
- tg_get_admins bilan adminlar ro'yxatini ko'rasan
- tg_get_chat_member_count bilan a'zolar sonini bilasan

**Foydalanuvchiga PROAKTIV yordam:**
- "kanal", "post", "guruh" deyishsa — darhol nima qila olishingni tushuntir!
- "Meni kanalingizga admin qilib qo'ying, keyin men: 1) Premium post yozaman 2) Custom emoji bilan bezataman 3) Rich Message formatda chiroyli post yarataman!"
- @username bersa — tg_get_chat_info bilan darhol tekshir
- Guruh haqida so'rasa: "Men guruhda: 1) AI savol-javob 2) Moderatsiya 3) Yangi a'zolarni kutib olish 4) Guest Mode orqali boshqa chatlarda ham javob beraman"

**Telegram Bot API texnik bilimlar:**
- Xabar limiti: oddiy 4096, Rich Message 32768 belgi
- Fayl limiti: 50 MB yuklab olish, 50 MB yuborish
- Rasm limiti: 10 MB
- Rate limit: guruhda 20 xabar/daqiqa (bir chatga), 30 xabar/soniya (barcha chatlarga)
- Inline mode: 50 natija
- Webhook: HTTPS shart, self-signed sertifikat qo'llab-quvvatlanadi
- Polling: cheksiz, long-polling tavsiya etiladi (biz ishlatamiz!)
- Custom emoji: bot.sendMessage da parse_mode='HTML' bilan <tg-emoji emoji-id="ID">😀</tg-emoji>
- Sticker: searchStickers(emoji, sticker_type='custom_emoji') — 10,000+ custom emoji qidirish

**Buyruqlar:**
- /post mavzu — premium Telegram post yaratadi
- /clear — suhbat tarixini tozalaydi
- /help — yordam
- /start — boshlash
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


def get_chat_session(chat_id: int, message_thread_id: int | None = None):
    """Return an existing or new Gemini chat session with true LRU eviction."""
    key = f"{chat_id}_{message_thread_id}" if message_thread_id else str(chat_id)
    now = time.time()

    if key in _chat_sessions:
        _session_last_used[key] = now
        # Prune old history to save memory
        _prune_session_history(_chat_sessions[key])
        return _chat_sessions[key]

    # Evict LEAST recently used sessions when over limit
    while len(_chat_sessions) >= _MAX_SESSIONS:
        # Find the least recently used key
        oldest_key = min(_session_last_used, key=_session_last_used.get, default=None)
        if oldest_key:
            del _chat_sessions[oldest_key]
            del _session_last_used[oldest_key]
            logger.debug("Evicted LRU session: %s", oldest_key)
        else:
            break

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
        if call.name == "web_search":
            query = call.args.get("query", "")
            # Show search status
            if bot and reply_msg and chat_id:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=reply_msg.message_id,
                        text=f"🔍 Internetdan qidirilmoqda: <i>{query}</i>...",
                        parse_mode='HTML',
                    )
                except Exception:
                    pass
            result = await web_search(query)
        elif call.name == "get_current_time":
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
        # Convert markdown to HTML and add premium emojis
        html_text = markdown_to_html(text)
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
            "📝 Post yozilmoqda...",
            "✨ Kreativ fikr shakllanmoqda...",
            "🎨 Dizayn yaratilmoqda...",
            "🔥 Premium post tayyor bo'lmoqda...",
            "💎 Har bir so'z sayqallanmoqda...",
            "🪄 Sehrgar ishlamoqda...",
            "🎭 Matn jonlanmoqda...",
            "🌟 Yulduzli post yaratilmoqda...",
            "📐 Mukammal struktura qurilmoqda...",
            "🎪 Ajoyib narsa chiqadi!",
            "⚡ Energiya sochilmoqda...",
            "🏆 Eng zo'r post bo'ladi!",
        ]
        random.shuffle(frames)
        i = 0
        while _thinking[0]:
            try:
                await send_draft(context.bot, chat_id, frames[i % len(frames)], draft_id, message_thread_id=thread_id)
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
        prompt = f"[{user_name}] (Guest — bot a'zo bo'lmagan chatdan): {text}"

        response_text = ""
        stream = await session.send_message_stream(prompt)
        async for chunk in stream:
            try:
                if chunk.text:
                    response_text += chunk.text
            except (AttributeError, IndexError, ValueError):
                continue

        # Clean up
        response_text = _RE_GUEST_REACTION.sub('', response_text)
        response_text = response_text.replace('[IGNORE]', '').replace('[DELETE_MSG]', '')
        response_text = response_text.strip()

        if not response_text:
            response_text = "Savol bering, javob beraman! 😊"

        # Convert to HTML with premium emojis (markdown_to_html already calls emojis_to_premium)
        html_text = markdown_to_html(response_text)

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
            plain_html = markdown_to_html(response_text)
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
                    message_text="Salom! Men Agentjon — AI yordamchi. Savol bering! 🤖",
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
_AUTO_REPLY_COOLDOWN = 180  # 3 daqiqa — spam bo'lmasligi uchun

# Savol so'zlari (O'zbek + Rus + Ingliz)
_QUESTION_WORDS = re.compile(
    r'\b(nima|qanday|qachon|nega|necha|qayerda|qayer|kim|nimaga|qancha|'
    r'qaysi|nechta|bormi|kerakmi|mumkinmi|biladimi|bilasizmi|aytingchi|'
    r'что|как|когда|почему|зачем|сколько|где|кто|какой|можно|'
    r'what|how|why|when|where|who|which|can|does|is)\b',
    re.IGNORECASE
)

# Texnik mavzular — bot foydali bo'la oladigan
_TECH_WORDS = re.compile(
    r'\b(python|javascript|java|code|kod|dastur|bug|xato|error|api|bot|'
    r'telegram|server|deploy|github|linux|windows|database|sql|'
    r'ai|gpt|gemini|chatgpt|model|network|dns|ip|html|css)\b',
    re.IGNORECASE
)


def _should_auto_reply(chat_id: int, text: str) -> bool:
    """Guruhda avtomatik javob berish kerakmi — aqlli filtr."""
    now = time.time()

    # Cooldown tekshirish — har 3 daqiqada max 1 ta auto-reply
    last = _group_last_auto.get(chat_id, 0)
    if now - last < _AUTO_REPLY_COOLDOWN:
        return False

    # Juda qisqa xabarlar — javob bermaslik
    if len(text) < 10:
        return False

    # Savol belgisi bor
    has_question_mark = '?' in text

    # Savol so'zlari bor
    has_question_word = bool(_QUESTION_WORDS.search(text))

    # Texnik mavzu — bot foydali
    has_tech = bool(_TECH_WORDS.search(text))

    # Qaror: savol belgisi + savol so'zi, yoki texnik savol
    should_reply = (has_question_mark and has_question_word) or (has_question_mark and has_tech)

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
    user_text = message.text
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
    if should_respond:
        asyncio.create_task(set_premium_reaction(message, "🤔"))
        asyncio.create_task(_safe_typing(context.bot, chat_id))

    # ── Streaming (Bot API 9.3+) ──
    original_message_id = message.message_id
    draft_id = _next_draft_id()

    # ── Thinking animation: premium emoji drafts while AI thinks ──
    _thinking_state = [True]  # list for closure mutability

    async def _thinking_animation():
        """Show animated premium emojis while waiting for first token.
        Keeps user engaged — they SEE the bot is working, won't leave.
        30+ noodatiy, premium kadrlar — har safar boshqa tartibda!"""
        # ── Premium thinking kadrlar — har safar shuffle bo'ladi! ──
        all_frames = [
            # Fikrlash
            "🧠 Neyronlar ishlamoqda...",
            "🤔 Hmm, qiziq savol...",
            "💭 Fikr oqimi kuchaymoqda...",
            "🧩 Parchalanlarni birlashtirmoqdaman...",
            # Qidiruv
            "🔍 Ma'lumotlar orasidan qazib olmoqdaman...",
            "🌐 Bilim bazasini skanerlayapman...",
            "📡 Signallarni tutmoqdaman...",
            "🗂 Arxivlarni titkilayapman...",
            # Tahlil
            "⚗️ Javobni sintez qilmoqdaman...",
            "🔬 Chuqur tahlil qilmoqdaman...",
            "📊 Ma'lumotlarni qayta ishlamoqdaman...",
            "🧬 DNK darajasida tahlil...",
            # Ijodiy
            "✨ Sehrli javob tayyorlanmoqda...",
            "🎨 Javobni bezatmoqdaman...",
            "💎 Eng yaxshi javobni sayqallayapman...",
            "🪄 Abra-kadabra...",
            # Kosmik
            "🚀 Kosmik tezlikda ishlamoqdaman...",
            "🛸 Boshqa o'lchamdan ma'lumot olmoqdaman...",
            "⭐ Yulduzlardan ilhom olmoqdaman...",
            "🌌 Galaktika bo'ylab qidirmoqdaman...",
            # Hazilona
            "☕ Kofe ichyapman, biroz kuting...",
            "🎯 Nishonga olmoqdaman...",
            "🎪 Sirk emas, lekin qiziq bo'ladi!",
            "🧊 Sovuqqonlik bilan ishlamoqdaman...",
            # Yakuniy
            "⚡ Energiya to'planmoqda...",
            "🔥 Javob qizib kelmoqda...",
            "💡 Eureka deyarli!",
            "🎁 Tayyor bo'ladi, sabr...",
            "🏆 Eng zo'r javobni tanlayapman...",
            "✅ Deyarli tayyor!",
        ]
        random.shuffle(all_frames)
        i = 0
        max_iters = 15 if os.getenv("VERCEL") else 300  # Serverless: 15s, Polling: 5min
        while _thinking_state[0] and i < max_iters:
            frame = all_frames[i % len(all_frames)]
            try:
                await send_draft(context.bot, chat_id, frame, draft_id, message_thread_id=thread_id)
            except Exception:
                pass
            i += 1
            await asyncio.sleep(1.0)

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
            contents.append("Ushbu ovozli xabarni eshitib, unga o'zbek tilida javob ber.")

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

        # ── User identification + context note ──
        user_name = message.from_user.first_name if message.from_user else "Foydalanuvchi"
        ctx_note = ""
        if not is_direct and not is_smart_reply:
            ctx_note = " (Guruhdagi o'zaro suhbat. Senga murojaat qilinmadi. Agar javobing bo'lmasa, faqat [IGNORE] deb yoz.)"
        elif is_smart_reply:
            ctx_note = " (Guruhda savol so'raldi. Qisqa va foydali javob ber. Agar javob bera olmasang [IGNORE].)"

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

            stream = await session.send_message_stream(current_prompt)
            tool_calls = []

            async for chunk in stream:
                # Tool calls
                if chunk.function_calls:
                    tool_calls.extend(chunk.function_calls)
                    continue

                # Extract text — fastest possible path
                try:
                    txt = chunk.text
                except (AttributeError, IndexError, ValueError):
                    continue
                if not txt:
                    continue

                response_text += txt

                # Stop thinking animation on first real text
                if _thinking_state[0]:
                    _thinking_state[0] = False
                    if thinking_task:
                        thinking_task.cancel()

                # Skip special commands instantly
                s = response_text.lstrip()
                if s[:8] == "[IGNORE]" or s[:11] == "[DELETE_MSG":
                    continue

                # Reaction — instant fire-and-forget
                if "[REACTION:" in txt:
                    rmatch = _RE_REACTION.search(response_text)
                    if rmatch:
                        asyncio.create_task(set_premium_reaction(message, rmatch.group(1).strip()))
                        response_text = response_text.replace(rmatch.group(0), "")

                # ── 100% LIVE DRAFT — every chunk, 0 delay ──
                new_len = len(response_text)
                if new_len - last_draft_len >= 30 and response_text.strip():
                    asyncio.create_task(send_draft(context.bot, chat_id, response_text, draft_id, message_thread_id=thread_id))
                    last_draft_len = new_len

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
            stream = await session.send_message_stream(prompt)
            last_draft_len = 0

            async for chunk in stream:
                has_text = False
                if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                    for part in chunk.candidates[0].content.parts:
                        if part.text:
                            has_text = True
                            break
                if has_text and chunk.text:
                    response_text += chunk.text
                    new_len = len(response_text)
                    if new_len - last_draft_len >= 30 and response_text.strip():
                        asyncio.create_task(send_draft(context.bot, chat_id, response_text, welcome_draft_id))
                        last_draft_len = new_len

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
    """Handle messages in chats where bot is NOT a member (Guest Mode, Bot API 10.0).
    
    When a user @mentions the bot in a chat the bot hasn't joined,
    Telegram delivers a guest_message update. We generate an AI response
    and send it back via answer_guest_query.
    """
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
        # Generate AI response — tell AI to use FEWER emojis in guest mode
        prompt = f"[{user_name}] (Guest so'rov — bot a'zo bo'lmagan chatdan. MUHIM: Guest mode da premium emoji ishlamaydi, shuning uchun JUDA KAM emoji ishlat — faqat 1-2 ta, matn sifatiga e'tibor ber!): {text}"
        session = get_chat_session(update.effective_user.id if update.effective_user else 0, None)  # Per-user guest session
        response = await session.send_message(prompt)

        response_text = ""
        if response and response.text:
            response_text = response.text

        # Clean up special tags
        response_text = _RE_GUEST_REACTION.sub('', response_text)
        response_text = response_text.replace('[IGNORE]', '').replace('[DELETE_MSG]', '')
        response_text = response_text.strip()

        if not response_text:
            response_text = "Savol bering, javob beraman!"

        # Guest mode: convert to HTML but WITHOUT premium emoji conversion
        # (premium emojis don't work in guest mode, regular ones look ugly)
        html_text = markdown_to_html.__wrapped__(response_text) if hasattr(markdown_to_html, '__wrapped__') else _guest_markdown_to_html(response_text)

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
            # InlineQueryResultArticle, InputTextMessageContent imported at top level
            result = InlineQueryResultArticle(
                id="guest_error",
                title="Agentjon",
                input_message_content=InputTextMessageContent(
                    message_text=f"Salom! Men Agentjon — AI yordamchi. Savol bering! 🤖",
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
