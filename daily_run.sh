#!/usr/bin/env bash
set -euo pipefail

WORKDIR="/srv/docker/moneymaker/verticals"
VENV="$WORKDIR/../.venv/bin"
LOGDIR="$HOME/.verticals/logs"
mkdir -p "$LOGDIR"

DATE=$(date +%Y%m%d)
LOG="$LOGDIR/daily_$DATE.log"

# Keys (wie bisher)
GEMINI_KEY_FILE="/srv/docker/hermes/exchange/env/google Ai.json"
if [ -f "$GEMINI_KEY_FILE" ]; then
    export GEMINI_API_KEY="$(cat "$GEMINI_KEY_FILE" | tr -d '\r\n ')"
else
    echo "ERROR: GEMINI_KEY_FILE $GEMINI_KEY_FILE nicht gefunden" >> "$LOG"
    exit 1
fi
# Smart routing: kein Pin
unset GEMINI_LLM_MODEL 2>/dev/null || true
unset GEMINI_MODEL 2>/dev/null || true
export GEMINI_MAX_TOKENS=8192
unset PAID_FALLBACK 2>/dev/null || true
unset LITELLM_MODEL 2>/dev/null || true
if [ -f /srv/docker/hermes/exchange/env/pexels.env ]; then
  export PEXELS_API_KEY="$(head -n1 /srv/docker/hermes/exchange/env/pexels.env | tr -d '\r\n ')"
fi
if [ -f /srv/docker/hermes/exchange/env/openrouter.env ]; then
  export OPENROUTER_API_KEY="$(head -n1 /srv/docker/hermes/exchange/env/openrouter.env | tr -d '\r\n ')"
else
  export OPENROUTER_API_KEY="$(grep OPENROUTER_API_KEY /srv/docker/hermes/data/.env 2>/dev/null | cut -d= -f2 | tr -d '\"'\"'')"
fi
export PATH="$VENV:$PATH"

echo "=== Daily MoneyMaker NEW $DATE $(date -Iseconds) ===" >> "$LOG"
cd "$WORKDIR"

# 1. Ingest (RSS + Reddit)
echo "--- ingest ---" | tee -a "$LOG"
RUN_DIR=$(/srv/docker/moneymaker/.venv/bin/python -m verticals ingest --niche apple --limit 20 --with-reddit --reddit-limit 5 2>&1 | tee -a "$LOG" | grep "Run dir:" | awk '{print $NF}')
if [ -z "$RUN_DIR" ] || [ ! -d "$RUN_DIR" ]; then
  RUN_DIR=$(ls -t $HOME/.verticals/runs | head -1)
  RUN_DIR="$HOME/.verticals/runs/$RUN_DIR"
fi
echo "RUN_DIR=$RUN_DIR" | tee -a "$LOG"
if [ ! -f "$RUN_DIR/articles.json" ]; then echo "FAIL ingest" | tee -a "$LOG"; exit 1; fi

# 2-8. Phasen
for phase in editorial script visual asset tts render qc; do
  echo "--- $phase ---" | tee -a "$LOG"
  if ! /srv/docker/moneymaker/.venv/bin/python -m verticals $phase --run-dir "$RUN_DIR" 2>&1 | tee -a "$LOG"; then
    echo "FAIL $phase" | tee -a "$LOG"
    exit 1
  fi
  sleep 1
done

# QC already done, check qc.json
if ! /srv/docker/moneymaker/.venv/bin/python -c "import json,sys; qc=json.load(open('$RUN_DIR/qc.json')); sys.exit(0 if qc.get('pass') else 1)" 2>&1 | tee -a "$LOG"; then
  echo "QC FAIL — kein YouTube" | tee -a "$LOG"
  # trotzdem Telegram mit Fail
  QC_PASS="FAIL"
else
  QC_PASS="PASS"
fi

FINAL="$RUN_DIR/final.mp4"
if [ ! -f "$FINAL" ]; then echo "FAIL no final.mp4" | tee -a "$LOG"; exit 1; fi

# 9. Copy to exchange (wie bisher, aber aus runs)
DEST="/srv/docker/hermes/exchange/Sammlung/MoneyMaker/daily_${DATE}.mp4"
cp "$FINAL" "$DEST"
echo "OK: $DEST ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FINAL" 2>/dev/null)s, QC $QC_PASS)" | tee -a "$LOG"
# auch _en Duplikat wird via prune entfernt, aber für Kompatibilität beide?
cp "$FINAL" "/srv/docker/hermes/exchange/Sammlung/MoneyMaker/daily_${DATE}_en.mp4" 2>/dev/null || true

# 10. YouTube Upload (QC-Gate, private)
YOUTUBE_URL=""
if [ "$QC_PASS" = "PASS" ]; then
  echo "--- youtube ---" | tee -a "$LOG"
  YOUTUBE_URL=$(/srv/docker/moneymaker/.venv/bin/python -m verticals youtube --run-dir "$RUN_DIR" --privacy private 2>&1 | tee -a "$LOG" | grep -o "https://youtu.be[^ ]*")
  if [ -n "$YOUTUBE_URL" ]; then
    echo "OK: YouTube $YOUTUBE_URL" | tee -a "$LOG"
  else
    echo "WARN: YouTube Upload fehlgeschlagen" | tee -a "$LOG"
    YOUTUBE_URL="fehlgeschlagen"
  fi
else
  YOUTUBE_URL="QC FAIL — kein Upload"
  echo "$YOUTUBE_URL" | tee -a "$LOG"
fi

# 11. Telegram
BOT_TOKEN=$(grep BOT_TOKEN /srv/docker/telegram/telegram.env 2>/dev/null | cut -d= -f2)
CHAT_ID=$(grep CHAT_ID /srv/docker/telegram/telegram.env 2>/dev/null | cut -d= -f2)
if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
  curl -s "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
    -d chat_id="$CHAT_ID" \
    -d parse_mode="HTML" \
    -d text="📹 <b>Daily NEW $DATE</b> QC $QC_PASS%0A${DEST##*/}%0A${YOUTUBE_URL:-kein YT}" >/dev/null 2>&1 || true
fi

# 12. Cleanup
if [[ "$YOUTUBE_URL" == https://youtu.be* ]]; then
  echo "Prune: behalte run $RUN_DIR, lösche alte runs >7d" | tee -a "$LOG"
fi
# prune old runs >7d keep 7 newest
if command -v python3 >/dev/null; then
  cd "$WORKDIR" && "$VENV/python3" -m verticals prune 2>&1 | tee -a "$LOG" || true
  # prune runs older than 14d (keep 14)
  find "$HOME/.verticals/runs" -maxdepth 1 -type d -mtime +14 2>/dev/null | head -n 20 | while read d; do
    # keep at least 14 newest
    cnt=$(ls -1 "$HOME/.verticals/runs" | wc -l)
    if [ "$cnt" -gt 14 ]; then
      echo "Prune old run $d" | tee -a "$LOG"
      rm -rf "$d" 2>/dev/null || true
    fi
  done
fi

echo "=== Daily NEW $DATE DONE QC $QC_PASS $YOUTUBE_URL ===" | tee -a "$LOG"
