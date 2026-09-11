import copy
import json
import logging
import os
import re
import sys
import threading
import time as _time
from pathlib import Path
from typing import Any, Optional, Union

# Bypass system proxy for Yuketang and ModelScope domains.
_NO_PROXY_DOMAINS = ",".join([
    "pro.yuketang.cn",
    "www.yuketang.cn",
    "changjiang.yuketang.cn",
    "huanghe.yuketang.cn",
    "api-inference.modelscope.cn",
])
_existing = os.environ.get("NO_PROXY", "")
os.environ["NO_PROXY"] = f"{_existing},{_NO_PROXY_DOMAINS}" if _existing else _NO_PROXY_DOMAINS

import requests


def _resolve_store_dir() -> Path:
    # 1. Explicit override — used by Docker (set in Dockerfile), CI, multi-instance.
    env_path = os.environ.get("YUKETANG_STORE_DIR")
    if env_path:
        return Path(env_path).expanduser()
    # 2. PyInstaller / frozen binary — OS-conventional per-user data dir.
    #    macOS:   ~/Library/Application Support/Yuketang Helper/
    #    Linux:   ~/.local/share/Yuketang Helper/
    #    Windows: %LOCALAPPDATA%\Yuketang Helper\
    if getattr(sys, "frozen", False):
        from platformdirs import user_data_dir
        return Path(user_data_dir("Yuketang Helper", appauthor=False))
    # 3. Python source mode — keep store next to the repo for hackability.
    return Path(__file__).resolve().parent.parent / "store"


STORE_DIR = _resolve_store_dir()
STORE_DIR.mkdir(parents=True, exist_ok=True)

_CONFIG_PATH = STORE_DIR / "config.json"

DEFAULT_COURSE_CONFIG: dict = {
    "type1": "ai",
    "type2": "ai",
    "type3": "ai",
    "type4": "off",
    "type5": "ai",
    "course_enabled": True,
    "answer_last5s": True,
    "auto_danmu": True,
    "auto_redpacket": True,
    "danmu_threshold": 3,
    # None means inherit the per-account default.  Keeping the value absent
    # in older files is also supported by get_course_config().
    "checkin_source": None,
    "notification": {
        "enabled": False,
        "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True,
    },
    "voice_notification": {
        "enabled": False,
        "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True,
    },
    "pushdeer_notification": {
        "enabled": False,
        "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True,
    },
}

ANSWER_MODES = frozenset({"ai", "random", "off"})


def normalize_answer_mode(value: Any, default: str = "off") -> str:
    """Keep persisted course settings inside the three supported modes."""
    if isinstance(value, str) and value in ANSWER_MODES:
        return str(value)
    # Migrate the previous blank-answer option to the new random policy.
    if value == "blank":
        return "random"
    return default if default in ANSWER_MODES else "off"

# These values are observed in public Yuketang clients/community projects,
# not an official public enum.  Keep the wire values centralized so the
# monitor, manual QR flow, and API validation cannot drift apart.
CHECKIN_SOURCE_OPTIONS = [
    {"value": 21, "label": "QR code", "label_zh": "二维码"},
    {"value": 23, "label": "APP classroom button", "label_zh": "APP 点击课堂"},
    {"value": 5, "label": "WeChat / Mini Program", "label_zh": "微信/小程序"},
    {"value": 14, "label": "PC / Web", "label_zh": "PC / Web"},
    {"value": 22, "label": "Passcode", "label_zh": "暗号"},
    {"value": 1, "label": "WeChat / Scan QR Code", "label_zh": "微信/扫二维码"},
]
CHECKIN_SOURCE_VALUES = frozenset(option["value"] for option in CHECKIN_SOURCE_OPTIONS)
DEFAULT_CHECKIN_SOURCE = 21
QR_CHECKIN_SOURCE = 21


