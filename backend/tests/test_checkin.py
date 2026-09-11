import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config  # noqa: E402
import event_log  # noqa: E402
from ai_provider import DeepSeekProvider, create_provider  # noqa: E402
from checkin import CheckinError, RainClassroomClient  # noqa: E402
from lesson import Lesson  # noqa: E402
from main import AIKeyEntry, AiAnsweringBody, AutoCheckinBody, CheckinDelayBody, CheckinSourceBody, QRCheckinBody, app  # noqa: E402
from monitor import Monitor  # noqa: E402


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class CheckinConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_config_path = config._CONFIG_PATH
        config._CONFIG_PATH = Path(self.temp_dir.name) / "config.json"
        config.save_config({"active_account_id": None, "accounts": {}})

    def tearDown(self):
        config._CONFIG_PATH = self.old_config_path
        self.temp_dir.cleanup()

    def add_account(self, account_id="account-1", **patches):
        account = config.new_empty_account("school.example")
        account.update(patches)
        config.upsert_account(account_id, account)

    def test_old_config_defaults_to_qr_source(self):
        self.add_account()
        self.assertEqual(config.get_checkin_source("account-1"), 21)

    def test_wechat_scan_checkin_source_is_source_one(self):
        self.add_account(checkin_source=1)
        self.assertEqual(config.get_checkin_source("account-1"), 1)
        self.assertEqual(
            config.resolve_checkin_source("account-1", "course-1", {"checkin_source": 1}),
            1,
        )

    def test_login_defaults_to_yuketang_server(self):
        self.assertEqual(config.DEFAULT_DOMAIN, "www.yuketang.cn")
        self.assertEqual(config.new_empty_account()["domain"], "www.yuketang.cn")

    def test_new_account_defaults_to_requested_checkin_settings(self):
        self.add_account()
        self.assertEqual(config.get_poll_interval("account-1"), 60)
        self.assertEqual(config.get_checkin_delay("account-1"), 60)
        self.assertFalse(config.get_auto_checkin("account-1"))
        self.assertEqual(config.get_auto_checkin_mode("account-1"), "off")
        self.assertEqual(config.new_empty_account()["auto_checkin_time"], "08:00")

    def test_auto_checkin_boolean_compatibility_persists(self):
        self.add_account()
        self.assertTrue(config.set_auto_checkin("account-1", True))
        self.assertTrue(config.get_auto_checkin("account-1"))
        self.assertFalse(config.set_auto_checkin("account-1", False))
        self.assertFalse(config.get_auto_checkin("account-1"))

    def test_new_account_default_settings_api(self):
        self.add_account()
        with TestClient(app) as client:
            poll = client.get("/api/accounts/account-1/poll-interval")
            delay = client.get("/api/accounts/account-1/checkin-delay")
            auto = client.get("/api/accounts/account-1/auto-checkin")

        self.assertEqual(poll.json()["poll_interval"], 60)
        self.assertEqual(poll.json()["default"], 60)
        self.assertEqual(delay.json()["checkin_delay"], 60)
        self.assertEqual(delay.json()["default"], 60)
        self.assertFalse(auto.json()["auto_checkin"])
        self.assertEqual(auto.json()["auto_checkin_mode"], "off")
        self.assertFalse(auto.json()["default"])

    def test_scheduled_auto_checkin_waits_until_local_start_time(self):
        self.add_account()
        mode, schedule_time = config.set_auto_checkin_mode("account-1", "scheduled", "09:00")
        self.assertEqual((mode, schedule_time), ("scheduled", "09:00"))
        with patch("config._time.strftime", return_value="08:59"):
            self.assertFalse(config.get_auto_checkin("account-1"))
        with patch("config._time.strftime", return_value="09:00"):
            self.assertTrue(config.get_auto_checkin("account-1"))
        self.assertEqual(config.get_auto_checkin_mode("account-1"), "scheduled")
        self.assertEqual(config.get_auto_checkin_time("account-1"), "09:00")

    def test_ai_answering_defaults_to_enabled_and_persists(self):
        self.add_account()
        self.assertTrue(config.get_ai_answering_enabled("account-1"))
        self.assertFalse(config.set_ai_answering_enabled("account-1", False))
        self.assertFalse(config.get_ai_answering_enabled("account-1"))

    def test_ai_answering_api_reads_and_updates_account_setting(self):
        self.add_account()
        with TestClient(app) as client:
            initial = client.get("/api/accounts/account-1/ai-answering")
            updated = client.put(
                "/api/accounts/account-1/ai-answering",
                json={"ai_answering_enabled": False},
            )

        self.assertEqual(initial.status_code, 200)
        self.assertTrue(initial.json()["ai_answering_enabled"])
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()["ai_answering_enabled"])
        self.assertFalse(config.get_ai_answering_enabled("account-1"))

    def test_ai_answering_mode_api_supports_three_global_modes(self):
        self.add_account()
        with TestClient(app) as client:
            initial = client.get("/api/accounts/account-1/ai-answering")
            random_mode = client.put(
                "/api/accounts/account-1/ai-answering",
                json={"ai_answering_mode": "random"},
            )
            off_mode = client.put(
                "/api/accounts/account-1/ai-answering",
                json={"ai_answering_mode": "off"},
            )

        self.assertEqual(initial.json()["ai_answering_mode"], "ai")
        self.assertEqual(random_mode.status_code, 200)
        self.assertEqual(random_mode.json()["ai_answering_mode"], "random")
        self.assertFalse(random_mode.json()["ai_answering_enabled"])
        self.assertEqual(off_mode.status_code, 200)
        self.assertEqual(config.get_ai_answering_mode("account-1"), "off")

    def test_ai_answering_api_rejects_ai_without_api_key(self):
        self.add_account(ai_answering_mode="random", ai_answering_enabled=False)
        with TestClient(app) as client:
            response = client.put(
                "/api/accounts/account-1/ai-answering",
                json={"ai_answering_mode": "ai"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "ai_api_key_required")
        self.assertEqual(config.get_ai_answering_mode("account-1"), "random")

    def test_checkin_delay_defaults_to_sixty_and_clamps(self):
        self.add_account()
        self.assertEqual(config.get_checkin_delay("account-1"), 60)
        self.assertEqual(config.set_checkin_delay("account-1", 12), 12)
        self.assertEqual(config.set_checkin_delay("account-1", 999), config.MAX_CHECKIN_DELAY)
        self.assertEqual(config.get_checkin_delay("account-1"), config.MAX_CHECKIN_DELAY)

    def test_checkin_delay_api_reads_and_updates_account_setting(self):
        self.add_account()
        with TestClient(app) as client:
            initial = client.get("/api/accounts/account-1/checkin-delay")
            updated = client.put(
                "/api/accounts/account-1/checkin-delay",
                json={"checkin_delay": 8},
            )

        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()["checkin_delay"], 60)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["checkin_delay"], 8)

    def test_auto_checkin_api_reads_and_updates_account_setting(self):
        self.add_account()
        with TestClient(app) as client:
            initial = client.get("/api/accounts/account-1/auto-checkin")
            updated = client.put(
                "/api/accounts/account-1/auto-checkin",
                json={"auto_checkin": False},
            )

        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["auto_checkin"])
        self.assertEqual(initial.json()["auto_checkin_mode"], "off")
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()["auto_checkin"])
        self.assertFalse(config.get_auto_checkin("account-1"))

    def test_auto_checkin_api_supports_scheduled_mode_and_time(self):
        self.add_account()
        with TestClient(app) as client:
            scheduled = client.put(
                "/api/accounts/account-1/auto-checkin",
                json={"auto_checkin_mode": "scheduled", "auto_checkin_time": "23:15"},
            )

        self.assertEqual(scheduled.status_code, 200)
        self.assertEqual(scheduled.json()["auto_checkin_mode"], "scheduled")
        self.assertEqual(scheduled.json()["auto_checkin_time"], "23:15")
        self.assertEqual(scheduled.json()["modes"], ["on", "scheduled", "off"])
        self.assertEqual(config.get_auto_checkin_mode("account-1"), "scheduled")
        self.assertEqual(config.get_auto_checkin_time("account-1"), "23:15")

    def test_clear_events_api_removes_current_account_history(self):
        self.add_account()
        old_log_dir = event_log._LOG_DIR
        temp_log_dir = Path(self.temp_dir.name) / "events"
        temp_log_dir.mkdir()
        event_log._LOG_DIR = temp_log_dir
        try:
            event_log.append("account-1", {"type": "test"})
            with TestClient(app) as client:
                response = client.delete("/api/accounts/account-1/events")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(event_log.load_recent("account-1"), [])
        finally:
            event_log._LOG_DIR = old_log_dir

    def test_global_and_course_override_resolution(self):
        self.add_account(checkin_source=23)
        self.assertEqual(config.resolve_checkin_source("account-1", "course-1"), 23)
        self.assertEqual(
            config.resolve_checkin_source("account-1", "course-1", {"checkin_source": 5}),
            5,
        )
        self.assertEqual(
            config.resolve_checkin_source("account-1", "course-1", {"checkin_source": "inherit"}),
            23,
        )

    def test_qr_resolution_is_always_source_21(self):
        self.add_account(checkin_source=23)
        self.assertEqual(
            config.resolve_checkin_source(
                "account-1", "course-1", {"checkin_source": 5}, force_qr_source=True
            ),
            21,
        )

    def test_invalid_persisted_course_value_falls_back_to_global(self):
        self.add_account(checkin_source=23)
        self.assertEqual(
            config.resolve_checkin_source("account-1", "course-1", {"checkin_source": 999}),
            23,
        )
        with self.assertRaises(ValueError):
            config.set_checkin_source("account-1", 999)

    def test_legacy_answer_modes_are_migrated_to_supported_modes(self):
        self.add_account(courses={"course-1": {"type1": "blank", "type2": "reserved", "type3": "unknown"}})
        course = config.get_course_config("account-1", "course-1")
        self.assertEqual(course["type1"], "random")
        self.assertEqual(course["type2"], "ai")
        self.assertEqual(course["type3"], "ai")

    def test_course_answer_mode_api_rejects_removed_modes(self):
        self.add_account()
        payload = {
            "type1": "blank",
            "type2": "ai",
            "type3": "random",
            "type4": "off",
            "type5": "ai",
            "course_enabled": True,
            "answer_last5s": True,
            "auto_danmu": True,
            "auto_redpacket": True,
            "danmu_threshold": 3,
            "checkin_source": "inherit",
            "notification": {"enabled": False, "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True},
            "voice_notification": {"enabled": False, "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True},
            "pushdeer_notification": {"enabled": False, "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True},
        }
        with TestClient(app) as client:
            response = client.put("/api/accounts/account-1/courses/settings/course-1", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_course_answer_mode_api_rejects_ai_transition_without_api_key(self):
        self.add_account(courses={"course-1": {"type1": "random"}})
        payload = {
            "type1": "ai",
            "type2": "random",
            "type3": "random",
            "type4": "off",
            "type5": "random",
            "course_enabled": True,
            "answer_last5s": True,
            "auto_danmu": True,
            "auto_redpacket": True,
            "danmu_threshold": 3,
            "checkin_source": "inherit",
            "notification": {"enabled": False, "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True},
            "voice_notification": {"enabled": False, "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True},
            "pushdeer_notification": {"enabled": False, "signin": True, "problem": True, "call": True, "danmu": True, "red_packet": True},
        }
        with TestClient(app) as client:
            response = client.put("/api/accounts/account-1/courses/settings/course-1", json=payload)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "ai_api_key_required")
        self.assertEqual(config.get_course_config("account-1", "course-1")["type1"], "random")


class CheckinClientTests(unittest.TestCase):
    def test_scan_posts_to_account_domain_and_returns_lesson_id(self):
        client = RainClassroomClient("account-1", "school.example", "session-secret")
        response = FakeResponse({"code": 0, "data": {"value": "lesson-42"}})
        with patch("checkin.http_request", return_value=response) as request:
            result = client.scan_qr_code("  https://qr.example/value  ")

        self.assertEqual(result.lesson_id, "lesson-42")
        args, kwargs = request.call_args
        self.assertEqual(args[:2], ("POST", "https://school.example/api/v3/app/scan"))
        self.assertEqual(json.loads(kwargs["data"]), {"url": "https://qr.example/value"})
        self.assertIn("session-secret", kwargs["headers"]["Cookie"])

    def test_scan_server_error_and_invalid_json_are_structured(self):
        client = RainClassroomClient("account-1", "school.example", "session")
        with patch("checkin.http_request", return_value=FakeResponse({"code": 50070, "msg": "expired"})):
            with self.assertRaises(CheckinError) as ctx:
                client.scan_qr_code("qr")
        self.assertEqual(ctx.exception.code, 50070)
        self.assertEqual(ctx.exception.stage, "scan")

        with patch("checkin.http_request", return_value=FakeResponse(ValueError("bad json"))):
            with self.assertRaises(CheckinError) as ctx:
                client.scan_qr_code("qr")
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_checkin_preserves_auth_and_uses_requested_source(self):
        client = RainClassroomClient("account-1", "school.example", "session")
        response = FakeResponse(
            {"code": 0, "data": {"lessonToken": "lesson-secret"}},
            {"Set-Auth": "bearer-secret"},
        )
        with patch("checkin.http_request", return_value=response) as request:
            result = client.checkin("lesson-42", 23)

        self.assertEqual(result.source, 23)
        self.assertEqual(client.headers["Authorization"], "Bearer bearer-secret")
        self.assertEqual(json.loads(request.call_args.kwargs["data"]), {"source": 23, "lessonId": "lesson-42"})

    def test_checkin_qr_opt_in_is_explicit_and_missing_auth_is_error(self):
        client = RainClassroomClient("account-1", "school.example", "session")
        response = FakeResponse(
            {"code": 0, "data": {"lessonToken": "lesson-secret"}},
            {"set-auth": "bearer-secret"},
        )
        with patch("checkin.http_request", return_value=response) as request:
            client.checkin("lesson-42", 21, join_if_not_in=True)
        self.assertTrue(json.loads(request.call_args.kwargs["data"])["joinIfNotIn"])

        missing_auth = FakeResponse({"code": 0, "data": {"lessonToken": "lesson-secret"}})
        with patch("checkin.http_request", return_value=missing_auth):
            with self.assertRaises(CheckinError) as ctx:
                client.checkin("lesson-43", 21)
        self.assertEqual(ctx.exception.code, "missing_set_auth")


class MonitorReuseTests(unittest.TestCase):
    def test_qr_lesson_is_not_registered_twice(self):
        created = []

        class FakeLesson:
            def __init__(self, **kwargs):
                created.append(kwargs)
                self.lessonid = kwargs["lesson_data"]["lessonid"]
                self.lessonname = kwargs["lesson_data"]["lessonname"]
                self.classroomid = kwargs["lesson_data"]["classroomid"]
                self.teacher_name = None
                self._checkin_result = object()

            def checkin(self):
                return None

            def start_lesson(self, **kwargs):
                return None

            def stop_lesson(self):
                return None

        monitor = Monitor("account-1", object())
        with patch("monitor.Lesson", FakeLesson), patch("monitor.get_domain", return_value="school.example"), patch(
            "monitor.get_sessionid", return_value="session"
        ), patch("monitor.get_course_config", return_value={}), patch("monitor.threading.Thread") as thread:
            first, first_already = monitor.start_qr_lesson(42)
            second, second_already = monitor.start_qr_lesson(42)

        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertFalse(first_already)
        self.assertTrue(second_already)
        self.assertEqual(len(created), 1)
        thread.assert_called_once()

    def test_disabled_auto_checkin_skips_new_lessons(self):
        monitor = Monitor("account-1", object())
        lesson_list = [{"lessonId": "lesson-42", "classroomId": "course-1", "courseName": "Test"}]
        with patch("monitor.get_auto_checkin", return_value=False), patch(
            "monitor.get_domain", return_value="school.example"
        ), patch("monitor.get_sessionid", return_value="session"), patch("monitor.Lesson") as lesson:
            monitor._sync_lessons(lesson_list)

        lesson.assert_not_called()
        self.assertEqual(monitor.get_active_lessons(), [])


class AiAnsweringSwitchTests(unittest.TestCase):
    def test_global_off_mode_still_starts_review_worker(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.course_config = {"type1": "ai"}
        lesson.problems_ls = [{"problemId": "problem-off", "problemType": 1, "result": None}]

        with patch("lesson.get_ai_answering_mode", return_value="off"), patch("lesson.threading.Thread") as thread:
            lesson._start_answer_for_problem("problem-off", 0)

        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"], lesson._answer_problem)

    def test_global_random_mode_overrides_course_ai_mode(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.lessonname = "Test lesson"
        lesson.lessonid = "lesson-1"
        lesson._running = True
        lesson.course_config = {}
        lesson._pending_answers = {}
        lesson._pending_answers_lock = threading.Lock()

        def on_event(event_type, data):
            if event_type == "answer_pending":
                lesson.resolve_pending_answer(data["answer_id"], "confirm")

        lesson.on_event = on_event

        with patch("lesson.get_ai_answering_mode", return_value="random"), patch.object(
            lesson, "_build_ai_answers", side_effect=AssertionError("AI provider should not be called")
        ), patch.object(lesson, "_build_fallback_answer", return_value=(["A"], "random")), patch.object(
            lesson, "_wait_for_delay", return_value=True
        ), patch.object(lesson, "_submit_answer") as submit:
            lesson._answer_problem({"problemId": "problem-1"}, "problem-1", 1, "ai", 0)

        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[:4], ("problem-1", 1, ["A"], "random"))
        self.assertIsInstance(submit.call_args.args[4], str)

    def test_global_off_mode_skips_answering(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.lessonname = "Test lesson"
        lesson.lessonid = "lesson-1"
        lesson._running = True
        lesson.course_config = {"type1": "ai"}
        lesson._pending_answers = {}
        lesson._pending_answers_lock = threading.Lock()
        events = []

        def on_event(event_type, data):
            events.append((event_type, data))
            if event_type == "answer_pending":
                lesson.resolve_pending_answer(data["answer_id"], "skip")

        lesson.on_event = on_event

        with patch("lesson.get_ai_answering_mode", return_value="off"), patch.object(lesson, "_submit_answer") as submit:
            lesson._answer_problem({"problemId": "problem-off"}, "problem-off", 1, "ai", 0)

        submit.assert_not_called()
        self.assertEqual(events[0][0], "answer_pending")
        self.assertEqual(events[0][1]["source"], "off")
        self.assertFalse(events[0][1]["answer_ready"])
        self.assertEqual(events[1][0], "answer_review_closed")

    def test_ai_answer_requires_review_decision_before_submit(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.lessonname = "Test lesson"
        lesson.lessonid = "lesson-1"
        lesson._running = True
        lesson.course_config = {}
        lesson._pending_answers = {}
        lesson._pending_answers_lock = threading.Lock()
        events = []

        def on_event(event_type, data):
            events.append((event_type, data))
            if event_type == "answer_pending":
                self.assertFalse(submit.called)
                self.assertIsNone(data["answer"])
                self.assertFalse(data["answer_ready"])
            if event_type == "answer_updated":
                self.assertEqual(data["answer"], ["B"])
                self.assertEqual(data["source"], "ai")
                lesson.resolve_pending_answer(data["answer_id"], "confirm")

        lesson.on_event = on_event
        problem = {
            "problemId": "problem-1",
            "problemType": 1,
            "content": "2 + 2?",
            "options": [{"key": "A", "text": "3"}, {"key": "B", "text": "4"}],
        }

        with patch("lesson.get_ai_answering_mode", return_value="ai"), patch.object(
            lesson, "_ai_keys_to_try", return_value=[("deepseek", "key", "test")]
        ), patch.object(lesson, "_build_ai_answers", return_value=["B"]), patch.object(
            lesson, "_compute_ai_window", return_value=(0, 5)
        ), patch.object(lesson, "_wait_for_delay", return_value=True), patch.object(lesson, "_submit_answer") as submit:
            lesson._answer_problem(problem, "problem-1", 1, "ai", 5)

        self.assertEqual(events[0][0], "answer_pending")
        self.assertEqual(events[0][1]["problem"]["content"], "2 + 2?")
        self.assertIsNone(events[0][1]["answer"])
        self.assertEqual(events[1][0], "answer_updated")
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[:4], ("problem-1", 1, ["B"], "ai"))
        self.assertEqual(submit.call_args.args[4], events[0][1]["answer_id"])

    def test_unhandled_ai_popup_falls_back_to_random_eight_seconds_before_deadline(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.lessonname = "Test lesson"
        lesson.lessonid = "lesson-1"
        lesson._running = True
        lesson.course_config = {}
        lesson._pending_answers = {}
        lesson._pending_answers_lock = threading.Lock()
        events = []
        lesson.on_event = lambda event_type, data: events.append((event_type, data))
        pending = lesson._begin_answer_review(
            {"problemId": "problem-1", "problemType": 1, "options": [{"key": "A"}, {"key": "B"}]},
            "problem-1",
            1,
            "ai",
        )

        with patch.object(lesson, "_wait_for_pending_answer", return_value=(None, dict(pending))) as wait_pending, patch.object(
            lesson, "_build_fallback_answer", return_value=(["B"], "random")
        ), patch.object(lesson, "_wait_for_delay", return_value=True), patch.object(
            lesson, "_submit_answer"
        ) as submit:
            lesson._wait_for_answer_confirmation(pending, 10, 0)

        wait_pending.assert_called_once_with(
            pending["answer_id"],
            10,
            0,
            retain_on_timeout=True,
            timeout_after=2.0,
        )
        self.assertEqual([event[0] for event in events], ["answer_pending", "answer_updated", "answer_review_closed"])
        self.assertEqual(events[1][1]["source"], "random")
        self.assertTrue(events[1][1]["answer_ready"])
        submit.assert_called_once_with("problem-1", 1, ["B"], "random", pending["answer_id"])
        self.assertEqual(lesson._pending_answers, {})

    def test_ai_fallback_preserves_ai_for_single_multiple_and_fill_in(self):
        for problemtype, ai_answer in ((1, ["A"]), (2, ["A", "B"]), (4, "AI fill")):
            lesson = Lesson.__new__(Lesson)
            lesson.account_id = "account-1"
            lesson.lessonname = "Test lesson"
            lesson.lessonid = "lesson-1"
            lesson._running = True
            lesson.course_config = {}
            lesson._pending_answers = {}
            lesson._pending_answers_lock = threading.Lock()
            lesson.on_event = lambda event_type, data: None
            problem = {
                "problemId": f"problem-{problemtype}",
                "problemType": problemtype,
                "options": [{"key": "A"}, {"key": "B"}],
            }
            pending = lesson._begin_answer_review(problem, problem["problemId"], problemtype, "ai")
            self.assertTrue(lesson._update_pending_answer(pending["answer_id"], ai_answer, "ai", True))

            with patch.object(lesson, "_wait_for_pending_answer", return_value=(None, dict(pending))), patch.object(
                lesson, "_wait_for_delay", return_value=True
            ), patch.object(lesson, "_submit_answer") as submit:
                lesson._wait_for_answer_confirmation(pending, 10, 0)

            submit.assert_called_once_with(
                problem["problemId"], problemtype, ai_answer, "ai", pending["answer_id"]
            )

    def test_ai_fallback_uses_random_for_vote_and_short_answer(self):
        for problemtype, ai_answer in ((3, ["A"]), (5, "AI short")):
            lesson = Lesson.__new__(Lesson)
            lesson.account_id = "account-1"
            lesson.lessonname = "Test lesson"
            lesson.lessonid = "lesson-1"
            lesson._running = True
            lesson.course_config = {}
            lesson._pending_answers = {}
            lesson._pending_answers_lock = threading.Lock()
            lesson.on_event = lambda event_type, data: None
            problem = {
                "problemId": f"problem-{problemtype}",
                "problemType": problemtype,
                "options": [{"key": "A"}, {"key": "B"}],
            }
            pending = lesson._begin_answer_review(problem, problem["problemId"], problemtype, "ai")
            self.assertTrue(lesson._update_pending_answer(pending["answer_id"], ai_answer, "ai", True))

            with patch.object(lesson, "_wait_for_pending_answer", return_value=(None, dict(pending))), patch.object(
                lesson, "_build_fallback_answer", return_value=("1", "random")
            ), patch.object(lesson, "_wait_for_delay", return_value=True), patch.object(
                lesson, "_submit_answer"
            ) as submit:
                lesson._wait_for_answer_confirmation(pending, 10, 0)

            submit.assert_called_once_with(
                problem["problemId"], problemtype, "1", "random", pending["answer_id"]
            )

    def test_random_policy_and_confirmation_flow(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.lessonname = "Test lesson"
        lesson.lessonid = "lesson-1"
        lesson._running = True
        lesson.course_config = {}
        lesson._pending_answers = {}
        lesson._pending_answers_lock = threading.Lock()
        events = []
        submit = None

        def on_event(event_type, data):
            events.append((event_type, data))
            if event_type == "answer_updated":
                self.assertFalse(submit.called)
                lesson.resolve_pending_answer(data["answer_id"], "confirm")

        lesson.on_event = on_event

        with patch("lesson.random.choice", return_value="B"), patch("lesson.random.sample", return_value=["B"]), patch(
            "lesson.random.randint", return_value=1
        ), patch.object(
            lesson, "_wait_for_delay", return_value=True
        ), patch.object(lesson, "_submit_answer") as submit_patch:
            submit = submit_patch
            self.assertEqual(lesson._build_fallback_answer({"options": [{"key": "A"}, {"key": "B"}]}, 1), (["B"], "random"))
            self.assertEqual(lesson._build_fallback_answer({"options": [{"key": "A"}, {"key": "B"}]}, 2), (["B"], "random"))
            self.assertEqual(lesson._build_fallback_answer({}, 4), ("1", "random"))
            self.assertEqual(lesson._build_fallback_answer({}, 5), ("1", "random"))
            lesson._answer_problem(
                {"problemId": "problem-2", "problemType": 2, "options": [{"key": "A"}, {"key": "B"}]},
                "problem-2",
                2,
                "random",
                0,
            )

        with patch("lesson.random.sample", return_value=["A"]), patch("lesson.random.randint", return_value=1):
            self.assertEqual(
                lesson._build_fallback_answer(
                    {"options": [{"key": "A"}, {"key": "B"}], "pollingCount": 2}, 3
                ),
                (["A"], "random"),
            )

        self.assertEqual(events[0][0], "answer_pending")
        self.assertIsNone(events[0][1]["answer"])
        self.assertFalse(events[0][1]["answer_ready"])
        self.assertEqual(events[1][0], "answer_updated")
        self.assertEqual(events[1][1]["answer"], ["B"])
        self.assertEqual(events[1][1]["source"], "random")
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[:4], ("problem-2", 2, ["B"], "random"))

    def test_skip_does_not_submit_candidate(self):
        lesson = Lesson.__new__(Lesson)
        lesson.account_id = "account-1"
        lesson.lessonname = "Test lesson"
        lesson.lessonid = "lesson-1"
        lesson._running = True
        lesson.course_config = {}
        lesson._pending_answers = {}
        lesson._pending_answers_lock = threading.Lock()
        events = []

        def on_event(event_type, data):
            events.append((event_type, data))
            if event_type == "answer_pending":
                lesson.resolve_pending_answer(data["answer_id"], "skip")

        lesson.on_event = on_event
        with patch.object(lesson, "_wait_for_delay", return_value=True), patch.object(lesson, "_submit_answer") as submit:
            lesson._answer_problem(
                {"problemId": "problem-3", "problemType": 1, "options": [{"key": "A"}]},
                "problem-3",
                1,
                "random",
                0,
            )

        submit.assert_not_called()
        self.assertEqual([event[0] for event in events], ["answer_pending", "answer_review_closed"])


class ApiValidationTests(unittest.TestCase):
    def test_source_and_qr_request_validation(self):
        with self.assertRaises(ValueError):
            CheckinSourceBody(checkin_source=99)
        with self.assertRaises(ValueError):
            QRCheckinBody(url="   ")
        self.assertEqual(QRCheckinBody(url="  qr-content  ").url, "qr-content")
        self.assertFalse(AutoCheckinBody(auto_checkin=False).auto_checkin)
        self.assertEqual(
            AutoCheckinBody(auto_checkin_mode="scheduled", auto_checkin_time="09:30").auto_checkin_time,
            "09:30",
        )
        with self.assertRaises(ValueError):
            AutoCheckinBody(auto_checkin_mode="scheduled", auto_checkin_time="25:00")
        self.assertFalse(AiAnsweringBody(ai_answering_enabled=False).ai_answering_enabled)
        self.assertEqual(CheckinDelayBody(checkin_delay=12).checkin_delay, 12)


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_config_path = config._CONFIG_PATH
        config._CONFIG_PATH = Path(self.temp_dir.name) / "config.json"
        config.save_config({"active_account_id": None, "accounts": {}})
        account = config.new_empty_account("school.example")
        config.upsert_account("account-1", account)

    def tearDown(self):
        config._CONFIG_PATH = self.old_config_path
        self.temp_dir.cleanup()

    @patch("openai.OpenAI")
    def test_deepseek_provider_uses_flash_model_and_openai_endpoint(self, openai_client):
        openai_client.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=" A "))]
        )

        provider = create_provider("deepseek", "sk-test")

        self.assertIsInstance(provider, DeepSeekProvider)
        self.assertEqual(provider.answer_choice("", ["A", "B"], 1), ["A"])
        openai_client.assert_called_once_with(
            base_url="https://api.deepseek.com",
            api_key="sk-test",
        )
        request = openai_client.return_value.chat.completions.create.call_args
        self.assertEqual(request.kwargs["model"], "deepseek-flash")
        self.assertEqual(request.kwargs["messages"][0]["content"][0]["type"], "text")

    def test_deepseek_key_endpoint_only_requires_api_key_and_activates_it(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/accounts/account-1/ai/deepseek",
                json={"api_key": "  sk-test  "},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "deepseek")
        self.assertEqual(response.json()["active_key"], 0)
        saved = config.get_ai_config("account-1")
        self.assertEqual(saved["active_key"], 0)
        self.assertEqual(saved["keys"][0], {
            "name": "DeepSeek",
            "provider": "deepseek",
            "key": "sk-test",
        })

    def test_deepseek_key_endpoint_rejects_blank_key(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/accounts/account-1/ai/deepseek",
                json={"api_key": "   "},
            )

        self.assertEqual(response.status_code, 422)

    def test_generic_key_schema_defaults_to_deepseek(self):
        entry = AIKeyEntry(key="  sk-test  ")
        self.assertEqual(entry.name, "DeepSeek")
        self.assertEqual(entry.provider, "deepseek")
        self.assertEqual(entry.key, "sk-test")


if __name__ == "__main__":
    unittest.main()
