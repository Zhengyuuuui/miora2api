"""Miora 后端 API 客户端。

只封装**已实测确认**的端点与字段；未验证的部分用注释标出 Hypothesis。
所有结论来自 sessions/2026-10-05-miora-design/{RECON.md,MEDIA-PROTOCOL.md}。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

BASE = "https://miora.design"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# 实测：模型列表**完全免登录**（匿名与带 token 返回一致）
PUBLIC_MODEL_PATH = "/api/ai/public/cloud-agent/media-models"
PUBLIC_TIERS_PATH = "/api/ai/public/cloud-agent/tiers"


class MioraError(RuntimeError):
    def __init__(self, code: str | None, message: str, status: int | None = None, raw: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.raw = raw


def _headers(token: str | None, risk_token: str | None = None, json_body: bool = True) -> dict:
    h = {
        "Accept": "application/json",
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "x-Source": "web",
        "X-Product": "miora",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    if risk_token:
        # 实测：图灵盾 risk token 是**软校验**，传任意值均放行（不传也能过）
        h["X-Risk-Device-Token"] = risk_token
    return h


def _unwrap(resp: httpx.Response) -> dict:
    """拆 Miora 的 {code,failed,data,message,httpStatus} 信封。

    注意：生成类接口存在**双层信封**（data.data），调用方需自行判断，
    这里返回完整 dict，由各方法按其已知形状取值。
    """
    try:
        return resp.json()
    except Exception:
        raise MioraError("BAD_JSON", resp.text[:200], resp.status_code)


def _check(env: dict) -> dict:
    code = env.get("code")
    if code not in (0, "0", None):
        raise MioraError(
            str(env.get("code") or env.get("error")),
            env.get("message") or "request failed",
            env.get("httpStatus"),
            env,
        )
    return env


# --------------------------------------------------------------------------- 公开（免登录）


def get_models(lang: str = "en-US", client: httpx.Client | None = None) -> dict:
    """GET /api/ai/public/cloud-agent/media-models — 41 个媒体模型 + userTier。

    实测：匿名可调用，返回 {code, data:{userTier, data:[...]}}。
    """
    c = client or httpx.Client(timeout=20.0)
    r = c.get(f"{BASE}{PUBLIC_MODEL_PATH}?lang={lang}", headers=_headers(None))
    r.raise_for_status()
    env = _unwrap(r)
    _check(env)
    return {
        "userTier": env["data"].get("userTier"),
        "models": env["data"].get("data") or [],
    }


def get_tiers(client: httpx.Client | None = None) -> list[dict]:
    """GET /api/ai/public/cloud-agent/tiers — 11 档 LLM（供 /v1/models 参考）。"""
    c = client or httpx.Client(timeout=20.0)
    r = c.get(f"{BASE}{PUBLIC_TIERS_PATH}", headers=_headers(None))
    r.raise_for_status()
    return _check(_unwrap(r))["data"]


# --------------------------------------------------------------------------- 账号


def get_credits(token: str, client: httpx.Client | None = None) -> dict:
    """GET /api/ai/quota/credit — 实测返回 {total_amount, used_amount, nextExpiring}。"""
    c = client or httpx.Client(timeout=20.0)
    r = c.get(f"{BASE}/api/ai/quota/credit", headers=_headers(token, json_body=False))
    r.raise_for_status()
    return _check(_unwrap(r))["data"]


def get_profile(token: str, client: httpx.Client | None = None) -> dict:
    """GET /api/ai/user/profile — 实测登录后 200（未登录 401）。返回 {userId,email,plan...}。

    当前 bridge 未使用（账号信息靠 /admin/accounts 导入时手填），保留供诊断/脚本用。
    """
    c = client or httpx.Client(timeout=20.0)
    r = c.get(f"{BASE}/api/ai/user/profile", headers=_headers(token, json_body=False))
    r.raise_for_status()
    return _check(_unwrap(r))["data"]


def refresh_token(token: str, refresh_token: str, client: httpx.Client | None = None) -> dict:
    """POST /api/auth/refresh-token {refresh_token} → {access_token, refresh_token}。

    静态还原（bundle 原文）：
      fetch(`${origin}/api/auth/refresh-token`, {method:'POST',
        body: JSON.stringify({refresh_token})})
      成功后 DE(access_token, refresh_token) 写回 localStorage + cookie
    注意：该端点用裸 fetch（不带 X-Product 等头），此处保持一致。
    """
    c = client or httpx.Client(timeout=20.0)
    r = c.post(
        f"{BASE}/api/auth/refresh-token",
        json={"refresh_token": refresh_token},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    env = _unwrap(r)
    _check(env)
    return env["data"]


# --------------------------------------------------------------------------- 生成（已实测 image）


def submit(
    media_type: str,
    model: str,
    prompt: str,
    token: str,
    task_kind: str | None = None,
    image_file_keys: list[str] | None = None,
    first_image_file_key: str | None = None,
    last_image_file_key: str | None = None,
    reference_image_file_keys: list[str] | None = None,
    duration: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    extra: dict | None = None,
    risk_token: str | None = None,
    node_id: str | None = None,
    workflow_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """POST /api/ai/media-generate/{image|video|3d}

    ⚠️ 三种媒体的参考图字段**完全不同**（2026-10-05 实测）：
      - image : ``imageFileKeys[]``
      - 3d    : ``imageFileKeys[]``（实测 image_to_3d 用这个，能过）
      - video : ``firstImageFileKey``（首帧）— 实测传 ``imageFileKeys`` 会得到
                ``10005: firstImageFileKey is required for image_to_video /
                frame_to_video type (unless characterAssetIds provided)``
                另有 ``lastImageFileKey``（尾帧）、``referenceImageFileKeys[]``、
                ``referenceVideoFileKeys[]``，时长字段是 **``duration``（秒，整数）不是 seconds**
    """
    kind = task_kind or {
        "image": "image_to_image" if image_file_keys else "text_to_image",
        "video": "image_to_video" if (first_image_file_key or image_file_keys) else "text_to_video",
        "3d": "image_to_3d" if image_file_keys else "text_to_3d",
    }[media_type]

    body: dict[str, Any] = {"type": kind, "prompt": prompt or "", "model": model}

    if media_type == "video":
        first = first_image_file_key or (image_file_keys or [None])[0]
        if first:
            body["firstImageFileKey"] = first
        if last_image_file_key:
            body["lastImageFileKey"] = last_image_file_key
        if reference_image_file_keys:
            body["referenceImageFileKeys"] = reference_image_file_keys
        if duration:
            body["duration"] = int(duration)
    elif image_file_keys:
        body["imageFileKeys"] = image_file_keys

    extra_params: dict[str, Any] = {}
    if resolution:
        extra_params["resolution"] = resolution
    if aspect_ratio:
        extra_params["aspectRatio"] = aspect_ratio
    if extra:
        extra_params.update(extra)
    if extra_params:
        body["extraParameters"] = extra_params
    if node_id:
        body["nodeId"] = node_id
    if workflow_id:
        body["workflowId"] = workflow_id

    c = client or httpx.Client(timeout=40.0)
    path = "/api/ai/media-generate/3d" if media_type == "3d" else f"/api/ai/media-generate/{media_type}"
    r = c.post(f"{BASE}{path}", json=body, headers=_headers(token, risk_token))
    r.raise_for_status()
    env = _check(_unwrap(r))

    data = env.get("data")
    # 兼容双层信封
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict) or not data.get("taskId"):
        raise MioraError(
            str(env.get("code")), env.get("message") or "no taskId in response",
            env.get("httpStatus"), env,
        )
    return data


def poll_tasks(task_ids: list[str], token: str, client: httpx.Client | None = None) -> dict[str, dict]:
    """POST /api/ai/async-task/batch-query {taskIds:[...]}

    ★ 实测修正（2026-10-05）：
      - image 任务在此端点可见，响应 ``data`` **直接为 {taskId:{...}}**，没有 tasks 包装。
      - ★ **video / 3d 任务在此端点返回空 data {}** —— 必须改用
        ``/api/ai/media-generate/progress``，其响应为 ``data.tasks{taskId:{...}}``。
        实测同一 video taskId：batch-query → {} ，media/progress → 正常返回。
    因此 wait_for() 需要知道媒体类型；未知时建议用 poll_all_tasks()。
    """
    if not task_ids:
        return {}
    c = client or httpx.Client(timeout=20.0)
    r = c.post(
        f"{BASE}/api/ai/async-task/batch-query",
        json={"taskIds": task_ids},
        headers=_headers(token),
    )
    r.raise_for_status()
    data = _check(_unwrap(r)).get("data") or {}
    if isinstance(data, dict) and "tasks" in data and isinstance(data["tasks"], dict):
        data = data["tasks"]
    return data if isinstance(data, dict) else {}


def poll_media_progress(
    token: str,
    image_ids: list[str] | None = None,
    video_ids: list[str] | None = None,
    three_d_ids: list[str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, dict]:
    """POST /api/ai/media-generate/progress（媒体专用批量轮询，静态还原）。"""
    body = {
        "imageTaskIds": image_ids or [],
        "videoTaskIds": video_ids or [],
        "threeDTaskIds": three_d_ids or [],
    }
    if not (body["imageTaskIds"] or body["videoTaskIds"] or body["threeDTaskIds"]):
        return {}
    c = client or httpx.Client(timeout=20.0)
    r = c.post(f"{BASE}/api/ai/media-generate/progress", json=body, headers=_headers(token))
    r.raise_for_status()
    data = _check(_unwrap(r)).get("data") or {}
    return data.get("tasks") or data if isinstance(data, dict) else {}


def wait_for(
    task_ids: list[str],
    token: str,
    media_type: str = "image",
    timeout: float = 300.0,
    interval: float = 5.0,
    client: httpx.Client | None = None,
    on_progress=None,
) -> dict[str, dict]:
    """轮询直到全部终态。终态：completed / inserted / failed / cancelled。

    ★ media_type 决定用哪个端点（实测 video/3d 不出现在 batch-query）：
      - image  → /api/ai/async-task/batch-query
      - video/3d → /api/ai/media-generate/progress
    """
    if media_type == "image":
        fn = lambda: poll_tasks(task_ids, token, client)
    elif media_type == "3d":
        fn = lambda: poll_media_progress(token, three_d_ids=task_ids, client=client)
    else:
        fn = lambda: poll_media_progress(token, video_ids=task_ids, client=client)

    deadline = time.time() + timeout
    last: dict[str, dict] = {}
    while time.time() < deadline:
        last = fn()
        if last and on_progress:
            on_progress(last)
        if last and all(
            (t.get("status") in ("completed", "inserted", "failed", "cancelled"))
            for t in last.values()
        ):
            return last
        time.sleep(interval)
    return last


def poll_any(
    task_ids: list[str], token: str, client: httpx.Client | None = None
) -> dict[str, dict]:
    """不知道媒体类型时：两个端点都查并合并（实测互补，代价是多一次请求）。"""
    merged = poll_tasks(task_ids, token, client)
    missing = [t for t in task_ids if t not in merged]
    if missing:
        got = poll_media_progress(
            token, image_ids=missing, video_ids=missing, three_d_ids=missing, client=client
        )
        merged.update(got)
    return merged


# --------------------------------------------------------------------------- 产物


def sign_urls(file_paths: list[str], token: str, client: httpx.Client | None = None) -> dict:
    """POST /api/ai/cos/cdn-sign-url {filePaths:[...]}

    ★ 实测返回：
      {"code":0,"data":{"signedUrls":"<单个URL字符串>",   ← 注意是字符串不是数组
       "expireTime":...,"currentTime":...,"validDuration":3600,"filePaths":[...]}}
    """
    c = client or httpx.Client(timeout=20.0)
    r = c.post(
        f"{BASE}/api/ai/cos/cdn-sign-url",
        json={"filePaths": file_paths},
        headers=_headers(token),
    )
    r.raise_for_status()
    return _check(_unwrap(r))["data"]


def sign_one(file_key: str, token: str, client: httpx.Client | None = None) -> dict:
    """单个产物签名。返回 {url, expires_at, file_key}。

    签名 3600s 过期 → 过期后用 file_key 重新签即可，不必重新生成。
    """
    d = sign_urls([file_key], token, client)
    url = d.get("signedUrls")
    if isinstance(url, list):
        url = url[0] if url else None
    return {
        "url": url,
        "expires_at": float(d.get("expireTime") or 0) or None,
        "file_key": (d.get("filePaths") or [file_key])[0],
    }


def mark_inserted(task_id: str, token: str, client: httpx.Client | None = None) -> dict:
    """POST /api/ai/async-task/{taskId}/mark-inserted

    静态还原；**是否必须调用未实测**。bridge 故意**不调**它 —— 实测 7 次生成
    在不调用的前提下全部成功、产物可读，说明它只影响"是否挂进画布资产表"。
    """
    c = client or httpx.Client(timeout=20.0)
    r = c.post(
        f"{BASE}/api/ai/async-task/{task_id}/mark-inserted",
        json={},
        headers=_headers(token),
    )
    r.raise_for_status()
    return _check(_unwrap(r))


# --------------------------------------------------------------------------- 3D 专项


def query_3d_tasks(task_ids: list[str], token: str, client: httpx.Client | None = None) -> dict:
    """POST /api/ai/ai-3d-generator/tasks/batch-query（静态还原，**未实测**）。

    bridge 走的是通用 ``poll_media_progress``（实测可用），此函数备用。
    """
    c = client or httpx.Client(timeout=20.0)
    r = c.post(
        f"{BASE}/api/ai/ai-3d-generator/tasks/batch-query",
        json={"taskIds": task_ids},
        headers=_headers(token),
    )
    r.raise_for_status()
    return (_check(_unwrap(r)).get("data") or {}).get("data") or {}


def query_3d_by_external(external_id: str, token: str, client: httpx.Client | None = None) -> dict | None:
    """GET /api/ai/ai-3d-generator/copilot/task-by-external/{id}（静态还原，**未实测**）。"""
    c = client or httpx.Client(timeout=20.0)
    r = c.get(
        f"{BASE}/api/ai/ai-3d-generator/copilot/task-by-external/{external_id}",
        headers=_headers(token, json_body=False),
    )
    r.raise_for_status()
    return _check(_unwrap(r)).get("data")
