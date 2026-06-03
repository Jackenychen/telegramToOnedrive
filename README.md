# Telegram 收藏 → OneDrive 自动转存

Telegram「收藏」或指定群/频道里的**视频、音频/语音** → 下载到 `tmp/` → `rclone moveto` 到 OneDrive → 删本地。视频配合同机 **91 视频站**扫盘入库；音频存网盘独立目录。独立旁路服务，不改 91 代码。

## 流程

```
转发到监听源
  ├─ 视频 → onedrive:<dest_dir>/     （默认 Telegram/，91 扫盘）
  └─ 音频/语音 → onedrive:<audio_dest_dir>/  （默认 音频/，网盘根下）
        ↓
Telethon 监听 → message id 去重 → 串行下载上传
```

## 要点

- **去重**：`processed.json` 记 message id（视频/音频共用）；重启按 `max(processed)` **增量补漏**
- **冷启动**：仅 `processed` 为空且 `backfill_limit > 0` 时回扫最近 N 条
- **上传**：`moveto` 成功删本地；同名 `lsf` + 自动 `_日期` / `_消息id`；按目录分缓存
- **下载**：Premium 设 `connections`（建议 8）；**≥5MB** 的视频与大音频走多连接，更小走单连接；连接池空闲 60s 关闭

## 文件

| 文件 | 说明 |
|------|------|
| `tg_to_onedrive.py` | 主程序 |
| `fast_download.py` | 并行下载 + 连接池 |
| `config.example.ini` | 配置模板 |
| `scan_backlog.py` / `bench_dl.py` | 估积压 / 测速（可选） |
| `迁移清单.md` | 打包迁移步骤 |
| `部署使用说明.md` | 运维说明 |

勿提交 git：`config.ini`、`*.session`、`processed.json`

## 配置（`config.ini`）

| Key | 说明 |
|-----|------|
| `source` | `me` 或群/频道 id |
| `dest_dir` | 视频目录（如 `Telegram`，须在 91 盘 `rootID` 下） |
| `audio_dest_dir` | 音频目录（如 `音频`，网盘**根下**）；**留空 = 不处理音频** |
| `backfill_limit` | 仅 `processed` 为空时回扫 N 条；平时 `0` + 增量补漏 |
| `connections` | Premium 并行数；≥5MB 音视频共用 |
| `sender_pool_idle_sec` | 连接池空闲关闭秒数，默认 `60` |
| `min_interval_sec` | 处理间隔，默认 `3` |

## 运维

```bash
journalctl -u tg-onedrive -f
systemctl restart tg-onedrive
```

日志：`下载video` / `下载audio` → `rclone 上传` → `完成video` / `完成audio`。

## 故障简表

| 现象 | 处理 |
|------|------|
| 停机后漏传 | 确认有 `processed.json` 并已重启（增量补漏） |
| 音频没上传 | 检查 `audio_dest_dir` 是否已配置 |
| `FloodWait` | 调大 `min_interval_sec` 或减小 `connections` |
| `rclone 失败` | `rclone lsd onedrive:` |

详见 **`部署使用说明.md`**、**`迁移清单.md`**。