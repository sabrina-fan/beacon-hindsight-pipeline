#!/bin/bash
# Evaluate all partitioned traces with Jev, one beacon invocation per project.
source ~/.zshrc >/dev/null 2>&1
LOG=/tmp/eval-run.log
DONE=/tmp/eval-run.done
: > "$LOG"
total=$(wc -l < /tmp/eval-plan.tsv | tr -d ' ')
i=0
while IFS=$'\t' read -r slug n exists proj; do
  i=$((i+1))
  f="${HOME}/.beacon/endpoint/eval-logs/${slug}.jsonl"
  [ -f "$f" ] || { echo "[$i/$total] SKIP no file $slug" >> "$LOG"; continue; }
  echo "[$i/$total] $(date +%H:%M:%S) $proj  traces=$n" >> "$LOG"
  command beacon memory evaluations run \
    --jev-endpoint "$BEACON_JEV_ENDPOINT" \
    --jev-model "$BEACON_JEV_MODEL" \
    --log-path "$f" \
    --project "$proj" \
    --limit "$n" >> "$LOG" 2>&1
  echo "    exit=$?" >> "$LOG"
done < /tmp/eval-plan.tsv
echo "ALL DONE $(date +%H:%M:%S)" >> "$LOG"
touch "$DONE"
