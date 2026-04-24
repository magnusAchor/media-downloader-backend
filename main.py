import os, re, json, tempfile, shutil, urllib.parse, urllib.request
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

INVIDIOUS_INSTANCES = [
    "https://invidious.snopyta.org",
    "https://yt.artemislena.eu",
    "https://invidious.nerdvpn.de",
    "https://invidious.privacydev.net",
    "https://inv.tux.pizza",
    "https://invidious.fdn.fr",
    "https://invidious.lunar.icu",
    "https://iv.datura.network",
]

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://piped-api.garudalinux.org",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.adminforge.de",
]

def fetch_streams(video_id: str):
    for instance in INVIDIOUS_INSTANCES:
        try:
            req_url = f"{instance}/api/v1/videos/{video_id}?fields=title,author,videoThumbnails,lengthSeconds,adaptiveFormats,formatStreams"
            print(f"Trying Invidious: {instance}")
            request = urllib.request.Request(
                req_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as r:
                data = json.loads(r.read().decode())

            if not data or ("adaptiveFormats" not in data and "formatStreams" not in data):
                continue

            video_streams = []
            audio_streams = []

            for f in data.get("formatStreams", []):
                if f.get("url") and f.get("resolution"):
                    video_streams.append({
                        "url": f["url"],
                        "quality": f.get("qualityLabel") or f.get("resolution", "?"),
                        "height": int(f.get("resolution", "0p").replace("p", "") or 0),
                        "mimeType": f.get("type", "video/mp4").split(";")[0],
                        "videoOnly": False,
                    })

            for f in data.get("adaptiveFormats", []):
                mime = f.get("type", "")
                furl = f.get("url", "")
                if not furl:
                    continue
                if "audio" in mime:
                    audio_streams.append({
                        "url": furl,
                        "bitrate": f.get("bitrate", 128000),
                        "mimeType": mime.split(";")[0],
                    })
                elif "video" in mime and f.get("resolution"):
                    height = int(f.get("resolution", "0p").replace("p", "") or 0)
                    video_streams.append({
                        "url": furl,
                        "quality": f.get("qualityLabel") or f.get("resolution", "?"),
                        "height": height,
                        "mimeType": mime.split(";")[0],
                        "videoOnly": True,
                    })

            if video_streams or audio_streams:
                print(f"Invidious success: {instance} — {len(video_streams)} video, {len(audio_streams)} audio")
                return {"videoStreams": video_streams, "audioStreams": audio_streams}

        except Exception as e:
            print(f"Invidious {instance} failed: {e}")
            continue

    for instance in PIPED_INSTANCES:
        try:
            req_url = f"{instance}/streams/{video_id}"
            print(f"Trying Piped: {instance}")
            request = urllib.request.Request(
                req_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as r:
                data = json.loads(r.read().decode())

            if not data or ("videoStreams" not in data and "audioStreams" not in data):
                continue

            video_streams = []
            audio_streams = []

            for s in data.get("videoStreams", []):
                if s.get("url"):
                    video_streams.append({
                        "url": s["url"],
                        "quality": s.get("quality", "?"),
                        "height": s.get("height", 0),
                        "mimeType": s.get("mimeType", "video/mp4"),
                        "videoOnly": s.get("videoOnly", False),
                    })
            for s in data.get("audioStreams", []):
                if s.get("url"):
                    audio_streams.append({
                        "url": s["url"],
                        "bitrate": s.get("bitrate", 128000),
                        "mimeType": s.get("mimeType", "audio/mp4"),
                    })

            if video_streams or audio_streams:
                print(f"Piped success: {instance}")
                return {"videoStreams": video_streams, "audioStreams": audio_streams}

        except Exception as e:
            print(f"Piped {instance} failed: {e}")
            continue

    return None

YDL_BASE_OPTS = {
    "quiet": False,
    "no_warnings": False,
    "socket_timeout": 30,
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "mweb"],
        }
    },
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
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

            title = "YouTube Video"
            author = "Unknown"
            thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            try:
                oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
                request = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=10) as r:
                    oembed = json.loads(r.read().decode())
                title = oembed.get("title", title)
                author = oembed.get("author_name", author)
                thumbnail = oembed.get("thumbnail_url", thumbnail)
            except Exception as e:
                print(f"oEmbed failed: {e}")

            streams_data = fetch_streams(video_id)
            video_streams = []
            audio_streams = []

            if streams_data:
                for s in streams_data.get("videoStreams", []):
                    if not s.get("videoOnly", True) and s.get("url"):
                        video_streams.append({
                            "url": s["url"],
                            "quality": s.get("quality", "?"),
                            "mimeType": s.get("mimeType", "video/mp4"),
                        })
                for s in streams_data.get("audioStreams", []):
                    if s.get("url"):
                        bitrate = s.get("bitrate", 128000)
                        audio_streams.append({
                            "url": s["url"],
                            "quality": f"{int(bitrate / 1000)} kbps",
                            "mimeType": s.get("mimeType", "audio/mp4"),
                        })

            if not video_streams:
                base = os.getenv("RENDER_EXTERNAL_URL", "")
                video_streams = [
                    {"url": f"{base}/download", "quality": "1080p", "mimeType": "video/mp4"},
                    {"url": f"{base}/download", "quality": "720p",  "mimeType": "video/mp4"},
                    {"url": f"{base}/download", "quality": "480p",  "mimeType": "video/mp4"},
                    {"url": f"{base}/download", "quality": "360p",  "mimeType": "video/mp4"},
                ]
            if not audio_streams:
                base = os.getenv("RENDER_EXTERNAL_URL", "")
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
                    "videoStreams": video_streams[:6],
                    "audioStreams": audio_streams[:3],
                },
                "downloadUrl": None,
                "progress": 0,
                "error": None,
            }

        else:
            opts = {**YDL_BASE_OPTS, "skip_download": True}
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
    except Exception as e:
        print("Unexpected error in /analyze:", str(e))
        raise HTTPException(500, f"Unexpected error: {str(e)}")


