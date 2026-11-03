# data 目录说明

这里存放 wy-checkin 的全部运行期数据。默认位置：`<项目根目录>/data`，
可以用环境变量 `NCM_DATA_DIR` 换到别处。

> `data/` 里的真实数据被 `.gitignore` 忽略；只有 `*.example.json` 模板和本文件会提交。
> 所有 `*.example.json` 都只是示例/模板，**程序不会读取它们**。

## 文件一览

| 文件 | 谁维护 | 是否提交 | 说明 |
| --- | --- | --- | --- |
| `config.json` | 人工 | 忽略 | 主配置：API 地址、歌单、配额、通知等 |
| `config.example.json` | 项目自带 | 提交 | `config.json` 模板 |
| `account.json` | 人工 / 环境变量 | 忽略 | 验证码登录的账号列表 |
| `account.example.json` | 项目自带 | 提交 | `account.json` 模板 |
| `ncm_cookie.json` | `login.py` 自动写 | 忽略 | 登录态，每个账号一条 |
| `ncm_cookie.example.json` | 项目自带 | 提交 | `ncm_cookie.json` 模板 |
| `ncm_state.json` | `run.py` 自动写 | 忽略 | 每日目标、断点、签到标记 |
| `ncm_state.example.json` | 项目自带 | 提交 | `ncm_state.json` 模板 |
| `ncm_run.lock` | 程序运行时生成 | 忽略 | 单实例锁，内容是当前进程 pid |
| `code.jpg` | `login.py` 可选生成 | 忽略 | 扫码登录二维码图片 |

真实数据文件只有在对应操作发生后才会出现：登录后才有 `ncm_cookie.json`，跑任务后才有
`ncm_state.json`、`ncm_run.lock`；没装 `qrcode[pil]` 时不会生成 `code.jpg`。

## config.json

主配置。`shared/common.py` 的 `DEFAULTS` 定义了全部字段与默认值，文件里没写的键一律用默认值。
完整模板见 `config.example.json`。

```json
{
  "api": "http://127.0.0.1:3000",
  "sckey": "",
  "user_sl_id": ["1234567890", "9876543210"]
}
```

### 必填

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `api` | string | 网易云 API 服务地址，必须以 `http://` 或 `https://` 开头 |
| `user_sl_id` | string / array | 歌单 ID；支持单个字符串、逗号分隔字符串或数组，去重保序 |

### 可选（括号里是默认值）

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `sckey` | `""` | Server酱 SendKey，用于推送通知（可选） |
| `daily_target_base` | `330` | 每日目标听歌数基准 |
| `daily_target_jitter` | `15` | 每日目标抖动；按日期确定性随机，同一天恒定 |
| `slots_per_day` | `2` | 每天分几轮跑 |
| `catch_up` | `false` | `true` = 一次运行把当天剩余全部跑完；`false` = 按轮次跑 |
| `run_ratio_min` / `run_ratio_max` | `0.35` / `0.65` | 每轮在「扣除后续轮次最低额度后的剩余量」里随机抽取的比例区间 |
| `min_songs_per_run` / `max_songs_per_run` | `100` / `240` | 每轮最少 / 最多播放首数 |
| `scrobble_seconds` | `70` | 每首上报的播放秒数；`0` = 按歌曲真实时长 |
| `scrobble_jitter_seconds` | `8` | 上报秒数的 ± 随机抖动 |
| `sleep_range` | `[30, 60]` | 每首之间随机等待秒数 |
| `sleep_long_pause_prob` | `0.12` | 插入一次长停顿的概率 |
| `sleep_long_pause_range` | `[120, 300]` | 长停顿秒数范围 |
| `slot_overlap_seconds` | `0` | 计入时间预算的重叠秒数 |
| `max_run_minutes` | `0` | 单次运行时间上限（分钟）；`0` = 按歌曲数与间隔自动计算 |
| `hard_exit_buffer_minutes` | `10` | 看门狗强制退出前的缓冲（分钟） |
| `qr_timeout` / `qr_poll_interval` | `180` / `1` | 扫码登录超时秒数 / 轮询间隔秒数 |
| `code_timeout` / `code_poll_interval` | `180` / `1` | 等验证码超时 / 读取间隔 |
| `max_workers` | `0` | 多账号并发数；`0` = 按账号数 |
| `request_timeout` | `15` | 单次 HTTP 请求超时（秒） |
| `notify_timeout` | `15` | 通知（青龙 notify / Server酱）超时（秒） |
| `scrobble_path` | `"/scrobble/v1"` | 打卡接口路径 |

## account.json

`login.py --type=code` 用的验证码登录账号列表，数组：

```json
[
  {"phone": "<手机号>", "code": ""}
]
```

