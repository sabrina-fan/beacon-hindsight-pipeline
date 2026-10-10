#!/bin/bash
# Evaluate devin-cli partitioned traces with official Jev, one beacon run per project partition.
source ~/.zshrc >/dev/null 2>&1
LOG=/tmp/devin-eval-run.log
DONE=/tmp/devin-eval-run.done
rm -f "$DONE"
: > "$LOG"
total=$(wc -l < /tmp/devin-eval-plan.tsv | tr -d ' ')
i=0
while IFS=$'\t' read -r slug nsess nev proj; do
  i=$((i+1))
  f="/tmp/devin-parts/${slug}.jsonl"
  [ -f "$f" ] || { echo "[$i/$total] SKIP no file $slug" >> "$LOG"; continue; }
  echo "[$i/$total] $(date +%H:%M:%S) $proj  sessions=$nsess events=$nev" >> "$LOG"
  command beacon memory evaluations run \
    --jev-endpoint "$BEACON_JEV_ENDPOINT" \
    --jev-model "$BEACON_JEV_MODEL" \
    --log-path "$f" \
    --project "$proj" \
    --limit "$nsess" >> "$LOG" 2>&1
  echo "    exit=$? done=$(date +%H:%M:%S)" >> "$LOG"
done < /tmp/devin-eval-plan.tsv
echo "ALL DONE $(date +%H:%M:%S)" >> "$LOG"
touch "$DONE"
