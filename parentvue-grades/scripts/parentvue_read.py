#!/usr/bin/env python3
"""Read-only ParentVUE (Edupoint Synergy) client for a parent account.

Reads a linked student's gradebook (marking-period grades, category weights,
assignments), attendance, class schedule and documents (report cards), and
prints normalized JSON on stdout.

ParentVUE has no public API. This uses the JSON API of the current
"ParentVUE (New)" mobile app (the legacy SOAP service is retired):

    POST {base}/api/v1/mobile/PXPWebServices/<Method>
    body: {"arguments": {"request": "<JSON string of parameters>"}}

Login is AttemptLogin with HTTP Basic credentials, which returns a bearer
token used for the other calls in the same run. Tokens are never written to
disk; each command logs in once.

Read-only by construction:
  * Requests go only to {base}/api/v1/mobile/PXPWebServices/<Method> over HTTPS.
  * <Method> must be in METHODS, which holds only login/refresh and read
    methods. Mail, acknowledgements, absence reporting, profile/contact
    updates, uploads, and every other write method are not callable.
  * Each method's request fields are fixed; values are type/pattern checked.
  * Redirects are never followed, so credentials cannot be sent elsewhere.
  * There is no command to call an arbitrary method.

Configuration. Environment variables win; otherwise they are read from a
private KEY=VALUE file (mode 0600) that `parentvue_read.py configure` writes:
  ${SCHOOL_SKILLS_CONFIG_DIR:-${XDG_CONFIG_HOME:-~/.config}/school-skills}/parentvue.env

  PARENTVUE_BASE_URL   e.g. https://yourdistrict-psv.edupoint.com
  PARENTVUE_USERNAME   ParentVUE login
  PARENTVUE_PASSWORD   ParentVUE password
  PARENTVUE_STUDENT    optional default student (name fragment or index)
  PARENTVUE_DOWNLOAD_DIR  optional folder for `document` downloads

Standard library only; Python 3.8+.
"""

import argparse
import base64
import datetime as dt
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

VERSION = "2.2.0"
API_PATH = "/api/v1/mobile/PXPWebServices/"
# The JSON API identifies the calling app with these headers. They mirror the
# official app because the server expects them; nothing else is spoofed.
APP_HEADER = "ParentVUE|Android|1.9.16"
USER_AGENT = "ksoap"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
CONFIG_FILE = "parentvue.env"
CONFIG_KEYS = ("PARENTVUE_BASE_URL", "PARENTVUE_USERNAME", "PARENTVUE_PASSWORD", "PARENTVUE_STUDENT",
               "PARENTVUE_DOWNLOAD_DIR")

_INT = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1000  # noqa: E731
_STR = lambda pattern: (lambda v: isinstance(v, str) and re.fullmatch(pattern, v) is not None)  # noqa: E731
_BOOL = lambda v: isinstance(v, bool)  # noqa: E731
_GUID = r"\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?"

# method -> {field: validator}. Only these methods/fields can ever be sent.
METHODS = {
    "AttemptLogin": None,  # special-cased: fixed body, Basic auth
    "RefreshToken": None,  # special-cased: empty body, Bearer refresh token
    "GetChildListData": {"legacyAppRequest": _BOOL, "secondaryLogin": _BOOL},
    "Gradebook": {"reportPeriod": _STR(r"\d{0,3}"), "concurrentSchOrgYearGU": _STR(r"(%s)?" % _GUID),
                  "childIntID": _INT, "languageCode": _STR(r"[a-z]{2}")},
    "StudentClassList": {"childIntID": _INT, "loadAllTerms": _BOOL, "conSchOrgYearGU": _STR(r"(%s)?" % _GUID),
                         "conSchTermIndex": _STR(r"-?\d{1,3}"), "termIndex": _STR(r"-?\d{1,3}")},
    "GetStudentAttendanceList": {"childIntID": _INT},
    "GetStudentDocuments": {"childIntID": _INT, "languageCode": _STR(r"[a-z]{2}")},
    "GetStudentDocumentContent": {"childIntID": _INT, "documentGU": _STR(_GUID)},
}
DATA_METHODS = sorted(m for m, spec in METHODS.items() if spec is not None)


class PVError(Exception):
    """A user-facing error. The message never contains the password or token."""


class ReadOnlyViolation(PVError):
    """Raised when a call would leave the read-only allowlist."""


