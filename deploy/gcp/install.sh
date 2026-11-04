#!/bin/bash
# Trading desk installer for a free Google Cloud e2-micro (Debian 12). Runs as root from the VM's
# startup script on every boot, and again whenever the dashboard asks for a code update. Idempotent.
set -uo pipefail
REPO="${REPO:-https://github.com/Gamergirllove/Tradingdesk.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/trading-desk
DATA=/var/lib/desk
SECR=$DATA/secrets
mkdir -p /var/log
exec >>/var/log/desk-install.log 2>&1
echo "=== install $(date -Is)"

export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null || ! python3 -c "import ensurepip" 2>/dev/null; then
  apt-get update -y && apt-get install -y git python3-venv python3-pip curl ca-certificates
fi

# 1 GB of RAM: add swap once
if [ -z "${SKIP_SWAP:-}" ] && [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
[ -f /swapfile ] && swapon /swapfile 2>/dev/null || true

# two users: the engine holds Kalshi keys, the judge/seats hold the Anthropic key. Neither can read the other's.
getent group desk >/dev/null || groupadd desk
id desk-engine >/dev/null 2>&1 || useradd -r -g desk -d $DATA/home-engine -s /usr/sbin/nologin desk-engine
id desk-judge  >/dev/null 2>&1 || useradd -r -g desk -d $DATA/home-judge  -s /usr/sbin/nologin desk-judge
mkdir -p $DATA $SECR/inbox $DATA/home-engine $DATA/home-judge
chown root:desk $DATA && chmod 2770 $DATA
chown root:desk $SECR && chmod 3770 $SECR
chown root:desk $SECR/inbox && chmod 2770 $SECR/inbox
chown desk-engine:desk $DATA/home-engine && chmod 700 $DATA/home-engine
chown desk-judge:desk $DATA/home-judge && chmod 700 $DATA/home-judge
if [ ! -s $SECR/desk_secret ]; then head -c 48 /dev/urandom | base64 | tr -d '/+=\n' > $SECR/desk_secret; fi
chown root:desk $SECR/desk_secret && chmod 0640 $SECR/desk_secret
if [ -n "${NTFY_TOPIC:-}" ]; then echo "$NTFY_TOPIC" > $DATA/ntfy_topic; chown root:desk $DATA/ntfy_topic; chmod 0664 $DATA/ntfy_topic; fi
if [ ! -f $SECR/dashboard_password ] && [ ! -f $DATA/setup_code ]; then
  printf "%06d\n" $(( $(od -An -N4 -tu4 /dev/urandom) % 1000000 )) > $DATA/setup_code
  chown desk-engine:desk $DATA/setup_code && chmod 0660 $DATA/setup_code
fi

# one-time secrets dropped in instance metadata by the project owner (removed from metadata after boot)
md() { curl -fs -m 5 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" 2>/dev/null; }
put() { # name owner value
  umask 077; printf '%s\n' "$3" > "$SECR/$1.tmp" && chown "$2":desk "$SECR/$1.tmp" && chmod 600 "$SECR/$1.tmp" && mv "$SECR/$1.tmp" "$SECR/$1"; umask 022; }
KENV=$(md desk-kalshi-env); KID=$(md desk-kalshi-key-id); KPEM=$(md desk-kalshi-pem); AKEY=$(md desk-anthropic-key)
PFX=kalshi; [ "$KENV" = "demo" ] && PFX=kalshi_demo
if [ -n "$KID" ] && [ -n "$KPEM" ]; then put ${PFX}_key_id desk-engine "$KID"; put ${PFX}_key.pem desk-engine "$KPEM"; echo "kalshi ($PFX) key installed from metadata"; fi
if [ -n "$AKEY" ]; then put anthropic_api_key desk-judge "$AKEY"; echo "anthropic key installed from metadata"; fi
unset KID KPEM AKEY

# code
if [ -d $APP/.git ]; then
  git -C $APP fetch -q origin $BRANCH && git -C $APP reset -q --hard origin/$BRANCH
else
  rm -rf $APP && git clone -q -b $BRANCH $REPO $APP
fi
[ -x $APP/.venv/bin/python ] || python3 -m venv $APP/.venv
$APP/.venv/bin/pip install -q --upgrade pip
$APP/.venv/bin/pip install -q -r $APP/requirements.txt
chmod -R a+rX $APP

# free HTTPS link to the dashboard (no account, no open ports)
if ! command -v cloudflared >/dev/null; then
  ARCH=$(dpkg --print-architecture)
  curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
    && dpkg -i /tmp/cloudflared.deb
fi

COMMON="Environment=DESK_DATA=$DATA DESK_SECRETS=$SECR PYTHONUNBUFFERED=1
WorkingDirectory=$APP
UMask=0007
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$DATA
PrivateTmp=yes"

cat > /etc/systemd/system/desk-judge.service <<EOF
[Unit]
Description=Desk judge (only process that talks to Claude for decisions)
After=network-online.target
[Service]
User=desk-judge
Group=desk
Environment=HOME=$DATA/home-judge
$COMMON
ExecStart=$APP/.venv/bin/python -m desk.judge.server
MemoryMax=300M
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/desk-seats.service <<EOF
[Unit]
Description=Desk seats (Claude Agent SDK: HEAD, RISK, SEARCH, EXIT, REVIEW)
After=desk-engine.service
[Service]
User=desk-judge
Group=desk
Environment=HOME=$DATA/home-judge
$COMMON
ExecStart=$APP/.venv/bin/python -m desk.seats.run
MemoryMax=700M
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/desk-engine.service <<EOF
[Unit]
Description=Desk engine (data, bots, risk, dashboard)
After=network-online.target desk-judge.service
[Service]
User=desk-engine
Group=desk
Environment=HOME=$DATA/home-engine
$COMMON
ExecStart=$APP/.venv/bin/python -m desk.engine.main
MemoryMax=400M
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/desk-tunnel.service <<EOF
[Unit]
Description=Desk dashboard link (Cloudflare quick tunnel)
After=desk-engine.service
[Service]
User=desk-engine
Group=desk
Environment=HOME=$DATA/home-engine DESK_DATA=$DATA
UMask=0007
Restart=always
RestartSec=10
ExecStart=/bin/bash $APP/deploy/gcp/tunnel.sh
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/desk-update.path <<EOF
[Unit]
Description=Watch for a code update request from the dashboard
[Path]
PathExists=$DATA/update_requested
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/desk-update.service <<EOF
[Unit]
Description=Pull latest desk code and restart
[Service]
Type=oneshot
ExecStartPre=/bin/rm -f $DATA/update_requested
ExecStart=/bin/bash $APP/deploy/gcp/install.sh
EOF

systemctl daemon-reload
systemctl enable desk-judge desk-engine desk-seats desk-tunnel desk-update.path >/dev/null 2>&1
systemctl restart desk-judge
sleep 2
systemctl restart desk-engine desk-seats
systemctl start desk-update.path
systemctl is-active --quiet desk-tunnel || systemctl start desk-tunnel
# report the dashboard link on the serial console (readable by the project owner with gcloud)
for i in $(seq 1 100); do [ -s $DATA/public_url ] && break; sleep 3; done
{ echo "DESK_URL=$(cat $DATA/public_url 2>/dev/null)"
  [ -f $DATA/setup_code ] && echo "DESK_SETUP_CODE=$(cat $DATA/setup_code)"; } > /dev/ttyS0 2>/dev/null || true
echo "=== done $(date -Is)"
