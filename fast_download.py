#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行多连接下载（FastTelethon 思路，适配本机 Telethon 1.43.2）。

Telethon 默认 download_media 走单条连接，受限于单管道吞吐（实测本机 ~0.5MiB/s）。
本模块对同一文件开多条独立连接，各拉一段 512KiB 块拼回本地文件，合计带宽叠加。
仅在 Telegram Premium 下建议使用——免费号并行更易触发 FloodWait。

实现要点：
  * 开 N 条**独立** MTProtoSender 连接（不用 _borrow_exported_sender：它每个 DC 只
    缓存一条，会被多路复用挤在一条 TCP 上，突破不了单管道上限）。
  * 建连接必须**串行**：_create_exported_sender 会改写共享的 client._init_request，
    且导出鉴权是一次性的，并发建会互相覆盖 → "authorization is invalid"。
    所以导出鉴权只做一次（第一条连接），其余连接复用其已授权的 auth_key——既避开
    竞态又省往返。文件在主 DC 时直接复用主连接 auth_key。下载本身仍并行。
  * 共享 offset 计数器做 work-stealing：抢 offset 的动作在 await 前同步完成，
    单线程 asyncio 下无竞态；os.pwrite 按绝对偏移写，无需文件锁。
  * file_reference 过期（积压相册排队太久，入队时的令牌失效）：重新拉取消息换新
    令牌后整轮重试并行（pwrite 按偏移幂等，重下覆盖即可）。原版 download_media 也是
    这么刷新的，这里补齐，避免积压大文件退回单连接慢速。
  * 任何意外（CDN 重定向、建连失败、字节数对不上、刷新后仍过期）抛 _NeedFallback，
    由 download_smart 回退到原版 download_media，保证正确性不退化。
