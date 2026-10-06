import os
import re
import uuid
import threading
import time
import urllib.parse
import requests
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import yt_dlp

try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

app = FastAPI(title="SnapSave Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = "/tmp/downloads" if os.path.exists("/tmp") else os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

COOKIE_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
env_cookies = os.environ.get("YOUTUBE_COOKIES")
if env_cookies and (not os.path.exists(COOKIE_FILE) or os.path.getsize(COOKIE_FILE) == 0):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(env_cookies.strip())
    except Exception:
        pass

def get_base_ydl_opts(custom_opts=None):
    import shutil
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android']
            }
        }
    }
    node_bin = shutil.which("node")
    deno_bin = shutil.which("deno")
    js_runtimes = {}
    if node_bin:
        js_runtimes['node'] = {'path': node_bin}
    if deno_bin:
        js_runtimes['deno'] = {'path': deno_bin}
    if js_runtimes:
        opts['js_runtimes'] = js_runtimes
        opts['remote_components'] = ['ejs:github']

    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 10:
        opts['cookiefile'] = COOKIE_FILE

    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("YOUTUBE_PROXY")
    if proxy:
        opts['proxy'] = proxy

    if custom_opts:
        opts.update(custom_opts)
    return opts

# In-memory dictionary for real-time download tasks
download_tasks = {}

def cleanup_file(filepath: str):
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass

def background_cleaner_loop():
    while True:
        try:
            time.sleep(300)
            now = time.time()
            if os.path.exists(TEMP_DIR):
                for f in os.listdir(TEMP_DIR):
                    fpath = os.path.join(TEMP_DIR, f)
                    try:
                        if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > 900):
                            os.remove(fpath)
                    except Exception:
                        pass
            for tid in list(download_tasks.keys()):
                tdata = download_tasks.get(tid, {})
                if tdata.get('started_at') and (now - tdata['started_at'] > 1800):
                    download_tasks.pop(tid, None)
        except Exception:
            pass

threading.Thread(target=background_cleaner_loop, daemon=True).start()

def clean_media_url(url: str) -> str:
    # 1. YouTube Mix / Playlist / Radio / Timestamp stripping
    yt_m = re.search(r'(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|shorts\/|embed\/))([a-zA-Z0-9_-]{11})', url)
    if yt_m:
        return f"https://www.youtube.com/watch?v={yt_m.group(1)}"
    
    # 2. Instagram tracking parameters stripping (?igsh=..., ?utm_source=...)
    ig_m = re.search(r'(https?:\/\/(?:www\.)?instagram\.com\/(?:reel|p|tv)\/[a-zA-Z0-9_-]+)', url)
    if ig_m:
        return f"{ig_m.group(1)}/"

    # 3. Facebook tracking stripping
    fb_m = re.search(r'(https?:\/\/(?:www\.|m\.|web\.)?facebook\.com\/(?:reel|watch|.*\/videos)\/\d+)', url)
    if fb_m:
        return fb_m.group(1)

    # 4. TikTok clean URL
    tk_m = re.search(r'(https?:\/\/(?:vm|vt|www)\.tiktok\.com\/(?:@[a-zA-Z0-9_.-]+\/video\/\d+|[a-zA-Z0-9]+))', url)
    if tk_m:
        return tk_m.group(1)

    # 5. Twitter / X clean URL
    tw_m = re.search(r'(https?:\/\/(?:twitter\.com|x\.com)\/[a-zA-Z0-9_]+\/status\/\d+)', url)
    if tw_m:
        return tw_m.group(1)

    return url

class ExtractRequest(BaseModel):
    url: str
    downloadMode: str = "auto"
    videoQuality: str = "1080"

class StartDownloadRequest(BaseModel):
    url: str
    mode: str = "auto"
    quality: str = "1080"
    filename: str = None

def sanitize_filename(name: str, ext: str = "mp4") -> str:
    clean = re.sub(r'[^a-zA-Z0-9_\-\. ]', '', name)
    clean = clean.strip()[:100]
    if not clean:
        clean = "download"
    return f"{clean}.{ext}"

@app.get("/")
def health_check():
    import shutil
    has_cookies = os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 10
    has_node = bool(shutil.which("node"))
    has_deno = bool(shutil.which("deno"))
    return {
        "status": "ok",
        "app": "SnapSave Downloader Engine",
        "version": "1.6.0",
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg": bool(FFMPEG_PATH),
        "cookies_loaded": has_cookies,
        "js_runtime": "node" if has_node else ("deno" if has_deno else "none")
    }

