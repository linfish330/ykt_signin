import json
import logging
import random
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Union

import websocket

from ai_provider import create_provider
from checkin import CheckinError, CheckinResult, RainClassroomClient
from config import (
    api_url,
    get_account,
    get_ai_answering_mode,
    get_checkin_delay,
    get_ai_config,
    http_request,
    resolve_checkin_source,
)

logger = logging.getLogger(__name__)

# API URLs
URL_WSS = "wss://{domain}/wsapp/"
URL_BASIC_INFO = "https://{domain}/api/v3/lesson/basic-info"
URL_DANMU_SEND = "https://{domain}/api/v3/lesson/danmu/send"
URL_PROBLEM_ANSWER = "https://{domain}/api/v3/lesson/problem/answer"
URL_PRESENTATION_FETCH = "https://{domain}/api/v3/lesson/presentation/fetch?presentation_id={presentation_id}"
URL_REDENVELOPE_PREPARE = "https://{domain}/api/v3/lesson/redenvelope/prepare"

# Keep enough time for the random fallback to be submitted before the deadline
# when the user does not confirm an AI/random candidate.
ANSWER_AUTO_FALLBACK_SECONDS = 8


class Lesson:
    def __init__(
        self,
        account_id: str,
        lesson_data: dict,
        sessionid: str,
        domain: str,
        course_config: dict,
        on_event: Callable[[str, dict], None],
        checkin_source: Optional[int] = None,
        join_if_not_in: bool = False,
    ):
        self.account_id = account_id
        self.lessonid: Any = lesson_data["lessonid"]
        self.lessonname: str = lesson_data["lessonname"]
        self.classroomid: Any = lesson_data["classroomid"]
        self.sessionid = sessionid
        self.domain = domain
        self.course_config = course_config
        self.on_event = on_event
        self.checkin_source = checkin_source if checkin_source is not None else resolve_checkin_source(
            account_id, self.classroomid, course_config
        )
        self.join_if_not_in = join_if_not_in

        self.client = RainClassroomClient(account_id, domain, sessionid)
        self.headers = self.client.headers
        self.auth: Optional[str] = None
        self._checkin_result: Optional[CheckinResult] = None
        self.wsapp: Optional[websocket.WebSocketApp] = None
        self._running = False

        self.danmu_dict: Dict[str, List[float]] = {}
        self.sent_danmu_dict: Dict[str, float] = {}
        self.problems_ls: List[dict] = []

        self.user_uid: Optional[int] = None
        self.user_uname: Optional[str] = None
        self.teacher_name: Optional[str] = None
        self._stopped_externally = False
        self._lesson_ended = False
        self._pending_answers: Dict[str, dict] = {}
        self._pending_answers_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_lesson(self, *, checkin: bool = True) -> None:
        self._running = True
        if checkin:
            if not self._wait_for_checkin_delay():
                self._running = False
                return
            try:
                self._checkin()
            except Exception:
                self._running = False
                raise

        # Yuketang closes the WS every ~40-60s while class is still live.
        # Reconnect on a fixed 1s delay until external stop or `lessonfinished`.
        while not self._stopped_externally and not self._lesson_ended:
            self.wsapp = websocket.WebSocketApp(
                url=api_url(self.domain, URL_WSS),
                header=self.headers,
                on_open=self._on_open,
                on_message=self._on_message,
            )
            self.wsapp.run_forever(ping_interval=30, ping_timeout=10)
            if self._stopped_externally or self._lesson_ended:
                break
            logger.info("[%s][WS %s] disconnected, reconnecting in 1s", self.account_id, self.lessonname)
            time.sleep(1)

        self._running = False
        if not self._stopped_externally:
            self.on_event("lesson_end", {"lesson": self.lessonname, "lessonid": self.lessonid})

    def stop_lesson(self) -> None:
        self._stopped_externally = True
        self._running = False
        with self._pending_answers_lock:
            for pending in self._pending_answers.values():
                pending["decision"] = "fallback"
                pending["event"].set()
        if self.wsapp:
            self.wsapp.close()

    def send_danmu(self, content: str) -> None:
        payload = {
            "lessonId": self.lessonid,
            "target": "",
            "userName": "",
            "message": content,
            "extra": "",
            "requiredCensor": False,
            "wordCloud": True,
            "showStatus": True,
            "fromStart": "50",
        }
        r = http_request("POST", api_url(self.domain, URL_DANMU_SEND), headers=self.headers, data=json.dumps(payload))
        self.on_event("danmu", {
            "lesson": self.lessonname,
            "lessonid": self.lessonid,
            "content": content,
            "status": "success" if r.json()["code"] == 0 else "error",
        })

    def _grab_red_packet(self, red_envelope_id: int) -> None:
        payload = {
            "lessonId": self.lessonid,
            "redEnvelopeId": red_envelope_id,
        }
        r = http_request("POST", api_url(self.domain, URL_REDENVELOPE_PREPARE), headers=self.headers, data=json.dumps(payload))
        result = r.json()
        self.on_event("red_packet", {
            "lesson": self.lessonname,
            "lessonid": self.lessonid,
            "redEnvelopeId": red_envelope_id,
            "status": "success" if result.get("code") == 0 else "error",
            "message": result.get("msg", ""),
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wait_for_checkin_delay(self) -> bool:
        delay = get_checkin_delay(self.account_id)
        if delay <= 0:
            return self._running

        logger.info(
            "[%s] Delaying automatic check-in for lesson %s by %ss",
            self.account_id,
            self.lessonid,
            delay,
        )
        deadline = time.monotonic() + delay
        while self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.25, remaining))
        return False

    def checkin(self) -> CheckinResult:
        """Perform the unified check-in and retain auth for the WebSocket."""
        if self._checkin_result is not None:
            return self._checkin_result

        try:
            result = self.client.checkin(
                self.lessonid,
                self.checkin_source,
                join_if_not_in=self.join_if_not_in,
            )
        except CheckinError as exc:
            self.on_event("signin", {
                "lesson": self.lessonname,
                "lessonid": self.lessonid,
                "source": self.checkin_source,
                "status": "error",
                "code": exc.code,
                "message": exc.message,
            })
            raise

        self.auth = result.lesson_token

        acc = get_account(self.account_id) or {}
        user = acc.get("user") or {}
        self.user_uid = user.get("id")
        self.user_uname = user.get("name")

        # Metadata is helpful for manual QR entry but not required to keep the
        # WebSocket alive.  Treat this ancillary request as best effort.
        try:
            info_result = http_request(
                "GET",
                api_url(self.domain, URL_BASIC_INFO),
                headers=self.headers,
            )
            info_json = info_result.json()
            info = info_json.get("data", {}) if isinstance(info_json, dict) else {}
            self.teacher_name = (info.get("teacher") or {}).get("name")
            self.lessonname = info.get("lessonName") or info.get("courseName") or self.lessonname
            self.classroomid = info.get("classroomId") or info.get("classroom_id") or self.classroomid
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] lesson metadata fetch failed for lesson %s: %s", self.account_id, self.lessonid, exc)

        self._checkin_result = result
        self.on_event("signin", {
            "lesson": self.lessonname,
            "lessonid": self.lessonid,
            "source": result.source,
            "status": "success",
            "message": result.message,
        })
        return result

    def _checkin(self) -> None:
        # Kept as a compatibility seam for existing callers/tests.
        self.checkin()

    def _get_problems_from_presentation(self, presentation_id: Any) -> List[dict]:
        r = http_request("GET", api_url(self.domain, URL_PRESENTATION_FETCH, presentation_id=presentation_id), headers=self.headers)
        data = r.json()["data"]
        problems = []
        for slide in data.get("slides", []):
            if "problem" in slide:
                problem = slide["problem"]
                problem["_cover"] = slide.get("cover", "")
                problems.append(problem)
        return problems

    def _add_problems(self, problems: List[dict]) -> None:
        existing_ids = {p["problemId"] for p in self.problems_ls}
        for p in problems:
            if p["problemId"] not in existing_ids:
                self.problems_ls.append(p)
                existing_ids.add(p["problemId"])

    def _build_fallback_answer(self, problem: dict, problemtype: int):
        if problemtype in (4, 5):
            return "1", "random"
        if problemtype in (1, 2, 3):
            options = [opt["key"] for opt in problem.get("options", []) if opt.get("key")]
            if options:
                if problemtype == 1:
                    return [random.choice(options)], "random"
                if problemtype == 3:
                    max_count = min(len(options), max(1, int(problem.get("pollingCount", 1) or 1)))
                else:
                    max_count = len(options)
                count = random.randint(1, max_count)
                return random.sample(options, count), "random"
        # Unknown question types are not answered by the random mode.
        return None, "skip"

    def _ai_keys_to_try(self) -> list[tuple[str, str, str]]:
        ai_cfg = get_ai_config(self.account_id)
        keys = ai_cfg.get("keys", [])
        active = ai_cfg.get("active_key", -1)
        fallback = ai_cfg.get("fallback_keys", True)

        if fallback:
            ordered = []
            if 0 <= active < len(keys):
                ordered.append(keys[active])
            ordered.extend(entry for i, entry in enumerate(keys) if i != active)
        elif 0 <= active < len(keys):
            ordered = [keys[active]]
        else:
            ordered = []

        out = []
        for entry in ordered:
            provider_name = entry.get("provider", "")
            api_key = entry.get("key", "")
            key_name = entry.get("name") or provider_name or "unnamed"
            if api_key:
                out.append((provider_name, api_key, key_name))
        return out

    def _build_ai_answers(self, problem: dict, keys_to_try: Optional[list[tuple[str, str, str]]] = None) -> Union[list, str]:
        if keys_to_try is None:
            keys_to_try = self._ai_keys_to_try()
        if not keys_to_try:
            raise RuntimeError("No AI provider available")

        cover_url = problem.get("_cover", "")
        problemtype = problem["problemType"]
        last_error = None

        for provider_name, api_key, key_name in keys_to_try:
            provider = create_provider(provider_name, api_key)
            try:
                if problemtype in (4, 5):
                    return provider.answer_short(cover_url)
                if problemtype not in (1, 2, 3):
                    raise RuntimeError("unsupported problem type")
                option_keys = [opt["key"] for opt in problem["options"]]
                count = int(problem.get("pollingCount", 1) or 1) if problemtype == 3 else None
                return provider.answer_choice(cover_url, option_keys, problemtype, count)
            except Exception as e:
                logger.warning("[%s] AI call failed with key %r (%s), trying next: %s", self.account_id, key_name, provider_name, e)
                last_error = e

        raise RuntimeError("All AI providers failed") from last_error

    @staticmethod
    def _problem_review_payload(problem: dict, problemid: Any, problemtype: int) -> dict:
        content = ""
        for key in ("content", "question", "stem", "title", "text", "problemText"):
            value = problem.get(key)
            if isinstance(value, str) and value.strip():
                content = value.strip()
                break

        options = []
        for option in problem.get("options", []) or []:
            if not isinstance(option, dict):
                continue
            key = option.get("key", "")
            text = ""
            for text_key in ("text", "content", "value", "name", "label"):
                value = option.get(text_key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
            options.append({"key": str(key), "text": text})

        return {
            "problem_id": problemid,
            "problem_type": problemtype,
            "content": content,
            "cover_url": problem.get("_cover", "") if isinstance(problem.get("_cover", ""), str) else "",
            "options": options,
        }

    def _create_pending_answer(
        self,
        problem: dict,
        problemid: Any,
        problemtype: int,
        answer: Optional[Union[list, str]] = None,
        source: str = "off",
        answer_ready: bool = False,
    ) -> dict:
        answer_id = uuid.uuid4().hex
        payload = {
            "answer_id": answer_id,
            "lesson": self.lessonname,
            "lessonid": self.lessonid,
            "problemid": problemid,
            "problemtype": problemtype,
            "problem": self._problem_review_payload(problem, problemid, problemtype),
            "answer": answer,
            "source": source,
            "answer_ready": answer_ready,
            "auto_fallback": source in {"ai", "random"},
        }
        with self._pending_answers_lock:
            self._pending_answers[answer_id] = {
                "event": threading.Event(),
                "decision": None,
                "payload": payload,
            }
        return payload

    def _update_pending_answer(
        self,
        answer_id: str,
        answer: Optional[Union[list, str]],
        source: str,
        answer_ready: bool,
    ) -> bool:
        """Update a visible review popup when a candidate answer is ready."""
        with self._pending_answers_lock:
            pending = self._pending_answers.get(str(answer_id))
            if pending is None:
                return False
            if pending.get("decision") == "skip":
                return False
            payload = pending["payload"]
            payload.update({
                "answer": answer,
                "source": source,
                "answer_ready": answer_ready,
            })
            update = {
                "answer_id": payload["answer_id"],
                "answer": payload["answer"],
                "source": payload["source"],
                "answer_ready": payload["answer_ready"],
            }
        self.on_event("answer_updated", update)
        return True

    def get_pending_answers(self) -> list[dict]:
        with self._pending_answers_lock:
            return [dict(item["payload"]) for item in self._pending_answers.values()]

    def resolve_pending_answer(self, answer_id: str, decision: str) -> bool:
        # Keep the old fallback decision as an alias for clients that may have
        # an older dashboard open. It now means dismiss/skip, never submit.
        if decision == "fallback":
            decision = "skip"
        if decision not in {"confirm", "skip"}:
            return False
        with self._pending_answers_lock:
            pending = self._pending_answers.get(str(answer_id))
            if pending is None:
                return False
            pending["decision"] = decision
            pending["event"].set()
            return True

    def _wait_for_pending_answer(
        self,
        answer_id: str,
        limit: int,
        start_time: float,
        retain_on_timeout: bool = False,
        timeout_after: Optional[float] = None,
    ) -> tuple[Optional[str], Optional[dict]]:
        with self._pending_answers_lock:
            pending = self._pending_answers.get(answer_id)
        if pending is None:
            return None, None

        timeout = None
        if timeout_after is not None:
            timeout = max(0.0, timeout_after - (time.time() - start_time))
        elif limit > 0:
            timeout = max(0.0, limit - (time.time() - start_time))
        pending["event"].wait(timeout=timeout)

        with self._pending_answers_lock:
            current = self._pending_answers.get(answer_id)
            if current is not None and not (retain_on_timeout and current.get("decision") is None):
                current = self._pending_answers.pop(answer_id, None)
        if current is None:
            return None, None
        return current.get("decision"), dict(current["payload"])

    def _pop_pending_answer(self, answer_id: str) -> Optional[dict]:
        with self._pending_answers_lock:
            current = self._pending_answers.pop(str(answer_id), None)
        return dict(current["payload"]) if current else None

    def _begin_answer_review(
        self,
        problem: dict,
        problemid: Any,
        problemtype: int,
        source: str,
    ) -> dict:
        """Create the review state before doing any potentially slow answer work."""
        pending = self._create_pending_answer(problem, problemid, problemtype, source=source)
        # Emit a snapshot: the stored payload is intentionally mutated later by
        # ``answer_updated`` and must not retroactively change this event.
        self.on_event("answer_pending", dict(pending))
        return pending

    def _emit_answer_review_closed(self, pending: dict, resolved: dict, reason: str) -> None:
        self.on_event("answer_review_closed", {
            "answer_id": pending["answer_id"],
            "lesson": self.lessonname,
            "lessonid": self.lessonid,
            "problemid": resolved["problemid"],
            "problemtype": resolved["problemtype"],
            "reason": reason,
        })

    def _wait_for_answer_confirmation(self, pending: dict, limit: int, start_time: float) -> None:
        # Give the user the full review window, then apply the final fallback
        # policy when there are only eight seconds left and no decision was made.
        fallback_at = float(limit - ANSWER_AUTO_FALLBACK_SECONDS) if limit > 0 else None
        decision, resolved = self._wait_for_pending_answer(
            pending["answer_id"],
            limit,
            start_time,
            retain_on_timeout=True,
            timeout_after=fallback_at,
        )
        if resolved is None:
            return
        if not self._running:
            self._pop_pending_answer(pending["answer_id"])
            return
        answer_id = pending["answer_id"]

        if decision is None:
            if not resolved.get("auto_fallback", False):
                self._pop_pending_answer(answer_id)
                self._emit_answer_review_closed(pending, resolved, "expired")
                return

            problemtype = resolved.get("problemtype")
            try:
                problemtype = int(problemtype)
            except (TypeError, ValueError):
                problemtype = 0

            # For AI mode, preserve a ready AI answer for single-choice,
            # multiple-choice, and fill-in questions. Other question types
            # must switch to the random policy at the fallback boundary.
            use_ai_fallback = (
                resolved.get("source") == "ai"
                and resolved.get("answer_ready")
                and resolved.get("answer") is not None
                and problemtype in {1, 2, 4}
            )
            if use_ai_fallback:
                fallback_answer = resolved["answer"]
                fallback_source = "ai"
            elif resolved.get("source") == "random" and resolved.get("answer_ready") and resolved.get("answer") is not None:
                fallback_answer = resolved["answer"]
                fallback_source = "random"
            else:
                fallback_answer, fallback_source = self._build_fallback_answer(
                    resolved["problem"], resolved["problemtype"]
                )

            if fallback_answer is None:
                self._pop_pending_answer(answer_id)
                self._emit_answer_review_closed(pending, resolved, "no_answer")
                return

            if not self._update_pending_answer(answer_id, fallback_answer, fallback_source, True):
                # A skip may race with the eight-second fallback boundary.
                self._pop_pending_answer(answer_id)
                return
            resolved["answer"] = fallback_answer
            resolved["source"] = fallback_source
            resolved["answer_ready"] = True
            self._pop_pending_answer(answer_id)
            if not self._wait_for_delay(start_time, limit):
                self._emit_answer_review_closed(pending, resolved, "expired")
                return
            self._emit_answer_review_closed(pending, resolved, "auto_fallback")
            self._submit_answer(
                resolved["problemid"],
                resolved["problemtype"],
                resolved["answer"],
                fallback_source,
                answer_id,
            )
            return

        if decision != "confirm":
            self._emit_answer_review_closed(pending, resolved, "skipped" if decision == "skip" else "expired")
            return
        if (
            resolved.get("source") == "off"
            or not resolved.get("answer_ready", False)
            or resolved.get("answer") is None
        ):
            self._emit_answer_review_closed(pending, resolved, "no_answer")
            return
        if not self._wait_for_delay(start_time, limit):
            self._emit_answer_review_closed(pending, resolved, "expired")
            return
        self._submit_answer(
            resolved["problemid"],
            resolved["problemtype"],
            resolved["answer"],
            resolved["source"],
            answer_id,
        )

    # ------------------------------------------------------------------
    # Answer submission
    # ------------------------------------------------------------------
    #
    # Submission timing depends on mode and the `answer_last5s` toggle.
    #
    #   Random mode:
    #     - Always show the generated candidate and wait for confirmation.
    #     - Once confirmed, last5s controls the actual submit timing.
    #
    #   AI mode (see `_compute_ai_window`):
    #     - last5s ON  + deadline → wait for AI up to the last-5s window,
    #       then show the AI candidate for confirmation. If AI doesn't return,
    #       show a random candidate for confirmation.
    #     - last5s OFF + deadline → show the AI candidate as soon as it returns;
    #       cap the AI wait at `limit - 1s` for a random candidate.
    #     - No deadline → wait indefinitely for AI (provider has its own
    #       request timeout).

    def _submit_answer(
        self,
        problemid: Any,
        problemtype: int,
        real_answer: Any,
        source: str,
        pending_answer_id: Optional[str] = None,
    ) -> None:
        if problemtype == 5:
            payload_result = {"content": real_answer, "pics": [{"pic": "", "thumb": ""}]}
        else:
            payload_result = real_answer
        payload = {
            "problemId": problemid,
            "problemType": problemtype,
            "dt": int(time.time() * 1000),
            "result": payload_result,
        }
        r = http_request("POST", api_url(self.domain, URL_PROBLEM_ANSWER), headers=self.headers, data=json.dumps(payload))
        result = r.json()
        event = {
            "lesson": self.lessonname,
            "lessonid": self.lessonid,
            "problemid": problemid,
            "problemtype": problemtype,
            "answers": real_answer,
            "source": source,
            "status": "success" if result["code"] == 0 else "error",
            "message": result.get("msg", ""),
        }
        if pending_answer_id:
            event["pending_answer_id"] = pending_answer_id
        self.on_event("problem", event)

    def _compute_ai_window(self, limit: int) -> tuple[float, Optional[float]]:
        """Submission window for AI mode: ``(min_hold, max_wait)`` seconds.

        - ``min_hold`` — earliest submit time, measured from problem receipt.
        - ``max_wait`` — how long to wait for AI before falling back.
          ``None`` means wait indefinitely.
        """
        if limit <= 0:
            return (0.0, None)
        if self.course_config.get("answer_last5s", True):
            target = max(0.0, limit - random.uniform(1, min(5, limit)))
            return (target, target)
        # last5s OFF: submit ASAP when AI returns; keep ~1s buffer for fallback.
        return (0.0, max(0.5, float(limit - 1)))

    def _wait_for_delay(self, start_time: float, limit: int) -> bool:
        """Wait until the target submit time. Returns False if lesson stopped."""
        delay = 0
        if limit > 0 and self.course_config.get("answer_last5s", True):
            delay = max(0, limit - random.uniform(1, min(5, limit)))
        remaining = delay - (time.time() - start_time)
        if remaining > 0:
            time.sleep(remaining)
        if limit > 0 and time.time() - start_time >= limit:
            return False
        return self._running

    def _answer_problem(self, problem: dict, problemid: Any, problemtype: int, mode: str, limit: int) -> None:
        start_time = time.time()

        # The account-level mode is a global override for every course.
        global_mode = get_ai_answering_mode(self.account_id)
        mode = global_mode if global_mode in {"ai", "random", "off"} else mode
        review_source = mode if mode in {"ai", "random"} else "off"
        pending = self._begin_answer_review(problem, problemid, problemtype, review_source)

        if mode == "off":
            logger.info("[%s] Global answer mode is off, waiting for review dismissal for problem %s", self.account_id, problemid)
            self._wait_for_answer_confirmation(pending, limit, start_time)
            return

        if mode == "ai":
            keys_to_try = self._ai_keys_to_try()
            if not keys_to_try:
                logger.warning("[%s] AI mode selected but no API key configured, using fallback for problem %s", self.account_id, problemid)
                answers, source = self._build_fallback_answer(problem, problemtype)
                self._update_pending_answer(
                    pending["answer_id"], answers, source if answers is not None else "off", answers is not None,
                )
                self._wait_for_answer_confirmation(pending, limit, start_time)
                return

            # Start AI call in background thread.
            result_holder = [None]
            ai_done = threading.Event()

            logger.info("[%s] Attempting AI answer for problem %s", self.account_id, problemid)

            def _call_ai():
                try:
                    result_holder[0] = self._build_ai_answers(problem, keys_to_try)
                except Exception:
                    logger.exception("[%s] AI answering failed for problem %s", self.account_id, problemid)
                finally:
                    ai_done.set()

            threading.Thread(target=_call_ai, daemon=True).start()

            _, max_wait = self._compute_ai_window(limit)

            # Wait for AI to return, capped by max_wait (None = forever).
            if max_wait is None:
                ai_done.wait()
            else:
                remaining_wait = max_wait - (time.time() - start_time)
                if remaining_wait > 0:
                    ai_done.wait(timeout=remaining_wait)

            if not self._running:
                self._wait_for_answer_confirmation(pending, limit, start_time)
                return

            if result_holder[0] is not None:
                self._update_pending_answer(
                    pending["answer_id"], result_holder[0], "ai", True,
                )
                self._wait_for_answer_confirmation(pending, limit, start_time)
                return

            # AI failed or timed out — show the deterministic random-mode
            # candidate and require the same explicit confirmation.
            self.on_event("problem", {
                "lesson": self.lessonname,
                "lessonid": self.lessonid,
                "problemid": problemid,
                "problemtype": problemtype,
                "status": "ai_failed",
            })
            fallback_answer, fallback_source = self._build_fallback_answer(problem, problemtype)
            self._update_pending_answer(
                pending["answer_id"], fallback_answer,
                fallback_source if fallback_answer is not None else "off",
                fallback_answer is not None,
            )
            self._wait_for_answer_confirmation(pending, limit, start_time)

        elif mode == "random":
            answers, source = self._build_fallback_answer(problem, problemtype)
            self._update_pending_answer(
                pending["answer_id"], answers, source if answers is not None else "off", answers is not None,
            )
            self._wait_for_answer_confirmation(pending, limit, start_time)

    def _start_answer_for_problem(self, problemid: Any, limit: int) -> None:
        for problem in self.problems_ls:
            if problem["problemId"] == problemid:
                if problem.get("result") is not None:
                    return
                problemtype = problem["problemType"]
                global_mode = get_ai_answering_mode(self.account_id)
                mode = global_mode if global_mode in {"ai", "random", "off"} else self.course_config.get(
                    "type%d" % problemtype, "off"
                )

                threading.Thread(
                    target=self._answer_problem,
                    args=(problem, problemid, problemtype, mode, limit),
                    daemon=True,
                ).start()
                return

    def _handle_danmu(self, content: str) -> None:
        if not self.course_config.get("auto_danmu", True):
            return

        key = content.lower().strip()
        now = time.time()
        self.danmu_dict.setdefault(key, [])
        self.danmu_dict[key] = [t for t in self.danmu_dict[key] if now - t <= 60]

        if now - self.sent_danmu_dict.get(key, 0) <= 60:
            return

        danmu_limit = max(1, self.course_config.get("danmu_threshold", 3))
        if len(self.danmu_dict[key]) + 1 >= danmu_limit:
            self.danmu_dict[key] = []
            self.sent_danmu_dict[key] = now
            threading.Thread(target=self.send_danmu, args=(content,), daemon=True).start()
        else:
            self.danmu_dict[key].append(now)

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, wsapp: websocket.WebSocketApp) -> None:
        wsapp.send(json.dumps({
            "op": "hello",
            "userid": self.user_uid,
            "role": "student",
            "auth": self.auth,
            "lessonid": self.lessonid,
        }))

    def _on_message(self, wsapp: websocket.WebSocketApp, message: str) -> None:
        data = json.loads(message)
        op = data.get("op", "")
        logger.info("[%s][WS %s] op=%s", self.account_id, self.lessonname, op)

        if op == "hello":
            timeline = data.get("timeline", [])
            presentation_ids = list({
                slide["pres"]
                for slide in timeline
                if slide.get("type") == "slide" and "pres" in slide
            })
            current = data.get("presentation")
            if current and current not in presentation_ids:
                presentation_ids.append(current)
            for pid in presentation_ids:
                self._add_problems(self._get_problems_from_presentation(pid))

        elif op == "unlockproblem":
            problem = data["problem"]
            self.on_event("problem_received", {
                "lesson": self.lessonname,
                "lessonid": self.lessonid,
                "problemid": problem["sid"],
            })
            self._start_answer_for_problem(problem["sid"], problem.get("limit", -1) - 1)

        elif op == "lessonfinished":
            self._lesson_ended = True
            wsapp.close()

        elif op in ("presentationupdated", "presentationcreated", "showpresentation"):
            pid = data.get("presentation")
            if pid:
                self._add_problems(self._get_problems_from_presentation(pid))

        elif op == "newdanmu":
            content = data.get("danmu", "")
            if content:
                self._handle_danmu(content)

        elif op == "gainbonus":
            logger.info("[%s][WS %s] gainbonus raw: %s", self.account_id, self.lessonname, message)
            redpacket = data.get("redpacket", data)
            red_envelope_id = redpacket.get("redEnvelopeId")
            if red_envelope_id and self.course_config.get("auto_redpacket", True):
                threading.Thread(
                    target=self._grab_red_packet,
                    args=(red_envelope_id,),
                    daemon=True,
                ).start()

        elif op == "callpaused":
            if data.get("name") == self.user_uname:
                self.on_event("call", {"lesson": self.lessonname, "lessonid": self.lessonid})
