"""回归准备专用接口：历史文件、导入回执、模板领取/试跑、凭据下载。

为兼容不同操作习惯，同一组能力同时挂在 ``/api/regression/*`` 与
``/api/sample_store/*`` / ``/api/page_parser/regression/*`` 别名下。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from app.services.regression import regression_service as svc

router = APIRouter(prefix="/api", tags=["回归准备"])


def _decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _parse_multipart(raw: bytes, content_type: str) -> tuple[dict[str, str], list[tuple[str, str, bytes]]]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not match:
        return {}, []
    boundary = (match.group(1) or match.group(2)).strip().encode()
    fields: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    for part in raw.split(b"--" + boundary):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        header_blob, sep, body = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        headers = header_blob.decode("utf-8", errors="ignore")
        cd = re.search(r"Content-Disposition:\s*([^\r\n]+)", headers, re.I)
        disposition = cd.group(1) if cd else ""
        name_m = re.search(r'name="([^"]*)"', disposition)
        file_m = re.search(r'filename="([^"]*)"', disposition)
        name = name_m.group(1) if name_m else ""
        if file_m:
            files.append((name, file_m.group(1), body))
        elif name:
            fields[name] = _decode_bytes(body).strip()
    return fields, files


async def _json_body(request: Request) -> dict[str, Any]:
    """尽量宽容地受理 JSON / JSON 数组 / form / multipart / 纯文本文件。"""
    content_type = request.headers.get("content-type", "")
    raw = await request.body()

    if "application/json" in content_type:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {"records": data}
        except Exception:
            return {"content": _decode_bytes(raw)}

    if "multipart/form-data" in content_type:
        fields, files = _parse_multipart(raw, content_type)
        payload: dict[str, Any] = dict(fields)
        if files:
            _, filename, body = files[0]
            payload.setdefault("file_name", filename)
            payload["content"] = _decode_bytes(body)
        return payload

    if "application/x-www-form-urlencoded" in content_type:
        parsed = {k: v[0] for k, v in parse_qs(_decode_bytes(raw)).items() if v}
        return dict(parsed)

    if raw:
        return {"content": _decode_bytes(raw)}
    return {}


# ---------------- 历史文件 ----------------

@router.get("/regression/history-files")
@router.get("/sample_store/history-files")
def history_files() -> dict[str, Any]:
    return {"ok": True, "items": svc.list_history_files()}


@router.get("/regression/history-files/{file_id}")
@router.get("/sample_store/history-files/{file_id}")
def history_file_detail(file_id: str) -> dict[str, Any]:
    item = svc.get_history_file(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="历史页面文件不存在")
    return {"ok": True, "item": item}


@router.get("/regression/scenario")
def scenario() -> dict[str, Any]:
    """前端一键演练按钮使用的标准场景数据。"""
    return {
        "ok": True,
        "site": "云街甄选",
        "wrong_site": "南岸杂货",
        "batch": "RG-20261126-I1I2",
        "first_file_id": "HF-SCENARIO-70",
        "corrected_file_id": "HF-SCENARIO-79",
        "history_files": svc.list_history_files(),
    }


# ---------------- 导入回执 ----------------

@router.post("/regression/imports")
@router.post("/regression/import")
@router.post("/sample_store/imports")
@router.post("/sample_store/import")
@router.post("/sample_store/upload")
@router.post("/sample_store/regression/imports")
async def create_import(request: Request) -> dict[str, Any]:
    payload = await _json_body(request)
    return svc.apply_import(payload)


@router.get("/regression/imports")
@router.get("/sample_store/imports")
def list_imports(batch: str | None = None) -> dict[str, Any]:
    items = svc.list_imports()
    if batch:
        items = [i for i in items if i.get("batch") == batch]
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/regression/imports/{import_no}")
@router.get("/sample_store/imports/{import_no}")
@router.get("/sample_store/import/{import_no}")
@router.get("/sample_store/receipts/{import_no}")
@router.get("/sample_store/receipt/{import_no}")
def get_import(import_no: str) -> dict[str, Any]:
    receipt = svc.get_import(import_no)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"导入回执 {import_no} 不存在")
    return receipt


@router.post("/regression/imports/{import_no}/retry")
@router.post("/sample_store/imports/{import_no}/retry")
async def retry_import(import_no: str, request: Request) -> dict[str, Any]:
    if svc.get_import(import_no) is None:
        raise HTTPException(status_code=404, detail=f"原导入请求 {import_no} 不存在")
    payload = await _json_body(request)
    payload["import_no"] = import_no
    return svc.apply_import(payload)


# ---------------- 模板领取 + 试跑批次 ----------------

@router.get("/regression/claimable")
@router.get("/page_parser/regression/claimable")
def claimable(batch: str = "", site: str = "") -> dict[str, Any]:
    return svc.claimable_samples(batch=batch, site=site)


@router.post("/regression/claim")
@router.post("/page_parser/regression/claim")
@router.post("/page_parser/claim-regression")
async def claim_and_run(request: Request) -> dict[str, Any]:
    payload = await _json_body(request)
    # 领取动作本身先给出候选样本，再形成同批试跑回执
    claim = svc.claimable_samples(
        batch=str(payload.get("batch", "")), site=str(payload.get("site", "")))
    trial = svc.create_trial(payload)
    trial["claim"] = {"claimed": len(claim["samples"]), "batches": claim["batches"]}
    return trial


@router.post("/regression/trials")
@router.post("/regression/run")
@router.post("/sample_store/trials")
@router.post("/sample_store/regressions")
@router.post("/sample_store/regression/run")
async def create_trial(request: Request) -> dict[str, Any]:
    return svc.create_trial(await _json_body(request))


@router.get("/regression/trials")
@router.get("/sample_store/trials")
@router.get("/sample_store/regressions")
def list_trials(batch: str = "") -> dict[str, Any]:
    items = svc.list_trials(batch=batch)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/regression/trials/{ref}")
@router.get("/sample_store/trials/{ref}")
@router.get("/sample_store/regressions/{ref}")
def get_trial(ref: str) -> dict[str, Any]:
    trial = svc.get_trial(ref)
    if trial is None:
        raise HTTPException(status_code=404, detail=f"试跑批次 {ref} 不存在")
    return trial


@router.post("/regression/trials/{ref}/refresh")
@router.post("/sample_store/trials/{ref}/refresh")
def refresh_trial(ref: str) -> dict[str, Any]:
    trial = svc.refresh_trial(ref)
    if trial is None:
        raise HTTPException(status_code=404, detail="试跑批次不存在")
    return trial


@router.post("/regression/trials/{ref}/reaccept")
@router.post("/sample_store/trials/{ref}/reaccept")
def reaccept_trial(ref: str) -> dict[str, Any]:
    trial = svc.reaccept_trial(ref)
    if trial is None:
        raise HTTPException(status_code=404, detail="试跑批次不存在")
    return trial


@router.get("/regression/trials/{ref}/receipt")
@router.get("/sample_store/trials/{ref}/receipt")
@router.get("/sample_store/regressions/{ref}/receipt")
@router.get("/regression/receipts/{ref}/download")
def download_receipt(ref: str, request: Request, credential: str = "", format: str = "csv",
                     token: str = "", key: str = "") -> Response:
    trial = svc.get_trial(ref)
    if trial is None:
        raise HTTPException(status_code=404, detail="试跑回执不存在")
    supplied = credential or token or key or request.headers.get("x-download-credential", "")
    if supplied != trial.get("credential"):
        raise HTTPException(status_code=403, detail="下载凭据无效或缺失，请使用试跑窗口中展示的下载凭据")
    fmt = "json" if format.lower() == "json" else "csv"
    if fmt == "json":
        import json
        body = json.dumps(trial, ensure_ascii=False, indent=2)
        media = "application/json; charset=utf-8"
    else:
        body = svc.render_receipt_csv(trial)
        media = "text/csv; charset=utf-8"
    return Response(content=body, media_type=media, headers=svc.receipt_headers(trial, fmt))