def validate_checkin_source(value: Any) -> int:
    """Return a valid wire value or raise ValueError for API callers."""
    if isinstance(value, bool):
        raise ValueError("checkin_source must be an integer source value")
    try:
        source = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("checkin_source must be an integer source value") from exc
    if source not in CHECKIN_SOURCE_VALUES:
        raise ValueError("unsupported checkin_source")
    return source

DEFAULT_AI_CONFIG: dict = {"keys": [], "active_key": -1, "fallback_keys": True}

DEFAULT_PUSHDEER_CONFIG: dict = {"keys": [], "active_key": -1, "language": "zh"}

# Seconds between polls for active lessons. Configurable per account.
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600
DEFAULT_AUTO_CHECKIN = False
AUTO_CHECKIN_MODES = ("on", "scheduled", "off")
DEFAULT_AUTO_CHECKIN_MODE = "off"
DEFAULT_AUTO_CHECKIN_TIME = "08:00"
DEFAULT_AI_ANSWERING_ENABLED = True
DEFAULT_AI_ANSWERING_MODE = "ai"
DEFAULT_CHECKIN_DELAY = 60
MIN_CHECKIN_DELAY = 0
MAX_CHECKIN_DELAY = 300

DOMAIN_OPTIONS = [
    {"key": "www.yuketang.cn", "label": "Yuketang", "label_zh": "雨课堂"},
    {"key": "pro.yuketang.cn", "label": "Hetang Yuketang", "label_zh": "荷塘雨课堂"},
    {"key": "changjiang.yuketang.cn", "label": "Changjiang Yuketang", "label_zh": "长江雨课堂"},
    {"key": "huanghe.yuketang.cn", "label": "Huanghe Yuketang", "label_zh": "黄河雨课堂"},
]

DEFAULT_DOMAIN = "www.yuketang.cn"


def new_empty_account(domain: str = DEFAULT_DOMAIN) -> dict:
    return {
        "name": "",
        "sessionid": "",
        "domain": domain,
        "user": {},
        "course_list": [],
        "courses": {},
        "ai": copy.deepcopy(DEFAULT_AI_CONFIG),
        "pushdeer": copy.deepcopy(DEFAULT_PUSHDEER_CONFIG),
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "checkin_delay": DEFAULT_CHECKIN_DELAY,
        "auto_checkin": DEFAULT_AUTO_CHECKIN,
        "auto_checkin_mode": DEFAULT_AUTO_CHECKIN_MODE,
        "auto_checkin_time": DEFAULT_AUTO_CHECKIN_TIME,
        "ai_answering_enabled": DEFAULT_AI_ANSWERING_ENABLED,
        "ai_answering_mode": DEFAULT_AI_ANSWERING_MODE,
        "checkin_source": DEFAULT_CHECKIN_SOURCE,
    }


def get_poll_interval(account_id: str) -> int:
    acc = get_account(account_id) or {}
    try:
        v = int(acc.get("poll_interval", DEFAULT_POLL_INTERVAL))
    except (TypeError, ValueError):
        v = DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, v))


def set_poll_interval(account_id: str, seconds: int) -> int:
    clamped = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, int(seconds)))
    update_account(account_id, {"poll_interval": clamped})
    return clamped


def get_checkin_delay(account_id: str) -> int:
    """Get the account-level delay before an automatic check-in."""
    acc = get_account(account_id) or {}
    try:
        value = int(acc.get("checkin_delay", DEFAULT_CHECKIN_DELAY))
    except (TypeError, ValueError):
        value = DEFAULT_CHECKIN_DELAY
    return max(MIN_CHECKIN_DELAY, min(MAX_CHECKIN_DELAY, value))


def set_checkin_delay(account_id: str, seconds: int) -> int:
    clamped = max(MIN_CHECKIN_DELAY, min(MAX_CHECKIN_DELAY, int(seconds)))
    update_account(account_id, {"checkin_delay": clamped})
    return clamped


