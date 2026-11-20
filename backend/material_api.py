"""
素材库路由（A4）
- GET    /materials                      素材库页面
- GET    /api/materials                  列表（keyword/tag/kind）
- POST   /api/materials                  新建素材组
- GET    /api/materials/{id}             详情（含文件与可直接发布的绝对路径）
- POST   /api/materials/{id}             改名/标签/备注/文案
- DELETE /api/materials/{id}             删除（图片进 .trash）
- POST   /api/materials/{id}/upload      上传图片（multipart，可多张）
- POST   /api/materials/{id}/import-url  从图片 URL 导入
- POST   /api/materials/{id}/files/{fid}/move?dir=up|down   调整主图顺序
- DELETE /api/materials/files/{fid}      删除单张
- GET    /api/materials/file/{fid}/raw   原图
- GET    /api/materials/file/{fid}/thumb 缩略图
- GET    /api/materials/stats            用量与上限
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import material_library as lib

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

MEDIA_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
}


@router.get("/materials", include_in_schema=False)
async def materials_page():
    return FileResponse(PROJECT_ROOT / "static" / "materials.html")


@router.get("/api/materials")
async def materials_list(keyword: str = "", tag: str = "", kind: str = "",
                         limit: int = 200):
    try:
        return lib.list_materials(keyword=keyword, tag=tag, kind=kind, limit=limit)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


@router.post("/api/materials")
async def materials_create(body: Dict[str, Any] = Body(...)):
    return lib.create_material(
        name=str(body.get("name") or ""),
        kind=str(body.get("kind") or "image"),
        tags=body.get("tags") or "",
        note=str(body.get("note") or ""),
        content=str(body.get("content") or ""),
    )


@router.get("/api/materials/stats")
async def materials_stats():
    try:
        return lib.stats()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/materials/{material_id}")
async def materials_detail(material_id: int):
    return lib.get_material(material_id)


@router.post("/api/materials/{material_id}")
async def materials_update(material_id: int, body: Dict[str, Any] = Body(...)):
    allowed = {k: v for k, v in (body or {}).items()
               if k in ("name", "tags", "note", "content")}
    if not allowed:
        return {"ok": False, "error": "没有可更新的字段"}
    return lib.update_material(material_id, **allowed)


@router.delete("/api/materials/{material_id}")
async def materials_delete(material_id: int):
    return lib.delete_material(material_id)


@router.post("/api/materials/{material_id}/upload")
async def materials_upload(material_id: int, files: List[UploadFile] = File(...)):
    results = []
    for f in files[:9]:
        try:
            data = await f.read()
        except Exception as e:
            results.append({"ok": False, "name": f.filename, "error": str(e)[:100]})
            continue
        r = lib.add_file(material_id, f.filename or "", data)
        r["name"] = f.filename or ""
        results.append(r)
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_n > 0, "uploaded": ok_n, "total": len(results),
            "results": results,
            "error": "" if ok_n else (results[0].get("error") if results else "没有收到文件")}


@router.post("/api/materials/{material_id}/import-url")
async def materials_import_url(material_id: int, body: Dict[str, Any] = Body(...)):
    raw = body.get("urls")
    if isinstance(raw, str):
        urls = [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]
    elif isinstance(raw, list):
        urls = [str(u).strip() for u in raw if str(u).strip()]
    else:
        urls = []
    if not urls:
        return {"ok": False, "error": "没有提供图片地址"}
    return lib.import_from_urls(material_id, urls)


@router.post("/api/materials/{material_id}/files/{file_id}/move")
async def materials_move_file(material_id: int, file_id: int, dir: str = Query("up")):
    return lib.reorder_file(file_id, -1 if dir == "up" else 1)


@router.delete("/api/materials/files/{file_id}")
async def materials_delete_file(file_id: int):
    return lib.delete_file(file_id)


@router.get("/api/materials/{material_id}/paths")
async def materials_paths(material_id: int):
    """发布用：直接拿到该素材全部图片的本地绝对路径。"""
    return lib.material_paths(material_id)


@router.get("/api/materials/file/{file_id}/raw")
async def materials_raw(file_id: int):
    f = lib.get_file(file_id, thumb=False)
    if not f:
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    return FileResponse(f["path"], media_type=MEDIA_TYPES.get(f["ext"], "application/octet-stream"),
                        filename=f["name"])


@router.get("/api/materials/file/{file_id}/thumb")
async def materials_thumb(file_id: int):
    f = lib.get_file(file_id, thumb=True)
    if not f:
        f = lib.get_file(file_id, thumb=False)
    if not f:
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    return FileResponse(f["path"], media_type=MEDIA_TYPES.get(f["ext"], "application/octet-stream"))
