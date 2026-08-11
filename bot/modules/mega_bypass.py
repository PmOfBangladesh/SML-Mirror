"""
mega_bypass.py  ─  SML-Mirror native MEGA bypass downloader
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• TaskListener / task_dict / /status / /cancel fully integrated
• Pipeline: file-1 dl completes → file-1 upload starts immediately
• Real-time download speed parsed from megadl --verbose
• Upload via TelegramUploader (same as /leech)
• Uses megareg / megadl binaries (not megatools)
"""

from __future__ import annotations

import asyncio
import random
import re
import shutil
import string
import subprocess
import time
from pathlib import Path

from faker import Faker
import sqlite3
import urllib.request
import json as _json

import pymailtm
from pymailtm.pymailtm import CouldNotGetAccountException, CouldNotGetMessagesException

from bot import (
    LOGGER,
    DOWNLOAD_DIR,
    task_dict,
    task_dict_lock,
    non_queued_dl,
    non_queued_up,
    queue_dict_lock,
)
from bot.helper.ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)
from bot.helper.ext_utils.task_manager import (
    check_running_tasks,
    start_from_queued,
)
from bot.helper.listeners.task_listener import TaskListener
from bot.helper.mirror_leech_utils.upload_utils.telegram_uploader import TelegramUploader
from bot.helper.mirror_leech_utils.status_utils.telegram_status import TelegramStatus
from bot.helper.mirror_leech_utils.status_utils.queue_status import QueueStatus
from bot.helper.telegram_helper.message_utils import (
    send_message,
    send_status_message,
    update_status_message,
    delete_message,
)

# megadl progress line (stdout):
# "filename.zip: 21.74% - 1.02 GiB of 4.7 GiB (45.23 MB/s)"
_MEGA_RE = re.compile(
    r"^(.+):\s+"
    r"(\d+(?:\.\d+)?)%\s+-\s+"
    r"([\d.]+)\s*(bytes?|KiB|MiB|GiB|TiB|KB|MB|GB|TB|B)\s+of\s+"
    r"([\d.]+)\s*(bytes?|KiB|MiB|GiB|TiB|KB|MB|GB|TB|B)"
    r"(?:\s+\(([\d.]+)\s*(bytes?|KiB|MiB|GiB|TiB|KB|MB|GB|TB|B)/s\))?",
    re.IGNORECASE,
)
_UNIT = {
    "bytes": 1, "byte": 1, "b": 1,
    "kib": 1024, "kb": 1024,
    "mib": 1024**2, "mb": 1024**2,
    "gib": 1024**3, "gb": 1024**3,
    "tib": 1024**4, "tb": 1024**4,
}

fake = Faker()

# ─────────────────────────────────────────────────────────────────────────────
# Temp email providers  —  fallback chain
# ─────────────────────────────────────────────────────────────────────────────

def _get_email_mailtm() -> tuple[str, str, str] | None:
    """Returns (email, id, password) or None."""
    try:
        acc = pymailtm.MailTm().get_account()
        return acc.address, acc.id_, acc.password
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] mail.tm failed: {e}")
        return None

def _get_email_guerrilla() -> tuple[str, str, str] | None:
    """
    Guerrilla Mail — no auth needed, just GET a random address.
    Returns (email, sid_token, '') — we poll by sid_token.
    """
    try:
        url = "https://api.guerrillamail.com/ajax.php?f=get_email_address"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.loads(r.read())
        email = data["email_addr"]
        sid   = data["sid_token"]
        LOGGER.info(f"[MegaBypass] GuerrillaM email: {email}")
        return email, sid, ""
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] guerrillamail failed: {e}")
        return None

