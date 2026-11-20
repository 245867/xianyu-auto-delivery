"""
素材库（A4）—— 图片资产与文案片段，本地 SQLite + 本地磁盘，不上传任何第三方。

为什么要有这个：
闲管家/芹菜/一尘都有素材库，虚拟商品上架的痛点是"同一套图反复找、反复传"。
我们把图片收进本地库，按素材（一组图）管理，发布时直接取绝对路径交给
opportunity_publisher 上传 CDN。

设计约束（与 Gate 0 一致）：
- 纯本地：目录 data/materials/，数据库 data/material_library.db，不出本机。
- 只收图片与纯文本；图片必须能被 PIL 打开，防"改后缀的假图"。
- 单文件 ≤ 10MB、单素材 ≤ 9 张（闲鱼主图上限 9）、全库 ≤ 2000 个文件。
- 路径一律由数据库中的相对路径反推，绝不接受外部传入的路径（防目录穿越）。
- 删除是软删目录（移到 .trash/），误删可手工找回。
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "material_library.db"
LIB_DIR = DATA_DIR / "materials"
TRASH_DIR = DATA_DIR / "materials" / ".trash"
THUMB_DIR = DATA_DIR / "materials" / ".thumbs"

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_MATERIAL = 9
MAX_LIBRARY_FILES = 2000
THUMB_SIZE = (480, 480)
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
KINDS = ("image", "text")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS materials (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'image',
            tags        TEXT DEFAULT '',
            note        TEXT DEFAULT '',
            content     TEXT DEFAULT '',
            cover_file  INTEGER DEFAULT 0,
            file_count  INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS material_files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL,
            rel_path    TEXT NOT NULL,
            thumb_rel   TEXT DEFAULT '',
            orig_name   TEXT DEFAULT '',
            ext         TEXT DEFAULT '',
            size        INTEGER DEFAULT 0,
            width       INTEGER DEFAULT 0,
            height      INTEGER DEFAULT 0,
            sha1        TEXT DEFAULT '',
            sort_order  INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uk_material_sha1
            ON material_files(material_id, sha1);
        CREATE INDEX IF NOT EXISTS idx_material_files_mid
            ON material_files(material_id, sort_order);
    """)
    return conn


# ---------------------------------------------------------------- 工具

def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _abs(rel: str) -> Optional[Path]:
    """把数据库里的相对路径还原成绝对路径，并确认落在库目录内。"""
    if not rel:
        return None
    lib = LIB_DIR.resolve()
    p = (LIB_DIR / rel).resolve()
    try:
        p.relative_to(lib)
    except ValueError:
        return None
    return p


def _clean_tags(tags: Any) -> str:
    if isinstance(tags, (list, tuple)):
        parts = [str(t).strip() for t in tags]
    else:
        parts = str(tags or "").replace("，", ",").replace("、", ",").split(",")
    out, seen = [], set()
    for p in parts:
        p = p.strip()[:20]
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ",".join(out[:8])


def _guess_ext(filename: str, data: bytes) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in ALLOWED_EXT:
        return ext
    # 无后缀时按魔数兜底
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"GIF":
        return ".gif"
    return ""


def _probe_image(data: bytes) -> Tuple[bool, int, int, str]:
    """用 PIL 真实解码，返回 (ok, width, height, error)。"""
    try:
        from PIL import Image
    except Exception as e:  # pragma: no cover
        return False, 0, 0, f"缺少 Pillow：{e}"
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
            if w < 40 or h < 40:
                return False, w, h, f"图片太小（{w}×{h}），至少 40×40"
            if w * h > 60_000_000:
                return False, w, h, f"图片像素过大（{w}×{h}）"
            return True, w, h, ""
    except Exception as e:
        return False, 0, 0, f"不是有效图片：{str(e)[:80]}"


def _make_thumb(data: bytes, sha1: str) -> str:
    """生成缩略图，返回相对路径；失败返回空串（页面会回退到原图）。"""
    try:
        from PIL import Image
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail(THUMB_SIZE)
            name = f"{sha1}.jpg"
            out = THUMB_DIR / name
            im.save(out, "JPEG", quality=82, optimize=True)
        return f".thumbs/{name}"
    except Exception:
        return ""


