# 部署与运行

本文记录本地运行、配置约定与测试环境（Docker Compose）部署方式。功能与规划见仓库根目录 `README.md`。

## 1. 环境要求

- 后端：Python ≥3.12 + uv
- 前端：Node ≥20 + npm
- 测试环境：Docker Compose（本地开发不强制安装 Docker）

## 2. 配置约定

- 后端从环境变量读取配置，文件样例为 `backend/.env.example`，复制为 `backend/.env` 后按需修改。
- 前端 dev server 的代理端口从 `frontend/.env.example` 复制为 `frontend/.env` 配置。
- 敏感凭据（如 TOS 对象存储 AK/SK）只从环境变量读取，不得写入代码或提交仓库。
- 服务监听地址与端口通过后端 `.env` 的 `APP_HOST` / `APP_PORT`（默认 `127.0.0.1:12439`）配置；前端 `.env` 的 `VITE_BACKEND_PORT` 需与后端 `APP_PORT` 保持一致。
- 对象存储通过 `STORAGE_BACKEND` 选择实现：`auto`（默认，凭据齐全用 TOS，否则回落本地 Mock 并记录 warning）、`tos`（强制真实，缺凭据直接报错，测试环境使用）、`mock`（强制本地内存实现）。`TOS_OBJECT_PREFIX` 是对象键的公共前缀（默认 `liverreview/`），`TOS_PRESIGNED_TTL_SECONDS` 是预签名下载链接的默认有效期（秒，默认 3600）。本地未配置 TOS 凭据时不影响启动，存储自动回落 Mock。
- 上传分片、合并临时文件、原始视频副本与切分中间产物落在 `MEDIA_ROOT`（本地默认 `./media`，容器内 `/app/media`），`UPLOAD_CHUNK_SIZE` 是下发给前端的分片大小（字节，默认 8MB）。该目录不是用户配置项之外的业务数据，清理它只影响未完成的上传与未完成切分的中间产物。
- 媒体探测（V0.1.4 起）通过 `FFPROBE_PATH`（可执行文件名或「解释器 + 脚本」形式的 JSON 列表）、`PROBE_TIMEOUT_SECONDS`（默认 300 秒）、`PROBE_MAX_OUTPUT_BYTES`（默认 8MB）配置。本地不部署 FFmpeg，因此本地启动只能验证探测的编排与失败原因，真实探测必须在测试环境执行。
- **模型接入（V0.2 起）**通过 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_NAME` 三项配置，对接 OpenAI 兼容的 `responses` 接口（当前使用火山引擎方舟的音画理解模型）。**三项都是识别的前置条件**：缺失时切分链路照常工作，但识别接口直接返回 503 并列出缺失项名称，不会逐片各失败一次。`LLM_TIMEOUT_SECONDS`（单次视频理解请求超时，默认 900 秒）是本版最容易低估的一项——分钟级的片段识别在网络与模型排队叠加后会逼近这个值，素材更长或并发更高时应上调。`LLM_FPS`（送模型的采样帧率，默认 1）、`LLM_MAX_ATTEMPTS`（单片最大尝试次数，默认 3）、`LLM_CONCURRENCY`（片段识别并发数，默认 1，串行）、`LLM_CLIP_URL_TTL_SECONDS`（片段预签名视频地址有效期，默认 7200 秒，须覆盖排队与单次识别耗时）、`LLM_UNDERSTANDING_PROMPT`（识别题面，留空使用默认的「识别片段中的所有人声语音并判断语气」）。**复盘（V0.3 起）复用同一组 `LLM_*` 配置**，另有 `REVIEW_TIMEOUT_SECONDS`（单次复盘请求超时，默认 300 秒，纯文本分析量级远小于视频理解）、`REVIEW_MAX_ATTEMPTS`（单次复盘的最大尝试次数，默认 3）、`REVIEW_INPUT_CHARS_PER_CALL`（单次调用送出的转写字符数上限，默认 20000，超出则按记录边界分批后合并）。
- 预处理与切分（V0.1.5 起）通过 `FFMPEG_PATH`（形式同 `FFPROBE_PATH`）、`SPLIT_MAX_DURATION_SECONDS`（单片最长时长，默认 3600 秒）、`SPLIT_MAX_CLIP_BYTES`（单片最大体积，默认 `1073741824` 即 1GiB）、`SPLIT_MIN_CLIP_SECONDS`（体积超限时递归对半的下限，默认 1 秒）、`SPLIT_TIMEOUT_SECONDS`（单次转封装/切片调用超时，默认 600 秒）、`FFMPEG_MAX_OUTPUT_BYTES`（ffmpeg stderr 收集上限，默认 8MB）配置。**`SPLIT_MAX_DURATION_SECONDS` 与 `SPLIT_MAX_CLIP_BYTES` 是硬约束**：调小它们会让片段更多、切分更慢，调大则可能超过后续识别环节的输入限制。

- **用户登录（V0.5.1 起）**的账号存在数据库 `users` 表里，**不由环境变量配置**（V0.5 的 `AUTH_USERS` 与 `AUTH_SESSION_SECRET` 已移除）。冷启动先跑 `cd backend && python -m app.auth.init_admin` 建管理员，容器内用 `docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin`，运维细节见 §4.1。登录态在 `sessions` 表里，`AUTH_SESSION_TTL_SECONDS`（会话有效期，默认 12 小时，每次访问顺延）与 `AUTH_SESSION_ABSOLUTE_TTL_SECONDS`（绝对上限，默认 7 天）控制时长。`AUTH_COOKIE_SECURE` 仅在 HTTPS 下置为 `true`，本地开发是 http，置 true 会导致登录后立刻掉线。**数据库里一个账号都没有时**，启动日志会告警且登录接口返回 503 并指明初始化脚本；登录后的 `GET /api/auth/status` 也能看到同一份告警。

## 3. 本地运行

### 后端

```bash
cd backend
cp .env.example .env   # 可按需修改 APP_PORT 等配置
uv sync
uv run python -m app   # http://localhost:12439/health
uv run pytest          # 测试
```

### 前端

```bash
cd frontend
cp .env.example .env   # VITE_BACKEND_PORT 需与后端 APP_PORT 一致
npm install
npm run dev      # http://localhost:5173（已代理 /health 到后端 12439）
npm run test     # 测试
npm run build    # 构建产物到 dist/
```

## 4. 测试环境（Docker Compose）

```bash
docker compose -f docker/docker-compose.yml up --build
```

拉起后访问 `http://localhost:12439`（前端页面，`/health` 由 nginx 反代到后端）。

