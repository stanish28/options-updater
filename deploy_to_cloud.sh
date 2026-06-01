#!/usr/bin/env bash
# ==============================================================================
# Master Cloud Deployment & Orchestration Script (Runs on your Mac)
# ==============================================================================
# This script automates the complete migration of the Options Position Tracker to
# your running GCP VM instance. It transfers cached 2FA sessions, pulls code,
# runs the remote setup, performs verification, and schedules cron.
# ==============================================================================

set -euo pipefail

# Configuration
INSTANCE_NAME="instance-20260528-233621"
ZONE="us-central1-c"
REPO_URL="https://github.com/stanish28/options-updater.git"

# Text Formatting Helpers
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

export CLOUDSDK_CORE_DISABLE_PROMPTS=1

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}   Options Position Tracker Automated Cloud Deploy    ${NC}"
echo -e "${BLUE}=====================================================${NC}"

# 1. Package Secrets Locally
echo -e "\n${YELLOW}[1/7] Packaging local secrets (.env, service-account.json, setup_cloud.sh, sync_positions.py)...${NC}"
if [ ! -f ".env" ] || [ ! -f "service-account.json" ] || [ ! -f "setup_cloud.sh" ] || [ ! -f "sync_positions.py" ]; then
    echo -e "${RED}Error: Local secrets, setup_cloud.sh, or sync_positions.py missing. Ensure they exist locally.${NC}"
    exit 1
fi
tar -czf secrets.tar.gz .env service-account.json setup_cloud.sh sync_positions.py
echo -e "${GREEN}Packaged secrets.tar.gz successfully.${NC}"

# 2. Remote Folder Provisioning
echo -e "\n${YELLOW}[2/7] Preparing directories on remote VM...${NC}"
gcloud compute ssh "$INSTANCE_NAME" --zone="$ZONE" --command="mkdir -p ~/.tokens ~/options-updater"
echo -e "${GREEN}Directories created successfully.${NC}"

# 3. Transfer Cached Robinhood Session (Bypass MFA!)
echo -e "\n${YELLOW}[3/7] Uploading cached Robinhood 2FA session token to VM...${NC}"
if [ -f "/Users/tanishshah/.tokens/robinhood.pickle" ]; then
    gcloud compute scp "/Users/tanishshah/.tokens/robinhood.pickle" "${INSTANCE_NAME}:~/.tokens/robinhood.pickle" --zone="$ZONE"
    echo -e "${GREEN}Cached session transferred! VM will not require 2FA authentication.${NC}"
else
    echo -e "${YELLOW}Warning: Local cached Robinhood session (~/.tokens/robinhood.pickle) not found.${NC}"
    echo -e "${YELLOW}You will be prompted for a 2FA code during remote sync verification.${NC}"
fi

# 4. Upload Secrets Archive
echo -e "\n${YELLOW}[4/7] Uploading secrets archive to remote VM...${NC}"
gcloud compute scp secrets.tar.gz "${INSTANCE_NAME}:~/secrets.tar.gz" --zone="$ZONE"
rm secrets.tar.gz
echo -e "${GREEN}Secrets archive uploaded successfully.${NC}"

# 5. Clone Code & Unpack Secrets on VM
echo -e "\n${YELLOW}[5/7] Installing git, cloning Git repository and extracting secrets on VM...${NC}"
gcloud compute ssh "$INSTANCE_NAME" --zone="$ZONE" --command="
  sudo apt-get -o DPkg::Lock::Timeout=180 update && sudo apt-get -o DPkg::Lock::Timeout=180 install -y git
  if [ -d \"\$HOME/options-updater\" ]; then
    rm -rf \"\$HOME/options-updater\"
  fi
  git clone \"$REPO_URL\" \"\$HOME/options-updater\"
  mv \"\$HOME/secrets.tar.gz\" \"\$HOME/options-updater/secrets.tar.gz\"
  cd \"\$HOME/options-updater\"
  tar -xzf secrets.tar.gz && rm secrets.tar.gz
  
  # Dynamically fix GOOGLE_APPLICATION_CREDENTIALS path in VM .env
  HOME_DIR=\$(eval echo ~\$USER)
  sed -i \"s|/Users/tanishshah/options_updater|\$HOME_DIR/options-updater|g\" \"\$HOME_DIR/options-updater/.env\"
"
echo -e "${GREEN}Code deployed and secrets extracted successfully.${NC}"

# 6. Execute Setup & Run Remote Sync Verification
echo -e "\n${YELLOW}[6/7] Running remote setup and executing diagnostic sync on VM...${NC}"
gcloud compute ssh "$INSTANCE_NAME" --zone="$ZONE" --command="
  cd \$HOME/options-updater
  bash setup_cloud.sh
  echo -e '\n--- Performing Remote Diagnostic Sync ---'
  ./sync
"
echo -e "${GREEN}Remote diagnostic sync executed successfully!${NC}"

# 7. Programmatically Install Weekday Cron Job
echo -e "\n${YELLOW}[7/7] Setting VM timezone to PT and installing weekday Cron jobs (6:00 AM + 12:00 PM PT)...${NC}"
gcloud compute ssh "$INSTANCE_NAME" --zone="$ZONE" --command="
  # Set the VM clock to Pacific so cron tracks 6 AM PT year-round (handles DST).
  sudo timedatectl set-timezone America/Los_Angeles

  # Fetch home directory programmatically
  HOME_DIR=\$(eval echo ~\$USER)
  CRON_JOB=\"0 6,12 * * 1-5 cd \$HOME_DIR/options-updater && ./sync >> launchd.log 2>&1\"

  # Replace any existing entry for this project (match the real command, not a guessed path).
  (crontab -l 2>/dev/null | grep -Fv \"options-updater && ./sync\" || true; echo \"\$CRON_JOB\") | crontab -

  # Restart cron so it picks up the new timezone.
  sudo systemctl restart cron
  echo -e 'Installed Cron schedule:'
  crontab -l
"

echo -e "\n${GREEN}=====================================================${NC}"
echo -e "${GREEN}   Deployment Complete! The Tracker is now in the Cloud${NC}"
echo -e "${GREEN}=====================================================${NC}\n"
