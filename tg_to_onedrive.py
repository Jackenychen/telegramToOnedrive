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
  * 启动时按 processed 最大 id 增量补漏（停机期间漏传）；冷启动可用 backfill_limit
  * rclone moveto = 上传成功后自动删除本地源文件（"传完删本地"）
  * 文件名取自视频 caption，命中 91 项目「[标签] 标题 - 作者」解析规则
  * 相册(media group)整组识别：共享 caption 套到组内所有视频/音频，加 _01/_02 序号
  * 音频（语音/音频文件）上传到 OneDrive 根下独立目录（config audio_dest_dir）

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
import sys
from pathlib import Path
from typing import NamedTuple

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
AUDIO_DEST_DIR = CFG.get("rclone", "audio_dest_dir", fallback="").strip().strip("/")

TMP_DIR = (BASE_DIR / CFG.get("download", "tmp_dir", fallback="tmp")).resolve()
MIN_INTERVAL = CFG.getfloat("download", "min_interval_sec", fallback=5.0)
MAX_FILESIZE_MB = CFG.getint("download", "max_filesize_mb", fallback=0)
# 并行下载连接数（仅 Premium 建议 >1；免费号并行易触发 FloodWait）
CONNECTIONS = CFG.getint("download", "connections", fallback=1)
SENDER_POOL_IDLE_SEC = CFG.getint("download", "sender_pool_idle_sec", fallback=60)
WARM_REMOTE_CACHE = CFG.getboolean("download", "warm_remote_cache", fallback=False)

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


def _ext_from_document(msg, mime_map: dict, default: str) -> str:
    doc = msg.document
    if doc:
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                ext = os.path.splitext(attr.file_name)[1].lower()
                if ext:
                    return ext
        mime = (doc.mime_type or "").lower()
        if mime in mime_map:
            return mime_map[mime]
    return default


def guess_ext(msg) -> str:
    return _ext_from_document(msg, {
        "video/mp4": ".mp4",
        "video/x-matroska": ".mkv",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }, ".mp4")


def guess_audio_ext(msg) -> str:
    if getattr(msg, "voice", None):
        return ".ogg"
    return _ext_from_document(msg, {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/ogg": ".ogg",
        "audio/x-vorbis+ogg": ".ogg",
        "audio/flac": ".flac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/aac": ".aac",
        "audio/x-m4a": ".m4a",
    }, ".mp3")


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


def build_audio_filename(msg) -> str:
    return f"{_base_title((msg.message or '').strip(), msg)}{guess_audio_ext(msg)}"


def is_video(msg) -> bool:
    if getattr(msg, "video", None):
        return True
    doc = msg.document
    return bool(doc and (doc.mime_type or "").lower().startswith("video/"))


def is_audio(msg) -> bool:
    if is_video(msg):
        return False
    if getattr(msg, "voice", None) or getattr(msg, "audio", None):
        return True
    doc = msg.document
    return bool(doc and (doc.mime_type or "").lower().startswith("audio/"))


def _album_media_names(messages, *, media_filter, ext_fn) -> list:
    """相册内指定类型媒体 → [(msg, 文件名), ...]。"""
    items = sorted((m for m in messages if media_filter(m)), key=lambda m: m.id)
    if not items:
        return []
    caption = ""
    for m in sorted(messages, key=lambda m: m.id):
        if (m.message or "").strip():
            caption = m.message.strip()
            break
    base = _base_title(caption, items[0])
    multi = len(items) > 1
    width = max(2, len(str(len(items))))
    out = []
    for i, m in enumerate(items, 1):
        seq = f"_{i:0{width}d}" if multi else ""
        out.append((m, f"{base}{seq}{ext_fn(m)}"))
    return out


def album_names(messages) -> list:
    """同一相册里的视频。"""
    return _album_media_names(messages, media_filter=is_video, ext_fn=guess_ext)


def album_audio_names(messages) -> list:
    """同一相册里的音频/语音。"""
    return _album_media_names(messages, media_filter=is_audio, ext_fn=guess_audio_ext)


# --------------------------- rclone（异步子进程 + 远端文件名缓存）---------------------------

_REMOTE_EXISTS: set[str] = set()   # 键：dest_dir/name（空 dest 时为 /name）


def _remote_key(dest_dir: str, name: str) -> str:
    return f"{dest_dir}/{name}" if dest_dir else name


def _rclone_target(dest_dir: str, name: str) -> str:
    return f"{REMOTE}:{dest_dir}/{name}" if dest_dir else f"{REMOTE}:{name}"


def _rclone_dir_target(dest_dir: str) -> str:
    return f"{REMOTE}:{dest_dir}/" if dest_dir else f"{REMOTE}:"


def _note_remote_name(dest_dir: str, name: str) -> None:
    _REMOTE_EXISTS.add(_remote_key(dest_dir, name))


