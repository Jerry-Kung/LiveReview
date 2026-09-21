# 部署与运维

面向「把 LiveReview 部署到一套新环境并验证可用」。按 §1 → §2 的顺序走即可完成一次全新部署；
§3 是配置基线，§4 与 §5 供上线后日常使用。

## 0. 目标形态

一套环境一个实例，Docker Compose 拉起两个容器：

| 组件 | 镜像基础 | 对外 | 说明 |
|---|---|---|---|
| `frontend` | Node 20 构建 → nginx 1.27 托管 | **唯一入口**，`<主机>:12439` | 静态页面 + 反代 `/api`、`/health` 到后端 |
| `backend` | python:3.12-slim + ffmpeg | 不发布端口 | FastAPI 单进程，媒体工具随镜像安装 |

后端容器不向宿主机发布端口，只在 compose 网络内可达。外部流量一律经前端容器进入，
避免与宿主机其它服务抢占端口。

**持久化全部落在宿主机目录**（不是具名卷）：数据库 `backend/data/liverreview.db`，
本地产物 `backend/media/`。容器内 `/app/data`、`/app/media` 与它们是同一份文件，
因此在宿主机建账号、查数据库都直接生效，不必进容器。

> 单实例约束：后台任务是进程内线程池，状态落库。**不要对同一份 `backend/data` 起第二个后端容器**
> （并发执行器会互相抢任务），横向扩展不在 V0 范围内。

## 1. 部署前确认

| 项 | 要求 | 怎么确认 |
|---|---|---|
| Docker | Docker Engine + Compose v2 | `docker compose version` |
| 端口 | **12439 空闲** | `ss -lntp \| grep 12439`（Windows：`netstat -ano \| findstr 12439`） |
| 磁盘 | 留足 **10 GB 以上** | 构建镜像 + 处理期临时盘峰值约素材的 2～3 倍 |
| 出网 | 可达对象存储端点、模型服务端点 | 见 §2.4 的自检 |
| 对外访问 | 安全组 / 防火墙放行 12439 | 否则 §2.5 的浏览器验证做不了 |
| 凭据 | TOS 的 AK/SK、Endpoint、Region、Bucket | 由 TOS 控制台提供；Bucket 建议先建好 |
| 模型 | OpenAI 兼容服务的 Base URL、API Key、模型名 | 音画理解模型必须支持视频输入 |

端口被占用时改 12439 有**三处必须同时改且保持一致**，否则登录会 403 或页面拿不到接口（见 §4.4）：
`docker/docker-compose.yml` 的 `ports`、同文件 backend 的 `APP_PORT` 与 healthcheck、
`frontend/nginx.conf` 里的两处 `proxy_pass` 端口。

## 2. 部署步骤

在仓库根目录执行（下同）。

### 2.1 准备运行目录

`backend/data` 与 `backend/media` 由 `.gitignore` 排除，新克隆的仓库里没有。
**必须先手动建好**：目录不存在时 compose 会以 root 身份创建，之后宿主机侧写文件会遇到权限问题。

```bash
mkdir -p backend/data backend/media
```

新环境用空库即可，无需从别处拷贝数据库文件。

### 2.2 准备 backend/.env

配置**在运行时注入**，不打进镜像（`backend/.dockerignore` 已排除 `.env`）。该文件缺失时
compose 在启动阶段直接报错，不会静默以「无凭据」状态拉起容器。

```bash
cp backend/.env.example backend/.env
```

需要填的只有凭据与少量环境相关项，其余保持默认。逐项含义见 §3。

- **对象存储（必填）**：`TOS_ACCESS_KEY`、`TOS_SECRET_KEY`、`TOS_ENDPOINT`、`TOS_REGION`、`TOS_BUCKET`。
  compose 里固定 `STORAGE_BACKEND=tos`，缺任何一项都会明确失败，不回落本地 Mock。
