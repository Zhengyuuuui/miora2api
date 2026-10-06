"""参考图上传：Miora → 腾讯云 COS 直传。

链路（bundle 静态还原 + 2026-10-05 实测确认）
------------------------------------------------
1. ``GET /api/ai/cos/sts-tokens``  → 临时凭证
   实测返回：``{secret_id, secret_key, token, enabled_at, expired_at,
   domain, bucket, region, prefix, timestamp_ms, timeDeviation}``
   bucket 固定为 ``miora-intl-private-1411336493``，region ``ap-singapore``，
   prefix 形如 ``file/{userId}/{yyyy}/{mm}/{dd}/{token24}``，**有效期 30 分钟**。
2. ``PUT https://{bucket}.cos.{region}.myqcloud.com/{prefix}/{ms}-{rand}.{ext}``
   → ``fileKey``（= 上面的 Key），**这个值直接喂给 image_edit / image_to_video / image_to_3d**
3. 产物读取走平台自己的签名器：``POST /api/ai/cos/cdn-sign-url``（见 miora_client.sign_one）
4. （可选）挂进画布：``POST /api/ai/workflow/{workflowId}/asset`` —— **生成类接口不需要**

⚠️ 关于签名
-----------
最初尝试手写 COS V5 签名（HMAC-SHA1 链式派生），报错 ``SignatureDoesNotMatch``；
改对之后变成 ``AccessDenied``，仍不稳定。**官方 SDK 一次成功**，
故此处直接用 ``cos-python-sdk-v5``，不自己维护签名实现。
（``cos_sign.py`` 保留作为算法参考，但不要用于生产。）
"""

from __future__ import annotations

import mimetypes
import os
import time
import uuid
import httpx

import miora_client as mc

MIME2EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp",
    "image/heic": "heic", "image/avif": "avif", "image/tiff": "tif",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    "audio/mpeg": "mp3", "audio/wav": "wav",
}

# 实际模型接受的参考图格式（由 media-models 的 supportedTools + mime 推导）
IMAGE_MIME = {k: v for k, v in MIME2EXT.items() if k.startswith("image/")}


def _ext_for(mime: str, fallback: str = "png") -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in MIME2EXT:
        return MIME2EXT[mime]
    g = (mimetypes.guess_extension(mime) or "").lstrip(".").lower()
    return g or fallback


def _sdk():
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少腾讯云 COS SDK。请安装：pip3 install cos-python-sdk-v5"
        ) from e
    return CosConfig, CosS3Client


# ---------------------------------------------------------------- STS


def get_sts(token: str, client: httpx.Client | None = None) -> dict:
    """GET /api/ai/cos/sts-tokens（实测 200，免不了登录但需 Bearer）。"""
    c = client or httpx.Client(timeout=20.0)
    r = c.get(f"{mc.BASE}/api/ai/cos/sts-tokens", headers=mc._headers(token, json_body=False))
    r.raise_for_status()
    env = r.json()
    if env.get("code") not in (0, None):
        raise mc.MioraError(str(env.get("code")), env.get("message", "sts failed"),
                            env.get("httpStatus"), env)
    return env["data"]


def _client_for(sts: dict):
    CosConfig, CosS3Client = _sdk()
    cfg = CosConfig(
        Region=sts["region"],
        SecretId=sts["secret_id"],
        SecretKey=sts["secret_key"],
        Token=sts["token"],
        Scheme="https",
    )
    return CosS3Client(cfg)


# ---------------------------------------------------------------- 上传


def upload_file(
    path_or_bytes: str | bytes,
    token: str,
    mime: str | None = None,
    filename: str | None = None,
    prefix: str | None = None,
) -> dict:
    """上传文件到 COS，返回 ``{fileKey, url, bytes, mime, ext, etag, bucket, region}``。

    ``fileKey`` 即生成接口的 ``image_file_keys`` 元素。
    """
    if isinstance(path_or_bytes, bytes):
        body = path_or_bytes
        if not mime:
            raise ValueError("上传字节流必须显式指定 mime")
        ext = os.path.splitext(filename)[1].lstrip(".").lower() if filename else ""
        ext = ext or _ext_for(mime)
        name = f"{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}.{ext}"
    else:
        with open(path_or_bytes, "rb") as f:
            body = f.read()
        mime = mime or mimetypes.guess_type(path_or_bytes)[0] or "image/png"
        ext = os.path.splitext(path_or_bytes)[1].lstrip(".") or _ext_for(mime)
        name = filename or f"{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}.{ext}"

    sts = get_sts(token)
    # 前端命名规则：{prefix}/{timestamp}-{rand}.{ext}
    base = prefix or sts["prefix"]
    key = f"{base}/{name}"

    cli = _client_for(sts)
    resp = cli.put_object(
        Bucket=sts["bucket"], Body=body, Key=key, ContentType=mime
    )
    etag = (resp.get("ETag") or "").strip('"') if isinstance(resp, dict) else None

    return {
        "fileKey": key,
        "url": f"https://{sts['bucket']}.cos.{sts['region']}.myqcloud.com/{key}",
        "bytes": len(body),
        "mime": mime,
        "ext": os.path.splitext(name)[1].lstrip("."),
        "etag": etag,
        "bucket": sts["bucket"],
        "region": sts["region"],
    }


def upload_many(paths: list[str], token: str, mime: str | None = None) -> list[dict]:
    """批量上传（bridge 目前逐个调，这里供脚本/批处理用）。"""
    return [upload_file(p, token, mime) for p in paths]


def sign_uploaded(file_key: str, token: str) -> dict:
    """用**平台自己的**签名器把上传的 fileKey 变成可访问 URL（3600s）。

    == miora_client.sign_one，此处是 upload 模块的语义化别名。
    """
    return mc.sign_one(file_key, token)


# ---------------------------------------------------------------- 挂画布（可选）


def upload_to_asset(
    workflow_id: str,
    file_key: str,
    data: bytes,
    mime: str,
    token: str,
    generation_type: str = "import",
) -> dict:
    """把已上传文件**挂进画布**。仅在需要出现在工作台时调用；生成接口不需要。"""
    c = httpx.Client(timeout=30.0)
    r = c.post(
        f"{mc.BASE}/api/ai/workflow/{workflow_id}/asset",
        json={
            "fileKey": file_key,
            "fileType": "image",
            "generationType": generation_type,
            "extraGenerationParams": {"mimeType": mime, "size": len(data)},
        },
        headers=mc._headers(token),
    )
    r.raise_for_status()
    return r.json()
