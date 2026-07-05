#!/bin/bash
# Isolated local stack for the API smoke sweep: throwaway Postgres database,
# scratch vault root, dedicated SAGE server on a spare port. Never touches
# ~/sage_vaults, the working `sage` database, or ports 8000/8001.
#
# Usage: local_stack.sh up | down | status
#
# bash-3.2 compatible (macOS stock bash): no associative arrays, no
# process substitution into globals.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SMOKE_DIR/.." && pwd)"
# A worktree checkout has no .venv of its own; fall back to the primary
# checkout's venv (PYTHONPATH below still pins imports to this checkout).
VENV_PY="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    VENV_PY="${REPO_ROOT%-worktrees/*}/.venv/bin/python"
fi
if [ ! -x "$VENV_PY" ]; then
    echo "no venv found for $REPO_ROOT"; exit 1
fi

BASE_DIR="$HOME/.cache/cas-smoke"
DB_NAME="sage_smoke"
SAGE_PORT=8765
VAULT_ROOT="$BASE_DIR/vaults"
STACK_CONFIG="$BASE_DIR/stack_config.yaml"
LOG_FILE="$BASE_DIR/sage.log"
PID_FILE="$BASE_DIR/sage.pid"

# The venv is installed editable against the primary checkout; pin imports
# to this worktree so the smoke run executes the code on this branch even
# if the primary checkout moves.
export PYTHONPATH="$REPO_ROOT"

up() {
    pg_isready >/dev/null || { echo "Postgres is not running (brew services start postgresql@17)"; exit 1; }
    mkdir -p "$BASE_DIR" "$VAULT_ROOT"

    if ! psql -lqt | cut -d'|' -f1 | grep -qw "$DB_NAME"; then
        echo "==> creating database $DB_NAME"
        createdb "$DB_NAME"
    fi
    echo "==> bootstrapping schema (idempotent)"
    "$VENV_PY" "$REPO_ROOT/scripts/bootstrap_postgres.py" --dsn "postgresql:///$DB_NAME"

    echo "==> writing stack config ($STACK_CONFIG)"
    sed "s/^  database: sage$/  database: $DB_NAME/" "$REPO_ROOT/sage/config.yaml" > "$STACK_CONFIG"
    grep -q "database: $DB_NAME" "$STACK_CONFIG" || { echo "database substitution failed"; exit 1; }

    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "==> SAGE already running (pid $(cat "$PID_FILE"))"
    else
        echo "==> starting SAGE on :$SAGE_PORT (log: $LOG_FILE)"
        SAGE_CONFIG_PATH="$STACK_CONFIG" \
            nohup "$VENV_PY" -m sage --vault-root "$VAULT_ROOT" --port "$SAGE_PORT" \
            > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
    fi

    echo "==> waiting for /health"
    i=0
    until curl -sf "http://127.0.0.1:$SAGE_PORT/health" >/dev/null 2>&1; do
        i=$((i+1))
        if [ "$i" -gt 120 ]; then
            echo "server did not come up; last log lines:"; tail -20 "$LOG_FILE"; exit 1
        fi
        sleep 1
    done
    echo "==> up: http://127.0.0.1:$SAGE_PORT (db $DB_NAME, vault root $VAULT_ROOT)"
}

down() {
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "==> stopping SAGE (pid $pid)"
            kill "$pid"
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
        fi
        rm -f "$PID_FILE"
    fi
    if psql -lqt | cut -d'|' -f1 | grep -qw "$DB_NAME"; then
        echo "==> dropping database $DB_NAME"
        dropdb "$DB_NAME"
    fi
    echo "==> removing $BASE_DIR"
    rm -rf "$BASE_DIR"
    echo "==> down"
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "SAGE running (pid $(cat "$PID_FILE"), port $SAGE_PORT)"
    else
        echo "SAGE not running"
    fi
    psql -lqt | cut -d'|' -f1 | grep -qw "$DB_NAME" && echo "database $DB_NAME exists" \
        || echo "database $DB_NAME absent"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    status) status ;;
    *) echo "usage: $0 up|down|status"; exit 64 ;;
esac