- **模型（必填）**：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_NAME`。三项均为识别与复盘的前置条件，
  缺失时切分链路照常工作，但识别接口返回 503 并列出缺失项名称。
- **会话 Cookie**：暂以 HTTP 访问时 `AUTH_COOKIE_SECURE` 保持 `false`；一旦前置 HTTPS 必须改为 `true`（见 §3.1）。
- 其余变量（切分约束、超时、保留期、并发）先用默认值，调优见 §3 与 §4.5。

> `docker/docker-compose.yml` 的 `environment` 优先级高于 `env_file`：`APP_ENV`、`APP_HOST`、
> `APP_PORT`、`APP_RELOAD`、`DATABASE_URL`、`MEDIA_ROOT`、`STORAGE_BACKEND`、`LLM_CONCURRENCY`
> 这八项以 compose 为准，**在 `.env` 里改它们不生效**。

### 2.3 构建并启动

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

构建会拉取 python:3.12-slim / node:20-alpine / nginx:1.27-alpine 并在镜像内安装 ffmpeg，
apt 与 pip 均已指向清华源。首次构建耗时主要取决于网络。

`frontend` 依赖 `backend` 健康检查通过后才启动（`depends_on: service_healthy`），
因此前端起来即代表后端已就绪。

### 2.4 启动自检

**一、容器状态与健康**

```bash
docker compose -f docker/docker-compose.yml ps
```

两个容器都应为 `running`，backend 为 `healthy`。若 backend 反复重启，先看日志：

```bash
docker compose -f docker/docker-compose.yml logs --tail=100 backend
```

**二、配置确实进入了容器**（只打印缺失项名称，不回显取值，输出可安全分享）

```bash
docker compose -f docker/docker-compose.yml exec backend \
  python -c "from app.config import get_settings as g; s=g(); print(s.storage_backend, s.storage_missing_fields, s.llm_missing_fields)"
```

期望输出 `tos [] []`。`storage_backend` 不是 `tos`、或任一缺失列表非空，都说明 `backend/.env`
未生效，**先解决这里再做后续步骤**。

**三、对象存储连通性**（上传 → 预签名可访问 → 下载校验 → 删除，共六个环节）

```bash
docker compose -f docker/docker-compose.yml exec backend python -m app.storage.verify
```

逐项打印结果，失败时打印失败环节与服务端 `request_id`（可用于向存储服务商定位问题），退出码非 0。
需要保留测试对象以便在控制台人工检查时加 `--keep`。

### 2.5 建管理员账号并登录

本版**没有注册入口**，账号存在数据库 `users` 表里、不由环境变量配置。空库时登录返回 503
并提示执行初始化脚本，这是冷启动的正常状态、不是故障。

```bash
docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin
```

交互式，口令不回显、需输两次；脚本自己建表，空库可直接跑。要造后续账号不必再进容器：
管理员登录后在侧栏「账号管理」里新增（**界面只能建普通账号，第二个管理员仍然只能跑脚本**）。

随后在浏览器打开 `http://<部署主机>:12439`，用刚建的账号登录。

### 2.6 端到端验证

界面右上角「新建任务」上传一段**真实录屏**（建议单文件 2 GB 以内，更大也能处理，只是更慢）。
依次确认五个环节，括号内是界面上的按钮名：

1. **上传完成**，等待「视频处理」自动跑完（解析 → 切片 → 完整性校验，见任务详情页的进度节点）。
2. 点「**开始识别语音**」。
3. 识别完成后点「**开始复盘分析**」。
4. 在「内容理解」页核对转写，在「复盘分析」页核对结论，并下载 Markdown 报告。

**识别与复盘都要手动点，系统不会自动往下跑。** 用一段几十分钟的素材走通即可；
总耗时为上传 + 切片 + 识别（约片段数 × 单片耗时 ÷ 并发）。

## 3. 配置基线

### 3.1 环境变量

完整清单与默认值见 `backend/.env.example`，配置契约见 `backend/app/config.py`。
部署时通常只需调下面这些。

| 变量 | 默认 | 何时需要改 |
|---|---|---|
| `STORAGE_BACKEND` | `auto` | compose 已固定 `tos`（强制真实存储，缺凭据报错） |
| `AUTH_COOKIE_SECURE` | `false` | **前置 HTTPS 时改为 `true`**，否则会话令牌在明文连接上传输 |
| `STORAGE_VIDEO_TTL_SECONDS` | 259200（72h） | 云端视频保留期，只影响此后新上传的任务 |
| `CLEANUP_ENABLED` / `CLEANUP_INTERVAL_SECONDS` | `true` / `900` | 后台清理线程开关与扫描周期 |
| `LLM_CONCURRENCY` | `1` | 模型配额确认后可上调（compose 固定为 1，须同步改 compose） |
| `LLM_TIMEOUT_SECONDS` | 900 | 本版最容易低估的一项：素材更长或并发更高时上调 |
| `SPLIT_MAX_DURATION_SECONDS` / `SPLIT_MAX_CLIP_BYTES` | 3600 / 1GiB | 硬约束，调小片段更多更慢，调大可能超出识别输入限制 |
| `PROBE_TIMEOUT_SECONDS` / `SPLIT_TIMEOUT_SECONDS` | 300 / 600 | 素材更长或磁盘更慢时上调 |
| `AUTH_SESSION_TTL_SECONDS` / `AUTH_SESSION_ABSOLUTE_TTL_SECONDS` | 12h / 7d | 登录态有效期，前者随访问顺延、后者是硬上限 |

