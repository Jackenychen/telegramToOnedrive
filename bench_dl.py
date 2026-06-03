#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载测速 + 正确性校验：单连接 vs 并行多连接。

挑一个 15~25MB 的视频，分别用 download_media（单连接）与 fast_download（并行）
下到 /tmp 计时，并用 sha256 校验并行结果与单连接字节完全一致。
运行前请先 `systemctl stop tg-onedrive` 释放 session。
"""
import asyncio
import configparser
import hashlib
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, utils

import fast_download

BASE = Path(__file__).resolve().parent
cfg = configparser.ConfigParser()
cfg.read(BASE / "config.ini", encoding="utf-8")
API_ID = cfg.getint("telegram", "api_id")
API_HASH = cfg.get("telegram", "api_hash").strip()
SESSION = cfg.get("telegram", "session_name", fallback="tg_saved").strip()
SRC = cfg.get("telegram", "source", fallback="me").strip()


def is_video(m):
    if getattr(m, "video", None):
        return True
    d = m.document
    return bool(d and (d.mime_type or "").lower().startswith("video/"))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


async def timed(coro_factory, dst, sz):
    t0 = time.monotonic()
    await coro_factory()
    dt = time.monotonic() - t0
    actual = os.path.getsize(dst)
    return dt, actual, sz / 1048576 / dt


async def main():
    if SRC.lower() in ("me", "self", "saved"):
        src = "me"
    elif re.fullmatch(r"-?\d+", SRC):
        src = int(SRC)
    else:
        src = SRC

    c = TelegramClient(str(BASE / SESSION), API_ID, API_HASH)
    await c.start()
    me = await c.get_me()
    home_dc = c.session.dc_id
    print(f"登录: {me.first_name} Premium={getattr(me, 'premium', False)} 主DC={home_dc}")
    ent = await c.get_entity(src)

    # 收集候选并打印各自 DC，优先挑一个“非主 DC”的视频来复现并行建连路径
    candidates = []
    async for m in c.iter_messages(ent, limit=120):
        if is_video(m) and m.document and 10 * 1024 * 1024 <= m.document.size <= 45 * 1024 * 1024:
            dc_id, _ = utils.get_input_location(m.document)
            candidates.append((m, dc_id))
            if len(candidates) >= 8:
                break
    if not candidates:
        print("没找到 10~45MB 的视频做基准")
        await c.disconnect()
        return
    print("候选: " + ", ".join(f"id={m.id}/{m.document.size/1048576:.0f}MB/DC{dc}" for m, dc in candidates))

    target = next((m for m, dc in candidates if dc != home_dc), None)
    if target is None:
        target = candidates[0][0]
        print(f"⚠ 全部在主 DC，无法复现非主 DC 路径，仍用 id={target.id} 测")
    else:
        tdc = utils.get_input_location(target.document)[0]
        print(f"选中非主 DC 文件 id={target.id} DC={tdc}（这正是之前报错的路径）")
    sz = target.document.size
    print(f"基准文件 id={target.id} size={sz / 1048576:.1f}MB\n")

    ref = f"/tmp/bench_single_{target.id}.bin"
    dt, act, spd = await timed(lambda: c.download_media(target, file=ref), ref, sz)
    ref_hash = sha(ref)
    print(f"[单连接 download_media] {dt:.1f}s  {spd:.2f} MiB/s  size={act}")

    for n in (4, 8):
        dst = f"/tmp/bench_p{n}_{target.id}.bin"
        dt, act, spd = await timed(
            lambda: fast_download.fast_download(c, target, dst, connections=n), dst, sz)
        ok = (act == sz) and (sha(dst) == ref_hash)
        print(f"[并行 {n} 连接]            {dt:.1f}s  {spd:.2f} MiB/s  size={act}  校验={'一致✓' if ok else '不符✗'}")
        os.remove(dst)

    os.remove(ref)
    await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
