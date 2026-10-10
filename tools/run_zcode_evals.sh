#!/bin/bash
# Evaluate zcode-partitioned traces with Jev, one beacon invocation per project.
source ~/.zshrc >/dev/null 2>&1
LOG=/tmp/zcode-eval-run.log
DONE=/tmp/zcode-eval-run.done
: > "$LOG"
total=$(wc -l < /tmp/zcode-eval-plan.tsv | tr -d ' ')
i=0
while IFS=$'\t' read -r slug nsess nlines exists proj; do
  i=$((i+1))
  f="${HOME}/.beacon/endpoint/eval-logs/${slug}.jsonl"
  [ -f "$f" ] || { echo "[$i/$total] SKIP no file $slug" >> "$LOG"; continue; }
  echo "[$i/$total] $(date +%H:%M:%S) $proj  sessions=$nsess events=$nlines" >> "$LOG"
  command beacon memory evaluations run \
    --jev-endpoint "$BEACON_JEV_ENDPOINT" \
    --jev-model "$BEACON_JEV_MODEL" \
    --log-path "$f" \
    --project "$proj" \
    --limit "$nsess" >> "$LOG" 2>&1
  echo "    exit=$?" >> "$LOG"
done < /tmp/zcode-eval-plan.tsv
echo "ALL DONE $(date +%H:%M:%S)" >> "$LOG"
touch "$DONE"
