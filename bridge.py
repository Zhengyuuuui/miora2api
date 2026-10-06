#!/usr/bin/env python3
"""miora2api bridge — 端口 4690

把已探明的 Miora 能力固化为可长期运行的 bridge：

已实测（Confirmed）
  · 41 模型列表        GET  /api/ai/public/cloud-agent/media-models   （免登录）
  · 11 档 LLM          GET  /api/ai/public/cloud-agent/tiers          （免登录）
  · 生图               POST /api/ai/media-generate/image              gem-3.1 1K=6.7c
  · 生视频             POST /api/ai/media-generate/video              vidu-q2=54c/5s
  · 轮询 image         POST /api/ai/async-task/batch-query
  · 轮询 video/3d      POST /api/ai/media-generate/progress      ← video 不在 batch-query！
  · 产物签名           POST /api/ai/cos/cdn-sign-url                 3600s 有效
  · 余额               GET  /api/ai/quota/credit
  · 官方账单           GET  /api/ai/billing/records/usage            ← 对账权威来源

已知限制（未实测/未实现）
  · 3D 生成未实测（协议已还原，tripo-3d-3.1 / hunyuan-3d-pro-3.1 可用）
  · 参考图上传（COS STS 直传）未接 → image_edit 暂不可用
  · LLM chat 是黑盒 Agent（有状态 runtime + 工具审批），**本 bridge 不做**
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

import db
import miora_client as mc
import upload as up
import browser_login as bl

PORT = 4690
_HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.join(_HERE, "console.html")
DB_PATH = os.environ.get("MIORA_DB", os.path.join(_HERE, "miora.db"))

app = FastAPI(title="miora2api bridge", version="1.0.0", docs_url="/docs")

# ---------------------------------------------------------------- 模型缓存

_model_cache: dict[str, Any] = {"ts": 0.0, "data": None}
MODEL_TTL = 600.0


def load_models(force: bool = False) -> dict:
    if not force and _model_cache["data"] and time.time() - _model_cache["ts"] < MODEL_TTL:
        return _model_cache["data"]
    data = mc.get_models("en-US")
    _model_cache.update({"ts": time.time(), "data": data})
    return data


# ★ 前端 supportedTools 用的标签 vs 后端真正接受的 type 枚举（实测 10005 报错确认）。
#   edit_image  → image_to_image
#   其余同名。
TOOL_TO_TYPE = {
    "edit_image": "image_to_image",
    "text_to_image": "text_to_image",
    "image_to_image": "image_to_image",
    "text_to_video": "text_to_video",
    "image_to_video": "image_to_video",
    "text_to_3d": "text_to_3d",
    "image_to_3d": "image_to_3d",
}


def tool_to_type(tool: str) -> str:
    return TOOL_TO_TYPE.get(tool, tool)


def norm_mt(mt: str) -> str:
    """把 API 的 contentType 归一：three_d → 3d。"""
    return "3d" if mt in ("3d", "three_d") else mt


def find_model(model_id: str, media_type: str | None = None) -> dict:
    data = load_models()
    want = norm_mt(media_type) if media_type else None
    for m in data["models"]:
        if m["modelId"] != model_id:
            continue
        if want is None or norm_mt(m.get("contentType")) == want:
            return m
    # 媒体类型不匹配时给出更清楚的提示
    same = [m for m in data["models"] if m["modelId"] == model_id]
    if same:
        raise HTTPException(
            400, f"{model_id} 是 {same[0]['contentType']} 模型，不能用于 {media_type}"
        )
    raise HTTPException(404, f"unknown model: {model_id}")


def caps_of(model_id: str, media_type: str) -> dict:
    m = find_model(model_id, media_type)
    if not m.get("available"):
        raise HTTPException(
            400, f"{model_id} unavailable on tier={load_models()['userTier']}"
        )
    return m.get("capabilities") or {}


# ★ 实测修正：三种媒体的 capabilities 结构**完全不同**，不能共用一套校验。
#   image  : supportedTools / allowedResolutions / aspectRatios / referenceImagesRange
#   video  : supportsTextToVideo / supportsImageUrl / resolutions(720P|1080P) / secondsRange
#   three_d: 无 text/image 开关，用 generateTypes / resultFormats / faceCountRange
def derive_tools(mt: str, caps: dict) -> list[str]:
    """把各媒体的能力字段归一化成统一的工具列表。"""
    if mt == "image":
        return caps.get("supportedTools") or []
    if mt == "video":
        t = []
        if caps.get("supportsTextToVideo"):
            t.append("text_to_video")
        if caps.get("supportsImageUrl") or caps.get("supportsReferenceImages"):
            t.append("image_to_video")
        return t
    if mt == "3d" or mt == "three_d":
        # ★ 3D 判据（实测各模型字段值）：
        #   miora-3d          imageOnly=true        → 仅图生 3D
        #   hunyuan-3d-rigging imageOnly=false, supportsPrompt=false → 仅图生 3D（骨骼绑定本就依赖图）
        #   其余 4 个          两者皆无              → 文/图都可
        if caps.get("imageOnly") is True or caps.get("supportsPrompt") is False:
            return ["image_to_3d"]
        return ["text_to_3d", "image_to_3d"]
    return []


def derive_resolutions(mt: str, caps: dict) -> list[str]:
    if mt == "video":
        return caps.get("resolutions") or []
    return caps.get("allowedResolutions") or []


def derive_ref_info(mt: str, caps: dict) -> dict:
    """返回参考图上传上限信息。

    ★ 三种媒体字段完全不同（2026-10-05 实测）：
      - image   : ``referenceImagesRange{max,min}``（如 gem-3.1 → 14、hy-image-3.5-f → 20）
      - video   : 同名字段（seedance-2.0 → 9、kling-o1 → 3）；但多数模型
                   ``supportsReferenceImages=false`` → 只能用首帧 1 张
      - three_d : **完全无声明**（全为 null）→ unknown

    返回 ``{max, min, supported, known, note}``：
      - ``max``      上传张数上限（None=不限/未知）
      - ``supported`` 是否支持多张参考图（False=只能用首帧/不支持）
      - ``known``    上限是否为官方明确声明（False=需实测）
    """
    info = {"max": None, "min": None, "supported": None, "known": False, "note": ""}
    rng = caps.get("referenceImagesRange")
    if isinstance(rng, dict) and rng.get("max") is not None:
        info["max"] = int(rng["max"])
        info["min"] = int(rng.get("min") or 1)
        info["known"] = True
        info["supported"] = True
    elif rng is None and mt in ("image", "video", "3d"):
        # 无 referenceImagesRange
        if mt == "image":
            info["note"] = "该模型不支持参考图"
            info["supported"] = False
        elif mt == "video":
            # 只有 supportsImageUrl → 只能用首帧
            if caps.get("supportsImageUrl"):
                info["max"] = 1
                info["min"] = 1
                info["supported"] = False
                info["known"] = True
                info["note"] = "仅支持首帧（firstImageFileKey），不支持多参考图"
            else:
                info["supported"] = False
                info["note"] = "不支持参考图"
        else:  # 3d
            info["note"] = "官方未声明上限（3D capabilities 无 referenceImagesRange）"
    return info


def derive_ref_max(mt: str, caps: dict) -> int | None:
    """便捷：只要上限值。"""
    return derive_ref_info(mt, caps)["max"]


# ---------------------------------------------------------------- 账号


def current_account() -> dict:
    acc = db.pick_account()
    if not acc:
        raise HTTPException(503, "no usable account — POST /admin/accounts 先导入")
    return acc


def _try_refresh_token(acc: dict) -> bool:
    """用 refresh_token 换新 authToken（POST /api/auth/refresh-token {refresh_token}）。
    实测：浏览器会自动轮转 JWT，DB 里的 token 可能过期 → 靠这条自愈。"""
    rt = acc.get("refresh_token")
    if not rt:
        return False
    try:
        r = httpx.post(f"{mc.BASE}/api/auth/refresh-token",
                       json={"refresh_token": rt},
                       headers={"Content-Type": "application/json"}, timeout=20)
        if r.status_code != 200:
            return False
        d = (r.json() or {}).get("data") or {}
        new_at = d.get("access_token") or d.get("accessToken")
        if not new_at:
            return False
        db.update_account(acc["id"], auth_token=new_at,
                          refresh_token=d.get("refresh_token") or rt, status="active")
        acc["auth_token"] = new_at
        return True
    except Exception:
        return False


def ensure_token(acc: dict) -> dict:
    """取一个可用 token；401 时先尝试 refresh，再失败则冷却该账号。"""
    try:
        mc.get_credits(acc["auth_token"])
        return acc
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403) and _try_refresh_token(acc):
            try:
                mc.get_credits(acc["auth_token"])
                return acc
            except Exception:
                pass
        acct_fail(acc, f"token invalid ({e.response.status_code})", cooldown=900)
        raise HTTPException(401, "账号 token 失效且无法自动续期，请重新导入 authToken")
    except Exception:
        return acc


def acct_fail(acc: dict, reason: str, cooldown: float = 300.0) -> None:
    db.update_account(acc["id"], status="cooldown", last_error=reason,
                      cooldown_until=db.now() + cooldown)


def acct_ok(acc: dict) -> None:
    db.update_account(acc["id"], status="active", last_error=None, cooldown_until=0)


def sync_credits(acc: dict) -> float | None:
    try:
        d = mc.get_credits(acc["auth_token"])
        total, used = float(d["total_amount"]), float(d["used_amount"])
        rem = round(total - used, 4)
        db.update_account(acc["id"], credits=rem, credit_total=total)
        return rem
    except Exception:
        return None


def official_usage(token: str) -> list[dict]:
    """官方逐条账单 —— 对账权威来源。"""
    c = httpx.Client(timeout=20.0)
    r = c.get(f"{mc.BASE}/api/ai/billing/records/usage",
              headers=mc._headers(token, json_body=False))
    r.raise_for_status()
    c.close()
    return (r.json().get("data") or {}).get("records") or []


# ---------------------------------------------------------------- 生成


class GenReq(BaseModel):
    model: str
    prompt: str = ""
    image_file_keys: list[str] = Field(default_factory=list)
    # video 专用（实测：首帧必须用 firstImageFileKey，不是 imageFileKeys）
    first_image_file_key: str | None = None
    last_image_file_key: str | None = None
    reference_image_file_keys: list[str] = Field(default_factory=list)
    duration: int | None = None          # 秒（video）；会覆盖 extra.seconds
    resolution: str | None = None
    aspect_ratio: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    n: int = 1
    wait: bool = True
    stream: bool = False


def _validate(req: GenReq, media_type: str) -> tuple[str, str, str]:
    caps = caps_of(req.model, media_type)
    has_ref = bool(req.image_file_keys or req.first_image_file_key
                   or req.reference_image_file_keys)
    tool = {
        "image": "edit_image" if req.image_file_keys else "text_to_image",
        "video": "image_to_video" if has_ref else "text_to_video",
        "3d": "image_to_3d" if req.image_file_keys else "text_to_3d",
    }[media_type]
    tools = derive_tools(media_type, caps)
    if tools and tool not in tools:
        raise HTTPException(400, f"{req.model} 不支持 {tool}（支持 {tools}）")
    kind = tool_to_type(tool)          # 标签 → 后端枚举

    res = req.resolution or caps.get("defaultResolution")
    allowed = derive_resolutions(media_type, caps)
    if allowed and res and res not in allowed:
        raise HTTPException(400, f"resolution {res} 不支持；可选 {allowed}")
    ratio = req.aspect_ratio or caps.get("defaultAspectRatio")
    ratios = caps.get("aspectRatios") or []
    if ratios and ratio and ratio not in ratios:
        raise HTTPException(400, f"aspect_ratio {ratio} 不支持；可选 {ratios}")
    rmax = derive_ref_max(media_type, caps)
    n_ref = len(req.image_file_keys) + len(req.reference_image_file_keys) + (1 if req.first_image_file_key else 0)
    if rmax is not None and n_ref > rmax:
        raise HTTPException(400, f"参考图最多 {rmax} 张")

    # 视频时长单独校验（capabilities.secondsRange）
    if media_type == "video":
        sr = caps.get("secondsRange") or {}
        sec = req.duration or (req.extra or {}).get("seconds") or caps.get("defaultSeconds")
        if sec is not None and sr:
            if sec < sr.get("min", 1) or sec > sr.get("max", 8):
                raise HTTPException(
                    400, f"seconds {sec} 超出范围 {sr.get('min')}~{sr.get('max')}")
            req.duration = sec
    return kind, res, ratio


def _submit_many(req: GenReq, media_type: str, acc: dict) -> list[dict]:
    acc = ensure_token(acc)          # 401 自愈（refresh_token 续期）
    kind, res, ratio = _validate(req, media_type)
    out = []
    for _ in range(max(1, min(req.n, 4))):
        t0 = time.time()
        try:
            t = mc.submit(
                media_type=media_type, model=req.model, prompt=req.prompt,
                token=acc["auth_token"], task_kind=kind,
                image_file_keys=req.image_file_keys or None,
                first_image_file_key=req.first_image_file_key,
                last_image_file_key=req.last_image_file_key,
                reference_image_file_keys=req.reference_image_file_keys or None,
                duration=req.duration,
                resolution=res, aspect_ratio=ratio, extra=req.extra,
            )
        except mc.MioraError as e:
            db.log_request(f"/api/ai/media-generate/{media_type}",
                           account_id=acc["id"], status=e.status, code=e.code,
                           ok=False, ms=(time.time() - t0) * 1000, note=e.message)
            if e.status in (401, 403):
                acct_fail(acc, f"{e.code}:{e.message}")
                raise HTTPException(401, f"账号被拒：{e.message}")
            raise HTTPException(e.status or 400, f"{e.code}: {e.message}")
        db.create_task(t["taskId"], media_type, req.model, account_id=acc["id"],
                       task_kind=kind, prompt=req.prompt, resolution=res,
                       aspect_ratio=ratio, ref_count=len(req.image_file_keys), raw=t)
        q = t.get("queue") or {}
        db.update_task(t["taskId"], queue_state=q.get("state"),
                       queue_position=q.get("position"), est_seconds=q.get("estimatedSeconds"))
        out.append(t)
    db.log_request(f"/api/ai/media-generate/{media_type}", account_id=acc["id"],
                   status=200, ok=True, ms=(time.time() - t0) * 1000,
                   note=f"{req.model} x{req.n}")
    acct_ok(acc)
    return out


def _finish(task_ids: list[str], acc: dict, media_type: str,
            timeout: float = 600.0) -> list[dict]:
    states = mc.wait_for(task_ids, acc["auth_token"], media_type=media_type,
                         timeout=timeout, interval=5)
    out = []
    for tid, t in states.items():
        st = t.get("status")
        if st in ("failed", "cancelled"):
            db.update_task(tid, status=st, error=t.get("errorMessage"))
            out.append({"task_id": tid, "status": st, "error": t.get("errorMessage")})
            continue
        if st not in ("completed", "inserted"):
            db.update_task(tid, status=st or "unknown", progress=t.get("progress"))
            out.append({"task_id": tid, "status": st, "progress": t.get("progress")})
            continue
        fk = t.get("resultFileKey") or (t.get("resultFiles") or [{}])[0].get("fileKey")
        if not fk:
            db.update_task(tid, status=st, error="no resultFileKey")
            continue
        s = mc.sign_one(fk, acc["auth_token"])
        ext = fk.rsplit(".", 1)[-1] if "." in fk else None
        # 记录官方直给的 resultSignUrl（有效期同签名）
        db.update_task(tid, status="completed", progress=100, file_key=s["file_key"],
                       signed_url=s["url"], url_signed_at=db.now(),
                       url_expires_at=s["expires_at"])
        db.add_media(tid, media_type, t.get("modelName"), acc["id"], fk, ext,
                     signed_url=s["url"], url_expires_at=s["expires_at"])
        db.add_cost(t.get("modelName") or "", media_type, account_id=acc["id"],
                    resolution=res_of(t), aspect_ratio=ratio_of(t), ok=True,
                    note=f"task={tid}")
        out.append({"task_id": tid, "status": st, "file_key": s["file_key"],
                    "url": s["url"], "expires_at": s["expires_at"]})
    return out


def res_of(t: dict) -> str | None:
    return (t.get("extraParameters") or {}).get("resolution")


def ratio_of(t: dict) -> str | None:
    return (t.get("extraParameters") or {}).get("aspect_ratio")


@app.post("/v1/images/generations")
def images(req: GenReq, acc: dict = Depends(current_account)):
    tasks = _submit_many(req, "image", acc)
    ids = [t["taskId"] for t in tasks]
    if req.stream:
        return StreamingResponse(_stream(ids, acc, "image"), media_type="text/event-stream")
    if not req.wait:
        return {"object": "list", "data": [
            {"task_id": t["taskId"], "status": t.get("status"), "queue": t.get("queue")}
            for t in tasks]}
    res = _finish(ids, acc, "image")
    sync_credits(acc)
    return {"created": int(time.time()), "object": "list", "data": res}


@app.post("/v1/videos/generations")
def videos(req: GenReq, acc: dict = Depends(current_account)):
    tasks = _submit_many(req, "video", acc)
    ids = [t["taskId"] for t in tasks]
    if not req.wait:
        return {"object": "list", "data": [
            {"task_id": t["taskId"], "status": t.get("status"), "queue": t.get("queue")}
            for t in tasks]}
    res = _finish(ids, acc, "video", timeout=900)
    sync_credits(acc)
    return {"created": int(time.time()), "object": "list", "data": res}


@app.post("/v1/3d/generations")
def threed(req: GenReq, acc: dict = Depends(current_account)):
    tasks = _submit_many(req, "3d", acc)
    ids = [t["taskId"] for t in tasks]
    if not req.wait:
        return {"object": "list", "data": [
            {"task_id": t["taskId"], "status": t.get("status"), "queue": t.get("queue")}
            for t in tasks]}
    res = _finish(ids, acc, "3d", timeout=1200)
    sync_credits(acc)
    return {"created": int(time.time()), "object": "list", "data": res}


async def _stream(task_ids: list[str], acc: dict, media_type: str):
    def ev(o):
        return f"data: {json.dumps(o, ensure_ascii=False)}\n\n"

    yield ev({"type": "task.created", "task_ids": task_ids, "media_type": media_type})
    deadline = time.time() + 600
    seen: dict[str, str] = {}
    while time.time() < deadline:
        states = mc.poll_any(task_ids, acc["auth_token"])
        for tid, t in states.items():
            if t.get("status") != seen.get(tid):
                seen[tid] = t.get("status")
                yield ev({"type": "progress", "task_id": tid,
                          "status": t.get("status"), "progress": t.get("progress"),
                          "queue": t.get("queue")})
        if states and all(t.get("status") in
                          ("completed", "inserted", "failed", "cancelled")
                          for t in states.values()):
            break
        import asyncio
        await asyncio.sleep(5)
    yield ev({"type": "result", "data": _finish(task_ids, acc, media_type)})
    yield "data: [DONE]\n\n"


@app.get("/v1/tasks/{task_id}")
def task(task_id: str, acc: dict = Depends(current_account)):
    t = mc.poll_any([task_id], acc["auth_token"]).get(task_id)
    row = db.get_task(task_id)
    if not t:
        if not row:
            raise HTTPException(404, "unknown task")
        return {"source": "db", **row}
    st = t.get("status")
    if st in ("completed", "inserted") and t.get("resultFileKey"):
        fk = t["resultFileKey"]
        s = mc.sign_one(fk, acc["auth_token"])
        db.update_task(task_id, status="completed", progress=100, file_key=s["file_key"],
                       signed_url=s["url"], url_signed_at=db.now(),
                       url_expires_at=s["expires_at"])
        return {**t, "file_key": s["file_key"], "url": s["url"],
                "expires_at": s["expires_at"]}
    db.update_task(task_id, status=st or "unknown", progress=t.get("progress"),
                   error=t.get("errorMessage"))
    return {"source": "live", **t}


# ---------------------------------------------------------------- 模型


@app.get("/v1/models")
def models(fmt: str = Query("openai", pattern="^(openai|raw)$")):
    data = load_models(force=(fmt == "raw"))
    if fmt == "raw":
        return data
    now_ts = int(time.time())
    out = []
    for m in data["models"]:
        c = m.get("capabilities") or {}
        mt = m.get("contentType")
        out.append({
            "id": m["modelId"], "object": "model", "created": now_ts,
            "owned_by": "miora.tencent.com",
            "miora": {
                "label": m.get("label"), "contentType": mt,
                "available": m.get("available"),
                "priceTier": (m.get("extra") or {}).get("priceTier"),
                "featureFlag": m.get("featureFlag"),
                # 归一化后的字段（跨媒体统一口径）
                "tools": derive_tools(norm_mt(mt), c),
                "resolutions": derive_resolutions(norm_mt(mt), c),
                "defaultResolution": c.get("defaultResolution"),
                "aspectRatios": c.get("aspectRatios") or [],
                "referenceImages": derive_ref_max(norm_mt(mt), c),
                "refInfo": derive_ref_info(norm_mt(mt), c),
                # 各媒体特有
                "estSeconds": c.get("estimatedDurationSeconds"),
                "secondsRange": c.get("secondsRange"),
                "defaultSeconds": c.get("defaultSeconds"),
                "generateTypes": c.get("generateTypes"),
                "resultFormats": c.get("resultFormats"),
                "faceCountRange": c.get("faceCountRange"),
                "supportsPbr": c.get("supportsPbr"),
                "watermark": c.get("supportsWatermark"),
                "durationTierRestricted": c.get("hasDurationTierRestriction"),
                "concurrencyGroup": m.get("concurrencyGroup"),
                "concurrencyLimit": m.get("concurrencyLimit"),
            },
        })
    return {"object": "list", "userTier": data["userTier"], "data": out}


@app.get("/v1/tiers")
def tiers():
    return {"object": "list", "data": mc.get_tiers()}


# ---------------------------------------------------------------- 产物 / 管理


class UploadReq(BaseModel):
    paths: list[str] = Field(default_factory=list)      # 本地文件路径
    urls: list[str] = Field(default_factory=list)       # 或已有 http(s) URL（服务端代下）
    data_urls: list[str] = Field(default_factory=list)  # 或 data:URL（浏览器 FileReader 产出）
    names: list[str] = Field(default_factory=list)      # data_urls 对应的文件名（取扩展名）


@app.post("/admin/upload")
def do_upload(body: UploadReq, acc: dict = Depends(current_account)):
    """上传参考图到 COS，返回可直接用于 image_file_keys 的 fileKey 列表。

    流程：GET /api/ai/cos/sts-tokens → PUT COS（官方 SDK 签名）
    """
    acc = ensure_token(acc)
    tok = acc["auth_token"]
    out: list[dict] = []
    for pth in body.paths:
        if not os.path.isfile(pth):
            raise HTTPException(400, f"文件不存在: {pth}")
        try:
            out.append(up.upload_file(pth, tok))
        except Exception as e:
            raise HTTPException(500, f"上传失败 {os.path.basename(pth)}: {e}")
    for u in body.urls:
        if not (u.startswith("http://") or u.startswith("https://")):
            raise HTTPException(400, f"不支持的 URL: {u}")
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as c:
                r = c.get(u)
            r.raise_for_status()
            mime = r.headers.get("content-type", "image/png").split(";")[0]
            out.append(up.upload_file(r.content, tok, mime=mime))
        except Exception as e:
            raise HTTPException(500, f"下载并上传失败: {e}")
    for i, du in enumerate(body.data_urls):
        if "," not in du:
            raise HTTPException(400, "data_url 格式错误")
        head, b64 = du.split(",", 1)
        if ";base64" not in head:
            raise HTTPException(400, "仅支持 base64 data URL")
        mime = head[5:].split(";")[0] or "image/png"
        import base64 as _b64
        try:
            raw = _b64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "base64 解码失败")
        ext = (body.names[i].rsplit(".", 1)[-1].lower()
               if i < len(body.names) and "." in body.names[i] else None)
        try:
            o = up.upload_file(raw, tok, mime=mime, filename=ext)
            out.append(o)
        except Exception as e:
            raise HTTPException(500, f"上传失败: {e}")
    db.log_request("/admin/upload", account_id=acc["id"], status=200, ok=True,
                   note=f"{len(out)} file(s)")
    return {"object": "list", "data": out,
            "image_file_keys": [o["fileKey"] for o in out]}


@app.get("/v1/media")
def media(limit: int = 50, media_type: str | None = None):
    rows = []
    for m in db.list_media(limit, media_type):
        rows.append({**m, "url_expired": (m.get("url_expires_at") or 0) < db.now()})
    return {"object": "list", "data": rows}


@app.get("/v1/media/{media_id}/refresh")
def refresh(media_id: int, acc: dict = Depends(current_account)):
    with db.get_conn() as c:
        row = c.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown media")
    m = dict(row)
    if not m.get("file_key"):
        raise HTTPException(400, "no file_key")
    s = mc.sign_one(m["file_key"], acc["auth_token"])
    db.update_media(media_id, bytes_=None, signed_url=s["url"],
                    url_expires_at=s["expires_at"])
    return {"media_id": media_id, **s}


@app.get("/v1/costs")
def costs():
    return {"object": "list", "data": db.cost_summary()}


class AccountIn(BaseModel):
    id: str
    auth_token: str
    refresh_token: str | None = None
    label: str = ""
    country: str | None = None
    plan: str | None = None


@app.post("/admin/accounts")
def add_account(b: AccountIn):
    credits = None
    try:
        d = mc.get_credits(b.auth_token)
        credits = round(float(d["total_amount"]) - float(d["used_amount"]), 4)
    except Exception:
        pass
    db.upsert_account(b.id, b.auth_token, b.refresh_token, b.label,
                      credits=credits, plan=b.plan, country=b.country)
    db.update_account(b.id, status="active")
    return {"ok": True, "id": b.id, "credits": credits}


# ---------------------------------------------------------------- 浏览器登录导入


class BlLaunchReq(BaseModel):
    port: int = 9222
    path: str = ""
    mode: str = "launch"      # launch=自己拉起干净浏览器 | attach=连接已有调试端口


@app.get("/admin/browser/status")
def browser_status(session_id: str | None = None, port: int | None = None):
    """浏览器探测 + 会话状态（含已连端口、探测到的浏览器路径）。"""
    return bl.status(session_id, port)


@app.post("/admin/browser/launch")
def browser_launch(body: BlLaunchReq):
    """启动浏览器或连接已有端口。launch 模式用独立 profile，不影响你的日常账号。"""
    try:
        if body.mode == "attach":
            r = bl.attach(body.port)
        else:
            r = bl.launch(port=body.port, path=body.path)
        # 无论哪种模式，都确保 miora 页面已打开
        r["opened"] = bl.open_start_url(body.port)
        # 已经登录了就直接导入，不必再点按钮
        try:
            if bl.grab_credentials(body.port):
                r["auto_imported"] = bl.import_from_browser(body.port, persist=True)
        except Exception as e:
            # 不要静默吞掉——前端需要知道为什么没自动导入
            r["auto_import_error"] = f"{type(e).__name__}: {e}"[:200]
        # 否则开启后台监听
        r["watching"] = bl.watch_and_import(body.port)
        return r
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/admin/browser/close")
def browser_close(session_id: str):
    return bl.close_session(session_id)


class BlImportReq(BaseModel):
    port: int = 9222
    label: str = ""
    persist: bool = True
    watch: bool = False          # True=后台监听，登录后自动导入（无需再点按钮）


@app.post("/admin/browser/import")
def browser_import(body: BlImportReq):
    """从浏览器读取登录态并导入账号池。**只读 localStorage，不碰密码。**

    ``watch=true`` 时不阻塞：后台轮询，检测到登录态立即自动导入。
    """
    try:
        if body.watch:
            return bl.watch_and_import(body.port, label=body.label)
        return bl.import_from_browser(body.port, label=body.label, persist=body.persist)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/admin/browser/watch")
def browser_watch_status():
    """后台监听的进度（控制台轮询这个实现"点了就不用再点"）。"""
    return bl.watch_status()


@app.post("/admin/browser/watch/cancel")
def browser_watch_cancel(port: int):
    return bl.cancel_watch(port)


@app.get("/admin/accounts")
def accounts():
    return {"object": "list", "data": db.list_accounts()}


@app.get("/admin/logs")
def logs(limit: int = 80):
    return {"object": "list", "data": db.recent_logs(limit)}


# ---------------------------------------------------------------- console 数据源


@app.get("/v1/stats")
def stats():
    """console 唯一数据源。含上游实时余额 + 官方账单。"""
    acc = db.pick_account()
    out: dict[str, Any] = {
        "bridge": {"port": PORT, "db": DB_PATH, "started": START},
        "models": {}, "accounts": [], "media": [], "tasks": [], "costs": [],
        "logs": [], "credits": None, "usage": [], "upstream_error": None,
    }
    try:
        data = load_models()
        out["models"] = {"total": len(data["models"]), "userTier": data["userTier"],
                         "byType": {}, "list": []}
        by: dict[str, list] = {}
        for m in data["models"]:
            by.setdefault(m["contentType"], []).append(m)
        for ct, items in by.items():
            out["models"]["byType"][ct] = {
                "total": len(items),
                "available": sum(1 for i in items if i.get("available")),
            }
            for i in items:
                c = i.get("capabilities") or {}
                mt = i["contentType"]
                out["models"]["list"].append({
                    "modelId": i["modelId"], "label": i.get("label"),
                    "contentType": mt, "available": i.get("available"),
                    "priceTier": (i.get("extra") or {}).get("priceTier"),
                    "resolutions": derive_resolutions(norm_mt(mt), c),
                    "defaultResolution": c.get("defaultResolution"),
                    "aspectRatios": c.get("aspectRatios") or [],
                    "tools": derive_tools(norm_mt(mt), c),
                    "refMax": derive_ref_max(norm_mt(mt), c),
                    "refInfo": derive_ref_info(norm_mt(mt), c),
                    "estSeconds": c.get("estimatedDurationSeconds"),
                    "secondsRange": c.get("secondsRange"),
                    "defaultSeconds": c.get("defaultSeconds"),
                    "generateTypes": c.get("generateTypes"),
                    "resultFormats": c.get("resultFormats"),
                })
    except Exception as e:
        out["upstream_error"] = f"models: {e}"

    out["accounts"] = db.list_accounts()
    if acc:
        rem = sync_credits(acc)
        out["accounts"] = db.list_accounts()
        out["credits"] = rem
        try:
            out["usage"] = official_usage(acc["auth_token"])[:30]
        except Exception as e:
            out["upstream_error"] = (out["upstream_error"] or "") + f" | usage: {e}"

    out["media"] = [{**m, "url_expired": (m.get("url_expires_at") or 0) < db.now()}
                    for m in db.list_media(40)]
    out["tasks"] = db.list_tasks(40)
    out["costs"] = db.cost_summary()
    out["logs"] = db.recent_logs(60)
    return out


@app.get("/favicon.ico", response_class=PlainTextResponse)
def favicon():
    return ""


@app.get("/console", response_class=HTMLResponse)
def console():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health", response_class=PlainTextResponse)
def health():
    try:
        d = load_models()
        rem = None
        acc = db.pick_account()
        if acc:
            rem = sync_credits(acc)
        return (f"ok models={len(d['models'])} tier={d['userTier']} "
                f"credits={rem} db={os.path.basename(DB_PATH)}")
    except Exception as e:
        return f"degraded: {e}"


START = time.time()


def main() -> None:
    db.init_db()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