@app.get("/api/test-clients")
def test_clients(url: str = "https://www.youtube.com/watch?v=GjfxDRRLXAQ"):
    results = {}
    clients = [None, ['android'], ['tv'], ['tv_embedded'], ['ios'], ['mweb'], ['web']]
    for c in clients:
        c_name = str(c)
        try:
            ydl_opts = {'quiet': True, 'no_warnings': True}
            if c:
                ydl_opts['extractor_args'] = {'youtube': {'player_client': c}}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results[c_name] = f"SUCCESS: {info.get('title')[:30]} ({len(info.get('formats', []))} formats)"
        except Exception as e:
            results[c_name] = f"FAILED: {str(e)[:100]}"
    return results

@app.post("/api/extract")
def extract_media(req: ExtractRequest, request: Request):
    raw_url = req.url.strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL is required")

    url = clean_media_url(raw_url)
    base_url = str(request.base_url).rstrip('/')

    # 1. Fast-path for TikTok
    if "tiktok.com" in url.lower():
        try:
            tik_res = requests.get(
                f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=10
            )
            if tik_res.ok:
                tdata = tik_res.json()
                if tdata.get("code") == 0 and "data" in tdata:
                    d = tdata["data"]
                    vurl = d.get("play") or d.get("hdplay")
                    aurl = d.get("music") or vurl
                    title = d.get("title") or "TikTok Media"
                    clean_name = sanitize_filename(title, "mp3" if req.downloadMode == "audio" else "mp4")
                    return {
                        "status": "tunnel",
                        "url": aurl if req.downloadMode == "audio" else vurl,
                        "videoUrl": vurl,
                        "audioUrl": aurl,
                        "title": title,
                        "author": d.get("author", {}).get("nickname", "TikTok"),
                        "thumbnail": d.get("cover", ""),
                        "duration": f"{d.get('duration', 0) // 60}:{d.get('duration', 0) % 60:02d}",
                        "filename": clean_name,
                    }
        except Exception:
            pass

    # 2. Fast-path for YouTube using official oEmbed (bypasses all bot challenges & timeouts)
    is_youtube = ("youtube.com" in url.lower()) or ("youtu.be" in url.lower())
    if is_youtube:
        yt_m = re.search(r'(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|shorts\/|embed\/))([a-zA-Z0-9_-]{11})', url)
        yt_id = yt_m.group(1) if yt_m else None
        
        title = "YouTube Video"
        author = "YouTube"
        thumbnail = f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg" if yt_id else ""
        
        if yt_id:
            try:
                oe = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={yt_id}&format=json", timeout=5).json()
                title = oe.get("title", title)
                author = oe.get("author_name", author)
                thumbnail = oe.get("thumbnail_url", thumbnail)
            except Exception:
                pass

        clean_mp4 = sanitize_filename(title, "mp4")
        clean_mp3 = sanitize_filename(title, "mp3")
        encoded_url = urllib.parse.quote(url)

        dl_video = f"{base_url}/api/download?url={encoded_url}&mode=auto&quality={req.videoQuality}&filename={urllib.parse.quote(clean_mp4)}"
        dl_audio = f"{base_url}/api/download?url={encoded_url}&mode=audio&filename={urllib.parse.quote(clean_mp3)}"

        return {
            "status": "tunnel",
            "url": dl_audio if req.downloadMode == "audio" else dl_video,
            "videoUrl": dl_video,
            "audioUrl": dl_audio,
            "title": title,
            "author": author,
            "thumbnail": thumbnail,
            "duration": "",
            "filename": clean_mp3 if req.downloadMode == "audio" else clean_mp4,
            "ytVideoId": yt_id,
        }

    # 3. For Instagram, Facebook, Twitter, etc., extract with yt-dlp
    try:
        ydl_opts = get_base_ydl_opts({
            'extract_flat': False,
            'socket_timeout': 15,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not info:
        raise HTTPException(status_code=404, detail="Could not extract media info")

    title = info.get('title', 'Media')
    thumb = info.get('thumbnail', '')
    duration_sec = info.get('duration', 0)
    duration_str = f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec else ""
    author = info.get('uploader', info.get('channel', ''))

    clean_mp4 = sanitize_filename(title, "mp4")
    clean_mp3 = sanitize_filename(title, "mp3")
    encoded_url = urllib.parse.quote(url)

    dl_video_url = f"{base_url}/api/download?url={encoded_url}&mode=auto&quality={req.videoQuality}&filename={urllib.parse.quote(clean_mp4)}"
    dl_audio_url = f"{base_url}/api/download?url={encoded_url}&mode=audio&filename={urllib.parse.quote(clean_mp3)}"

    return {
        "status": "tunnel",
        "url": dl_audio_url if req.downloadMode == "audio" else dl_video_url,
        "videoUrl": dl_video_url,
        "audioUrl": dl_audio_url,
        "title": title,
        "author": author,
        "thumbnail": thumb,
        "duration": duration_str,
        "filename": clean_mp3 if req.downloadMode == "audio" else clean_mp4,
        "ytVideoId": None,
    }

def format_bytes(b):
    if not b: return ""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} TB"

def run_download_task(task_id: str, clean_url: str, mode: str, quality: str, requested_filename: str):
    if task_id not in download_tasks:
        download_tasks[task_id] = {}
    is_audio = (mode == "audio")
    file_id = task_id[:8]
    ext = "mp3" if is_audio else "mp4"
    out_template = os.path.join(TEMP_DIR, f"{file_id}.%(ext)s")
    is_youtube = ("youtube.com" in clean_url.lower()) or ("youtu.be" in clean_url.lower())

    def hook(d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            percent = round((downloaded / total) * 100, 1) if total > 0 else 40.0
            d_str = format_bytes(downloaded)
            t_str = format_bytes(total)
            msg = f"Step 1/2: Processing & Merging ({percent}%)..." if total > 0 else f"Step 1/2: Processing stream: {d_str}"
            download_tasks[task_id].update({
                'status': 'downloading',
                'percent': percent,
                'speed': d.get('_speed_str', '').strip(),
                'eta': d.get('_eta_str', '').strip(),
                'downloaded': downloaded,
                'total': total,
                'message': msg
            })
        elif d.get('status') == 'finished':
            download_tasks[task_id].update({
                'status': 'processing',
                'percent': 95.0,
                'speed': '',
                'eta': '',
                'message': 'Step 1/2: Packaging final HD file with FFmpeg...'
            })

    try:
        ydl_opts = get_base_ydl_opts({
            'outtmpl': out_template,
            'progress_hooks': [hook],
            'concurrent_fragment_downloads': 16,
            'http_chunk_size': 10485760,
            'buffersize': 1048576,
        })

        if FFMPEG_PATH:
            ydl_opts['ffmpeg_location'] = FFMPEG_PATH

        if is_audio:
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['format_sort'] = ['abr', 'quality', 'size']
            if FFMPEG_PATH:
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',
                }]
        else:
            ydl_opts['merge_output_format'] = 'mp4'
            # Format sorting: Prioritize highest resolution, highest fps, crisp H.264 (AVC) or VP9 codec, and highest bitrate!
            ydl_opts['format_sort'] = ['res', 'fps', 'codec:h264:vp9', 'size', 'br']
            if quality == '360':
                ydl_opts['format'] = 'bestvideo[height<=360]+bestaudio/best[height<=360]/best'
            elif quality == '480':
                ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
            elif quality == '720':
                ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
            elif quality == '1080':
                ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
            else:
                ydl_opts['format'] = 'bestvideo+bestaudio/best'

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clean_url])

        # Find produced file
        final_file = None
        for f in os.listdir(TEMP_DIR):
            if f.startswith(file_id) and os.path.getsize(os.path.join(TEMP_DIR, f)) > 0:
                final_file = os.path.join(TEMP_DIR, f)
                break

        if not final_file or not os.path.exists(final_file):
            raise Exception("File was not created by download engine")

        actual_ext = final_file.rsplit('.', 1)[-1]
        dl_name = requested_filename or f"media_{file_id}.{actual_ext}"
        if not dl_name.endswith(f".{actual_ext}"):
            dl_name += f".{actual_ext}"

        download_tasks[task_id].update({
            'status': 'ready',
            'percent': 100.0,
            'filepath': final_file,
            'filename': dl_name,
            'size': os.path.getsize(final_file),
            'ext': actual_ext,
            'message': 'Step 2/2: Ready! Saving to your device...'
        })
    except Exception as e:
        err_msg = str(e)
        if "Failed to extract any player response" in err_msg or "unavailable" in err_msg.lower():
            friendly_err = "यह वीडियो YouTube पर मौजूद नहीं है, हटा दी गई है या प्राइवेट है। कृपया किसी चालू वीडियो का लिंक डालें。"
        else:
            friendly_err = f"Download error: {err_msg[:120]}"
        download_tasks[task_id].update({
            'status': 'error',
            'error': friendly_err,
            'message': friendly_err
        })