async def _rclone_run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        RCLONE, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    err = (stderr or b"").decode(errors="replace").strip()
    out = (stdout or b"").decode(errors="replace").strip()
    return proc.returncode or 0, err or out


async def remote_exists_async(dest_dir: str, name: str) -> bool:
    """OneDrive 是否已有同名文件。先查内存缓存，未命中再 rclone lsf。"""
    key = _remote_key(dest_dir, name)
    if key in _REMOTE_EXISTS:
        return True
    code, out = await _rclone_run("lsf", _rclone_target(dest_dir, name))
    exists = code == 0 and bool(out.strip())
    if exists:
        _note_remote_name(dest_dir, name)
    return exists


async def rclone_moveto_async(local: Path, name: str, dest_dir: str) -> None:
    target = _rclone_target(dest_dir, name)
    log.info("rclone 上传 → %s", target)
    code, err = await _rclone_run("moveto", str(local), target, "--no-traverse")
    if code != 0:
        raise RuntimeError(f"rclone 失败({code}): {err}")
    _note_remote_name(dest_dir, name)


async def resolve_unique_name_async(name: str, msg, dest_dir: str) -> str:
    """目标已存在时在扩展名前补后缀，避免覆盖 OneDrive 上的同名文件。"""
    if not await remote_exists_async(dest_dir, name):
        return name
    stem, ext = os.path.splitext(name)
    for suffix in (msg.date.strftime("%Y%m%d"), str(msg.id)):
        cand = f"{stem}_{suffix}{ext}"
        if not await remote_exists_async(dest_dir, cand):
            return cand
    n = 2
    while True:
        cand = f"{stem}_{msg.id}_{n}{ext}"
        if not await remote_exists_async(dest_dir, cand):
            return cand
        n += 1


async def _warm_dir_cache(dest_dir: str) -> int:
    code, out = await _rclone_run("lsf", _rclone_dir_target(dest_dir), "--files-only")
    if code != 0:
        log.warning("预热远端缓存失败 [%s]：%s", dest_dir or "(root)", out)
        return 0
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for n in names:
        _REMOTE_EXISTS.add(_remote_key(dest_dir, n))
    return len(names)


async def warm_remote_cache_async() -> None:
    if not WARM_REMOTE_CACHE:
        return
    n = await _warm_dir_cache(DEST_DIR)
    if DEST_DIR:
        log.info("预热远端缓存 [%s]：%d 个文件名", DEST_DIR, n)
    if AUDIO_DEST_DIR:
        na = await _warm_dir_cache(AUDIO_DEST_DIR)
        log.info("预热远端缓存 [%s]：%d 个文件名", AUDIO_DEST_DIR, na)


# --------------------------- 处理与队列 ---------------------------

class Job(NamedTuple):
    msg: object
    name: str
    dest_dir: str
    kind: str          # "video" | "audio"


QUEUE = None  # 在 main() 内创建，避免无 event loop 时构造


async def transfer(client, job: Job) -> None:
    msg, name, dest_dir, kind = job
    if msg.id in PROCESSED:
        return

    size = msg.document.size if msg.document else 0
    if MAX_FILESIZE_MB and size and size > MAX_FILESIZE_MB * 1024 * 1024:
        log.info("跳过超限%s id=%s size=%.1fMB", kind, msg.id, size / 1048576)
        PROCESSED.add(msg.id)
        save_processed(PROCESSED)
        return

    unique = await resolve_unique_name_async(name, msg, dest_dir)
    if unique != name:
        log.info("目标已存在，改名避免覆盖：%s → %s", name, unique)
    name = unique

    # 音频/语音与视频分目录，避免同名冲突
    local = TMP_DIR / (f"audio_{name}" if kind == "audio" else name)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    # 音频 >=5MB 与视频相同走多连接；小语音/短音频仍单连接（与 fast_download 阈值一致）
    if kind == "audio":
        dl_conns = CONNECTIONS if (size or 0) >= fast_download.MIN_PARALLEL_SIZE else 1
    else:
        dl_conns = CONNECTIONS

    try:
        log.info(
            "下载%s id=%s (%.1fMB, %d连接) → %s",
            kind, msg.id, (size or 0) / 1048576, dl_conns, name,
        )
        await fast_download.download_smart(
            client, msg, str(local), connections=dl_conns, log=log,
        )
        await rclone_moveto_async(local, name, dest_dir)
        PROCESSED.add(msg.id)
        save_processed(PROCESSED)
        log.info("完成%s id=%s", kind, msg.id)
    except FloodWaitError as e:
        log.warning("FloodWait %ss，稍后重排重试 id=%s", e.seconds, msg.id)
        if local.exists():
            local.unlink(missing_ok=True)
        await asyncio.sleep(e.seconds + 5)
        await QUEUE.put(job)                    # 重新入队重试
    except Exception as e:
        log.error("处理 id=%s 失败：%s", msg.id, e)
        if local.exists():
            local.unlink(missing_ok=True)   # 删半成品，留待下次


