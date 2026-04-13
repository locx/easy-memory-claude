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

# Count session activity to calibrate prompt
MEMORY_DIR="${CLAUDE_PROJECT_DIR}/.memory"
GRAPH="${MEMORY_DIR}/graph.jsonl"
LAST_START="${MEMORY_DIR}/.last-session-start"
ACTIVITY=""
if [ -f "$LAST_START" ] && [ -f "$GRAPH" ]; then
    START_TS=$(cat "$LAST_START" 2>/dev/null || echo "")
    if [ -n "$START_TS" ]; then
        # Count entities updated since session start
        UPDATED=$(python3 -c "
import json, sys
ts='$START_TS'
n=u=0
try:
    for line in open('$GRAPH'):
        line=line.strip()
        if not line: continue
        try:
            obj=json.loads(line)
            if obj.get('type')!='entity': continue
            c=obj.get('_created','')
            up=obj.get('_updated','')
            if c and c>ts: n+=1
            elif up and up>ts: u+=1
        except: pass
except: pass
print(f'{n},{u}')
" 2>/dev/null || echo "0,0")
        NEW_C=$(echo "$UPDATED" | cut -d, -f1)
        UPD_C=$(echo "$UPDATED" | cut -d, -f2)
        if [ "$NEW_C" -gt 0 ] || [ "$UPD_C" -gt 0 ]; then
            ACTIVITY="This session: +${NEW_C} new entities, ~${UPD_C} updated."
        fi
    fi
fi

MEM="$HOME/.claude/memory/mem"
cat << MSG
SESSION END — persist what you learned:
${ACTIVITY:+$ACTIVITY
}
1. DECISIONS: If you chose between approaches or made architectural
   calls, persist each now:
     $MEM decide '{"title":"what was decided","rationale":"why this approach","alternatives":["rejected option -- reason"],"scope":"affected code area"}'

2. OUTCOMES: If you revisited a prior decision and saw it succeed or
   fail, close the loop:
     $MEM decide '{"action":"resolve","title":"prior decision","outcome":"successful","lesson":"what we learned"}'

3. WARNINGS: If you found gotchas, fragile code, or foot-guns:
     $MEM write '{"entities":[{"name":"filename.py","entityType":"file-warning","observations":["[WARNING] description"]}]}'

4. PATTERNS: If you discovered reusable knowledge (conventions,
   integration points, API quirks), persist as entities + relations:
     $MEM write '{"entities":[...],"relations":[...]}'

Skip any that don't apply. Only persist what's genuinely useful.
MSG
exit 0