def _refresh_count(conn: sqlite3.Connection, material_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM material_files WHERE material_id=?", (material_id,)
    ).fetchone()
    c = int(row["c"]) if row else 0
    cover = conn.execute(
        "SELECT id FROM material_files WHERE material_id=? ORDER BY sort_order, id LIMIT 1",
        (material_id,),
    ).fetchone()
    conn.execute(
        "UPDATE materials SET file_count=?, cover_file=?, updated_at=? WHERE id=?",
        (c, int(cover["id"]) if cover else 0, _now(), material_id),
    )
    return c


def _row_to_material(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "tags": [t for t in str(row["tags"] or "").split(",") if t],
        "tags_text": row["tags"] or "",
        "note": row["note"] or "",
        "content": row["content"] or "",
        "file_count": row["file_count"] or 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_file(row: sqlite3.Row) -> Dict[str, Any]:
    p = _abs(row["rel_path"])
    return {
        "id": row["id"],
        "material_id": row["material_id"],
        "name": row["orig_name"],
        "ext": row["ext"],
        "size": row["size"],
        "width": row["width"],
        "height": row["height"],
        "size_text": _size_text(row["size"] or 0),
        "path": str(p) if p else "",
        "thumb_url": f"/api/materials/file/{row['id']}/thumb",
        "raw_url": f"/api/materials/file/{row['id']}/raw",
        "created_at": row["created_at"],
    }


def _size_text(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


# ---------------------------------------------------------------- 素材（组）

def create_material(name: str, kind: str = "image", tags: Any = "",
                    note: str = "", content: str = "") -> Dict[str, Any]:
    name = (name or "").strip()[:80]
    if not name:
        return {"ok": False, "error": "素材名称不能为空"}
    kind = kind if kind in KINDS else "image"
    if kind == "text" and not (content or "").strip():
        return {"ok": False, "error": "文案素材需要填写内容"}
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO materials(name,kind,tags,note,content,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, kind, _clean_tags(tags), (note or "")[:500],
             (content or "")[:20000], _now(), _now()),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        conn.close()


def update_material(material_id: int, **fields) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "素材不存在"}
        sets, args = [], []
        if "name" in fields:
            name = str(fields["name"] or "").strip()[:80]
            if not name:
                return {"ok": False, "error": "素材名称不能为空"}
            sets.append("name=?"); args.append(name)
        if "tags" in fields:
            sets.append("tags=?"); args.append(_clean_tags(fields["tags"]))
        if "note" in fields:
            sets.append("note=?"); args.append(str(fields["note"] or "")[:500])
        if "content" in fields:
            sets.append("content=?"); args.append(str(fields["content"] or "")[:20000])
        if not sets:
            return {"ok": True, "id": material_id, "changed": 0}
        sets.append("updated_at=?"); args.append(_now())
        args.append(material_id)
        conn.execute(f"UPDATE materials SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
        return {"ok": True, "id": material_id, "changed": 1}
    finally:
        conn.close()


def delete_material(material_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT id FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "素材不存在"}
        d = LIB_DIR / str(material_id)
        if d.exists():
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            target = TRASH_DIR / f"{material_id}_{int(time.time())}"
            try:
                shutil.move(str(d), str(target))
            except Exception:
                shutil.rmtree(d, ignore_errors=True)
        conn.execute("DELETE FROM material_files WHERE material_id=?", (material_id,))
        conn.execute("DELETE FROM materials WHERE id=?", (material_id,))
        conn.commit()
        return {"ok": True, "id": material_id}
    finally:
        conn.close()


def list_materials(keyword: str = "", tag: str = "", kind: str = "",
                   limit: int = 200) -> Dict[str, Any]:
    conn = _conn()
    try:
        sql = "SELECT * FROM materials WHERE 1=1"
        args: List[Any] = []
        if keyword:
            sql += " AND (name LIKE ? OR tags LIKE ? OR note LIKE ?)"
            kw = f"%{keyword}%"
            args += [kw, kw, kw]
        if tag:
            sql += " AND (',' || tags || ',') LIKE ?"
            args.append(f"%,{tag},%")
        if kind in KINDS:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(500, int(limit or 200))))
        rows = conn.execute(sql, args).fetchall()
        items = []
        for r in rows:
            m = _row_to_material(r)
            m["cover_url"] = (f"/api/materials/file/{r['cover_file']}/thumb"
                              if r["cover_file"] else "")
            items.append(m)
        tags = _all_tags(conn)
        return {"ok": True, "items": items, "total": len(items), "tags": tags,
                "stats": _stats(conn)}
    finally:
        conn.close()


