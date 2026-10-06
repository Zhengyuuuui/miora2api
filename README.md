# miora2api

[Miora](https://miora.design)(腾讯 AI 创作平台)的媒体生成 → OpenAI 兼容网关。把 Miora 的图像/视频/3D 生成能力封装为 HTTP API,凭证与产物落 SQLite。

**6 种生成 type 全部实测跑通**,41 个模型清单**免登录**可取。

## 端点

| 端点 | 说明 |
|---|---|
| `GET /v1/models` | 41 个模型清单(24 图像 / 10 视频 / 6 个 3D / 1 UI),**免登录** |
| `GET /v1/tiers` | 11 档 LLM(仅信息,不提供 chat,见下) |
| `POST /v1/images/generations` | 文生图/图生图;`stream:true` 伪流式;`wait:false` 异步入队 |
| `POST /v1/videos/generations` | 文生视频/图生视频;`extra`→`duration` 秒;`wait:false` 异步入队 |
| `POST /v1/3d/generations` | 文生 3D/图生 3D;输出 GLB/FBX/STL/USDZ |
| `GET /v1/tasks/{id}` | 任务进度 + 取签名 URL |
| `GET /v1/media` | 已存产物(含 `url_expired` 标记) |
| `GET /v1/media/{id}/refresh` | 签名 3600s 过期 → 用 `file_key` 重签,**不必重新生成** |
| `GET /v1/costs` | 成本矩阵(按模型/媒体/分辨率聚合) |
| `POST /admin/upload` | 上传参考图 → `fileKey`(支持 `paths`/`urls`/`data_urls`) |
| `POST /admin/browser/launch` | 启动/连接浏览器,自动打开 Miora 页面并后台监听登录 |
| `POST /admin/browser/import` | 读取登录态导入账号(`watch:true`=登录后自动导入) |
| `GET /admin/accounts` · `POST` | 账号池管理 |
| `GET /admin/logs` | 请求日志 |
| `GET /v1/stats` | bridge 状态 + 实时余额 + 官方账单(console 数据源) |
| `GET /console` | **Web 控制台**(总览/模型/生成/产物/成本/账号/账单/日志) |
| `GET /health` | 健康检查 |

## 实测能力(2026-10)

| type | 模型 | 产物 | 成本 | 耗时 |
|---|---|---|---|---|
| `text_to_image` | `gem-3.1` | 1024×1024 JPEG | **6.7 c** | ~13s |
| `image_to_image` | `gem-3.1` | 1024×1024 JPEG | **6.7 c** | ~18s |
| `text_to_video` | `vidu-q2-pro-video` | 1280×704 MP4 5.04s | **54 c** | ~90s |
| `image_to_video` | `vidu-q2-pro-video` | 1440×1440 MP4 5.08s | **54 c** | ~90s |
| `text_to_3d` | `tripo-3d-3.1` | GLB 2.0 / 45MB / 147 万面 / PBR | **11.52 c** | ~60s |
| `image_to_3d` | `tripo-3d-3.1` | GLB 2.0 / 42MB / 149 万面 | **11.52 c** | ~180s |

**图生与文生同价**(官方账单 `billing.usage.reason.*` 核对):image 走独立键 `image.edit`,video/3d 只有 `generate`;多给参考图不额外收费。free 档 41 个模型中 **40 个可用**(仅 `seedance-2.5` 锁死)。

### 参考图上限(随模型变化)

`/v1/models` 每项的 `miora.refInfo` 给出 `{max,min,supported,known,note}`:

| 类型 | 上限 | 模型 |
|---|---|---|
| 图像 | **20** | `hy-image-3.5-f`(实测 21 张被拒 `REFERENCE_IMAGE_COUNT_EXCEEDED`) |
| | 16 | `gpt-image-2.5-{flare,sunburst}` 各 6 档 |
| | 14 | `gem-3.0` / `gem-3.1` |
| | 10 | `seedream-5.0-pro` |
| | 不支持 | `gpt-image-2-{low,medium,high,auto}`、`midjourney` ×4 |
| 视频 | 9 | `seedance-2.0` / `seedance-2.0-fast` / `minimax-h3` |
| | 3 | `kling-o1-video` |
| | 30 | `seedance-2.5`(唯一 `available:false`,未实测) |
| | 仅首帧 1 张 | `minimax-h3-max`/`kling-3.0-video`/`hailuo-2.3`/`vidu-q2`/`vidu-q3` |
| 3D | 未声明 | capabilities 无 `referenceImagesRange` |

网关在**提交前**按上限校验,超限返回 400 且**不扣 credits**。

## 认证(上游 Miora)

Miora 海外站仅支持 **Google / GitHub OAuth** 登录,凭证写在 `localStorage`:

| 键 | 说明 |
|---|---|
| `authToken` | RS256 JWT,`iss=ardot.ai`(腾讯统一身份),有效期 15 天 |
| `refreshToken` | 814 字符 opaque,用于自动续期 |
| `miora__user_info` | `userId` / `email` / `plan` / `cr`(`cr` 字段不可信,以 `/quota/credit` 为准) |

### 方式 A:浏览器登录导入(推荐)

console「账号 → 浏览器登录导入」,或:

```bash
# 1) 启动独立 profile 的浏览器(干净,不影响日常账号);端口占用会自动降级为 attach
curl -X POST localhost:4690/admin/browser/launch -H 'Content-Type: application/json' \
  -d '{"mode":"launch","port":9222}'
#    → 弹出 Chrome,你在里面登录 miora.design
#    → 若该浏览器已登录则**立即自动导入**;否则后台监听,登录成功自动导入

# 2) 需要时可手动触发一次
curl -X POST localhost:4690/admin/browser/import -H 'Content-Type: application/json' -d '{"port":9222}'

# 3) 关闭并清理临时 profile
curl -X POST localhost:4690/admin/browser/close?session_id=launch-9222
```

也支持 `mode:"attach"` 连接已在运行的调试端口(需以 `--remote-debugging-port=<port>` 启动)。**全程只读 localStorage,不接触密码。**

### 方式 B:手动粘贴

DevTools → Application → Local Storage 取 `authToken` / `refreshToken` / `miora__user_info.userId`:

```bash
curl -X POST localhost:4690/admin/accounts -H 'Content-Type: application/json' \
  -d '{"id":"<userId>","auth_token":"<authToken>","refresh_token":"<可选>","label":"my acct"}'
```

401 时 bridge 会自动用 `refresh_token` 走 `POST /api/auth/refresh-token` 续期并重试;换不到才冷却账号 15 分钟。

### 参考图上传(COS 直传)

```
1. GET /api/ai/cos/sts-tokens  → 临时凭证(30min)
   实测 bucket=miora-intl-private-1411336493  region=ap-singapore
   prefix=file/{userId}/{yyyy}/{mm}/{dd}/{token24}
2. PUT https://{bucket}.cos.{region}.myqcloud.com/{prefix}/{ms}-{rand}.{ext}
   → fileKey  ← 直接喂给 image_to_image / image_to_video / image_to_3d
3. 读产物:POST /api/ai/cos/cdn-sign-url(3600s)
```

> 手写 COS V5 签名会踩 `SignatureDoesNotMatch` → `AccessDenied`,直接用官方 SDK `cos-python-sdk-v5`。

## 运行

```bash
pip install -r requirements.txt      # Windows: pip install -r requirements.txt
python bridge.py                     # → http://127.0.0.1:4690
```

控制台:浏览器打开 `http://127.0.0.1:4690/console`。

环境变量:`MIORA_DB` 指定数据库路径(默认同目录 `miora.db`);`PORT` 见 `bridge.py` 顶部常量。

## Cherry Studio(及其它 OpenAI 兼容客户端)

图像/视频/3D 生成走标准 REST,但**不是 chat 协议**——Miora 的 LLM 是黑盒 Agent(见下),因此 `/v1/chat/completions` **未实现**。Cherry Studio 的「文生图」类插件可直接指向本 bridge 的 `/v1/images/generations`。

⚠️ **能力上限(重要)**:
- **Miora 的 LLM 无法反代为 chat**。它是**有状态云端 Agent**:每请求需 `runtimeId`(绑 projectId)+ `acpLink` 临时 token;工具调用需人工审批(Socket.IO `TOOL_CONFIRMATION_PENDING`);对话模型是别名(`standard→balanced-model`,真实上游不可见)。协议走 ACP(Agent Client Protocol)JSON-RPC,**无状态转换路径不存在**,故不做。
- 需要通用对话请直接用 OpenAI / Anthropic 等官方 API。
- 参考图上传链路虽然通了,但 `image_edit` 等 type 的**图生**能力受各模型 `refInfo` 上限约束(见上表)。

## SQLite

`accounts` 表结构对齐 `meshy2api/exa_pool.db` 约定,便于多项目统一管理。

| 表 | 作用 | 要点 |
|---|---|---|
| `accounts` | 多账号 | `auth_token`/`refresh_token`/余额/冷却/权重/请求统计 |
| `tasks` | 图像/视频/3D 统一任务 | `task_kind`(六种 type)、队列信息、原始响应 |
| `media` | 产物 | **同时存 `file_key`(永久)与 `signed_url`(3600s)** |
| `cost_matrix` | 成本观测 | 支撑性价比决策 |
| `request_log` | 请求审计 | 含错误码,便于定位风控/业务失败 |

> 3D 产物的 `width`/`height` 列存的是**三角面数 / 顶点数**(GLB 无像素尺寸)。

### 产物 URL 三层

| 层 | 格式 | 有效期 |
|---|---|---|
| **fileKey** | `file/{userId}/{yyyy}/{mm}/{dd}/{token}/video/xxx.mp4` | **永久** |
| COS 直连 | `https://{bucket}.cos.{region}.myqcloud.com/{fileKey}` | 需 STS |
| CDN 签名 | `https://sign.miorausercontent.com/{fileKey}?sign=..&t=..` | **3600s** |

`media` 表两者都存:**URL 过期后用 `file_key` 重签即可**,不必重新生成。

## Windows 部署

要求:**Python ≥ 3.10**。

```bat
:: 1. 安装依赖
pip install -r requirements.txt

:: 2. 启动
python bridge.py

:: 3. 导入账号(浏览器自动导入,见上)
:: 4. 打开控制台
::    浏览器访问 http://127.0.0.1:4690/console
```

浏览器路径自动探测(优先用户级安装,免管理员权限);也可在 launch 时手动指定。

## 实测坑位(13 条,避免重犯)

1. **三种媒体 `capabilities` 结构完全不同**:image 用 `supportedTools`/`allowedResolutions`;video 用 `supportsTextToVideo`/`resolutions`/`secondsRange`;3D **无 text/image 开关**,判据是 `imageOnly`/`supportsPrompt`。
2. **`contentType` 值是 `three_d` 不是 `3d`**。
3. **`X-Risk-Device-Token` 是软校验** —— 传假值甚至不传都放行。
4. **video/3D 任务不出现在 `async-task/batch-query`**(返回 `data:{}`),必须用 `media-generate/progress`。
5. **双层信封**:`image` 返回 `data.taskId`;`video`/`3d` 返回 `data.data.taskId`。
6. **前端 `supportedTools` 标 `edit_image`,后端只接受 `text_to_image`/`image_to_image`**(传错得 `10005: type must be one of ...`)。
7. **video 用 `imageFileKeys` 会被拒** —— 必须 `firstImageFileKey`(首帧),时长字段是 `duration` 而非 `seconds`。
8. **`localStorage.user_info.cr` 不可信**(显示 100,实际 1000)。
9. **`bytes` 是 SQL 关键字** → Python 侧用 `bytes_=`,`db.update_media` 内部映射。
10. **token 会过期** —— 浏览器自动轮转 JWT,DB 不同步会全站 401(已接 401 自愈)。
11. **别在请求进行中 kill bridge** —— 会产生孤儿 `media` 行。
12. **账号提交频率有限制**:短时间大量探测会被风控拦成 `17001 内容审核未通过`(误导性文案,与图片内容无关)。
13. **OneTrust cookie 横幅会挡住登录按钮点击**(退出登录时 consent cookie 被清 → 横幅重新出现)。先写 `OptanonConsent` / `OptanonAlertBoxClosed` cookie。

## 目录

```
miora2api/
├── bridge.py          FastAPI 网关 + /console + /v1/stats
├── miora_client.py    上游 API 封装(生成/轮询/签名/账单)
├── upload.py          参考图上传(STS + COS 直传)
├── browser_login.py   浏览器登录导入(手写极简 WebSocket 跑 CDP,无第三方依赖)
├── db.py              SQLite 存储层
├── mp4probe.py        极简 MP4 探测(无 ffprobe 依赖)
├── schema.sql         表结构
├── console.html       Web 控制台
└── requirements.txt
```

## 免责声明

本项目仅供学习与互操作性研究。Miora 的协议逆向结果与账号额度均归其平台方所有,请遵守 Miora 的服务条款,勿用于绕过计费或批量滥用。