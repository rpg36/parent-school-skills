#!/usr/bin/env python3
"""Read-only Canvas LMS client for a parent (observer) account.

Reads a linked student's courses, grades, assignments, missing work, due dates
and teacher comments, and prints normalized JSON on stdout.

Read-only by construction:
  * Every request is an HTTP GET. There is no code path that sends any other
    method or a request body.
  * Every URL (including pagination links returned by Canvas) is checked
    against a fixed allowlist of API paths on the configured host before it is
    requested. Anything else raises ReadOnlyViolation.
  * Redirects are never followed, so the token cannot be forwarded elsewhere.
  * There is no "raw request" command; only the subcommands below exist.

Configuration. Environment variables win; otherwise they are read from a
private KEY=VALUE file (mode 0600) that `canvas_read.py configure` writes:
  ${SCHOOL_SKILLS_CONFIG_DIR:-${XDG_CONFIG_HOME:-~/.config}/school-skills}/canvas.env

  CANVAS_BASE_URL  e.g. https://yourdistrict.instructure.com
  CANVAS_TOKEN     personal access token from the parent's Canvas account
  CANVAS_STUDENT   optional default student (name fragment or Canvas user id)

Standard library only; Python 3.8+.
"""

import argparse
import datetime as dt
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.1.0"
USER_AGENT = "canvas-grades-skill/%s (read-only)" % VERSION
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_PAGES = 50
CONFIG_FILE = "canvas.env"
CONFIG_KEYS = ("CANVAS_BASE_URL", "CANVAS_TOKEN", "CANVAS_STUDENT")
PER_PAGE = "100"

_ID = r"\d+(?:~\d+)?"
ALLOWED_PATHS = [
    re.compile(p)
    for p in (
        r"/api/v1/users/self",
        r"/api/v1/users/self/observees",
        r"/api/v1/users/%s/courses" % _ID,
        r"/api/v1/users/%s/courses/%s/assignments" % (_ID, _ID),
        r"/api/v1/users/%s/missing_submissions" % _ID,
        r"/api/v1/courses/%s/students/submissions" % _ID,
        r"/api/v1/courses/%s/assignment_groups" % _ID,
    )
]


class CanvasError(Exception):
    """A user-facing error. The message never contains the token."""


