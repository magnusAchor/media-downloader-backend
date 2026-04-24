import os, subprocess, tempfile, shutil, re, json
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
    format: str = "video"
    quality: str = "1080p"

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug")
def debug():
    ytdlp = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
    ffmpeg = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    return {
        "yt-dlp": ytdlp.stdout.strip() or ytdlp.stderr.strip(),
        "ffmpeg": ffmpeg.stdout[:80].strip() or ffmpeg.stderr[:80].strip(),
    }

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
            [
                "yt-dlp",
                "--dump-json",
                "--no-playlist",
                "--extractor-args", "youtube:player_client=web",
                url,
            ],
            capture_output=True, text=True, timeout=60
        )

        if result.returncode != 0:
            print("yt-dlp stderr:", result.stderr[:500])
            print("yt-dlp stdout:", result.stdout[:500])
            raise HTTPException(422, f"Could not fetch media info: {result.stderr[:200]}")

        info = json.loads(result.stdout)

        video_streams = []
        audio_streams = []

        for f in info.get("formats", []):
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            ext = f.get("ext", "mp4")
            url_f = f.get("url", "")

            if not url_f:
                continue

            if vcodec != "none" and acodec != "none" and height:
                video_streams.append({
                    "url": url_f,
                    "quality": f.get("format_note") or f"{height}p",
                    "mimeType": f"video/{ext}",
                })
            elif vcodec == "none" and acodec != "none":
                bitrate = f.get("abr") or f.get("tbr") or 128
                audio_streams.append({
                    "url": url_f,
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
                "title": info.get("title", "Media"),
                "thumbnail": info.get("thumbnail", ""),
                "author": info.get("uploader") or info.get("channel") or platform.capitalize(),
                "duration": duration_str,
                "videoStreams": video_streams[:6],
                "audioStreams": audio_streams[:3],
            },
            "downloadUrl": None,
            "progress": 0,
            "error": None,
        }

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Request timed out. Please try again.")
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse media info from yt-dlp.")
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

        quality_map = {
            "4K (2160p)": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
            "1080p":      "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
            "720p":       "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
            "480p":       "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
            "360p":       "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
        }

        if req.format == "audio":
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--extractor-args", "youtube:player_client=web",
                "-o", out_template,
                url,
            ]
        else:
            fmt = quality_map.get(req.quality, quality_map["1080p"])
            cmd = [
                "yt-dlp",
                "-f", fmt,
                "--merge-output-format", "mp4",
                "--extractor-args", "youtube:player_client=web",
                "-o", out_template,
                url,
            ]

        print(f"Running yt-dlp command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            print("yt-dlp stderr:", result.stderr[:500])
            raise HTTPException(422, f"Download failed: {result.stderr[:200]}")

        files = os.listdir(tmpdir)
        if not files:
            raise HTTPException(500, "No file was produced by yt-dlp.")

        filepath = os.path.join(tmpdir, files[0])
        safe_title = re.sub(r'[<>:"/\\|?*]', '', os.path.splitext(files[0])[0])[:80]
        filename = f"{safe_title}.{ext}"

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
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(504, "Download timed out. Try a lower quality.")
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("Unexpected error in /download:", str(e))
        raise HTTPException(500, f"Unexpected error: {str(e)}")