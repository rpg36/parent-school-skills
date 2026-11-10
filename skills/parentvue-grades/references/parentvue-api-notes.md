# ParentVUE / Synergy API notes for parentvue-grades

Background for the agent and maintainers. The agent should still only access ParentVUE
through `scripts/parentvue_read.py`.

## History

v1 used the legacy SOAP service (`/Service/PXPCommunication.asmx`,
`ProcessWebServiceRequest`) that the old mobile apps used. Districts began retiring it in
2026 along with the old apps. Calls now fail with "This app is deprecated. To continue, please download
ParentVUE (New). Error code: D5517-00." v2 uses the JSON API of the current
"ParentVUE (New)" app. It's documented by the community at
<https://github.com/StudentVue/docs/blob/master/JSON-API.md> (researched on StudentVUE
Android 1.9.16).

## Transport

```
POST {PARENTVUE_BASE_URL}/api/v1/mobile/PXPWebServices/<Method>
Content-Type: application/json
User-Agent: ksoap
AppNameOSAndVersion: ParentVUE|Android|1.9.16
Authorization: Bearer <access_token>          (Basic <user:pass> for AttemptLogin)

{"arguments": {"request": "<JSON-encoded string of the method's parameters>"}}
```

- `request` is a JSON object **serialized to a string**, not a nested object.
- Responses are `{"error": null | {"code","message"}, "data": {<one method-specific key>}}`.
  They're always HTTP 200, even on failure. Code `2100` means the feature is off at this
  school, and `400` means unavailable. The script reports both as `available: false`.
- `AttemptLogin` sends the credentials **only** in the Basic header. The body has `userID`
  and `password` null and `userType: "parent"`. It returns a bare token object
  (`access_token`, `refresh_token`) with no `error`/`data` envelope.
- `RefreshToken` uses `Bearer <refresh_token>` with an empty `{}` body. The script calls it
  once if a data call returns HTTP 401.
- Tokens are kept in memory only. Each command run logs in once and never writes a token to
  disk.
- The `AppNameOSAndVersion` and `User-Agent` values mirror the official app because the
  server expects them. The optional `edupointkeyversion` header is omitted (accepted today),
  and device attestation isn't enabled server-side. If Edupoint enforces either one, this
  skill will stop working. Do not try to get around that.

## Method allowlist

The script refuses any method not listed here. Each method's parameters are fixed, and their
values are type- and pattern-checked.

| Method | Command | `data` key | Parameters sent |
|---|---|---|---|
| `AttemptLogin` | (every command) | bare token | `userID: null, password: null, userType: "parent"` |
| `RefreshToken` | (after a 401) | bare token | none |
| `GetChildListData` | `check`, `students` | `children.childInfos[]` | `legacyAppRequest: false, secondaryLogin: false` |
| `Gradebook` | `grades`, `assignments`, `summary` | `traditionalGradebook` / `standardsGradebook` | `reportPeriod` ("" = current), `concurrentSchOrgYearGU: ""`, `childIntID`, `languageCode: "en"` |
| `GetStudentAttendanceList` | `attendance`, `summary` | `periodAttendance`, `dailyAttendance` | `childIntID` |
| `StudentClassList` | `schedule` | `studentClassScheduleForAllTerms` / `studentClassSchedule` | `childIntID`, `loadAllTerms`, `conSchOrgYearGU: ""`, `conSchTermIndex: "-1"`, `termIndex` |
| `GetStudentDocuments` | `documents` | `studentDocuments.studentDocumentDatas[]` | `childIntID`, `languageCode: "en"` |
| `GetStudentDocumentContent` | `document` | `studentAttachedDocumentData.documentDatas[].base64Code` | `childIntID`, `documentGU` |

Deliberately excluded:
- `GetStudentInfoData` (demographics, contacts, guardians, photo)
- `GetHealthData`, `GetStudentSpecialEdData`, `GetStudentDisciplineData` and `GetStudentFeeData`
- All Synergy Mail methods
- Calendar and hall-pass methods
- Every `Update*`, `Upload*` or `Save*` method (absence reporting, acknowledgements, account
  updates, signatures, notification preferences, device tokens)

`childIntID` is the ordinal from `GetChildListData`, not the `studentGU`.

**Side effect to know about:** `Gradebook` responses include `assignmentsSinceLastAccess`,
which is per-account read state the server tracks. Reading the gradebook through the skill
may count as the parent having viewed it.

## Field mapping

The response shapes for children, schedule, gradebook courses and marks, and documents come
from the community docs. Assignment fields (`measure`, `score`, `points`, `notes`, `dueDate`,
…), category weights (`gradeCalculationSummary`) and attendance are **inferred** from the
legacy SOAP names converted to camelCase. The parser matches keys case-insensitively and
tries several alternatives, and missing fields come back as `null`.

To check against the live server, run:

```
parentvue_read.py debug-shape --method Gradebook        # or GetStudentAttendanceList, StudentClassList, ...
```

It prints key paths, counts and value **types**, never values. Adjust the parser to match.

## Why the safety lives in the script

The credential is the parent's real ParentVUE password, which covers the whole account and
has no built-in expiry. Read-only access is enforced by:

1. one fixed path prefix on the configured host, over HTTPS only, with redirects never
   followed,
2. a method allowlist with fixed, validated parameters,
3. no command to call arbitrary methods, and
4. SKILL.md telling the agent never to use the credentials any other way.

The agent's shell can still read the config file (or environment) that holds the password.
That's the remaining risk. Mitigations: use a unique password that isn't used anywhere else,
keep access to the agent limited to the parents, keep the config file at mode 0600, and
change the password if the skill is retired.

## Login safety

A rejected login (an error envelope or 401 from `AttemptLogin`) stops immediately with no
retry, so repeated attempts can't lock the account.

## Verified live (November 2026)

Structure-only probes (`debug-shape`, no values) against one district's ParentVUE server:

- **Login** works with `userType: "parent"` and `AppNameOSAndVersion: ParentVUE|Android|1.9.16`.
- **`GetChildListData`**: children are at `children.childrenList[]`, with `childIntID`,
  `childName`, `childFirstName`, `organizationName` and `grade`. (The StudentVUE docs'
  `childInfos[]` doesn't appear here.) The same object carries district module flags such as
  `showGradeBookModule`, `showDocumentModule` and `showReportCardModule`.
- **`Gradebook`** (district with grades in Canvas): `traditionalGradebook` is null.
  `standardsGradebook` has an empty `subjects[]`, Q1–Q4 `reportingPeriods`, and an
  `errorMessage` containing a link to the district's Canvas ("Access Canvas … to view your
  student's grades"). The script reports that as `available: false` with the message as
  `note`. Sending `concurrentSchOrgYearGU` (the `orgYearGU`) makes the server return code
  500 "A critical error has occurred", so the script always sends `""`. A district that
  publishes grades in ParentVUE returns `traditionalGradebook`, whose assignment and
  category fields are still inferred (see Field mapping).
- **`GetStudentAttendanceList`**: `dailyAttendance` holds `absences[]` (`absenceDate`,
  `reason`, `reason2`, `note`, `codeAllDayDescription`, and an empty `periods[]`) and **integer**
  totals (`totalExcused`, `totalTardies`, `totalUnexcused`, `totalActivities`,
  `totalUnexcusedTardies`). `periodAttendance` is null.
- **`StudentClassList`**: matches the docs. Note that `termIndex` and `thisTermIndex` are
  strings.
- **`GetStudentDocuments`** (same district): error `2100` "Documents not available for this
  school".
  `showReportCardModule` is false and `supportingReportCardPassThruASPX` is true, so report
  cards are only served through the web portal.