@app.post("/api/start-download")
def start_download(req: StartDownloadRequest):
    raw_url = req.url.strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL is required")

    clean_url = clean_media_url(raw_url)
    task_id = str(uuid.uuid4())

    download_tasks[task_id] = {
        'status': 'queued',
        'percent': 0.0,
        'speed': '',
        'eta': '',
        'message': 'Starting download...',
        'started_at': time.time()
    }

    t = threading.Thread(
        target=run_download_task,
        args=(task_id, clean_url, req.mode, req.quality, req.filename),
        daemon=True
    )
    t.start()

    return {"taskId": task_id, "status": "queued"}

@app.get("/api/progress")
def get_progress(id: str = Query(None, description="Task ID"), taskId: str = Query(None, description="Task ID")):
    task_id = id or taskId
    if not task_id or task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = download_tasks[task_id]
    return {
        "status": task.get("status"),
        "percent": task.get("percent", 0.0),
        "speed": task.get("speed", ""),
        "eta": task.get("eta", ""),
        "message": task.get("message", ""),
        "filename": task.get("filename", ""),
        "size": task.get("size", 0),
        "error": task.get("error", "")
    }

@app.get("/api/file")
def get_downloaded_file(
    background_tasks: BackgroundTasks,
    id: str = Query(None, description="Task ID"),
    taskId: str = Query(None, description="Task ID")
):
    task_id = id or taskId
    if not task_id or task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = download_tasks[task_id]
    if task.get("status") != "ready":
        raise HTTPException(status_code=400, detail="File is not ready yet")

    filepath = task.get("filepath")
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File has expired")

    filename = task.get("filename", "download.mp4")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "video/mp4"

    file_size = os.path.getsize(filepath)

    def file_streamer():
        with open(filepath, "rb") as f:
            while chunk := f.read(1024 * 1024):  # 1MB buffer for fast streaming
                yield chunk

    # Schedule cleanup after download completes
    background_tasks.add_task(cleanup_file, filepath)

    safe_name = urllib.parse.quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{safe_name}",
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }

    return StreamingResponse(
        file_streamer(),
        media_type=media_type,
        headers=headers
    )

