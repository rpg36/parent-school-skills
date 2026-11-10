"""Tests for canvas_read.py against a local mock Canvas server.

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import datetime as dt
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "canvas-grades", "scripts"))
import canvas_read  # noqa: E402

TOKEN = "secret-test-token-123"
NOW = dt.datetime.now(dt.timezone.utc)


def iso(delta_days):
    return (NOW + dt.timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


STATE = {"requests": [], "observees": None, "overrides": {}}


def default_observees():
    return [{"id": 200, "name": "Casey Doe", "short_name": "Casey", "sortable_name": "Doe, Casey"}]


COURSE_MATH = {
    "id": 11, "name": "Algebra I", "course_code": "ALG1", "term": {"name": "2026-27"},
    "enrollments": [{
        "type": "student", "user_id": 200, "computed_current_score": 88.5,
        "computed_current_grade": "B+", "computed_final_score": 70.1, "computed_final_grade": "C-",
        "has_grading_periods": True, "current_grading_period_title": "Q2",
        "current_period_computed_current_score": 91.0, "current_period_computed_current_grade": "A-",
        "current_period_computed_final_score": 80.0, "current_period_computed_final_grade": "B-",
    }],
}
COURSE_ENG = {
    "id": 12, "name": "English 9", "course_code": "ENG9", "term": {"name": "2026-27"},
    "hide_final_grades": True,
    "enrollments": [{"type": "student", "user_id": 200, "computed_current_score": None}],
}
COURSE_LOCKED = {"id": 13, "access_restricted_by_date": True}

ASSIGNMENTS = {
    11: [
        {"id": 1001, "name": "HW 1", "due_at": iso(-10), "points_possible": 10, "assignment_group_id": 1,
         "html_url": "x/1001", "submission": {"score": 9, "workflow_state": "graded", "submitted_at": iso(-11)}},
        {"id": 1002, "name": "Quiz 1", "due_at": iso(-3), "points_possible": 20, "assignment_group_id": 2,
         "html_url": "x/1002", "submission": {"missing": True, "workflow_state": "unsubmitted"}},
        {"id": 1003, "name": "HW 2", "due_at": iso(3), "points_possible": 10, "assignment_group_id": 1,
         "html_url": "x/1003", "submission": {"workflow_state": "unsubmitted"}},
        {"id": 1004, "name": "Project", "due_at": iso(30), "points_possible": 100, "assignment_group_id": 2,
         "html_url": "x/1004", "submission": {}},
    ],
    12: [
        {"id": 2001, "name": "Essay", "due_at": iso(5), "points_possible": 50, "html_url": "y/2001",
         "submission": {"submitted_at": iso(-1), "workflow_state": "submitted"}},
    ],
}

SUBMISSIONS = {
    11: [
        {"assignment_id": 1001, "score": 9, "grade": "9", "workflow_state": "graded", "late": True,
         "points_deducted": 1, "submitted_at": iso(-11), "graded_at": iso(-2),
         "submission_comments": [{"author_name": "Ms. Lee", "created_at": iso(-2),
                                  "comment": "<p>Nice work,  show steps</p>"}],
         "assignment": ASSIGNMENTS[11][0]},
        {"assignment_id": 1002, "missing": True, "workflow_state": "unsubmitted", "score": None},
    ],
    12: [],
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        parts = urllib.parse.urlsplit(self.path)
        STATE["requests"].append({"method": self.command, "path": parts.path,
                                  "query": urllib.parse.parse_qs(parts.query),
                                  "auth": self.headers.get("Authorization")})
        return parts

    def do_POST(self):  # noqa: N802 - should never be called
        self._record()
        self._send(500, {"error": "POST received"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self):  # noqa: N802
        parts = self._record()
        path, q = parts.path, urllib.parse.parse_qs(parts.query)
        host = "http://%s:%d" % self.server.server_address[:2]
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, {"errors": [{"message": "Invalid access token."}]})
        if path in STATE["overrides"]:
            code, body, headers = STATE["overrides"][path]
            return self._send(code, body, {k: v.replace("{host}", host) for k, v in headers.items()})
        if path == "/api/v1/users/self":
            return self._send(200, {"id": 100, "name": "Pat Parent"})
        if path == "/api/v1/users/self/observees":
            return self._send(200, STATE["observees"] or default_observees())
        if path == "/api/v1/users/200/courses":
            if q.get("page") == ["2"]:
                return self._send(200, [COURSE_ENG, COURSE_LOCKED])
            nxt = "<%s/api/v1/users/200/courses?page=2&per_page=100>; rel=\"next\"" % host
            return self._send(200, [COURSE_MATH], {"Link": nxt})
        m = path.split("/")
        if path.startswith("/api/v1/users/200/courses/") and path.endswith("/assignments"):
            return self._send(200, ASSIGNMENTS.get(int(m[6]), []))
        if path.startswith("/api/v1/courses/") and path.endswith("/students/submissions"):
            assert q.get("student_ids[]") == ["200"]
            subs = SUBMISSIONS.get(int(m[4]), [])
            if "graded_since" in q:
                subs = [s for s in subs if s.get("graded_at")]
            return self._send(200, subs)
        if path.startswith("/api/v1/courses/") and path.endswith("/assignment_groups"):
            return self._send(200, [{"id": 1, "name": "Homework", "group_weight": 40},
                                    {"id": 2, "name": "Assessments", "group_weight": 60}])
        if path == "/api/v1/users/200/missing_submissions":
            return self._send(200, [{"id": 1002, "name": "Quiz 1", "course_id": 11, "due_at": iso(-3),
                                     "points_possible": 20, "html_url": "x/1002",
                                     "course": {"name": "Algebra I"}}])
        return self._send(404, {"errors": [{"message": "not found"}]})


class CanvasReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        STATE["requests"].clear()
        STATE["observees"] = None
        STATE["overrides"] = {}
        self.env = {"CANVAS_BASE_URL": self.base, "CANVAS_TOKEN": TOKEN}
        self._cfg = tempfile.TemporaryDirectory()
        self.addCleanup(self._cfg.cleanup)
        self.cfg_dir = self._cfg.name

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        keys = ("CANVAS_BASE_URL", "CANVAS_TOKEN", "CANVAS_STUDENT", "SCHOOL_SKILLS_CONFIG_DIR")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in saved:
                os.environ.pop(k, None)
            os.environ["SCHOOL_SKILLS_CONFIG_DIR"] = self.cfg_dir  # never read a real config file
            os.environ.update(env if env is not None else self.env)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = canvas_read.main(list(argv))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def assert_only_gets(self):
        self.assertTrue(STATE["requests"])
        for r in STATE["requests"]:
            self.assertEqual(r["method"], "GET", r)

    # -- happy paths ---------------------------------------------------------

    def test_check(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertEqual(out["account"]["name"], "Pat Parent")
        self.assertEqual(out["linked_students"], 1)
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_courses_paginates_and_skips_restricted(self):
        code, out, _ = self.run_cli("courses")
        self.assertEqual(code, 0)
        self.assertEqual([c["name"] for c in out["courses"]], ["Algebra I", "English 9"])
        alg = out["courses"][0]
        self.assertEqual(alg["current_score"], 88.5)
        self.assertEqual(alg["grading_period"]["title"], "Q2")
        self.assertEqual(alg["grading_period"]["current_grade"], "A-")
        self.assertTrue(out["courses"][1]["grades_hidden"])
        self.assertEqual(out["student"]["name"], "Casey Doe")
        self.assert_only_gets()

    def test_assignments_merges_submissions_and_comments(self):
        code, out, _ = self.run_cli("assignments", "--course", "11")
        self.assertEqual(code, 0)
        by_id = {a["id"]: a for a in out["assignments"]}
        hw = by_id[1001]
        self.assertEqual(hw["status"], "graded")
        self.assertEqual(hw["percent"], 90.0)
        self.assertTrue(hw["late"])
        self.assertEqual(hw["group"], "Homework")
        self.assertEqual(hw["comments"][0]["comment"], "Nice work, show steps")
        self.assertEqual(by_id[1002]["status"], "missing")
        self.assertEqual(by_id[1003]["status"], "not_submitted")
        self.assertEqual(out["assignment_groups"][1]["weight"], 60)

    def test_assignments_status_filter(self):
        _, out, _ = self.run_cli("assignments", "--course", "11", "--status", "missing")
        self.assertEqual([a["name"] for a in out["assignments"]], ["Quiz 1"])

    def test_upcoming_window(self):
        _, out, _ = self.run_cli("upcoming", "--days", "14")
        self.assertEqual([u["name"] for u in out["upcoming"]], ["HW 2", "Essay"])
        self.assertTrue(out["upcoming"][1]["submitted"])

    def test_missing(self):
        _, out, _ = self.run_cli("missing")
        self.assertEqual(out["missing"][0]["course"], "Algebra I")

    def test_recent(self):
        _, out, _ = self.run_cli("recent", "--days", "7")
        self.assertEqual(len(out["recently_graded"]), 1)
        self.assertEqual(out["recently_graded"][0]["course"], "Algebra I")
        sub_reqs = [r for r in STATE["requests"] if r["path"].endswith("/students/submissions")]
        self.assertTrue(all("graded_since" in r["query"] for r in sub_reqs))

    def test_summary(self):
        code, out, _ = self.run_cli("--compact", "summary")
        self.assertEqual(code, 0)
        for key in ("courses", "missing", "upcoming", "recently_graded"):
            self.assertIn(key, out)
        self.assertEqual(out["source"], "canvas")
        self.assert_only_gets()
        self.assertTrue(all(r["auth"] == "Bearer " + TOKEN for r in STATE["requests"]))

    # -- student selection ---------------------------------------------------

    def test_multiple_students_require_selection(self):
        STATE["observees"] = default_observees() + [{"id": 201, "name": "Riley Doe", "short_name": "Riley"}]
        code, _, err = self.run_cli("courses")
        self.assertEqual(code, 1)
        self.assertIn("Riley Doe", err)
        code, out, _ = self.run_cli("courses", "--student", "casey")
        self.assertEqual(code, 0)
        self.assertEqual(out["student"]["id"], 200)
        code, _, err = self.run_cli("courses", "--student", "doe")
        self.assertEqual(code, 1)
        self.assertIn("several", err)

    def test_no_linked_students(self):
        STATE["observees"] = []
        STATE["overrides"]["/api/v1/users/self/observees"] = (200, [], {})
        code, _, err = self.run_cli("summary")
        self.assertEqual(code, 1)
        self.assertIn("No students are linked", err)

    # -- safety ----------------------------------------------------------------

    def test_pagination_link_off_allowlist_is_refused(self):
        STATE["overrides"]["/api/v1/users/self/observees"] = (
            200, default_observees(), {"Link": "<{host}/api/v1/conversations?page=2>; rel=\"next\""})
        code, _, err = self.run_cli("students")
        self.assertEqual(code, 1)
        self.assertIn("non-allowlisted", err)
        self.assertFalse(any(r["path"] == "/api/v1/conversations" for r in STATE["requests"]))

    def test_pagination_link_to_other_host_is_refused(self):
        STATE["overrides"]["/api/v1/users/self/observees"] = (
            200, default_observees(),
            {"Link": "<https://evil.example.com/api/v1/users/self/observees?page=2>; rel=\"next\""})
        code, _, err = self.run_cli("students")
        self.assertEqual(code, 1)
        self.assertIn("other than CANVAS_BASE_URL", err)

    def test_redirect_not_followed(self):
        STATE["overrides"]["/api/v1/users/self"] = (
            302, {}, {"Location": "https://evil.example.com/steal"})
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("redirect", err)
        self.assertNotIn(TOKEN, err)

    def test_bad_token_message_has_no_token(self):
        code, _, err = self.run_cli("check", env={"CANVAS_BASE_URL": self.base, "CANVAS_TOKEN": "wrong-token"})
        self.assertEqual(code, 1)
        self.assertIn("401", err)
        self.assertNotIn("wrong-token", err)

    def test_allowlist_rejects_write_style_paths(self):
        client = canvas_read.CanvasClient("https://school.instructure.com", "t")
        for bad in (
            "https://school.instructure.com/api/v1/conversations",
            "https://school.instructure.com/api/v1/courses/11/assignments/5/submissions",
            "https://school.instructure.com/api/v1/users/self/../../accounts",
            "https://school.instructure.com/api/v1/users/self/observees/200",
            "https://school.instructure.com/api/v1/courses/11%2F..%2Fx/students/submissions",
            "https://school.instructure.com.evil.com/api/v1/users/self",
        ):
            with self.assertRaises(canvas_read.ReadOnlyViolation, msg=bad):
                client._check_url(bad)
        client._check_url("https://school.instructure.com/api/v1/courses/11/students/submissions?student_ids[]=1")

    def test_requires_https_for_remote_hosts(self):
        with self.assertRaises(canvas_read.CanvasError):
            canvas_read.CanvasClient("http://school.instructure.com", "t")
        c = canvas_read.CanvasClient("school.instructure.com/login/canvas", "t")
        self.assertEqual(c.base, "https://school.instructure.com")

    def test_missing_config(self):
        code, _, err = self.run_cli("check", env={})
        self.assertEqual(code, 1)
        self.assertIn("CANVAS_BASE_URL", err)

    def test_no_raw_request_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                canvas_read.build_parser().parse_args(["get", "/api/v1/conversations"])

    # -- config file -----------------------------------------------------------

    def write_config(self, text, mode=0o600):
        path = os.path.join(self.cfg_dir, "canvas.env")
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def test_reads_config_file(self):
        self.write_config('# comment\nCANVAS_BASE_URL=%s\nexport CANVAS_TOKEN="%s"\n' % (self.base, TOKEN))
        code, out, err = self.run_cli("check", env={})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["linked_students"], 1)

    def test_environment_overrides_config_file(self):
        self.write_config("CANVAS_BASE_URL=%s\nCANVAS_TOKEN=wrong-token\n" % self.base)
        code, _, err = self.run_cli("check", env={"CANVAS_TOKEN": TOKEN})
        self.assertEqual(code, 0, err)

    def test_refuses_config_readable_by_others(self):
        path = self.write_config("CANVAS_BASE_URL=%s\nCANVAS_TOKEN=%s\n" % (self.base, TOKEN), mode=0o644)
        code, _, err = self.run_cli("check", env={})
        self.assertEqual(code, 1)
        self.assertIn("chmod 600 %s" % path, err)
        self.assertNotIn(TOKEN, err)
        self.assertEqual(STATE["requests"], [])

    def test_configure_refuses_without_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("configure", env={})
        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", err)
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, "canvas.env")))

    def test_write_env_file_is_private(self):
        path = os.path.join(self.cfg_dir, "sub", "canvas.env")
        canvas_read._write_env_file(path, {"CANVAS_BASE_URL": "https://x.example", "CANVAS_TOKEN": "t", "CANVAS_STUDENT": ""})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(canvas_read._read_env_file(path, canvas_read.CanvasError),
                         {"CANVAS_BASE_URL": "https://x.example", "CANVAS_TOKEN": "t"})


if __name__ == "__main__":
    unittest.main()
