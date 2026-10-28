#!/bin/bash
# Generate a LAN TLS cert + Apple config profile for the VTH dashboard.
set -euo pipefail

SSL_DIR="${SSL_DIR:-/etc/nginx/ssl}"
LAN_IP="${LAN_IP:-$(hostname -I | awk '{print $1}')}"
HOST_NAME="${HOST_NAME:-$(hostname)}"
EXTRA_IPS="${EXTRA_IPS:-}"

sudo mkdir -p "$SSL_DIR"

SAN="IP:${LAN_IP},DNS:${HOST_NAME},DNS:${HOST_NAME}.local,DNS:localhost"
for ip in $EXTRA_IPS; do
    SAN="${SAN},IP:${ip}"
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$TMP/vth.key" \
    -out "$TMP/vth.crt" \
    -subj "/CN=${HOST_NAME}/O=YesTechie LABS" \
    -addext "subjectAltName=${SAN}"

DER_B64=$(openssl x509 -in "$TMP/vth.crt" -outform DER | base64 | tr -d '\n')
UUID1=$(cat /proc/sys/kernel/random/uuid)
UUID2=$(cat /proc/sys/kernel/random/uuid)

cat > "$TMP/vth.mobileconfig" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>PayloadCertificateFileName</key>
      <string>YesTechie-VTH.cer</string>
      <key>PayloadContent</key>
      <data>${DER_B64}</data>
      <key>PayloadDescription</key>
      <string>Trust the YesTechie VoIP console so the browser microphone works without a warning.</string>
      <key>PayloadDisplayName</key>
      <string>YesTechie VTH</string>
      <key>PayloadIdentifier</key>
      <string>labs.yestechie.vth.cert</string>
      <key>PayloadType</key>
      <string>com.apple.security.root</string>
      <key>PayloadUUID</key>
      <string>${UUID1}</string>
      <key>PayloadVersion</key>
      <integer>1</integer>
    </dict>
  </array>
  <key>PayloadDisplayName</key>
  <string>YesTechie VTH HTTPS</string>
  <key>PayloadIdentifier</key>
  <string>labs.yestechie.vth</string>
  <key>PayloadRemovalDisallowed</key>
  <false/>
  <key>PayloadType</key>
  <string>Configuration</string>
  <key>PayloadUUID</key>
  <string>${UUID2}</string>
  <key>PayloadVersion</key>
  <integer>1</integer>
</dict>
</plist>
EOF

sudo cp "$TMP/vth.crt" "$SSL_DIR/vth.crt"
sudo cp "$TMP/vth.key" "$SSL_DIR/vth.key"
sudo cp "$TMP/vth.mobileconfig" "$SSL_DIR/vth.mobileconfig"
sudo chmod 644 "$SSL_DIR/vth.crt" "$SSL_DIR/vth.mobileconfig"
sudo chmod 640 "$SSL_DIR/vth.key"

echo "Wrote $SSL_DIR/vth.crt with SAN $SAN"
