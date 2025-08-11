import os
import tempfile
import asyncio
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from yt_dlp import YoutubeDL
import aiofiles

app = FastAPI(title="Video Download API (No YouTube)")

# Blacklist of host substrings to block
BLACKLIST_HOST_SUBSTRINGS = {
    "youtube.com",
    "youtu.be",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
}


def is_blacklisted(url: str, blacklist=BLACKLIST_HOST_SUBSTRINGS) -> bool:
    """Check if the URL's hostname matches any blocked patterns."""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if ":" in host:  # remove port if present
            host = host.split(":")[0]
        return any(sub in host for sub in blacklist)
    except Exception:
        return False


def run_ydl_sync(url: str, outtmpl: str, resolution: str):
    """Run yt-dlp synchronously with given resolution."""
    opts = {
        "format": f"bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info


async def stream_file(path: str, chunk_size: int = 1024 * 64):
    """Async generator to stream a file in chunks."""
    async with aiofiles.open(path, "rb") as f:
        while True:
            chunk = await f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def remove_file(path: str):
    """Remove a file, ignore errors."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def remove_dir(path: str):
    """Remove a directory, ignore errors."""
    try:
        if os.path.exists(path):
            os.rmdir(path)
    except Exception:
        pass


@app.post("/download")
async def download_video(
    url: str = Form(...),
    resolution: str = Form("1080"),  # default to 1080p
    filename: str | None = Form(None),
    background_tasks: BackgroundTasks = None,
):
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    # Block YouTube links
    if is_blacklisted(url):
        raise HTTPException(
            status_code=403,
            detail="Downloads from YouTube are blocked by this service."
        )

    # Ensure background_tasks is always available
    if background_tasks is None:
        background_tasks = BackgroundTasks()

    # Create temp directory for download
    tmpdir = tempfile.mkdtemp(prefix="viddl_")
    outtmpl = os.path.join(tmpdir, "%(title).100s.%(ext)s")

    # Run yt-dlp with resolution
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, run_ydl_sync, url, outtmpl, resolution)
    except Exception as e:
        remove_dir(tmpdir)
        raise HTTPException(status_code=500, detail=f"Download error: {e}")

    # Find the downloaded file
    files = os.listdir(tmpdir)
    if not files:
        remove_dir(tmpdir)
        raise HTTPException(status_code=500, detail="No file produced.")
    files_full = [os.path.join(tmpdir, f) for f in files]
    chosen = max(files_full, key=lambda p: os.path.getsize(p))
    stat = os.stat(chosen)

    # Prepare download filename
    out_name = filename or os.path.basename(chosen)
    headers = {
        "Content-Length": str(stat.st_size),
        "Content-Disposition": f'attachment; filename="{out_name}"'
    }

    # Schedule file and dir cleanup after sending
    background_tasks.add_task(remove_file, chosen)
    background_tasks.add_task(remove_dir, tmpdir)

    return StreamingResponse(
        stream_file(chosen),
        media_type="application/octet-stream",
        headers=headers,
        background=background_tasks
    )
