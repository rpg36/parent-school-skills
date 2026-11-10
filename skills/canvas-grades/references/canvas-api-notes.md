# Canvas API notes for canvas-grades

Background for the agent and for whoever maintains the skill. The agent should still only
access Canvas through `scripts/canvas_read.py`.

## Endpoint allowlist

The script refuses any URL that isn't on `CANVAS_BASE_URL`'s host and on one of these paths.
It only ever sends `GET`.

| Path | Used by | Purpose |
|---|---|---|
| `/api/v1/users/self` | `check` | Confirms the token works (parent's name and id) |
| `/api/v1/users/self/observees` | all | Students linked to the parent account |
| `/api/v1/users/:student/courses` | `courses`, `summary` | Courses with `total_scores` and `current_grading_period_scores` |
| `/api/v1/users/:student/courses/:course/assignments` | `assignments`, `upcoming` | Assignments with the student's submission |
| `/api/v1/courses/:course/students/submissions` | `assignments`, `recent` | Submissions with comments, filtered to the student (`student_ids[]`) |
| `/api/v1/courses/:course/assignment_groups` | `assignments` | Category names and weights |
| `/api/v1/users/:student/missing_submissions` | `missing`, `summary` | Past-due work with no submission |

Pagination follows Canvas's `Link: rel="next"` header. Each next-page URL is checked against
the same allowlist. Redirects are never followed, so the bearer token can't be forwarded to
another host.

## Why the safety lives in the script

Canvas personal access tokens can't be limited to read-only (scoped tokens need an admin
developer key). Whatever the parent account can do in the web UI, the token can do too, and
observers can still do a few things, like sending inbox messages. The skill keeps those
actions out of reach by:

1. exposing no generic or raw request command,
2. sending `GET` requests only,
3. allowing only the paths above, matched in full against numeric ids, and
4. telling the agent (in SKILL.md) never to use the token any other way.

For extra protection, the parent can give the token a short expiry date when creating it, and
can revoke it anytime under Account > Settings > Approved Integrations.

## Observer permissions

- Observers can see a linked student's courses, assignments, submissions, scores and
  comments, unless the school or course limits what observers can see.
- `hide_final_grades` on a course hides the totals (`computed_*` values come back null), but
  each assignment's score is still visible.
- Unposted grades (a teacher grading without releasing yet) show up as `score: null`.
- Courses that haven't started or are date-restricted come back with
  `access_restricted_by_date` and are skipped.

## Errors

| HTTP | Meaning |
|---|---|
| 401 | The token is invalid, expired or revoked |
| 403 | The observer isn't allowed to see this, or the rate limit was hit (the script retries rate-limit errors with backoff) |
| 404 | Wrong course id, or the student isn't enrolled in that course |
| 3xx | `CANVAS_BASE_URL` points at a vanity host that redirects. Use the real host shown after logging in |

## Output shape (shared with parentvue-grades)

Every response is wrapped like this:

```json
{"source": "canvas", "generated_at": "...Z", "student": {"id": 1, "name": "..."}, ...}
```

`parentvue-grades` uses the same envelope with `source: "parentvue"` and similar
`student` / `courses` / `assignments` field names, so an agent can compare the two systems
directly.
