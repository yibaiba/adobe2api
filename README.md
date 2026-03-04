# adobe2api

Adobe Firefly / OpenAI 兼容网关服务。

English README: `README_EN.md`

当前设计：

- 对外统一入口：`/v1/chat/completions`（图像 + 视频）
- 可选图像专用接口：`/v1/images/generations`
- Token 池管理（手动 Token + 自动刷新 Token）
- 管理后台 Web UI：Token / 配置 / 日志 / 刷新配置导入

## 1）运行服务

安装依赖：

```bash
pip install -r requirements.txt
```

启动服务（在 `adobe2api/` 目录下执行）：

```bash
uvicorn app:app --host 0.0.0.0 --port 6001 --reload
```

打开管理后台：

- `http://127.0.0.1:6001/`
- 默认管理员账号密码：`admin / admin`
- 登录后可在「系统配置」修改，或直接编辑 `config/config.json`（`admin_username`、`admin_password`）

### Docker 部署

本项目支持 Docker 和 Docker Compose。

构建并运行（Docker）：

```bash
docker build -t adobe2api .
docker run -d --name adobe2api \
  -p 6001:6001 \
  -e TZ=Asia/Shanghai \
  -e PORT=6001 \
  -e ADOBE_API_KEY=clio-playground-web \
  -v ./data:/app/data \
  -v ./config:/app/config \
  adobe2api
```

使用 Compose 启动：

```bash
docker compose up -d --build
```

Compose 文件：`docker-compose.yml`

## 2）服务鉴权

服务 API Key 配置在 `config/config.json` 的 `api_key` 字段。

- 若已设置，调用时可使用以下任一方式：
  - `Authorization: Bearer <api_key>`
  - `X-API-Key: <api_key>`

管理后台和管理 API 需要先通过 `/api/v1/auth/login` 登录并持有会话 Cookie。

## 3）外部 API 使用

### 3.0 支持的模型族

当前支持如下模型族：

- `firefly-nano-banana-*`（图像，对应上游 `nano-banana-2`）
- `firefly-nano-banana2-*`（图像，对应上游 `nano-banana-3`）
- `firefly-nano-banana-pro-*`（图像）
- `firefly-sora2-*`（视频）
- `firefly-sora2-pro-*`（视频）
- `firefly-veo31-*`（视频）
- `firefly-veo31-ref-*`（视频，参考图模式）
- `firefly-veo31-fast-*`（视频）

Nano Banana 图像模型（`nano-banana-2`）：

- 命名：`firefly-nano-banana-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- 示例：
  - `firefly-nano-banana-2k-16x9`
  - `firefly-nano-banana-4k-1x1`

Nano Banana 2 图像模型（`nano-banana-3`）：

- 命名：`firefly-nano-banana2-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- 示例：
  - `firefly-nano-banana2-2k-16x9`
  - `firefly-nano-banana2-4k-1x1`

Nano Banana Pro 图像模型（兼容旧命名）：

- 命名：`firefly-nano-banana-pro-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- 示例：
  - `firefly-nano-banana-pro-2k-16x9`
  - `firefly-nano-banana-pro-4k-1x1`

Sora2 视频模型：

- 命名：`firefly-sora2-{duration}-{ratio}`
- 时长：`4s` / `8s` / `12s`
- 比例：`9x16` / `16x9`
- 示例：
  - `firefly-sora2-4s-16x9`
  - `firefly-sora2-8s-9x16`

Sora2 Pro 视频模型：

- 命名：`firefly-sora2-pro-{duration}-{ratio}`
- 时长：`4s` / `8s` / `12s`
- 比例：`9x16` / `16x9`
- 示例：
  - `firefly-sora2-pro-4s-16x9`
  - `firefly-sora2-pro-8s-9x16`

Veo31 视频模型：

- 命名：`firefly-veo31-{duration}-{ratio}-{resolution}`
- 时长：`4s` / `6s` / `8s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p` / `720p`
- 最多支持 2 张参考图：
  - 1 张：首帧参考
  - 2 张：首帧 + 尾帧参考
- 音频默认开启
- 示例：
  - `firefly-veo31-4s-16x9-1080p`
  - `firefly-veo31-6s-9x16-720p`

Veo31 Ref 视频模型（参考图模式）：

- 命名：`firefly-veo31-ref-{duration}-{ratio}-{resolution}`
- 时长：`4s` / `6s` / `8s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p` / `720p`
- 始终使用参考图模式（不是首尾帧模式）
- 最多支持 3 张参考图（映射到上游 `referenceBlobs[].usage="asset"`）
- 示例：
  - `firefly-veo31-ref-4s-9x16-720p`
  - `firefly-veo31-ref-6s-16x9-1080p`
  - `firefly-veo31-ref-8s-9x16-1080p`

Veo31 Fast 视频模型：

- 命名：`firefly-veo31-fast-{duration}-{ratio}-{resolution}`
- 时长：`4s` / `6s` / `8s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p` / `720p`
- 最多支持 2 张参考图：
  - 1 张：首帧参考
  - 2 张：首帧 + 尾帧参考
- 音频默认开启
- 示例：
  - `firefly-veo31-fast-4s-16x9-1080p`
  - `firefly-veo31-fast-6s-9x16-720p`

### 3.1 获取模型列表

```bash
curl -X GET "http://127.0.0.1:6001/v1/models" \
  -H "Authorization: Bearer <service_api_key>"