后端容器不向宿主机发布端口（仅 compose 网络内 `expose 12439`），对外只保留前端这一个入口，避免与宿主机其它服务抢占端口。

后端镜像内 ffmpeg 已安装。**媒体与数据都用宿主机目录绑定**：`backend/media` 挂到 `/app/media`，`backend/data` 挂到 `/app/data`（数据库文件是 `backend/data/liverreview.db`）。两边是同一个文件，因此在宿主机用 `uv run python -m app.auth.init_admin` 建的账号，容器里的后端直接就能用，不必再进容器跑第二遍；反过来在容器里建号，宿主机也看得见。首次部署前先确认这两个目录存在（`backend/data` 与 `backend/media` 已由 `.gitignore` 排除，新克隆的仓库里可能没有），否则 compose 会以 root 身份创建它们，之后宿主机写文件会遇到权限问题。

后端镜像直接使用 `pip` 从 `backend/requirements.txt` 安装依赖（apt 与 pip 均已切换为清华源），不依赖 uv。

### 配置注入

容器内的配置由 `docker/docker-compose.yml` 的 backend 服务从两处注入：

- `env_file: ../backend/.env`（相对 compose 文件所在目录）：在宿主机 `backend/.env` 中配置的 `TOS_*` 等变量于**运行时**注入容器，不打进镜像（`backend/.dockerignore` 已排除 `.env`，凭据不得进入镜像层）。
- `environment`：`APP_ENV=test`、`APP_HOST=0.0.0.0`、`APP_PORT=12439`、`APP_RELOAD=false`、`DATABASE_URL`、`MEDIA_ROOT=/app/media`、`STORAGE_BACKEND=tos`。同名项优先级高于 `env_file`，容器内的这些取值不随 `.env` 漂移。

因此启动前需先将 `backend/.env.example` 复制为 `backend/.env` 并填好 `TOS_*`。该文件缺失时 compose 会在启动阶段直接报错，不会静默以「无凭据」状态拉起容器。

先确认变量已进入容器（只打印缺失项名称，不回显取值，输出可安全分享）：

```bash
docker compose -f docker/docker-compose.yml exec backend python -c "from app.config import get_settings as g; s=g(); print(s.storage_backend, s.storage_missing_fields)"
```

`storage_backend` 应为 `tos`，缺失列表应为 `[]`，否则说明 `backend/.env` 未生效，无需继续自检。

## 4.1 账号运维（V0.5.1 起）

本版没有注册机制，账号存在数据库的 `users` 表里。**账号不由环境变量配置**，因此新增或改口令之后不需要重启后端。

**冷启动建管理员**（在 `backend` 目录执行，容器内则在 `backend` 容器里执行）：

```bash
python -m app.auth.init_admin
# 管理员用户名（直接回车使用 admin）：
# 管理员密码：        ← 不回显
# 再次输入密码：      ← 不回显
# 管理员 admin 创建成功，界面称呼为「管理员」。
```

脚本自己建表，数据库是空的也能直接跑。

**查看已有账号**：`python -m app.auth.init_admin --list`。输出只有账号、角色、显示名与上次登录时间，不含明文口令与哈希。

