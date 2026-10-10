import os
import uuid
import json
import asyncio
import socket
import urllib.parse
import ipaddress
import urllib.request
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp
import httpx

# --- CONFIGURATION ---
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 8000))

app = FastAPI(title="Universal Media Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- MODELS ---
class AnalyzeRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str
    mode: str
    output_format: str
    quality: str


# --- SECURITY & UTILS ---
def is_safe_url(url_str: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url_str))
        if parsed.scheme not in ("http", "https"):
            return False
        ip = socket.gethostbyname(parsed.hostname)
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback:
            return False
        return True
    except Exception:
        return False


# --- API ENDPOINTS ---


@app.post("/api/analyze")
async def analyze_url(req: AnalyzeRequest):
    url = str(req.url)
    if not is_safe_url(url):
        raise HTTPException(403, "Unsafe URL blocked.")

    # 1. Direct Image URL check
    is_img = any(
        url.lower().split("?")[0].endswith(ext)
        for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    )
    if is_img:
        return {
            "title": "Direct Image Link",
            "thumbnail": url,
            "thumbnails_list": [
                {"label": "Original Image", "res": "(Auto)", "url": url}
            ],
            "duration": 0,
            "source": "Web Image",
            "formats": [
                {
                    "format_id": "direct",
                    "mode": "image",
                    "ext": "jpg",
                    "quality": "Original Image",
                    "size_str": "Unknown Size",
                }
            ],
        }

    ydl_opts = {
        "extract_flat": False,
        "download": False,
        "quiet": True,
        "noplaylist": True,
    }

    try:
        info = await asyncio.to_thread(
            lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False)
        )
    except Exception as e:
        error_str = str(e).lower()
        # 2. Pinterest/Instagram Fallback Handler
        if "no video formats" in error_str or "unsupported" in error_str:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
                }
                async with httpx.AsyncClient(
                    follow_redirects=True, headers=headers
                ) as client:
                    resp = await client.get(url, timeout=7.0)
                    match = re.search(
                        r'<meta[^>]*property=[\'"]og:image[\'"][^>]*content=[\'"]([^\'"]+)[\'"]',
                        resp.text,
                        re.IGNORECASE,
                    )
                    if match:
                        img_url = match.group(1).replace("&amp;", "&")
                        title_match = re.search(
                            r'<meta[^>]*property=[\'"]og:title[\'"][^>]*content=[\'"]([^\'"]+)[\'"]',
                            resp.text,
                            re.IGNORECASE,
                        )
                        title = title_match.group(1) if title_match else "Image Post"
                        return {
                            "title": title,
                            "thumbnail": img_url,
                            "thumbnails_list": [
                                {
                                    "label": "HD Image",
                                    "res": "(Original)",
                                    "url": img_url,
                                }
                            ],
                            "duration": 0,
                            "source": "Social Media (Image)",
                            "formats": [],
                        }
            except Exception:
                pass
        raise HTTPException(
            status_code=400, detail="Analysis failed or no media found."
        )

    if not info:
        raise HTTPException(400, "Could not extract metadata.")

    thumbnails_data = []
    if info.get("extractor") == "youtube" and info.get("id"):
        vid_id = info["id"]
        thumbnails_data = [
            {
                "label": "HD Image",
                "res": "(1280x720)",
                "url": f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg",
            },
            {
                "label": "SD Image",
                "res": "(640x480)",
                "url": f"https://i.ytimg.com/vi/{vid_id}/sddefault.jpg",
            },
            {
                "label": "Normal Image",
                "res": "(480x360)",
                "url": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
            },
            {
                "label": "Normal Image",
                "res": "(320x180)",
                "url": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
            },
            {
                "label": "Normal Image",
                "res": "(120x90)",
                "url": f"https://i.ytimg.com/vi/{vid_id}/default.jpg",
            },
        ]
    else:
        thumb_url = info.get("thumbnail")
        if thumb_url:
            thumbnails_data = [
                {"label": "Standard Image", "res": "(Original)", "url": thumb_url}
            ]

    seen_qualities = set()
    available_formats = []
    raw_formats = sorted(
        info.get("formats", []),
        key=lambda x: (x.get("height") or 0, x.get("tbr") or 0),
        reverse=True,
    )

    for f in raw_formats:
        vcodec, acodec = f.get("vcodec", "none"), f.get("acodec", "none")
        if vcodec == "none" and acodec == "none":
            continue

        height = f.get("height") or 0
        ext = f.get("ext", "unknown")

        if ext not in ["mp4", "webm", "m4a"]:
            continue
        mode = "audio" if vcodec == "none" else "video"
        quality = (
            f"{height}p" if height else ("Audio" if mode == "audio" else "Unknown")
        )

        identifier = f"{mode}_{quality}_{ext}"
        if identifier in seen_qualities:
            continue
        seen_qualities.add(identifier)

        size = f.get("filesize") or f.get("filesize_approx")
        if not size and info.get("duration"):
            size = int(
                info.get("duration")
                * (f.get("vbr") or 0 + f.get("abr") or 0)
                * 1000
                / 8
            )

        format_id = f.get("format_id")
        if mode == "video" and acodec == "none":
            format_id = f"{format_id}+bestaudio/best"

        available_formats.append(
            {
                "format_id": format_id,
                "mode": mode,
                "ext": ext,
                "quality": quality,
                "size_str": (
                    f"{round(size / (1024*1024), 2)} MB" if size else "Size Unknown"
                ),
            }
        )

    return {
        "title": info.get("title", "Unknown Title"),
        "thumbnail": info.get("thumbnail"),
        "thumbnails_list": thumbnails_data,
        "duration": info.get("duration", 0),
        "source": info.get("extractor", "Platform"),
        "formats": available_formats,
    }


