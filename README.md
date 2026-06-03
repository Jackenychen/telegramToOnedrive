# Telegram 收藏 → OneDrive 自动转存

把你在 Telegram「收藏」(Saved Messages) 或某个频道里转发的视频，自动下载、
上传到 OneDrive，并删除本地临时文件。配合同机部署的 **91 视频站项目**，
视频会被扫盘入库，在站点里直接显示和播放。

这是一个**独立的旁路小服务**，不改动 91 项目本身的任何代码。

---

## 工作原理

```
手机转发视频到「收藏」
        │
        ▼
本服务监听到新消息（Telethon，用户账号 MTProto）
        │  只挑视频，按 message id 去重，串行处理
        ▼
下载到本地临时目录 tmp/
        │
        ▼
rclone moveto 上传到 OneDrive（上传成功即删除本地源文件）
        │
        ▼
91 项目扫盘 → 解析文件名 → 入库 → 站点显示
        │
        ▼
播放时 OneDrive 走 302 直连微软 CDN，不占 VPS 带宽
```

关键设计：

- **串行 + 间隔处理**：平滑 CPU/IO，降低触发 Telegram 限流 (FloodWait) 的概率
- **message id 去重**：记录在 `processed.json`，服务重启不会重复转存
- **`rclone moveto`**：上传成功后自动删除本地源文件，天然满足"传完删本地"
- **文件名取自视频 caption**：取文字里所有含 `#` 标签的行拼接为标题（无 `#` 行则用首行，
  完全无文字用 `TG_日期_消息id`），命中 91 项目 `[标签] 标题 - 作者.ext` 的解析规则，
  让站内标题更好看（标签由 91 的词库自动匹配文件名）
- **同名不覆盖**：上传前检查 OneDrive，目标已存在则自动补 `_日期`（仍冲突再补 `_消息id`）后缀，避免相同标题的视频互相覆盖
- **并行多连接下载（Premium 提速）**：默认单连接受限于单管道吞吐（实测约 `0.5~1 MiB/s`）。开 Telegram Premium 后把 `connections` 设为 >1，对同一文件开多条独立连接并行拉取、带宽叠加（8 连接实测大文件约 `12 MiB/s`、中小文件约 `5~7 MiB/s`），下载结果经 sha256 校验与单连接字节一致。积压排队致 `file_reference` 过期时，会自动重取消息换新令牌重试并行（不退回单连接）；其余异常自动回退单连接，`<5MB` 小文件因建连开销不划算直接走单连接。免费账号不建议并行（更易触发 FloodWait）

---

## 目录文件

| 文件 | 说明 |
|------|------|
| `tg_to_onedrive.py` | 主程序 |
| `fast_download.py` | 并行多连接下载器（主程序调用；任何异常自动回退单连接） |
| `bench_dl.py` | 下载测速 / 正确性校验工具（调 `connections` 时用；服务不依赖） |
| `scan_backlog.py` | 扫描来源群、统计未处理视频数并估算 `backfill_limit`（服务不依赖） |
| `config.example.ini` | 配置模板，复制为 `config.ini` 后填写 |
| `requirements.txt` | Python 依赖（telethon + cryptg 加速 AES） |
| `tg-onedrive.service` | systemd 常驻服务模板 |
| `config.ini` | **你的实际配置（含密钥，需自建，勿泄露）** |
| `*.session` | **Telegram 登录态（首次登录后生成，勿泄露）** |
| `processed.json` | 去重记录（运行时自动生成） |
| `tmp/` | 下载临时目录（运行时自动创建，文件传完即删） |

---

## 前置要求

- 目标 VPS 已部署 91 项目，并已挂好一个 **OneDrive** 网盘（状态 `ok`）
- VPS 建议至少 **1 核 / 1GB 内存**（本服务常驻仅几十 MB，峰值主要是 91 侧 ffmpeg 生成预览）
- Python 3.8+
- 一个 Telegram 账号（用真实手机号的老号，别用接码虚拟号）

---

## 部署步骤

