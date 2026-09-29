#!/usr/bin/env bash
# Установка с нуля одной командой: make bootstrap (DEPLOY.md, «Установка с нуля»).
#   env → up → build-archive → load-db → emulator-slices → health
# Уже сделанные шаги пропускаются: архив собран, база загружена, срезы готовы. FORCE=1 — всё заново.
# ARCHIVE_DIR — другая папка архива (по умолчанию data/archive), DUCKDB_MEMORY — память DuckDB (по умолчанию 6GB).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

RAW=data/raw
ARCHIVE_DIR=${ARCHIVE_DIR:-data/archive}
DUCKDB_MEMORY=${DUCKDB_MEMORY:-6GB}
FORCE=${FORCE:-0}
CATALOGS=("справочник_каналов_датчиков.csv" "справочник_объектов_диспетчер.csv")

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

# --- проверки до запуска ------------------------------------------------------------------------------------------

for tool in docker uv curl python3; do
    command -v "$tool" >/dev/null || fail "не найден $tool. Нужны Docker Desktop, uv (brew install uv), curl, python3 — DEPLOY.md"
done
docker info >/dev/null 2>&1 || fail "Docker не запущен — запустите Docker Desktop"

missing=()
for f in "${CATALOGS[@]}"; do [ -f "$RAW/$f" ] || missing+=("$f"); done
journals=$(find -L "$RAW" -maxdepth 1 -name 'ext-journal-*.csv' 2>/dev/null | sort)
[ -n "$journals" ] || missing+=("ext-journal-YYYY.csv (журналы 2019–2026)")
if [ ${#missing[@]} -gt 0 ]; then
    printf '\033[31mОшибка:\033[0m в %s нет исходных данных хакатона — они не хранятся в git:\n' "$RAW" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    printf 'Положите файлы в %s/ (журналы 2019–2024 — распакованные из .7z). Подробно — data/README.md.\n' "$ROOT/$RAW" >&2
    exit 1
fi
years=$(echo "$journals" | sed -E 's/.*ext-journal-([0-9]{4})\.csv/\1/' | tr '\n' ' ')

# --- шаги с замером времени ---------------------------------------------------------------------------------------

TIMINGS=()
step() {
    local name=$1; shift
    bold "== $name"
    local start=$SECONDS
    "$@"
    TIMINGS+=("$(printf '%-16s %5d с' "$name" $((SECONDS - start)))")
}

env_file() {
    make -s env
    # Шаблон пополняется — в старый infra/.env дописываются недостающие переменные со значениями по умолчанию
    local added=0
    while IFS= read -r line; do
        key=${line%%=*}
        if ! grep -q "^${key}=" infra/.env; then echo "$line" >> infra/.env; added=$((added + 1)); fi
    done < <(grep -E '^[A-Z_]+=' infra/.env.example)
    [ $added -eq 0 ] || echo "в infra/.env добавлено переменных из шаблона: $added"
}

load_env() {
    set -a
    # shellcheck disable=SC1091
    . infra/.env
    set +a
    DATABASE_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:${POSTGRES_PORT}/${POSTGRES_DB}"
    API="http://localhost:${API_PORT}/api/v1"
}

up() {
    make -s up
    printf 'жду api'
    for _ in $(seq 1 120); do
        curl -sf "$API/health/live" >/dev/null && { echo " — готов"; return; }
        printf '.'; sleep 2
    done
    fail "api не поднялся за 4 минуты: make logs"
}

build_archive() {
    local report="$ARCHIVE_DIR/report.json"
    if [ "$FORCE" != 1 ] && [ -f "$report" ] && python3 - "$report" $years <<'PY'
import json, sys
built = json.load(open(sys.argv[1]))["years"]
sys.exit(0 if all(y in built for y in sys.argv[2:]) else 1)
PY
    then
        echo "архив уже собран: $ARCHIVE_DIR (FORCE=1 — пересобрать)"; return
    fi
    (cd backend && ARCHIVE_DIR="$ROOT/$ARCHIVE_DIR" uv run python -m app.cli build-archive --raw "$ROOT/$RAW" \
        --out "$ROOT/$ARCHIVE_DIR" --memory "$DUCKDB_MEMORY")
}

load_db() {
    local loaded
    loaded=$(cd backend && DATABASE_URL=$DATABASE_URL uv run python - <<'PY'
import psycopg, os
url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url) as c:
    print(c.execute("SELECT count(*) FROM channel_state WHERE run_id IS NULL").fetchone()[0])
PY
)
    if [ "$FORCE" != 1 ] && [ "$loaded" -gt 0 ]; then
        echo "рабочая база уже загружена: $loaded каналов с состоянием (FORCE=1 — загрузить заново)"; return
    fi
    (cd backend && DATABASE_URL=$DATABASE_URL ARCHIVE_DIR="$ROOT/$ARCHIVE_DIR" \
        uv run python -m app.cli load-db --archive "$ROOT/$ARCHIVE_DIR" --memory "$DUCKDB_MEMORY" > /dev/null)
    echo "отчёт: data/interim/load-db/load-db-report.json"
}

slices() {
    if [ "$FORCE" != 1 ] && ls data/processed/emulator/*.parquet >/dev/null 2>&1; then
        echo "срезы сценариев уже готовы: data/processed/emulator (FORCE=1 — пересобрать)"; return
    fi
    make -s emulator-slices
}

health() {
    # после up контейнеры перезапускаются: эмулятор строит адресное пространство, шлюз переподключается
    for _ in $(seq 1 30); do
        curl -s "$API/health" | grep -q '"status": *"ok", *"components"' && break
        sleep 2
    done
    curl -s "$API/health" | python3 -c '
import json, sys
h = json.load(sys.stdin)
for name, c in h["components"].items():
    print("  %-11s %s" % (name, c.get("status")))
print("итого:", h["status"])'
}

step env env_file
load_env
step up up
step build-archive build_archive
step load-db load_db
step emulator-slices slices
step health health

bold "== время шагов"
printf '%s\n' "${TIMINGS[@]}"
bold "Готово: интерфейс — http://localhost:${WEB_PORT}, API — http://localhost:${API_PORT}/api/v1/docs"
