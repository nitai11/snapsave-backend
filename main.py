import os
import re
import uuid
import urllib.parse
import requests
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="SnapSave Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = "/tmp/downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

def cleanup_file(filepath: str):
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass

class ExtractRequest(BaseModel):
    url: str
    downloadMode: str = "auto"
    videoQuality: str = "1080"

def sanitize_filename(name: str, ext: str = "mp4") -> str:
    clean = re.sub(r'[^a-zA-Z0-9_\-\. ]', '', name)
    clean = clean.strip()[:100]
    if not clean:
        clean = "download"
    return f"{clean}.{ext}"

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "app": "SnapSave Downloader Engine",
        "version": "1.1.0",
        "yt_dlp_version": yt_dlp.version.__version__
    }

@app.post("/api/extract")
def extract_media(req: ExtractRequest, request: Request):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    base_url = str(request.base_url).rstrip('/')

    # 1. Fast-path for TikTok
    if "tiktok.com" in url.lower():
        try:
            tik_res = requests.get(f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}", timeout=10)
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

    # 2. Extract with yt-dlp (android client for YouTube bypass)
    is_youtube = ("youtube.com" in url.lower()) or ("youtu.be" in url.lower())
    info = None

    if is_youtube:
        clients_to_try = [['android'], ['ios'], ['mweb'], ['tv_embedded']]
        for client in clients_to_try:
            try:
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': False,
                    'extractor_args': {'youtube': {'player_client': client}}
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(url, download=False)
                    if extracted and extracted.get('title'):
                        info = extracted
                        break
            except Exception:
                continue

    if not info and is_youtube:
        # Fallback to official YouTube oEmbed for guaranteed metadata
        m = re.search(r'(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|shorts\/|embed\/))([a-zA-Z0-9_-]{11})', url)
        yt_id = m.group(1) if m else None
        if yt_id:
            try:
                oembed = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={yt_id}&format=json", timeout=6).json()
                title = oembed.get('title', 'YouTube Video')
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
                    "author": oembed.get('author_name', 'YouTube'),
                    "thumbnail": oembed.get('thumbnail_url', f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg"),
                    "duration": "",
                    "filename": clean_mp3 if req.downloadMode == "audio" else clean_mp4,
                    "ytVideoId": yt_id,
                }
            except Exception:
                pass

    if not info:
        # Try generic extractor for Instagram, Facebook, Twitter, etc.
        try:
            ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False}
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
    yt_id = info.get('id') if is_youtube else None

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
        "ytVideoId": yt_id,
    }

@app.get("/api/download")
def download_media(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="Target media URL"),
    mode: str = Query("auto", description="auto or audio"),
    quality: str = Query("1080", description="Video quality"),
    filename: str = Query(None, description="Download filename")
):
    clean_url = urllib.parse.unquote(url)
    is_audio = (mode == "audio")
    file_id = str(uuid.uuid4())[:8]
    ext = "mp3" if is_audio else "mp4"
    out_template = os.path.join(TEMP_DIR, f"{file_id}.%(ext)s")

    is_youtube = ("youtube.com" in clean_url.lower()) or ("youtu.be" in clean_url.lower())

    if is_audio:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': out_template,
        }
        if is_youtube:
            ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'ios']}}
    else:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': '18/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': out_template,
        }
        if is_youtube:
            ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'ios']}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clean_url])

        # Find the downloaded file
        downloaded_file = None
        for f in os.listdir(TEMP_DIR):
            if f.startswith(file_id):
                downloaded_file = os.path.join(TEMP_DIR, f)
                break

        if not downloaded_file or not os.path.exists(downloaded_file) or os.path.getsize(downloaded_file) == 0:
            raise HTTPException(status_code=500, detail="Download engine could not generate file")

        media_type = "audio/mpeg" if is_audio else "video/mp4"
        dl_name = filename or os.path.basename(downloaded_file)
        if not dl_name.endswith(f".{ext}"):
            dl_name += f".{ext}"

        # Clean up the file after it has been sent to client
        background_tasks.add_task(cleanup_file, downloaded_file)

        return FileResponse(
            downloaded_file,
            media_type=media_type,
            filename=dl_name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
