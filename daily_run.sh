#!/usr/bin/env bash
set -euo pipefail

WORKDIR="/srv/docker/moneymaker/verticals"
VENV="$WORKDIR/../.venv/bin"
LOGDIR="$HOME/.verticals/logs"
mkdir -p "$LOGDIR"

DATE=$(date +%Y%m%d)
LOG="$LOGDIR/daily_$DATE.log"

# Gemini-API-Key aus lokaler .env-artiger Datei lesen (nicht hardcoden, nie committen).
# Quelle: hermes-exchange/env/google Ai.json (Server-Pfad)
GEMINI_KEY_FILE="/srv/docker/hermes/exchange/env/google Ai.json"
if [ -f "$GEMINI_KEY_FILE" ]; then
    export GEMINI_API_KEY="$(cat "$GEMINI_KEY_FILE" | tr -d "\r\n ")"
else
    echo "ERROR: GEMINI_KEY_FILE $GEMINI_KEY_FILE nicht gefunden" >> "$LOG"
    exit 1
fi
export GEMINI_MAX_TOKENS=8192
export PATH="$VENV:$PATH"

echo "=== Daily MoneyMaker $DATE ===" >> "$LOG"
cd "$WORKDIR"

# 1. Topic + Draft + Produce (echte Fehlerprüfung via mtime-Vergleich)
MEDIA_DIR="$HOME/.verticals/media"
BEFORE=$(ls -t "$MEDIA_DIR"/verticals_*_en.mp4 2>/dev/null | head -1)
BEFORE_T=0; [ -n "$BEFORE" ] && BEFORE_T=$(stat -c %Y "$BEFORE")

set +e
python3 -m verticals daily --lang en --niche selfhosting 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
set -e

# 2. Verify: ein NEUES Video muss erzeugt worden sein
LATEST=$(ls -t "$MEDIA_DIR"/verticals_*_en.mp4 2>/dev/null | head -1)
LATEST_T=0; [ -n "$LATEST" ] && LATEST_T=$(stat -c %Y "$LATEST")
if [ "$RC" -ne 0 ] || [ -z "$LATEST" ] || [ "$LATEST_T" -le "$BEFORE_T" ]; then
    echo "FAIL: kein neues Video erzeugt (rc=$RC, latest=${LATEST:-none})" >> "$LOG"
    exit 1
fi

# 3. Hard-Cut auf 60s (YouTube Shorts Limit)
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$LATEST")
if [ "$(echo "$DUR > 60" | bc)" = "1" ]; then
    TRIMMED="${LATEST%.mp4}_60s.mp4"
    ffmpeg -y -i "$LATEST" -t 60 -c:v copy -c:a copy "$TRIMMED" 2>/dev/null
    LATEST="$TRIMMED"
    echo "Geschnitten auf 60s" >> "$LOG"
fi

# 4. Ambient Music Mix
WORKDIR_MEDIA="$HOME/.verticals/media/work_$(basename "$LATEST" _en_60s.mp4 | sed 's/verticals_//')_en"
DUR_INT=${DUR%.*}
[ "$DUR_INT" -gt 60 ] && DUR_INT=60

MUSIC_PAD="$WORKDIR_MEDIA/music_pad_daily.wav"
mkdir -p "$WORKDIR_MEDIA"
ffmpeg -y -f lavfi -i "aevalsrc=\
0.07*sin(2*PI*130.81*t)*(0.6+0.4*sin(2*PI*0.12*t))\
+0.05*sin(2*PI*164.81*t)*(0.5+0.5*sin(2*PI*0.09*t+1.2))\
+0.04*sin(2*PI*196.00*t)*(0.4+0.6*sin(2*PI*0.15*t+2.5))\
+0.03*sin(2*PI*261.63*t)*(0.3+0.7*sin(2*PI*0.07*t+0.8))\
+0.02*sin(2*PI*329.63*t)*(0.5+0.5*sin(2*PI*0.11*t+3.1))\
:s=44100:d=${DUR_INT}" -ar 44100 -ac 1 "$MUSIC_PAD" 2>/dev/null

FINAL="$HOME/.verticals/media/daily_final_${DATE}.mp4"
ffmpeg -y -i "$LATEST" -i "$MUSIC_PAD" \
  -filter_complex "[1:a]volume=0.10,afade=t=in:st=0:d=2,afade=t=out:st=$((DUR_INT-3)):d=3[m];[0:a][m]amix=inputs=2:duration=first[aout]" \
  -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 192k -t 60 \
  "$FINAL" -loglevel error 2>&1

# 5. Copy to exchange share
DEST="/srv/docker/hermes/exchange/Sammlung/MoneyMaker/daily_${DATE}.mp4"
cp "$FINAL" "$DEST"
echo "OK: $DEST (${DUR}s → 60s mit Musik)" >> "$LOG"

# 6. YouTube-Upload (privat) mit Metadaten + SRT aus dem neuesten Draft
YOUTUBE_URL=""
DRAFT="$(ls -t "$HOME/.verticals/drafts"/*.json 2>/dev/null | head -1)"
if [ -n "$DRAFT" ] && command -v python3 >/dev/null; then
    YOUTUBE_URL=$(cd /srv/docker/moneymaker/verticals && \
        VENV_BIN="$VENV" PATH="$PATH" python3 - "$FINAL" "$DRAFT" "$LOG" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "/srv/docker/moneymaker/verticals")
from verticals.upload import upload_to_youtube
video, draft_path = Path(sys.argv[1]), Path(sys.argv[2])
import json
draft = json.loads(draft_path.read_text())
srt = draft.get("srt_en")
try:
    url = upload_to_youtube(video, draft, Path(srt) if srt else None, "en")
    print(url)
except Exception as e:
    print(f"UPLOAD_ERROR: {e}")
PYEOF
)
    if [[ "$YOUTUBE_URL" == UPLOAD_ERROR:* ]]; then
        echo "WARN: YT-Upload fehlgeschlagen: ${YOUTUBE_URL#UPLOAD_ERROR: }" >> "$LOG"
        YOUTUBE_URL=""
    else
        [ -n "$YOUTUBE_URL" ] && echo "OK: YouTube $YOUTUBE_URL" >> "$LOG"
    fi
else
    echo "WARN: kein Draft gefunden, YT-Upload übersprungen" >> "$LOG"
fi

# 7. Telegram-Status (Bot: @ServerCallsBot via /srv/docker/telegram/)
BOT_TOKEN=$(grep BOT_TOKEN /srv/docker/telegram/telegram.env 2>/dev/null | cut -d= -f2)
CHAT_ID=$(grep CHAT_ID /srv/docker/telegram/telegram.env 2>/dev/null | cut -d= -f2)
if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
    BOT="$BOT_TOKEN"
    CHAT="$CHAT_ID"
    curl -s "https://api.telegram.org/bot$BOT/sendMessage" \
        -d chat_id="$CHAT" \
        -d parse_mode="HTML" \
        -d text="📹 <b>Daily Video $DATE</b>%0A${DEST##*/}%0A${YOUTUBE_URL:-kein YT-Upload}" >/dev/null 2>&1 || true
fi
