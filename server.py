import os
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any
import io

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

# 路径定义
BASE_DIR = Path(__file__).resolve().parent
PHOTO_DIR = BASE_DIR / "photoes"
STATIC_DIR = BASE_DIR / "static"
CACHE_DIR = BASE_DIR / ".cache" / "thumbs"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="画廊 - Gallery Server")

# 允许跨域（方便本地多端口调试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}

# 内存缓存图片元数据 (path -> (width, height, size, mtime))
_meta_cache: Dict[str, Dict[str, Any]] = {}


def get_image_meta(file_path: Path) -> Dict[str, Any]:
    key = str(file_path)
    mtime = file_path.stat().st_mtime
    size = file_path.stat().st_size
    
    if key in _meta_cache and _meta_cache[key].get("mtime") == mtime:
        return _meta_cache[key]
    
    width, height = 800, 600
    try:
        with Image.open(file_path) as img:
            width, height = img.size
    except Exception:
        pass
    
    meta = {
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 4) if height > 0 else 1.0,
        "size": size,
        "mtime": mtime,
    }
    _meta_cache[key] = meta
    return meta


@app.get("/api/series")
def list_series():
    """扫描 photoes 目录下的所有文件夹作为一个系列"""
    if not PHOTO_DIR.exists():
        return {"total": 0, "series": []}
    
    series_list = []
    
    # 获取所有子目录
    folders = [f for f in PHOTO_DIR.iterdir() if f.is_dir()]
    
    # 按照名称自然排序 (中文、英文、数字混合)
    def natural_sort_key(folder: Path):
        import re
        parts = re.split(r'(\d+)', folder.name)
        return [int(p) if p.isdigit() else p.lower() for p in parts]
    
    folders.sort(key=natural_sort_key)
    
    for folder in folders:
        # 查找该文件夹内的所有有效图片
        images = []
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                images.append(p)
        
        # 自然排序图片文件
        images.sort(key=lambda p: natural_sort_key(p))
        
        if not images:
            continue
            
        img_items = []
        for img_path in images:
            meta = get_image_meta(img_path)
            encoded_folder = urllib.parse.quote(folder.name)
            encoded_name = urllib.parse.quote(img_path.name)
            img_items.append({
                "name": img_path.name,
                "url": f"/api/image/{encoded_folder}/{encoded_name}",
                "thumb": f"/api/thumb/{encoded_folder}/{encoded_name}",
                "width": meta["width"],
                "height": meta["height"],
                "aspect_ratio": meta["aspect_ratio"],
                "size": meta["size"],
            })
            
        first_img = img_items[0]
        series_list.append({
            "id": folder.name,
            "title": folder.name,
            "count": len(img_items),
            "cover": first_img["url"],
            "cover_thumb": first_img["thumb"],
            "cover_aspect_ratio": first_img["aspect_ratio"],
            "cover_width": first_img["width"],
            "cover_height": first_img["height"],
            "images": img_items,
            "mtime": max(p.stat().st_mtime for p in images) if images else folder.stat().st_mtime
        })
        
    return {
        "total": len(series_list),
        "total_images": sum(s["count"] for s in series_list),
        "series": series_list
    }


@app.get("/api/image/{folder}/{filename}")
def get_original_image(folder: str, filename: str):
    """返回原始图片文件，带有强缓存支持"""
    folder = urllib.parse.unquote(folder)
    filename = urllib.parse.unquote(filename)
    
    target_path = PHOTO_DIR / folder / filename
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
        
    # 防止路径穿越攻击
    try:
        target_path.resolve().relative_to(PHOTO_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径")
        
    ext = target_path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".avif": "image/avif"
    }
    media_type = media_types.get(ext, "application/octet-stream")
    
    headers = {
        "Cache-Control": "public, max-age=86400",
    }
    return FileResponse(target_path, media_type=media_type, headers=headers)


@app.get("/api/thumb/{folder}/{filename}")
def get_thumbnail(folder: str, filename: str, max_w: int = 800):
    """生成并返回高质量缩略图（WebP 格式），带磁盘持久化缓存"""
    folder = urllib.parse.unquote(folder)
    filename = urllib.parse.unquote(filename)
    
    src_path = PHOTO_DIR / folder / filename
    if not src_path.exists() or not src_path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
        
    try:
        src_path.resolve().relative_to(PHOTO_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="非法路径")
        
    # 构造唯一缓存文件名
    mtime = int(src_path.stat().st_mtime)
    cache_filename = f"{folder}_{filename}_{max_w}_{mtime}.webp"
    # 清理缓存文件名中的非法字符
    safe_cache_filename = "".join([c if c.isalnum() or c in "._-" else "_" for c in cache_filename])
    thumb_path = CACHE_DIR / safe_cache_filename
    
    if thumb_path.exists():
        return FileResponse(
            thumb_path, 
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=604800"}
        )
        
    try:
        with Image.open(src_path) as img:
            # 保持比例缩放
            w, h = img.size
            if w > max_w:
                ratio = max_w / float(w)
                new_h = int(float(h) * float(ratio))
                img = img.resize((max_w, new_h), Image.Resampling.LANCZOS)
                
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img.save(thumb_path, "WEBP", quality=85)
            else:
                img = img.convert("RGB")
                img.save(thumb_path, "WEBP", quality=85)
                
        return FileResponse(
            thumb_path, 
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=604800"}
        )
    except Exception as e:
        # 如果缩略图生成失败，回退直接返回原图
        return get_original_image(folder, filename)


# 挂载静态文件
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>画廊服务正在启动中，请稍候刷新...</h1>")


if __name__ == "__main__":
    import uvicorn
    import webbrowser
    import threading
    import time
    import socket
    
    def find_free_port(start_port: int = 8000) -> int:
        for p in range(start_port, start_port + 50):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('127.0.0.1', p))
                    return p
            except OSError:
                continue
        return start_port
        
    port = find_free_port(8000)
    
    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://127.0.0.1:{port}")
        
    threading.Thread(target=open_browser, daemon=True).start()
    print(f"==================================================")
    print(f" 画廊服务已启动: http://127.0.0.1:{port}")
    print(f"==================================================")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