class FeatureUnavailable(PVError):
    """The school has this area turned off (Synergy error 2100 / 400)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PVError(
            "ParentVUE answered with a redirect to %s. Redirects are not followed "
            "(so credentials are never sent to another address). If that is the real "
            "ParentVUE address, set PARENTVUE_BASE_URL to it." % urllib.parse.urlsplit(newurl).netloc)


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class ParentVueClient:
    def __init__(self, base_url, username, password, timeout=45):
        if not base_url:
            raise PVError("PARENTVUE_BASE_URL is not set (e.g. https://yourdistrict-psv.edupoint.com). "
                          "Run `parentvue_read.py configure` in a terminal to set it up.")
        if not username or not password:
            raise PVError("PARENTVUE_USERNAME and PARENTVUE_PASSWORD must both be set. "
                          "Run `parentvue_read.py configure` in a terminal to set them up.")
        if "://" not in base_url:
            base_url = "https://" + base_url
        parts = urllib.parse.urlsplit(base_url.strip())
        host = (parts.hostname or "").lower()
        if not host:
            raise PVError("PARENTVUE_BASE_URL is not a valid URL.")
        if parts.scheme != "https" and not (parts.scheme == "http" and host in LOCAL_HOSTS):
            raise PVError("PARENTVUE_BASE_URL must use https://.")
        self.base = "%s://%s" % (parts.scheme, parts.netloc.lower())
        self._basic = base64.b64encode(("%s:%s" % (username, password)).encode("utf-8")).decode("ascii")
        self._access = None
        self._refresh = None
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _url(self, method):
        if method not in METHODS:
            raise ReadOnlyViolation("Refusing non-allowlisted ParentVUE method %r." % method)
        return self.base + API_PATH + method

    @staticmethod
    def _validated(method, params):
        spec = METHODS.get(method)
        if spec is None:
            raise ReadOnlyViolation("Refusing non-allowlisted ParentVUE method %r." % method)
        for key, value in params.items():
            check = spec.get(key)
            if check is None:
                raise ReadOnlyViolation("Refusing unknown parameter %r for %s." % (key, method))
            if not check(value):
                raise ReadOnlyViolation("Refusing malformed value for %s.%s." % (method, key))
        return params

    def _post(self, method, body, auth):
        req = urllib.request.Request(self._url(method), data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("AppNameOSAndVersion", APP_HEADER)
        req.add_header("Authorization", auth)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return 401, None
            raise PVError("ParentVUE returned HTTP %d for %s." % (e.code, method))
        except urllib.error.URLError as e:
            raise PVError("Could not reach ParentVUE at %s: %s" % (self.base, e.reason))
        try:
            return 200, json.loads(raw)
        except ValueError:
            raise PVError("ParentVUE returned a non-JSON response for %s." % method)

    def login(self):
        body = {"arguments": {"request": json.dumps({"userID": None, "password": None, "userType": "parent"})}}
        status, data = self._post("AttemptLogin", body, "Basic " + self._basic)
        if status == 401 or not isinstance(data, dict):
            raise PVError("ParentVUE rejected the login. Check PARENTVUE_USERNAME / PARENTVUE_PASSWORD. "
                          "Not retrying, to avoid locking the account.")
        if data.get("access_token"):
            self._access = data["access_token"]
            self._refresh = data.get("refresh_token")
            return
        err = data.get("error") or {}
        message = (err.get("message") if isinstance(err, dict) else str(err)) or "no token returned"
        raise PVError("ParentVUE rejected the login (%s). Not retrying, to avoid locking the account." % message)

    def _refresh_token(self):
        if not self._refresh:
            return False
        status, data = self._post("RefreshToken", {}, "Bearer " + self._refresh)
        if status == 200 and isinstance(data, dict) and data.get("access_token"):
            self._access, self._refresh = data["access_token"], data.get("refresh_token") or self._refresh
            return True
        return False

    def call(self, method, params):
        """Run one read-only method; return its `data` object."""
        body = {"arguments": {"request": json.dumps(self._validated(method, dict(params)))}}
        if self._access is None:
            self.login()
        status, data = self._post(method, body, "Bearer " + self._access)
        if status == 401 and self._refresh_token():
            status, data = self._post(method, body, "Bearer " + self._access)
        if status == 401:
            raise PVError("ParentVUE session expired for %s; run the command again." % method)
        if not isinstance(data, dict):
            raise PVError("ParentVUE returned an unexpected response for %s." % method)
        err = data.get("error")
        if err:
            code = str(err.get("code")) if isinstance(err, dict) else ""
            message = (err.get("message") if isinstance(err, dict) else str(err)) or "unknown error"
            if code in ("2100", "400"):
                raise FeatureUnavailable("%s (code %s)" % (message, code))
            raise PVError("ParentVUE error for %s: %s%s" % (method, message, " (code %s)" % code if code else ""))
        return data.get("data") or {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _g(obj, *names):
    """Case-insensitive get across several candidate keys; '' and whitespace -> None."""
    if not isinstance(obj, dict):
        return None
    lower = {k.lower(): k for k in obj}
    for name in names:
        key = lower.get(name.lower())
        if key is not None:
            v = obj[key]
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    continue
            if v is not None:
                return v
    return None


def _list(obj, *names):
    v = _g(obj, *names)
    if isinstance(v, list):
        return v
    if isinstance(v, dict):  # some lists are wrapped one level deeper
        for inner in v.values():
            if isinstance(inner, list):
                return inner
    return []


def _str(v):
    return None if v is None else str(v)


def _date(value):
    """'9/15/2026' or '2026-09-15...' -> '2026-09-15' (None if unparseable)."""
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", value.strip())
    if m:
        mo, d, y = (int(x) for x in m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    return "%s-%s-%s" % m.groups() if m else None


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def _int(value):
    n = _num(value)
    return int(n) if n is not None else None


def _round(x):
    return None if x is None else round(x, 2)


def _parse_points(points_text, score_text):
    """Returns (score, possible). Handles '9.00 / 10.0000', '10.0000 Points Possible',
    and '9 out of 10.0000'."""
    for text in (points_text, score_text):
        if not isinstance(text, str):
            continue
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:/|out of)\s*(\d+(?:\.\d+)?)", text, re.I)
        if m:
            return float(m.group(1)), float(m.group(2))
    if isinstance(points_text, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*Points?\s*Possible", points_text, re.I)
        if m:
            return None, float(m.group(1))
    return None, None


def _strip_html(text):
    if not isinstance(text, str):
        return text
    text = re.sub(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None


def _split_title(title):
    """'Algebra I (MAT1010)' -> ('Algebra I', 'MAT1010')."""
    if not title:
        return title, None
    m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", title)
    return (m.group(1), m.group(2)) if m else (title, None)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def config_path():
    base = os.environ.get("SCHOOL_SKILLS_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "school-skills")
    return os.path.join(base, CONFIG_FILE)


def _read_env_file(path, error_cls):
    values = {}
    if not os.path.isfile(path):
        return values
    if os.name == "posix" and os.stat(path).st_mode & 0o077:
        raise error_cls("%s can be read by other users. Fix it with: chmod 600 %s" % (path, path))
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key] = value
    return values


def load_config():
    """Settings from the config file, overridden by any set environment variables."""
    cfg = {k: v for k, v in _read_env_file(config_path(), PVError).items() if k in CONFIG_KEYS}
    for key in CONFIG_KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def _write_env_file(path, values):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Written by %s configure. Keep this file private (mode 0600).\n" % os.path.basename(sys.argv[0]))
        for key, value in values.items():
            if value:
                f.write("%s='%s'\n" % (key, value))  # quoted, so the value round-trips exactly
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def configure():
    """Interactive setup for a human at a terminal. Never prints the password."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PVError("`configure` must be run by a person in an interactive terminal, not by an agent.")
    path = config_path()
    current = _read_env_file(path, PVError)
    print("ParentVUE setup. Values are saved to %s (mode 0600)." % path)
    print("Tip: use a password you don't use anywhere else.")
    default_url = current.get("PARENTVUE_BASE_URL", "")
    url = input("ParentVUE address%s: " % (" [%s]" % default_url if default_url else
                                           " (the https://... host of your ParentVUE login page)")).strip()
    url = url or default_url
    default_user = current.get("PARENTVUE_USERNAME", "")
    username = input("ParentVUE username%s: " % (" [%s]" % default_user if default_user else "")).strip()
    username = username or default_user
    password = getpass.getpass("ParentVUE password%s (input hidden): " % (
        " [press Enter to keep the saved one]" if current.get("PARENTVUE_PASSWORD") else ""))
    password = password or current.get("PARENTVUE_PASSWORD", "")
    if any(ord(ch) < 32 for ch in username + password):
        raise PVError("The username or password contains control characters; please re-type it.")
    student = input("Default student (optional, only needed if several are linked)%s: " % (
        " [%s]" % current["PARENTVUE_STUDENT"] if current.get("PARENTVUE_STUDENT") else "")).strip()
    student = student or current.get("PARENTVUE_STUDENT", "")
    client = ParentVueClient(url, username, password)  # validates the address
    values = dict(current)
    values.update({"PARENTVUE_BASE_URL": client.base, "PARENTVUE_USERNAME": username,
                   "PARENTVUE_PASSWORD": password, "PARENTVUE_STUDENT": student})
    _write_env_file(path, {k: values.get(k, "") for k in CONFIG_KEYS})
    print("Saved. Checking the login once (no retries)...")
    result = run(build_parser().parse_args(["check"]), Reader(client))
    result["config"] = path
    return result


