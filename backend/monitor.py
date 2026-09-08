import asyncio
import logging
import threading
import time
from typing import Callable, Dict, Optional

import event_log
import pushdeer
from checkin import CheckinError
from config import (
    DEFAULT_POLL_INTERVAL, MAX_POLL_INTERVAL, MIN_POLL_INTERVAL,
    api_url, get_account, get_course_config, get_domain, get_poll_interval,
    get_auto_checkin, get_sessionid, http_request, make_headers, QR_CHECKIN_SOURCE, update_course_config,
)
from lesson import Lesson

logger = logging.getLogger(__name__)

URL_ON_LESSON = "https://{domain}/api/v3/classroom/on-lesson-upcoming-exam"


class Monitor:
    """One Monitor per account — polls that account's active lessons and manages
    the per-lesson WebSocket threads for it."""

    def __init__(
        self,
        account_id: str,
        event_queue: asyncio.Queue,
        on_session_expired: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.account_id = account_id
        self.event_queue = event_queue
        self._on_session_expired = on_session_expired
        self._active_lessons: Dict[int, Lesson] = {}
        self._manual_lesson_ids: set[str] = set()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lesson_start_lock = threading.Lock()
        # Wakes the poll loop early when poll_interval is reduced.
        self._wake_event = threading.Event()

    def wake(self) -> None:
        # Poll interval is re-read from config each cycle; kick the loop so a
        # config change takes effect without waiting for the current sleep.
        self._wake_event.set()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._loop = loop
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"monitor-{self.account_id}")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for lesson in list(self._active_lessons.values()):
                lesson.stop_lesson()
            self._active_lessons.clear()
            self._manual_lesson_ids.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def get_active_lessons(self) -> list:
        with self._lock:
            return [
                {
                    "lessonid": lesson.lessonid,
                    "lessonname": lesson.lessonname,
                    "classroomid": lesson.classroomid,
                    "teacher_name": lesson.teacher_name,
                }
                for lesson in self._active_lessons.values()
            ]

    def get_lesson(self, lesson_id: object) -> Optional[Lesson]:
        """Return an active lesson, tolerating numeric/string API ids."""
        with self._lock:
            lesson = self._active_lessons.get(lesson_id)
            if lesson is not None:
                return lesson
            key = str(lesson_id)
            return next(
                (candidate for candidate_id, candidate in self._active_lessons.items()
                 if str(candidate_id) == key),
                None,
            )

    def emit_event(self, event_type: str, data: dict) -> None:
        """Public seam for account-scoped flows outside the poll loop."""
        self._emit(event_type, data)

    def get_pending_answers(self) -> list[dict]:
        with self._lock:
            lessons = list(self._active_lessons.values())
        pending = []
        for lesson in lessons:
            pending.extend(lesson.get_pending_answers())
        return pending

    def resolve_pending_answer(self, answer_id: str, decision: str) -> bool:
        with self._lock:
            lessons = list(self._active_lessons.values())
        for lesson in lessons:
            if lesson.resolve_pending_answer(answer_id, decision):
                return True
        return False

    def start_qr_lesson(self, lesson_id: object) -> tuple[Optional[Lesson], bool]:
        """Check in and start a manually scanned lesson once.

        The scan itself is performed by RainClassroomClient.  This method owns
        only lesson registration and reuse, so Monitor's automatic discovery
        and manual QR entry share one active-lesson registry.
        """
        with self._lesson_start_lock:
            existing = self.get_lesson(lesson_id)
            if existing is not None:
                return existing, True

            domain, sessionid = self._current_credentials()
            if not sessionid:
                raise CheckinError("not_authenticated", "account session has expired or is missing", stage="setup")

            course_config = get_course_config(self.account_id, str(lesson_id))
            lesson_data = {
                "lessonid": lesson_id,
                "lessonname": course_config.get("name") or "QR lesson %s" % lesson_id,
                # /app/scan returns lessonId; basic-info may enrich this with
                # the real classroom id before the lesson is registered.
                "classroomid": lesson_id,
            }
            lesson = Lesson(
                account_id=self.account_id,
                lesson_data=lesson_data,
                sessionid=sessionid,
                domain=domain,
                course_config=course_config,
                on_event=self._emit,
                checkin_source=QR_CHECKIN_SOURCE,
                join_if_not_in=True,
            )
            # QR entry is deliberately synchronous up to check-in so the API
            # can report scan success separately from classroom entry.
            lesson.checkin()

            actual_classroom_id = str(lesson.classroomid)
            if actual_classroom_id != str(lesson_id):
                lesson.course_config = get_course_config(self.account_id, actual_classroom_id)

            existing = self.get_lesson(lesson.lessonid)
            if existing is not None:
                return existing, True
            with self._lock:
                self._active_lessons[lesson.lessonid] = lesson
                self._manual_lesson_ids.add(str(lesson.lessonid))

            self._emit("lesson_start", {
                "lesson": lesson.lessonname,
                "lessonid": lesson.lessonid,
                "message": "Started from QR code: %s" % lesson.lessonname,
            })
            threading.Thread(
                target=self._lesson_thread,
                args=(lesson,),
                daemon=True,
                name="lesson-qr-%s-%s" % (self.account_id, lesson.lessonid),
            ).start()
            return lesson, False

    def _current_credentials(self) -> tuple[str, str]:
        return get_domain(self.account_id), get_sessionid(self.account_id)

    def _run(self) -> None:
        while self._running:
            try:
                domain, sessionid = self._current_credentials()
                if not sessionid:
                    return
                headers = make_headers(domain, sessionid)
                r = http_request("GET", api_url(domain, URL_ON_LESSON), headers=headers)
                data = r.json()
                if data.get("code") != 0:
                    logger.warning("[%s] Session expired: %s", self.account_id, data.get("msg", ""))
                    self._emit("session_expired", {"message": data.get("msg", "Session expired")})
                    # Stop all per-lesson WS threads so they don't keep hammering
                    # Yuketang with an expired sessionid.
                    with self._lock:
                        for lesson in list(self._active_lessons.values()):
                            lesson.stop_lesson()
                        self._active_lessons.clear()
                    if self._on_session_expired:
                        self._on_session_expired(self.account_id)
                    self._running = False
                    return
                lesson_list = data["data"]["onLessonClassrooms"]
                logger.info("[%s] Monitor poll: %d active lesson(s)", self.account_id, len(lesson_list))
                self._sync_lessons(lesson_list)
            except Exception as e:
                logger.warning("[%s] Monitor poll failed: %s", self.account_id, e)
            interval = get_poll_interval(self.account_id)
            self._wake_event.clear()
            # Tick once per second so a stop() / interval change takes effect
            # within ~1s instead of waiting the whole interval.
            for _ in range(interval):
                if not self._running:
                    return
                if self._wake_event.is_set():
                    break
                time.sleep(1)

    def _sync_lessons(self, lesson_list: list) -> None:
        incoming_ids = set()
        domain, sessionid = self._current_credentials()

        for item in lesson_list:
            lesson_id = item["lessonId"]
            incoming_ids.add(str(lesson_id))

            already_tracked = self.get_lesson(lesson_id) is not None

            if not already_tracked:
                if not get_auto_checkin(self.account_id):
                    logger.info(
                        "[%s] Skipping lesson %s: automatic check-in is disabled",
                        self.account_id, lesson_id,
                    )
                    incoming_ids.discard(str(lesson_id))
                    continue
                lesson_name = item.get("courseName", "Unknown")
                lesson_data = {
                    "lessonid": lesson_id,
                    "lessonname": lesson_name,
                    "classroomid": item["classroomId"],
                }
                classroom_id = str(item["classroomId"])
                course_config = get_course_config(self.account_id, classroom_id)
                if course_config.get("name") != lesson_name:
                    course_config["name"] = lesson_name
                    update_course_config(self.account_id, classroom_id, {"name": lesson_name})
                if not course_config.get("course_enabled", True):
                    logger.info(
                        "[%s] Skipping lesson %s (%s): course disabled",
                        self.account_id, lesson_id, lesson_name,
                    )
                    # Drop from incoming so we re-evaluate next poll (cheap)
                    # but don't emit lesson_start.
                    incoming_ids.discard(str(lesson_id))
                    continue
                lesson = Lesson(
                    account_id=self.account_id,
                    lesson_data=lesson_data,
                    sessionid=sessionid,
                    domain=domain,
                    course_config=course_config,
                    on_event=self._emit,
                )

                # Re-check while holding the registry lock.  A QR request can
                # finish between the first lookup and this insertion; never
                # overwrite that already-running Lesson with a second one.
                with self._lock:
                    existing = next(
                        (candidate for candidate_id, candidate in self._active_lessons.items()
                         if str(candidate_id) == str(lesson_id)),
                        None,
                    )
                    if existing is None:
                        self._active_lessons[lesson_id] = lesson
                if existing is not None:
                    continue

                self._emit("lesson_start", {
                    "lesson": lesson.lessonname,
                    "lessonid": lesson_id,
                    "message": "Started monitoring: %s" % lesson.lessonname,
                })

                threading.Thread(
                    target=self._lesson_thread,
                    args=(lesson,),
                    daemon=True,
                    name="lesson-%s-%s" % (self.account_id, lesson_id),
                ).start()

        with self._lock:
            ended = [
                lid for lid in self._active_lessons
                if str(lid) not in incoming_ids and str(lid) not in self._manual_lesson_ids
            ]
        for lid in ended:
            with self._lock:
                lesson = self._active_lessons.pop(lid, None)
            if lesson:
                lesson.stop_lesson()
                self._emit("lesson_end", {
                    "lesson": lesson.lessonname,
                    "lessonid": lesson.lessonid,
                    "message": "Lesson ended: %s" % lesson.lessonname,
                })

    def _lesson_thread(self, lesson: Lesson) -> None:
        try:
            # QR lessons have already completed check-in before registration.
            lesson.start_lesson(checkin=lesson._checkin_result is None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] lesson %s stopped during startup: %s", self.account_id, lesson.lessonid, exc)
        finally:
            with self._lock:
                if self._active_lessons.get(lesson.lessonid) is lesson:
                    self._active_lessons.pop(lesson.lessonid, None)
                self._manual_lesson_ids.discard(str(lesson.lessonid))

    def _emit(self, event_type: str, data: dict) -> None:
        event = {"type": event_type, "account_id": self.account_id, **data}
        # A pending answer is transient UI state. Persisting it would recreate
        # an already-resolved popup after a browser reconnect.
        if event_type not in {"answer_pending", "answer_updated", "answer_review_closed"}:
            event_log.append(self.account_id, event)

        lesson_id = data.get("lessonid")
        if lesson_id is not None and event_type not in {"answer_pending", "answer_updated", "answer_review_closed"}:
            with self._lock:
                lesson = self._active_lessons.get(lesson_id)
            if lesson is not None:
                pushdeer.dispatch(self.account_id, str(lesson.classroomid), event_type, data)

        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.event_queue.put(event), self._loop)
