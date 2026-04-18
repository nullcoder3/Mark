"""
Telegram Video Streaming Backend
---------------------------------
FastAPI + Telethon backend that:
1. Connects to your Telegram account
2. Lists all videos from your channel
3. Streams videos to the browser on demand
"""

import os
import asyncio
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo, DocumentAttributeFilename
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_ID       = int(os.getenv("API_ID", "0"))
API_HASH     = os.getenv("API_HASH", "")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "")   # e.g. "@mychannel" or "-1001234567890"
SESSION_NAME = "telegram_session"
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Telegram Video Streamer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # In production, replace with your website URL
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Global Telegram client (created once at startup)
client: TelegramClient = None


@app.on_event("startup")
async def startup():
    global client
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    print("✅ Connected to Telegram!")


@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()


def get_video_info(message) -> dict | None:
    """Extract video metadata from a Telegram message."""
    if not message.media or not isinstance(message.media, MessageMediaDocument):
        return None

    doc = message.media.document
    is_video = any(isinstance(a, DocumentAttributeVideo) for a in doc.attributes)
    if not is_video:
        return None

    # Get filename
    filename = f"video_{message.id}.mp4"
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break

    # Get duration
    duration = 0
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            duration = attr.duration
            break

    return {
        "id":        message.id,
        "filename":  filename,
        "size":      doc.size,
        "duration":  duration,
        "date":      message.date.isoformat(),
        "caption":   message.message or filename,
        "mime_type": doc.mime_type or "video/mp4",
    }


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "running", "message": "Telegram Video Streamer API"}


@app.get("/videos")
async def list_videos(limit: int = 50):
    """Return a list of all videos in the channel."""
    videos = []
    try:
        async for message in client.iter_messages(CHANNEL_NAME, limit=limit):
            info = get_video_info(message)
            if info:
                videos.append(info)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"videos": videos, "count": len(videos)})


@app.get("/stream/{message_id}")
async def stream_video(message_id: int, request: Request):
    """Stream a video by its Telegram message ID (supports range requests for seeking)."""
    try:
        message = await client.get_messages(CHANNEL_NAME, ids=message_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Message not found: {e}")

    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    info = get_video_info(message)
    if not info:
        raise HTTPException(status_code=400, detail="Not a video message")

    file_size  = info["size"]
    mime_type  = info["mime_type"]

    # ── Handle Range requests (needed for video seeking) ──────────────────────
    range_header = request.headers.get("range")
    start, end = 0, file_size - 1

    if range_header:
        try:
            range_val = range_header.strip().replace("bytes=", "")
            parts     = range_val.split("-")
            start     = int(parts[0]) if parts[0] else 0
            end       = int(parts[1]) if parts[1] else file_size - 1
        except Exception:
            pass

    chunk_size    = end - start + 1
    offset        = start
    # Telethon streams in 1MB chunks; align to nearest 1MB boundary
    request_size  = min(1 * 1024 * 1024, chunk_size)

    async def video_generator() -> AsyncGenerator[bytes, None]:
        downloaded = 0
        async for chunk in client.iter_download(
            message.media,
            offset      = offset,
            request_size= request_size,
            limit       = chunk_size,
        ):
            downloaded += len(chunk)
            yield chunk
            if downloaded >= chunk_size:
                break

    status_code = 206 if range_header else 200
    headers = {
        "Content-Range":  f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges":  "bytes",
        "Content-Length": str(chunk_size),
        "Content-Type":   mime_type,
    }

    return StreamingResponse(
        video_generator(),
        status_code = status_code,
        headers     = headers,
        media_type  = mime_type,
    )


@app.get("/thumbnail/{message_id}")
async def get_thumbnail(message_id: int):
    """Return a video thumbnail (if available)."""
    try:
        message = await client.get_messages(CHANNEL_NAME, ids=message_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Message not found")

    if not message or not message.media:
        raise HTTPException(status_code=404, detail="No media found")

    try:
        thumb_bytes = await client.download_media(message.media, bytes, thumb=-1)
        if thumb_bytes:
            return StreamingResponse(
                iter([thumb_bytes]),
                media_type="image/jpeg",
            )
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="No thumbnail available")