# Streaming Endpoints
@app.get("/api/download-image")
async def stream_image(url: str):
    if not is_safe_url(url):
        raise HTTPException(403, "Unsafe URL")

    ext = url.split(".")[-1].split("?")[0]
    if len(ext) > 4:
        ext = "jpg"
    filename = f"image_{uuid.uuid4().hex[:8]}.{ext}"

    async def image_generator():
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    yield chunk

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(image_generator(), headers=headers)


@app.get("/api/stream")
async def stream_media(url: str, format_id: str, mode: str, output_format: str):
    if not is_safe_url(url):
        raise HTTPException(403, "Unsafe URL")

    filename = f"media_{uuid.uuid4().hex[:8]}.{output_format}"

    # Run yt-dlp to stream directly to stdout
    async def media_generator():
        # Setup yt-dlp options to stream to stdout (-o -)
        cmd = ["yt-dlp", "-o", "-", "-f", format_id, "--quiet"]

        # Audio conversion if needed
        if mode == "audio":
            cmd.extend(["--extract-audio", "--audio-format", output_format])

        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )

        try:
            while True:
                chunk = await process.stdout.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                process.kill()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "application/octet-stream",
    }
    return StreamingResponse(media_generator(), headers=headers)


# --- WEB DASHBOARD WITH PREMIUM UI ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Universal Media Downloader</title>
    <style>
        :root {
            --bg-dark: #09090b;
            --bg-card: #18181b;
            --primary: #8b5cf6;
            --primary-hover: #7c3aed;
            --accent: #3b82f6;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --border: #27272a;
            --danger: #ef4444;
            --success: #10b981;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', system-ui, sans-serif; }
        body { background-color: var(--bg-dark); color: var(--text-main); line-height: 1.5; padding: 2rem; display: flex; justify-content: center;}
        .container { max-width: 950px; width: 100%; }
        
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
        h1 { font-size: 2rem; font-weight: 700; background: linear-gradient(90deg, var(--primary), var(--accent)); -webkit-background-clip: text; color: transparent; }
        
        .card { background-color: var(--bg-card); border-radius: 16px; padding: 1.8rem; margin-bottom: 2rem; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5); border: 1px solid var(--border); transition: transform 0.2s; }
        
        .input-group { display: flex; gap: 0.8rem; margin-bottom: 1rem; position: relative; }
        input[type="text"] { flex: 1; padding: 1rem 1.2rem; border-radius: 12px; border: 1px solid var(--border); background: #121214; color: white; outline: none; font-size: 1rem; transition: all 0.3s;}
        input[type="text"]:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.2); }
        
        button { padding: 1rem 1.8rem; border-radius: 12px; border: none; font-weight: 600; font-size: 1rem; cursor: pointer; transition: all 0.3s; color: white; display: flex; align-items: center; justify-content: center; gap: 8px;}
        button.btn-primary { background: linear-gradient(135deg, var(--primary), var(--accent)); box-shadow: 0 4px 15px rgba(139, 92, 246, 0.3); }
        button.btn-primary:hover { opacity: 0.9; transform: translateY(-1px); box-shadow: 0 6px 20px rgba(139, 92, 246, 0.4); }
        button.btn-secondary { background: #27272a; border: 1px solid #3f3f46; color: var(--text-main); }
        button.btn-secondary:hover { border-color: var(--primary); background: #3f3f46; }
        
        select { width: 100%; padding: 1rem; border-radius: 12px; border: 1px solid var(--border); background: #121214; color: white; margin-bottom: 1rem; font-size: 0.95rem; outline: none; appearance: none; cursor: pointer; }
        select:focus { border-color: var(--primary); }
        select:disabled { opacity: 0.6; cursor: not-allowed; }
        
        #media-preview { display: none; }
        .preview-grid { display: grid; grid-template-columns: 320px 1fr; gap: 2rem; align-items: start; }
        .img-wrapper { position: relative; border-radius: 12px; overflow: hidden; border: 1px solid var(--border); margin-bottom: 1rem; }
        .preview-img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
        .metadata { color: var(--text-muted); font-size: 0.95rem; margin-bottom: 1.5rem; display: flex; gap: 10px; align-items: center; }
        .badge { background: #27272a; padding: 4px 10px; border-radius: 6px; font-size: 0.8rem; font-weight: 600; color: var(--primary); }

        /* Skeleton Loading */
        .skeleton-wrapper { display: none; grid-template-columns: 320px 1fr; gap: 2rem; }
        .skeleton { background: linear-gradient(90deg, #27272a 25%, #3f3f46 50%, #27272a 75%); background-size: 400% 100%; animation: shimmer 1.5s infinite; border-radius: 12px; }
        @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        
        /* Spinner */
        .spinner { width: 20px; height: 20px; border: 3px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: white; animation: spin 1s linear infinite; display: none; }
        @keyframes spin { 100% { transform: rotate(360deg); } }

        .toast { position: fixed; bottom: 30px; right: 30px; background: #18181b; color: white; padding: 1rem 1.5rem; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 1px solid var(--border); opacity: 0; transform: translateY(20px); transition: all 0.3s cubic-bezier(0.68, -0.55, 0.265, 1.55); pointer-events: none; z-index: 9999;}
        .toast.show { opacity: 1; transform: translateY(0); pointer-events: all; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Universal Downloader</h1>
            <div style="background: rgba(16, 185, 129, 0.1); color: var(--success); padding: 6px 14px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; border: 1px solid rgba(16,185,129,0.2);">System Online</div>
        </header>

        <div class="card">
            <div class="input-group">
                <input type="text" id="url-input" placeholder="Paste Video, Audio, or Image URL here...">
                <button class="btn-primary" onclick="analyzeUrl()" id="analyze-btn">
                    <span id="analyze-txt">Analyze</span>
                    <div class="spinner" id="analyze-spinner"></div>
                </button>
            </div>
            <p style="font-size: 0.85rem; color: var(--text-muted); margin-left: 5px;">Supports YouTube, Instagram, X, Pinterest, Direct Images & more.</p>
        </div>

        <!-- Loading Skeleton -->
        <div class="card skeleton-wrapper" id="skeleton-loader">
            <div>
                <div class="skeleton" style="height: 180px; margin-bottom: 15px;"></div>
            </div>
            <div>
                <div class="skeleton" style="height: 28px; width: 80%; margin-bottom: 15px;"></div>
                <div class="skeleton" style="height: 20px; width: 40%; margin-bottom: 25px;"></div>
                <div class="skeleton" style="height: 50px; width: 100%; margin-bottom: 15px;"></div>
                <div class="skeleton" style="height: 50px; width: 100%;"></div>
            </div>
        </div>

        <!-- Actual Preview -->
        <div class="card" id="media-preview">
            <div class="preview-grid">
                <div>
                    <div class="img-wrapper">
                        <img id="thumb" class="preview-img" src="" alt="Thumbnail">
                    </div>
                </div>
                <div>
                    <h2 id="media-title" style="font-size: 1.4rem; margin-bottom: 8px;">Title</h2>
                    <div class="metadata">
                        <span class="badge" id="media-source">Platform</span>
                        <span id="media-duration">Duration</span>
                    </div>
                    
                    <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                        <select id="output-ext" onchange="updateQualityDropdown()" style="margin-bottom: 0; flex: 1;">
                            <!-- Populated dynamically -->
                        </select>
                        <select id="format-select" style="margin-bottom: 0; flex: 2;"></select>
                    </div>

                    <button class="btn-primary" style="width: 100%; padding: 1.2rem;" onclick="startDownload()">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin-right: 8px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                        Download Direct
                    </button>
                    <p style="font-size: 0.8rem; color: var(--text-muted); text-align:center; margin-top: 10px;">Download starts automatically in your browser.</p>
                </div>
            </div>
        </div>
    </div>
    
    <div class="toast" id="toast">Message</div>

    <script>
        let currentFormats = [];
        window.currentThumbnails = [];

        function showToast(msg, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.style.borderLeft = isError ? '4px solid var(--danger)' : '4px solid var(--primary)';
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 3500);
        }

        // Dynamic Format Filtering logic
        function updateQualityDropdown() {
            const formatExt = document.getElementById('output-ext').value;
            const select = document.getElementById('format-select');
            select.innerHTML = '';
            
            // Thumbnail / Image Dropdown options
            if(formatExt === 'thumbnail' || formatExt === 'jpg') {
                window.currentThumbnails.forEach((t, idx) => {
                    const opt = document.createElement('option');
                    opt.value = 'thumb_' + idx;
                    opt.textContent = `[Image] ${t.label} ${t.res}`;
                    select.appendChild(opt);
                });
                return;
            }

            const targetMode = (formatExt === 'mp3' || formatExt === 'm4a') ? 'audio' : 'video';
            let filteredFormats = currentFormats.filter(f => f.mode === targetMode);
            
            if (targetMode === 'video') {
                const exactMatches = filteredFormats.filter(f => f.ext === formatExt);
                if(exactMatches.length > 0) filteredFormats = exactMatches;
            }

            if(filteredFormats.length === 0) {
                const opt = document.createElement('option');
                opt.textContent = `No ${targetMode.toUpperCase()} options available`;
                opt.disabled = true;
                select.appendChild(opt);
                return;
            }

            filteredFormats.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.format_id;
                const modeTxt = f.mode.charAt(0).toUpperCase() + f.mode.slice(1);
                opt.textContent = `[${modeTxt}] ${f.quality} - ${f.size_str} (${f.ext})`;
                select.appendChild(opt);
            });
        }

        async function analyzeUrl() {
            const url = document.getElementById('url-input').value;
            if(!url) return showToast('Please enter a valid URL', true);

            document.getElementById('analyze-txt').style.display = 'none';
            document.getElementById('analyze-spinner').style.display = 'block';
            document.getElementById('media-preview').style.display = 'none';
            document.getElementById('skeleton-loader').style.display = 'grid';

            try {
                const res = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url})
                });
                const data = await res.json();
                if(!res.ok) throw new Error(data.detail || 'Analysis failed');

                document.getElementById('media-title').textContent = data.title;
                document.getElementById('media-source').textContent = data.source;
                document.getElementById('media-duration').textContent = data.duration ? `${data.duration} sec` : 'Image / Unknown';
                document.getElementById('thumb').src = data.thumbnail || 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
                
                // Store globally for dropdown selection
                window.currentThumbnails = data.thumbnails_list;
                currentFormats = data.formats;
                
                if(data.formats.length === 0 || data.formats[0].mode === 'image') {
                    document.getElementById('output-ext').innerHTML = '<option value="thumbnail">Format: Image</option>';
                } else {
                    document.getElementById('output-ext').innerHTML = `
                        <option value="mp4">Format: MP4 (Video)</option>
                        <option value="webm">Format: WebM (Video)</option>
                        <option value="mp3">Format: MP3 (Audio)</option>
                        <option value="m4a">Format: M4A (Audio)</option>
                        <option value="thumbnail">Format: Image (Thumbnail)</option>`;
                }

                // Trigger dropdown filter immediately
                updateQualityDropdown();

                document.getElementById('skeleton-loader').style.display = 'none';
                document.getElementById('media-preview').style.display = 'block';
            } catch (e) {
                document.getElementById('skeleton-loader').style.display = 'none';
                showToast(e.message, true);
            } finally {
                document.getElementById('analyze-txt').style.display = 'block';
                document.getElementById('analyze-spinner').style.display = 'none';
            }
        }

        async function startDownload() {
            const url = document.getElementById('url-input').value;
            const formatSelect = document.getElementById('format-select');
            const formatId = formatSelect.value;
            const outputFmt = document.getElementById('output-ext').value;
            
            if(!formatId || formatSelect.options[formatSelect.selectedIndex].disabled) {
                return showToast('Please select a valid quality option', true);
            }

            showToast('Download started. Please wait...');

            // If downloading an Image / Thumbnail
            if (outputFmt === 'thumbnail' || outputFmt === 'jpg') {
                const idx = formatId.split('_')[1];
                const targetUrl = window.currentThumbnails[idx].url;
                window.location.href = `/api/download-image?url=${encodeURIComponent(targetUrl)}`;
                return;
            }

            // Video or Audio Download Stream
            const selectedFormatInfo = currentFormats.find(f => f.format_id === formatId);
            const mode = (outputFmt === 'mp3' || outputFmt === 'm4a') ? 'audio' : 'video';
            
            const queryParams = new URLSearchParams({
                url: url,
                format_id: formatId,
                mode: mode,
                output_format: outputFmt
            });

            // Redirect browser directly to the stream endpoint to force download
            window.location.href = `/api/stream?${queryParams.toString()}`;
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return DASHBOARD_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