def get_auto_checkin_mode(account_id: str) -> str:
    """Get the account-level automatic check-in mode with legacy migration."""
    acc = get_account(account_id) or {}
    value = acc.get("auto_checkin_mode")
    if isinstance(value, str) and value in AUTO_CHECKIN_MODES:
        return value
    legacy = acc.get("auto_checkin")
    if isinstance(legacy, bool):
        return "on" if legacy else "off"
    return DEFAULT_AUTO_CHECKIN_MODE


def validate_auto_checkin_time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("auto_checkin_time must use HH:MM format")
    value = value.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("auto_checkin_time must use HH:MM format")
    return value


def get_auto_checkin_time(account_id: str) -> str:
    acc = get_account(account_id) or {}
    try:
        return validate_auto_checkin_time(acc.get("auto_checkin_time", DEFAULT_AUTO_CHECKIN_TIME))
    except ValueError:
        return DEFAULT_AUTO_CHECKIN_TIME


def set_auto_checkin_mode(
    account_id: str,
    mode: str,
    start_time: Optional[str] = None,
) -> tuple[str, str]:
    if mode not in AUTO_CHECKIN_MODES:
        raise ValueError("auto_checkin_mode must be on, scheduled, or off")
    schedule_time = get_auto_checkin_time(account_id) if start_time is None else validate_auto_checkin_time(start_time)
    update_account(account_id, {
        "auto_checkin_mode": mode,
        "auto_checkin_time": schedule_time,
        # Keep the legacy field enabled for the scheduled mode so older
        # clients do not accidentally treat it as permanently disabled.
        "auto_checkin": mode != "off",
    })
    return mode, schedule_time


def get_auto_checkin(account_id: str) -> bool:
    """Return whether automatic check-in is active at the current local time."""
    mode = get_auto_checkin_mode(account_id)
    if mode == "off":
        return False
    if mode == "scheduled":
        return _time.strftime("%H:%M") >= get_auto_checkin_time(account_id)
    return True


def set_auto_checkin(account_id: str, enabled: bool) -> bool:
    """Backward-compatible boolean setter for older API clients."""
    if not isinstance(enabled, bool):
        raise ValueError("auto_checkin must be a boolean")
    set_auto_checkin_mode(account_id, "on" if enabled else "off")
    return enabled


def get_ai_answering_mode(account_id: str) -> str:
    """Get the account-level answer mode, migrating the old boolean switch."""
    acc = get_account(account_id) or {}
    value = acc.get("ai_answering_mode")
    if isinstance(value, str) and value in ANSWER_MODES:
        return str(value)
    legacy = acc.get("ai_answering_enabled")
    if isinstance(legacy, bool):
        return "ai" if legacy else "off"
    return DEFAULT_AI_ANSWERING_MODE


def set_ai_answering_mode(account_id: str, mode: str) -> str:
    if mode not in ANSWER_MODES:
        raise ValueError("ai_answering_mode must be ai, random, or off")
    update_account(account_id, {
        "ai_answering_mode": mode,
        # Keep the legacy field in sync for older clients/config readers.
        "ai_answering_enabled": mode == "ai",
    })
    return mode


def get_ai_answering_enabled(account_id: str) -> bool:
    """Backward-compatible boolean view of the global answer mode."""
    return get_ai_answering_mode(account_id) == "ai"


def set_ai_answering_enabled(account_id: str, enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("ai_answering_enabled must be a boolean")
    set_ai_answering_mode(account_id, "ai" if enabled else "off")
    return enabled


def get_checkin_source(account_id: str) -> int:
    """Get an account default, tolerating old or malformed config files."""
    acc = get_account(account_id) or {}
    try:
        return validate_checkin_source(acc.get("checkin_source", DEFAULT_CHECKIN_SOURCE))
    except ValueError:
        return DEFAULT_CHECKIN_SOURCE


def set_checkin_source(account_id: str, source: int) -> int:
    validated = validate_checkin_source(source)
    update_account(account_id, {"checkin_source": validated})
    return validated


_EMPTY_CONFIG = {"active_account_id": None, "accounts": {}}


# ---------------------------------------------------------------------------
# Core load / save
# ---------------------------------------------------------------------------


# Serialize every read-modify-write transaction against store/config.json.
# Using RLock so helpers that call other locked helpers don't deadlock.
_config_lock = threading.RLock()


def get_config() -> dict:
    with _config_lock:
        if not _CONFIG_PATH.exists():
            save_config(dict(_EMPTY_CONFIG))
            return dict(_EMPTY_CONFIG)
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)


