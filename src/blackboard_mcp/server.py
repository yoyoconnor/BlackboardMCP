"""Blackboard Learn MCP server.

Read-only tools for courses, class meetings, assignments, grades, announcements,
and course files -- plus text extraction from slide decks / PDFs for studying.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.types import ToolAnnotations

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .client import BlackboardClient, BlackboardError
from .extract import Section, extract, html_to_text, render

mcp = _Server(
    "blackboard",
    instructions=(
        "Read-only access to the student's Blackboard Learn account. Start with "
        "list_courses to get course IDs (tools also accept a course name fragment like "
        "'CS 301'). Use get_upcoming for 'what's due / what's next', get_class_schedule "
        "for class times, find_study_materials + read_course_file to study slide decks. "
        "read_course_file is paginated by slide/page: request ranges for long decks."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_client: BlackboardClient | None = None
_courses_cache: list[dict] | None = None

CACHE_DIR = Path(os.environ.get("BB_CACHE_DIR", Path.home() / ".cache" / "blackboard-mcp"))
TZ = ZoneInfo(os.environ.get("BB_TIMEZONE", "America/Chicago"))

FOLDERISH = {"resource/x-bb-folder", "resource/x-bb-lesson", "resource/x-bb-module-page"}
STUDY_EXT = (".pptx", ".pdf", ".docx", ".txt", ".md", ".ppt")


def client() -> BlackboardClient:
    global _client
    if _client is None:
        _client = BlackboardClient()
    return _client


# ------------------------------------------------------------------ utils
def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_dt(s: str | datetime | None) -> str:
    dt = _parse_dt(s) if isinstance(s, str) or s is None else s
    if not dt:
        return "—"
    return dt.astimezone(TZ).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _err(e: Exception) -> str:
    return f"Error: {e}"


async def _courses(refresh: bool = False) -> list[dict]:
    """Return the user's course memberships with the course object expanded."""
    global _courses_cache
    if _courses_cache is None or refresh:
        c = client()
        me = await c.me()
        rows = await c.collect(f"/v1/users/{me['id']}/courses", {"expand": "course", "limit": 100})
        _courses_cache = [r for r in rows if r.get("course")]
    return _courses_cache


async def _resolve_course(ref: str) -> dict:
    """Accept an internal id (_123_1), a courseId, or a fragment of the course name."""
    ref = ref.strip()
    courses = await _courses()
    for m in courses:
        c = m["course"]
        if ref in (c.get("id"), c.get("courseId"), c.get("externalId")):
            return c
    needle = re.sub(r"[\s\-_]+", "", ref.lower())
    hits = [
        m["course"]
        for m in courses
        if needle in re.sub(r"[\s\-_]+", "", (m["course"].get("name", "") + m["course"].get("courseId", "")).lower())
    ]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise BlackboardError(f"No course matches '{ref}'. Call list_courses to see your courses.")
    opts = ", ".join(f"{h['name']} ({h['id']})" for h in hits[:8])
    raise BlackboardError(f"'{ref}' matches several courses: {opts}. Use the id.")


def _is_current(member: dict) -> bool:
    c = member["course"]
    if c.get("availability", {}).get("available") == "No":
        return False
    dur = (c.get("availability") or {}).get("duration") or {}
    end = _parse_dt(dur.get("end"))
    return not (end and end < datetime.now(timezone.utc))


# ------------------------------------------------------------------ tools: overview
@mcp.tool(annotations=READ_ONLY)
async def whoami() -> str:
    """Check the connection and show which Blackboard account is signed in."""
    try:
        me = await client().me()
    except Exception as e:
        return _err(e)
    name = me.get("name", {})
    return (
        f"Signed in as {name.get('given', '')} {name.get('family', '')} "
        f"(username {me.get('userName')}, id {me.get('id')}) on {client().base_url}"
    )