def _all_tags(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    counter: Dict[str, int] = {}
    for row in conn.execute("SELECT tags FROM materials").fetchall():
        for t in str(row["tags"] or "").split(","):
            t = t.strip()
            if t:
                counter[t] = counter.get(t, 0) + 1
    return [{"tag": k, "count": v}
            for k, v in sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:40]]


def _stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    m = conn.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"]
    f = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM material_files").fetchone()
    return {"materials": int(m), "files": int(f["c"]), "bytes": int(f["s"]),
            "bytes_text": _size_text(int(f["s"])),
            "max_files": MAX_LIBRARY_FILES}


def get_material(material_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "素材不存在"}
        m = _row_to_material(row)
        files = conn.execute(
            "SELECT * FROM material_files WHERE material_id=? ORDER BY sort_order, id",
            (material_id,),
        ).fetchall()
        m["files"] = [_row_to_file(f) for f in files]
        m["paths"] = [f["path"] for f in m["files"] if f["path"]]
        return {"ok": True, "material": m}
    finally:
        conn.close()


# ---------------------------------------------------------------- 文件

def add_file(material_id: int, filename: str, data: bytes) -> Dict[str, Any]:
    if not data:
        return {"ok": False, "error": "空文件"}
    if len(data) > MAX_FILE_BYTES:
        return {"ok": False, "error": f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB"}
    ext = _guess_ext(filename, data)
    if not ext:
        return {"ok": False, "error": "只支持 jpg/png/webp/gif/bmp 图片"}
    ok, w, h, err = _probe_image(data)
    if not ok:
        return {"ok": False, "error": err}

    sha = _sha1(data)
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "素材不存在"}
        dup = conn.execute(
            "SELECT id FROM material_files WHERE material_id=? AND sha1=?",
            (material_id, sha),
        ).fetchone()
        if dup:
            return {"ok": True, "id": int(dup["id"]), "dup": True,
                    "message": "这张图已在该素材里，未重复添加"}
        cnt = conn.execute("SELECT COUNT(*) c FROM material_files WHERE material_id=?",
                           (material_id,)).fetchone()["c"]
        if int(cnt) >= MAX_FILES_PER_MATERIAL:
            return {"ok": False, "error": f"单个素材最多 {MAX_FILES_PER_MATERIAL} 张（闲鱼主图上限）"}
        total = conn.execute("SELECT COUNT(*) c FROM material_files").fetchone()["c"]
        if int(total) >= MAX_LIBRARY_FILES:
            return {"ok": False, "error": f"素材库已达上限 {MAX_LIBRARY_FILES} 个文件，请先清理"}

        rel_dir = str(material_id)
        abs_dir = LIB_DIR / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{sha[:12]}{ext}"
        rel_path = f"{rel_dir}/{fname}"
        (LIB_DIR / rel_path).write_bytes(data)
        thumb_rel = _make_thumb(data, sha)

        order = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 o FROM material_files WHERE material_id=?",
            (material_id,),
        ).fetchone()["o"]
        cur = conn.execute(
            "INSERT INTO material_files(material_id,rel_path,thumb_rel,orig_name,ext,size,"
            "width,height,sha1,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (material_id, rel_path, thumb_rel, (filename or fname)[:120], ext,
             len(data), w, h, sha, int(order), _now()),
        )
        _refresh_count(conn, material_id)
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "dup": False,
                "file": _row_to_file(conn.execute(
                    "SELECT * FROM material_files WHERE id=?", (cur.lastrowid,)).fetchone())}
    except Exception as e:
        return {"ok": False, "error": f"保存失败：{str(e)[:120]}"}
    finally:
        conn.close()


def delete_file(file_id: int) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "文件不存在"}
        mid = int(row["material_id"])
        p = _abs(row["rel_path"])
        if p and p.exists():
            try:
                p.unlink()
            except Exception:
                pass
        th = _abs(row["thumb_rel"]) if row["thumb_rel"] else None
        if th and th.exists():
            try:
                th.unlink()
            except Exception:
                pass
        conn.execute("DELETE FROM material_files WHERE id=?", (file_id,))
        _refresh_count(conn, mid)
        conn.commit()
        return {"ok": True, "id": file_id, "material_id": mid}
    finally:
        conn.close()