**改口令**：再跑一次同名账号的初始化脚本。脚本会说明该账号已存在并单独问一次是否重设，答 `y` 才动手；`--force` 跳过询问（供脚本使用）。**重设口令会顺带作废该账号的全部旧会话**，需要重新登录——这正是把账号收进数据库要换来的性质，V0.5 那时改完口令旧登录态仍然有效。

**在容器内建号**（测试环境：宿主机没装 uv、或只想在部署现场直接建一次）：

```bash
# 交互式，口令不回显；数据库已绑定到宿主机 backend/data/，在容器里建与在宿主机建是同一个库
docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin

# 非交互，供脚本与自动化（--password 会在 shell 历史里留痕，人工操作请用交互模式）
docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin --username admin --password '<口令>' --display-name 管理员

# 查看已有账号 / 重设口令（重设会作废该账号的全部旧会话）
docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin --list
docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin --force
```

`exec` 要求后端容器已在运行；**还没有起容器时**用 `run --rm` 起一个一次性的（脚本自己建表，空库也能跑）：

```bash
docker compose -f docker/docker-compose.yml run --rm backend python -m app.auth.init_admin
```

注意 `run` 会连带解析并等待 `frontend` 的 `depends_on`，只是 `--rm` 用完即弃，不会留下额外容器。

**非交互建号**（测试环境与自动化）：

```bash
python -m app.auth.init_admin --username admin --password '<口令>' --display-name 管理员
```

`--password` 会在 shell 历史里留痕，人工操作请用交互模式。

**忘记口令**：本版没有找回入口，用上面的重设路径。

**锁定了账号**：同一账号 1 分钟内连续失败 5 次会被拒绝直到窗口滑过（错误文案是「登录尝试过于频繁」）。这是进程内计数，重启后端即清空。阈值集中在 `backend/app/auth/service.py` 的 `FAILURE_WINDOW_SECONDS` / `FAILURE_LIMIT`。

**登录报「服务端尚未创建任何账号」**：数据库里一条账号记录都没有（冷启动的正常状态，不是故障）。执行一次 `python -m app.auth.init_admin` 即可。

**会话的存放与清理**：登录态在 `sessions` 表里，Cookie 只携带令牌，服务端保存的是它的 SHA-256 摘要。有效期默认 12 小时、每次访问顺延，绝对上限 7 天（`AUTH_SESSION_TTL_SECONDS` / `AUTH_SESSION_ABSOLUTE_TTL_SECONDS`）。过期记录在撞上时即时删除，服务启动时再扫一遍，不需要定时任务。**要踢掉某个账号的全部会话**，改一次口令即可，不必再换签名密钥。

**部署到 HTTPS 时**把 `AUTH_COOKIE_SECURE` 置为 `true`，否则会话令牌会在明文连接上传输。

**写请求报 403「请求来源不被信任」**：后端校验 `Origin` 与请求自身的 Host 是否同源。代理转发的 Host 只要把端口吃掉（`proxy_set_header Host $host` 就会），浏览器发来的 `Origin: http://<主机>:<端口>` 与后端看到的 `Host: <主机>` 就对不上，登录会被判成跨站。**nginx 已改为转发 `$http_host`（保留端口）**，不要改回 `$host`；后端同时认 `X-Forwarded-Host`，且 Host 里没有端口时按同源放行，外层再加网关也不必额外配置。**这个 403 与账号、口令、数据库都无关**，不要往那个方向排查。

## 5. 对象存储连通自检（测试环境）

测试环境 `STORAGE_BACKEND` 固定为 `tos`，缺凭据时启动与自检都会明确失败，不会静默回落本地 Mock。

在后端容器内执行，验证 TOS 凭据与网络连通性（上传 → 预签名可访问 → 下载校验 → 删除）：

```bash
docker compose -f docker/docker-compose.yml exec backend python -m app.storage.verify
# 保留测试对象以便在控制台人工检查
docker compose -f docker/docker-compose.yml exec backend python -m app.storage.verify --keep
```

输出会逐项打印六个环节的结果；失败时打印失败环节与服务端错误的 `request_id`（可用于向火山引擎定位问题），进程退出码非 0。

自检对象的键形如 `{TOS_OBJECT_PREFIX}original/verify-source_<时间戳>_<随机串>.txt`，默认在结束时删除。

## 6. 上传与任务（V0.1.3 起）

前端在 `http://localhost:12439` 的工作台界面选择文件后，按后端下发的分片大小切片并发上传；上传完成后后端合并、写入对象存储 `original/` 前缀，并把任务交给后台执行器。页面可关闭，任务在容器内继续推进；重新打开页面可在侧栏的「历史记录」看到状态，点击某条记录可在工作区查看它的探测结论与切片结果。切片结果只以文字形式呈现（时间范围、时长、体积、状态），界面不提供视频播放与回看。所有 /api 接口自 V0.5 起要求登录：未登录访问会落在登录页，任务列表、上传与下载口一律返回 401。

### 大文件上传自检（测试环境）