### 1. 拷贝到目标 VPS

在打包机器上：

```bash
cd /path/to && tar czf tg.tar.gz --exclude=__pycache__ --exclude='*.session' --exclude=config.ini tg-saved-to-onedrive
scp tg.tar.gz 用户@目标VPS:/opt/
```

目标 VPS 上：

```bash
cd /opt && tar xzf tg.tar.gz && cd tg-saved-to-onedrive
```

### 2. 安装依赖

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
curl https://rclone.org/install.sh | sudo bash      # 安装 rclone
```

### 3. 配置 rclone 的 OneDrive remote

> ⚠️ 必须使用 **91 项目里挂的同一个 OneDrive 账号**，传上去的视频才会被 91 扫到。

```bash
rclone config
#  n) New remote
#  name> onedrive
#  Storage> onedrive
#  按提示完成 OAuth 授权
rclone lsd onedrive:          # 验证能列出网盘目录
```

无头 VPS（没有浏览器）授权：`rclone config` 会提示你在**本地有浏览器的电脑**上运行
`rclone authorize "onedrive"`，然后把输出的一长串 token 粘回 VPS。

### 4. 申请 Telegram API

登录 <https://my.telegram.org> → **API development tools** → 创建应用，得到 `api_id` 和 `api_hash`。

### 5. 填写配置

```bash
cp config.example.ini config.ini
nano config.ini
chmod 600 config.ini
```

至少要填：`api_id`、`api_hash`、`remote`、`dest_dir`（详见下方配置项说明）。

### 6. 首次登录（必须交互式运行一次）

```bash
venv/bin/python tg_to_onedrive.py
# 依次输入：+86你的手机号 → 收到的验证码 → 两步验证密码(如有)
```

看到 `开始监听 source=...` 后，用手机转发一个视频到「收藏」测试：

- 日志应出现 `下载 → rclone 上传 → 完成`
- 去 OneDrive 对应目录确认文件已上传
- 确认无误后按 `Ctrl+C` 退出

成功后会在目录下生成 `tg_saved.session`，之后即可交给 systemd 无人值守运行。

### 7. 配置 systemd 常驻

```bash
nano tg-onedrive.service        # 确认 WorkingDirectory / ExecStart 路径正确
sudo cp tg-onedrive.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-onedrive
```

### 8. 让 91 入库

等 91 的夜间扫盘（默认凌晨 1 点），或在后台点「扫描所有网盘」手动触发，
视频即出现在站点，点开走 302 直连播放。

---

## 配置项说明

`config.ini`：

| Section | Key | 说明 |
|---------|-----|------|
| `telegram` | `api_id` / `api_hash` | my.telegram.org 申请 |
| | `session_name` | 登录态文件名，生成 `<名>.session`，默认 `tg_saved` |
| | `source` | `me` = 监听「收藏」；或频道 `@用户名` / `-100xxxxxxxxxx` |
| | `backfill_limit` | 启动时回扫最近 N 条消息、按 `processed.json` 去重后重新入队（不重下已完成的）；`0` = 只处理今后的新消息。**积压很多没下完时设为 ≥ 群消息总数**即可在重启后自动补下（条数可用 `scan_backlog.py` 估算）；清空积压后可改回 `0` |
| `rclone` | `rclone_path` | rclone 可执行路径，在 PATH 里就填 `rclone` |
| | `remote` | rclone 配的 OneDrive remote 名 |
| | `dest_dir` | OneDrive 上的目标目录，须在 91 OneDrive drive 的 `rootID`(默认 `root`) 之下；留空 = 网盘根目录 |
| `download` | `tmp_dir` | 本地临时目录（相对脚本目录） |
| | `min_interval_sec` | 两次处理间最小间隔秒数（控速），默认 `5` |
| | `max_filesize_mb` | 跳过超过该大小的视频，`0` = 不限（Premium 单文件上限 4GB，免费号 2GB） |
| | `connections` | 并行下载连接数。`1` = 单连接（缺省回退值，模板预设 `8`）。**仅 Premium 建议 >1**：`8` 实测大文件约 12MiB/s、中小文件 5~7（单连接仅 0.5~1）；`<5MB` 自动走单连接；免费号并行易 FloodWait，撞到就调小 |
| `state` | `processed_file` | 去重记录文件，默认 `processed.json` |

---

## 下载提速（Premium 并行下载）

Telegram 对**单条连接**的下载吞吐有上限（本机实测约 `0.5~1 MiB/s`，开不开会员都一样——会员放宽的是总带宽天花板，不是单管道）。提速的正确做法是**多连接并行**：

1. 给脚本登录用的账号开 **Telegram Premium**（并行下载本质是账号级特性；免费号并行更易撞 FloodWait，不建议）。无需新建 bot——bot 不能开会员。
2. 安装 `cryptg`（`requirements.txt` 已含）：C 实现的 AES，多连接同时解密时不会卡在纯 Python 上。
3. `config.ini` 里设 `connections = 8`（或 4~12），`systemctl restart tg-onedrive` 生效。

实战参考：一个 469MB 的视频，单连接需十几分钟，8 连接约 **40 秒**拉完（≈12 MiB/s）。文件越大并行收益越明显；`<5MB` 小文件因建连开销不划算，自动走单连接。

启动日志的 `已登录：xxx (id=..., Premium=True)` 可确认会员已被 API 认出；下载行会显示 `下载 id=xxx (xx.xMB, 8连接)`。想自己测速/验证可跑 `venv/bin/python bench_dl.py`（需先 `systemctl stop tg-onedrive` 释放 session）。

> 并行只在 Premium 下安全好用。若日志频繁出现 `FloodWait`，把 `connections` 调小再重启。

---

## 日常运维

```bash
journalctl -u tg-onedrive -f          # 实时日志
sudo systemctl restart tg-onedrive    # 重启
sudo systemctl stop tg-onedrive       # 停止
sudo systemctl status tg-onedrive     # 状态
```

**更新脚本**：替换 `tg_to_onedrive.py` 后 `sudo systemctl restart tg-onedrive`。
`processed.json` 和 `*.session` 保留即可，不要删（删了会重新处理 / 需重新登录）。

---

## 安全注意

- `config.ini`（含 `api_hash`）和 `*.session`（等于登录态）**绝不能泄露或上传到 git**，
  建议 `chmod 600`。
- 用真实手机号的老账号；本服务只做**下载**、不发消息不加群，是风险最低的用法，
  但仍建议保持低频、遇 FloodWait 让它自动等待。

---

## 故障排查

| 现象 | 排查 |
|------|------|
| `找不到配置文件 config.ini` | 执行 `cp config.example.ini config.ini` 并填写 |
| 启动卡住没反应 | 首次必须**交互式**登录生成 session；systemd 启动前请先手动跑一次 |
| `rclone 失败` | 检查 `remote` 名是否对、授权是否过期、`rclone lsd onedrive:` 是否正常 |
| 视频上传了但站点没出现 | 确认 `dest_dir` 在 91 OneDrive drive 的 `rootID` 之下；已触发扫盘；扩展名是 `.mp4/.mkv/.mov/.webm` |
| 日志报 `FloodWait` | 正常限流保护，会自动等待后重排；可调大 `min_interval_sec`；若开了并行可调小 `connections` |
| 下载很慢（约 0.5 MiB/s） | 单连接上限所致；开 Telegram Premium 后把 `connections` 设为 >1（见「下载提速」），并确认 `cryptg` 已装 |
| 积压很多没下 / 重启后没续上 | 把 `backfill_limit` 调大（≥ 待补条数，用 `scan_backlog.py` 估）后重启，自动重新入队未处理的；已完成的去重跳过 |
| 某视频下载失败 | 来源可能开了「禁止保存内容」(content protection)，这类无法下载，少见 |
| 收藏里混入非视频 | 服务只处理视频消息，其余自动忽略；想更干净可改用专用频道（设 `source`） |