@app.get("/api/download")
def download_media(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="Target media URL"),
    mode: str = Query("auto", description="auto or audio"),
    quality: str = Query("1080", description="Video quality"),
    filename: str = Query(None, description="Download filename")
):
    raw_url = urllib.parse.unquote(url)
    clean_url = clean_media_url(raw_url)
    is_audio = (mode == "audio")
    file_id = str(uuid.uuid4())[:8]
    ext = "mp3" if is_audio else "mp4"
    out_template = os.path.join(TEMP_DIR, f"{file_id}.%(ext)s")

    is_youtube = ("youtube.com" in clean_url.lower()) or ("youtu.be" in clean_url.lower())

    client_configs = [
        ['ios', 'android'],
        ['android'],
        ['ios'],
        None
    ]

    last_error = None
    downloaded_file = None

    for client_list in client_configs:
        try:
            ydl_opts = get_base_ydl_opts({
                'outtmpl': out_template,
                'concurrent_fragment_downloads': 16,
                'http_chunk_size': 10485760,
                'buffersize': 1048576,
            })
            if is_youtube and client_list:
                ydl_opts['extractor_args'] = {'youtube': {'player_client': client_list}}

            if FFMPEG_PATH:
                ydl_opts['ffmpeg_location'] = FFMPEG_PATH


            if is_audio:
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['format_sort'] = ['abr', 'quality', 'size']
                if FFMPEG_PATH:
                    ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '320',
                    }]
            else:
                ydl_opts['merge_output_format'] = 'mp4'
                ydl_opts['format_sort'] = ['res', 'fps', 'codec:h264:vp9', 'size', 'br']
                if quality == '360':
                    ydl_opts['format'] = 'bestvideo[height<=360]+bestaudio/best[height<=360]/best'
                elif quality == '480':
                    ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
                elif quality == '720':
                    ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
                elif quality == '1080':
                    ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
                else:
                    ydl_opts['format'] = 'bestvideo+bestaudio/best'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])

            # Check if file was created
            for f in os.listdir(TEMP_DIR):
                if f.startswith(file_id) and os.path.getsize(os.path.join(TEMP_DIR, f)) > 0:
                    downloaded_file = os.path.join(TEMP_DIR, f)
                    break

            if downloaded_file:
                break
        except Exception as e:
            last_error = e
            continue

    if not downloaded_file or not os.path.exists(downloaded_file) or os.path.getsize(downloaded_file) == 0:
        err_msg = str(last_error) if last_error else 'No file produced'
        if "Failed to extract any player response" in err_msg or "unavailable" in err_msg.lower():
            friendly_err = "यह वीडियो YouTube पर मौजूद नहीं है, हटा दी गई है या प्राइवेट है। कृपया चालू वीडियो का लिंक चेक करें।"
        else:
            friendly_err = f"Download engine error: {err_msg[:120]}"
        raise HTTPException(
            status_code=400,
            detail=friendly_err
        )

    # Determine real extension
    actual_ext = downloaded_file.rsplit('.', 1)[-1]
    media_type = "audio/mpeg" if actual_ext == "mp3" else "video/mp4"

    dl_name = filename or f"media_{file_id}.{actual_ext}"
    if not dl_name.endswith(f".{actual_ext}"):
        dl_name += f".{actual_ext}"

    # Schedule cleanup after download completes
    background_tasks.add_task(cleanup_file, downloaded_file)

    return FileResponse(
        downloaded_file,
        media_type=media_type,
        filename=dl_name
    )
