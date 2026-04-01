#!/bin/bash
# Stop hook: structured decision capture + session summary.
# Global — works for any project with .memory/ dir.
[ -n "${CLAUDE_PROJECT_DIR:-}" ] || exit 0
[ -d "${CLAUDE_PROJECT_DIR}/.memory" ] || exit 0

# Guard unset session ID to avoid marker collisions
[ -n "${CLAUDE_SESSION_ID:-}" ] || exit 0
SAFE_SID="${CLAUDE_SESSION_ID//[^a-zA-Z0-9_-]/_}"
MARKER="/tmp/.claude-mem-reminded-${SAFE_SID}"

if [ -f "$MARKER" ]; then
    exit 0
fi

touch "$MARKER"

cat << 'MSG'
SESSION END — persist what you learned:

1. DECISIONS: If you chose between approaches or made architectural
   calls, persist each now:
     mem decide '{"title":"what was decided","rationale":"why this approach","alternatives":["rejected option -- reason"],"scope":"affected code area"}'

2. OUTCOMES: If you revisited a prior decision and saw it succeed or
   fail, close the loop:
     mem decide '{"action":"resolve","decision_name":"prior decision","outcome":"successful","lesson":"what we learned"}'

3. WARNINGS: If you found gotchas, fragile code, or foot-guns:
     mem write '{"entities":[{"name":"filename.py","entityType":"file-warning","observations":["[WARNING] description"]}]}'

4. PERSONAL CONTEXT: If you learned user preferences or received
   feedback worth remembering across sessions:
     mem remember --type feedback "description of the feedback"
     mem remember --type user "user role or preference info"

5. PATTERNS: If you discovered reusable knowledge (conventions,
   integration points, API quirks), persist as entities + relations:
     mem write '{"entities":[...],"relations":[...]}'

Skip any that don't apply. Only persist what's genuinely useful.
MSG
exit 0