后端容器不向宿主机暴露端口，直接用 `curl` 从宿主机试不便；推荐在容器内跑一遍分片上传，确认「分片落盘 → 合并 → 入库 → 任务完成」：

```bash
# 1. 造一个 200MB 的测试文件（比 2GB 快，足以覆盖多分片与末片不足一整片两种情况）
docker compose -f docker/docker-compose.yml exec backend python -c "open('/tmp/probe.ts','wb').write(bytes(200*1024*1024))"

# 2. 走一遍上传链路；脚本按后端下发的分片大小切片，逐片 PUT，最后提交完成
docker compose -f docker/docker-compose.yml exec backend python - <<'PY'
import json, os, urllib.request
CHUNK = 8 * 1024 * 1024
path = "/tmp/probe.ts"
size = os.path.getsize(path)

def call(method, url, data=None, headers=None):
    req = urllib.request.Request(f"http://localhost:12439{url}", data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)

status, created = call("POST", "/api/uploads", json.dumps({"filename": "probe.ts", "size": size}).encode(),
                       {"Content-Type": "application/json"})
upload_id, task_id, chunk_size = created["upload_id"], created["task_id"], created["chunk_size"]
with open(path, "rb") as fh:
    index = 0
    while True:
        block = fh.read(chunk_size)
        if not block:
            break
        call("PUT", f"/api/uploads/{upload_id}/chunks/{index}", block, {"Content-Type": "application/octet-stream"})
        index += 1
status, done = call("POST", f"/api/uploads/{upload_id}/complete")
print("upload:", status, done)
print("task:", call("GET", f"/api/tasks/{task_id}")[1])
PY
```

预期：`upload` 返回 200 且带 `object_key`；`task` 的 `status` 为 `succeeded`、`object_key` 与上传返回一致、`size` 等于文件大小，且 `metadata` 不为 `null`（V0.1.4 起，探测成功才会有）。

上面的脚本用全零字节充当视频，只覆盖「分片落盘 → 合并 → 入库 → 任务流转」；V0.1.4 起探测会接手处理，这类假文件会被判为探测失败（`status=failed`，原因指向无法识别媒体流或缺少音视频流），这属于预期。要用真实素材验证端到端，请上传一场真实 TS 录屏。

### 排查要点（上传与任务）

- **上传失败**：`GET /api/uploads/{upload_id}` 与 `GET /api/tasks/{task_id}` 的 `error` 字段给出原因；服务端存储错误会带 `request_id`，可用它向火山引擎定位。分片仍保留在 `MEDIA_ROOT/chunks/{upload_id}/`，重新提交分片即可继续，不必重传整个文件。
- **缺片**：完成接口返回 409 且带 `missing_chunks`，按该列表补齐后重新提交完成即可。
- **任务停在 `uploaded`**：说明后台执行器未推进。检查容器日志有无执行器异常；重启容器会按该状态重新入队（`requeue_pending`）。
- **磁盘占用**：未完成的上传会占用宿主机 `backend/media`。确认无人续传后可删除 `MEDIA_ROOT/chunks/` 下对应会话目录。

## 7. 媒体探测（V0.1.4 起）

上传完成后任务自动执行 ffprobe 探测：后端优先使用上传留下的本地副本（`/app/media/originals/{任务 id}{后缀}`），副本缺失时按对象键从 TOS 取回，再写入元数据。**探测成功后本地副本不再删除**（V0.1.5 起）：紧接着的转封装与切分要用它，删掉就得先花几十分钟从 TOS 取回 2GB 文件。副本与转封装产物在切分成功后一并释放；任一步失败则保留，便于用同一份文件复现问题。

任务接口的 `metadata` 字段是探测结论（时长、分辨率、帧率、音视频编码、容器、流数、码率、内容 sha256），`probed_at` 非空表示探测成功过。未探测成功的任务该字段为 `null`，前端不展示媒体信息。

### 探测自检与定位（测试环境）

对某个文件或某个任务的本地副本直接跑一遍探测，用于区分「环境/工具问题」与「媒体文件本身的问题」：

```bash
# 按任务 id 前缀探测其本地副本（副本已清理时提示文件不存在）
docker compose -f docker/docker-compose.yml exec backend python -m app.media.cli <任务 id 前缀>

# 按文件路径探测（例如容器内可直接读到的一份素材）
docker compose -f docker/docker-compose.yml exec backend python -m app.media.cli /app/media/originals/<任务 id>.ts
```

输出逐条打印流信息与摘要；探测失败时打印 ffprobe 给出的原因，进程退出码非 0。元数据抽查核对（时长、分辨率、编码与实际媒体是否一致）也用它。

### 排查要点（探测）