@mcp.tool(annotations=READ_ONLY)
async def list_courses(include_past: bool = False) -> str:
    """List the student's courses with ids. By default hides courses that are unavailable or ended."""
    try:
        rows = await _courses(refresh=True)
    except Exception as e:
        return _err(e)
    if not include_past:
        rows = [r for r in rows if _is_current(r)]
    if not rows:
        return "No courses found." + ("" if include_past else " Try include_past=true.")
    rows.sort(key=lambda r: r["course"].get("name", ""))
    lines = []
    for r in rows:
        c = r["course"]
        role = r.get("courseRoleId", "")
        lines.append(
            f"- **{c.get('name')}** — id `{c['id']}`, courseId `{c.get('courseId')}`"
            + (f", role {role}" if role and role != "Student" else "")
            + (f", {c.get('ultraStatus', '').lower()} view" if c.get("ultraStatus") else "")
        )
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def get_class_schedule(days_ahead: int = 7, course: str | None = None) -> str:
    """Upcoming class meetings and other calendar events (office hours, institution events).

    Uses the Blackboard calendar. Class meeting times only appear if the instructor or
    registrar put them on the course calendar. days_ahead is capped at 112 (16 weeks).
    """
    try:
        days_ahead = max(1, min(days_ahead, 112))
        now = datetime.now(timezone.utc)
        params = {"since": _iso(now - timedelta(hours=1)), "until": _iso(now + timedelta(days=days_ahead))}
        items = await client().collect("/v1/calendars/items", params)
        names = {m["course"]["id"]: m["course"]["name"] for m in await _courses()}
        if course:
            cid = (await _resolve_course(course))["id"]
            items = [i for i in items if i.get("calendarId") == cid]
    except Exception as e:
        return _err(e)
    items = [i for i in items if i.get("type") != "GradebookColumn"]  # due dates live in get_upcoming
    if not items:
        return (
            f"No class meetings or events on the Blackboard calendar in the next {days_ahead} days. "
            "Many instructors don't put meeting times on the calendar; check the syllabus "
            "(find_study_materials with query 'syllabus')."
        )
    items.sort(key=lambda i: i.get("start", ""))
    out, day = [], None
    for i in items:
        start = _parse_dt(i.get("start"))
        d = start.astimezone(TZ).strftime("%A, %B %d") if start else "Unscheduled"
        if d != day:
            out.append(f"\n### {d}")
            day = d
        end = _parse_dt(i.get("end"))
        t = f"{start.astimezone(TZ):%I:%M %p}".lstrip("0") if start else ""
        if end:
            t += f"–{end.astimezone(TZ):%I:%M %p}".replace("–0", "–")
        where = f" @ {i['location']}" if i.get("location") else ""
        cname = names.get(i.get("calendarId"), i.get("calendarName", i.get("type", "")))
        out.append(f"- {t} **{i.get('title', 'Event')}** ({cname}){where}")
    return "\n".join(out).strip()


# ------------------------------------------------------------------ tools: assignments & grades
async def _columns(course_id: str) -> list[dict]:
    try:
        return await client().collect(f"/v2/courses/{course_id}/gradebook/columns", {"limit": 200})
    except BlackboardError:
        return await client().collect(f"/v1/courses/{course_id}/gradebook/columns", {"limit": 200})


async def _calendar_due(lo: datetime, hi: datetime) -> list[dict]:
    """Due-date items from the calendar (fallback when gradebook columns aren't readable)."""
    items: list[dict] = []
    cur = lo
    while cur < hi:  # calendar API caps each query at 16 weeks
        nxt = min(hi, cur + timedelta(weeks=16))
        items += await client().collect("/v1/calendars/items", {"since": _iso(cur), "until": _iso(nxt), "type": "GradebookColumn"})
        cur = nxt
    return [i for i in items if i.get("type") == "GradebookColumn"]


async def _my_grades(course_id: str) -> dict[str, dict]:
    me = await client().me()
    try:
        rows = await client().collect(f"/v2/courses/{course_id}/gradebook/users/{me['id']}")
    except BlackboardError:
        try:
            rows = await client().collect(f"/v1/courses/{course_id}/gradebook/users/{me['id']}")
        except BlackboardError:
            return {}
    return {r.get("columnId"): r for r in rows}


def _grade_str(g: dict | None, possible: float | None) -> str:
    if not g:
        return "not submitted"
    disp = (g.get("displayGrade") or {})
    score = disp.get("score", g.get("score"))
    text = disp.get("text")
    status = g.get("status", "")
    if score is not None and possible:
        return f"{score:g}/{possible:g}"
    if text:
        return str(text)
    if status == "NeedsGrading":
        return "submitted, awaiting grade"
    return status.lower() or "no grade"


