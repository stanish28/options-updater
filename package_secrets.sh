#!/usr/bin/env bash
# ==============================================================================
# Local Secrets Packager (Run on your Mac)
# ==============================================================================
# This script bundles gitignored local configuration files (.env, service-account.json)
# into a single archive to make transferring them to your cloud VPS via SCP easy.
# Run on your Mac: bash package_secrets.sh
# ==============================================================================

set -euo pipefail

# Text Formatting Helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}    Options Position Tracker Local Secrets Packager  ${NC}"
echo -e "${BLUE}=====================================================${NC}"

# Check for files
FILES_TO_PACK=()
if [ -f ".env" ]; then
    FILES_TO_PACK+=(".env")
else
    echo -e "${RED}Warning: Local .env file not found!${NC}"
fi

if [ -f "service-account.json" ]; then
    FILES_TO_PACK+=("service-account.json")
else
    echo -e "${RED}Warning: Local service-account.json not found!${NC}"
fi

if [ ${#FILES_TO_PACK[@]} -eq 0 ]; then
    echo -e "${RED}Error: No secrets found to package. Ensure .env or service-account.json exist in this directory.${NC}"
    exit 1
fi

# Package them
ARCHIVE_NAME="secrets.tar.gz"
echo -e "\n${YELLOW}Packaging files: ${FILES_TO_PACK[*]}...${NC}"
tar -czf "$ARCHIVE_NAME" "${FILES_TO_PACK[@]}"
echo -e "${GREEN}Created secure archive: $ARCHIVE_NAME${NC}"

# Print instructions
echo -e "\n${YELLOW}====================== DEPLOYMENT INSTRUCTIONS ======================${NC}"
echo -e "Follow these steps to deploy to your Cloud VPS:"
echo -e "\n${GREEN}Step 1: Upload this secrets archive to your VPS root${NC}"
echo -e "Copy/paste this command (replace 'your_vps_ip' and 'username' with your VPS credentials):"
echo -e "  ${BLUE}scp secrets.tar.gz username@your_vps_ip:~/options-updater/${NC}"

echo -e "\n${GREEN}Step 2: SSH into your VPS${NC}"
echo -e "  ${BLUE}ssh username@your_vps_ip${NC}"

echo -e "\n${GREEN}Step 3: Extract the secrets inside your VPS options-updater/ folder${NC}"
echo -e "  ${BLUE}cd ~/options-updater && tar -xzf secrets.tar.gz && rm secrets.tar.gz${NC}"
echo -e "=====================================================================${NC}\n"
