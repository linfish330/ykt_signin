"""RainClassroom check-in primitives.

This module deliberately contains only the server-side QR scan and classroom
check-in protocol.  It does not decode images or attempt to bypass Yuketang's
normal ``/api/v3/app/scan`` validation.
"""

from dataclasses import dataclass
import json
import logging
from typing import Any, Optional

from config import (
    api_url,
    get_account,
    get_domain,
    get_sessionid,
    make_headers,
    validate_checkin_source,
    http_request,
)

logger = logging.getLogger(__name__)

URL_SCAN = "https://{domain}/api/v3/app/scan"
URL_CHECKIN = "https://{domain}/api/v3/lesson/checkin"


class CheckinError(Exception):
    """A safe, structured error suitable for an API response."""

    def __init__(self, code: Any, message: str, *, stage: str) -> None:
        self.code = code
        self.message = message
        self.stage = stage
        super().__init__(message)


@dataclass(frozen=True)
class ScanResult:
    lesson_id: Any
    code: Any = 0
    message: str = ""


@dataclass(frozen=True)
class CheckinResult:
    lesson_id: Any
    source: int
    lesson_token: str
    set_auth: str
    code: Any = 0
    message: str = ""


class RainClassroomClient:
    """Small account-scoped client for the check-in protocol.

    ``domain`` always comes from the selected account.  The host in a pasted
    QR value is sent as data to Yuketang and is never used as the request host.
    """

    def __init__(self, account_id: str, domain: str, sessionid: str) -> None:
        if not domain or not str(domain).strip():
            raise CheckinError("domain_unavailable", "account domain is unavailable", stage="setup")
        if not sessionid:
            raise CheckinError("not_authenticated", "account session has expired or is missing", stage="setup")
        self.account_id = str(account_id)
        self.domain = str(domain).strip()
        self.sessionid = sessionid
        self.headers = make_headers(self.domain, sessionid)
        self._last_response_headers: Any = {}

    @classmethod
    def for_account(cls, account_id: str) -> "RainClassroomClient":
        if get_account(account_id) is None:
            raise CheckinError("account_not_found", "account not found", stage="setup")
        return cls(account_id, get_domain(account_id), get_sessionid(account_id))

    @staticmethod
    def _read_json(response: Any, *, stage: str) -> dict:
        try:
            result = response.json()
        except Exception as exc:  # noqa: BLE001
            raise CheckinError("invalid_json", "Yuketang returned invalid JSON", stage=stage) from exc
        if not isinstance(result, dict):
            raise CheckinError("invalid_json", "Yuketang returned an invalid response", stage=stage)
        return result

    def _post(self, template: str, payload: dict, *, stage: str) -> dict:
        try:
            request_headers = dict(self.headers)
            request_headers["Content-Type"] = "application/json"
            response = http_request(
                "POST",
                api_url(self.domain, template),
                headers=request_headers,
                data=json.dumps(payload),
                retries=3,
                timeout=10,
            )
        except CheckinError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Do not include request headers, cookies, or the pasted QR value
            # in the exception or log message.
            raise CheckinError("network_error", "request to Yuketang failed", stage=stage) from exc
        self._last_response_headers = getattr(response, "headers", {}) or {}
        return self._read_json(response, stage=stage)

    def scan_qr_code(self, qr_url: str) -> ScanResult:
        value = qr_url.strip() if isinstance(qr_url, str) else ""
        if not value:
            raise CheckinError("invalid_qr_url", "QR code content cannot be empty", stage="scan")

        result = self._post(URL_SCAN, {"url": value}, stage="scan")
        code = result.get("code")
        if code != 0:
            raise CheckinError(code if code is not None else "scan_failed", result.get("msg") or "QR scan failed", stage="scan")

        data = result.get("data")
        lesson_id = data.get("value") if isinstance(data, dict) else None
        if lesson_id is None or (isinstance(lesson_id, str) and not lesson_id.strip()):
            raise CheckinError("missing_lesson_id", "QR scan response did not include lessonId", stage="scan")

        logger.info("[QR] account=%s scan success lesson=%s", self.account_id, lesson_id)
        return ScanResult(lesson_id=lesson_id, code=code, message=result.get("msg", ""))

    # Short alias for callers that prefer the protocol name.
    scan_qr = scan_qr_code

    def checkin(
        self,
        lesson_id: Any,
        source: int,
        *,
        join_if_not_in: bool = False,
    ) -> CheckinResult:
        try:
            validated_source = validate_checkin_source(source)
        except ValueError as exc:
            raise CheckinError("invalid_source", "unsupported check-in source", stage="checkin") from exc

        payload: dict[str, Any] = {"source": validated_source, "lessonId": lesson_id}
        # The reference client opts into this only for its explicit classroom
        # entry path.  Monitor keeps the legacy payload; QR entry opts in via
        # Lesson's explicit join_if_not_in argument.
        if join_if_not_in:
            payload["joinIfNotIn"] = True

        result = self._post(URL_CHECKIN, payload, stage="checkin")
        code = result.get("code")
        if code != 0:
            raise CheckinError(code if code is not None else "checkin_failed", result.get("msg") or "check-in failed", stage="checkin")

        raw_set_auth = self._get_header("Set-Auth")
        data = result.get("data")
        lesson_token = data.get("lessonToken") if isinstance(data, dict) else None
        if not raw_set_auth:
            raise CheckinError("missing_set_auth", "check-in response did not include Set-Auth", stage="checkin")
        if not isinstance(lesson_token, str) or not lesson_token:
            raise CheckinError("missing_lesson_token", "check-in response did not include lessonToken", stage="checkin")

        self.headers["Authorization"] = "Bearer %s" % raw_set_auth
        return CheckinResult(
            lesson_id=lesson_id,
            source=validated_source,
            lesson_token=lesson_token,
            set_auth=raw_set_auth,
            code=code,
            message=result.get("msg", ""),
        )

    def _get_header(self, name: str) -> Optional[str]:
        # requests uses a case-insensitive mapping; this fallback also keeps
        # the client friendly to tiny response fakes used in tests.
        headers = self._last_response_headers if hasattr(self, "_last_response_headers") else {}
        try:
            value = headers.get(name) or headers.get(name.lower())
            if value:
                return str(value)
            for key, candidate in headers.items():
                if str(key).lower() == name.lower() and candidate:
                    return str(candidate)
        except (AttributeError, TypeError):
            pass
        return None
