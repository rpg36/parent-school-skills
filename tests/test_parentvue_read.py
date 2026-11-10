"""Tests for parentvue_read.py against a local mock of the ParentVUE (New) JSON API.

Run: python3 -m unittest discover -s tests -v
"""

import base64
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "parentvue-grades", "scripts"))
import parentvue_read as pv  # noqa: E402

USER, PASSWORD = "parent@example.com", 's3cret:&"pw'
TODAY = dt.date.today()
DOC_GU = "1A2B3C4D-1111-2222-3333-444455556666"
PDF = b"%PDF-1.4 fake report card"


def us(d):
    return "%02d/%02d/%d" % (d.month, d.day, d.year)


DATA = {
    # Parent-account shape verified live (live district, 2026-11): children.childrenList[].
    "GetChildListData": {"children": {"allModules": [{"name": "Grade Book", "module": "GB", "isEnabled": "Y"}],
                                      "childrenList": [
        {"childIntID": 0, "ID": 0, "studentGU": "G", "childName": "Casey Doe", "childFirstName": "Casey",
         "organizationName": "Example High", "grade": "9", "photo": "AAAA", "allModules": []}],
        "showGradeBookModule": True}},
    "Gradebook": {"traditionalGradebook": {
        "type": "Traditional", "errorMessage": None,
        "reportingPeriods": [
            {"index": "0", "gradePeriod": "Quarter 1", "startDate": "09/02/2026", "endDate": "11/01/2026"},
            {"index": "1", "gradePeriod": "Quarter 2", "startDate": "11/04/2026", "endDate": "01/24/2027"}],
        "reportingPeriod": {"gradePeriod": "Quarter 2", "startDate": "11/04/2026", "endDate": "01/24/2027"},
        "courses": [
            {"period": "1", "title": "Algebra I (MAT1010)", "room": "204", "staff": "Lee, Ann",
             "staffEMail": "alee@example.org", "marks": [{
                 "markName": "Q2", "calculatedScoreString": "B+", "calculatedScoreRaw": "88.5",
                 "gradeCalculationSummary": {"assignmentGradeCalc": [
                     {"type": "Assessments", "weight": "60%", "points": "85.00", "pointsPossible": "100.00",
                      "weightedPct": "51%", "calculatedMark": "B"},
                     {"type": "TOTAL", "weight": "100%", "points": "185", "pointsPossible": "210",
                      "weightedPct": "88.5%", "calculatedMark": "B+"}]},
                 "assignments": [
                     {"measure": "Quiz 1", "type": "Assessments", "date": "11/05/2026", "dueDate": "11/05/2026",
                      "score": "17 out of 20.0000", "points": "17.00 / 20.0000", "notes": ""},
                     {"measure": "HW 3", "type": "Classwork", "date": "11/06/2026", "dueDate": "11/06/2026",
                      "score": "Not Graded", "points": "10.0000 Points Possible", "notes": "Missing"},
                     {"measure": "Project", "type": "Assessments", "dueDate": "11/20/2026",
                      "score": "Not Due", "points": "50.0000 Points Possible", "notes": None}],
                 "assignmentsSinceLastAccess": []}]},
            {"period": "2", "title": "English 9", "room": "110", "staff": "Park, Jo",
             "staffEMail": "jpark@example.org",
             "marks": [{"markName": "Q2", "calculatedScoreString": "A", "calculatedScoreRaw": "95.0",
                        "assignments": []}]}]},
        "standardsGradebook": None},
    # Daily-attendance shape verified live: int totals, empty periods[].
    "GetStudentAttendanceList": {
        "dailyAttendance": {
            "type": "Daily", "isTwiceDailyAttendanceType": False, "homeroomTeacher": None,
            "absences": [
                {"absenceDate": us(TODAY - dt.timedelta(days=3)), "reason": "Excused", "reason2": "",
                 "note": "", "dailyIconName": "", "codeAllDayReasonType": "Excused",
                 "codeAllDayDescription": "Excused Absence", "periods": []},
                {"absenceDate": us(TODAY - dt.timedelta(days=90)), "reason": "Unexcused", "reason2": "",
                 "note": "", "codeAllDayDescription": "Unexcused Absence", "periods": []}],
            "totalExcused": 1, "totalTardies": 0, "totalUnexcused": 1, "totalActivities": 0,
            "totalUnexcusedTardies": 0},
        "periodAttendance": None},
    "GetStudentAttendanceList:period": {
        "periodAttendance": {
            "absences": [
                {"absenceDate": us(TODAY - dt.timedelta(days=5)), "reason": "Excused", "note": "Doctor",
                 "codeAllDayDescription": "Excused Absence", "periods": [
                     {"number": 1, "name": "Excused", "reason": "Excused", "course": "Algebra I", "staff": "Lee, Ann"},
                     {"number": 2, "name": "", "reason": "", "course": "", "staff": ""}]},
                {"absenceDate": us(TODAY - dt.timedelta(days=60)), "reason": "Tardy", "periods": [
                    {"number": 2, "name": "Tardy", "reason": "Tardy", "course": "English 9", "staff": "Park, Jo"}]}],
            "totalExcused": [{"number": 1, "total": 1}, {"number": 2, "total": 0}],
            "totalTardies": [{"number": 2, "total": 1}],
            "totalUnexcused": [{"number": 1, "total": 0}]},
        "dailyAttendance": None},
    "StudentClassList": {"studentClassScheduleForAllTerms": {
        "termIndex": 0, "termIndexName": "Semester 1",
        "termLists": [{"termIndex": 0, "termName": "Semester 1", "beginDate": "09/02/2026", "endDate": "01/24/2027"},
                      {"termIndex": 1, "termName": "Semester 2", "beginDate": "01/27/2027", "endDate": "06/12/2027"}],
        "studentClassScheduleForTerms": [
            {"thisTermIndex": 0, "classLists": [
                {"period": "1", "courseTitle": "Algebra I", "roomName": "204", "teacher": "Ann Lee",
                 "teacherEmail": "alee@example.org", "excludePVUE": False},
                {"period": "2", "courseTitle": "English 9", "roomName": "110", "teacher": "Jo Park",
                 "teacherEmail": "jpark@example.org", "excludePVUE": False},
                {"period": "9", "courseTitle": "Hidden", "excludePVUE": True}]},
            {"thisTermIndex": 1, "classLists": [{"period": "1", "courseTitle": "Geometry"}]}]}},
    "GetStudentDocuments": {"studentDocuments": {"studentDocumentDatas": [
        {"documentGU": DOC_GU, "documentFileName": DOC_GU + ".pdf", "documentDate": "11/08/2026",
         "documentType": "Report Card", "documentComment": "Q1"},
        {"documentGU": "9A2B3C4D-1111-2222-3333-444455556666", "documentDate": "10/01/2026",
         "documentType": "Parent Letter"}]}},
    "GetStudentDocumentContent": {"studentAttachedDocumentData": {"documentDatas": [
        {"documentGU": DOC_GU, "docDate": "11/08/2026", "fileName": "../../x.pdf", "docType": "Report Card",
         "base64Code": base64.b64encode(PDF).decode()}]}},
}

