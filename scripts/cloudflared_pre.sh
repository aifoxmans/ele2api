#!/usr/bin/env bash
set -euo pipefail
: "${CLOUDFLARED_CONFIG:?}" "${ELE2API_HOSTNAME:?}" "${ELE2API_ORIGIN:?}"
sudo test -f "$CLOUDFLARED_CONFIG"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
sudo cat "$CLOUDFLARED_CONFIG" > "$tmp"
CLOUDFLARED_TMP="$tmp" python3 - <<'PY'
import os
from pathlib import Path
path = Path(os.environ['CLOUDFLARED_TMP'])
lines = path.read_text(encoding='utf-8').splitlines()
hostname, origin = os.environ['ELE2API_HOSTNAME'], os.environ['ELE2API_ORIGIN']
if not any(line.strip() == 'ingress:' for line in lines):
    raise SystemExit('cloudflared ingress is missing')
fallback = next((i for i, line in enumerate(lines) if line.strip() == '- service: http_status:404'), None)
if fallback is None:
    raise SystemExit('cloudflared final 404 route is missing')
indent = lines[fallback][:len(lines[fallback]) - len(lines[fallback].lstrip())]
route = '- hostname: ' + hostname
start = next((i for i, line in enumerate(lines) if line.strip() == route), None)
if start is None:
    lines[fallback:fallback] = [indent + route, indent + '  service: ' + origin]
else:
    stop = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith('- ')), len(lines))
    service = next((i for i in range(start + 1, stop) if lines[i].strip().startswith('service:')), None)
    if service is None:
        lines.insert(start + 1, indent + '  service: ' + origin)
    else:
        lines[service] = indent + '  service: ' + origin
path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
# Validate the candidate before replacing the live connector configuration.
sudo cloudflared tunnel --config "$tmp" ingress validate
if ! cmp -s "$tmp" <(sudo cat "$CLOUDFLARED_CONFIG"); then
  sudo cp "$CLOUDFLARED_CONFIG" "${CLOUDFLARED_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
  sudo install -m 600 -o root -g root "$tmp" "$CLOUDFLARED_CONFIG"
fi
tunnel_id="$(sudo awk '/^tunnel:/ {print $2; exit}' "$CLOUDFLARED_CONFIG")"
test -n "$tunnel_id"
origin_cert="${HOME}/.cloudflared/cert.pem"
if [ -f "$origin_cert" ]; then
  cloudflared --origincert "$origin_cert" tunnel route dns "$tunnel_id" "$ELE2API_HOSTNAME"
else
  sudo cloudflared tunnel route dns "$tunnel_id" "$ELE2API_HOSTNAME"
fi
sudo systemctl restart cloudflared