- **任务失败且原因为「未找到 ffprobe」**：镜像内没有 ffmpeg 或 `FFPROBE_PATH` 被改错。确认容器内 `ffprobe -version` 可用。
- **任务失败且原因为「探测超时」**：`-count_packets` 需要完整读一遍文件，长录屏耗时随之增长。确认 `PROBE_TIMEOUT_SECONDS` 与磁盘读取速度是否足够；必要时先提高超时再排查 IO。
- **任务失败且原因为「没有音频或视频流」**：探测到的不是可播放的录屏素材，检查上传的文件本身。
- **任务失败且原因为「本地副本缺失/存储取回失败」**：本地副本已被清理且对象存储取不回。先用 `app.media.cli <任务 id>` 确认副本是否真的不存在，再核对 TOS 中该对象键是否存在（服务端错误会带 `request_id`）。
- **本地副本占磁盘**：处理失败的任务会保留副本与转封装产物。确认不再复现后，删除 `/app/media/originals/` 与 `/app/media/converted/` 下对应文件即可；删除任务会一并清理。
- **任务停在 `processing`**：进程重启后这类任务会在启动时自动退回 `uploaded` 并重新入队（`reclaim_stale_processing`）。若反复出现，检查容器是否被 OOM 杀掉。

## 7.1 预处理与切分（V0.1.5 起）

探测成功后任务继续执行：**转封装为 MP4**（源已是 MP4 则跳过）→ 按「时长 + 体积」双约束**切分**→ 逐片**上传**到 TOS `clip/` 前缀 → **整场覆盖校验**→ 清理本地产物。全程 `-c copy` 不重编码，且始终带 `-map 0:v -map 0:a?`，音频不会被丢掉。

**转封装产物比源素材短是正常的**：直播中途断流会在源 TS 里留下数秒到数十秒的时间戳空洞，容器标称时长把这些空洞算了进去，而转封装产物的时间轴是连续的。实测一份素材：源标称 1683.2s、产物标称 1649.7s，但两者的**可播放时长**分别是 1638.1s 与 1638.2s——**时长校验因此比的是可播放时长**（逐包读 PTS，跨度为负的乱序包按 B 帧处理，超过阈值的时间戳跳变算空洞、不计入），而不是容器标称时长。转封装会逐包统计源与产物两侧，多花约十秒；产物比源短超过时长比例容差（不低于 1s）才判失败。切分与后续识别一律以**产物时长**为时间基准。

任务接口的 `clips` 字段按序号（即原视频时间顺序）列出片段，含时间区间、实测时长、体积、状态与预签名下载地址；`coverage` 字段是覆盖校验结论，`checked_at` 非空表示已校验过，`issues` 为空数组表示通过。**覆盖校验发现问题时任务判失败**，问题逐条写在 `coverage.issues` 与任务 `error` 中（例如「第 1 片与第 2 片之间缺失 37.5s」）。

处理过程中本地会同时存在：原始副本、转封装产物（`converted/{任务 id}.mp4`）与切片产物（`clips/{任务 id}/clip_NNN.mp4`，上传成功后即删）。峰值磁盘约为原视频的 2～3 倍，按 2GB 素材预留 6GB 以上。

### 切分自检与定位（测试环境）

```bash
# 看某任务的处理进度与片段结论
docker compose -f docker/docker-compose.yml exec backend python - <<'PY'
import json, urllib.request
task = json.load(urllib.request.urlopen("http://localhost:12439/api/tasks/<任务 id>"))
print("状态", task["status"], "进度", task["progress"], "失败原因", task["error"])
print("切片数", len(task["clips"]), "覆盖校验", task["coverage"])
for clip in task["clips"]:
    print(clip["index"], clip["start_seconds"], clip["end_seconds"], clip["size_bytes"], clip["status"])
PY

# 直接核对容器内某个片段的音视频流与时长（副本/片段已清理时提示文件不存在）
docker compose -f docker/docker-compose.yml exec backend python -m app.media.cli /app/media/clips/<任务 id>/clip_000.mp4
```

抽查要点：**片段确实有音频流**（`app.media.cli` 输出里能看到 audio 流）、时长与接口给出的区间一致、体积不超过 `SPLIT_MAX_CLIP_BYTES`、序号与时间区间递增且首尾相接。

### 排查要点（切分）

- **任务失败且原因为「未找到 ffmpeg」**：容器内 `ffmpeg -version` 不可用或 `FFMPEG_PATH` 被改错。
- **任务失败且原因为「转封装产物时长与原始视频偏差过大」**：按**可播放时长**比过之后，产物仍比源短出容差，属于真的截断或时间轴异常——先确认源文件本身是否完整（`app.media.cli` 直接探测原始副本），再决定是否需要人工处理素材。若日志里出现「未能量出可播放时长，本次按容器标称时长比对」，说明逐包统计没跑成（此时标称时长含空洞，这条失败先按假阳性处理），需检查 ffprobe 能否正常枚举包。
- **任务失败且原因为「丢失了声音」**：转封装或切分产物缺音频流，源素材可能是纯视频。先探测原始副本确认它到底有没有音频流。
- **任务失败且原因为「片段体积超过上限…无法在满足体积约束的同时保留可用片段」**：`SPLIT_MAX_CLIP_BYTES` 相对素材码率过小，或 `SPLIT_MIN_CLIP_SECONDS` 设置过大。按素材调整这两个值后重试。
- **任务失败且原因为「覆盖校验发现 N 处问题」**：片段之间存在缺口、重叠或首尾未覆盖。问题信息里给出了具体的秒数与相邻片段序号；调大 `SPLIT_MAX_DURATION_SECONDS` 减少交界数量常能规避由关键帧对齐引起的边界偏差。
- **重试会不会重传已有片段**：不会。已成功上传的片段按序号复用、对象键不变，重试只补齐未完成的部分。若调整了切分参数导致片段区间变化，旧片段的对象会被删除并重新切分上传。
- **对象存储里只有片段、没有转封装产物**：符合预期。转封装产物是容器内的中间态，只有切分后的 MP4 片段会上传到 `clip/` 前缀下。

