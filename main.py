import os
import re
import urllib.parse
import subprocess
import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="SnapSave Downloader API")

# Enable CORS for all domains
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        "version": "1.0.0",
        "yt_dlp_version": yt_dlp.version.__version__
    }

@app.post("/api/extract")
def extract_media(req: ExtractRequest, request: Request):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    base_url = str(request.base_url).rstrip('/')

    # 1. Fast-path for TikTok to avoid IP rate-limits
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

    # 2. Multi-client YouTube pool (tries android first, then ios, then mweb, then tv_embedded)
    is_youtube = ("youtube.com" in url.lower()) or ("youtu.be" in url.lower())
    info = None
    last_err = None

    if is_youtube:
        clients_to_try = [
            ['android'],
            ['ios'],
            ['mweb'],
            ['android_creator'],
            ['tv_embedded']
        ]

        for client in clients_to_try:
            try:
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': False,
                    'extractor_args': {
                        'youtube': {
                            'player_client': client,
                        }
                    }
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(url, download=False)
                    if extracted and extracted.get('title'):
                        info = extracted
                        break
            except Exception as e:
                last_err = e
                continue
    else:
        # All other platforms: Instagram, Facebook, Twitter/X, Reddit, Vimeo, SoundCloud
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = e

    if not info:
        # If all yt-dlp clients fail for YouTube, fallback to official oEmbed so preview & info always load!
        if is_youtube:
            m = re.search(r'(?:youtu\.be\/|youtube\.com\/(?:watch\?v=|shorts\/|embed\/))([a-zA-Z0-9_-]{11})', url)
            yt_id = m.group(1) if m else None
            if yt_id:
                try:
                    oembed = requests.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={yt_id}&format=json", timeout=6).json()
                    title = oembed.get('title', 'YouTube Video')
                    author = oembed.get('author_name', 'YouTube')
                    thumb = oembed.get('thumbnail_url', f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg")
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
                        "duration": "",
                        "filename": clean_mp3 if req.downloadMode == "audio" else clean_mp4,
                        "ytVideoId": yt_id,
                    }
                except Exception:
                    pass

        raise HTTPException(status_code=500, detail=str(last_err or "Media extraction failed"))

    title = info.get('title', 'Media')
    thumb = info.get('thumbnail', '')
    duration_sec = info.get('duration', 0)
    duration_str = f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec else ""
    author = info.get('uploader', info.get('channel', ''))
    
    # YouTube ID if present
    yt_id = info.get('id') if 'youtube' in (info.get('extractor', '')).lower() else None

    # Generate direct download endpoints on this API server
    encoded_url = urllib.parse.quote(url)
    clean_mp4 = sanitize_filename(title, "mp4")
    clean_mp3 = sanitize_filename(title, "mp3")

    dl_video_url = f"{base_url}/api/download?url={encoded_url}&mode=auto&quality={req.videoQuality}&filename={urllib.parse.quote(clean_mp4)}"
    dl_audio_url = f"{base_url}/api/download?url={encoded_url}&mode=audio&filename={urllib.parse.quote(clean_mp3)}"

    # Progressive preview URL if available
    preview_url = ""
    formats = info.get('formats', [])
    for f in reversed(formats):
        if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url'):
            preview_url = f['url']
            break

    return {
        "status": "tunnel",
        "url": dl_audio_url if req.downloadMode == "audio" else dl_video_url,
        "videoUrl": preview_url or dl_video_url,
        "audioUrl": dl_audio_url,
        "title": title,
        "author": author,
        "thumbnail": thumb,
        "duration": duration_str,
        "filename": clean_mp3 if req.downloadMode == "audio" else clean_mp4,
        "ytVideoId": yt_id,
    }

@app.get("/api/download")
def download_stream(
    url: str = Query(..., description="Target media URL"),
    mode: str = Query("auto", description="auto or audio"),
    quality: str = Query("1080", description="Video quality"),
    filename: str = Query(None, description="Download filename")
):
    clean_url = urllib.parse.unquote(url)
    is_audio = (mode == "audio")
    dl_filename = filename or ("audio.mp3" if is_audio else "video.mp4")

    # Command line args for yt-dlp to stream directly to stdout using android/ios clients
    if is_audio:
        cmd = [
            "yt-dlp",
            "-q", "--no-warnings",
            "--extractor-args", "youtube:player_client=android,ios,mweb",
            "-x", "--audio-format", "mp3",
            "-o", "-",
            clean_url
        ]
        media_type = "audio/mpeg"
    else:
        cmd = [
            "yt-dlp",
            "-q", "--no-warnings",
            "--extractor-args", "youtube:player_client=android,ios,mweb",
            "-f", "best[ext=mp4]/bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", "-",
            clean_url
        ]
        media_type = "video/mp4"

    def iter_stream():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(65536) # 64KB chunks
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()

    headers = {
        "Content-Disposition": f'attachment; filename="{dl_filename}"',
        "Content-Type": media_type,
        "Cache-Control": "no-cache",
    }

    return StreamingResponse(iter_stream(), headers=headers, media_type=media_type)