改完 `.env` 需要重启才生效（账号数据不在此列，改账号无需重启）：

```bash
docker compose -f docker/docker-compose.yml up -d --force-recreate backend
```

### 3.2 数据与保留期

- **本地不留视频**：上传入库成功后本地副本即释放，处理成功或失败都不留原文件与切片。
  `backend/media/` 只需容纳「正在上传 / 正在处理」的那一场，峰值约为原视频的 2～3 倍。
- **云端视频保留 72 小时**（`STORAGE_VIDEO_TTL_SECONDS`）：到期由应用内定时线程删除桶里的
  原始视频与切片。**只删视频、不删结论**——转写与复盘结论已落库，过期任务仍可查看与下载报告，
  只是不能再启动或重试识别。未完成识别的任务过期后**需要用户重新上传**（新建任务，无断点续跑）。
- 为什么不配桶的生命周期规则：清理与「哪些任务还在跑」强相关，在途任务必须跳过，
  只有应用知道这个状态。

### 3.3 表结构升级

表在启动时用 `create_all` 建缺失的表，并**为已存在的表补加「可空或有默认值」的新列**。
因此新增表与新增可选字段只需重新构建并重启，不必动数据库：

```bash
docker compose -f docker/docker-compose.yml up -d --build backend
```

补列只做加法。两类变更必须删库重建，操作前先备份 `backend/data/liverreview.db`：

- 新增**既不可空又无默认值**的列（补列会明确报错并给出列名，不会静默跳过）；
- 改动已有列的语义（类型、可空性、重命名）或已有数据的含义。

删库后账号一并消失，**必须重新执行一次 §2.5 的建号脚本**。

## 4. 上线后运维

### 4.1 账号

| 操作 | 做法 |
|---|---|
| 建普通账号 | 管理员登录 → 侧栏「账号管理」→ 新增 |
| 建第二个管理员 | 只能在服务端跑 `python -m app.auth.init_admin`（界面请求体里没有角色字段） |
| 查看已有账号 | `... exec backend python -m app.auth.init_admin --list`（含账号、角色、显示名、上次登录） |
| 改口令 | 再跑一次同名账号的初始化脚本，会单独确认一次；`--force` 跳过确认（供脚本用） |
| 踢掉某账号全部会话 | 改一次口令即可，旧会话一并作废 |
| 忘记口令 | 本版没有找回入口，用上面的重设路径 |

容器未运行时可用 `docker compose ... run --rm backend python -m app.auth.init_admin` 起一次性的。
`--password` 会在 shell 历史里留痕，人工操作请用交互模式。管理员账号不能在界面删除。

### 4.2 日志与健康

```bash
# 后端日志（任务失败原因、恢复动作、清理记录都在这里）
docker compose -f docker/docker-compose.yml logs -f --tail=200 backend

# 前端 nginx 日志
docker compose -f docker/docker-compose.yml logs -f --tail=100 frontend

# 服务健康（聚合数据库与对象存储两路状态）
curl -s http://localhost:12439/health
```

任务失败原因会写入任务与上传会话的 `error` 字段，服务端存储错误带 `request_id`。
预签名 URL 是凭据，日志与失败原因只记对象键、不回显 URL。

### 4.3 备份

需要备份的只有一处有状态文件，且不在容器内：

- `backend/data/liverreview.db`——账号、任务、识别与复盘结论，直接复制即可；
- `backend/media/`——只是未完成上传/处理的中间产物，可不备份。

对象存储中的视频按 §3.2 的保留期自动回收，不属于备份范围。

### 4.4 故障速查

