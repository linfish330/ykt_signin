import asyncio
import json
import logging
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_fmt = logging.Formatter(
    fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
_fmt.converter = time.gmtime

logging.basicConfig(level=logging.INFO)
logging.root.handlers[0].setFormatter(_fmt)

for _name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _logger = logging.getLogger(_name)
    for _h in _logger.handlers:
        _h.setFormatter(_fmt)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional, Union

import base64

from Crypto.PublicKey import RSA as CryptoRSA
from Crypto.Cipher import PKCS1_v1_5

import websocket
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from checkin import CheckinError, RainClassroomClient
import event_log
import pushdeer
from config import (
    CHECKIN_SOURCE_OPTIONS, DEFAULT_CHECKIN_SOURCE, DEFAULT_COURSE_CONFIG, DEFAULT_DOMAIN,
    AUTO_CHECKIN_MODES, DEFAULT_AI_ANSWERING_MODE, DEFAULT_AUTO_CHECKIN, DEFAULT_AUTO_CHECKIN_MODE, DEFAULT_AUTO_CHECKIN_TIME,
    DEFAULT_CHECKIN_DELAY, DEFAULT_POLL_INTERVAL, DOMAIN_OPTIONS, QR_CHECKIN_SOURCE,
    MAX_CHECKIN_DELAY, MAX_POLL_INTERVAL, MIN_CHECKIN_DELAY, MIN_POLL_INTERVAL,
    _config_lock,
    account_exists, api_url, delete_account,
    get_account, get_active_account_id, get_ai_config, get_checkin_source, get_config,
    get_ai_answering_mode, get_auto_checkin, get_auto_checkin_mode, get_auto_checkin_time, get_checkin_delay, get_course_config, get_domain, get_poll_interval, get_pushdeer_config, get_sessionid,
    http_request, list_accounts_summary, make_headers, new_empty_account,
    save_config, set_active_account_id, set_domain, set_poll_interval,
    set_ai_answering_enabled, set_ai_answering_mode, set_auto_checkin_mode, set_checkin_delay, set_checkin_source, update_account, update_ai_config, update_course_config, update_pushdeer_config,
    validate_auto_checkin_time,
    upsert_account,
)
from monitor import Monitor

URL_WSS = "wss://{domain}/wsapp/"
URL_USER_INFO = "https://{domain}/api/v3/user/basic-info"
URL_COURSE_LIST = "https://{domain}/v2/api/web/courses/list?identity=2"
URL_WEB_LOGIN = "https://{domain}/pc/web_login"
URL_PASSWORD_LOGIN = "https://{domain}/pc/login/verify_pwd_login/"
URL_GET_PUBLIC_KEY = "https://{domain}/pc/register/get_pws_public_key/"

# ---------------------------------------------------------------------------
# Application state — one event queue per account, shared subscriber list
# ---------------------------------------------------------------------------


event_queue: asyncio.Queue = asyncio.Queue()
# subscribers: { account_id: set[asyncio.Queue] }
_subscribers: dict[str, set[asyncio.Queue]] = {}
_subscribers_lock = threading.Lock()

# Registry of monitors, one per account
_monitors: dict[str, Monitor] = {}
_monitors_lock = threading.Lock()

# In-memory pending account slots — never persisted; lost on restart
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _pending_or_account(account_id: str) -> Optional[dict]:
    with _pending_lock:
        p = _pending.get(account_id)
    if p:
        return {"domain": p.get("domain") or DEFAULT_DOMAIN}
    return get_account(account_id)


def _get_monitor(account_id: str) -> Optional[Monitor]:
    with _monitors_lock:
        return _monitors.get(account_id)


def _set_monitor(account_id: str, m: Optional[Monitor]) -> None:
    with _monitors_lock:
        if m is None:
            _monitors.pop(account_id, None)
        else:
            _monitors[account_id] = m


def _handle_session_expired(account_id: str) -> None:
    # Clear cached session + user info so the UI reflects logged-out state
    # immediately and a stale name/avatar isn't shown in the switcher.
    update_account(account_id, {"sessionid": "", "user": {}, "course_list": []})
    _set_monitor(account_id, None)


def _start_monitor(account_id: str) -> None:
    loop = asyncio.get_running_loop()
    existing = _get_monitor(account_id)
    if existing:
        existing.stop()
    m = Monitor(account_id=account_id, event_queue=event_queue, on_session_expired=_handle_session_expired)
    _set_monitor(account_id, m)
    m.start(loop)


def _stop_monitor(account_id: str) -> None:
    m = _get_monitor(account_id)
    if m:
        m.stop()
        _set_monitor(account_id, None)


def _kick_subscribers(account_id: str) -> None:
    """Push a sentinel into every subscriber queue for this account so the
    WebSocket handler exits its `await q.get()` and closes cleanly."""
    with _subscribers_lock:
        queues = list(_subscribers.pop(account_id, ()))
    for q in queues:
        try:
            q.put_nowait({"type": "account_closed"})
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Per-account cache refresh after login
# ---------------------------------------------------------------------------


def _refresh_local_cache(account_id: str, user: Optional[dict] = None) -> None:
    """Populate user info + course_list for `account_id`. If `user` is supplied
    it's used as-is; otherwise fetched. Individual HTTP failures are logged
    but do not raise so partial-success state (sessionid saved, courses missing)
    is still written."""
    acc = get_account(account_id) or {}
    domain = acc.get("domain") or DEFAULT_DOMAIN
    sessionid = acc.get("sessionid", "")
    if not sessionid:
        return
    headers = make_headers(domain, sessionid)

    if user is None:
        try:
            user = http_request("GET", api_url(domain, URL_USER_INFO), headers=headers).json()["data"]
        except Exception as e:  # noqa: BLE001
            logging.getLogger("refresh").warning("[%s] user info fetch failed: %s", account_id, e)
            user = {}

    try:
        raw_courses = http_request("GET", api_url(domain, URL_COURSE_LIST), headers=headers).json()["data"]["list"]
    except Exception as e:  # noqa: BLE001
        logging.getLogger("refresh").warning("[%s] course list fetch failed: %s", account_id, e)
        raw_courses = None  # leave existing courses alone

    course_list = None
    if raw_courses is not None:
        course_list = [
            {
                "classroom_id": str(c["classroom_id"]),
                "name": c["course"]["name"],
                "classroom_name": c["name"],
                "teacher_name": c["teacher"]["name"],
            }
            for c in raw_courses
        ]

    with _config_lock:  # one atomic transaction for the cache update
        cfg = get_config()
        acc = cfg.setdefault("accounts", {}).setdefault(account_id, new_empty_account(domain))
        if user:
            acc["user"] = user
            acc["name"] = user.get("name") or acc.get("name", "")
        if course_list is not None:
            acc["course_list"] = course_list
            courses = acc.setdefault("courses", {})
            for c in course_list:
                cid = c["classroom_id"]
                if cid not in courses:
                    courses[cid] = {"name": c["name"], **DEFAULT_COURSE_CONFIG}
                elif courses[cid].get("name") != c["name"]:
                    courses[cid]["name"] = c["name"]
        save_config(cfg)


def _finalize_login(pending_id: str, sessionid: str) -> str:
    """Resolve the pending domain (in-memory or persistent), look up user.id,
    persist a real account keyed by user.id, then refresh cache + start monitor."""
    with _pending_lock:
        pending = _pending.pop(pending_id, None)
    if pending:
        domain = pending.get("domain") or DEFAULT_DOMAIN
    else:
        existing = get_account(pending_id) or {}
        domain = existing.get("domain") or DEFAULT_DOMAIN

    # Single user-info fetch; reuse the result in _refresh_local_cache.
    headers = make_headers(domain, sessionid)
    user = http_request("GET", api_url(domain, URL_USER_INFO), headers=headers).json()["data"]
    user_id = str(user.get("id") or "") or pending_id

    if account_exists(user_id):
        update_account(user_id, {"sessionid": sessionid, "domain": domain})
    else:
        acc = new_empty_account(domain)
        acc["sessionid"] = sessionid
        upsert_account(user_id, acc)

    if pending_id != user_id and account_exists(pending_id):
        delete_account(pending_id)

    try:
        _refresh_local_cache(user_id, user=user)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("login").warning("[%s] refresh after login failed: %s", user_id, e)
    set_active_account_id(user_id)
    _start_monitor(user_id)
    return user_id


# ---------------------------------------------------------------------------
# Lifespan — start monitors for all logged-in accounts
# ---------------------------------------------------------------------------


async def _broadcast_events():
    log = logging.getLogger("broadcast")
    while True:
        try:
            event = await event_queue.get()
            aid = event.get("account_id")
            dead: list[asyncio.Queue] = []
            with _subscribers_lock:
                queues = list(_subscribers.get(aid, ()))
            for q in queues:
                if q.full():
                    dead.append(q)
                else:
                    q.put_nowait(event)
            if dead:
                with _subscribers_lock:
                    bucket = _subscribers.get(aid)
                    if bucket:
                        for q in dead:
                            bucket.discard(q)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("event broadcast failed: %s", e)


_HEARTBEAT_TZ = ZoneInfo("Asia/Shanghai")
_HEARTBEAT_HOUR = 7


def _seconds_until_next_heartbeat() -> float:
    now = datetime.now(_HEARTBEAT_TZ)
    target = now.replace(hour=_HEARTBEAT_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _pushdeer_heartbeat_scheduler():
    log = logging.getLogger("heartbeat")
    while True:
        try:
            wait_s = _seconds_until_next_heartbeat()
            log.info("next PushDeer heartbeat in %.0fs", wait_s)
            await asyncio.sleep(wait_s)
            for summary in list_accounts_summary():
                if not summary["logged_in"]:
                    continue
                try:
                    await asyncio.to_thread(pushdeer.send_liveness, summary["id"])
                except Exception as e:  # noqa: BLE001
                    log.warning("heartbeat for %s failed: %s", summary["id"], e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("heartbeat scheduler error: %s", e)
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global event_queue
    loop = asyncio.get_running_loop()
    # asyncio.Queue binds to the event loop that first awaits it.  Recreate it
    # for each application lifespan so TestClient reloads and controlled
    # restarts cannot inherit a queue tied to a previous loop.
    event_queue = asyncio.Queue()

    broadcaster = asyncio.create_task(_broadcast_events())
    heartbeat_task = asyncio.create_task(_pushdeer_heartbeat_scheduler())

    # Sweep any orphaned pending-* accounts persisted by earlier versions.
    for summary in list_accounts_summary():
        if summary["id"].startswith("pending-"):
            delete_account(summary["id"])

    for summary in list_accounts_summary():
        if summary["logged_in"]:
            aid = summary["id"]
            try:
                _refresh_local_cache(aid)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("startup").warning("refresh cache failed for %s: %s", aid, e)
            m = Monitor(account_id=aid, event_queue=event_queue, on_session_expired=_handle_session_expired)
            _set_monitor(aid, m)
            m.start(loop)

    yield

    broadcaster.cancel()
    heartbeat_task.cancel()

    with _monitors_lock:
        ms = list(_monitors.values())
        _monitors.clear()
    for m in ms:
        m.stop()


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


app = FastAPI(title="Yuketang Helper API", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class NotificationSub(BaseModel):
    enabled: bool
    signin: bool
    problem: bool
    call: bool
    danmu: bool
    red_packet: bool = True


class CourseConfig(BaseModel):
    type1: Literal["ai", "random", "off"]
    type2: Literal["ai", "random", "off"]
    type3: Literal["ai", "random", "off"]
    type4: Literal["ai", "random", "off"]
    type5: Literal["ai", "random", "off"]
    course_enabled: bool = True
    answer_last5s: bool = True
    auto_danmu: bool
    auto_redpacket: bool = True
    danmu_threshold: int
    checkin_source: Union[int, Literal["inherit"], None] = "inherit"
    notification: NotificationSub
    voice_notification: NotificationSub
    pushdeer_notification: NotificationSub

    @field_validator("checkin_source")
    @classmethod
    def validate_source(cls, value: Union[int, str, None]) -> Union[int, str, None]:
        if value is None or value == "inherit":
            return value
        if isinstance(value, bool):
            raise ValueError("unsupported checkin_source")
        if value not in {option["value"] for option in CHECKIN_SOURCE_OPTIONS}:
            raise ValueError("unsupported checkin_source")
        return value


class CheckinSourceBody(BaseModel):
    checkin_source: int

    @field_validator("checkin_source")
    @classmethod
    def validate_source(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("unsupported checkin_source")
        if value not in {option["value"] for option in CHECKIN_SOURCE_OPTIONS}:
            raise ValueError("unsupported checkin_source")
        return value


class AutoCheckinBody(BaseModel):
    # ``auto_checkin`` remains accepted for older clients.
    auto_checkin: Optional[bool] = None
    auto_checkin_mode: Optional[Literal["on", "scheduled", "off"]] = None
    auto_checkin_time: Optional[str] = None

    @field_validator("auto_checkin_time")
    @classmethod
    def validate_time(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return validate_auto_checkin_time(value)


class AiAnsweringBody(BaseModel):
    ai_answering_mode: Optional[Literal["ai", "random", "off"]] = None
    # Backward-compatible request field for existing clients.
    ai_answering_enabled: Optional[bool] = None


def _has_configured_ai_key(account_id: str) -> bool:
    """Return whether the account has at least one non-empty AI API key."""
    cfg = get_ai_config(account_id)
    return any(
        isinstance(entry, dict) and bool(str(entry.get("key", "")).strip())
        for entry in cfg.get("keys", [])
    )


def _course_enables_ai_without_key(account_id: str, course_id: str, data: dict) -> bool:
    """Detect a course-level transition into AI mode without a configured key."""
    current = get_course_config(account_id, course_id)
    return any(
        data.get(f"type{problem_type}") == "ai"
        and current.get(f"type{problem_type}") != "ai"
        for problem_type in range(1, 6)
    ) and not _has_configured_ai_key(account_id)


class QRCheckinBody(BaseModel):
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("QR code content cannot be empty")
        return value


class AIKeyEntry(BaseModel):
    name: str = "DeepSeek"
    provider: str = "deepseek"
    key: str = Field(min_length=1, max_length=512)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("key cannot be empty")
        return value


class DeepSeekKeyBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("api_key cannot be empty")
        return value


class AIActiveKey(BaseModel):
    active_key: int


class PushdeerKeyEntry(BaseModel):
    name: str
    endpoint: str
    push_key: str


class PushdeerActiveKey(BaseModel):
    active_key: int


class PushdeerLanguage(BaseModel):
    language: str


class PollIntervalBody(BaseModel):
    poll_interval: int


class CheckinDelayBody(BaseModel):
    checkin_delay: int


class DomainPatch(BaseModel):
    domain: str


class SetActiveAccount(BaseModel):
    account_id: Optional[str]


class CreateAccountRequest(BaseModel):
    domain: str = DEFAULT_DOMAIN


class PasswordLoginBody(BaseModel):
    phone: str
    password: str
    ticket: str
    randstr: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_account(account_id: str) -> dict:
    acc = get_account(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    return acc


def _emit_account_event(account_id: str, event_type: str, data: dict) -> None:
    """Emit non-Monitor events through the same log/WebSocket path."""
    monitor = _get_monitor(account_id)
    if monitor is not None:
        monitor.emit_event(event_type, data)
        return
    event = {"type": event_type, "account_id": account_id, **data}
    event_log.append(account_id, event)
    event_queue.put_nowait(event)


def _publish_account_message(account_id: str, message_type: str, data: Optional[dict] = None) -> None:
    """Publish a transient account message without adding it to the log."""
    event_queue.put_nowait({"type": message_type, "account_id": account_id, **(data or {})})


# ---------------------------------------------------------------------------
# Global / domain options
# ---------------------------------------------------------------------------


@app.get("/api/domains")
async def list_domains():
    return {"options": DOMAIN_OPTIONS, "default": DEFAULT_DOMAIN}


# ---------------------------------------------------------------------------
# Check-in source and manual QR entry
# ---------------------------------------------------------------------------


@app.get("/api/accounts/{account_id}/checkin-source")
async def get_account_checkin_source(account_id: str):
    _require_account(account_id)
    return {
        "checkin_source": get_checkin_source(account_id),
        "default": DEFAULT_CHECKIN_SOURCE,
        "options": CHECKIN_SOURCE_OPTIONS,
    }


@app.put("/api/accounts/{account_id}/checkin-source")
@app.patch("/api/accounts/{account_id}/checkin-source")
async def set_account_checkin_source(account_id: str, body: CheckinSourceBody):
    _require_account(account_id)
    source = set_checkin_source(account_id, body.checkin_source)
    return {"ok": True, "checkin_source": source}


@app.get("/api/accounts/{account_id}/auto-checkin")
async def get_account_auto_checkin(account_id: str):
    _require_account(account_id)
    mode = get_auto_checkin_mode(account_id)
    return {
        "auto_checkin": get_auto_checkin(account_id),
        "default": DEFAULT_AUTO_CHECKIN,
        "auto_checkin_mode": mode,
        "auto_checkin_time": get_auto_checkin_time(account_id),
        "default_mode": DEFAULT_AUTO_CHECKIN_MODE,
        "default_time": DEFAULT_AUTO_CHECKIN_TIME,
        "modes": list(AUTO_CHECKIN_MODES),
    }


@app.put("/api/accounts/{account_id}/auto-checkin")
@app.patch("/api/accounts/{account_id}/auto-checkin")
async def set_account_auto_checkin(account_id: str, body: AutoCheckinBody):
    _require_account(account_id)
    if body.auto_checkin_mode is None and body.auto_checkin is None and body.auto_checkin_time is None:
        raise HTTPException(status_code=422, detail="auto_checkin_mode or auto_checkin is required")

    mode = body.auto_checkin_mode
    if mode is None:
        mode = ("on" if body.auto_checkin else "off") if body.auto_checkin is not None else get_auto_checkin_mode(account_id)
    mode, schedule_time = set_auto_checkin_mode(account_id, mode, body.auto_checkin_time)
    monitor = _get_monitor(account_id)
    if monitor:
        monitor.wake()
    return {
        "ok": True,
        "auto_checkin": get_auto_checkin(account_id),
        "default": DEFAULT_AUTO_CHECKIN,
        "auto_checkin_mode": mode,
        "auto_checkin_time": schedule_time,
        "default_mode": DEFAULT_AUTO_CHECKIN_MODE,
        "default_time": DEFAULT_AUTO_CHECKIN_TIME,
        "modes": list(AUTO_CHECKIN_MODES),
    }


@app.get("/api/accounts/{account_id}/ai-answering")
async def get_account_ai_answering(account_id: str):
    _require_account(account_id)
    mode = get_ai_answering_mode(account_id)
    return {
        "ai_answering_mode": mode,
        "ai_answering_enabled": mode == "ai",
        "ai_api_key_configured": _has_configured_ai_key(account_id),
        "default": DEFAULT_AI_ANSWERING_MODE,
    }


@app.put("/api/accounts/{account_id}/ai-answering")
@app.patch("/api/accounts/{account_id}/ai-answering")
async def set_account_ai_answering(account_id: str, body: AiAnsweringBody):
    _require_account(account_id)
    if body.ai_answering_mode is not None:
        mode = body.ai_answering_mode
    elif body.ai_answering_enabled is not None:
        mode = "ai" if body.ai_answering_enabled else "off"
    else:
        raise HTTPException(status_code=422, detail="ai_answering_mode is required")

    if mode == "ai" and not _has_configured_ai_key(account_id):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ai_api_key_required",
                "message": "Configure an AI API key before enabling AI answering",
            },
        )

    # Persist only after validation so an invalid AI enable request cannot
    # leave the account in AI mode while silently falling back to random.
    if body.ai_answering_mode is not None:
        mode = set_ai_answering_mode(account_id, body.ai_answering_mode)
    elif body.ai_answering_enabled is not None:
        mode = "ai" if set_ai_answering_enabled(account_id, body.ai_answering_enabled) else "off"
    return {
        "ok": True,
        "ai_answering_mode": mode,
        "ai_answering_enabled": mode == "ai",
        "ai_api_key_configured": _has_configured_ai_key(account_id),
    }


@app.post("/api/accounts/{account_id}/checkin/qr")
async def checkin_from_qr(account_id: str, body: QRCheckinBody):
    """Scan a pasted QR value through Yuketang, then join that lesson."""
    _require_account(account_id)
    monitor = _get_monitor(account_id)
    if monitor is None:
        _start_monitor(account_id)
        monitor = _get_monitor(account_id)

    try:
        client = RainClassroomClient.for_account(account_id)
        scan_result = await asyncio.to_thread(client.scan_qr_code, body.url)
    except CheckinError as exc:
        logging.getLogger("qr").warning(
            "[QR] account=%s scan failed code=%s", account_id, exc.code,
        )
        _emit_account_event(account_id, "qr_scan", {
            "status": "error",
            "code": exc.code,
            "message": exc.message,
        })
        return {"ok": False, "code": exc.code, "message": exc.message, "stage": exc.stage}

    lesson_id = scan_result.lesson_id
    _emit_account_event(account_id, "qr_scan", {
        "lessonid": lesson_id,
        "status": "success",
        "message": "QR code parsed",
    })

    if monitor is None:
        message = "account monitor is unavailable"
        logging.getLogger("qr").warning("[QR] account=%s check-in failed: monitor unavailable", account_id)
        _emit_account_event(account_id, "qr_checkin", {
            "lessonid": lesson_id,
            "status": "error",
            "code": "monitor_unavailable",
            "message": message,
        })
        return {"ok": False, "code": "monitor_unavailable", "message": message, "stage": "checkin"}

    try:
        _, already_running = await asyncio.to_thread(monitor.start_qr_lesson, lesson_id)
    except CheckinError as exc:
        logging.getLogger("qr").warning(
            "[QR] account=%s check-in failed lesson=%s code=%s",
            account_id, lesson_id, exc.code,
        )
        _emit_account_event(account_id, "qr_checkin", {
            "lessonid": lesson_id,
            "source": QR_CHECKIN_SOURCE,
            "status": "error",
            "code": exc.code,
            "message": exc.message,
        })
        return {"ok": False, "code": exc.code, "message": exc.message, "stage": exc.stage}
    except Exception:  # noqa: BLE001
        logging.getLogger("qr").exception(
            "[QR] account=%s check-in failed lesson=%s", account_id, lesson_id,
        )
        message = "unable to enter classroom"
        _emit_account_event(account_id, "qr_checkin", {
            "lessonid": lesson_id,
            "source": QR_CHECKIN_SOURCE,
            "status": "error",
            "code": "checkin_failed",
            "message": message,
        })
        return {"ok": False, "code": "checkin_failed", "message": message, "stage": "checkin"}

    logging.getLogger("qr").info(
        "[QR] account=%s check-in success lesson=%s source=%s",
        account_id, lesson_id, QR_CHECKIN_SOURCE,
    )
    return {
        "ok": True,
        "lesson_id": lesson_id,
        "source": QR_CHECKIN_SOURCE,
        "already_running": already_running,
        "stage": "already_running" if already_running else "checkin_success",
    }


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


@app.get("/api/accounts")
async def list_accounts_route():
    return {
        "active_account_id": get_active_account_id(),
        "accounts": list_accounts_summary(),
    }


@app.put("/api/accounts/active")
async def set_active_route(body: SetActiveAccount):
    if body.account_id and not account_exists(body.account_id):
        raise HTTPException(status_code=404, detail="account not found")
    set_active_account_id(body.account_id)
    return {"ok": True, "active_account_id": get_active_account_id()}


@app.post("/api/accounts")
async def create_account_route(body: CreateAccountRequest):
    """Allocate an in-memory pending slot; returns its id. Not persisted until
    login completes."""
    domain = body.domain if body.domain in {o["key"] for o in DOMAIN_OPTIONS} else DEFAULT_DOMAIN
    aid = f"pending-{uuid.uuid4().hex[:8]}"
    with _pending_lock:
        _pending[aid] = {"domain": domain}
    return {"ok": True, "account_id": aid}


@app.delete("/api/accounts/{account_id}")
async def delete_account_route(account_id: str):
    with _pending_lock:
        removed_pending = _pending.pop(account_id, None)
    if removed_pending and not account_exists(account_id):
        return {"ok": True, "active_account_id": get_active_account_id()}
    _stop_monitor(account_id)
    _kick_subscribers(account_id)
    event_log.clear(account_id)
    delete_account(account_id)
    return {"ok": True, "active_account_id": get_active_account_id()}


@app.delete("/api/accounts/{account_id}/events")
async def clear_account_events(account_id: str):
    _require_account(account_id)
    event_log.clear(account_id)
    _publish_account_message(account_id, "events_cleared")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/logout")
async def logout_account_route(account_id: str):
    _require_account(account_id)
    _stop_monitor(account_id)
    update_account(account_id, {"sessionid": "", "user": {}, "course_list": []})
    return {"ok": True}


@app.get("/api/accounts/{account_id}/domain")
async def get_account_domain(account_id: str):
    _require_account(account_id)
    return {"domain": get_domain(account_id), "options": DOMAIN_OPTIONS}


@app.put("/api/accounts/{account_id}/domain")
async def set_account_domain(account_id: str, body: DomainPatch):
    _require_account(account_id)
    valid_keys = {o["key"] for o in DOMAIN_OPTIONS}
    if body.domain not in valid_keys:
        return {"ok": False, "error": "Invalid domain"}
    set_domain(account_id, body.domain)
    return {"ok": True, "domain": body.domain}


@app.get("/api/accounts/{account_id}/poll-interval")
async def get_account_poll_interval(account_id: str):
    _require_account(account_id)
    return {
        "poll_interval": get_poll_interval(account_id),
        "default": DEFAULT_POLL_INTERVAL,
        "min": MIN_POLL_INTERVAL,
        "max": MAX_POLL_INTERVAL,
    }


@app.put("/api/accounts/{account_id}/poll-interval")
async def set_account_poll_interval(account_id: str, body: PollIntervalBody):
    _require_account(account_id)
    clamped = set_poll_interval(account_id, body.poll_interval)
    m = _get_monitor(account_id)
    if m:
        m.wake()
    return {"ok": True, "poll_interval": clamped}


@app.get("/api/accounts/{account_id}/checkin-delay")
async def get_account_checkin_delay(account_id: str):
    _require_account(account_id)
    return {
        "checkin_delay": get_checkin_delay(account_id),
        "default": DEFAULT_CHECKIN_DELAY,
        "min": MIN_CHECKIN_DELAY,
        "max": MAX_CHECKIN_DELAY,
    }


@app.put("/api/accounts/{account_id}/checkin-delay")
@app.patch("/api/accounts/{account_id}/checkin-delay")
async def set_account_checkin_delay(account_id: str, body: CheckinDelayBody):
    _require_account(account_id)
    clamped = set_checkin_delay(account_id, body.checkin_delay)
    return {"ok": True, "checkin_delay": clamped}


@app.get("/api/accounts/{account_id}/pending-answers")
async def get_pending_answers(account_id: str):
    _require_account(account_id)
    monitor = _get_monitor(account_id)
    return {"answers": monitor.get_pending_answers() if monitor else []}


@app.post("/api/accounts/{account_id}/pending-answers/{answer_id}/confirm")
async def confirm_pending_answer(account_id: str, answer_id: str):
    _require_account(account_id)
    monitor = _get_monitor(account_id)
    if monitor is None or not monitor.resolve_pending_answer(answer_id, "confirm"):
        raise HTTPException(status_code=404, detail="pending answer not found")
    return {"ok": True, "answer_id": answer_id, "decision": "confirm"}


@app.post("/api/accounts/{account_id}/pending-answers/{answer_id}/skip")
@app.post("/api/accounts/{account_id}/pending-answers/{answer_id}/fallback")
async def skip_pending_answer(account_id: str, answer_id: str):
    _require_account(account_id)
    monitor = _get_monitor(account_id)
    if monitor is None or not monitor.resolve_pending_answer(answer_id, "skip"):
        raise HTTPException(status_code=404, detail="pending answer not found")
    return {"ok": True, "answer_id": answer_id, "decision": "skip"}


# ---------------------------------------------------------------------------
# Login — password (needs pre-created account_id)
# ---------------------------------------------------------------------------


@app.post("/api/accounts/{account_id}/auth/password-login")
async def password_login(account_id: str, body: PasswordLoginBody):
    acc = _pending_or_account(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="account not found")
    log = logging.getLogger("auth")
    domain = acc.get("domain") or DEFAULT_DOMAIN

    key_r = http_request("GET", api_url(domain, URL_GET_PUBLIC_KEY),
        headers={"Referer": f"https://{domain}/"},
    )
    pub_pem = key_r.json()["data"]["public_key"]
    cipher = PKCS1_v1_5.new(CryptoRSA.import_key(pub_pem))
    encrypted = base64.b64encode(cipher.encrypt(body.password.encode())).decode()

    login_url = api_url(domain, URL_PASSWORD_LOGIN)
    payload = {
        "name": body.phone,
        "pwd": encrypted,
        "type": "PP",
        "ticket": body.ticket,
        "randstr": body.randstr,
        "hcaptcha_token": "",
    }
    log.info("[%s] Sending password login to %s", account_id, login_url)

    r = http_request("POST", login_url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Referer": f"https://{domain}/",
        },
    )

    data = r.json()
    if not data.get("success"):
        return {"ok": False, "error": data.get("msg") or str(data)}

    sessionid = r.cookies["sessionid"]
    final_id = _finalize_login(account_id, sessionid)
    user = (get_account(final_id) or {}).get("user")
    return {"ok": True, "account_id": final_id, "user": user}


# ---------------------------------------------------------------------------
# Login — QR code WebSocket (creates a placeholder account on the fly)
# ---------------------------------------------------------------------------


@app.websocket("/ws/accounts/{account_id}/login")
async def ws_login(ws: WebSocket, account_id: str):
    await ws.accept()
    acc = _pending_or_account(account_id)
    if acc is None:
        await ws.send_json({"type": "error", "message": "account not found"})
        await ws.close()
        return
    domain = acc.get("domain") or DEFAULT_DOMAIN
    loop = asyncio.get_running_loop()
    login_queue: asyncio.Queue = asyncio.Queue()

    def on_open(wsapp):
        wsapp.send(json.dumps({
            "op": "requestlogin",
            "role": "web",
            "version": 1.4,
            "type": "qrcode",
            "from": "web",
        }))

    def on_message(wsapp, message):
        data = json.loads(message)
        op = data["op"]

        if op == "requestlogin":
            resp = http_request("GET", data["ticket"])
            img_b64 = base64.b64encode(resp.content).decode()
            content_type = resp.headers.get("Content-Type", "image/png").split(";")[0]
            data_url = "data:%s;base64,%s" % (content_type, img_b64)
            asyncio.run_coroutine_threadsafe(
                login_queue.put({"type": "qr", "url": data_url}), loop
            )

        elif op == "loginsuccess":
            r = http_request("POST", api_url(domain, URL_WEB_LOGIN),
                data=json.dumps({"UserID": data["UserID"], "Auth": data["Auth"]}),
            )
            sessionid = dict(r.cookies)["sessionid"]
            asyncio.run_coroutine_threadsafe(
                login_queue.put({"type": "success", "sessionid": sessionid}),
                loop,
            )
            wsapp.close()

    def on_error(wsapp, error):
        asyncio.run_coroutine_threadsafe(
            login_queue.put({"type": "error", "message": str(error)}), loop
        )

    def on_close(wsapp, *args):
        pass

    def qr_refresh_loop(wsapp_ref):
        count = 0
        while getattr(wsapp_ref, "_keep_running", True):
            if count >= 55:
                count = 0
                wsapp_ref.send(json.dumps({
                    "op": "requestlogin",
                    "role": "web",
                    "version": 1.4,
                    "type": "qrcode",
                    "from": "web",
                }))
            else:
                time.sleep(1)
                count += 1

    wsapp = websocket.WebSocketApp(
        url=api_url(domain, URL_WSS),
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    threading.Thread(target=wsapp.run_forever, daemon=True, name="login-ws").start()
    threading.Thread(target=qr_refresh_loop, args=(wsapp,), daemon=True, name="login-ws-refresh").start()

    msg = None
    try:
        while True:
            msg = await login_queue.get()
            if msg["type"] == "success":
                final_id = _finalize_login(account_id, msg["sessionid"])
                user = (get_account(final_id) or {}).get("user")
                await ws.send_json({"type": "success", "account_id": final_id, "user": user})
                break
            await ws.send_json(msg)
            if msg["type"] == "error":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass

    wsapp._keep_running = False
    wsapp.close()


# ---------------------------------------------------------------------------
# Per-account routes: courses / ai / pushdeer
# ---------------------------------------------------------------------------


@app.get("/api/accounts/{account_id}/courses/active")
async def get_active_courses(account_id: str):
    _require_account(account_id)
    m = _get_monitor(account_id)
    if not m:
        return {"lessons": []}
    return {"lessons": m.get_active_lessons()}


@app.get("/api/accounts/{account_id}/courses/all")
async def get_all_courses_route(account_id: str):
    _require_account(account_id)
    acc = get_account(account_id) or {}
    cached = acc.get("course_list", [])
    m = _get_monitor(account_id)
    active_map: dict = {}
    if m:
        for lesson in m.get_active_lessons():
            active_map[str(lesson["classroomid"])] = lesson["lessonid"]
    return [
        {
            "classroom_id": c["classroom_id"],
            "name": c["name"],
            "classroom_name": c["classroom_name"],
            "teacher_name": c["teacher_name"],
            "active": c["classroom_id"] in active_map,
        }
        for c in cached
    ]


@app.get("/api/accounts/{account_id}/courses/defaults")
async def get_course_defaults(account_id: str):
    _require_account(account_id)
    return DEFAULT_COURSE_CONFIG


@app.get("/api/accounts/{account_id}/courses/settings")
async def get_all_course_settings(account_id: str):
    _require_account(account_id)
    acc = get_account(account_id) or {}
    courses = acc.get("courses", {})
    return {cid: get_course_config(account_id, cid) for cid in courses}


@app.get("/api/accounts/{account_id}/courses/settings/{course_id}")
async def get_course_settings(account_id: str, course_id: str):
    _require_account(account_id)
    return get_course_config(account_id, course_id)


@app.put("/api/accounts/{account_id}/courses/settings/{course_id}")
async def update_course_settings(account_id: str, course_id: str, body: CourseConfig):
    _require_account(account_id)
    data = body.model_dump()
    if _course_enables_ai_without_key(account_id, course_id, data):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ai_api_key_required",
                "message": "Configure an AI API key before enabling AI answering",
            },
        )
    update_course_config(account_id, course_id, data)

    m = _get_monitor(account_id)
    if m:
        with m._lock:
            lesson = next(
                (l for l in m._active_lessons.values() if str(l.classroomid) == course_id),
                None,
            )
        if lesson:
            lesson.course_config.update(data)

    return {"ok": True, "course_id": course_id, "config": data}


# AI per-account ----------------------------------------------------------


@app.get("/api/accounts/{account_id}/ai/settings")
async def get_ai_settings(account_id: str):
    _require_account(account_id)
    cfg = get_ai_config(account_id)
    masked_keys = []
    for entry in cfg["keys"]:
        raw = entry["key"]
        masked = raw[:4] + "****" + raw[-4:] if len(raw) > 8 else "****"
        masked_keys.append({**entry, "key": masked})
    return {"keys": masked_keys, "active_key": cfg["active_key"], "fallback_keys": cfg.get("fallback_keys", True)}


@app.post("/api/accounts/{account_id}/ai/keys")
async def add_ai_key(account_id: str, body: AIKeyEntry):
    _require_account(account_id)
    cfg = get_ai_config(account_id)
    keys = cfg["keys"]
    keys.append(body.model_dump())
    active = cfg["active_key"]
    if active < 0:
        active = 0
    update_ai_config(account_id, {"keys": keys, "active_key": active})
    return {"ok": True, "index": len(keys) - 1}


@app.post("/api/accounts/{account_id}/ai/deepseek")
async def save_deepseek_key(account_id: str, body: DeepSeekKeyBody):
    """Save and activate one DeepSeek key without requiring model settings."""
    _require_account(account_id)
    cfg = get_ai_config(account_id)
    keys = list(cfg.get("keys", []))
    entry = {"name": "DeepSeek", "provider": "deepseek", "key": body.api_key}

    existing_index = next(
        (index for index, item in enumerate(keys) if item.get("provider") == "deepseek"),
        None,
    )
    if existing_index is None:
        keys.append(entry)
        active_index = len(keys) - 1
    else:
        keys[existing_index] = entry
        # Collapse duplicate legacy DeepSeek entries while preserving the
        # order of all other configured providers.
        keys = [
            item for index, item in enumerate(keys)
            if item.get("provider") != "deepseek" or index == existing_index
        ]
        active_index = next(index for index, item in enumerate(keys) if item is entry)

    update_ai_config(account_id, {"keys": keys, "active_key": active_index})
    return {"ok": True, "index": active_index, "provider": "deepseek", "active_key": active_index}


@app.delete("/api/accounts/{account_id}/ai/keys/{index}")
async def delete_ai_key(account_id: str, index: int):
    _require_account(account_id)
    cfg = get_ai_config(account_id)
    keys = cfg["keys"]
    if index < 0 or index >= len(keys):
        return {"ok": False, "error": "Invalid index"}
    keys.pop(index)
    active = cfg["active_key"]
    if active >= len(keys):
        active = len(keys) - 1
    elif active > index:
        active -= 1
    elif active == index:
        active = 0 if keys else -1
    update_ai_config(account_id, {"keys": keys, "active_key": active})
    return {"ok": True}


@app.put("/api/accounts/{account_id}/ai/active")
async def set_active_ai_key(account_id: str, body: AIActiveKey):
    _require_account(account_id)
    update_ai_config(account_id, {"active_key": body.active_key})
    return {"ok": True}


@app.put("/api/accounts/{account_id}/ai/fallback")
async def set_ai_fallback(account_id: str, body: dict):
    _require_account(account_id)
    update_ai_config(account_id, {"fallback_keys": bool(body.get("fallback_keys", True))})
    return {"ok": True}


# PushDeer per-account ----------------------------------------------------


@app.get("/api/accounts/{account_id}/pushdeer/settings")
async def get_pushdeer_settings(account_id: str):
    _require_account(account_id)
    cfg = get_pushdeer_config(account_id)
    masked_keys = []
    for entry in cfg.get("keys", []):
        raw = entry.get("push_key", "")
        masked = raw[:4] + "****" + raw[-4:] if len(raw) > 8 else ("****" if raw else "")
        masked_keys.append({
            "name": entry.get("name", ""),
            "endpoint": entry.get("endpoint", ""),
            "push_key": masked,
        })
    return {
        "keys": masked_keys,
        "active_key": cfg.get("active_key", -1),
        "language": cfg.get("language", "zh"),
    }


@app.post("/api/accounts/{account_id}/pushdeer/keys")
async def add_pushdeer_key(account_id: str, body: PushdeerKeyEntry):
    _require_account(account_id)
    cfg = get_pushdeer_config(account_id)
    keys = cfg.get("keys", [])
    keys.append(body.model_dump())
    active = cfg.get("active_key", -1)
    if active < 0:
        active = 0
    update_pushdeer_config(account_id, {"keys": keys, "active_key": active})
    return {"ok": True, "index": len(keys) - 1}


@app.delete("/api/accounts/{account_id}/pushdeer/keys/{index}")
async def delete_pushdeer_key(account_id: str, index: int):
    _require_account(account_id)
    cfg = get_pushdeer_config(account_id)
    keys = cfg.get("keys", [])
    if index < 0 or index >= len(keys):
        return {"ok": False, "error": "Invalid index"}
    keys.pop(index)
    active = cfg.get("active_key", -1)
    if active >= len(keys):
        active = len(keys) - 1
    elif active > index:
        active -= 1
    elif active == index:
        active = 0 if keys else -1
    update_pushdeer_config(account_id, {"keys": keys, "active_key": active})
    return {"ok": True}


@app.put("/api/accounts/{account_id}/pushdeer/active")
async def set_active_pushdeer_key(account_id: str, body: PushdeerActiveKey):
    _require_account(account_id)
    update_pushdeer_config(account_id, {"active_key": body.active_key})
    return {"ok": True}


@app.put("/api/accounts/{account_id}/pushdeer/language")
async def set_pushdeer_language(account_id: str, body: PushdeerLanguage):
    _require_account(account_id)
    lang = body.language if body.language in ("zh", "en") else "zh"
    update_pushdeer_config(account_id, {"language": lang})
    return {"ok": True}


@app.post("/api/accounts/{account_id}/pushdeer/test/{index}")
async def pushdeer_test(account_id: str, index: int):
    _require_account(account_id)
    cfg = get_pushdeer_config(account_id)
    keys = cfg.get("keys", [])
    if index < 0 or index >= len(keys):
        return {"ok": False, "message": "Invalid index"}
    # Temporarily flip the active key to `index` so send_liveness targets the
    # row the user clicked, then restore it.
    original_active = cfg.get("active_key", -1)
    try:
        if original_active != index:
            update_pushdeer_config(account_id, {"active_key": index})
        ok, msg = pushdeer.send_liveness(account_id)
    finally:
        if original_active != index:
            update_pushdeer_config(account_id, {"active_key": original_active})
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------------------
# Events WebSocket — per-account subscription
# ---------------------------------------------------------------------------


@app.websocket("/ws/accounts/{account_id}/events")
async def ws_events(ws: WebSocket, account_id: str):
    await ws.accept()
    if not account_exists(account_id):
        await ws.send_json({"type": "error", "message": "account not found"})
        await ws.close()
        return

    client_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    with _subscribers_lock:
        _subscribers.setdefault(account_id, set()).add(client_queue)
    hb_task: Optional[asyncio.Task] = None

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            await ws.send_json({"type": "heartbeat"})

    try:
        history = event_log.load_recent(account_id, 50)
        if history:
            await ws.send_json({"type": "history", "events": history})

        hb_task = asyncio.create_task(heartbeat())

        while True:
            event = await client_queue.get()
            await ws.send_json(event)
            if event.get("type") == "account_closed":
                break
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    finally:
        if hb_task is not None:
            hb_task.cancel()
        with _subscribers_lock:
            bucket = _subscribers.get(account_id)
            if bucket:
                bucket.discard(client_queue)
                if not bucket:
                    _subscribers.pop(account_id, None)


# ---------------------------------------------------------------------------
# Static file serving (production)
# ---------------------------------------------------------------------------


def _get_static_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "static"
    return Path(__file__).resolve().parent / "static"


_STATIC_DIR = _get_static_dir()

if _STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file = _STATIC_DIR / full_path
        if file.is_file():
            return FileResponse(file)
        return FileResponse(_STATIC_DIR / "index.html")
