#!/usr/bin/env bash
# One-time bootstrap for a fresh Ubuntu 22.04/24.04 VPS. Run as root:
#   bash setup_vps.sh                 # base setup (safe to re-run)
#   HARDEN_SSH=1 bash setup_vps.sh    # ALSO disable password + root SSH login.
#       Only do that after confirming you can log in as `trader` with your SSH key.
set -euo pipefail

BOT_USER=trader
APP_DIR=/home/$BOT_USER/tradebot

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
. /etc/os-release
[[ $ID == ubuntu ]] || { echo "Ubuntu only (found $ID)"; exit 1; }

echo "== packages, time, timezone"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl gnupg ufw fail2ban unattended-upgrades chrony rsync
timedatectl set-timezone UTC          # the bot stores UTC; IST is computed where needed
systemctl enable --now chrony         # accurate clock matters for signed API requests
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== Docker Engine (official apt repo)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

echo "== user $BOT_USER"
if ! id $BOT_USER >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" $BOT_USER
fi
usermod -aG docker $BOT_USER
if [[ -f /root/.ssh/authorized_keys && ! -f /home/$BOT_USER/.ssh/authorized_keys ]]; then
  install -d -m 700 -o $BOT_USER -g $BOT_USER /home/$BOT_USER/.ssh
  install -m 600 -o $BOT_USER -g $BOT_USER /root/.ssh/authorized_keys /home/$BOT_USER/.ssh/
fi
install -d -o $BOT_USER -g $BOT_USER $APP_DIR
# container runs as uid 10001: give it the writable state directories
install -d -o 10001 -g 10001 $APP_DIR/data $APP_DIR/logs $APP_DIR/reports $APP_DIR/backups

echo "== swap (small VPS safety net)"
if ! swapon --show | grep -q . && [[ $(awk '/MemTotal/ {print $2}' /proc/meminfo) -lt 3000000 ]]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "== firewall: SSH only inbound (the bot needs NO inbound ports; Telegram is long-polling)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
systemctl enable --now fail2ban

if [[ "${HARDEN_SSH:-0}" == 1 ]]; then
  echo "== SSH hardening"
  cat > /etc/ssh/sshd_config.d/90-tradebot.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
CONF
  sshd -t && systemctl reload ssh
fi

echo "== daily backup at 02:30 UTC"
cat > /etc/cron.d/tradebot-backup <<CRON
30 2 * * * $BOT_USER cd $APP_DIR && docker compose run --rm -T bot tradebot backup --dest /app/backups --keep 30 >> $APP_DIR/backups/backup.log 2>&1
CRON

echo
echo "Done. Next (as $BOT_USER): copy the project to $APP_DIR, create .env + config/local.yaml,"
echo "then: cd $APP_DIR && docker compose build && docker compose up -d   (see deploy/DEPLOY.md)"
