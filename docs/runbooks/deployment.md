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
- 上传分片与合并临时文件落在 `MEDIA_ROOT`（本地默认 `./media`，容器内 `/app/media`），`UPLOAD_CHUNK_SIZE` 是下发给前端的分片大小（字节，默认 8MB）。该目录不是用户配置项之外的业务数据，清理它只影响未完成的上传。

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

后端镜像内 ffmpeg 已安装，媒体与数据通过命名卷 `liverreview-media`、`liverreview-data` 持久化。

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

前端在 `http://localhost:12439` 的「上传录屏」页选择文件后，按后端下发的分片大小切片并发上传；上传完成后后端合并、写入对象存储 `original/` 前缀，并把任务交给后台执行器。页面可关闭，任务在容器内继续推进；重新打开页面可在任务列表看到状态。

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

预期：`upload` 返回 200 且带 `object_key`；`task` 的 `status` 为 `succeeded`、`object_key` 与上传返回一致、`size` 等于文件大小。

### 排查要点

- **上传失败**：`GET /api/uploads/{upload_id}` 与 `GET /api/tasks/{task_id}` 的 `error` 字段给出原因；服务端存储错误会带 `request_id`，可用它向火山引擎定位。分片仍保留在 `MEDIA_ROOT/chunks/{upload_id}/`，重新提交分片即可继续，不必重传整个文件。
- **缺片**：完成接口返回 409 且带 `missing_chunks`，按该列表补齐后重新提交完成即可。
- **任务停在 `uploaded`**：说明后台执行器未推进。检查容器日志有无执行器异常；重启容器会按该状态重新入队（`requeue_pending`）。
- **磁盘占用**：未完成的上传会占用 `liverreview-media` 卷。确认无人续传后可删除 `MEDIA_ROOT/chunks/` 下对应会话目录。
- **数据库表结构变更**：表在启动时用 `create_all` 建立，只建缺失的表。升级到新增了表或字段的版本后，需删除 `liverreview-data` 卷中的 SQLite 文件再重启（测试环境数据可重建）。