## 7.2 语音识别（V0.2 起）

切分成功后即可在工作区点击**开始识别语音**，或对历史任务启动识别。后端逐片取预签名视频地址、调用音画理解模型识别片段中的人声语音，把结果按**原视频时间轴**落库（片段内相对时间 + 片段起点）。识别是后台任务：关掉页面不影响执行，回来看历史记录即可。

识别结果有三处可核对：

- 任务接口的 `understanding` 字段是整场汇总（状态、已识别片段数、语音条数、失败片段数与模型名）；
- `clips[].understanding_status` 是逐片状态与失败原因，失败的那一片可在界面上**单独重新识别**；
- `GET /api/tasks/{id}/transcript` 是整场语音记录汇总（含按时间排序的条目与拼好的全文），`/transcript.txt` 以 `text/plain` 下载同一份全文。

三条与验收直接相关的语义：

- **已成功的片段不会重复识别**：重复点击「开始识别语音」只为未完成与失败的片段发新请求，不会把同一批片段重新计费一遍。
- **单片失败不牵连其余片段**：整场状态标为「部分失败」，失败原因写在片段的 `understanding_error` 上（服务端错误带 `request_id`）。
- **缺口显式可见**：识别失败的片段在全文里留一行括注而不是被跳过；模型返回但被解析丢弃的条目记在片段的 `understanding_warnings` 上。

### 识别自检（测试环境）

```bash
# 确认模型配置已进入容器（只打印缺失项名称，不回显取值）
docker compose -f docker/docker-compose.yml exec backend python -c "from app.config import get_settings as g; s=g(); print(s.llm_configured, s.llm_missing_fields)"

# 启动整场识别（后台执行，接口立即返回 202）
docker compose -f docker/docker-compose.yml exec backend python -c "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://localhost:12439/api/tasks/<任务 id>/understanding', data=b'', method='POST')).status)"

# 看逐片识别状态与整场汇总
docker compose -f docker/docker-compose.yml exec backend python - <<'PY'
import json, urllib.request
task = json.load(urllib.request.urlopen("http://localhost:12439/api/tasks/<任务 id>"))
print("汇总", task["understanding"])
for clip in task["clips"]:
    print(clip["index"], clip["understanding_status"], clip["understanding_segment_count"], clip["understanding_error"])
PY

# 取回整场全文，人工核对识别质量
docker compose -f docker/docker-compose.yml exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:12439/api/tasks/<任务 id>/transcript.txt').read().decode('utf-8')[:2000])"
```

**识别质量必须人工核查**：本版验收要求用包含密集讲解、报价、产品展示与互动的真实片段，逐条比对语音是否遗漏、价格与数字是否准确。自动化测试只覆盖编排、解析与失败隔离，不覆盖模型答得准不准。

### 排查要点（识别）

- **接口返回 503 且列出缺失项**：模型配置未进入容器。对照上文「配置注入」检查 `backend/.env`，注意 compose 的 `environment` 优先级高于 `env_file`。
- **片段失败且原因为 `LLMTimeoutError`**：单次请求超时。先确认 `LLM_TIMEOUT_SECONDS` 是否够用，再确认片段是否过长（调小 `SPLIT_MAX_DURATION_SECONDS` 能切出更短的片段，识别更快但片段数更多）。
- **片段失败且原因为「模型服务返回可重试错误」**：限流或服务端故障，错误里带 `status_code`、`code` 与 `request_id`。`LLM_MAX_ATTEMPTS` 次退避重试仍失败才会落库为失败，可稍后单片重试。
- **片段失败且原因为「模型输出无法解析」**：模型没有返回可用的 JSON 数组。片段的 `understanding_raw_text` 保留了原始返回，据此判断是题面问题（`LLM_UNDERSTANDING_PROMPT`）还是模型侧问题。
- **片段失败且原因为「片段对象地址生成失败」**：预签名 URL 生成失败，多为 TOS 凭据或网络问题；先按第 5 节跑一次对象存储连通自检。
- **识别成功但语音条数明显偏少**：查看片段上是否有 `understanding_warnings`（被丢弃的条目）与 `out_of_range` 标记（时间越界，本系统只标记不裁切）。这两类都要在人工核查时单独看，不要当成模型没识别到。
- **耗时与用量**：`clips[].understanding_attempts` 是实际发出的请求次数（判断重试是否被触发过），片段行的 `understanding_usage_json` 保留每次调用的 token 用量，任务行是 `understanding_started_at` / `understanding_finished_at`。整场耗时约等于片段数 × 单片耗时 ÷ `LLM_CONCURRENCY`，据此估算是否值得提高并发。
- **重启后识别自动续跑**：进程重启会把被打断的识别退回可续跑状态并重新入队（`reclaim_interrupted_understanding`），只补未完成的片段——已成功的片段不重复请求，因此重启不产生额外模型费用。**从未启动过识别的任务不会被自动拉起**（识别要花钱，必须由用户明确点一次）。若某场识别在重启后停在「未开始」，检查容器日志有无「终态检查」的 warning：模型未配置时恢复会跳过而不是反复失败。
- **任务停在 `running`（识别中）不动**：正常情况重启一次即可恢复（见上一条）。若重启后仍卡住，检查容器日志有无恢复阶段的异常；状态落库不完整（如 `understanding_started_at` 有值但片段行缺失）时应把该任务的识别重置后重新点击启动。

