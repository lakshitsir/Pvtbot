import os
import asyncio
import subprocess
import math
import uuid
from pathlib import Path
from typing import Dict
import os
import time
import threading
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

app = Flask(__name__)

APP_SCRIPT = "app.py"
CHECK_INTERVAL = 300  # 5 minutes
process = None

def is_process_running(name):
    try:
        # Use pgrep to check if script is running
        output = subprocess.check_output(["pgrep", "-f", name])
        return bool(output.strip())
    except subprocess.CalledProcessError:
        return False

def start_app():
    global process
    print(f"Starting {APP_SCRIPT}...")
    process = subprocess.Popen(["python3", APP_SCRIPT])

def monitor_app():
    while True:
        if not is_process_running(APP_SCRIPT):
            print(f"{APP_SCRIPT} is not running. Restarting...")
            start_app()
        else:
            print(f"{APP_SCRIPT} is running.")
        time.sleep(CHECK_INTERVAL)

@app.route("/")
def status():
    running = is_process_running(APP_SCRIPT)
    return f"{APP_SCRIPT} is {'running ✅' if running else 'not running ❌'}."

if __name__ == "__main__":
    # Start monitor in background
    monitor_thread = threading.Thread(target=monitor_app)
    monitor_thread.daemon = True
    monitor_thread.start()

    # Start Flask app
    app.run(host="0.0.0.0", port=8000)


# ---------------------------
# CONFIG (replace these)
# ---------------------------
API_ID = 12767104              # 
API_HASH = "a0ce1daccf78234927eb68a62f894b97"     # 
BOT_TOKEN = "8449049312:AAF48rvDz7tl2bK9dC7R63OSO6u4_xh-_t8"   # 
a
# Performance tuning
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))  # number of parallel compress jobs
DOWNLOAD_CHUNK = 1024 * 1024  # chunk size for progress calcs (used for UI math)
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "veryfast")  # faster: ultrafast | veryfast | faster | medium
CRF = os.getenv("CRF", "28")  # quality (lower -> better size), 28 recommended for ~90% quality

# Working directories
TMP_DIR = Path(os.getenv("TMP_DIR", "neon_tmp")).resolve()
TMP_DIR.mkdir(parents=True, exist_ok=True)

# Pyrogram client
app = Client(
    "neon_concurrent_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Semaphore to limit concurrent jobs
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Track running tasks and metadata
running_jobs: Dict[str, asyncio.Task] = {}  # job_id -> task
job_meta: Dict[str, dict] = {}  # job_id -> {user_id, chat_id, orig_name, sizes...}

# UI templates (neon style)
START_TEXT = """🔮 𝗡𝗲𝗼𝗻 𝗛𝗤 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗼𝗿 — Online ⚡

Send any video/file and I'll ask before compressing.
You can process multiple files — I handle them concurrently.

Developer: @lakshitpatidar
"""

MEDIA_PROMPT_TEXT = """🔔 Media detected!

File: {name}
Size: {size_mb:.2f} MB

Should I compress this file now?
(Mode: HEVC • ~90% quality)
"""

PROGRESS_TEMPLATE = "⏳ {phase} — {percent:.1f}%\n{done_mb:.2f} MB / {total_mb:.2f} MB"

DONE_TEXT = """✅ Compression finished!
Original: {orig_mb:.2f} MB
Compressed: {new_mb:.2f} MB
Saved: {saved:.1f}%
"""

# Inline keyboard
PROMPT_KBD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✅ Yes — Compress", callback_data="compress_yes")],
        [InlineKeyboardButton("❌ No — Skip", callback_data="compress_no")],
    ]
)

# Helper functions
def human_mb(n_bytes: int) -> float:
    return n_bytes / (1024 * 1024)

