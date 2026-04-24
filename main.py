import os, re, json, tempfile, shutil
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

PLATFORM_PATTERNS = {
    "youtube.com": "youtube", "youtu.be": "youtube",
    "facebook.com": "facebook", "fb.watch": "facebook", "fb.com": "facebook",
    "instagram.com": "instagram", "instagr.am": "instagram",
}

def detect_platform(url: str):
    lower = url.lower()
    for domain, platform in PLATFORM_PATTERNS.items():
        if domain in lower:
            return platform
    return None

YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["ios", "web"],
        }
    },
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    },
}

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format: str = "video"
    quality: str = "1080p"

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

    opts = {
        **YDL_BASE_OPTS,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise HTTPException(422, "Could not fetch media info. The video may be private or restricted.")

        video_streams = []
        audio_streams = []

        for f in info.get("formats", []):
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height  = f.get("height")
            ext     = f.get("ext", "mp4")
            furl    = f.get("url", "")

            if not furl:
                continue

            if vcodec != "none" and acodec != "none" and height:
                video_streams.append({
                    "url": furl,
                    "quality": f.get("format_note") or f"{height}p",
                    "mimeType": f"video/{ext}",
                })
            elif vcodec == "none" and acodec != "none":
                bitrate = f.get("abr") or f.get("tbr") or 128
                audio_streams.append({
                    "url": furl,
                    "quality": f"{int(bitrate)} kbps",
                    "mimeType": f"audio/{ext}",
                })

        duration_secs = info.get("duration")
        if duration_secs:
            mins = int(duration_secs) // 60
            secs = int(duration_secs) % 60
            duration_str = f"{mins}:{secs:02d}"
        else:
            duration_str = "--:--"

        return {
            "phase": "ready",
            "videoInfo": {
                "title":        info.get("title", "Media"),
                "thumbnail":    info.get("thumbnail", ""),
                "author":       info.get("uploader") or info.get("channel") or platform.capitalize(),
                "duration":     duration_str,
                "videoStreams": video_streams[:6],
                "audioStreams": audio_streams[:3],
            },
            "downloadUrl": None,
            "progress":    0,
            "error":       None,
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
            "720p":   720,
            "480p":   480,
            "360p":   360,
        }
        max_h = quality_height.get(req.quality, 1080)

        if req.format == "audio":
            opts = {
                **YDL_BASE_OPTS,
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
        else:
            opts = {
                **YDL_BASE_OPTS,
                "format": f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_h}]",
                "outtmpl": out_template,
                "merge_output_format": "mp4",
                "postprocessors": [{
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }],
            }

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = os.listdir(tmpdir)
        if not files:
            raise HTTPException(500, "No file was produced.")

        filepath  = os.path.join(tmpdir, files[0])
        safe_name = re.sub(r'[<>:"/\\|?*]', '', os.path.splitext(files[0])[0])[:80]
        filename  = f"{safe_name}.{ext}"

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
        raise HTTPException(422, f"Download failed: {str(e)[:200]}")
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("Unexpected error in /download:", str(e))
        raise HTTPException(500, f"Unexpected error: {str(e)}")