@mcp.tool(annotations=READ_ONLY)
async def get_upcoming(days_ahead: int = 14, course: str | None = None, include_overdue_days: int = 3) -> str:
    """Assignments, tests and other graded work due soon, with submission/grade status.

    Scans every current course (or one course). Items due up to include_overdue_days ago are
    included so recently missed work shows up.
    """
    try:
        if course:
            targets = [await _resolve_course(course)]
        else:
            targets = [m["course"] for m in await _courses() if _is_current(m)]
    except Exception as e:
        return _err(e)
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(days=include_overdue_days), now + timedelta(days=days_ahead)

    async def per_course(c):
        try:
            cols, grades = await asyncio.gather(_columns(c["id"]), _my_grades(c["id"]))
        except BlackboardError as e:
            return [], f"{c['name']}: {e}"
        rows = []
        for col in cols:
            due = _parse_dt((col.get("grading") or {}).get("due"))
            if not due or not (lo <= due <= hi):
                continue
            if (col.get("availability") or {}).get("available") == "No":
                continue
            possible = (col.get("score") or {}).get("possible")
            rows.append((due, c, col, _grade_str(grades.get(col["id"]), possible)))
        return rows, None

    results = await asyncio.gather(*(per_course(c) for c in targets))
    rows = [r for rs, _ in results for r in rs]
    errors = [e for _, e in results if e]
    failed = {c["id"]: c for c, (_, e) in zip(targets, results) if e}
    if failed:  # fall back to calendar due dates for courses whose gradebook we couldn't read
        try:
            for i in await _calendar_due(lo, hi):
                c = failed.get(i.get("calendarId"))
                due = _parse_dt(i.get("end") or i.get("start"))
                if c and due:
                    col = {"id": i.get("id"), "name": i.get("title")}
                    rows.append((due, c, col, "status unknown"))
            errors = []
        except BlackboardError as e:
            errors.append(f"calendar: {e}")
    rows.sort(key=lambda r: r[0])
    if not rows:
        msg = f"Nothing with a due date in the next {days_ahead} days."
        return msg + ("\n\nErrors: " + "; ".join(errors) if errors else "")
    out = []
    for due, c, col, status in rows:
        flag = " ⚠️ OVERDUE" if due < now and status == "not submitted" else ""
        pts = (col.get("score") or {}).get("possible")
        out.append(
            f"- **{_fmt_dt(due)}** — {col.get('name')} ({c['name']})"
            f"{f', {pts:g} pts' if pts else ''} — {status}{flag}\n"
            f"  course `{c['id']}`, column `{col['id']}`"
            + (f", content `{col['contentId']}`" if col.get("contentId") else "")
        )
    if errors:
        out.append("\n_Some courses couldn't be read: " + "; ".join(errors) + "_")
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
async def get_assignment_details(course: str, column_id: str) -> str:
    """Full instructions, attached files, due date and status for one assignment/test (gradebook column)."""
    try:
        c = await _resolve_course(course)
        col = await client().get(f"/v2/courses/{c['id']}/gradebook/columns/{column_id}")
        grades = await _my_grades(c["id"])
    except Exception as e:
        return _err(e)
    possible = (col.get("score") or {}).get("possible")
    g = grades.get(column_id)
    parts = [
        f"# {col.get('name')}",
        f"Course: {c['name']}",
        f"Due: {_fmt_dt((col.get('grading') or {}).get('due'))}",
        f"Points: {possible:g}" if possible else "",
        f"Status: {_grade_str(g, possible)}",
    ]
    if g and g.get("feedback"):
        parts.append(f"Feedback: {html_to_text(g['feedback'])}")
    if col.get("description"):
        parts.append("\n## Description\n" + html_to_text(col["description"]))
    if col.get("contentId"):
        try:
            content = await client().get(f"/v1/courses/{c['id']}/contents/{col['contentId']}")
            body = html_to_text(content.get("body"))
            if body:
                parts.append("\n## Instructions\n" + body)
            atts = await _attachments(c["id"], col["contentId"])
            if atts:
                parts.append("\n## Attached files")
                parts += [f"- {a['fileName']} (attachment `{a['id']}`, content `{col['contentId']}`)" for a in atts]
                parts.append("Use read_course_file to read them.")
        except BlackboardError as e:
            parts.append(f"\n(Couldn't load linked content: {e})")
    return "\n".join(p for p in parts if p)