def reorder_file(file_id: int, direction: int) -> Dict[str, Any]:
    """把某张图前移/后移一位（决定发布时的主图顺序）。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "文件不存在"}
        mid = int(row["material_id"])
        files = conn.execute(
            "SELECT id FROM material_files WHERE material_id=? ORDER BY sort_order, id",
            (mid,),
        ).fetchall()
        ids = [int(f["id"]) for f in files]
        if file_id not in ids:
            return {"ok": False, "error": "文件不存在"}
        i = ids.index(file_id)
        j = i - 1 if direction < 0 else i + 1
        if j < 0 or j >= len(ids):
            return {"ok": True, "changed": 0}
        ids[i], ids[j] = ids[j], ids[i]
        for order, fid in enumerate(ids):
            conn.execute("UPDATE material_files SET sort_order=? WHERE id=?", (order, fid))
        _refresh_count(conn, mid)
        conn.commit()
        return {"ok": True, "changed": 1}
    finally:
        conn.close()


def get_file(file_id: int, thumb: bool = False) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return None
        rel = row["thumb_rel"] if (thumb and row["thumb_rel"]) else row["rel_path"]
        p = _abs(rel)
        if not p or not p.exists():
            return None
        # 缩略图是转码后的 JPEG，扩展名必须按实际文件算，否则 Content-Type 会跟内容不符
        real_ext = p.suffix.lstrip(".").lower() or (row["ext"] or "").lstrip(".").lower()
        name = p.name if thumb else (row["orig_name"] or p.name)
        return {"path": p, "name": name, "ext": real_ext}
    finally:
        conn.close()


def material_paths(material_id: int) -> Dict[str, Any]:
    """取某素材的绝对路径列表，直接喂给发布接口。"""
    r = get_material(material_id)
    if not r.get("ok"):
        return r
    m = r["material"]
    if m["kind"] == "text":
        return {"ok": False, "error": "这是文案素材，没有图片路径"}
    if not m["paths"]:
        return {"ok": False, "error": "这个素材里还没有图片"}
    return {"ok": True, "id": material_id, "name": m["name"],
            "paths": m["paths"], "count": len(m["paths"])}


# ---------------------------------------------------------------- 从 URL 导入

def _url_is_safe(url: str) -> Tuple[bool, str]:
    """只允许 http/https 且不指向内网/本机，避免被当成 SSRF 跳板。"""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False, "URL 解析失败"
    if u.scheme not in ("http", "https"):
        return False, "只支持 http/https 图片地址"
    host = (u.hostname or "").lower()
    if not host:
        return False, "URL 缺少主机名"
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local"):
        return False, "不允许内网地址"
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "不允许内网地址"
    except ValueError:
        pass
    return True, ""


def import_from_urls(material_id: int, urls: List[str]) -> Dict[str, Any]:
    """把外部图片下载进素材库（闲鱼图床直填会被拒，所以必须先落地再上传 CDN）。"""
    import requests
    added, failed, dups = [], [], []
    for raw in (urls or [])[:MAX_FILES_PER_MATERIAL]:
        url = str(raw or "").strip()
        if not url:
            continue
        ok, err = _url_is_safe(url)
        if not ok:
            failed.append({"url": url[:120], "error": err})
            continue
        try:
            resp = requests.get(url, timeout=20, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                failed.append({"url": url[:120], "error": f"HTTP {resp.status_code}"})
                continue
            buf = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                buf.extend(chunk)
                if len(buf) > MAX_FILE_BYTES:
                    break
            if len(buf) > MAX_FILE_BYTES:
                failed.append({"url": url[:120], "error": "文件过大"})
                continue
            name = url.split("?")[0].rsplit("/", 1)[-1] or "image.jpg"
            r = add_file(material_id, name, bytes(buf))
            if r.get("ok"):
                (dups if r.get("dup") else added).append(url[:120])
            else:
                failed.append({"url": url[:120], "error": r.get("error", "")})
        except Exception as e:
            failed.append({"url": url[:120], "error": str(e)[:80]})
    conn = _conn()
    try:
        _refresh_count(conn, material_id)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "added": len(added), "dup": len(dups),
            "failed": failed, "added_urls": added, "dup_urls": dups}


def stats() -> Dict[str, Any]:
    conn = _conn()
    try:
        return {"ok": True, **_stats(conn), "tags": _all_tags(conn),
                "limits": {"max_file_mb": MAX_FILE_BYTES // 1024 // 1024,
                           "max_files_per_material": MAX_FILES_PER_MATERIAL,
                           "max_library_files": MAX_LIBRARY_FILES}}
    finally:
        conn.close()