| 症状 | 处理方向 |
|---|---|
| 登录返回 **403「请求来源不被信任」** | 同源校验失败，**与账号、口令、数据库都无关，不要往那个方向排查**。原因是代理转发吃掉了端口，`Host` 必须是 `$http_host` 而非 `$host`。改端口时尤其容易撞上，见 §1 的三处一致性要求 |
| 登录报「**服务端尚未创建任何账号**」 | 库里一条账号记录都没有，冷启动的正常状态。跑 §2.5 建号 |
| 登录报「**登录尝试过于频繁**」 | 同一账号 1 分钟内连续失败 5 次被拒。进程内计数，重启后端即清空 |
| 启动日志告警「**没有任何账号可登录**」 | 同上，建号即可 |
| 任务**停在 `uploaded`** | 后台执行器未推进。看后端日志有无执行器异常；重启容器会按落库状态重新入队 |
| 任务**停在 `processing`** | 重启后会自动退回 `uploaded` 重新入队。反复出现则检查容器是否被 OOM 杀掉 |
| 识别**重启后停在「未开始」** | 从未启动过识别的任务不会被自动拉起（识别要花钱，必须用户点一次）。已启动过的会续跑，只补未完成片段、不重复计费 |
| 识别接口 **503 并列出缺失项** | 模型配置未进入容器，回到 §2.4 第二项自检 |
| 任务失败，原因为「**未找到 ffprobe / ffmpeg**」 | 容器内 `ffprobe -version` 确认工具可用；核对 `FFPROBE_PATH` / `FFMPEG_PATH` |
| 任务失败，原因为「**探测超时**」 | `-count_packets` 要完整读一遍文件，长录屏耗时随之增长。上调 `PROBE_TIMEOUT_SECONDS` 或查磁盘 IO |
| 任务失败，原因为「**没有音频或视频流**」 | 上传的不是可播放的录屏素材，检查文件本身 |
| 任务失败，原因为「**覆盖校验发现 N 处问题**」 | 片段间有缺口或重叠，问题信息含秒数与相邻序号；调大 `SPLIT_MAX_DURATION_SECONDS` 减少交界数量，常能规避关键帧对齐引起的边界偏差 |
| 任务失败，原因为「**转封装产物时长与原始视频偏差过大**」 | 先确认源文件是否完整。若日志出现「未能量出可播放时长」，说明逐包统计没跑成，这条失败先按假阳性处理 |
| 识别成功但**语音条数偏少** | 先看片段上是否有 `understanding_warnings`（被丢弃的条目）与越界标记，再判断是模型问题还是解析问题 |
| **全部接口 401** | 会话失效（默认 12 小时、绝对上限 7 天），重新登录即可 |
| **磁盘被上传分片占满** | 未完成的上传留在 `backend/media/chunks/`。确认无人续传后删除对应会话目录 |

需要更细的定位手段（逐片识别状态、模型原始返回、token 用量）时，直接查任务接口或进容器执行，
这里不再重复列出。

### 4.5 容量与调优要点

- **处理耗时** ≈ 上传时间 + 转封装/切分（与素材时长成正比）+ 识别（片段数 × 单片耗时 ÷ `LLM_CONCURRENCY`）。
- 识别是**最贵也最慢**的一环：调小 `SPLIT_MAX_DURATION_SECONDS` 能得到更短的片段、识别更快，
  但片段数与总调用次数上升。
- 整场转写超出 `REVIEW_INPUT_CHARS_PER_CALL` 会按记录边界分批调用，合并不额外调模型。
- 表结构变更或素材类型明显变化时，先在一场真实素材上跑通 §2.6，再开放给用户。

## 5. 当前版本的已知限制

部署到正式环境前应知晓，避免把它们当成部署缺陷：

- **单实例**：后台任务是进程内线程池，状态落库；不做横向扩展。
- **无用户隔离**：所有登录账号看到同一套任务数据。角色只决定「能不能管账号」，
  上传、处理、复盘、下载对全量账号一视同仁。
- **无注册、无改口令界面**：账号由管理员创建，口令重置只能走服务端脚本。
- **对象存储与应用层清理强绑定**：不要单独在桶上配生命周期规则，否则会删掉在途任务正在用的视频。
- **上传无体积硬上限**：界面上「2 GB 以内」是典型素材规模的建议值，不是系统限制；
  真实约束来自探测/切分超时与处理期临时盘占用。
- **视频 72 小时过期**：过期任务的识别不可重跑，用户需重新上传。
- **`.env` 中八项以 compose 为准**：改动它们要同时改 `docker/docker-compose.yml` 才会生效。
