import os, subprocess, tempfile, shutil, re
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format: str = "video"   # "video" | "audio"
    quality: str = "1080p"

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Please enter a valid URL.")

    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "This platform is not yet supported. Try YouTube, Facebook, or Instagram.")

    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HTTPException(422, "Could not fetch media info. The video may be private or restricted.")

        import json
        info = json.loads(result.stdout)

        video_streams = []
        audio_streams = []

        for f in info.get("formats", []):
            if f.get("vcodec") != "none" and f.get("acodec") != "none":
                video_streams.append({
                    "url": f.get("url", ""),
                    "quality": f.get("format_note") or f"{f.get('height', '?')}p",
                    "mimeType": f"video/{f.get('ext', 'mp4')}",
                })
            elif f.get("vcodec") == "none" and f.get("acodec") != "none":
                bitrate = f.get("abr") or f.get("tbr") or 128
                audio_streams.append({
                    "url": f.get("url", ""),
                    "quality": f"{int(bitrate)} kbps",
                    "mimeType": f"audio/{f.get('ext', 'm4a')}",
                })

        return {
            "phase": "ready",
            "videoInfo": {
                "title": info.get("title", "Media"),
                "thumbnail": info.get("thumbnail", ""),
                "author": info.get("uploader") or info.get("channel") or platform.capitalize(),
                "duration": str(int(info.get("duration", 0))) + "s" if info.get("duration") else "--:--",
                "videoStreams": video_streams[:6],
                "audioStreams": audio_streams[:3],
            },
            "downloadUrl": None,
            "progress": 0,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Request timed out. Please try again.")
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse media info.")

@app.post("/download")
def download(req: DownloadRequest):
    url = req.url.strip()
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "Unsupported platform.")

    tmpdir = tempfile.mkdtemp()
    try:
        ext = "mp3" if req.format == "audio" else "mp4"
        out_template = os.path.join(tmpdir, f"media.%(ext)s")

        quality_map = {
            "4K (2160p)": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]",
        }

        if req.format == "audio":
            cmd = [
                "yt-dlp", "-x", "--audio-format", "mp3",
                "--audio-quality", "0",
                "-o", out_template, url,
            ]
        else:
            fmt = quality_map.get(req.quality, quality_map["1080p"])
            cmd = [
                "yt-dlp", "-f", fmt,
                "--merge-output-format", "mp4",
                "-o", out_template, url,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise HTTPException(422, "Download failed. The video may be private or restricted.")

        files = os.listdir(tmpdir)
        if not files:
            raise HTTPException(500, "No file was produced.")

        filepath = os.path.join(tmpdir, files[0])
        filename = re.sub(r'[<>:"/\\|?*]', '', os.path.splitext(files[0])[0])[:80] + f".{ext}"

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

    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(504, "Download timed out. Try a lower quality.")
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(500, str(e))