STATE = {"requests": [], "mode": None, "tokens": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        STATE["requests"].append({"http": "GET", "path": self.path})
        self._json({}, 405)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        body = json.loads(raw) if raw else None
        method = self.path.rsplit("/", 1)[-1]
        inner = None
        if isinstance(body, dict) and "arguments" in body:
            inner = json.loads(body["arguments"]["request"])
        STATE["requests"].append({"http": "POST", "path": self.path, "method": method,
                                  "auth": self.headers.get("Authorization"),
                                  "app": self.headers.get("AppNameOSAndVersion"),
                                  "ua": self.headers.get("User-Agent"), "inner": inner, "body": body})
        if not self.path.startswith("/api/v1/mobile/PXPWebServices/"):
            return self._json({}, 404)
        if STATE["mode"] == "redirect":
            return self._json({}, 302, {"Location": "https://evil.example.com/x"})
        if method == "AttemptLogin":
            expected = "Basic " + base64.b64encode(("%s:%s" % (USER, PASSWORD)).encode()).decode()
            if self.headers.get("Authorization") != expected or inner.get("userType") != "parent":
                return self._json({"error": {"code": "1", "message": "Invalid user id or password"}, "data": None})
            STATE["tokens"] += 1
            return self._json({"access_token": "acc%d" % STATE["tokens"], "refresh_token": "ref",
                               "token_type": None, "expires_in": None, "scope": None})
        if method == "RefreshToken":
            if self.headers.get("Authorization") != "Bearer ref" or body != {}:
                return self._json({}, 401)
            return self._json({"access_token": "fresh", "refresh_token": "ref2"})
        auth = self.headers.get("Authorization") or ""
        if STATE["mode"] == "expire-once" and auth == "Bearer acc1":
            return self._json({}, 401)
        if not auth.startswith("Bearer "):
            return self._json({}, 401)
        if STATE["mode"] == "no-gradebook" and method == "Gradebook":
            return self._json({"error": {"code": "2100", "message": "Grade Book data not available for this school",
                                         "stackTrace": None}, "data": None})
        if STATE["mode"] == "canvas-only-gradebook" and method == "Gradebook":
            return self._json({"error": None, "data": {"traditionalGradebook": None, "standardsGradebook": {
                "type": "Standards", "subjects": [], "homeroomTeacher": "X",
                "errorMessage": '<b><a href="https://district.instructure.com/grades" target="_blank">Access Canvas </a>'
                                "</b> to view your student's grades.",
                "reportingPeriods": [{"index": "0", "gradePeriod": "Q1", "startDate": "09/02/2026",
                                      "endDate": "11/01/2026"}],
                "reportingPeriod": {"index": None, "gradePeriod": "Q2", "startDate": "11/04/2026",
                                    "endDate": "01/24/2027"}}}})
        if STATE["mode"] == "period-attendance" and method == "GetStudentAttendanceList":
            return self._json({"error": None, "data": DATA["GetStudentAttendanceList:period"]})
        if method in DATA:
            return self._json({"error": None, "data": DATA[method]})
        return self._json({"error": {"code": "500", "message": "unknown"}, "data": None})


class ParentVueTests(unittest.TestCase):
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
        STATE["mode"] = None
        STATE["tokens"] = 0
        self.env = {"PARENTVUE_BASE_URL": self.base, "PARENTVUE_USERNAME": USER, "PARENTVUE_PASSWORD": PASSWORD}
        self._cfg = tempfile.TemporaryDirectory()
        self.addCleanup(self._cfg.cleanup)
        self.cfg_dir = self._cfg.name

    def run_cli(self, *argv, env=None):
        keys = ("PARENTVUE_BASE_URL", "PARENTVUE_USERNAME", "PARENTVUE_PASSWORD", "PARENTVUE_STUDENT",
                "PARENTVUE_DOWNLOAD_DIR", "SCHOOL_SKILLS_CONFIG_DIR")
        saved = {k: os.environ.get(k) for k in keys}
        out, err = io.StringIO(), io.StringIO()
        try:
            for k in keys:
                os.environ.pop(k, None)
            os.environ["SCHOOL_SKILLS_CONFIG_DIR"] = self.cfg_dir  # never read a real config file
            os.environ.update(self.env if env is None else env)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = pv.main(list(argv))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def calls(self):
        return [r["method"] for r in STATE["requests"]]

    # -- happy paths ---------------------------------------------------------

    def test_check_logs_in_once_with_parent_basic_auth(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertEqual(out["students"], ["Casey Doe"])
        self.assertEqual(self.calls(), ["AttemptLogin", "GetChildListData"])
        login = STATE["requests"][0]
        self.assertEqual(login["inner"], {"userID": None, "password": None, "userType": "parent"})
        self.assertEqual(login["app"], "ParentVUE|Android|1.9.16")
        self.assertEqual(login["ua"], "ksoap")
        self.assertEqual(STATE["requests"][1]["auth"], "Bearer acc1")
        self.assertEqual(STATE["requests"][1]["inner"], {"legacyAppRequest": False, "secondaryLogin": False})
        self.assertNotIn(PASSWORD, json.dumps(out))

    def test_students_omits_photo_and_guid(self):
        _, out, _ = self.run_cli("students")
        self.assertEqual(out["students"][0], {"index": 0, "name": "Casey Doe", "first_name": "Casey",
                                              "grade": "9", "school": "Example High"})

    def test_grades(self):
        code, out, _ = self.run_cli("grades")
        self.assertEqual(code, 0)
        self.assertTrue(out["available"])
        self.assertEqual(out["reporting_period"]["name"], "Quarter 2")
        self.assertEqual(out["reporting_periods"][1], {"index": 1, "name": "Quarter 2",
                                                       "start": "2026-11-04", "end": "2027-01-24"})
        alg = out["courses"][0]
        self.assertEqual((alg["name"], alg["code"], alg["teacher"]), ("Algebra I", "MAT1010", "Lee, Ann"))
        mark = alg["marks"][0]
        self.assertEqual((mark["grade"], mark["score"], mark["missing_count"]), ("B+", 88.5, 1))
        self.assertEqual(mark["categories"][0]["weight"], "60%")
        self.assertNotIn("assignments", mark)
        self.assertEqual(STATE["requests"][-1]["inner"], {"reportPeriod": "", "concurrentSchOrgYearGU": "",
                                                          "childIntID": 0, "languageCode": "en"})

    def test_grades_period_param(self):
        self.run_cli("grades", "--period", "0")
        self.assertEqual(STATE["requests"][-1]["inner"]["reportPeriod"], "0")

    def test_gradebook_unavailable_is_not_a_failure(self):
        STATE["mode"] = "no-gradebook"
        code, out, _ = self.run_cli("grades")
        self.assertEqual(code, 0)
        self.assertFalse(out["available"])
        self.assertIn("2100", out["note"])

    def test_assignments(self):
        _, out, _ = self.run_cli("assignments", "--course", "1")
        a = {x["name"]: x for x in out["courses"][0]["marks"][0]["assignments"]}
        self.assertEqual((a["Quiz 1"]["score"], a["Quiz 1"]["points_possible"], a["Quiz 1"]["percent"]),
                         (17.0, 20.0, 85.0))
        self.assertEqual([a[n]["status"] for n in ("Quiz 1", "HW 3", "Project")], ["graded", "missing", "not_graded"])
        self.assertEqual(a["Project"]["points_possible"], 50.0)
        _, out, _ = self.run_cli("assignments", "--course", "algebra", "--status", "missing")
        self.assertEqual([x["name"] for x in out["courses"][0]["marks"][0]["assignments"]], ["HW 3"])
        code, _, err = self.run_cli("assignments", "--course", "chemistry")
        self.assertEqual(code, 1)
        self.assertIn("English 9", err)

    def test_attendance_daily(self):
        _, out, _ = self.run_cli("attendance")
        att = out["attendance"]
        self.assertEqual(att["tracking"], ["daily"])
        self.assertEqual(att["school_year_totals"], {"daily": {
            "excused": 1, "unexcused": 1, "tardies": 0, "unexcused_tardies": 0, "activities": 0}})
        self.assertEqual(att["days_with_absences"], 2)
        self.assertEqual(att["absences"][0]["kind"], "daily")
        self.assertEqual(att["absences"][0]["description"], "Excused Absence")
        _, out, _ = self.run_cli("attendance", "--days", "30")
        self.assertEqual(out["attendance"]["days_with_absences"], 1)

    def test_attendance_period(self):
        STATE["mode"] = "period-attendance"
        _, out, _ = self.run_cli("attendance")
        att = out["attendance"]
        self.assertEqual(att["school_year_totals"], {"period": {"excused": 1, "unexcused": 0, "tardies": 1}})
        self.assertEqual(len(att["absences"][0]["periods"]), 1)
        self.assertEqual(att["absences"][0]["periods"][0]["period"], "1")

    def test_gradebook_that_points_to_canvas(self):
        STATE["mode"] = "canvas-only-gradebook"
        code, out, _ = self.run_cli("grades")
        self.assertEqual(code, 0)
        self.assertFalse(out["available"])
        self.assertEqual(out["note"], "Access Canvas (https://district.instructure.com/grades) to view your student's grades.")
        self.assertEqual(out["reporting_period"]["name"], "Q2")
        code, out, _ = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertEqual((out["courses"], out["missing"]), ([], []))
        self.assertIn("Canvas", out["note"])

    def test_schedule_current_term_only(self):
        _, out, _ = self.run_cli("schedule")
        s = out["schedule"]
        self.assertEqual(s["term"], "Semester 1")
        self.assertEqual([c["course"] for c in s["classes"]], ["Algebra I", "English 9"])
        self.assertEqual(s["terms"][1]["start"], "2027-01-27")
        self.assertEqual(STATE["requests"][-1]["inner"]["loadAllTerms"], True)

    def test_documents_and_download(self):
        _, out, _ = self.run_cli("documents", "--type", "report card")
        self.assertEqual([d["document_id"] for d in out["documents"]], [DOC_GU])
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self.run_cli("document", "--id", DOC_GU, "--out", d)
            self.assertEqual(code, 0)
            doc = out["document"]
            self.assertTrue(doc["is_pdf"])
            self.assertEqual(os.path.basename(doc["path"]), "2026-11-08_report-card_1A2B3C4D.pdf")
            with open(doc["path"], "rb") as f:
                self.assertEqual(f.read(), PDF)
            self.assertEqual(os.stat(doc["path"]).st_mode & 0o777, 0o600)

    def test_summary(self):
        code, out, _ = self.run_cli("--compact", "summary")
        self.assertEqual(code, 0)
        self.assertEqual([m["name"] for m in out["missing"]], ["HW 3"])
        self.assertEqual(out["attendance"]["days_with_absences"], 1)
        self.assertNotIn("assignments", out["courses"][0]["marks"][0])
        self.assertEqual(self.calls(), ["AttemptLogin", "GetChildListData", "Gradebook", "GetStudentAttendanceList"])

    def test_debug_shape_has_no_values(self):
        _, out, _ = self.run_cli("debug-shape", "--method", "Gradebook")
        dumped = json.dumps(out["shape"])
        self.assertIn("traditionalGradebook.courses[].marks[].assignments[].measure", dumped)
        for value in ("88.5", "Algebra", "alee@example.org", "Quiz 1", "Missing"):
            self.assertNotIn(value, dumped)

    def test_token_refresh_on_401(self):
        STATE["mode"] = "expire-once"
        code, out, _ = self.run_cli("grades")
        self.assertEqual(code, 0)
        self.assertIn("RefreshToken", self.calls())
        self.assertEqual(self.calls().count("AttemptLogin"), 1)

    # -- safety ----------------------------------------------------------------

    def test_only_allowlisted_methods_and_paths(self):
        for cmd in (["summary"], ["schedule"], ["documents"], ["students"]):
            self.run_cli(*cmd)
        for r in STATE["requests"]:
            self.assertEqual(r["http"], "POST")
            self.assertTrue(r["path"].startswith("/api/v1/mobile/PXPWebServices/"))
            self.assertIn(r["method"], pv.METHODS)

    def test_client_refuses_write_methods_and_params(self):
        c = pv.ParentVueClient("https://xx-example-psv.edupoint.com", "u", "p")
        for method in ("GetSynergyMailSaveNewMessage", "UpdateStudentAbsence", "UpdateMyAccountData",
                       "GetStudentInfoData", "UpdateStudentHWNotes", "GetHealthData"):
            with self.assertRaises(pv.ReadOnlyViolation):
                c.call(method, {})
        with self.assertRaises(pv.ReadOnlyViolation):
            c.call("Gradebook", {"childIntID": 0, "extra": "x"})
        with self.assertRaises(pv.ReadOnlyViolation):
            c.call("Gradebook", {"childIntID": "0; drop"})
        with self.assertRaises(pv.ReadOnlyViolation):
            c.call("GetStudentDocumentContent", {"childIntID": 0, "documentGU": "../etc"})
        self.assertEqual(STATE["requests"], [])

    def test_bad_password_single_attempt_not_leaked(self):
        env = dict(self.env, PARENTVUE_PASSWORD="wrong-pass-123")
        code, _, err = self.run_cli("summary", env=env)
        self.assertEqual(code, 1)
        self.assertIn("rejected the login", err)
        self.assertNotIn("wrong-pass-123", err)
        self.assertEqual(self.calls(), ["AttemptLogin"])

    def test_redirect_not_followed(self):
        STATE["mode"] = "redirect"
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("redirect", err)
        self.assertEqual(len(STATE["requests"]), 1)

    def test_https_required_and_base_normalized(self):
        with self.assertRaises(pv.PVError):
            pv.ParentVueClient("http://xx-district-psv.edupoint.com", "u", "p")
        c = pv.ParentVueClient("xx-district-psv.edupoint.com/Home_PXP2.aspx", "u", "p")
        self.assertEqual(c._url("Gradebook"),
                         "https://xx-district-psv.edupoint.com/api/v1/mobile/PXPWebServices/Gradebook")

    def test_missing_config(self):
        code, _, err = self.run_cli("check", env={})
        self.assertEqual(code, 1)
        self.assertIn("PARENTVUE_BASE_URL", err)

    def test_no_generic_call_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pv.build_parser().parse_args(["call", "GetSynergyMailSaveNewMessage"])
            with self.assertRaises(SystemExit):
                pv.build_parser().parse_args(["debug-shape", "--method", "GetStudentInfoData"])

    # -- config file -----------------------------------------------------------

    def write_config(self, text, mode=0o600):
        path = os.path.join(self.cfg_dir, "parentvue.env")
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def test_reads_config_file_with_quoted_password(self):
        self.write_config("PARENTVUE_BASE_URL=%s\nPARENTVUE_USERNAME=%s\nPARENTVUE_PASSWORD='%s'\n"
                          % (self.base, USER, PASSWORD))
        code, out, err = self.run_cli("check", env={})
        self.assertEqual(code, 0, err)
        self.assertEqual(out["students"], ["Casey Doe"])

    def test_refuses_config_readable_by_others(self):
        self.write_config("PARENTVUE_BASE_URL=%s\nPARENTVUE_USERNAME=u\nPARENTVUE_PASSWORD=p\n" % self.base,
                          mode=0o640)
        code, _, err = self.run_cli("check", env={})
        self.assertEqual(code, 1)
        self.assertIn("chmod 600", err)
        self.assertEqual(STATE["requests"], [])

    def test_env_file_round_trips_tricky_passwords(self):
        path = os.path.join(self.cfg_dir, "parentvue.env")
        for secret in (PASSWORD, '"quoted"', "'single'", "  spaces  ", "a=b#c", "x'y\"z"):
            pv._write_env_file(path, {"PARENTVUE_PASSWORD": secret})
            self.assertEqual(pv._read_env_file(path, pv.PVError)["PARENTVUE_PASSWORD"], secret)

    def test_configure_refuses_without_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("configure", env={})
        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", err)
        self.assertEqual(STATE["requests"], [])


if __name__ == "__main__":
    unittest.main()
