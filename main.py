import os, re, json, tempfile, shutil, urllib.parse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import urllib.request

app = FastAPI()

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

PLATFORM_PATTERNS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
}

def detect_platform(url: str):
    lower = url.lower()
    for domain, platform in PLATFORM_PATTERNS.items():
        if domain in lower:
            return platform
    return None

def extract_youtube_id(url: str):
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname == "youtu.be":
            return parsed.path.lstrip("/").split("/")[0]
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        path = parsed.path
        for segment in ["shorts", "live", "embed"]:
            if segment in path:
                parts = path.split(segment + "/")
                if len(parts) > 1:
                    return parts[1].split("/")[0]
    except Exception:
        pass
    return None

YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["ios"],
        }
    },
    "http_headers": {
        "User-Agent": "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)",
    },
}

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format: str = "video"
    quality: str = "720p"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug")
def debug():
    return {"yt-dlp-version": yt_dlp.version.__version__}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Please enter a valid URL.")

    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "This platform is not yet supported. Try YouTube, Facebook, or Instagram.")

    try:
        if platform == "youtube":
            video_id = extract_youtube_id(url)
            if not video_id:
                raise HTTPException(400, "Could not extract YouTube video ID.")

            # Use oEmbed for title/author (no API key needed)
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            try:
                with urllib.request.urlopen(oembed_url, timeout=10) as r:
                    oembed = json.loads(r.read().decode())
                title = oembed.get("title", "YouTube Video")
                author = oembed.get("author_name", "Unknown")
                thumbnail = oembed.get("thumbnail_url", f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
            except Exception:
                title = "YouTube Video"
                author = "Unknown"
                thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

            # Build stream options pointing back through our /download endpoint
            # so the frontend has quality choices without needing raw YT stream URLs
            base = os.getenv("RENDER_EXTERNAL_URL", "")
            video_streams = [
                {"url": f"{base}/download", "quality": "1080p", "mimeType": "video/mp4"},
                {"url": f"{base}/download", "quality": "720p",  "mimeType": "video/mp4"},
                {"url": f"{base}/download", "quality": "480p",  "mimeType": "video/mp4"},
                {"url": f"{base}/download", "quality": "360p",  "mimeType": "video/mp4"},
            ]
            audio_streams = [
                {"url": f"{base}/download", "quality": "192 kbps", "mimeType": "audio/mp3"},
            ]

            return {
                "phase": "ready",
                "videoInfo": {
                    "title": title,
                    "thumbnail": thumbnail,
                    "author": author,
                    "duration": "--:--",
                    "videoStreams": video_streams,
                    "audioStreams": audio_streams,
                },
                "downloadUrl": None,
                "progress": 0,
                "error": None,
            }

        else:
            # Facebook / Instagram — use yt-dlp (less rate limited than YouTube)
            opts = {
                **YDL_BASE_OPTS,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                raise HTTPException(422, "Could not fetch media info.")

            duration_secs = info.get("duration")
            duration_str = "--:--"
            if duration_secs:
                mins = int(duration_secs) // 60
                secs = int(duration_secs) % 60
                duration_str = f"{mins}:{secs:02d}"

            base = os.getenv("RENDER_EXTERNAL_URL", "")
            return {
                "phase": "ready",
                "videoInfo": {
                    "title": info.get("title", "Media"),
                    "thumbnail": info.get("thumbnail", ""),
                    "author": info.get("uploader") or info.get("channel") or platform.capitalize(),
                    "duration": duration_str,
                    "videoStreams": [{"url": f"{base}/download", "quality": "Best", "mimeType": "video/mp4"}],
                    "audioStreams": [{"url": f"{base}/download", "quality": "Best", "mimeType": "audio/mp3"}],
                },
                "downloadUrl": None,
                "progress": 0,
                "error": None,
            }

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        print("yt-dlp DownloadError:", str(e))
        raise HTTPException(422, f"Could not fetch media info: {str(e)[:200]}")
    except Exception as e:
        print("Unexpected error in /analyze:", str(e))
        raise HTTPException(500, f"Unexpected error: {str(e)}")


@app.post("/download")
def download(req: DownloadRequest):
    url = req.url.strip()
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "Unsupported platform.")

    tmpdir = tempfile.mkdtemp()

    try:
        ext = "mp3" if req.format == "audio" else "mp4"
        out_template = os.path.join(tmpdir, "media.%(ext)s")

        quality_height = {
            "4K (2160p)": 2160,
            "1080p": 1080,
            "720p": 720,
            "480p": 480,
            "360p": 360,
        }
        max_h = quality_height.get(req.quality, 720)

        if req.format == "audio":
            opts = {
                **YDL_BASE_OPTS,
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        else:
            opts = {
                **YDL_BASE_OPTS,
                "format": f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best",
                "outtmpl": out_template,
                "merge_output_format": "mp4",
            }

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = os.listdir(tmpdir)
        if not files:
            raise HTTPException(500, "No file was produced.")

        filepath = os.path.join(tmpdir, files[0])
        safe_name = re.sub(r'[<>:"/\\|?*]', '', os.path.splitext(files[0])[0])[:80]
        filename = f"{safe_name}.{ext}"

        def stream_file():
            try:
                with open(filepath, "rb") as f:
                    while chunk := f.read(1024 * 256):
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"
        return StreamingResponse(
            stream_file(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("yt-dlp DownloadError:", str(e))
        raise HTTPException(422, f"Download failed. The video may be restricted: {str(e)[:150]}")
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("Unexpected error in /download:", str(e))
        raise HTTPException(500, f"Unexpected error: {str(e)}")