async def worker(client) -> None:
    while True:
        job = await QUEUE.get()
        try:
            await transfer(client, job)
        finally:
            QUEUE.task_done()
        await asyncio.sleep(MIN_INTERVAL)   # 控速


async def _enqueue_job(msg, name: str, dest_dir: str, kind: str) -> None:
    await QUEUE.put(Job(msg, name, dest_dir, kind))


# --------------------------- 启动补漏 ---------------------------

async def enqueue_backlog(client, target, *, limit=None, min_id=None) -> tuple[int, int]:
    """历史消息补漏入队。返回 (视频条数, 音频条数)。"""
    kwargs = {}
    if limit is not None:
        kwargs["limit"] = limit
    if min_id is not None:
        kwargs["min_id"] = min_id
    recent = [m async for m in client.iter_messages(target, **kwargs)]
    groups: dict = {}
    qv = qa = 0
    for m in recent:
        gid = getattr(m, "grouped_id", None)
        if gid is not None:
            groups.setdefault(gid, []).append(m)
        elif m.id not in PROCESSED:
            if is_video(m):
                await _enqueue_job(m, build_filename(m), DEST_DIR, "video")
                qv += 1
            elif AUDIO_DEST_DIR and is_audio(m):
                await _enqueue_job(m, build_audio_filename(m), AUDIO_DEST_DIR, "audio")
                qa += 1
    for members in groups.values():
        for m, name in album_names(members):
            if m.id not in PROCESSED:
                await _enqueue_job(m, name, DEST_DIR, "video")
                qv += 1
        if AUDIO_DEST_DIR:
            for m, name in album_audio_names(members):
                if m.id not in PROCESSED:
                    await _enqueue_job(m, name, AUDIO_DEST_DIR, "audio")
                    qa += 1
    return qv, qa


# --------------------------- 主流程 ---------------------------

async def main() -> None:
    global QUEUE
    QUEUE = asyncio.Queue()

    fast_download.configure_pool(SENDER_POOL_IDLE_SEC)

    client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)
    try:
        await client.start()                    # 首次交互登录；已有 session 直接复用
        me = await client.get_me()
        log.info("已登录：%s (id=%s, Premium=%s)", me.first_name or me.username, me.id, getattr(me, "premium", False))
        await warm_remote_cache_async()

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
                    await _enqueue_job(m, name, DEST_DIR, "video")
            if AUDIO_DEST_DIR:
                for m, name in album_audio_names(event.messages):
                    if m.id not in PROCESSED:
                        await _enqueue_job(m, name, AUDIO_DEST_DIR, "audio")

        # 单条(非相册)。func 过滤 grouped，交给 Album。
        @client.on(events.NewMessage(
            chats=target, incoming=True, outgoing=True,
            func=lambda e: getattr(e.message, "grouped_id", None) is None,
        ))
        async def _on_new(event):
            msg = event.message
            if msg.id in PROCESSED:
                return
            if is_video(msg):
                await _enqueue_job(msg, build_filename(msg), DEST_DIR, "video")
            elif AUDIO_DEST_DIR and is_audio(msg):
                await _enqueue_job(msg, build_audio_filename(msg), AUDIO_DEST_DIR, "audio")

        # 有 processed 记录时：始终按最大 id 增量补漏（覆盖停机期间漏传，无需 backfill_limit>0）
        if PROCESSED:
            watermark = max(PROCESSED)
            log.info("增量补漏：拉取 id > %d 的未处理消息…", watermark)
            qv, qa = await enqueue_backlog(client, target, min_id=watermark)
            log.info("增量补漏完成，入队 视频 %d 条、音频 %d 条", qv, qa)
        elif BACKFILL_LIMIT > 0:
            log.info("冷启动回扫最近 %d 条…", BACKFILL_LIMIT)
            qv, qa = await enqueue_backlog(client, target, limit=BACKFILL_LIMIT)
            log.info("冷启动回扫完成，入队 视频 %d 条、音频 %d 条", qv, qa)
        else:
            log.info("processed 为空且 backfill_limit=0，不拉历史，仅监听新消息")

        audio_hint = f"，音频→{AUDIO_DEST_DIR}/" if AUDIO_DEST_DIR else "，未配置 audio_dest_dir（不处理音频）"
        log.info("开始监听 source=%s，视频→%s/%s%s（Ctrl+C 退出）",
                 SOURCE, REMOTE, DEST_DIR or "(root)", audio_hint)
        await client.run_until_disconnected()
    finally:
        await fast_download.close_pool()
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("退出")