def ffmpeg_compress(input_path: str, output_path: str) -> None:
    """
    Blocking call: run ffmpeg to compress video with libx265.
    This runs in a threadpool to avoid blocking asyncio loop.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vcodec", "libx265",
        "-crf", str(CRF),
        "-preset", FFMPEG_PRESET,
        "-acodec", "aac",
        "-b:a", "96k",
        str(output_path)
    ]
    # Run and let ffmpeg print to stdout/stderr (useful for debugging)
    subprocess.run(cmd, check=False)

async def download_with_progress(client: Client, message: Message, dest: Path, progress_msg: Message, phase_label: str="Downloading"):
    """
    Download media and update progress message.
    Uses pyrogram's download_media with its progress callback.
    """
    total = 0
    last_edit_ts = 0

    def _progress(current, total_bytes):
        nonlocal total, last_edit_ts
        total = total_bytes
        # update every ~0.8s to avoid rate limits
        now = asyncio.get_event_loop().time()
        if now - last_edit_ts < 0.8:
            return
        last_edit_ts = now
        percent = (current / total_bytes) * 100 if total_bytes else 0.0
        asyncio.create_task(progress_msg.edit(PROGRESS_TEMPLATE.format(
            phase=phase_label, percent=percent,
            done_mb=human_mb(current), total_mb=human_mb(total_bytes)
        )))

    # call pyrogram download_media (this is blocking await but has internal streaming)
    path = await client.download_media(message=message, file_name=str(dest), progress=_progress, progress_args=None)
    # final update
    await progress_msg.edit(PROGRESS_TEMPLATE.format(
        phase=phase_label, percent=100.0,
        done_mb=human_mb(total), total_mb=human_mb(total)
    ))
    return Path(path)

async def upload_with_progress(client: Client, chat_id: int, file_path: Path, caption: str, progress_msg: Message, phase_label: str="Uploading"):
    """
    Upload file with progress; uses pyrogram send_document progress callback.
    """
    total = file_path.stat().st_size
    last_edit_ts = 0

    async def _progress(current, total_bytes):
        nonlocal last_edit_ts
        now = asyncio.get_event_loop().time()
        if now - last_edit_ts < 0.8:
            return
        last_edit_ts = now
        percent = (current / total_bytes) * 100 if total_bytes else 0.0
        await progress_msg.edit(PROGRESS_TEMPLATE.format(
            phase=phase_label, percent=percent,
            done_mb=human_mb(current), total_mb=human_mb(total_bytes)
        ))

    await client.send_document(chat_id=chat_id, document=str(file_path), caption=caption, progress=_progress, progress_args=None)
    await progress_msg.edit(PROGRESS_TEMPLATE.format(
        phase=phase_label, percent=100.0,
        done_mb=human_mb(total), total_mb=human_mb(total)
    ))

# Job worker
async def process_job(client: Client, job_id: str):
    meta = job_meta.get(job_id)
    if not meta:
        return
    chat_id = meta["chat_id"]
    user_id = meta["user_id"]
    orig_name = meta["orig_name"]
    incoming_msg_id = meta["incoming_msg_id"]

    # Acquire semaphore (limit concurrent compress jobs)
    async with job_semaphore:
        progress_msg = await client.send_message(chat_id, f"🎛️ Job started: {orig_name}")
        try:
            # Step 1: download
            tmp_in = TMP_DIR / f"{job_id}_in"
            await progress_msg.edit("📥 Preparing download...")
            downloaded_path = await download_with_progress(client, meta["message_obj"], tmp_in, progress_msg, phase_label="Downloading")

            orig_size = downloaded_path.stat().st_size
            meta["orig_size"] = orig_size

            # Step 2: compress (run in executor to avoid blocking)
            await progress_msg.edit("⚙️ Compressing (fast HEVC)...")
            tmp_out = TMP_DIR / f"{job_id}_out.mp4"
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, ffmpeg_compress, str(downloaded_path), str(tmp_out))

            # Step 3: upload
            await progress_msg.edit("📤 Uploading compressed file...")
            new_size = tmp_out.stat().st_size
            caption = f"🎥 Compressed: {orig_name}\nSaved: {100 - (new_size / orig_size * 100):.1f}% (approx)\nMode: HEVC • ~90% quality"
            await upload_with_progress(client, chat_id, tmp_out, caption, progress_msg, phase_label="Uploading")

            # Step 4: final edit
            await progress_msg.edit(DONE_TEXT.format(orig_mb=human_mb(orig_size), new_mb=human_mb(new_size), saved=(100 - (new_size / orig_size * 100))))

        except Exception as e:
            await progress_msg.edit(f"❌ Error: {e}")
        finally:
            # cleanup files if exist
            try:
                for p in [tmp_in, tmp_out]:
                    if p.exists():
                        p.unlink()
            except Exception:
                pass
            # remove job from running_jobs
            running_jobs.pop(job_id, None)
            job_meta.pop(job_id, None)

# Handlers: start
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    await message.reply(START_TEXT)

# When media is detected, prompt user with Yes / No
@app.on_message(filters.private & (filters.video | filters.document | filters.audio | filters.photo))
async def media_detected(client: Client, message: Message):
    # If user sent multiple media in one message (album), pyrogram may handle each individually
    name = (message.document.file_name if message.document else
            (message.video.file_name if message.video else
             (message.audio.file_name if message.audio else "photo.jpg")))
    size = message.document.file_size if message.document else (message.video.file_size if message.video else (message.audio.file_size if message.audio else 0))
    # Prepare prompt
    prompt_text = MEDIA_PROMPT_TEXT.format(name=name, size_mb=human_mb(size))
    # Send prompt with inline buttons
    sent = await message.reply(prompt_text, reply_markup=PROMPT_KBD)
    # store mapping from message id to original message object for callback lookup
    # Save minimal meta to check later
    # We'll attach message_obj to job meta later when user presses Yes
    # Temporarily save pointer in job_meta keyed by the prompt message id
    job_meta_key = f"prompt_{sent.message_id}"
    job_meta[job_meta_key] = {
        "origin_msg": message,
        "prompt_msg_id": sent.message_id,
        "chat_id": message.chat.id,
        "user_id": message.from_user.id,
        "name": name,
        "size": size
    }

# Callback for Yes / No
@app.on_callback_query(filters.regex(r"compress_(yes|no)"))
async def on_prompt_answer(client: Client, callback_query):
    action = callback_query.data.split("_")[1]  # yes or no
    prompt_msg = callback_query.message
    user = callback_query.from_user
    # find job_meta entry by prompt_msg_id
    key = f"prompt_{prompt_msg.message_id}"
    meta = job_meta.get(key)
    if not meta:
        await prompt_msg.edit("⚠️ This prompt expired or is invalid.")
        return
    orig_msg = meta["origin_msg"]
    if action == "no":
        await prompt_msg.edit("❌ Skipped compression.")
        # cleanup prompt meta
        job_meta.pop(key, None)
        return

    # action == yes -> create a job
    await prompt_msg.edit("✅ Compression queued. You will receive progress updates here.")
    # create job id
    job_id = str(uuid.uuid4())[:8]
    job_meta[job_id] = {
        "chat_id": meta["chat_id"],
        "user_id": meta["user_id"],
        "orig_name": meta["name"],
        "incoming_msg_id": orig_msg.message_id,
        "message_obj": orig_msg  # keep the original message object for download
    }
    # remove prompt meta
    job_meta.pop(key, None)

    # start job task and track
    task = asyncio.create_task(process_job(client, job_id))
    running_jobs[job_id] = task
    await callback_query.answer("Queued ✅", show_alert=False)

# Admin /status command to see active jobs
@app.on_message(filters.command("status") & filters.private)
async def status_cmd(client: Client, message: Message):
    lines = [f"Max parallel jobs: {MAX_CONCURRENT_JOBS}"]
    lines.append(f"Running jobs: {len(running_jobs)}")
    for jid, t in running_jobs.items():
        lines.append(f"- {jid} state: {t._state}")
    await message.reply("\n".join(lines))

# Graceful shutdown handler
async def _shutdown():
    # cancel all running tasks
    for jid, t in list(running_jobs.items()):
        if not t.done():
            t.cancel()
    await asyncio.sleep(0.2)

# run the bot
if __name__ == "__main__":
    print("🔥 Neon Concurrent Compressor Bot starting...")
    try:
        app.run()
    finally:
        asyncio.run(_shutdown())