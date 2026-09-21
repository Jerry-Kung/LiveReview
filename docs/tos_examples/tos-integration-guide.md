# 火山引擎 TOS 对象存储接入指南

> 面向：需要复用本项目对象存储能力的其他项目。
> 依据：本仓库 `app/tools/storage/` 的实际用法与 `docs/design/assets/tos_api_example.py`。

## 1. 用途与场景

本项目用 TOS（火山引擎对象存储）做「本地文件 → 公网 URL」的中转层：

1. 把本地图片/视频上传到 TOS；
2. 生成带有效期的**预签名 URL**；
3. 把该 URL 交给只接受公网 URL 输入的第三方服务（文生视频、图片高清化等）；
4. 第三方服务从 URL 拉取文件，任务结束后删除云上的中转对象。

如果你的项目需要「把本地文件暴露成临时公网 URL 给别的服务消费」，可直接复用这套用法。

## 2. 依赖安装

Python 官方 SDK，版本 `>= 2.6.0`：

```bash
pip install "tos>=2.6.0"
```

> TOS 兼容 S3 协议，理论上也可用 `boto3` 等 S3 客户端对接；本项目采用官方 `tos` SDK，本指南以此为准。

## 3. 前置准备

1. 在火山引擎控制台开通**对象存储 TOS**，创建桶（Bucket）。
2. 在「访问控制 → 访问密钥」获取 **Access Key（AK）** 与 **Secret Key（SK）**。
3. 记录桶所在地域的 **endpoint** 与 **region**。

## 4. 核心概念

| 概念 | 说明 | 本项目取值示例 |
|---|---|---|
| `endpoint` | 访问域名 | `tos-cn-beijing.volces.com` |
| `region` | 地域标识 | `cn-beijing` |
| `bucket` | 桶名 | `bu-tmp` |
| `object_key` | 对象在桶内的完整路径（含前缀） | `tmp/image_1731657645123.png` |
| 预签名 URL | 带签名与过期时间的临时访问链接，GET 用于下载 | 形如 `https://...?...&X-Tos-Expires=3600` |

## 5. 配置

推荐通过环境变量传入（AK/SK 必填，其余带默认值）：

```
TOS_ENDPOINT=tos-cn-beijing.volces.com
TOS_REGION=cn-beijing
TOS_BUCKET=bu-tmp
TOS_OBJECT_PREFIX=tmp/
TOS_PRESIGNED_TTL_SECONDS=3600
TOS_ACCESS_KEY=<你的 AK>
TOS_SECRET_KEY=<你的 SK>
```

## 6. 核心操作

### 6.1 初始化客户端

```python
import tos

client = tos.TosClientV2(ak, sk, endpoint, region)
```

`TosClientV2` 对桶和对象的操作都通过这一个实例完成，线程安全，可全局复用一个实例。

### 6.2 上传文件

```python
client.put_object_from_file(bucket, object_key, local_file_path)
```

### 6.3 生成预签名 URL（下载）

```python
from tos import HttpMethodType

resp = client.pre_signed_url(
    HttpMethodType.Http_Method_Get,
    bucket,
    object_key,
    expires=3600,          # 有效期，单位秒
)
url = resp.signed_url      # 直接取 signed_url 属性
```

### 6.4 删除对象

```python
client.delete_object(bucket, object_key)
```

## 7. 异常处理

SDK 抛出两类异常，务必区分并记录：

| 异常 | 含义 | 关键字段 |
|---|---|---|
| `tos.exceptions.TosClientError` | 客户端/网络类错误（非法参数、连接失败等） | `.message`、`.cause` |
| `tos.exceptions.TosServerError` | 服务端错误（鉴权失败、对象不存在等） | `.code`、`.request_id`、`.message`、`.status_code` |

`TosServerError` 的 `request_id` 可用于向火山引擎定位具体问题，强烈建议写入日志；AK/SK 一律不得写入日志。

```python
from tos.exceptions import TosClientError, TosServerError

try:
    client.put_object_from_file(bucket, key, local_path)
except TosServerError as exc:
    logger.error("TOS server error code=%s request_id=%s message=%s",
                 exc.code, exc.request_id, exc.message)
    raise
except TosClientError as exc:
    logger.error("TOS client error message=%s", exc.message)
    raise
```

## 8. 推荐封装

本项目 `app/tools/storage/client.py` 的封装要点，新项目可参照：

1. **客户端单例**：`TosClientV2` 复用同一实例，用 `threading.Lock` 防止并发重复初始化。
2. **上传 + 预签名合并**：把「上传本地文件 → 返回 `(object_key, signed_url)`」封装成一个方法，业务方只关心拿到的 URL。
3. **object_key 唯一化**：`前缀 + 原文件名 + 毫秒时间戳 + 扩展名`，避免同名覆盖。
4. **用后即删**：中转对象在任务结束（无论成功失败）后放到 `finally` 里删除，清理失败仅记 warning、不影响任务状态。

## 9. 完整示例

可直接运行的自包含脚本见同目录 `docs/tos-integration-example.py`，覆盖初始化、上传、预签名、删除与异常处理的完整流程。

## 10. 注意事项与坑

- **预签名 URL 有时效**：第三方服务必须在 `expires` 秒数内拉取，超时即失效；按下游服务的拉取窗口设置合理的 TTL。
- **AK/SK 是敏感凭据**：只从环境变量读取，禁止硬编码或提交到代码库，禁止打印到日志。
- **中转对象是临时的**：云对象只是中转，本地产物不依赖它；上传后要规划清理时机，桶可配 lifecycle 自动过期规则兜底。
- **object_key 避免重名**：同名前缀会覆盖旧对象，务必带唯一后缀。