@mcp.tool(annotations=READ_ONLY)
async def get_grades(course: str) -> str:
    """All gradebook items for a course with the student's scores."""
    try:
        c = await _resolve_course(course)
        cols, grades = await asyncio.gather(_columns(c["id"]), _my_grades(c["id"]))
    except Exception as e:
        return _err(e)
    out = [f"# Grades — {c['name']}"]
    for col in cols:
        if (col.get("availability") or {}).get("available") == "No":
            continue
        possible = (col.get("score") or {}).get("possible")
        g = grades.get(col["id"])
        label = col.get("name")
        if col.get("externalGrade"):
            label = f"**{label} (overall)**"
        out.append(f"- {label}: {_grade_str(g, possible)}")
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
async def get_announcements(course: str | None = None, days_back: int = 14) -> str:
    """Recent course announcements (all current courses, or one)."""
    try:
        targets = [await _resolve_course(course)] if course else [m["course"] for m in await _courses() if _is_current(m)]
    except Exception as e:
        return _err(e)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    async def per(c):
        try:
            rows = await client().collect(f"/v1/courses/{c['id']}/announcements", {"limit": 50})
        except BlackboardError:
            return []
        out = []
        for a in rows:
            when = _parse_dt(a.get("modified") or a.get("created") or (a.get("availability") or {}).get("duration", {}).get("start"))
            if when and when >= cutoff:
                out.append((when, c, a))
        return out

    rows = sorted((r for rs in await asyncio.gather(*(per(c) for c in targets)) for r in rs), key=lambda r: r[0], reverse=True)
    if not rows:
        return f"No announcements in the last {days_back} days."
    return "\n\n".join(
        f"### {a.get('title')}\n_{c['name']} — {_fmt_dt(when)}_\n{html_to_text(a.get('body'))[:2000]}"
        for when, c, a in rows
    )


# ------------------------------------------------------------------ tools: content & study materials
async def _children(course_id: str, content_id: str | None) -> list[dict]:
    path = f"/v1/courses/{course_id}/contents" + (f"/{content_id}/children" if content_id else "")
    return await client().collect(path, {"limit": 200})


async def _attachments(course_id: str, content_id: str) -> list[dict]:
    try:
        return await client().collect(f"/v1/courses/{course_id}/contents/{content_id}/attachments")
    except BlackboardError:
        return []


def _handler(item: dict) -> str:
    return (item.get("contentHandler") or {}).get("id", "")


async def _walk(course_id: str, root: str | None, max_depth: int, max_items: int = 400):
    """Breadth-first walk of course content -> list of (path, item)."""
    out: list[tuple[str, dict]] = []
    queue: list[tuple[str | None, str, int]] = [(root, "", 0)]
    while queue and len(out) < max_items:
        cid, prefix, depth = queue.pop(0)
        try:
            kids = await _children(course_id, cid)
        except BlackboardError:
            continue
        for k in sorted(kids, key=lambda k: k.get("position", 0)):
            path = f"{prefix}/{k.get('title', '?')}"
            out.append((path, k))
            if (k.get("hasChildren") or _handler(k) in FOLDERISH) and depth + 1 < max_depth:
                queue.append((k["id"], path, depth + 1))
    return out


_KIND = {
    "resource/x-bb-folder": "folder",
    "resource/x-bb-lesson": "learning module",
    "resource/x-bb-file": "file",
    "resource/x-bb-document": "document",
    "resource/x-bb-externallink": "link",
    "resource/x-bb-asmt-test-link": "assignment/test",
    "resource/x-bb-assignment": "assignment",
    "resource/x-bb-blankpage": "page",
}