```

### 3.2 统一入口：`/v1/chat/completions`

文生图：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "messages": [{"role":"user","content":"a cinematic mountain sunrise"}]
  }'
```

图生图（在最新 user 消息中传入图片）：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"turn this photo into watercolor style"},
        {"type":"image_url","image_url":{"url":"https://example.com/input.jpg"}}
      ]
    }]
  }'
```

文生视频：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-sora2-4s-16x9",
    "messages": [{"role":"user","content":"a drone shot over snowy forest"}]
  }'
```

Veo31 单图语义说明：

- `firefly-veo31-*` / `firefly-veo31-fast-*`：帧模式
  - 1 张图 => 首帧
  - 2 张图 => 首帧 + 尾帧
- `firefly-veo31-ref-*`：参考图模式
  - 1~3 张图 => 参考图

图生视频：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-sora2-8s-9x16",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"animate this character walking forward"},
        {"type":"image_url","image_url":{"url":"https://example.com/character.png"}}
      ]
    }]
  }'
```

### 3.3 图像接口：`/v1/images/generations`

```bash
curl -X POST "http://127.0.0.1:6001/v1/images/generations" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-4k-16x9",
    "prompt": "futuristic city skyline at dusk"
  }'
```

## 4）管理 API

- `GET /api/v1/tokens`
- `POST /api/v1/tokens`
- `DELETE /api/v1/tokens/{id}`
- `PUT /api/v1/tokens/{id}/status?status=active|disabled`
- `POST /api/v1/tokens/{id}/refresh`
- `GET /api/v1/config`
- `PUT /api/v1/config`
- `GET /api/v1/logs?limit=200`
- `DELETE /api/v1/logs`
- `GET /api/v1/refresh-profiles`
- `POST /api/v1/refresh-profiles/import-cookie`
- `POST /api/v1/refresh-profiles/import-cookie-batch`
- `POST /api/v1/refresh-profiles/{id}/refresh-now`
- `PUT /api/v1/refresh-profiles/{id}/enabled`
- `DELETE /api/v1/refresh-profiles/{id}`

## 5）Cookie 导入说明

导入流程：

1. 打开管理后台「Token 管理」页签
2. 点击「导入 Cookie」
3. 粘贴 Cookie 字符串 / Cookie JSON，或上传 `.txt/.json`
4. 点击「导入 Cookie」（服务会立即自动执行一次刷新）
5. Token 列表中会显示每个刷新配置对应的一条 `自动刷新=是` 的 Token

批量导入说明：

- 导入弹窗支持一次上传多个文件
- 或粘贴 JSON 数组：
  - `[{"name":"account-a","cookie":"k1=v1; k2=v2"}, {"name":"account-b","cookie":[{"name":"k1","value":"v1"}]}]`

## 6）存储路径

- 生成媒体文件：`data/generated/`
- 请求日志：`data/request_logs.jsonl`
- Token 池：`config/tokens.json`
- 服务配置：`config/config.json`
- 刷新配置（本地私有）：`config/refresh_profile.json`

生成媒体保留策略：

- `data/generated/` 下文件会保留，并通过 `/generated/*` 对外访问
- 启用按容量阈值自动清理（最旧文件优先）
  - `generated_max_size_mb`（默认 `1024`）
  - `generated_prune_size_mb`（默认 `200`）
- 当总大小超过 `generated_max_size_mb` 时，服务会删除旧文件，直到至少回收 `generated_prune_size_mb` 且总大小降回阈值以内

## 7）安全建议

- Cookie 数据包含高敏感会话信息。
- 不要提交或分享 Cookie 导出文件。
- 如果敏感信息泄露，请及时轮换 Adobe 会话。
