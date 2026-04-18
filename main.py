"""
Telegram Video Streaming Backend (Render-compatible fix)
"""

import os
import asyncio
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo, DocumentAttributeFilename

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_ID       = int(os.environ.get("API_ID", "0"))
API_HASH     = os.environ.get("API_HASH", "")
CHANNEL_NAME = os.environ.get("CHANNEL_NAME", "")

# Session file lives in /opt/render/project/src/ on Render
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(BASE_DIR, "telegram_session")
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Telegram Video Streamer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

client: TelegramClient = None


@app.on_event("startup")
async def startup():
    global client

    # Validate env vars
    if not API_ID or API_ID == 0:
        raise RuntimeError("API_ID environment variable is missing or zero!")
    if not API_HASH:
        raise RuntimeError("API_HASH environment variable is missing!")
    if not CHANNEL_NAME:
        raise RuntimeError("CHANNEL_NAME environment variable is missing!")

    # Check session file exists
    session_file = SESSION_PATH + ".session"
    if not os.path.exists(session_file):
        raise RuntimeError(
            f"Session file not found at {session_file}. "
            "Did you upload telegram_session.session to your GitHub repo?"
        )

    print(f"✅ Session file found: {session_file}")
    print(f"✅ Connecting with API_ID={API_ID}, CHANNEL={CHANNEL_NAME}")

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is not authorized. "
            "Please re-create the session file using Replit."
        )

    me = await client.get_me()
    print(f"✅ Connected to Telegram as: {me.first_name} (@{me.username})")


@app.on_event("shutdown")
async def shutdown():
    global client
    if client:
        await client.disconnect()


def get_video_info(message) -> dict | None:
    if not message.media or not isinstance(message.media, MessageMediaDocument):
        return None

    doc = message.media.document
    is_video = any(isinstance(a, DocumentAttributeVideo) for a in doc.attributes)
    if not is_video:
        return None

    filename = f"video_{message.id}.mp4"
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break

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


@app.get("/")
async def root():
    return {"status": "running", "message": "Telegram Video Streamer API ✅"}


@app.get("/videos")
async def list_videos(limit: int = 50):
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
    try:
        message = await client.get_messages(CHANNEL_NAME, ids=message_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Message not found: {e}")

    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    info = get_video_info(message)
    if not info:
        raise HTTPException(status_code=400, detail="Not a video message")

    file_size = info["size"]
    mime_type = info["mime_type"]

    range_header = request.headers.get("range")
    start, end = 0, file_size - 1

    if range_header:
        try:
            range_val = range_header.strip().replace("bytes=", "")
            parts = range_val.split("-")
            start = int(parts[0]) if parts[0] else 0
            end   = int(parts[1]) if parts[1] else file_size - 1
        except Exception:
            pass

    chunk_size   = end - start + 1
    request_size = min(1 * 1024 * 1024, chunk_size)

    async def video_generator() -> AsyncGenerator[bytes, None]:
        downloaded = 0
        async for chunk in client.iter_download(
            message.media,
            offset=start,
            request_size=request_size,
            limit=chunk_size,
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
        status_code=status_code,
        headers=headers,
        media_type=mime_type,
    )


@app.get("/thumbnail/{message_id}")
async def get_thumbnail(message_id: int):
    try:
        message = await client.get_messages(CHANNEL_NAME, ids=message_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Message not found")

    if not message or not message.media:
        raise HTTPException(status_code=404, detail="No media found")

    try:
        thumb_bytes = await client.download_media(message.media, bytes, thumb=-1)
        if thumb_bytes:
            return StreamingResponse(iter([thumb_bytes]), media_type="image/jpeg")
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="No thumbnail available")