## 7.3 复盘分析（V0.3 起）

整场识别完成后即可在工作区点击**开始复盘分析**。后端把整场语音转写（带时间戳与语气标注）交给文本模型，按裁剪后的《直播复盘分析准则》产出结构化结论。复盘也是后台任务：关掉页面不影响执行。

**前置条件是「有识别成功的语音记录」**：识别还没跑过时接口返回 409 并给出可操作的原因，不会入队一个注定被跳过的任务。识别有失败片段**不阻断**复盘——缺口会写进结论的分析等级与「本场不足以判断」清单。

结论有三处可核对：

- 任务接口的 `review` 字段是状态与覆盖面（状态、进度、投入分析的语音条数、调用批次、模型、起止时间）；
- 同字段下的 `review.result` 是结构化结论（一句话结论、分析等级与定级依据、关键事件、分析发现、TOP 问题、下一场动作、缺口清单、归一告警）；
- `GET /api/tasks/{id}/review.md` 是可下载的 Markdown 报告，与页面同源渲染。

三条与验收直接相关的语义：

- **分析等级由程序兜底**：模型看不到识别缺口，因此成功率低于 50% 时强制判「受限」，有失败片段时不得判「完整」。等级与降级理由都写在结论里。
- **长转写分批但不摘要**：单批上限见 `REVIEW_INPUT_CHARS_PER_CALL`，超出则按记录边界分批调用；合并是结构性的（去重、按时间排序、按准则截断 TOP3），不额外发起摘要调用。
- **复盘覆盖而不是追加**：换提示词或补完失败片段后重跑，结论以最后一次为准，不留历史版本。

### 复盘自检（测试环境）

```bash
# 启动整场复盘（后台执行，接口立即返回 202）
docker compose -f docker/docker-compose.yml exec backend python -c "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://localhost:12439/api/tasks/<任务 id>/review', data=b'', method='POST')).status)"

# 看复盘状态与结构化结论
docker compose -f docker/docker-compose.yml exec backend python - <<'PY'
import json, urllib.request
task = json.load(urllib.request.urlopen("http://localhost:12439/api/tasks/<任务 id>"))
review = task["review"]
result = review["result"]
print("状态", review["status"], "等级", result["analysis_level"] if result else None)
print("依据", result["level_reason"] if result else review["error"])
for issue in (result or {}).get("top_issues", []):
    print("问题", issue["problem"], "→", issue["action"])
PY

# 下载 Markdown 报告
docker compose -f docker/docker-compose.yml exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:12439/api/tasks/<任务 id>/review.md').read().decode('utf-8')[:2000])"
```

**复盘质量必须人工评估**：本版验收要求用真实整场直播检查结论是否忠实于素材、建议是否具体可执行。自动化测试只覆盖编排、解析、合并与失败隔离，不覆盖模型分析得好不好。

### 排查要点（复盘）

- **接口返回 503 且列出缺失项**：与识别共用模型配置，检查方式同 7.2。
- **接口返回 409「还没有任何识别成功的片段」**：本场识别未跑或全部失败。先按 7.2 补齐识别再复盘。
- **状态为 `skipped`**：识别成功但没有任何语音记录（全片识别为空）。此时**不会发请求**，也不会给出空结论——先检查识别结果是否合理。
- **状态为 `failed` 且原因为 `LLMTimeoutError`**：单次复盘请求超时。长直播应调大 `REVIEW_TIMEOUT_SECONDS`，或调小 `REVIEW_INPUT_CHARS_PER_CALL` 让每批更短、批数更多。
- **状态为 `failed` 且原因为「复盘输出无法解析」**：模型没有返回可用的 JSON 对象。任务行的 `review_raw_text` 保留了**各批的原始返回**，据此判断是提示词问题还是模型侧问题。
- **结论里有「解析告警」**：多为 `top_issues` / `next_actions` 超出准则条数被截断，属预期行为；若提示某条缺少有效内容被丢弃，说明模型那一批的输出格式异常，可结合 `review_raw_text` 复核。
- **耗时与用量**：`review.batch_count` 是本次调用批数，任务行的 `review_usage_json` 保留各批 token 用量。耗时约等于批数 × 单批耗时。
- **重启后复盘自动续跑**：进程重启会把被打断的复盘退回待跑并重新入队（`reclaim_interrupted_review`）。复盘输入（识别结果）已落库，续跑最多重跑一次文本调用，**不会触发识别或重新计费视频理解**。