@mcp.tool(annotations=READ_ONLY)
async def browse_course_content(course: str, folder_id: str | None = None, depth: int = 2) -> str:
    """Show the course's content tree (folders, modules, files, documents, assignments).

    Start at the top (no folder_id) and drill into folders by id. depth 1-4.
    """
    try:
        c = await _resolve_course(course)
        rows = await _walk(c["id"], folder_id, max(1, min(depth, 4)))
    except Exception as e:
        return _err(e)
    if not rows:
        return "This folder is empty (or hidden from students)."
    lines = [f"# {c['name']} content" + (f" (under {folder_id})" if folder_id else "")]
    for path, it in rows:
        indent = "  " * (path.count("/") - 1)
        kind = _KIND.get(_handler(it), _handler(it).replace("resource/x-bb-", "") or "item")
        hidden = " (not available)" if (it.get("availability") or {}).get("available") == "No" else ""
        extra = ""
        if _handler(it) == "resource/x-bb-externallink":
            extra = f" → {(it.get('contentHandler') or {}).get('url', '')}"
        lines.append(f"{indent}- [{kind}] {it.get('title')} `{it['id']}`{hidden}{extra}")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def find_study_materials(course: str, query: str | None = None, folder_id: str | None = None) -> str:
    """List downloadable files (slide decks, PDFs, docs) in a course, optionally filtered by a
    keyword matched against file names and folder paths (e.g. 'week 5', 'chapter 3', 'syllabus').
    """
    try:
        c = await _resolve_course(course)
        rows = await _walk(c["id"], folder_id, max_depth=5)
    except Exception as e:
        return _err(e)
    candidates = [(p, it) for p, it in rows if _handler(it) in ("resource/x-bb-file", "resource/x-bb-document", "resource/x-bb-lesson", "resource/x-bb-assignment", "resource/x-bb-asmt-test-link")]
    sem = asyncio.Semaphore(8)

    async def atts(p, it):
        async with sem:
            return p, it, await _attachments(c["id"], it["id"])

    found = []
    for p, it, a in await asyncio.gather(*(atts(p, it) for p, it in candidates)):
        for att in a:
            found.append((p, it, att))
        body = html_to_text(it.get("body")) if it.get("body") else ""
        if not a and _handler(it) == "resource/x-bb-document" and len(body) > 200:
            found.append((p, it, None))
    if query:
        q = query.lower()
        terms = q.split()
        found = [f for f in found if all(t in (f[0] + " " + (f[2] or {}).get("fileName", "")).lower() for t in terms)]
    if not found:
        return "No matching files." + (" Try a broader query or browse_course_content." if query else "")
    lines = [f"# Study materials — {c['name']}" + (f" matching '{query}'" if query else "")]
    for p, it, att in found:
        if att:
            lines.append(f"- {att['fileName']} — in {p.rsplit('/', 1)[0] or '/'} — content `{it['id']}`, attachment `{att['id']}`")
        else:
            lines.append(f"- (text page) {p} — content `{it['id']}`")
    lines.append("\nRead one with read_course_file(course, content_id, attachment_id).")
    return "\n".join(lines)


def _cache_path(course_id: str, content_id: str, attachment_id: str) -> Path:
    key = hashlib.sha1(f"{client().base_url}|{course_id}|{content_id}|{attachment_id}".encode()).hexdigest()[:20]
    return CACHE_DIR / f"{key}.json"


async def _load_sections(course_id: str, content_id: str, attachment_id: str | None) -> tuple[str, str, list[Section]]:
    """Return (filename, kind, sections), using a local cache keyed on the attachment."""
    if attachment_id is None:
        atts = await _attachments(course_id, content_id)
        if len(atts) == 1:
            attachment_id = atts[0]["id"]
        elif len(atts) > 1:
            names = ", ".join(f"{a['fileName']} (`{a['id']}`)" for a in atts)
            raise BlackboardError(f"This item has several files; pass attachment_id. Files: {names}")
        else:
            item = await client().get(f"/v1/courses/{course_id}/contents/{content_id}")
            text = html_to_text(item.get("body"))
            if not text:
                raise BlackboardError("This content item has no files and no text body.")
            return item.get("title", "page"), "html", [Section("Page", item.get("title", ""), text)]

    cp = _cache_path(course_id, content_id, attachment_id)
    if cp.exists():
        d = json.loads(cp.read_text())
        return d["filename"], d["kind"], [Section(**s) for s in d["sections"]]

    meta = await client().get(f"/v1/courses/{course_id}/contents/{content_id}/attachments/{attachment_id}")
    data, ctype, hdr_name = await client().download(
        f"/v1/courses/{course_id}/contents/{content_id}/attachments/{attachment_id}/download"
    )
    fname = meta.get("fileName") or hdr_name or "file"
    kind, sections = await asyncio.to_thread(extract, data, fname, ctype or meta.get("mimeType", ""))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({"filename": fname, "kind": kind, "sections": [s.__dict__ for s in sections]}))
    return fname, kind, sections


