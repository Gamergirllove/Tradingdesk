# Phone setup (free Google Cloud VM)

1. **ntfy app** (App Store / Play Store) → `+` → subscribe to your private topic (keep the name secret: whoever knows it sees the setup code).
2. **Google Cloud**: console.cloud.google.com → sign in → start the free trial (card on file; the e2-micro
   below stays inside the always-free tier).
3. Search **Compute Engine** → enable it → **VM instances** → **Create instance**:
   - Name `trading-desk`
   - Region **us-west1 (Oregon)** (free tier: us-west1, us-central1, us-east1 only)
   - Machine type **e2-micro**
   - Boot disk → Change → **Debian 12**, disk type **Standard persistent disk**, **30 GB**
     (the default "Balanced" disk is not free)
   - Advanced options → Management → **Automation → Startup script**, paste:
     ```
     #!/bin/bash
     export NTFY_TOPIC=YOUR-PRIVATE-TOPIC
     curl -fsSL https://raw.githubusercontent.com/Gamergirllove/Tradingdesk/main/deploy/gcp/install.sh | bash
     ```
   - **Create**
4. In about 10 minutes ntfy pings you: *Tap to set up the desk. Setup code: ######*. Tap it.
5. Setup page: code, a dashboard password, your **Anthropic API key**
   (console.anthropic.com → Billing → add $5 → API keys → Create). Save. Then Share → **Add to Home Screen**.
6. Kalshi keys only when a bot moves past paper: demo.kalshi.co for the demo rung, kalshi.com for live
   (Account & security → API Keys; read + trade scopes).

The dashboard link changes when the VM restarts; ntfy sends the new one each time.
Reboot / update: dashboard → Setup → *Pull latest code and restart*.
