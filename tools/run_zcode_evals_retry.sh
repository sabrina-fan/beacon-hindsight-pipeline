#!/bin/bash
# zcode partitions -> official TypeSafe Jev (default endpoint jev-latest).
# Skips partitions already evaluated in eval-logs/memory.db.
source ~/.zshrc >/dev/null 2>&1
LOG=/tmp/zcode-eval-run.log
DONE=/tmp/zcode-eval-run.done
rm -f "$DONE"

while IFS=$'\t' read -r slug nsess nlines exists proj; do
  f="${HOME}/.beacon/endpoint/eval-logs/${slug}.jsonl"
  [ -f "$f" ] || continue
  db="${HOME}/.beacon/endpoint/eval-logs/memory.db"
  n=$(sqlite3 "$db" "SELECT count(*) FROM evaluations WHERE project_id='$proj'" 2>/dev/null || echo 0)
  [ "${n:-0}" -gt 0 ] && { echo "SKIP $proj (already $n evals)" >> "$LOG"; continue; }
  echo "$(date +%H:%M:%S) RUN $proj sessions=$nsess" >> "$LOG"
  command beacon memory evaluations run --log-path "$f" --project "$proj" --limit "$nsess" >> "$LOG" 2>&1
  echo "    exit=$?" >> "$LOG"
done < /tmp/zcode-eval-plan.tsv
echo "ALL DONE $(date +%H:%M:%S)" >> "$LOG"
touch "$DONE"
