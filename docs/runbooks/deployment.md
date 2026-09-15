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

## 5. 对象存储连通自检（测试环境）

在后端容器内执行，验证 TOS 凭据与网络连通性（上传 → 预签名可访问 → 下载校验 → 删除）：

```bash
docker compose -f docker/docker-compose.yml exec backend python -m app.storage.verify
# 保留测试对象以便在控制台人工检查
docker compose -f docker/docker-compose.yml exec backend python -m app.storage.verify --keep
```

输出会逐项打印六个环节的结果；失败时打印失败环节与服务端错误的 `request_id`（可用于向火山引擎定位问题），进程退出码非 0。

自检对象的键形如 `{TOS_OBJECT_PREFIX}original/verify-source_<时间戳>_<随机串>.txt`，默认在结束时删除。