@mcp.tool(annotations=READ_ONLY)
async def read_course_file(
    course: str,
    content_id: str,
    attachment_id: str | None = None,
    start: int = 1,
    end: int | None = None,
    include_notes: bool = True,
    max_chars: int = 40000,
) -> str:
    """Extract the text of a course file -- slide decks (.pptx, with speaker notes), PDFs, Word
    docs, or a text page -- for studying. Paginated by slide/page number: start/end are 1-based
    and inclusive. Output stops at max_chars and says where to continue.
    """
    try:
        c = await _resolve_course(course)
        fname, kind, sections = await _load_sections(c["id"], content_id, attachment_id)
    except Exception as e:
        return _err(e)
    total = len(sections)
    s = max(1, start)
    e = min(total, end or total)
    chosen = sections[s - 1 : e]
    text, shown = "", 0
    for sec in chosen:
        piece = render([sec], include_notes)
        if text and len(text) + len(piece) > max_chars:
            break
        text += ("\n\n" if text else "") + piece
        shown += 1
    last = s + shown - 1
    header = f"# {fname}\n_{kind.upper()}, {total} {'slides' if kind == 'pptx' else 'sections' if kind == 'docx' else 'pages'}; showing {s}–{last}_\n\n"
    if total and not any(x.body.strip() for x in chosen):
        header += "_Little or no extractable text — this may be a scanned PDF or image-only slides._\n\n"
    footer = f"\n\n_…continue with start={last + 1}_" if last < e else ""
    return header + text + footer


@mcp.tool(annotations=READ_ONLY)
async def search_course_materials(course: str, query: str, folder_id: str | None = None, max_files: int = 25) -> str:
    """Full-text search inside a course's slide decks/PDFs/docs (downloads and caches them).
    Returns the matching slides/pages with a snippet, so you can then read_course_file around them.
    """
    try:
        c = await _resolve_course(course)
        rows = await _walk(c["id"], folder_id, max_depth=5)
    except Exception as e:
        return _err(e)
    sem = asyncio.Semaphore(4)
    targets = []
    for p, it in rows:
        if _handler(it) in ("resource/x-bb-file", "resource/x-bb-document", "resource/x-bb-lesson"):
            for a in await _attachments(c["id"], it["id"]):
                if a.get("fileName", "").lower().endswith(STUDY_EXT[:-1]):
                    targets.append((it, a))
    targets = targets[:max_files]
    terms = [t.lower() for t in re.findall(r"\w+", query)]
    if not terms:
        return "Empty query."

    async def scan(it, a):
        async with sem:
            try:
                fname, _, secs = await _load_sections(c["id"], it["id"], a["id"])
            except Exception:
                return []
        hits = []
        for idx, sec in enumerate(secs, 1):
            blob = f"{sec.title}\n{sec.body}\n{sec.notes}"
            low = blob.lower()
            if all(t in low for t in terms):
                pos = low.find(terms[0])
                snip = re.sub(r"\s+", " ", blob[max(0, pos - 120) : pos + 200])
                hits.append(f"- **{fname}** {sec.label}" + (f" ({sec.title})" if sec.title else "")
                            + f" — content `{it['id']}`, attachment `{a['id']}`, start={idx}\n  …{snip}…")
        return hits

    hits = [h for hs in await asyncio.gather(*(scan(i, a) for i, a in targets)) for h in hs]
    if not hits:
        return f"No matches for '{query}' in {len(targets)} file(s)."
    return f"# '{query}' in {c['name']} ({len(hits)} hit(s) across {len(targets)} files scanned)\n" + "\n".join(hits[:60])


# ------------------------------------------------------------------ prompts
@mcp.prompt()
def study_guide(course: str, topic: str) -> str:
    """Build a study guide for a topic from the course's own slides."""
    return (
        f"Using the Blackboard tools, find the slide decks and readings in '{course}' about '{topic}' "
        "(search_course_materials, then read_course_file on the relevant ranges). Write a study guide: "
        "key concepts and definitions, important formulas/algorithms with a worked example, how the "
        "ideas connect, and likely exam questions. Cite slide/page numbers for each point."
    )


@mcp.prompt()
def quiz_me(course: str, topic: str, count: int = 10) -> str:
    """Quiz yourself on a topic using only the course's materials."""
    return (
        f"Read the '{course}' materials on '{topic}' with the Blackboard tools, then quiz me with {count} "
        "questions one at a time, mixing recall, application and conceptual questions. Wait for my "
        "answer each time, then grade it and point to the slide/page that covers it."
    )


@mcp.prompt()
def weekly_plan() -> str:
    """Plan the week from what's due and what's on the calendar."""
    return (
        "Call get_upcoming (14 days), get_class_schedule (7 days) and get_announcements (7 days). "
        "Then give me a day-by-day plan for this week that fits work around class times, flags "
        "anything overdue or worth a lot of points, and notes announcements that change deadlines."
    )


def main() -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request logs out of the client's stderr
    mcp.run()


if __name__ == "__main__":
    main()
