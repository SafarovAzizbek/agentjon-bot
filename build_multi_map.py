"""
Fetch ALL 11 emoji packs from Telegram and build emoji_multi_map.json.

Maps each emoji to a LIST of all custom_emoji_ids from all packs:
  { "😀": ["id_from_pack1", "id_from_pack2", ...], ... }

Usage:
  .venv\Scripts\python build_multi_map.py
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import asyncio
import json
import os
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

PACK_NAMES = [
    "Fl0rkHDMaydroid",
    "adaptiveqp_by_emsetbot",
    "collegelogos1",
    "ivyleagueschools",
    "TgDuckX",
    "statuspack_uz",
    "TgPremiumIcon",
    "ApplicationEmoji",
    "EmoticonEmoji",
    "ABCEmoji",
    "NewsEmoji",
]


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)

    multi_map: dict[str, list[str]] = {}
    total_stickers = 0
    total_unique_ids = 0

    for pack_name in PACK_NAMES:
        print(f"[PACK] Fetching: {pack_name} ... ", end="", flush=True)
        try:
            sticker_set = await bot.get_sticker_set(pack_name)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        pack_count = 0
        for sticker in sticker_set.stickers:
            if sticker.custom_emoji_id and sticker.emoji:
                emoji = sticker.emoji
                eid = sticker.custom_emoji_id
                if emoji not in multi_map:
                    multi_map[emoji] = []
                # Avoid duplicates within the same emoji
                if eid not in multi_map[emoji]:
                    multi_map[emoji].append(eid)
                    total_unique_ids += 1
                pack_count += 1
                total_stickers += 1

        print(f"OK {pack_count} emojis")

    # Save
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emoji_multi_map.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(multi_map, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"Total stickers processed: {total_stickers}")
    print(f"Unique emojis: {len(multi_map)}")
    print(f"Total unique IDs: {total_unique_ids}")
    print(f"Saved to: {out_path}")

    # Stats: emojis with multiple IDs
    multi_count = sum(1 for ids in multi_map.values() if len(ids) > 1)
    print(f"Emojis with 2+ variants: {multi_count}")

    await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