"""
import asyncio
import os

from telethon import utils
from telethon.errors import (
    FileReferenceExpiredError,
    FilerefUpgradeNeededError,
    FloodWaitError,
)
from telethon.network import MTProtoSender
from telethon.tl import functions, types

REQUEST_SIZE = 512 * 1024          # 单次请求块大小，须为 4096 整数倍且 <=512KiB
ALIGN = 4096
MIN_PARALLEL_SIZE = 5 * 1024 * 1024  # 小于此值不并行：建连/导出鉴权开销不划算


class _NeedFallback(Exception):
    """并行路径无法安全处理，需回退到 download_media。"""


async def _connect_with_key(client, dc, auth_key) -> MTProtoSender:
    """用已授权的 auth_key 新建并连接一条 sender（不再导出/导入鉴权）。"""
    sender = MTProtoSender(auth_key, loggers=client._log)
    await sender.connect(client._connection(
        dc.ip_address, dc.port, dc.id,
        loggers=client._log, proxy=client._proxy, local_addr=client._local_addr,
    ))
    return sender


async def _make_senders(client, dc_id, count) -> list:
    """串行创建 count 条到 dc_id 的独立连接。导出鉴权至多一次，其余复用同一 auth_key。"""
    home = dc_id is None or dc_id == client.session.dc_id
    dc = await client._get_dc(dc_id if dc_id else client.session.dc_id)
    senders: list[MTProtoSender] = []
    try:
        if home:
            base_key = client._sender.auth_key      # 主 DC：复用主连接 auth_key
        else:
            first = await client._create_exported_sender(dc_id)  # 仅此一次：导出+导入
            senders.append(first)
            base_key = first.auth_key
        while len(senders) < count:
            senders.append(await _connect_with_key(client, dc, base_key))
    except Exception:
        for s in senders:
            try:
                await s.disconnect()
            except Exception:
                pass
        raise
    return senders


async def _run_parallel(client, dc_id, location, size, dest_path, connections, request_size) -> int:
    """用多连接并行把文件拉到 dest_path，返回写入字节数。
    file_reference 过期会从这里向上抛 FileReferenceExpiredError，交给 fast_download 刷新重试。"""
    senders = await _make_senders(client, dc_id, connections)
    next_offset = 0
    written = 0
    fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)

    async def worker(sender):
        nonlocal next_offset, written
        while True:
            offset = next_offset            # 抢块：取+自增在 await 之前完成，无竞态
            if offset >= size:
                return
            next_offset += request_size
            req = functions.upload.GetFileRequest(location, offset=offset, limit=request_size)
            result = await client._call(sender, req)
            if isinstance(result, types.upload.FileCdnRedirect):
                raise _NeedFallback("命中 CDN 重定向")
            data = result.bytes
            if data:
                os.pwrite(fd, data, offset)
                written += len(data)

    tasks = [asyncio.ensure_future(worker(s)) for s in senders]
    try:
        os.ftruncate(fd, size)              # 预分配，便于各 worker 按偏移稀疏写
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)   # 等其余 worker 收尾
        raise
    finally:
        os.close(fd)
        for s in senders:
            try:
                await s.disconnect()
            except Exception:
                pass
    return written


async def _refresh_location(client, msg):
    """重新拉取消息，返回新的 (dc_id, location)；失败抛 _NeedFallback。"""
    try:
        chat = await msg.get_input_chat()
        fresh = await client.get_messages(chat, ids=msg.id)
    except Exception as e:
        raise _NeedFallback(f"刷新消息失败 {type(e).__name__}: {e}")
    doc = getattr(fresh, "document", None) if fresh else None
    if doc is None:
        raise _NeedFallback("刷新后消息无 document（可能已被删/编辑）")
    return utils.get_input_location(doc)


async def fast_download(client, msg, dest_path, connections: int = 4,
                        request_size: int = REQUEST_SIZE) -> int:
    """并行下载 msg 的视频到 dest_path，返回写入字节数。失败抛 _NeedFallback。"""
    media = getattr(msg, "document", None)
    if media is None:
        raise _NeedFallback("非 document 媒体")

    dc_id, location = utils.get_input_location(media)
    size = int(getattr(media, "size", 0) or 0)
    if size <= 0:
        raise _NeedFallback("未知文件大小")
    if size < MIN_PARALLEL_SIZE:
        raise _NeedFallback(f"文件较小({size / 1048576:.1f}MB)，单连接即可")

    request_size -= request_size % ALIGN
    request_size = max(ALIGN, min(request_size, 512 * 1024))
    connections = max(1, min(int(connections), 16))

    written = 0
    for attempt in range(2):
        try:
            written = await _run_parallel(
                client, dc_id, location, size, dest_path, connections, request_size)
            break
        except (FileReferenceExpiredError, FilerefUpgradeNeededError):
            if attempt == 1:
                raise _NeedFallback("file_reference 刷新后仍过期")
            dc_id, location = await _refresh_location(client, msg)   # 换新令牌后整轮重试
        except FloodWaitError:
            raise                           # 交给上层 transfer() 等待并重排
        except _NeedFallback:
            raise
        except Exception as e:
            raise _NeedFallback(f"建连/下载失败 {type(e).__name__}: {e}")

    if written != size:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise _NeedFallback(f"字节数不符 写入{written}≠预期{size}")
    return written


async def download_smart(client, msg, dest_path, connections: int = 4, log=None) -> None:
    """优先并行下载；任何异常回退到原版 download_media，保证不退化。"""
    if connections and connections > 1:
        try:
            await fast_download(client, msg, dest_path, connections=connections)
            return
        except FloodWaitError:
            raise                           # 交给上层 transfer() 等待并重排，回退单连接没意义
        except _NeedFallback as e:
            if log:
                log.info("并行下载回退到单连接：%s", e)
        except Exception as e:
            if log:
                log.warning("并行下载异常，回退到单连接：%s", e)
            try:
                if os.path.exists(dest_path):
                    os.remove(dest_path)
            except OSError:
                pass
    await client.download_media(msg, file=str(dest_path))
