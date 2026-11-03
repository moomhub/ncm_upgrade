# ncm-upgrade

某云音乐工具集：**登录（扫码 / 验证码）** + **自动听歌涨等级**（青龙定时任务 / 本地）。

> [!TIP]
>
> 使用前，请先自行部署：[api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)

> [!CAUTION]
>
> 当前验证码登录需要依赖PR [Pull requests](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/pull/294)，等这个PR 合并后访问相关API接口才能正常登录，不然会提示风险登录。

## 模块划分

| 模块 | 入口 / 目录 | 职责 |
| --- | --- | --- |
| **login** | `login.py`（青龙 `task login.py`）+ `login/` | 只做登录（扫码 / 验证码），把登录态写进 `data/ncm_cookie.json` |
| **run** | `run.py`（青龙 `task run.py`）+ `run/` | 只跑任务（签到 / 播放 / 通知 / 自检），按 cookie 干活 |
| 共享底座 | `shared/`（`common.py`、`api.py`） | 数据目录与配置校验、日志、异常、JSON 原子读写、HTTP Client |

login 与 run 的主流程互不 import：login 只写数据文件，run 只读数据文件，可以分开部署、分开定时。
根目录的 `login.py` / `run.py` 只是青龙入口壳，参数原样透传给 `login/`、`run/` 里的实现。
所有运行期数据（配置 / 账号 / cookie / 断点）都在 **`data/` 目录**里。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `login.py` | **青龙登录入口**：`task login.py --type=qrcode` |
| `run.py` | **青龙任务入口**：`task run.py`，文件头带 `cron` / `new Env`，可直接订阅 |
| `login/` | login 模块：`flow.py`（扫码/验证码流程）、`cli.py`（参数解析）、`accounts.py`（验证码账号列表） |
| `run/` | run 模块：`main.py`（锁/并发/签到/通知/CLI）、`player.py`（播放循环）、`state.py`（每日目标与断点）、`selftest.py`（自检） |
| **`data/`** | **运行数据目录**：`config.json`、`account.json`、`ncm_cookie.json`、`ncm_state.json`、锁文件、二维码图片；含 `README.md` 与 `*.example.json` 模板 |
| **`shared/`** | **通用底座**：`common.py`（数据目录、配置加载/校验、路径、日志、异常、JSON 原子读写、时间预算与看门狗）、`api.py`（每账号一个 `Client`、cookie 解析、`/login/refresh`、业务接口） |
| `pyproject.toml` / `requirements.txt` / `uv.lock` | 依赖（`requests`，二维码可选 `qrcode[pil]`） |

## 数据目录：data/（一个文件夹维护全部）

```text
data/
├── README.md                # 本目录说明：每个文件的字段与结构
├── config.json              # 配置：api / user_sl_id / 配额等（手动维护）
├── config.example.json      # 配置模板
├── account.json             # 验证码登录账号（phone / code，手动或环境变量导入）
├── account.example.json     # 账号模板
├── ncm_cookie.json          # 登录态，login.py 自动写
├── ncm_cookie.example.json  # 登录态模板（假数据）
├── ncm_state.json           # 断点与签到标记，run.py 自动写
├── ncm_state.example.json   # 断点模板（假数据）
├── ncm_run.lock             # 单实例锁（运行时自动生成）
└── code.jpg                 # 扫码登录的二维码图片（可选，自动生成）
```

- 每个文件的字段、结构和示例见 [data/README.md](data/README.md)；`*.example.json` 只是模板，程序不会读取。
- **首次使用**：`cp data/config.example.json data/config.json`，填好 `api` 与 `user_sl_id`；
  用验证码登录再 `cp data/account.example.json data/account.json`。
- 只需人工维护 `data/config.json` 和 `data/account.json`；cookie / state / 锁 / 二维码都由程序自动生成。
- 如果旧版数据文件还散落在项目根目录（`config.json`、`account.json`、`ncm_cookie.json`、`ncm_state.json`），
  首次运行会自动把它们移进 `data/`（同名文件已存在则不覆盖）；也可以手动执行 `task run.py --migrate`。
- 真实数据文件权限 `0600`、目录 `0700`（`*.example.json` 模板是 `644`，不含真实数据）。
- 真实数据文件（`config.json` / `account.json` / `ncm_cookie.json` / `ncm_state.json` / 锁 / 二维码）在 `.gitignore` 中，不会被提交；`data/README.md` 与 `*.example.json` 模板会提交。
- 数据目录可用环境变量 `NCM_DATA_DIR` 换到别处（默认就是项目根目录的 `data/`）。
- 进阶：`NCM_CONFIG` 或 `--config` 可以临时指向一个独立的 config JSON，覆盖 `data/config.json`。

## 快速开始

```bash
uv sync                                     # 需要二维码图片时：uv sync --extra qr
cp data/config.example.json data/config.json   # 填 api 与 user_sl_id

# ① 登录（两种方式二选一）
python login.py --type=qrcode
python login.py --type=code --account=<手机号>

# ② 跑任务
python run.py
```

## 青龙

### 订阅（推荐，自动建任务）

把 `run.py` 放进你的脚本仓库 / Gist，在青龙「订阅管理」填写 raw 地址即可。
文件头的注释会被青龙识别：

```
cron: 30 9,17 * * *
new Env('网易云听歌')
```

订阅后自动创建任务，任务命令就是 `task run.py`；默认每天 9:30、17:30 各跑一轮
（对应 `slots_per_day: 2`，想改成一次跑完就把 cron 改成 `30 8 * * *` 并调大 `slots_per_day` 或 `daily_target_base`）。