- 程序每秒读一次对应手机号的 `code`，把收到的 6 位验证码填进去就会继续登录。
- 登录成功后会把该手机号的 `code` 清空，避免重复使用。
- 青龙里也可以不建这个文件，改用环境变量 `account.json` / `ACCOUNT_JSON` / `NCM_ACCOUNT`
  （值是 JSON 字符串）；首次读取后会落到本目录的 `account.json`。

## ncm_cookie.json

登录态，数组，每个账号一条。由 `login.py` 自动写入，**不需要手改**（要禁用账号可以改 `enable`）。

```json
[
  {
    "userId": "343525065",
    "nickname": "示例账号",
    "type": "qrcode",
    "enable": true,
    "cookie": {
      "MUSIC_U": "…",
      "__csrf": "…"
    }
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `userId` | string | 网易云用户 ID，登录成功后从 `/user/account` 取 |
| `nickname` | string | 昵称，只用于日志和通知 |
| `type` | string | 登录方式：`qrcode` 或 `code`；`code` 支持 `/login/refresh`，`qrcode` 不支持 |
| `enable` | bool | 是否启用；`false` 时 `run.py` 会跳过，重新登录不会改回 `true` |
| `cookie` | object | `MUSIC_U`、`__csrf` 等键值，请求时拼成 `Cookie` 头 |

行为：

- 按 `userId` 覆盖或追加；重新登录不会丢失 `enable` 设置。
- 登录态失效时：`code` 账号自动刷新并回写；`qrcode` 账号跳过并在通知里提示重新扫码。
- 文件里没有可用账号时，`run.py` 会提示先执行 `python login.py --type=qrcode`。

## ncm_state.json

每个账号的每日目标、单轮配额、断点和「每天只做一次」的标记。由 `run.py` 自动写入，数组：

```json
[
  {
    "userId": "343525065",
    "date": "2026-11-03",
    "target_today": 336,
    "slot_index": 0,
    "slot_target": 180,
    "slot_played": 180,
    "played_today": 180,
    "playlist_index": 1,
    "last_song_id": 518066366,
    "total_played": 12034,
    "runs": 1,
    "signin_date": "2026-11-03",
    "yunbei_date": "2026-11-03",
    "updated_at": "2026-11-03 09:30:00"
  }
]
```

| 字段 | 说明 |
| --- | --- |
| `userId` | 账号 ID |
| `date` | 本条记录对应的日期；跨天自动归零当日计数 |
| `target_today` | 当天目标总量（按日期确定性随机，同一天恒定） |
| `slot_index` | 已开始的轮次下标（0 到 `slots_per_day - 1`） |
| `slot_target` | 本轮随机目标首数 |
| `slot_played` | 本轮已播首数 |
| `played_today` | 今天累计已播首数 |
| `playlist_index` | 当前歌单在 `user_sl_id` 里的下标（歌单断点） |
| `last_song_id` | 最后播放的歌曲 ID，下次从下一首继续 |
| `total_played` | 历史累计播放首数 |
| `runs` | 运行次数 |
| `signin_date` | 每日签到完成日期；当天已完成就不再请求 |
| `yunbei_date` | 云贝签到完成日期 |
| `updated_at` | 最后更新时间 |

行为：

- 每播放一首都会落盘，中途断网/到时间上限后下次只补没播完的部分。
- 跨天后：当日目标、轮次、已播数归零；`playlist_index` / `last_song_id` 等断点保留。
- 删掉某个账号的条目 = 该账号从头开始（会重新签到）；`task run.py --reset` 清空全部 cookie 与 state。

## ncm_run.lock 与 code.jpg

- `ncm_run.lock`：单实例锁，内容是当前进程 pid。`run.py` 启动时检查，防止青龙定时任务重叠；
  正常退出会删除，异常残留时如果 pid 已不存在会自动接管。
- `code.jpg`：扫码登录时生成的二维码图片（可选）。需要安装二维码依赖：
  `uv sync --extra qr` 或 `pip install 'qrcode[pil]'`；没装时只打印二维码链接和终端二维码。

## 安全与维护

- 真实数据文件权限 `0600`、目录 `0700`（`*.example.json` 模板是 `644`，不含真实数据）；不要把 `ncm_cookie.json` 贴到 issue / 截图 / 日志里。
- 备份：只需要备份 `config.json`（和 `account.json`）；cookie 失效后重新登录即可。
- 迁移旧文件：如果 `config.json` / `account.json` / `ncm_cookie.json` / `ncm_state.json`
  还在项目根目录，首次运行会自动移进本目录（同名文件已存在则不覆盖），也可以手动
  `task run.py --migrate`。
- 清空登录态与断点：`task run.py --reset`（保留 `config.json` / `account.json`）；
  也可以直接删除 `ncm_cookie.json` / `ncm_state.json`。
- 换数据目录：`NCM_DATA_DIR=/path/to/data task run.py`。