## 7.4 上传中断与关页面后的续跑

分片传输由浏览器驱动，原文件在用户磁盘上，**服务端拿不到没传上来的部分**；但「分片收齐之后的合并与入库」是后端的事，不依赖页面是否打开：

- **关页面时已传完最后一片**：分片全部在服务端，容器下次启动时会自动把这次上传做完（合并 → 入库 → 建任务 → 入队处理），用户回来后从历史记录里看到结果即可，不必重传。
- **关页面时还有分片没传**：缺的部分只能由浏览器补。前端会把当前上传会话留在浏览器本地，重开页面时提示「这次上传还没传完」，用户选一次同一个文件即补传缺片，已传分片不会重传。
- **放弃一次未传完的上传**：点「放弃这次上传」只清掉浏览器本地凭据，服务端的分片仍留在宿主机 `backend/media` 下；确认无人续传后按第 6 节「磁盘占用」清理。

### 排查要点（上传恢复）

- **重开页面没提示续传**：浏览器本地没有该会话的凭据（换浏览器、隐私模式、清过站点数据，或距上次中断超过 24 小时），或该会话已被删除。此时重新上传即可，服务端残留的分片按上文清理。
- **上传停在「正在合并并写入对象存储」很久**：GB 级文件合并与上传耗时随体积增长，这一段的进度只在完成后才落库，属预期。查看容器日志中「上传会话 … 完成 / 恢复未完成的上传会话」确认是否在推进。
- **容器启动日志出现「恢复上传会话 … 失败」**：该会话入库失败，原因已写入会话与任务的 `error`，按第 6 节排查后从界面重新发起；分片保留，不需要重传整个文件。

## 8. 表结构变更与升级

表在启动时用 `create_all` 建立，只建缺失的表、不改已存在的表；随后 `init_db` 会再跑一次补列：把模型里有、库里没有的**可空或有默认值**的新列用 `ALTER TABLE ADD COLUMN` 加上。因此新增表与新增可选字段都能直接升级——直接重启后端即可，不必删库：

```bash
docker compose -f docker/docker-compose.yml build backend
docker compose -f docker/docker-compose.yml up -d
```

补列只做加法：不改已有列的类型、可空性与含义，也不删除列。还有两类变更补列处理不了，必须删库重建：

- 新增**既不可空又无默认值**的列（补列时会明确报错并给出列名，不会静默跳过）；
- 改列语义（改类型、改可空性、重命名列），或改动已有数据的含义。

```bash
# 数据库就在宿主机 backend/data/liverreview.db（容器内看到的 /app/data 是同一个文件），
# 因此直接在宿主机删除即可，不必进容器
rm -f backend/data/liverreview.db
docker compose -f docker/docker-compose.yml restart backend
```

删库后账号也一并消失，**需要重新执行一次 `python -m app.auth.init_admin`**（见 §4.1）。

删除数据库会一并丢掉历史任务与探测结果（对象存储中的原始视频不受影响，但记录与对象的对应关系会丢失，需要重新上传）。

**从命名卷切到宿主机目录（本次变更）**：老部署的数据库与媒体原在命名卷 `liverreview-data` / `liverreview-media` 里，换成绑定时容器会改读宿主机目录，卷里的东西不会自动搬过来——**换成绑定后必须重新建号**（旧卷里的账号与历史任务留在卷中，确认不再需要可 `docker volume rm` 删掉）。若打算保留旧数据，先在旧卷上执行一次 `init_admin`，再用 `docker compose cp` 把 `/app/data/liverreview.db` 拷到宿主机 `backend/data/`：

```bash
# 用旧配置（命名卷）起一次容器，建号后拷出来
docker compose -f docker/docker-compose.yml exec backend python -m app.auth.init_admin
docker compose -f docker/docker-compose.yml cp backend:/app/data/liverreview.db backend/data/liverreview.db
```

跨版本升级的注意点：V0.1.4 的任务在探测成功后已删除本地副本，这类任务重试时会按对象键从 TOS 取回原始视频再处理，属正常路径，只是多一次下载。V0.1.5 之前创建的任务在清单里没有切片记录，重试时会重新切分并上传，桶里多出来的旧对象不会被自动回收，需要时人工清理。
