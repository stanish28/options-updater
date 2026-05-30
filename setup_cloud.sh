#!/usr/bin/env bash
# ==============================================================================
# Setup Script for Remote Cloud VPS (Ubuntu/Debian)
# ==============================================================================
# This script automates system packages, dependencies, virtual environment setup,
# and guides you through setting up credentials, authentication caching, and cron.
# Run on the cloud VPS: bash setup_cloud.sh
# ==============================================================================

set -euo pipefail

# Text Formatting Helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}   Starting Options Position Tracker Remote Setup    ${NC}"
echo -e "${BLUE}=====================================================${NC}"

# 1. System Package Check & Update
echo -e "\n${YELLOW}[1/4] Checking and installing system packages (requires sudo)...${NC}"
if ! command -v apt-get &> /dev/null; then
    echo -e "${RED}Error: This script is configured for Debian/Ubuntu systems using apt.${NC}"
    echo -e "${YELLOW}Please install python3, python3-venv, python3-pip, and git manually on your server.${NC}"
    exit 1
fi

sudo apt-get -o DPkg::Lock::Timeout=180 update
sudo apt-get -o DPkg::Lock::Timeout=180 install -y python3 python3-pip python3-venv git

# 2. Virtual Environment & Python Packages
echo -e "\n${YELLOW}[2/4] Setting up Python virtual environment and dependencies...${NC}"
if [ -d ".venv" ]; then
    echo -e "${GREEN}Virtual environment (.venv) already exists. Re-installing dependencies...${NC}"
else
    python3 -m venv .venv
    echo -e "${GREEN}Virtual environment (.venv) created successfully.${NC}"
fi

# Activate venv and install dependencies
source .venv/bin/activate
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    echo -e "${GREEN}Dependencies installed successfully.${NC}"
else
    echo -e "${RED}Error: requirements.txt not found. Are you running this script in the repository root?${NC}"
    exit 1
fi

# Ensure sync wrapper is executable
if [ -f "sync" ]; then
    chmod +x sync
    echo -e "${GREEN}Made 'sync' wrapper executable.${NC}"
fi

# 3. Environment Secrets Audit
echo -e "\n${YELLOW}[3/4] Checking environment configurations...${NC}"
SECRETS_FOUND=true

if [ ! -f ".env" ]; then
    echo -e "${RED}Warning: .env configuration file is missing.${NC}"
    SECRETS_FOUND=false
else
    echo -e "${GREEN}Found .env file.${NC}"
fi

if [ ! -f "service-account.json" ]; then
    echo -e "${RED}Warning: Google Sheets service-account.json file is missing.${NC}"
    SECRETS_FOUND=false
else
    echo -e "${GREEN}Found Google service-account.json.${NC}"
fi

if [ "$SECRETS_FOUND" = false ]; then
    echo -e "\n${YELLOW}====================== ACTION REQUIRED ======================${NC}"
    echo -e "You must upload your gitignored secrets to the VPS."
    echo -e "Use SCP or SFTP to copy these files from your Mac to the VPS root:"
    echo -e "  scp .env service-account.json your_user@your_vps_ip:$(pwd)/"
    echo -e "=============================================================${NC}"
fi

# 4. Next Steps / Cron Instructions
echo -e "\n${YELLOW}[4/4] Setup complete! Follow these final steps to activate tracking:${NC}"
echo -e "${BLUE}-------------------------------------------------------------${NC}"
echo -e "${GREEN}Step A: Upload Secrets (if you haven't already)${NC}"
echo -e "Ensure both '.env' and 'service-account.json' reside in this directory."
echo -e "Ensure GOOGLE_APPLICATION_CREDENTIALS in '.env' points to the absolute path of service-account.json on this server."

echo -e "\n${GREEN}Step B: Perform Initial Sync & 2FA Verification${NC}"
echo -e "Execute the following command manually to authenticate Robinhood:"
echo -e "  ${YELLOW}./sync${NC}"
echo -e "Type in the 6-digit MFA code when prompted. This will cache your session."

echo -e "\n${GREEN}Step C: Configure the Automated Cron Job${NC}"
echo -e "First set the VM clock to Pacific so cron tracks 6 AM PT year-round (handles DST):"
echo -e "  ${YELLOW}sudo timedatectl set-timezone America/Los_Angeles && sudo systemctl restart cron${NC}"
echo -e "Then run: ${YELLOW}crontab -e${NC}"
echo -e "Select your editor and paste the following line at the bottom to run at 6:00 AM PT:"
echo -e "  ${YELLOW}0 6 * * 1-5 cd $(pwd) && ./sync >> launchd.log 2>&1${NC}"
echo -e "${BLUE}-------------------------------------------------------------${NC}"
echo -e "${GREEN}Script completed!${NC}\n"
