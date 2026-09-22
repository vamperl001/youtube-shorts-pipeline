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

# Fixed: stabiles Gemini Modell (statt sorted()[−1]=nano-banana 429). ponytail: pin, dynamisch nur wenn nötig
if [ -z "${GEMINI_LLM_MODEL:-}" ]; then
  GEMINI_LLM_MODEL="gemini-flash-latest"
  export GEMINI_LLM_MODEL
  export GEMINI_MODEL="$GEMINI_LLM_MODEL"
fi
export GEMINI_MAX_TOKENS=8192
# Fallback-Kette (llm.py): Gemini direkt -> PAID zuerst -> Free-Reserve.
# Paid-Default verifiziert (15.09., antwortet, ~0.005ct/Draft); altes
# google/gemini-2.0-flash-001 ist bei OpenRouter retired (404).
export PAID_FALLBACK="${PAID_FALLBACK:-openrouter/openai/gpt-4o-mini}"
# Kein Hardcode - llm.py holt freie Modelle dynamisch via /api/v1/models, Fallback via env FALLBACK_MODELS / PAID_FALLBACK
# Optional setzen: export FALLBACK_MODELS='["openrouter/free"]'
unset LITELLM_MODEL 2>/dev/null || true
# Pexels-Key fuer echte Stock-Fotos/Videos (gratis API-Key, 200 Req/h)
if [ -f /srv/docker/hermes/exchange/env/pexels.env ]; then
  export PEXELS_API_KEY="$(head -n1 /srv/docker/hermes/exchange/env/pexels.env | tr -d '\r\n ')"
fi
# Zentraler Key (raw file, Fallback data/.env)
if [ -f /srv/docker/hermes/exchange/env/openrouter.env ]; then
  export OPENROUTER_API_KEY="$(head -n1 /srv/docker/hermes/exchange/env/openrouter.env | tr -d '\r\n ')"
else
  export OPENROUTER_API_KEY="$(grep OPENROUTER_API_KEY /srv/docker/hermes/data/.env 2>/dev/null | cut -d= -f2 | tr -d '\"'\"'')"
fi
export PATH="$VENV:$PATH"

echo "=== Daily MoneyMaker $DATE ===" >> "$LOG"
cd "$WORKDIR"

# 1. Topic + Draft + Produce (echte Fehlerprüfung via mtime-Vergleich)
MEDIA_DIR="$HOME/.verticals/media"
BEFORE=$(ls -t "$MEDIA_DIR"/verticals_*_en.mp4 2>/dev/null | head -1)
BEFORE_T=0; [ -n "$BEFORE" ] && BEFORE_T=$(stat -c %Y "$BEFORE")

set +e
python3 -m verticals daily --lang en --niche apple  # ponytail: nur apple (höhere Audience) statt selfhosting-Mix 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
set -e

# 2. Verify: ein NEUES Video muss erzeugt worden sein
LATEST=$(ls -t "$MEDIA_DIR"/verticals_*_en.mp4 2>/dev/null | head -1)
LATEST_T=0; [ -n "$LATEST" ] && LATEST_T=$(stat -c %Y "$LATEST")
if [ "$RC" -ne 0 ] || [ -z "$LATEST" ] || [ "$LATEST_T" -le "$BEFORE_T" ]; then
    echo "FAIL: kein neues Video erzeugt (rc=$RC, latest=${LATEST:-none})" >> "$LOG"
    exit 1
fi

# 3. Variabler Cut bis 180s (YT Shorts seit 15.10.2024 bis 3 Min) - an Script-Laenge angepasst
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$LATEST")
DUR_INT=${DUR%.*}
[ "$DUR_INT" -gt 180 ] && DUR_INT=180
if [ "$(echo "$DUR > 180" | bc)" = "1" ]; then
    TRIMMED="${LATEST%.mp4}_180s.mp4"
    ffmpeg -y -i "$LATEST" -t 180 -c:v copy -c:a copy "$TRIMMED" 2>/dev/null
    LATEST="$TRIMMED"
    echo "Geschnitten auf 180s (war ${DUR}s)" >> "$LOG"
fi

# 4. Ambient Music Mix - Laenge = Script-Laenge (max 180s)
WORKDIR_MEDIA="$HOME/.verticals/media/work_$(basename "$LATEST" _en_180s.mp4 | sed 's/verticals_//;s/_180s//')_en"
# Fallback falls kein _180s im Namen
[ -d "$WORKDIR_MEDIA" ] || WORKDIR_MEDIA="$HOME/.verticals/media/work_$(basename "$LATEST" .mp4 | sed 's/verticals_//')_en"

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
  -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 192k -t $DUR_INT \
  "$FINAL" -loglevel error 2>&1

# 5. Copy to exchange share
DEST="/srv/docker/hermes/exchange/Sammlung/MoneyMaker/daily_${DATE}.mp4"
cp "$FINAL" "$DEST"
echo "OK: $DEST (${DUR}s → 60s mit Musik)" >> "$LOG"

# 6. YouTube-Upload (privat) mit Metadaten + SRT aus dem neuesten Draft
YOUTUBE_URL="$(head -n1 /srv/docker/hermes/exchange/env/pexels.env | tr -d '\r\n ')"
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
        YOUTUBE_URL="$(head -n1 /srv/docker/hermes/exchange/env/pexels.env | tr -d '\r\n ')"
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

# 8. Cleanup nach erfolgreichem Upload (ponytail: work nach Upload + 7d/14d Rotation)
if [[ "$YOUTUBE_URL" == https://youtu.be* ]]; then
    # sofort work dir dieses Runs löschen (spart 15-30 MB je Run)
    if [ -n "${WORKDIR_MEDIA:-}" ] && [ -d "$WORKDIR_MEDIA" ]; then
        rm -rf "$WORKDIR_MEDIA" 2>/dev/null && echo "Prune: work gelöscht $WORKDIR_MEDIA" >> "$LOG" || true
        # _en variant
        ALT="${WORKDIR_MEDIA}_en"
        [ -d "$ALT" ] && rm -rf "$ALT" 2>/dev/null && echo "Prune: work gelöscht $ALT" >> "$LOG" || true
    fi
fi
# wöchentliche Rotation: work>7d (keep 5 newest), daily_final>14d, verticals>7d, exchange Duplikate
if command -v python3 >/dev/null; then
    cd "$WORKDIR" && "$VENV/python3" -m verticals prune 2>&1 | tee -a "$LOG" || true
fi