def _guerrilla_get_verify_link(sid: str) -> str | None:
    """Poll guerrillamail inbox for MEGA verification link."""
    try:
        url = f"https://api.guerrillamail.com/ajax.php?f=get_email_list&offset=0&sid_token={sid}"
        for _ in range(15):
            time.sleep(5)
            with urllib.request.urlopen(url, timeout=10) as r:
                data = _json.loads(r.read())
            for mail in data.get("list", []):
                if "mega" in mail.get("mail_subject", "").lower() or "verification" in mail.get("mail_subject", "").lower():
                    # fetch full mail
                    mid = mail["mail_id"]
                    murl = f"https://api.guerrillamail.com/ajax.php?f=fetch_email&email_id={mid}&sid_token={sid}"
                    with urllib.request.urlopen(murl, timeout=10) as mr:
                        mdata = _json.loads(mr.read())
                    body = mdata.get("mail_body", "") + mdata.get("mail_body_text", "")
                    links = _extract_urls(body)
                    mega_links = [l for l in links if "mega.nz" in l and "verify" in l.lower()]
                    if mega_links:
                        return mega_links[0]
        return None
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] guerrillamail poll failed: {e}")
        return None

def _mailtm_get_verify_link(eid: str, email: str, epw: str) -> str | None:
    """Poll mail.tm inbox for MEGA verification link."""
    try:
        for _ in range(15):
            time.sleep(5)
            try:
                msgs = pymailtm.Account(eid, email, epw).get_messages()
            except Exception:
                continue
            for m in msgs:
                if "verification required" in m.subject.lower():
                    links = _extract_urls(m.text)
                    if links:
                        return links[0]
        return None
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] mail.tm poll failed: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# Account pool  —  SQLite cache so accounts are reused across tasks
# ─────────────────────────────────────────────────────────────────────────────

_DB_PATH = "/usr/src/app/mega_accounts.db"