> `run.py` 依赖同目录的 `run/`、`shared/`、`login.py` 和 `data/`。
> 青龙订阅只会下载脚本文件本身，所以请把整个项目放进订阅仓库（保留相对路径），
> 或先手动把这些文件上传到脚本目录，再订阅 `run.py`。

### 手动

把 `login.py`、`run.py`、`login/`、`run/`、`shared/`、`data/` 放在同一目录：

- 定时任务：`task run.py`（可带参数，例如 `task run.py --user 123456`）
- 首次登录：`task login.py --type=qrcode`（或 `task login.py --type=code --account=<手机号>`）
- 青龙内置 Python 装好 `requests` 即可（二维码可选 `qrcode pillow`），不需要 uv
- 两个入口都自带 sys.path 兜底（按文件位置找项目根目录），不需要设置 `PYTHONPATH`

## 登录

登录方式完全由 login 模块的 `--type` 决定（默认 `qrcode`），`data/config.json` 里没有 `type`，
run 模块也不关心它 —— 主程序只按 `data/ncm_cookie.json` 里的登录态干活。

| 方式 | 命令 | 说明 |
| --- | --- | --- |
| 扫码 | `task login.py --type=qrcode` | 生成二维码后**每秒**轮询一次 `/login/qr/check`，手机确认后立刻返回 |
| 验证码 | `task login.py --type=code --account=<手机号>` | 从 `data/account.json` 读该手机号 → 发码 → **每秒**读一次 `code` → 登录 → 成功后把该手机号的 `code` 清空 |

> 验证码登录走 **PR #294 的 `/api/yidun/*`**（提前就位）：服务端没合并时会直接报错并提示两个方案；
> 临时可用 `--legacy` 退回仓库原有的 `/captcha/sent` + `/login/cellphone`。

验证码方式的账号列表在 `data/account.json`（青龙也可以配成环境变量
`account.json` / `ACCOUNT_JSON` / `NCM_ACCOUNT`，内容为 JSON 字符串，首次读取后导入数据目录）：

```json
[
  {"phone": "<手机号>", "code": ""}
]
```

脚本每秒检查 `code` 字段，把收到的 6 位验证码填进去即可登录（改 `data/account.json` 或环境变量都行）。

登录成功后写入 `data/ncm_cookie.json`，例如：

```json
[
  {
    "userId": "343525065",
    "nickname": "MoomHub",
    "type": "code",
    "enable": true,
    "cookie": {"MUSIC_U": "…", "__csrf": "…"}
  }
]
```

> 多个账号各自登录一次即可，`run.py` 会并发跑全部账号。

## 多账号 / 并发

- `data/ncm_cookie.json` 里有多少条**启用**的账号，就跑多少个，默认并发数 = 账号数（`max_workers` 可限制）。
- 每个账号有 `enable` 开关：改成 `false` 后 `run.py` 会跳过它（重新登录不会把这个开关改回来）。
- 只跑指定账号：`task run.py --user 343525065,123456`。
- 每个账号有独立的 cookie、独立的 `data/ncm_state.json` 记录（按 `userId` 归档），互不影响。

## 每日签到只做一次

`每日签到 / 云贝签到` 记在 `data/ncm_state.json` 的 `signin_date / yunbei_date` 上，
同一天重复运行会直接跳过，不会重复请求（防重复、防风控）。跨天自动重来。

## 登录态失效怎么处理

| 登录方式 | 失效时 |
| --- | --- |
| `code`（验证码） | 自动带 cookie 调 `/login/refresh`，成功则写回 `data/ncm_cookie.json` 并重试一次 |
| `qrcode`（扫码） | 扫码 cookie 不支持刷新 → 该账号跳过 + 推送通知，提示重新 `task login.py --type=qrcode` |

## 命令行

```bash
task run.py                    # 跑全部账号（青龙）
python run.py                  # 本地等价写法
python run.py --user 123       # 只跑指定 userId（逗号分隔）
python run.py --status         # 打印每个账号的断点与今日进度
python run.py --selftest       # 21 个自检用例，不联网
python run.py --reset          # 清除 data/ncm_cookie.json 与 data/ncm_state.json（保留 config / account）
python run.py --migrate        # 把项目根目录的旧数据文件移入 data/
python run.py --config X       # 指定独立配置文件（进阶）

task login.py --type=qrcode                  # 扫码登录（青龙）
task login.py --type=code --account=<手机号>  # 验证码登录
```

> `python -m run` / `python -m login` 仍然可用；拆分前的 `python main.py` 现在对应 `task run.py`。
> `login.py` / `run.py` 两个入口只是壳，参数与拆分前完全一致（`--type` / `--account` / `--user` / `--status` …）。

## 配置项（data/config.json）

必填：`api`、`user_sl_id`。其余参数（配额、轮次、间隔、超时、并发数等）
都有默认值，写在 `data/config.json` 里的才覆盖，完整模板见 `data/config.example.json`。

环境变量：

| 变量 | 作用 |
| --- | --- |
| `NCM_DATA_DIR` | 数据目录，默认项目根目录的 `data/` |
| `NCM_CONFIG` | 指向一个独立 config JSON（覆盖 `data/config.json`，进阶用法） |
| `account.json` / `ACCOUNT_JSON` / `NCM_ACCOUNT` | 验证码登录的账号列表（JSON 字符串），首次读取后导入 `data/account.json` |

## 自检

```bash
python run.py --selftest        # 21 个用例，不联网（含数据目录迁移 / 并发 / 断点 / 配额）
```

> 自检会顺带验证 login 模块的账号列表逻辑，所以两个模块都要在（单独部署 run 时跳过 `--selftest`）。
