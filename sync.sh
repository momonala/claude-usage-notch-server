#!/bin/bash

REMOTE_URL="mnalavadi@192.168.2.107"
PROJECT_NAME=$(basename "$(pwd)")
DB_NAME="claude-usage.db"

REMOTE_PATH="${REMOTE_URL}:/home/mnalavadi/${PROJECT_NAME}/${DB_NAME}"
LOCAL_PATH="${DB_NAME}"

usage() {
    echo "Usage: $0 <direction>"
    echo ""
    echo "  pull   Sync database from remote ??? local"
    echo "  push   Sync database from local ??? remote"
    echo ""
    echo "Examples:"
    echo "  $0 pull   # download latest DB from server"
    echo "  $0 push   # upload local DB to server"
}

if [[ $# -ne 1 ]]; then
    echo "Error: expected exactly one argument." >&2
    echo "" >&2
    usage >&2
    exit 1
fi

case "$1" in
    pull)
        echo "Pulling ${DB_NAME} from remote..."
        rsync -avz "${REMOTE_PATH}" "${LOCAL_PATH}"
        ;;
    push)
        echo "Pushing ${DB_NAME} to remote..."
        rsync -avz "${LOCAL_PATH}" "${REMOTE_PATH}"
        ssh "${REMOTE_URL}" "chown mnalavadi:mnalavadi /home/mnalavadi/${PROJECT_NAME}/${DB_NAME}"
        ;;
    *)
        echo "Error: unknown direction '${1}'. Expected 'pull' or 'push'." >&2
        echo "" >&2
        usage >&2
        exit 1
        ;;
esac