def save_config(cfg: dict) -> None:
    # Atomic write: dump to *.tmp then os.replace so concurrent readers never
    # see a half-written file.
    tmp_path = _CONFIG_PATH.with_suffix(".json.tmp")
    with _config_lock:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _CONFIG_PATH)


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


def get_active_account_id() -> Optional[str]:
    aid = get_config().get("active_account_id")
    return str(aid) if aid else None


def set_active_account_id(account_id: Optional[str]) -> None:
    with _config_lock:
        cfg = get_config()
        cfg["active_account_id"] = str(account_id) if account_id else None
        save_config(cfg)


def list_account_ids() -> list:
    return [str(k) for k in get_config().get("accounts", {}).keys()]


def list_accounts_summary() -> list:
    """UI-friendly summary: no secrets."""
    out = []
    accs = get_config().get("accounts", {})
    for aid, acc in accs.items():
        user = acc.get("user") or {}
        out.append({
            "id": str(aid),
            "name": user.get("name") or acc.get("name") or str(aid),
            "avatar": user.get("avatar") or "",
            "domain": acc.get("domain") or DEFAULT_DOMAIN,
            "logged_in": bool(acc.get("sessionid")),
        })
    return out


def get_account(account_id: str) -> Optional[dict]:
    return get_config().get("accounts", {}).get(str(account_id))


def account_exists(account_id: str) -> bool:
    return str(account_id) in get_config().get("accounts", {})


def upsert_account(account_id: str, data: dict) -> None:
    with _config_lock:
        cfg = get_config()
        cfg.setdefault("accounts", {})[str(account_id)] = data
        save_config(cfg)


def update_account(account_id: str, patch: dict) -> None:
    with _config_lock:
        cfg = get_config()
        acc = cfg.setdefault("accounts", {}).setdefault(str(account_id), new_empty_account())
        acc.update(patch)
        save_config(cfg)


def delete_account(account_id: str) -> None:
    with _config_lock:
        cfg = get_config()
        accs = cfg.setdefault("accounts", {})
        accs.pop(str(account_id), None)
        if cfg.get("active_account_id") == str(account_id):
            cfg["active_account_id"] = next(iter(accs.keys()), None)
        save_config(cfg)


# ---------------------------------------------------------------------------
# Per-account getters (require account_id)
# ---------------------------------------------------------------------------


def get_domain(account_id: str) -> str:
    acc = get_account(account_id) or {}
    return acc.get("domain") or DEFAULT_DOMAIN


def set_domain(account_id: str, domain: str) -> None:
    update_account(account_id, {"domain": domain})


def get_sessionid(account_id: str) -> str:
    acc = get_account(account_id) or {}
    return acc.get("sessionid", "")


def get_course_config(account_id: str, course_id: str) -> dict:
    acc = get_account(account_id) or {}
    course = acc.get("courses", {}).get(str(course_id), {})
    merged = dict(course)
    for key, value in DEFAULT_COURSE_CONFIG.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict):
            m = dict(value)
            m.update(merged[key])
            merged[key] = m
    for type_key in ("type1", "type2", "type3", "type4", "type5"):
        merged[type_key] = normalize_answer_mode(merged.get(type_key), DEFAULT_COURSE_CONFIG[type_key])
    return merged


