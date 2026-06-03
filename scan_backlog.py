#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描来源群，统计还有多少视频未处理（不在 processed.json 里），
以及最旧的未处理视频在「最近第几条消息」处——用来设 backfill_limit。
运行前先 systemctl stop tg-onedrive 释放 session。
"""
import asyncio
import configparser
import json
import re
from pathlib import Path

from telethon import TelegramClient

BASE = Path(__file__).resolve().parent
cfg = configparser.ConfigParser()
cfg.read(BASE / "config.ini", encoding="utf-8")
API_ID = cfg.getint("telegram", "api_id")
API_HASH = cfg.get("telegram", "api_hash").strip()
SESSION = cfg.get("telegram", "session_name", fallback="tg_saved").strip()
SRC = cfg.get("telegram", "source", fallback="me").strip()
STATE = BASE / cfg.get("state", "processed_file", fallback="processed.json")
processed = set(json.loads(STATE.read_text(encoding="utf-8"))) if STATE.exists() else set()

CAP = 6000  # 最多回看这么多条消息


def is_video(m):
    if getattr(m, "video", None):
        return True
    d = m.document
    return bool(d and (d.mime_type or "").lower().startswith("video/"))


async def main():
    if SRC.lower() in ("me", "self", "saved"):
        src = "me"
    elif re.fullmatch(r"-?\d+", SRC):
        src = int(SRC)
    else:
        src = SRC
    c = TelegramClient(str(BASE / SESSION), API_ID, API_HASH)
    await c.start()
    ent = await c.get_entity(src)

    scanned = vids = todo = todo_bytes = 0
    todo_min = todo_max = None
    depth = 0          # 最旧未处理视频所在的「最近第几条」
    i = 0
    async for m in c.iter_messages(ent, limit=CAP):
        i += 1
        scanned += 1
        if is_video(m):
            vids += 1
            if m.id not in processed:
                todo += 1
                depth = i
                todo_bytes += int(getattr(m.document, "size", 0) or 0) if m.document else 0
                todo_min = m.id if todo_min is None else min(todo_min, m.id)
                todo_max = m.id if todo_max is None else max(todo_max, m.id)

    print(f"扫描最近 {scanned} 条消息：视频 {vids} 个，已处理 {vids - todo}，未处理 {todo}")
    if todo:
        sugg = ((depth // 500) + 1) * 500
        print(f"未处理视频 id {todo_min}~{todo_max}，共约 {todo_bytes / 1073741824:.1f} GB")
        print(f"最旧未处理视频在「最近第 {depth} 条」→ backfill_limit 建议设 {sugg}")
        if depth >= CAP:
            print(f"⚠ 已扫到上限 {CAP}，可能还有更旧的未处理视频，需调大 CAP 再扫")
    else:
        print("没有未处理视频，无需回扫")
    await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