# --------------------------------------------------------------------------
# Domain
# --------------------------------------------------------------------------


class Reader:
    def __init__(self, client, default_student=None):
        self.c = client
        self.default_student = default_student
        self.download_dir = None
        self._students = None

    # -- students -----------------------------------------------------------------

    def students(self):
        if self._students is None:
            data = self.c.call("GetChildListData", {"legacyAppRequest": False, "secondaryLogin": False})
            holder = data.get("children") if isinstance(data, dict) else None
            # Parent accounts: children.childrenList[] (verified live against a district, 2026-11).
            # StudentVUE docs show children.childInfos[]; accept both.
            children = _list(holder, "childrenList", "childInfos")
            out = []
            for i, ch in enumerate(children):
                idx = _int(_g(ch, "childIntID"))
                out.append({
                    "index": idx if idx is not None else i,
                    "name": _g(ch, "childName", "name", "studentName"),
                    "first_name": _g(ch, "childFirstName", "firstName"),
                    "grade": _str(_g(ch, "grade", "gradeLevel")),
                    "school": _g(ch, "organizationName", "schoolName"),
                })
            self._students = out
        return self._students

    def resolve_student(self, selector=None):
        selector = selector or self.default_student or None
        students = self.students()
        if not students:
            raise PVError("No students are linked to this ParentVUE account.")
        if selector is None:
            if len(students) == 1:
                return students[0]
            raise PVError("More than one student is linked; pass --student with one of: " +
                          ", ".join("%s (index %d)" % (s["name"], s["index"]) for s in students))
        sel = str(selector).strip().lower()
        if sel.isdigit():
            for s in students:
                if s["index"] == int(sel):
                    return s
        matches = [s for s in students
                   if any(sel in (s.get(k) or "").lower() for k in ("name", "first_name"))]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise PVError("No linked student matches %r. Linked: %s" % (
                selector, ", ".join(s["name"] or "?" for s in students)))
        raise PVError("%r matches several students: %s" % (selector, ", ".join(s["name"] for s in matches)))

    # -- gradebook --------------------------------------------------------------

    def gradebook(self, student, period=None, with_assignments=True):
        try:
            data = self.c.call("Gradebook", {
                "reportPeriod": "" if period is None else str(period), "concurrentSchOrgYearGU": "",
                "childIntID": student["index"], "languageCode": "en"})
        except FeatureUnavailable as e:
            return {"available": False, "note": str(e), "reporting_period": None,
                    "reporting_periods": [], "courses": []}
        gb = _g(data, "traditionalGradebook")
        if not isinstance(gb, dict):
            std = _g(data, "standardsGradebook")
            if isinstance(std, dict):
                # Some districts return an empty standards gradebook whose errorMessage says
                # grades live in Canvas.
                message = _strip_html(_g(std, "errorMessage"))
                note = message or ("Standards-based gradebook returned with %d subjects; not yet parsed."
                                   % len(_list(std, "subjects")))
                periods = [{"index": _int(_g(rp, "index")), "name": _g(rp, "gradePeriod"),
                            "start": _date(_g(rp, "startDate")), "end": _date(_g(rp, "endDate"))}
                           for rp in _list(std, "reportingPeriods")]
                cur = _g(std, "reportingPeriod")
                current = {"name": _g(cur, "gradePeriod"), "start": _date(_g(cur, "startDate")),
                           "end": _date(_g(cur, "endDate"))} if isinstance(cur, dict) else None
                return {"available": False, "note": note, "reporting_period": current,
                        "reporting_periods": periods, "courses": []}
            return {"available": False, "note": "No gradebook returned.", "reporting_period": None,
                    "reporting_periods": [], "courses": []}
        periods = [{
            "index": _int(_g(rp, "index")),
            "name": _g(rp, "gradePeriod", "name"),
            "start": _date(_g(rp, "startDate")),
            "end": _date(_g(rp, "endDate")),
        } for rp in _list(gb, "reportingPeriods")]
        cur = _g(gb, "reportingPeriod")
        current = None if not isinstance(cur, dict) else {
            "name": _g(cur, "gradePeriod", "name"),
            "start": _date(_g(cur, "startDate")),
            "end": _date(_g(cur, "endDate")),
        }
        courses = [_normalize_course(c, with_assignments) for c in _list(gb, "courses")]
        return {"available": True, "reporting_period": current, "reporting_periods": periods,
                "courses": courses, "note": _g(gb, "errorMessage")}

    # -- attendance ---------------------------------------------------------------

    def attendance(self, student, days=None):
        try:
            data = self.c.call("GetStudentAttendanceList", {"childIntID": student["index"]})
        except FeatureUnavailable as e:
            return {"available": False, "note": str(e), "absences": [], "school_year_totals": {}}
        cutoff = (_now().date() - dt.timedelta(days=days)).isoformat() if days else None
        absences = []
        totals = {}
        for section_key in ("periodAttendance", "dailyAttendance"):
            section = _g(data, section_key)
            if not isinstance(section, dict):
                continue
            for a in _list(section, "absences", "absenceList", "attendanceEntries", "entries"):
                date = _date(_g(a, "absenceDate", "date", "attendanceDate"))
                if cutoff and (not date or date < cutoff):
                    continue
                periods = []
                for p in _list(a, "periods", "periodList"):
                    if not (_g(p, "name", "attendanceType", "reason")):
                        continue
                    periods.append({
                        "period": _str(_g(p, "number", "period")),
                        "type": _g(p, "name", "attendanceType", "code"),
                        "reason": _g(p, "reason"),
                        "course": _g(p, "course", "courseTitle"),
                        "teacher": _g(p, "staff", "teacher"),
                    })
                absences.append({
                    "kind": "period" if section_key == "periodAttendance" else "daily",
                    "date": date,
                    "reason": _g(a, "reason", "attendanceType", "code"),
                    "reason2": _g(a, "reason2"),
                    "description": _g(a, "codeAllDayDescription", "description"),
                    "note": _g(a, "note", "notes", "comment"),
                    "periods": periods,
                })
            for key, label in (("totalExcused", "excused"), ("totalUnexcused", "unexcused"),
                               ("totalTardies", "tardies"), ("totalUnexcusedTardies", "unexcused_tardies"),
                               ("totalActivities", "activities")):
                raw = _g(section, key)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):  # daily: plain count (verified live)
                    value = int(raw)
                elif isinstance(raw, list):  # period attendance: per-period rows
                    value = sum(_int(_g(r, "total")) or 0 for r in raw)
                else:
                    continue
                totals.setdefault(section_key, {})[label] = value
        absences.sort(key=lambda x: x["date"] or "", reverse=True)
        kinds = [k for k in ("dailyAttendance", "periodAttendance") if isinstance(_g(data, k), dict)]
        return {"available": True, "tracking": ["daily" if k == "dailyAttendance" else "period" for k in kinds],
                "school_year_totals": {("daily" if k == "dailyAttendance" else "period"): v for k, v in totals.items()},
                "days_with_absences": len({a["date"] for a in absences}), "absences": absences,
                "window_days": days}

    # -- schedule ----------------------------------------------------------------

    def schedule(self, student, term=None):
        all_terms = term is None
        data = self.c.call("StudentClassList", {
            "childIntID": student["index"], "loadAllTerms": all_terms, "conSchOrgYearGU": "",
            "conSchTermIndex": "-1", "termIndex": "-1" if all_terms else str(term)})
        sched = _g(data, "studentClassScheduleForAllTerms") if all_terms else None
        sched = sched if isinstance(sched, dict) else _g(data, "studentClassSchedule")
        sched = sched if isinstance(sched, dict) else {}
        terms = [{
            "index": _int(_g(t, "termIndex")),
            "name": _g(t, "termName"),
            "start": _date(_g(t, "beginDate")),
            "end": _date(_g(t, "endDate")),
        } for t in _list(sched, "termLists")]
        current_index = _int(_g(sched, "termIndex"))
        blocks = _list(sched, "studentClassScheduleForTerms")
        if blocks:
            chosen = [b for b in blocks if _int(_g(b, "thisTermIndex")) == current_index] or blocks
            rows = [r for b in chosen for r in _list(b, "classLists")]
        else:
            rows = _list(sched, "classLists")
        seen, classes = set(), []
        for cl in rows:
            if _g(cl, "excludePVUE") is True:
                continue
            item = {
                "period": _str(_g(cl, "period")),
                "course": _g(cl, "courseTitle"),
                "room": _g(cl, "roomName", "room"),
                "teacher": _g(cl, "teacher"),
                "teacher_email": _g(cl, "teacherEmail"),
            }
            key = tuple(item.values())
            if key not in seen:
                seen.add(key)
                classes.append(item)
        return {"term": _g(sched, "termIndexName"), "classes": classes, "terms": terms}

    # -- documents ----------------------------------------------------------------

    def documents(self, student):
        try:
            data = self.c.call("GetStudentDocuments", {"childIntID": student["index"], "languageCode": "en"})
        except FeatureUnavailable as e:
            return {"available": False, "note": str(e), "documents": []}
        docs = _g(data, "studentDocuments")
        out = [{
            "document_id": _g(d, "documentGU"),
            "type": _g(d, "documentType", "category"),
            "date": _date(_g(d, "documentDate", "docDate")),
            "comment": _g(d, "documentComment", "notes"),
        } for d in _list(docs, "studentDocumentDatas")]
        out.sort(key=lambda d: d["date"] or "", reverse=True)
        return {"available": True, "documents": out}

    def document(self, student, document_id, out_dir):
        if not METHODS["GetStudentDocumentContent"]["documentGU"](document_id or ""):
            raise PVError("--id must be a document_id from `documents`.")
        data = self.c.call("GetStudentDocumentContent", {"childIntID": student["index"], "documentGU": document_id})
        holder = _g(data, "studentAttachedDocumentData")
        datas = _list(holder, "documentDatas") if isinstance(holder, dict) else []
        doc = datas[0] if datas else (holder if isinstance(holder, dict) else {})
        b64 = _g(doc, "base64Code")
        if not b64:
            raise PVError("ParentVUE returned no content for that document.")
        try:
            content = base64.b64decode(b64)
        except (ValueError, TypeError):
            raise PVError("Document content was not valid base64.")
        kind = re.sub(r"[^A-Za-z0-9]+", "-", _g(doc, "docType", "category") or "document").strip("-").lower()
        date = _date(_g(doc, "docDate")) or "undated"
        ext = ".pdf" if content[:4] == b"%PDF" else os.path.splitext(_g(doc, "fileName") or "")[1][:8] or ".bin"
        ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
        name = "%s_%s_%s%s" % (date, kind, re.sub(r"\W+", "", document_id)[:8], ext)
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        path = os.path.join(out_dir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return {"path": os.path.abspath(path), "bytes": len(content), "is_pdf": content[:4] == b"%PDF",
                "type": _g(doc, "docType", "category")}

    # -- debug ---------------------------------------------------------------------

    def shape(self, student, method):
        """Key paths and value TYPES only; never values. For fixing parsers."""
        params = {
            "GetChildListData": {"legacyAppRequest": False, "secondaryLogin": False},
            "Gradebook": {"reportPeriod": "", "concurrentSchOrgYearGU": "", "childIntID": student["index"],
                          "languageCode": "en"},
            "StudentClassList": {"childIntID": student["index"], "loadAllTerms": True, "conSchOrgYearGU": "",
                                 "conSchTermIndex": "-1", "termIndex": "-1"},
            "GetStudentAttendanceList": {"childIntID": student["index"]},
            "GetStudentDocuments": {"childIntID": student["index"], "languageCode": "en"},
        }[method]
        data = self.c.call(method, params)
        paths = {}

        def walk(v, path):
            if isinstance(v, dict):
                entry = paths.setdefault(path or "$", {"type": "object", "count": 0})
                entry["count"] += 1
                for k, child in v.items():
                    walk(child, "%s.%s" % (path, k) if path else k)
            elif isinstance(v, list):
                entry = paths.setdefault(path + "[]", {"type": "list", "count": 0, "max_len": 0})
                entry["count"] += 1
                entry["max_len"] = max(entry["max_len"], len(v))
                for child in v:
                    walk(child, path + "[]")
            else:
                t = "null" if v is None else ("empty" if v == "" else type(v).__name__)
                entry = paths.setdefault(path, {"types": set(), "count": 0})
                entry["types"].add(t)
                entry["count"] += 1

        walk(data, "")
        out = []
        for p, e in paths.items():
            item = {"path": p, "count": e["count"]}
            if "types" in e:
                item["types"] = sorted(e["types"])
            else:
                item["type"] = e["type"]
                if "max_len" in e:
                    item["max_len"] = e["max_len"]
            out.append(item)
        return out


def _normalize_course(c, with_assignments):
    name, code = _split_title(_g(c, "title", "courseName"))
    course = {
        "period": _str(_g(c, "period")),
        "name": name,
        "code": code or _g(c, "courseID"),
        "room": _g(c, "room"),
        "teacher": _g(c, "staff"),
        "teacher_email": _g(c, "staffEMail", "staffEmail"),
        "marks": [],
    }
    for m in _list(c, "marks"):
        summary = _g(m, "gradeCalculationSummary")
        cats = _list(summary, "assignmentGradeCalc", "assignmentGradeCalcs") if isinstance(summary, dict) else (
            summary if isinstance(summary, list) else [])
        mark = {
            "name": _g(m, "markName", "shortMarkName"),
            "grade": _g(m, "calculatedScoreString"),
            "score": _num(_g(m, "calculatedScoreRaw")),
            "categories": [{
                "type": _g(g, "type"),
                "weight": _str(_g(g, "weight")),
                "points": _num(_g(g, "points")),
                "points_possible": _num(_g(g, "pointsPossible")),
                "weighted_pct": _str(_g(g, "weightedPct")),
                "mark": _g(g, "calculatedMark"),
            } for g in cats],
        }
        assignments = [_normalize_assignment(a) for a in _list(m, "assignments")]
        mark["assignment_count"] = len(assignments)
        mark["missing_count"] = sum(1 for a in assignments if a["status"] == "missing")
        if with_assignments:
            mark["assignments"] = assignments
        course["marks"].append(mark)
    return course


def _normalize_assignment(a):
    score_text = _str(_g(a, "score", "displayScore"))
    points_text = _str(_g(a, "points"))
    notes = _g(a, "notes")
    score, possible = _parse_points(points_text, score_text)
    if score is None and isinstance(_g(a, "scoreRaw"), (int, float)):
        score = float(_g(a, "scoreRaw"))
    if possible is None:
        possible = _num(_g(a, "pointsPossible", "scoreMaxValue"))
    blob = " ".join(x for x in (score_text, notes, points_text) if isinstance(x, str))
    if _g(a, "isMissing", "missing") is True or re.search(r"\bmissing\b", blob, re.I):
        status = "missing"
    elif re.search(r"\bexcused\b|\bexempt\b", blob, re.I):
        status = "excused"
    elif score is not None:
        status = "graded"
    else:
        status = "not_graded"
    return {
        "name": _g(a, "measure", "name", "assignmentName"),
        "type": _g(a, "type", "category"),
        "date": _date(_g(a, "date", "assignedDate")),
        "due_date": _date(_g(a, "dueDate")),
        "score": _round(score),
        "points_possible": _round(possible),
        "percent": round(100.0 * score / possible, 1) if score is not None and possible else None,
        "status": status,
        "score_text": score_text,
        "notes": notes,
        "description": _g(a, "measureDescription", "description"),
    }


def _match_course(courses, selector):
    if not selector:
        return courses
    sel = selector.strip().lower()
    hits = [c for c in courses if (c["period"] or "").lower() == sel]
    if not hits:
        hits = [c for c in courses if sel in (c["name"] or "").lower() or sel == (c["code"] or "").lower()]
    if not hits:
        raise PVError("No course matches %r. Courses: %s" % (
            selector, ", ".join("%s (period %s)" % (c["name"], c["period"]) for c in courses)))
    return hits


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _envelope(student=None, **data):
    out = {"source": "parentvue", "generated_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}
    if student is not None:
        out["student"] = {"id": student["index"], "name": student["name"],
                          "grade": student.get("grade"), "school": student.get("school")}
    out.update(data)
    return out


def run(args, reader):
    cmd = args.command
    if cmd == "check":
        students = reader.students()
        return _envelope(ok=True, linked_students=len(students), students=[s["name"] for s in students],
                         note="Login works. All calls from this tool are read-only methods.")
    if cmd == "students":
        return _envelope(students=reader.students())

    student = reader.resolve_student(args.student)
    if cmd == "grades":
        return _envelope(student, **reader.gradebook(student, period=args.period, with_assignments=False))
    if cmd == "assignments":
        gb = reader.gradebook(student, period=args.period, with_assignments=True)
        courses = _match_course(gb["courses"], args.course) if gb["courses"] else []
        if args.status:
            for c in courses:
                for m in c["marks"]:
                    m["assignments"] = [a for a in m["assignments"] if a["status"] == args.status]
        return _envelope(student, reporting_period=gb["reporting_period"], courses=courses, note=gb.get("note"))
    if cmd == "attendance":
        return _envelope(student, attendance=reader.attendance(student, days=args.days))
    if cmd == "schedule":
        return _envelope(student, schedule=reader.schedule(student, term=args.term))
    if cmd == "documents":
        docs = reader.documents(student)
        if args.type:
            docs["documents"] = [d for d in docs["documents"] if args.type.lower() in (d["type"] or "").lower()]
        return _envelope(student, **docs)
    if cmd == "document":
        out_dir = args.out or reader.download_dir or os.path.join(os.getcwd(), "parentvue-documents")
        return _envelope(student, document=reader.document(student, args.id, out_dir))
    if cmd == "summary":
        gb = reader.gradebook(student, with_assignments=True)
        missing = []
        for c in gb["courses"]:
            for m in c["marks"]:
                for a in m.pop("assignments", []):
                    if a["status"] == "missing":
                        missing.append(dict(a, course=c["name"], period=c["period"]))
        att = reader.attendance(student, days=args.attendance_days)
        return _envelope(student, reporting_period=gb["reporting_period"], courses=gb["courses"],
                         missing=missing, attendance=att, note=gb.get("note"))
    if cmd == "debug-shape":
        return _envelope(student, method=args.method, shape=reader.shape(student, args.method))
    raise PVError("Unknown command %r" % cmd)


def build_parser():
    p = argparse.ArgumentParser(
        prog="parentvue_read.py",
        description="Read-only access to a student's ParentVUE (Synergy) records via a parent account.")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--compact", action="store_true", help="print JSON on one line")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def with_student(sp):
        sp.add_argument("--student", help="student name fragment or index (default: PARENTVUE_STUDENT or the only linked student)")
        return sp

    sub.add_parser("configure", help="interactive setup; run it yourself in a terminal")
    sub.add_parser("check", help="verify the address and login work")
    sub.add_parser("students", help="list students linked to this parent account")
    sp = with_student(sub.add_parser("grades", help="marking-period grade and category weights per course"))
    sp.add_argument("--period", type=int, help="reporting period index (default: current)")
    sp = with_student(sub.add_parser("assignments", help="gradebook assignments with scores"))
    sp.add_argument("--course", help="period number, course name fragment, or course code")
    sp.add_argument("--period", type=int, help="reporting period index (default: current)")
    sp.add_argument("--status", choices=["graded", "not_graded", "missing", "excused"])
    sp = with_student(sub.add_parser("attendance", help="absences, tardies and totals"))
    sp.add_argument("--days", type=int, help="only absences in the last N days")
    sp = with_student(sub.add_parser("schedule", help="class schedule with teachers and rooms"))
    sp.add_argument("--term", type=int, help="term index (default: current)")
    sp = with_student(sub.add_parser("documents", help="list documents (report cards, test reports, letters)"))
    sp.add_argument("--type", help='filter by type text, e.g. "Report Card"')
    sp = with_student(sub.add_parser("document", help="download one document (usually a PDF)"))
    sp.add_argument("--id", required=True, help="document_id from `documents`")
    sp.add_argument("--out", help="directory to save into (default: ./parentvue-documents)")
    sp = with_student(sub.add_parser("summary", help="grades + missing work + recent attendance"))
    sp.add_argument("--attendance-days", type=int, default=30)
    sp = with_student(sub.add_parser("debug-shape", help="maintenance: response structure without values"))
    sp.add_argument("--method", required=True, choices=[m for m in DATA_METHODS if m != "GetStudentDocumentContent"])
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            result = configure()
        else:
            cfg = load_config()
            client = ParentVueClient(cfg.get("PARENTVUE_BASE_URL", ""), cfg.get("PARENTVUE_USERNAME", ""),
                                     cfg.get("PARENTVUE_PASSWORD", ""))
            reader = Reader(client, cfg.get("PARENTVUE_STUDENT"))
            reader.download_dir = cfg.get("PARENTVUE_DOWNLOAD_DIR")
            result = run(args, reader)
    except PVError as e:
        json.dump({"source": "parentvue", "error": str(e)}, sys.stderr)
        sys.stderr.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=None if args.compact else 2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