def _db():
    con = sqlite3.connect(_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email    TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            status   TEXT NOT NULL DEFAULT 'active',
            created  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
    """)
    con.commit()
    return con

def _get_cached_account() -> dict | None:
    """Return a saved active account, or None if pool is empty."""
    try:
        with _db() as con:
            row = con.execute(
                "SELECT email, password FROM accounts WHERE status='active' ORDER BY created ASC LIMIT 1"
            ).fetchone()
        if row:
            return {"email": row[0], "password": row[1]}
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] DB read error: {e}")
    return None

def _save_account(email: str, password: str):
    try:
        with _db() as con:
            con.execute(
                "INSERT OR REPLACE INTO accounts (email, password, status) VALUES (?, ?, 'active')",
                (email, password)
            )
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] DB save error: {e}")

def _mark_account_dead(email: str):
    """Mark account as quota-exceeded or dead so it won't be reused."""
    try:
        with _db() as con:
            con.execute(
                "UPDATE accounts SET status='dead' WHERE email=?", (email,)
            )
    except Exception as e:
        LOGGER.warning(f"[MegaBypass] DB update error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Binary detection  —  megareg / megadl  (not "megatools")
# ─────────────────────────────────────────────────────────────────────────────

def _find_bin(name: str) -> str:
    cached = getattr(_find_bin, f"_{name}", None)
    if cached:
        return cached
    p = shutil.which(name)
    if not p:
        for c in [f"/usr/local/bin/{name}", f"/usr/bin/{name}", f"/opt/homebrew/bin/{name}"]:
            if Path(c).is_file():
                p = c
                break
    if not p:
        raise FileNotFoundError(f"{name} not found! Install megatools package.")
    setattr(_find_bin, f"_{name}", p)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# MegaBypassStatus  —  shown in /status during download phase
# duck-types the interface expected by status_utils.get_readable_message()
# ─────────────────────────────────────────────────────────────────────────────

class MegaBypassStatus:
    def __init__(self, listener: "MegaBypassListener", gid: str):
        self.listener = listener
        self._gid = gid
        self.engine = "MegaTools Bypass"

    def name(self) -> str:
        return self.listener.name or "…"

    def gid(self) -> str:
        return self._gid

    def progress(self) -> str:
        return f"{self.listener._dl_pct:.1f}%"

    def processed_bytes(self) -> str:
        return get_readable_file_size(self.listener._dl_done_bytes)

    def size(self) -> str:
        return get_readable_file_size(self.listener.size)

    def speed(self) -> str:
        return f"{get_readable_file_size(self.listener._dl_speed)}/s"

    def eta(self) -> str:
        try:
            remaining = self.listener.size - self.listener._dl_done_bytes
            return get_readable_time(int(remaining / self.listener._dl_speed))
        except Exception:
            return "-"

    def status(self) -> str:
        return MirrorStatus.STATUS_DOWNLOAD

    def task(self):
        return self

    async def cancel_task(self):
        self.listener.is_cancelled = True
        LOGGER.info(f"[MegaBypass] Cancelling: {self.listener.name}")
        if self.listener._dl_proc:
            try:
                self.listener._dl_proc.kill()
            except Exception:
                pass
        await self.listener.on_download_error("Task cancelled by user!")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rand(n: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def _extract_urls(text: str) -> list[str]:
    regex = (
        r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)"
        r"(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+"
        r"(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)"
        r"|[^\s`!()\[\]{};:'\".,<>?«»\u201c\u201d\u2018\u2019]))"
    )
    return [x[0] for x in re.findall(regex, text)]


# ─────────────────────────────────────────────────────────────────────────────
# Account creation  (blocking → asyncio.to_thread)
# megareg --scripted outputs:  megareg --verify <key> @LINK@
# We replace @LINK@ with the URL from the verification email, run via shell=True
# ─────────────────────────────────────────────────────────────────────────────

def _create_account() -> dict:
    """
    Try to create a MEGA account using a fallback chain of temp email providers:
      1. mail.tm  (pymailtm)
      2. guerrillamail (no-auth REST API)
    Each provider is tried up to 2 times before moving to next.
    """
    out: dict = {"email": None, "password": None, "success": False, "error": None}
    password = _rand(random.randint(10, 14))
    name = fake.name()

    try:
        megareg = _find_bin("megareg")
    except FileNotFoundError as e:
        out["error"] = str(e)
        return out

    # Provider chain: (label, get_fn, poll_fn)
    # get_fn  → (email, id_or_sid, pw) | None
    # poll_fn → verify_link_str | None
    providers = [
        ("mail.tm",       _get_email_mailtm,      None),   # poll handled inline
        ("guerrillamail", _get_email_guerrilla,    None),   # poll handled inline
    ]

    for provider_label, get_fn, _ in providers:
        for attempt in range(1, 3):
            LOGGER.info(f"[MegaBypass] [{provider_label}] attempt {attempt}/2…")

            result = get_fn()
            if not result:
                LOGGER.warning(f"[MegaBypass] [{provider_label}] get_email failed, retrying…")
                time.sleep(5)
                continue

            email, eid_or_sid, epw = result
            LOGGER.info(f"[MegaBypass] [{provider_label}] email: {email}")

            # Register with MEGA
            reg = subprocess.run(
                [megareg, "--scripted", "--register",
                 "--email", email, "--name", name, "--password", password],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            )
            if reg.returncode != 0:
                LOGGER.warning(f"[MegaBypass] megareg failed: {reg.stderr.strip()[:100]}")
                time.sleep(5)
                continue

            verify_template = reg.stdout.strip()
            LOGGER.info(f"[MegaBypass] verify template: {verify_template[:60]}…")

            # Poll inbox for verify link
            LOGGER.info(f"[MegaBypass] [{provider_label}] polling inbox…")
            if provider_label == "mail.tm":
                verify_link = _mailtm_get_verify_link(eid_or_sid, email, epw)
            else:
                verify_link = _guerrilla_get_verify_link(eid_or_sid)

            if not verify_link:
                LOGGER.warning(f"[MegaBypass] [{provider_label}] no verify link found")
                time.sleep(5)
                continue

            # Run verify command
            verify_cmd = verify_template.replace("@LINK@", verify_link)
            LOGGER.info(f"[MegaBypass] Running verify: {verify_cmd[:80]}…")
            try:
                vr = subprocess.run(
                    verify_cmd,
                    shell=True, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True,
                )
            except subprocess.CalledProcessError as e:
                err = e.output.strip()[:200] if e.output else str(e)
                LOGGER.warning(f"[MegaBypass] Verify failed: {err}")
                time.sleep(10)
                continue

            if "registered successfully!" not in vr.stdout:
                LOGGER.warning(f"[MegaBypass] No success msg: {vr.stdout.strip()[:100]}")
                time.sleep(10)
                continue

            # ✅ Success
            _save_account(email, password)
            out.update(email=email, password=password, success=True)
            LOGGER.info(f"[MegaBypass] Account ready via [{provider_label}]: {email}")
            return out

    out["error"] = "All email providers failed after retries"
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MegaBypassListener  —  extends TaskListener
# /cancel, /status, queue system all work automatically
# ─────────────────────────────────────────────────────────────────────────────

class MegaBypassListener(TaskListener):
    def __init__(self, client, message, url: str):
        self.message = message
        self.client = client
        super().__init__()          # TaskConfig.__init__ reads self.message

        self.link = url
        self.is_leech = True
        self.is_mega = False        # intentionally bypassing MEGA SDK
        self._set_mode_engine()
        self.mode = ("#MegaBypass", self.mode[1])

        # download-phase tracking (read by MegaBypassStatus)
        self._dl_pct: float = 0.0
        self._dl_speed: float = 0.0
        self._dl_done_bytes: int = 0
        self._dl_proc: asyncio.subprocess.Process | None = None

    # ─────────────────────────────────────────────────────────────────────────
    async def start_download(self, email: str, password: str):
        # set tag (user mention) — must be awaited, not in __init__
        await self.get_tag(self.message.text.split("\n"))
        # initialize split_size from LEECH_SPLIT_SIZE config (handles TG 2GB limit)
        from bot.core.config_manager import Config
        from bot.core.tg_client import TgClient
        self.split_size = (
            self.user_dict.get("LEECH_SPLIT_SIZE")
            or Config.LEECH_SPLIT_SIZE
        )
        self.max_split_size = (
            TgClient.MAX_SPLIT_SIZE if self.user_transmission else 2097152000
        )
        self.split_size = min(self.split_size, self.max_split_size)

        gid = _rand(8)
        dest = Path(f"{DOWNLOAD_DIR}{self.mid}/")
        dest.mkdir(parents=True, exist_ok=True)
        self.dir = f"{DOWNLOAD_DIR}{self.mid}"

        # register in task_dict → shows in /status immediately
        async with task_dict_lock:
            task_dict[self.mid] = MegaBypassStatus(self, gid)
        await send_status_message(self.message)

        # queue-system: wait for DL slot if needed
        add_to_queue, event = await check_running_tasks(self, "dl")
        if add_to_queue:
            LOGGER.info(f"[MegaBypass] Queued/DL: {self.link}")
            await event.wait()
            if self.is_cancelled:
                return

        async with queue_dict_lock:
            non_queued_dl.add(self.mid)

        LOGGER.info(f"[MegaBypass] Pipeline start: {self.link}")
        await self._pipeline(email, password, dest, gid)

    # ─────────────────────────────────────────────────────────────────────────
    async def _pipeline(self, email: str, password: str, dest: Path, gid: str):
        try:
            megadl = _find_bin("megadl")
        except FileNotFoundError as e:
            await self.on_download_error(str(e))
            return

        proc = await asyncio.create_subprocess_exec(
            megadl,
            "--username", email,
            "--password", password,
            "--path", str(dest),
            self.link,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._dl_proc = proc

        upload_queue: asyncio.Queue = asyncio.Queue()
        seen_files: set[Path] = set()
        dl_stderr_lines: list[str] = []

        # ── reader: parse megadl stdout progress + capture stderr errors ──
        async def _read_stdout():
            last_ui = 0.0
            async for raw in proc.stdout:
                if self.is_cancelled:
                    break
                line = raw.decode(errors="replace").strip()
                m = _MEGA_RE.match(line)
                if m:
                    fname          = m.group(1).strip()
                    self._dl_pct   = float(m.group(2))
                    done_bytes     = float(m.group(3)) * _UNIT.get(m.group(4).lower(), 1)
                    total_bytes    = float(m.group(5)) * _UNIT.get(m.group(6).lower(), 1)
                    self._dl_speed = float(m.group(7)) * _UNIT.get(m.group(8).lower(), 1) if m.group(7) else self._dl_speed
                    self._dl_done_bytes = int(done_bytes)
                    if not self.name:
                        self.name = fname
                    if total_bytes and not self.size:
                        self.size = int(total_bytes)
                    now = time.time()
                    if now - last_ui >= 4:
                        last_ui = now
                        await update_status_message(self.message.chat.id)
            await update_status_message(self.message.chat.id)

        async def _read_stderr():
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    dl_stderr_lines.append(line)
                    LOGGER.debug(f"[megadl] {line}")

        async def _reader():
            await asyncio.gather(_read_stdout(), _read_stderr())

        # ── watcher: detect completed files, push to upload_queue ────────
        async def _watcher():
            # megadl writes <file>.part while downloading,
            # renames to <file> when complete
            while not self.is_cancelled:
                try:
                    new = {
                        p for p in dest.rglob("*")
                        if p.is_file()
                        and p.suffix != ".part"
                        and not p.name.startswith(".megatmp")
                        and p.stat().st_size > 0
                        and p not in seen_files
                    }
                    for f in sorted(new):
                        seen_files.add(f)
                        await upload_queue.put(f)
                except Exception:
                    pass
                await asyncio.sleep(2)

            # drain any remaining after cancel/exit
            try:
                final = {
                    p for p in dest.rglob("*")
                    if p.is_file()
                    and p.suffix != ".part"
                    and not p.name.startswith(".megatmp")
                    and p.stat().st_size > 0
                    and p not in seen_files
                }
                for f in sorted(final):
                    seen_files.add(f)
                    await upload_queue.put(f)
            except Exception:
                pass
            await upload_queue.put(None)   # sentinel

        # ── uploader: upload each file as soon as it's ready ─────────────
        uploaded_count = 0
        upload_errors  = 0

        async def _uploader():
            nonlocal uploaded_count, upload_errors

            while True:
                try:
                    fpath: Path | None = await asyncio.wait_for(
                        upload_queue.get(), timeout=5
                    )
                except asyncio.TimeoutError:
                    if self.is_cancelled:
                        break
                    continue

                if fpath is None or self.is_cancelled:
                    break

                import shutil as _shutil

                fname = fpath.name
                fsize = fpath.stat().st_size if fpath.exists() else 0
                LOGGER.info(f"[MegaBypass] Uploading: {fname} ({fsize} bytes)")

                # Stage dir: TelegramUploader needs self.dir/self.name
                # TelegramUploader handles split automatically via LEECH_SPLIT_SIZE
                stage_dir = Path(f"{DOWNLOAD_DIR}{self.mid}_up_{_rand(4)}")
                stage_dir.mkdir(parents=True, exist_ok=True)
                stage_file = stage_dir / fname
                try:
                    _shutil.move(str(fpath), str(stage_file))
                except Exception as mv_err:
                    LOGGER.error(f"[MegaBypass] Stage failed: {mv_err}")
                    _shutil.rmtree(stage_dir, ignore_errors=True)
                    upload_errors += 1
                    continue

                self.name = fname
                self.size = fsize
                self.dir  = str(stage_dir)

                # queue slot
                add_to_up_queue, up_event = await check_running_tasks(self, "up")
                if add_to_up_queue:
                    async with task_dict_lock:
                        task_dict[self.mid] = QueueStatus(self, gid, "Up")
                    await update_status_message(self.message.chat.id)
                    await up_event.wait()
                    if self.is_cancelled:
                        _shutil.rmtree(stage_dir, ignore_errors=True)
                        break

                tg = TelegramUploader(self, str(stage_dir))

                async with task_dict_lock:
                    task_dict[self.mid] = TelegramStatus(self, tg, gid, "up")

                async with queue_dict_lock:
                    non_queued_up.add(self.mid)

                await update_status_message(self.message.chat.id)

                try:
                    await tg.upload()
                    uploaded_count += 1
                except Exception as e:
                    LOGGER.error(f"[MegaBypass] Upload error {fname}: {e}")
                    upload_errors += 1
                finally:
                    async with queue_dict_lock:
                        non_queued_up.discard(self.mid)
                    try:
                        _shutil.rmtree(stage_dir, ignore_errors=True)
                    except Exception:
                        pass

        # ── run all coroutines concurrently ───────────────────────────────
        reader_task   = asyncio.create_task(_reader())
        watcher_task  = asyncio.create_task(_watcher())
        uploader_task = asyncio.create_task(_uploader())

        await proc.wait()
        await asyncio.sleep(3)         # grace: last files finish writing
        watcher_task.cancel()
        await upload_queue.put(None)   # sentinel even if watcher cancelled
        reader_task.cancel()
        await uploader_task            # wait until all uploads complete

        async with queue_dict_lock:
            non_queued_dl.discard(self.mid)
        await start_from_queued()

        # ── result ────────────────────────────────────────────────────────
        if self.is_cancelled:
            await self.on_download_error("Task cancelled by user!")
            return

        if proc.returncode != 0:
            err_detail = " | ".join(dl_stderr_lines[-5:]) if dl_stderr_lines else f"exit code {proc.returncode}"
            # Mark account dead on quota/auth errors so it won't be reused
            if any(k in err_detail.lower() for k in ("509", "quota", "over quota", "403", "auth", "eagain")):
                _mark_account_dead(email)
                LOGGER.warning(f"[MegaBypass] Marked dead (quota/auth): {email}")
            await self.on_download_error(f"megadl failed: {err_detail}")
            return

        if uploaded_count == 0:
            await self.on_download_error("No files were downloaded from MEGA.")
            return

        LOGGER.info(f"[MegaBypass] Done — {uploaded_count} file(s) uploaded.")

        await self.on_upload_complete(
            link=None,
            files={},
            folders=uploaded_count,
            mime_type=upload_errors,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Command handler  —  add to handlers.py as /mega
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_mega(client, message):
    parts = message.text.split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""

    # Extract only the mega URL — ignore any extra args after it
    mega_match = re.search(r"https://mega\.nz/\S+", raw)
    url = mega_match.group(0) if mega_match else ""

    if not url and message.reply_to_message and message.reply_to_message.text:
        found = re.findall(r"https://mega\.nz/\S+", message.reply_to_message.text)
        if found:
            url = found[0]

    if not url.startswith("https://mega.nz"):
        return await send_message(
            message,
            "⚠️ Please send a valid <code>https://mega.nz</code> link."
        )

    # Try cached account first
    cached = await asyncio.to_thread(_get_cached_account)
    if cached:
        LOGGER.info(f"[MegaBypass] Using cached account: {cached['email']}")
        acc = cached
    else:
        wait_msg = await send_message(
            message,
            "🔑 <b>MEGA Bypass</b>\n<i>Creating bypass account…</i>"
        )
        acc = await asyncio.to_thread(_create_account)
        await delete_message(wait_msg)

        if not acc.get("success"):
            return await send_message(
                message,
                f"❌ <b>Account creation failed:</b>\n<code>{acc.get('error')}</code>"
            )

    listener = MegaBypassListener(client, message, url)
    asyncio.create_task(
        listener.start_download(acc["email"], acc["password"])
    )
