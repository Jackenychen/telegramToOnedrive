# Telegram 收藏 → OneDrive 自动转存

Telegram「收藏」或指定群/频道里的视频 → 下载到 `tmp/` → `rclone moveto` 到 OneDrive → 删本地。配合同机 **91 视频站**扫盘入库、302 播放。独立旁路服务，不改 91 代码。

## 流程

```
转发视频 → Telethon 监听（仅视频、message id 去重、串行）
        → 下载（Premium 可多连接）→ rclone 上传 → 91 扫盘
```

## 要点

- **去重**：`processed.json` 记 message id；**重启**时按 `max(processed)` **增量补漏**（停机期间 id 更大的消息）
- **冷启动**：仅当 `processed` 为空且 `backfill_limit > 0` 时回扫最近 N 条
- **上传**：`moveto` 成功删本地；同名用 `rclone lsf` 检测，自动加 `_日期` / `_消息id` 后缀（内存缓存减少重复 lsf）
- **下载**：Premium 可设 `connections > 8`；连接池复用并行连接，空闲 60s 关闭；`<5MB` 或异常回退单连接

## 文件

| 文件 | 说明 |
|------|------|
| `tg_to_onedrive.py` | 主程序 |
| `fast_download.py` | 并行下载 + 连接池 |
| `config.example.ini` | 配置模板 → 复制为 `config.ini` |
| `scan_backlog.py` / `bench_dl.py` | 估积压条数 / 测速（可选） |
| `tg-onedrive.service` | systemd 模板 |
| `P1-优化方案.md` | 性能优化设计说明 |

勿提交 git：`config.ini`、`*.session`、`processed.json`

## 快速部署

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
# 安装 rclone 并 rclone config 配好 onedrive（须与 91 同一 OneDrive）
cp config.example.ini config.ini   # 填 api_id、api_hash、remote、dest_dir、source
venv/bin/python tg_to_onedrive.py  # 首次交互登录
sudo cp tg-onedrive.service /etc/systemd/system/ && systemctl enable --now tg-onedrive
```

## 配置（`config.ini`）

| Key | 说明 |
|-----|------|
| `source` | `me` = 收藏；或群/频道 `@名` / 数字 id |
| `backfill_limit` | **仅** `processed` 为空时回扫最近 N 条；有 processed 时靠增量补漏，通常 `0` |
| `connections` | Premium 并行数，建议 `8`；撞 FloodWait 调小 |
| `sender_pool_idle_sec` | 并行连接池空闲 N 秒后关闭，`60`；`0` = 常驻 |
| `warm_remote_cache` | 启动 `rclone lsf` 预热文件名缓存，默认 `false` |
| `min_interval_sec` | 两条处理间隔，默认 `3` |
| `dest_dir` / `remote` | OneDrive 路径，须在 91 盘 `rootID` 下 |

## 运维

```bash
journalctl -u tg-onedrive -f
systemctl restart tg-onedrive
```

更新代码后重启；**勿删** `processed.json`、`*.session`。积压用 `venv/bin/python scan_backlog.py` 估 `backfill_limit`。

## 故障简表

| 现象 | 处理 |
|------|------|
| 停机后漏传 | 有 `processed.json` 会自动增量补漏；确认服务已重启 |
| 删了 `processed.json` | 临时加大 `backfill_limit` 冷启动扫一遍 |
| `FloodWait` | 自动等待；调大 `min_interval_sec` 或减小 `connections` |
| 下载 ~0.5 MiB/s | Premium + `connections > 1` + 已装 `cryptg` |
| `rclone 失败` | `rclone lsd onedrive:` 检查授权 |

详细部署见 **`部署使用说明.md`**。