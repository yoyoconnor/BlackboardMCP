# blackboard-mcp

A read-only MCP server for Blackboard Learn (Original and Ultra). It lets Claude see your courses, class meetings, what's due, grades, announcements and course files, and it extracts the text of slide decks, PDFs and Word docs so you can study from them.

It never submits, posts or changes anything.

## Tools

| Tool | What it does |
|---|---|
| `whoami` | Checks the connection and shows the signed-in account |
| `list_courses` | Your current courses and their ids |
| `get_class_schedule` | Class meetings, office hours and events from the Blackboard calendar |
| `get_upcoming` | Assignments/tests due soon across all courses, with submitted/graded/overdue status |
| `get_assignment_details` | Instructions, attached files, points, feedback for one assignment |
| `get_grades` | Every gradebook item and your score for a course |
| `get_announcements` | Recent announcements |
| `browse_course_content` | The course's folder/module tree |
| `find_study_materials` | Lists slide decks, PDFs and docs, filtered by keyword ("week 5", "midterm") |
| `read_course_file` | Text of a .pptx (with speaker notes), PDF, .docx or page, by slide/page range |
| `search_course_materials` | Full-text search inside all of a course's files; returns slide/page hits |

Prompts: `study_guide`, `quiz_me`, `weekly_plan`.

Every tool that takes `course` accepts the internal id (`_12345_1`), the course code, or a fragment of the name like `CS 301`.

## Setup

### 1. Install

```bash
cd blackboard-mcp
uv venv && uv pip install -e .        # or: python -m venv .venv && .venv/bin/pip install -e .
```

On NixOS, `nix-shell -p uv python3` (or add `uv` to your devShell) is enough; all dependencies are pure Python.

### 2. Get your session cookie

Schools rarely give students REST API keys, so the server uses your browser session (the same way the Blackboard web app calls its own API).

1. Log in to Blackboard normally (SSO + Duo).
2. Open DevTools → **Network**, then click into any course.
3. Click any request whose URL contains `/learn/api/`.
4. Under **Request Headers**, copy the whole `Cookie:` value.

It should contain `BbRouter=...`. Sessions usually last a few hours of inactivity; when the tools start returning "401 / cookie expired", repeat these steps.

Treat this cookie like your password. Don't commit it.

### 3. Add to Claude

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "blackboard": {
      "command": "/absolute/path/to/blackboard-mcp/.venv/bin/blackboard-mcp",
      "env": {
        "BB_BASE_URL": "https://ualearn.blackboard.com",
        "BB_COOKIE": "BbRouter=expires:...; JSESSIONID=...",
        "BB_TIMEZONE": "America/Chicago"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add blackboard \
  -e BB_BASE_URL=https://ualearn.blackboard.com \
  -e BB_COOKIE="BbRouter=..." \
  -- /absolute/path/to/blackboard-mcp/.venv/bin/blackboard-mcp
```

Set `BB_BASE_URL` to whatever host you see in the address bar when you're in Blackboard (scheme + host only).

### Environment variables

| Variable | Required | Notes |
|---|---|---|
| `BB_BASE_URL` | yes | e.g. `https://ualearn.blackboard.com` |
| `BB_COOKIE` | yes* | Full browser Cookie header |
| `BB_APP_KEY` / `BB_APP_SECRET` / `BB_USER_ID` | alt* | Only if you have a registered REST app (client-credentials) |
| `BB_TIMEZONE` | no | Default `America/Chicago` |
| `BB_CACHE_DIR` | no | Extracted file text is cached here; default `~/.cache/blackboard-mcp` |

## Try it

- "What do I have due this week, and what's overdue?"
- "When do my classes meet tomorrow?"
- "Find the week 6 slides for CS 457 and make me a study guide."
- "Search my Networks slides for 'congestion window' and quiz me on it."

## Limits worth knowing

- **Class times** only show up if the instructor or registrar put them on the Blackboard course calendar. If `get_class_schedule` is empty, the syllabus usually has them (`find_study_materials` with `syllabus`).
- **Gradebook visibility** varies by school. If a course's gradebook can't be read, `get_upcoming` falls back to calendar due dates and shows "status unknown".
- **Legacy `.ppt`** and **scanned PDFs** have no extractable text; the tool says so. Export to `.pptx`/PDF with text, or upload the file to Claude directly.
- **Third-party tools** (Gradescope, McGraw Hill, Pearson, Zybooks links) live outside Blackboard; they appear as links only.

## Development

```bash
python tests/test_mock.py   # exercises every tool against a fake Blackboard API, no network
```
