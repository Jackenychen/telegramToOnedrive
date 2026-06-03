#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram「收藏」/ 频道 → OneDrive 自动转存

监听你账号的 Saved Messages（收藏）或指定频道里的新视频，下载到本地临时
目录后用 rclone 传到 OneDrive，再删除本地临时文件。之后部署在同一台 VPS
上的 91 项目扫盘，即可在站点显示并播放（OneDrive 走 302 直连，不占 VPS
播放带宽）。

设计要点：
  * 串行处理 + 处理间隔，平滑 CPU/IO 并降低 FloodWait 概率
  * 用 message id 去重，重启不会重复转存
  * rclone moveto = 上传成功后自动删除本地源文件（"传完删本地"）
  * 文件名取自视频 caption，命中 91 项目「[标签] 标题 - 作者」解析规则
  * 相册(media group)整组识别：共享 caption 套到组内所有视频，加 _01/_02 序号

依赖：
  * pip install -r requirements.txt   (telethon)
  * 系统已安装 rclone 且配好 onedrive remote（rclone config）

首次运行需在能交互的终端登录一次（手机号 + 验证码 [+ 两步验证密码]），
成功后生成 <session_name>.session，之后即可交给 systemd 常驻运行。

配置见同目录 config.ini（参考 config.example.ini）。
"""

import asyncio
import configparser
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeFilename

import fast_download


BASE_DIR = Path(__file__).resolve().parent


# --------------------------- 配置加载 ---------------------------

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    path = BASE_DIR / "config.ini"
    if not path.exists():
        sys.exit(f"找不到配置文件 {path}，请复制 config.example.ini 为 config.ini 并填写。")
    cfg.read(path, encoding="utf-8")
    return cfg


CFG = load_config()

API_ID = CFG.getint("telegram", "api_id")
API_HASH = CFG.get("telegram", "api_hash").strip()
SESSION_NAME = CFG.get("telegram", "session_name", fallback="tg_saved").strip()
SOURCE = CFG.get("telegram", "source", fallback="me").strip()
BACKFILL_LIMIT = CFG.getint("telegram", "backfill_limit", fallback=0)

RCLONE = CFG.get("rclone", "rclone_path", fallback="rclone").strip()
REMOTE = CFG.get("rclone", "remote").strip()
DEST_DIR = CFG.get("rclone", "dest_dir", fallback="").strip().strip("/")

TMP_DIR = (BASE_DIR / CFG.get("download", "tmp_dir", fallback="tmp")).resolve()
MIN_INTERVAL = CFG.getfloat("download", "min_interval_sec", fallback=5.0)
MAX_FILESIZE_MB = CFG.getint("download", "max_filesize_mb", fallback=0)
# 并行下载连接数（仅 Premium 建议 >1；免费号并行易触发 FloodWait）
CONNECTIONS = CFG.getint("download", "connections", fallback=1)

STATE_FILE = (BASE_DIR / CFG.get("state", "processed_file", fallback="processed.json")).resolve()


# --------------------------- 日志 ---------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("telethon").setLevel(logging.WARNING)  # 压低 difference 等 INFO 刷屏
log = logging.getLogger("tg2od")


# --------------------------- 去重状态 ---------------------------

def load_processed() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as e:  # 文件损坏不致命，按空集继续
            log.warning("读取去重文件失败，按空集处理：%s", e)
    return set()


def save_processed(done: set) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(done)), encoding="utf-8")
    tmp.replace(STATE_FILE)


PROCESSED = load_processed()


# --------------------------- 文件名 ---------------------------

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')   # 文件系统非法字符
_DASH = re.compile(r"\s+-\s+")               # " - " 会被 91 解析成作者，归一掉


def sanitize(text: str) -> str:
    text = _BAD.sub(" ", text)
    text = _DASH.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    # 按 utf-8 字节截到 <=180，避免本地 ext4 文件名超 255 字节
    return text.encode("utf-8")[:180].decode("utf-8", "ignore").strip()


def guess_ext(msg) -> str:
    doc = msg.document
    if doc:
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                ext = os.path.splitext(attr.file_name)[1].lower()
                if ext:
                    return ext
        mime = (doc.mime_type or "").lower()
        mapping = {
            "video/mp4": ".mp4",
            "video/x-matroska": ".mkv",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
        }
        if mime in mapping:
            return mapping[mime]
    return ".mp4"


def _base_title(caption: str, msg) -> str:
    """标题取 caption 里所有含 # 的行(按出现顺序拼接，空格分隔)；无 # 行则取
    首行；完全无文字则用 TG_日期_消息id。
    """
    title = ""
    if caption:
        lines = caption.splitlines()
        tagged = [ln for ln in lines if "#" in ln]
        title = sanitize(" ".join(tagged)) if tagged else sanitize(lines[0])
    if not title:
        title = f"TG_{msg.date.strftime('%Y%m%d')}_{msg.id}"
    return title


def build_filename(msg) -> str:
    return f"{_base_title((msg.message or '').strip(), msg)}{guess_ext(msg)}"


def is_video(msg) -> bool:
    if getattr(msg, "video", None):
        return True
    doc = msg.document
    return bool(doc and (doc.mime_type or "").lower().startswith("video/"))


def album_names(messages) -> list:
    """同一相册(media group)里的视频 → [(msg, 文件名), ...]。

    标题取相册的 caption（组内第一条带文字的消息）；多条视频按消息 id 升序加
    _01/_02 序号，只有一条视频时不加序号。序号基于完整视频列表计算，断点续传/
    重启也不会错位。组内非视频(图片等)被忽略。
    """
    videos = sorted((m for m in messages if is_video(m)), key=lambda m: m.id)
    if not videos:
        return []
    caption = ""
    for m in sorted(messages, key=lambda m: m.id):
        if (m.message or "").strip():
            caption = m.message.strip()
            break
    base = _base_title(caption, videos[0])
    multi = len(videos) > 1
    width = max(2, len(str(len(videos))))
    out = []
    for i, m in enumerate(videos, 1):
        seq = f"_{i:0{width}d}" if multi else ""
        out.append((m, f"{base}{seq}{guess_ext(m)}"))
    return out


# --------------------------- rclone 上传 ---------------------------

def rclone_moveto(local: Path, name: str) -> None:
    target = f"{REMOTE}:{DEST_DIR}/{name}" if DEST_DIR else f"{REMOTE}:{name}"
    log.info("rclone 上传 → %s", target)
    res = subprocess.run(
        [RCLONE, "moveto", str(local), target, "--no-traverse"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"rclone 失败({res.returncode}): {res.stderr.strip()}")
    # moveto 成功后源文件已被自动删除


def remote_exists(name: str) -> bool:
    """OneDrive 目标目录里是否已有同名文件。

    `rclone lsf <文件路径>`：存在则 exit 0 并打印文件名；不存在报
    "directory not found" 非 0 退出。文件名按精确路径查询，不当作 glob，
    含 [标签] 方括号也安全。
    """
    target = f"{REMOTE}:{DEST_DIR}/{name}" if DEST_DIR else f"{REMOTE}:{name}"
    res = subprocess.run([RCLONE, "lsf", target], capture_output=True, text=True)
    return res.returncode == 0 and bool(res.stdout.strip())


def resolve_unique_name(name: str, msg) -> str:
    """目标已存在时在扩展名前补后缀，避免覆盖 OneDrive 上的同名文件。

    依次尝试 _日期、_消息id，仍冲突再用 _id_序号 兜底。带文字的标题(单条/相册)
    本身不含 id，最容易撞名(如两次都叫"旅行")；无文字命名已带唯一 id，通常直接通过。
    """
    if not remote_exists(name):
        return name
    stem, ext = os.path.splitext(name)
    for suffix in (msg.date.strftime("%Y%m%d"), str(msg.id)):
        cand = f"{stem}_{suffix}{ext}"
        if not remote_exists(cand):
            return cand
    n = 2
    while True:
        cand = f"{stem}_{msg.id}_{n}{ext}"
        if not remote_exists(cand):
            return cand
        n += 1


# --------------------------- 处理与队列 ---------------------------

QUEUE = None  # 在 main() 内创建，避免无 event loop 时构造


async def transfer(client, msg, name: str) -> None:
    if msg.id in PROCESSED:
        return

    size = msg.document.size if msg.document else 0
    if MAX_FILESIZE_MB and size and size > MAX_FILESIZE_MB * 1024 * 1024:
        log.info("跳过超限视频 id=%s size=%.1fMB", msg.id, size / 1048576)
        PROCESSED.add(msg.id)
        save_processed(PROCESSED)
        return

    unique = resolve_unique_name(name, msg)
    if unique != name:
        log.info("目标已存在，改名避免覆盖：%s → %s", name, unique)
    name = unique

    local = TMP_DIR / name
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        log.info("下载 id=%s (%.1fMB, %d连接) → %s", msg.id, (size or 0) / 1048576, CONNECTIONS, name)
        await fast_download.download_smart(client, msg, str(local), connections=CONNECTIONS, log=log)
        rclone_moveto(local, name)
        PROCESSED.add(msg.id)
        save_processed(PROCESSED)
        log.info("完成 id=%s", msg.id)
    except FloodWaitError as e:
        log.warning("FloodWait %ss，稍后重排重试 id=%s", e.seconds, msg.id)
        if local.exists():
            local.unlink(missing_ok=True)
        await asyncio.sleep(e.seconds + 5)
        await QUEUE.put((msg, name))            # 重新入队重试
    except Exception as e:
        log.error("处理 id=%s 失败：%s", msg.id, e)
        if local.exists():
            local.unlink(missing_ok=True)   # 删半成品，留待下次


async def worker(client) -> None:
    while True:
        msg, name = await QUEUE.get()
        try:
            await transfer(client, msg, name)
        finally:
            QUEUE.task_done()
        await asyncio.sleep(MIN_INTERVAL)   # 控速


# --------------------------- 主流程 ---------------------------

async def main() -> None:
    global QUEUE
    QUEUE = asyncio.Queue()

    client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)
    await client.start()                    # 首次交互登录；已有 session 直接复用
    me = await client.get_me()
    log.info("已登录：%s (id=%s, Premium=%s)", me.first_name or me.username, me.id, getattr(me, "premium", False))

    if SOURCE.lower() in ("me", "self", "saved"):
        source = "me"
    elif re.fullmatch(r"-?\d+", SOURCE.strip()):
        # 纯数字(群/频道 ID)转 int，否则 Telethon 会把 -5217033513 误当手机号
        source = int(SOURCE.strip())
    else:
        source = SOURCE
    target = await client.get_entity(source)

    asyncio.create_task(worker(client))

    # 相册(media group)：整组一起处理，caption 套用到组内所有视频并加序号。
    # Album 事件只对 grouped_id 非空的消息触发，且每组只触发一次。
    @client.on(events.Album(chats=target))
    async def _on_album(event):
        for m, name in album_names(event.messages):
            if m.id not in PROCESSED:
                await QUEUE.put((m, name))

    # 单条(非相册)视频。func 过滤掉 grouped 消息，交给上面的 Album，避免重复处理。
    # incoming+outgoing 都开：收藏里的消息是 outgoing，频道里的是 incoming
    @client.on(events.NewMessage(
        chats=target, incoming=True, outgoing=True,
        func=lambda e: getattr(e.message, "grouped_id", None) is None,
    ))
    async def _on_new(event):
        msg = event.message
        if is_video(msg) and msg.id not in PROCESSED:
            await QUEUE.put((msg, build_filename(msg)))

    if BACKFILL_LIMIT > 0:
        log.info("回扫最近 %d 条补漏…", BACKFILL_LIMIT)
        # 先全部取回再按 grouped_id 归组，相册才能正确编号
        # （注意：limit 若刚好截断某相册，该组编号/数量会不准；backfill 默认关闭）
        recent = [m async for m in client.iter_messages(target, limit=BACKFILL_LIMIT)]
        groups: dict = {}
        for m in recent:
            gid = getattr(m, "grouped_id", None)
            if gid is not None:
                groups.setdefault(gid, []).append(m)
            elif is_video(m) and m.id not in PROCESSED:
                await QUEUE.put((m, build_filename(m)))
        for members in groups.values():
            for m, name in album_names(members):
                if m.id not in PROCESSED:
                    await QUEUE.put((m, name))

    log.info("开始监听 source=%s，等待新视频…（Ctrl+C 退出）", SOURCE)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("退出")
