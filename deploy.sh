#!/bin/bash

set -e

PROJECT_NAME=$(basename "$(pwd)")
REMOTE_HOST="pi-cloud"
REMOTE_PATH="~/${PROJECT_NAME}"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}Deploying ${PROJECT_NAME} to ${REMOTE_HOST}...${NC}"
ssh "$REMOTE_HOST" "cd $REMOTE_PATH && git pull && sudo systemctl restart projects_${PROJECT_NAME}"
echo -e "${GREEN}Deployment complete.${NC}"