class ReadOnlyViolation(CanvasError):
    """Raised when a request would leave the read-only allowlist."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CanvasError(
            "Canvas answered with a redirect to %s. Redirects are not followed "
            "(so the token is never sent to another address). If this is your "
            "school's real Canvas address, set CANVAS_BASE_URL to it."
            % urllib.parse.urlsplit(newurl).netloc
        )


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class CanvasClient:
    def __init__(self, base_url, token, timeout=30):
        if not base_url:
            raise CanvasError("CANVAS_BASE_URL is not set (e.g. https://yourdistrict.instructure.com). "
                              "Run `canvas_read.py configure` in a terminal to set it up.")
        if not token:
            raise CanvasError("CANVAS_TOKEN is not set. Create one in Canvas (Account > Settings > "
                              "New Access Token), then run `canvas_read.py configure` in a terminal.")
        if "://" not in base_url:
            base_url = "https://" + base_url
        parts = urllib.parse.urlsplit(base_url.strip())
        host = (parts.hostname or "").lower()
        if not host:
            raise CanvasError("CANVAS_BASE_URL is not a valid URL.")
        if parts.scheme != "https" and not (parts.scheme == "http" and host in LOCAL_HOSTS):
            raise CanvasError("CANVAS_BASE_URL must use https://.")
        self.scheme = parts.scheme
        self.netloc = parts.netloc.lower()
        self.base = "%s://%s" % (self.scheme, self.netloc)
        self._token = token.strip()
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- safety ---------------------------------------------------------------

    def _check_url(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != self.scheme or parts.netloc.lower() != self.netloc:
            raise ReadOnlyViolation("Refusing request to a host other than CANVAS_BASE_URL.")
        if parts.fragment or not any(p.fullmatch(parts.path) for p in ALLOWED_PATHS):
            raise ReadOnlyViolation("Refusing request to non-allowlisted path: %s" % parts.path)

    # -- requests ---------------------------------------------------------------

    def _request(self, url):
        self._check_url(url)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        assert req.get_method() == "GET" and req.data is None
        for attempt in range(4):
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    link = resp.headers.get("Link", "")
                    return (json.loads(body) if body.strip() else None), link
            except urllib.error.HTTPError as e:
                detail = _error_detail(e)
                if e.code == 403 and "rate limit" in detail.lower() and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise CanvasError(_explain_http(e.code, urllib.parse.urlsplit(url).path, detail))
            except urllib.error.URLError as e:
                raise CanvasError("Could not reach Canvas at %s: %s" % (self.base, e.reason))
            except json.JSONDecodeError:
                raise CanvasError("Canvas returned a non-JSON response for %s." % urllib.parse.urlsplit(url).path)
        raise CanvasError("Canvas rate limit persisted after retries; try again in a minute.")

    def get(self, path, params=None):
        """GET one resource or every page of a list. params: list of (key, value)."""
        query = list(params or [])
        is_list = path not in ("/api/v1/users/self",)
        if is_list:
            query.append(("per_page", PER_PAGE))
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data, link = self._request(url)
        if not isinstance(data, list):
            return data
        items = list(data)
        pages = 1
        nxt = _next_link(link)
        while nxt and pages < MAX_PAGES:
            page, link = self._request(nxt)
            items.extend(page or [])
            pages += 1
            nxt = _next_link(link)
        return items


def _next_link(link_header):
    for part in (link_header or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if m:
            return m.group(1)
    return None


def _error_detail(e):
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        data = json.loads(raw)
        errs = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errs, list):
            return "; ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in errs)
        if isinstance(errs, dict):
            return json.dumps(errs)
        if isinstance(data, dict) and "message" in data:
            return str(data["message"])
    except ValueError:
        pass
    return raw[:200]


def _explain_http(code, path, detail):
    hints = {
        401: "the token is invalid, expired, or was revoked. Create a new one in Canvas (Account > Settings).",
        403: "this account is not allowed to see that data (the school or teacher may restrict observers).",
        404: "not found. Check the course id with the `courses` command.",
    }
    msg = "Canvas returned HTTP %d for %s: %s" % (code, path, hints.get(code, "unexpected error."))
    if detail:
        msg += " (Canvas said: %s)" % detail
    return msg


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percent(score, possible):
    if score is None or not possible:
        return None
    return round(100.0 * float(score) / float(possible), 1)


def _strip_html(text):
    if not text:
        return text
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


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
    cfg = {k: v for k, v in _read_env_file(config_path(), CanvasError).items() if k in CONFIG_KEYS}
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
    """Interactive setup for a human at a terminal. Never prints the token."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CanvasError("`configure` must be run by a person in an interactive terminal, not by an agent.")
    path = config_path()
    current = _read_env_file(path, CanvasError)
    print("Canvas setup. Values are saved to %s (mode 0600)." % path)
    default_url = current.get("CANVAS_BASE_URL", "")
    url = input("Canvas address%s: " % (" [%s]" % default_url if default_url else
                                        " (e.g. https://yourdistrict.instructure.com)")).strip() or default_url
    token = getpass.getpass("Canvas access token%s (input hidden): " % (
        " [press Enter to keep the saved one]" if current.get("CANVAS_TOKEN") else "")).strip()
    token = token or current.get("CANVAS_TOKEN", "")
    student = input("Default student (optional, only needed if several are linked)%s: " % (
        " [%s]" % current["CANVAS_STUDENT"] if current.get("CANVAS_STUDENT") else "")).strip()
    student = student or current.get("CANVAS_STUDENT", "")
    client = CanvasClient(url, token)  # validates the address and that a token was given
    _write_env_file(path, {"CANVAS_BASE_URL": client.base, "CANVAS_TOKEN": token, "CANVAS_STUDENT": student})
    print("Saved. Checking the connection...")
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
        self._observees = None

    # -- students -----------------------------------------------------------

    def me(self):
        u = self.c.get("/api/v1/users/self") or {}
        return {"id": u.get("id"), "name": u.get("name")}

    def students(self):
        if self._observees is None:
            raw = self.c.get("/api/v1/users/self/observees") or []
            self._observees = [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "short_name": s.get("short_name"),
                    "sortable_name": s.get("sortable_name"),
                }
                for s in raw
            ]
        return self._observees

    def resolve_student(self, selector=None):
        selector = selector or self.default_student or None
        students = self.students()
        if not students:
            raise CanvasError(
                "No students are linked to this Canvas account. The token must come from the "
                "parent (observer) account that is paired with the student."
            )
        if selector is None:
            if len(students) == 1:
                return students[0]
            raise CanvasError(
                "More than one student is linked; pass --student with one of: "
                + ", ".join("%s (id %s)" % (s["name"], s["id"]) for s in students)
            )
        sel = str(selector).strip().lower()
        exact = [s for s in students if str(s["id"]) == sel]
        if exact:
            return exact[0]
        matches = [
            s for s in students
            if any(sel in (s.get(k) or "").lower() for k in ("name", "short_name", "sortable_name"))
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CanvasError("No linked student matches %r. Linked: %s" % (
                selector, ", ".join(s["name"] for s in students)))
        raise CanvasError("%r matches several students: %s" % (
            selector, ", ".join(s["name"] for s in matches)))

    # -- courses ----------------------------------------------------------------

    def courses(self, student, include_completed=False):
        params = [
            ("include[]", "total_scores"),
            ("include[]", "current_grading_period_scores"),
            ("include[]", "term"),
        ]
        if not include_completed:
            params.append(("enrollment_state", "active"))
        raw = self.c.get("/api/v1/users/%s/courses" % student["id"], params) or []
        out = []
        for course in raw:
            if course.get("access_restricted_by_date") or not course.get("name"):
                continue
            enr = _student_enrollment(course.get("enrollments") or [], student["id"])
            entry = {
                "id": course.get("id"),
                "name": course.get("name"),
                "course_code": course.get("course_code"),
                "term": (course.get("term") or {}).get("name"),
                "current_score": enr.get("computed_current_score"),
                "current_grade": enr.get("computed_current_grade"),
                "final_score": enr.get("computed_final_score"),
                "final_grade": enr.get("computed_final_grade"),
                "grades_hidden": bool(course.get("hide_final_grades")),
                "grading_period": None,
                "url": "%s/courses/%s/grades/%s" % (self.c.base, course.get("id"), student["id"]),
            }
            if enr.get("has_grading_periods") and enr.get("current_grading_period_title"):
                entry["grading_period"] = {
                    "title": enr.get("current_grading_period_title"),
                    "current_score": enr.get("current_period_computed_current_score"),
                    "current_grade": enr.get("current_period_computed_current_grade"),
                    "final_score": enr.get("current_period_computed_final_score"),
                    "final_grade": enr.get("current_period_computed_final_grade"),
                }
            out.append(entry)
        out.sort(key=lambda c: (c["name"] or "").lower())
        return out

    def _course_name_map(self, student):
        return {c["id"]: c["name"] for c in self.courses(student)}

    # -- assignments ------------------------------------------------------------

    def assignment_groups(self, course_id):
        try:
            raw = self.c.get("/api/v1/courses/%s/assignment_groups" % course_id) or []
        except CanvasError:
            return []
        return [
            {"id": g.get("id"), "name": g.get("name"), "weight": g.get("group_weight")}
            for g in raw
        ]

    def submissions(self, student, course_id, comments=True, graded_since=None, with_assignment=False):
        params = [("student_ids[]", str(student["id"]))]
        if comments:
            params.append(("include[]", "submission_comments"))
        if with_assignment:
            params.append(("include[]", "assignment"))
        if graded_since:
            params.append(("graded_since", _iso(graded_since)))
        return self.c.get("/api/v1/courses/%s/students/submissions" % course_id, params) or []

    def raw_assignments(self, student, course_id):
        params = [("include[]", "submission"), ("order_by", "due_at")]
        return self.c.get("/api/v1/users/%s/courses/%s/assignments" % (student["id"], course_id), params) or []

    def assignments(self, student, course_id, comments=True):
        raw = self.raw_assignments(student, course_id)
        groups = self.assignment_groups(course_id)
        group_names = {g["id"]: g["name"] for g in groups}
        subs = {}
        try:
            for s in self.submissions(student, course_id, comments=comments):
                subs[s.get("assignment_id")] = s
        except CanvasError:
            # Fall back to the submission embedded in each assignment (no comments).
            pass
        items = []
        for a in raw:
            sub = subs.get(a.get("id")) or a.get("submission") or {}
            item = _normalize_assignment(a, sub, comments)
            item["group"] = group_names.get(a.get("assignment_group_id"))
            items.append(item)
        items.sort(key=lambda x: (x["due_at"] is None, x["due_at"] or "", (x["name"] or "").lower()))
        return {"assignment_groups": groups, "assignments": items}

    def missing(self, student):
        raw = self.c.get(
            "/api/v1/users/%s/missing_submissions" % student["id"],
            [("include[]", "course"), ("filter[]", "submittable")],
        ) or []
        out = []
        for a in raw:
            out.append({
                "course_id": a.get("course_id"),
                "course": (a.get("course") or {}).get("name"),
                "assignment_id": a.get("id"),
                "name": a.get("name"),
                "due_at": a.get("due_at"),
                "points_possible": a.get("points_possible"),
                "url": a.get("html_url"),
            })
        out.sort(key=lambda x: x["due_at"] or "")
        return out

    def upcoming(self, student, days=14, courses=None):
        now = _now()
        horizon = now + dt.timedelta(days=days)
        courses = courses if courses is not None else self.courses(student)
        out = []
        for course in courses:
            for a in self.raw_assignments(student, course["id"]):
                due = _parse_time(a.get("due_at"))
                if not due or not (now <= due <= horizon):
                    continue
                sub = a.get("submission") or {}
                out.append({
                    "course_id": course["id"],
                    "course": course["name"],
                    "assignment_id": a.get("id"),
                    "name": a.get("name"),
                    "due_at": a.get("due_at"),
                    "points_possible": a.get("points_possible"),
                    "submitted": bool(sub.get("submitted_at")),
                    "url": a.get("html_url"),
                })
        out.sort(key=lambda x: x["due_at"])
        return out

    def recent(self, student, days=7, courses=None):
        since = _now() - dt.timedelta(days=days)
        courses = courses if courses is not None else self.courses(student)
        out = []
        for course in courses:
            for s in self.submissions(student, course["id"], comments=True,
                                      graded_since=since, with_assignment=True):
                graded = _parse_time(s.get("graded_at"))
                if not graded or graded < since:
                    continue
                a = s.get("assignment") or {}
                item = _normalize_assignment(a, s, True)
                item["course_id"] = course["id"]
                item["course"] = course["name"]
                out.append(item)
        out.sort(key=lambda x: x["graded_at"] or "", reverse=True)
        return out


def _student_enrollment(enrollments, student_id):
    students = [e for e in enrollments if e.get("type") in ("student", "StudentEnrollment")]
    for e in students:
        if str(e.get("user_id")) == str(student_id):
            return e
    return students[0] if students else {}


def _status(sub):
    if not sub:
        return "not_submitted"
    if sub.get("excused"):
        return "excused"
    if sub.get("missing"):
        return "missing"
    if sub.get("workflow_state") == "graded" and (sub.get("score") is not None or sub.get("grade")):
        return "graded"
    if sub.get("submitted_at") or sub.get("workflow_state") in ("submitted", "pending_review"):
        return "submitted"
    return "not_submitted"


def _normalize_assignment(a, sub, comments):
    sub = sub or {}
    points = a.get("points_possible")
    item = {
        "id": a.get("id") or sub.get("assignment_id"),
        "name": a.get("name"),
        "due_at": a.get("due_at"),
        "points_possible": points,
        "score": sub.get("score"),
        "grade": sub.get("grade"),
        "percent": _percent(sub.get("score"), points),
        "status": _status(sub),
        "late": bool(sub.get("late")),
        "missing": bool(sub.get("missing")),
        "excused": bool(sub.get("excused")),
        "points_deducted": sub.get("points_deducted"),
        "submitted_at": sub.get("submitted_at"),
        "graded_at": sub.get("graded_at"),
        "counts_toward_grade": not a.get("omit_from_final_grade", False),
        "url": a.get("html_url"),
    }
    if comments:
        item["comments"] = [
            {
                "author": c.get("author_name"),
                "created_at": c.get("created_at"),
                "comment": _strip_html(c.get("comment")),
            }
            for c in (sub.get("submission_comments") or [])
        ]
    return item


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _envelope(student=None, **data):
    out = {"source": "canvas", "generated_at": _iso(_now())}
    if student is not None:
        out["student"] = {"id": student["id"], "name": student["name"]}
    out.update(data)
    return out


def run(args, reader):
    cmd = args.command
    if cmd == "check":
        me = reader.me()
        students = reader.students()
        return _envelope(ok=True, account=me, linked_students=len(students),
                         note="Connection works. All requests from this tool are read-only GETs.")
    if cmd == "students":
        return _envelope(students=reader.students())

    student = reader.resolve_student(args.student)
    if cmd == "courses":
        return _envelope(student, courses=reader.courses(student, include_completed=args.all))
    if cmd == "assignments":
        data = reader.assignments(student, args.course, comments=not args.no_comments)
        items = data["assignments"]
        if args.status:
            items = [a for a in items if a["status"] == args.status]
        return _envelope(student, course_id=args.course,
                         assignment_groups=data["assignment_groups"], assignments=items)
    if cmd == "missing":
        return _envelope(student, missing=reader.missing(student))
    if cmd == "upcoming":
        return _envelope(student, days=args.days, upcoming=reader.upcoming(student, days=args.days))
    if cmd == "recent":
        return _envelope(student, days=args.days, recently_graded=reader.recent(student, days=args.days))
    if cmd == "summary":
        courses = reader.courses(student)
        return _envelope(
            student,
            courses=courses,
            missing=reader.missing(student),
            upcoming=reader.upcoming(student, days=args.days, courses=courses),
            recently_graded=reader.recent(student, days=args.recent_days, courses=courses),
            windows={"upcoming_days": args.days, "recent_days": args.recent_days},
        )
    raise CanvasError("Unknown command %r" % cmd)


def build_parser():
    p = argparse.ArgumentParser(
        prog="canvas_read.py",
        description="Read-only access to a student's Canvas grades via a parent (observer) account.",
    )
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--compact", action="store_true", help="print JSON on one line")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def with_student(sp):
        sp.add_argument("--student", help="student name fragment or Canvas user id (default: CANVAS_STUDENT or the only linked student)")
        return sp

    sub.add_parser("configure", help="interactive setup; run it yourself in a terminal")
    sub.add_parser("check", help="verify the URL and token work")
    sub.add_parser("students", help="list students linked to this parent account")
    sp = with_student(sub.add_parser("courses", help="current grade and score in each course"))
    sp.add_argument("--all", action="store_true", help="include completed/past courses")
    sp = with_student(sub.add_parser("assignments", help="every assignment in one course with score, status and comments"))
    sp.add_argument("--course", required=True, help="course id (from `courses`)")
    sp.add_argument("--status", choices=["graded", "submitted", "not_submitted", "missing", "excused"])
    sp.add_argument("--no-comments", action="store_true", help="skip teacher comments")
    with_student(sub.add_parser("missing", help="past-due work Canvas marks as missing"))
    sp = with_student(sub.add_parser("upcoming", help="assignments due soon"))
    sp.add_argument("--days", type=int, default=14)
    sp = with_student(sub.add_parser("recent", help="recently graded work with scores and comments"))
    sp.add_argument("--days", type=int, default=7)
    sp = with_student(sub.add_parser("summary", help="grades + missing + upcoming + recently graded"))
    sp.add_argument("--days", type=int, default=14, help="upcoming window in days")
    sp.add_argument("--recent-days", type=int, default=7, help="recently-graded window in days")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            result = configure()
        else:
            cfg = load_config()
            client = CanvasClient(cfg.get("CANVAS_BASE_URL", ""), cfg.get("CANVAS_TOKEN", ""))
            result = run(args, Reader(client, cfg.get("CANVAS_STUDENT")))
    except CanvasError as e:
        json.dump({"source": "canvas", "error": str(e)}, sys.stderr)
        sys.stderr.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=None if args.compact else 2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