def resolve_checkin_source(
    account_id: str,
    course_id: Optional[Union[str, int]] = None,
    course_config: Optional[dict] = None,
    *,
    force_qr_source: bool = False,
    source_override: Any = None,
) -> int:
    """Resolve the source for one classroom without leaking wire constants.

    ``force_qr_source`` is intentionally explicit: a QR scan always enters
    through source 21, even when the account default is another source.
    Invalid persisted course values are treated as inherit so a stale config
    cannot stop Monitor from starting.
    """
    if force_qr_source:
        return QR_CHECKIN_SOURCE
    if source_override is not None:
        return validate_checkin_source(source_override)

    if course_config is None and course_id is not None:
        course_config = get_course_config(account_id, str(course_id))
    course_value = (course_config or {}).get("checkin_source")
    if course_value not in (None, "", "inherit"):
        try:
            return validate_checkin_source(course_value)
        except ValueError:
            pass
    return get_checkin_source(account_id)


def update_course_config(account_id: str, course_id: str, data: dict) -> None:
    data = dict(data)
    if "checkin_source" in data:
        source = data["checkin_source"]
        if source in (None, "", "inherit"):
            data["checkin_source"] = None
        else:
            data["checkin_source"] = validate_checkin_source(source)
    with _config_lock:
        cfg = get_config()
        acc = cfg.setdefault("accounts", {}).setdefault(str(account_id), new_empty_account())
        acc.setdefault("courses", {}).setdefault(str(course_id), {}).update(data)
        save_config(cfg)


def get_ai_config(account_id: str) -> dict:
    acc = get_account(account_id) or {}
    ai = acc.get("ai", {})
    merged = copy.deepcopy(DEFAULT_AI_CONFIG)
    merged.update(ai)
    return merged


def update_ai_config(account_id: str, data: dict) -> None:
    with _config_lock:
        cfg = get_config()
        acc = cfg.setdefault("accounts", {}).setdefault(str(account_id), new_empty_account())
        ai = acc.setdefault("ai", copy.deepcopy(DEFAULT_AI_CONFIG))
        ai.update(data)
        save_config(cfg)


def get_pushdeer_config(account_id: str) -> dict:
    acc = get_account(account_id) or {}
    pd = acc.get("pushdeer", {})
    merged = copy.deepcopy(DEFAULT_PUSHDEER_CONFIG)
    merged.update(pd)
    return merged


def update_pushdeer_config(account_id: str, data: dict) -> None:
    with _config_lock:
        cfg = get_config()
        acc = cfg.setdefault("accounts", {}).setdefault(str(account_id), new_empty_account())
        pd = acc.setdefault("pushdeer", copy.deepcopy(DEFAULT_PUSHDEER_CONFIG))
        pd.update(data)
        save_config(cfg)


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------


def make_headers(domain: str, sessionid: str) -> dict:
    return {
        "Cookie": "sessionid=%s" % sessionid,
        "Referer": "https://%s/" % domain,
        "xt-agent": "web",
    }


def api_url(domain: str, template: str, **kwargs: Any) -> str:
    return template.format(domain=domain, **kwargs)


_http_log = logging.getLogger("http")

_DEFAULT_PROXIES = {"http": None, "https": None}
_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:104.0) Gecko/20100101 Firefox/104.0"


def http_request(
    method: str,
    url: str,
    retries: int = 10,
    timeout: int = 5,
    **kwargs: Any,
) -> requests.Response:
    kwargs.setdefault("proxies", _DEFAULT_PROXIES)
    kwargs.setdefault("timeout", timeout)
    headers = kwargs.setdefault("headers", {})
    headers.setdefault("User-Agent", _DEFAULT_UA)

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code < 500:
                return r
            _http_log.warning("HTTP %s %s → %s (attempt %d/%d)", method, url, r.status_code, attempt, retries)
        except requests.RequestException as e:
            _http_log.warning("HTTP %s %s failed: %s (attempt %d/%d)", method, url, e, attempt, retries)
            last_exc = e
        if attempt < retries:
            _time.sleep(min(attempt, 3))

    if last_exc:
        raise last_exc
    return r  # type: ignore[possibly-undefined]