@app.post("/download")
def download(req: DownloadRequest):
    url = req.url.strip()
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "Unsupported platform.")

    try:
        if platform == "youtube":
            video_id = extract_youtube_id(url)
            if not video_id:
                raise HTTPException(400, "Could not extract YouTube video ID.")

            streams_data = fetch_streams(video_id)
            if not streams_data:
                raise HTTPException(422, "Could not fetch stream URLs. Please try again later.")

            quality_height = {
                "4K (2160p)": 2160,
                "1080p": 1080,
                "720p": 720,
                "480p": 480,
                "360p": 360,
            }
            max_h = quality_height.get(req.quality, 720)

            if req.format == "audio":
                audio_streams = streams_data.get("audioStreams", [])
                if not audio_streams:
                    raise HTTPException(422, "No audio streams available for this video.")
                stream = sorted(audio_streams, key=lambda s: s.get("bitrate", 0), reverse=True)[0]
                stream_url = stream["url"]
                filename = "audio.mp3"
            else:
                video_streams = streams_data.get("videoStreams", [])
                combined = [
                    s for s in video_streams
                    if not s.get("videoOnly", True)
                    and s.get("url")
                    and (s.get("height") or 0) <= max_h
                ]
                if not combined:
                    combined = [s for s in video_streams if not s.get("videoOnly", True) and s.get("url")]
                if not combined:
                    combined = [s for s in video_streams if s.get("url")]
                if not combined:
                    raise HTTPException(422, "No video streams available for this video.")
                stream = sorted(combined, key=lambda s: s.get("height") or 0, reverse=True)[0]
                stream_url = stream["url"]
                filename = "video.mp4"

            print(f"Returning direct stream URL: {stream_url[:80]}...")
            return {
                "downloadUrl": stream_url,
                "filename": filename,
                "direct": True,
            }

        else:
            tmpdir = tempfile.mkdtemp()
            try:
                ext = "mp3" if req.format == "audio" else "mp4"
                out_template = os.path.join(tmpdir, "media.%(ext)s")

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
                        "format": "best",
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
                print(f"Streaming: {filename} ({os.path.getsize(filepath)} bytes)")

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

            except Exception:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        print("yt-dlp DownloadError:", str(e))
        raise HTTPException(422, f"Download failed: {str(e)[:150]}")
    except Exception as e:
        print("Unexpected error in /download:", str(e))
        raise HTTPException(500, f"Unexpected error: {str(e)}")
