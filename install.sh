#!/usr/bin/env bash
# VoIP to Horns installer for Raspberry Pi (UniFi Talk -> Protect AI Horns paging).
#
#   Install or upgrade:  curl -fsSL https://labs.yestechie.com/install/voiptohorns.sh | bash
#   Uninstall:           curl -fsSL https://labs.yestechie.com/install/voiptohorns.sh | bash -s -- --uninstall
#
# Run as a normal user with sudo rights, not as root. Prompts read from the terminal,
# so piping through bash works. Re-running upgrades in place and keeps your settings.
#
# Environment overrides:
#   VTH_BASE_URL     where releases live (default https://labs.yestechie.com/install)
#   VTH_VERSION      install this version instead of the one named in "latest"
#   VTH_FORCE=1      skip the Raspberry Pi model / OS check (testing only)
#   VTH_RECONFIGURE=1  ask for a new dashboard password on upgrade
set -euo pipefail
# ufw, nginx and ip live in /usr/sbin, which is not on a normal user's PATH on Debian.
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

# ---------------------------------------------------------------- terms notice (edit text here)
# Shown on a fresh install, right after the banner and before anything changes.
# Not shown on upgrade (existing /opt/voiptohorns) or with --uninstall.
TERMS_NOTICE='VoIP to Horns is published for personal use only.
For business use, or for an integration built for your site,
contact us at yestechie.com/contact

By installing, you agree to the YesTechie Labs terms:
labs.yestechie.com/terms.html'
TERMS_PROMPT='Continue? [y/N] '

VTH_BASE_URL="${VTH_BASE_URL:-https://labs.yestechie.com/install}"
VTH_BASE_URL="${VTH_BASE_URL%/}"
VTH_FORCE="${VTH_FORCE:-0}"
VTH_RECONFIGURE="${VTH_RECONFIGURE:-0}"

APP_DIR=/opt/voiptohorns
SVC_USER=voiptohorns
BRIDGE_UNIT=voiptohorns-bridge.service
DASH_UNIT=voiptohorns-dashboard.service
NGINX_SITE=/etc/nginx/sites-available/voiptohorns
NGINX_LINK=/etc/nginx/sites-enabled/voiptohorns
NGINX_DEFAULT_LINK=/etc/nginx/sites-enabled/default
SUDOERS_FILE=/etc/sudoers.d/voiptohorns
SSL_DIR=/etc/nginx/ssl
UNIT_DIR=/etc/systemd/system
MARK_NGINX_DEFAULT=.nginx-default-disabled
# Files the app writes at runtime; carried over on upgrade and restored on rollback.
KEEP_FILES=(.env extensions.json dashboard/state.json dashboard/.flask_secret bridge_tcp.log "$MARK_NGINX_DEFAULT")

MODE=install
VERSION=""
TMP_DIR=""
KEEPALIVE_PID=""
PREV_DIR=""

# ---------------------------------------------------------------- output helpers
if [ -t 1 ]; then
    C_BOLD=$'\033[1m'; C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_OFF=$'\033[0m'
else
    C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_OFF=""
fi

step() { printf '\n%s==>%s %s\n' "$C_BOLD" "$C_OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%sWARN%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '\n%sERROR%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

is_utf8() {  # effective charset: locale charmap, else LC_ALL / LC_CTYPE / LANG
    local cm=""
    cm="$(locale charmap 2>/dev/null)" || cm=""
    if [ -z "$cm" ]; then cm="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}"; fi
    case "$cm" in *UTF-8*|*utf-8*|*UTF8*|*utf8*) return 0 ;; esac
    return 1
}

# Installer banner (Vera, approved 2026-10-25). Both drawings are kept byte for byte;
# {VERSION} is the version being installed. Braille mark when the session is UTF-8 and
# TERM is not "linux" (bare console fonts lack braille); otherwise the console-safe banner
# (only full block, light shade and light box corners, present in the common console fonts).
IFS= read -r -d '' BANNER_BRAILLE <<'BANNER' || true
┏━━

                      ⢀⣠⣤⣶⣶⣦    ⢴⣿⣶⣦⣄⡀
                    ⣠⣶⣿⣿⠿⠛⠋⠁    ⠈⠉⠛⠿⣿⣿⣷⣄
                   ⢾⣿⡿⠋⢀⣠⣴⣾⣿⣿⠄ ⣿⣿⣷⣦⣄⡀⠙⠻⣿⡷
                      ⣴⣿⣿⠿⠛⠉⠁⣀⣀⡈⠉⠛⠻⣿⣿⣦⡀
                      ⠛⠛⠁⣠⣴⣿⣿⣿⣿⣿⣿⣶⣄⠈⠛⠛
                        ⢸⣿⡿⠋⠁⣀⣀⠈⠙⢿⣿⡇
                           ⢠⣾⣿⣿⣿⡄
                           ⢸⣿⣿⣿⣿⡇
                           ⢸⣿⣿⣿⣿⡇
                           ⢸⣿⣿⣿⣿⡇
                           ⠈⠻⢿⡿⠟⠁

      ░█░█░█▀█░▀█▀░█▀█░░░▀█▀░█▀█░░░█░█░█▀█░█▀▄░█▀█░█▀▀
      ░▀▄▀░█░█░░█░░█▀▀░░░░█░░█░█░░░█▀█░█░█░█▀▄░█░█░▀▀█
      ░░▀░░▀▀▀░▀▀▀░▀░░░░░░▀░░▀▀▀░░░▀░▀░▀▀▀░▀░▀░▀░▀░▀▀▀

                     // YESTECHIE LABS
                UniFi PA and Overhead Paging

        VERSION  {VERSION}    SOURCE  labs.yestechie.com

                                                         ━━┛
BANNER
IFS= read -r -d '' BANNER_CONSOLE <<'BANNER' || true
┌──

                       ███        ███
                  ████████        ████████
               ██████                  ██████
              ████    ███████  ███████    ████
                   ███████        ███████
                  ████    ████████    ████
                       ██████████████
                      ████        ████
                           ██████
                          ████████
                          ████████
                          ████████
                          ████████
                          ████████
                            ████

      ░█░█░███░███░███░░░███░███░░░█░█░███░██ ░███░███
      ░█░█░█ █░ █ ░█ █░░░ █ ░█ █░░░█░█░█ █░█ █░█ █░█
      ░█ █░█░█░░█░░███░░░░█░░█░█░░░███░█░█░██ ░█░█░███
      ░ █ ░█░█░░█░░█  ░░░░█░░█░█░░░█ █░█░█░█ █░█░█░  █
      ░░█░░███░███░█░░░░░░█░░███░░░█░█░███░█░█░█░█░███

                     // YESTECHIE LABS
                UniFi PA and Overhead Paging

        VERSION  {VERSION}    SOURCE  labs.yestechie.com

                                                         ──┘
BANNER

banner() {
    local ver="${1:--}" art line pre rest sp1 val sp2 sp3
    local cb="" cy="" cl="" cm="" cg="" co=""
    # Labs tokens: blue #6388b0, yellow #c69c5e, muted #8c949e, green #7a9f82. Name and URL use bold
    # default text color so they read on light and dark terminals.
    # Off when stdout is not a terminal or NO_COLOR is set (no-color.org). 24-bit when the
    # terminal says so (COLORTERM), else the nearest 256-color index.
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        case "${COLORTERM:-}" in
            truecolor|24bit)
                cb=$'\033[38;2;99;136;176m'; cy=$'\033[38;2;198;156;94m'; cl=$'\033[1m'
                cm=$'\033[38;2;140;148;158m'; cg=$'\033[38;2;122;159;130m' ;;
            *)
                cb=$'\033[38;5;67m'; cy=$'\033[38;5;179m'; cl=$'\033[1m'
                cm=$'\033[38;5;246m'; cg=$'\033[38;5;108m' ;;
        esac
        co=$'\033[0m'
    fi
    if is_utf8 && [ "${TERM:-}" != linux ]; then art="$BANNER_BRAILLE"; else art="$BANNER_CONSOLE"; fi
    art="${art%$'\n'}"
    while IFS= read -r line; do
        if [ -z "$line" ]; then
            printf '\n'
        elif [[ "$line" == *"{VERSION}"* ]]; then
            line="${line//\{VERSION\}/$ver}"
            pre="${line%%VERSION*}"; rest="${line#*VERSION}"
            sp1="${rest%%[! ]*}"; rest="${rest#"$sp1"}"
            val="${rest%% *}"; rest="${rest#"$val"}"
            sp2="${rest%%[! ]*}"; rest="${rest#"$sp2"}"; rest="${rest#SOURCE}"
            sp3="${rest%%[! ]*}"; rest="${rest#"$sp3"}"
            printf '%s%sVERSION%s%s%s%s%s%s%sSOURCE%s%s%s%s%s\n' "$pre" "$cm" "$co" "$sp1" "$cg" "$val" "$co" \
                "$sp2" "$cm" "$co" "$sp3" "$cl" "$rest" "$co"
        elif [[ "$line" == *"// "* ]]; then
            pre="${line%%//*}"; rest="${line#*//}"
            printf '%s%s//%s%s%s%s\n' "$pre" "$cy" "$co" "$cl" "$rest" "$co"
        elif [[ "$line" == *"UniFi PA"* ]]; then
            printf '%s%s%s\n' "$cm" "$line" "$co"
        else
            printf '%s%s%s\n' "$cb" "$line" "$co"
        fi
    done <<< "$art"
    printf '\n'
}

# ---------------------------------------------------------------- terminal input
have_tty() { { : </dev/tty; } 2>/dev/null; }

ask() {  # ask VAR "Prompt" [default]
    local __var="$1" __prompt="$2" __def="${3:-}" __val=""
    if [ -n "$__def" ]; then __prompt="$__prompt [$__def]"; fi
    printf '%s: ' "$__prompt" >/dev/tty
    IFS= read -r __val </dev/tty || true
    [ -z "$__val" ] && __val="$__def"
    printf -v "$__var" '%s' "$__val"
}

ask_secret() {  # ask_secret VAR "Prompt"
    local __var="$1" __prompt="$2" __val=""
    printf '%s: ' "$__prompt" >/dev/tty
    IFS= read -r -s __val </dev/tty || true
    printf '\n' >/dev/tty
    printf -v "$__var" '%s' "$__val"
}

confirm() {  # confirm "Question" -> 0 if the user types yes
    local ans=""
    printf '%s Type "yes" to continue: ' "$1" >/dev/tty
    IFS= read -r ans </dev/tty || true
    [ "$ans" = "yes" ]
}

accept_terms() {
    local ans=""
    printf '%s\n\n' "$TERMS_NOTICE" >/dev/tty
    printf '%s' "$TERMS_PROMPT" >/dev/tty
    IFS= read -r ans </dev/tty || { ans=""; printf '\n' >/dev/tty; }
    case "$ans" in
        y|Y|yes|YES|Yes) return 0 ;;
    esac
    printf 'Nothing was installed.\n' >/dev/tty
    exit 1
}

# ---------------------------------------------------------------- cleanup
cleanup() {
    if [ -n "$KEEPALIVE_PID" ]; then kill "$KEEPALIVE_PID" 2>/dev/null || true; fi
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
}
trap cleanup EXIT

# ---------------------------------------------------------------- checks
usage() {
    cat <<EOF
Usage: voiptohorns.sh [--uninstall] [--help]

  Install or upgrade:  curl -fsSL $VTH_BASE_URL/voiptohorns.sh | bash
  Uninstall:           curl -fsSL $VTH_BASE_URL/voiptohorns.sh | bash -s -- --uninstall

Options:
  --uninstall          remove VoIP to Horns from this Pi
  -h, --help           show this help

Environment settings (put them before bash, for example: curl ... | VTH_RECONFIGURE=1 bash):
  VTH_RECONFIGURE=1    on upgrade, ask for a new dashboard password
  VTH_VERSION=<V>      install this version instead of the latest one
  VTH_BASE_URL=<url>   where releases are downloaded from (default https://labs.yestechie.com/install)
  VTH_FORCE=1          skip the Raspberry Pi model and OS check (testing only)
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --uninstall) MODE=uninstall ;;
            -h|--help) usage; exit 0 ;;
            *) die "Unknown option: $1 (use --uninstall or --help)" ;;
        esac
        shift
    done
}

check_not_root() {
    if [ "$(id -u)" -eq 0 ]; then
        die "Please run this as your normal user, not as root (no sudo in front).
      The installer asks for your password once when it needs admin rights."
    fi
}

check_platform() {
    local model="" arch codename os_id
    [ -r /proc/device-tree/model ] && model="$(tr -d '\0' </proc/device-tree/model)"
    arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
    # shellcheck disable=SC1091
    codename="$(. /etc/os-release 2>/dev/null && printf '%s' "${VERSION_CODENAME:-}")"
    # shellcheck disable=SC1091
    os_id="$(. /etc/os-release 2>/dev/null && printf '%s' "${ID:-}")"

    if [ "$VTH_FORCE" = "1" ]; then
        warn "VTH_FORCE=1: skipping the Raspberry Pi check (model='${model:-unknown}', arch=$arch, os=$os_id $codename)."
        case "$arch" in arm64|amd64) ;; *) die "Only arm64 or amd64 is supported, even with VTH_FORCE=1 (found $arch)." ;; esac
        case "$codename" in bookworm|trixie) ;; *) die "Debian 12 (bookworm) or 13 (trixie) is required (found '${codename:-unknown}')." ;; esac
        return 0
    fi
    case "$model" in
        "Raspberry Pi 4"*|"Raspberry Pi 5"*) ;;
        *) die "This installer supports Raspberry Pi 4 and Raspberry Pi 5 only (found: ${model:-not a Raspberry Pi})." ;;
    esac
    [ "$arch" = "arm64" ] || die "64-bit Raspberry Pi OS is required (found $arch). Flash the 64-bit Raspberry Pi OS Lite image."
    case "$codename" in
        bookworm|trixie) ;;
        *) die "Raspberry Pi OS based on Debian 12 (bookworm) or 13 (trixie) is required (found '${codename:-unknown}')." ;;
    esac
    [ -d /run/systemd/system ] || die "systemd is not running; this installer needs a normal Raspberry Pi OS boot."
    info "Detected $model, $arch, Debian $codename."
}

check_tools() {
    command -v curl >/dev/null || die "curl is missing. Run: sudo apt-get install -y curl"
    command -v sudo >/dev/null || die "sudo is missing. Log in as a user with sudo rights."
    command -v sha256sum >/dev/null || die "sha256sum is missing (coreutils)."
    have_tty || die "No terminal available for questions. Run the command in an interactive SSH session."
}

start_sudo() {
    info "${1:-Admin rights are needed to install packages, services and the firewall rules.}"
    sudo -v || die "sudo failed. Your user needs sudo rights."
    ( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done ) &
    KEEPALIVE_PID=$!
}

# ---------------------------------------------------------------- download
resolve_version() {
    if [ -n "${VTH_VERSION:-}" ]; then
        VERSION="$VTH_VERSION"
    else
        VERSION="$(curl -fsSL "$VTH_BASE_URL/latest" | tr -d '[:space:]')" \
            || die "Could not reach $VTH_BASE_URL/latest. Check the internet connection."
    fi
    [[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+-]*$ ]] || die "The release server returned an invalid version ('$VERSION')."
}

download_release() {
    local name="voiptohorns-$VERSION.tar.gz"
    TMP_DIR="$(mktemp -d)"
    step "Downloading VoIP to Horns $VERSION"
    curl -fsSL -o "$TMP_DIR/$name" "$VTH_BASE_URL/$name" || die "Download failed: $VTH_BASE_URL/$name"
    curl -fsSL -o "$TMP_DIR/$name.sha256" "$VTH_BASE_URL/$name.sha256" || die "Download failed: $VTH_BASE_URL/$name.sha256"
    local want got
    want="$(awk 'NR==1{print $1}' "$TMP_DIR/$name.sha256")"
    got="$(sha256sum "$TMP_DIR/$name" | awk '{print $1}')"
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "The checksum file is malformed."
    [ "$want" = "$got" ] || die "Checksum mismatch for $name. The download is corrupt or was tampered with; nothing was installed."
    info "Checksum verified (sha256 $got)."
    mkdir -p "$TMP_DIR/src"
    tar -xzf "$TMP_DIR/$name" -C "$TMP_DIR/src" --strip-components=1 --no-same-owner
    [ -f "$TMP_DIR/src/voip_bridge_tcp.py" ] && [ -f "$TMP_DIR/src/dashboard/app.py" ] \
        || die "The release archive is missing files."
    local inner
    inner="$(tr -d '[:space:]' <"$TMP_DIR/src/VERSION")"
    [ "$inner" = "$VERSION" ] || die "Release archive says version '$inner' but '$VERSION' was requested."
}

# ---------------------------------------------------------------- questions
CONSOLE=""; DASH_PASS=""

ask_questions() {
    step "Dashboard password"
    printf '%s\n' "Choose the password for the VoIP to Horns web dashboard (username: admin)." \
        "The password is stored only as a hash. You connect your UniFi console later in the dashboard." >/dev/tty
    local again=""
    while :; do
        ask_secret DASH_PASS "Dashboard password (hidden, at least 8 characters)"
        if [ "${#DASH_PASS}" -lt 8 ]; then printf 'Too short.\n' >/dev/tty; continue; fi
        ask_secret again "Repeat dashboard password"
        [ "$DASH_PASS" = "$again" ] && break
        printf 'The passwords do not match. Try again.\n' >/dev/tty
    done
    again=""
}

# Single-quote a value for .env (python-dotenv): escape backslash and single quote.
envq() {
    local v="$1"
    v="${v//\\/\\\\}"
    v="${v//\'/\\\'}"
    printf "'%s'" "$v"
}

write_env() {
    # Fresh install: base .env without a console (the dashboard Config tab saves it).
    # VTH_RECONFIGURE=1 on an existing install: only the dashboard password changes.
    if ! sudo test -f "$APP_DIR/.env"; then
        {
            printf '# VoIP to Horns settings. Written by install.sh; the dashboard Config tab updates it.\n'
            printf 'UNIFI_IP=\n'
            printf 'UNIFI_USERNAME=\n'
            printf 'UNIFI_PASSWORD=\n'
            printf 'SIP_SERVER=\n'
            printf 'SIP_PORT=5060\n'
            printf '# Empty = auto-detect this Pi'"'"'s LAN address toward the console.\n'
            printf 'MY_IP=\n'
            printf 'RTP_PORT=10000\n'
        } | sudo -u "$SVC_USER" sh -c "umask 077; cat > '$APP_DIR/.env'"
    fi
    printf '%s\n' "$DASH_PASS" | sudo -u "$SVC_USER" "$APP_DIR/venv/bin/python3" \
        "$APP_DIR/dashboard/dashboard_auth.py" set-login "$APP_DIR/.env" \
        || die "Could not save the dashboard password."
    DASH_PASS=""
    sudo chmod 600 "$APP_DIR/.env"
    sudo chown "$SVC_USER:$SVC_USER" "$APP_DIR/.env"
    info "Wrote $APP_DIR/.env (mode 600, owner $SVC_USER)."
}

# ---------------------------------------------------------------- install steps
# apt/dpkg can be busy on first boot (unattended-upgrades, apt-daily) or left
# half-done by an interrupted update; handle both before installing anything.
APT_LOCK_TIMEOUT="${VTH_APT_LOCK_TIMEOUT:-600}"
APT_LOCKS="/var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock"
APT_OPTS=(-o "DPkg::Lock::Timeout=$APT_LOCK_TIMEOUT" -o Acquire::Retries=3)

apt_busy() {
    local f held
    # lslocks (util-linux, always present) lists held POSIX locks; fuser (psmisc) if installed.
    held="$(sudo lslocks -n -o PATH 2>/dev/null || true)"
    for f in $APT_LOCKS; do
        if printf '%s\n' "$held" | grep -qxF "$f"; then return 0; fi
        if command -v fuser >/dev/null 2>&1 && sudo test -e "$f" && sudo fuser "$f" >/dev/null 2>&1; then return 0; fi
    done
    pgrep -x 'apt|apt-get|aptitude|dpkg|unattended-upgr' >/dev/null 2>&1
}

wait_for_apt() {
    local waited=0 said=0
    while apt_busy; do
        if [ "$said" = 0 ]; then
            info "Waiting for a system update that is already running..."
            said=1
        fi
        if [ "$waited" -ge "$APT_LOCK_TIMEOUT" ]; then
            local span="$((APT_LOCK_TIMEOUT / 60)) minutes"
            [ "$APT_LOCK_TIMEOUT" -lt 120 ] && span="$APT_LOCK_TIMEOUT seconds"
            die "A system update is still running after waiting $span. Let it finish (or reboot the Pi), then re-run the command. Nothing else was changed."
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 0
}

dpkg_interrupted() {
    [ -n "$(sudo find /var/lib/dpkg/updates -maxdepth 1 -type f 2>/dev/null | head -n1)" ] && return 0
    [ -n "$(sudo dpkg --audit 2>/dev/null)" ]
}

finish_dpkg() {
    info "Finishing an interrupted system update first..."
    wait_for_apt
    sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a --force-confdef --force-confold >/dev/null \
        || die "Could not finish the interrupted system update. Run: sudo dpkg --configure -a  then re-run the command."
}

apt_update() {
    local n out
    for n in 1 2 3; do
        wait_for_apt
        if out="$(sudo DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" update -qq 2>&1)"; then
            # exit 0 with W: lines (e.g. one mirror hiccup) is fine
            if printf '%s' "$out" | grep -q '^W:'; then
                info "Some package sources could not be reached; using the package lists already on this Pi."
            fi
            return 0
        fi
        if printf '%s' "$out" | grep -q 'dpkg was interrupted'; then finish_dpkg; continue; fi
        if [ "$n" -lt 3 ]; then
            warn "Updating the package lists failed (attempt $n of 3); retrying in $((n * 10)) seconds."
            sleep $((n * 10))
        fi
    done
    if [ -n "$(sudo find /var/lib/apt/lists -maxdepth 1 -name '*_Packages*' -print -quit 2>/dev/null)" ]; then
        warn "Could not refresh the package lists (mirror problem); continuing with the lists already on this Pi."
        return 0
    fi
    printf '%s\n' "$out" >&2
    die "apt-get update failed (network or package mirror problem). Nothing else was changed; re-run the command later."
}

apt_install() {
    local n out
    for n in 1 2; do
        wait_for_apt
        if out="$(sudo DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" install -y -qq \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@" 2>&1)"; then
            return 0
        fi
        if [ "$n" = 1 ] && printf '%s' "$out" | grep -q 'dpkg was interrupted'; then finish_dpkg; continue; fi
        break
    done
    printf '%s\n' "$out" | grep -E '^(E|W):' >&2 || printf '%s\n' "$out" | tail -n 20 >&2
    die "Installing system packages failed. Check the apt messages above, then re-run the command."
}

install_packages() {
    step "Installing system packages (python3, ffmpeg, nginx)"
    wait_for_apt
    if dpkg_interrupted; then finish_dpkg; fi
    apt_update
    apt_install python3 python3-venv python3-pip ffmpeg nginx openssl ca-certificates curl
}

ensure_user() {
    if id "$SVC_USER" >/dev/null 2>&1; then
        info "Service user $SVC_USER exists."
    else
        sudo useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
        info "Created service user $SVC_USER."
    fi
}

stop_services() {
    sudo systemctl stop "$DASH_UNIT" "$BRIDGE_UNIT" 2>/dev/null || true
}

# Move the current install aside so it can be restored if the new one fails.
backup_current() {
    PREV_DIR=""
    if [ -d "$APP_DIR" ]; then
        PREV_DIR="$APP_DIR.prev-$(date +%Y%m%d%H%M%S)"
        step "Backing up the current install to $PREV_DIR"
        stop_services
        sudo mv "$APP_DIR" "$PREV_DIR"
        filter_own_zones "$PREV_DIR"
    fi
}

# Keep only this install's own zones in a tree's extensions.json (the backup, and so also the
# copy carried into the new version and any rollback). Own = state.json "owned" (3.3.2+) or
# "owned_exts" (3.3.1); a tree with neither record keeps no zones. Prints numbers only.
filter_own_zones() {
    local dir="$1"
    sudo test -f "$dir/extensions.json" || return 0
    sudo python3 - "$dir/extensions.json" "$dir/dashboard/state.json" <<'PY' || warn "Could not check the zones kept in $dir/extensions.json."
import json, os, sys
ext_path, state_path = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(ext_path))
except Exception:
    sys.exit(0)
try:
    st = json.load(open(state_path))
except Exception:
    st = {}
own = st.get("owned") if isinstance(st.get("owned"), dict) else st.get("owned_exts")
own = {str(e).strip().zfill(4) for e in (own or []) if str(e).strip().isdigit()}
exts = data.get("extensions") or {}
drop = sorted(e for e in exts if str(e).strip().zfill(4) not in own)
if not drop:
    sys.exit(0)
data["extensions"] = {e: v for e, v in exts.items() if e not in drop}
info = os.stat(ext_path)
tmp = ext_path + ".tmp-install"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
os.chown(tmp, info.st_uid, info.st_gid)
os.replace(tmp, ext_path)
print("    Kept only zones created by this install; removed from the backup: " + ", ".join(drop))
PY
}

lay_down_files() {
    step "Installing files to $APP_DIR"
    sudo mkdir -p "$APP_DIR"
    sudo cp -a "$TMP_DIR/src/." "$APP_DIR/"
    if [ -n "$PREV_DIR" ]; then
        local f
        for f in "${KEEP_FILES[@]}"; do
            if sudo test -e "$PREV_DIR/$f"; then
                sudo mkdir -p "$(dirname "$APP_DIR/$f")"
                sudo cp -a "$PREV_DIR/$f" "$APP_DIR/$f"
            fi
        done
        info "Kept settings, zones and sessions from the previous install."
    fi
    sudo chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
    sudo chmod 750 "$APP_DIR"
}

build_venv() {
    step "Creating the Python environment (this can take a few minutes on a Pi)"
    sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/venv"
    sudo -u "$SVC_USER" "$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
}

install_services() {
    step "Installing services"
    sudo install -m 644 "$APP_DIR/voiptohorns-bridge.service" "$UNIT_DIR/$BRIDGE_UNIT"
    sudo install -m 644 "$APP_DIR/voiptohorns-dashboard.service" "$UNIT_DIR/$DASH_UNIT"
    sudo visudo -cf "$APP_DIR/dashboard/sudoers-voiptohorns" >/dev/null || die "The sudoers rule failed validation."
    sudo install -m 440 -o root -g root "$APP_DIR/dashboard/sudoers-voiptohorns" "$SUDOERS_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable "$BRIDGE_UNIT" "$DASH_UNIT" >/dev/null 2>&1
}

detect_ip() {
    local ip=""
    ip="$(ip -4 route get "$CONSOLE_IP_FOR_ROUTE" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')" || true
    [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '%s' "$ip"
}

setup_https() {
    step "Setting up HTTPS on port 443"
    if sudo test -f "$SSL_DIR/vth.crt" && sudo test -f "$SSL_DIR/vth.key"; then
        info "Keeping the existing certificate in $SSL_DIR."
    else
        if ! LAN_IP="$PI_IP" HOST_NAME="$(hostname)" SSL_DIR="$SSL_DIR" \
            bash "$TMP_DIR/src/dashboard/setup-https.sh" >"$TMP_DIR/https.log" 2>&1; then
            cat "$TMP_DIR/https.log" >&2
            die "Could not create the HTTPS certificate."
        fi
        info "Created a self-signed certificate for $PI_IP."
    fi
    sudo install -m 644 "$APP_DIR/dashboard/nginx-voiptohorns.conf" "$NGINX_SITE"
    sudo ln -sfn "$NGINX_SITE" "$NGINX_LINK"
    if [ -L "$NGINX_DEFAULT_LINK" ]; then
        sudo rm -f "$NGINX_DEFAULT_LINK"
        sudo touch "$APP_DIR/$MARK_NGINX_DEFAULT"
        info "Disabled the nginx default site (it also listens on port 80)."
    fi
    sudo nginx -t >/dev/null 2>&1 || die "nginx configuration test failed. Run: sudo nginx -t"
    sudo systemctl enable nginx >/dev/null 2>&1 || true
    sudo systemctl restart nginx
}

setup_firewall() {
    step "Firewall"
    if ! command -v ufw >/dev/null; then
        info "ufw is not installed; no firewall changes. Open 443/tcp, 5060/tcp and 10000/udp if you use another firewall."
        return 0
    fi
    sudo ufw allow 443/tcp comment 'VoIP to Horns dashboard' >/dev/null
    sudo ufw allow 5060/tcp comment 'VoIP to Horns SIP' >/dev/null
    sudo ufw allow 10000/udp comment 'VoIP to Horns RTP' >/dev/null
    if sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
        info "ufw is active; allowed 443/tcp, 5060/tcp and 10000/udp."
    else
        info "ufw is installed but not enabled; rules for 443/tcp, 5060/tcp, 10000/udp were added for later."
        info "If you enable ufw yourself, allow SSH first: sudo ufw allow 22/tcp"
    fi
}

services_up() {
    local _
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if sudo systemctl is-active --quiet "$BRIDGE_UNIT" && sudo systemctl is-active --quiet "$DASH_UNIT" \
            && curl -fs -o /dev/null --max-time 3 http://127.0.0.1:5050/api/version; then
            return 0
        fi
        sleep 2
    done
    return 1
}

start_services() {
    step "Starting services"
    sudo systemctl restart "$BRIDGE_UNIT" "$DASH_UNIT"
    if services_up; then
        info "Bridge and dashboard are running."
        return 0
    fi
    return 1
}

rollback() {
    warn "The new version did not start. Rolling back."
    stop_services
    sudo rm -rf "$APP_DIR"
    sudo mv "$PREV_DIR" "$APP_DIR"
    sudo install -m 644 "$APP_DIR/voiptohorns-bridge.service" "$UNIT_DIR/$BRIDGE_UNIT" 2>/dev/null || true
    sudo install -m 644 "$APP_DIR/voiptohorns-dashboard.service" "$UNIT_DIR/$DASH_UNIT" 2>/dev/null || true
    sudo systemctl daemon-reload
    sudo systemctl restart "$BRIDGE_UNIT" "$DASH_UNIT" || true
    if services_up; then
        die "Upgrade failed; the previous version was restored and is running. Logs: journalctl -u $DASH_UNIT -u $BRIDGE_UNIT"
    fi
    die "Upgrade failed and the previous version did not start either. Logs: journalctl -u $DASH_UNIT -u $BRIDGE_UNIT"
}

prune_backups() {
    local d
    for d in "$APP_DIR".prev-*; do
        [ -d "$d" ] || continue
        [ "$d" = "$PREV_DIR" ] && continue
        sudo rm -rf "$d"
    done
    [ -n "$PREV_DIR" ] && info "Previous version kept at $PREV_DIR (removed by the next upgrade or --uninstall)."
    return 0
}

# ---------------------------------------------------------------- self-test
SELFTEST_FAIL=0
self_test() {
    step "Self-test"
    local u
    for u in "$BRIDGE_UNIT" "$DASH_UNIT" nginx; do
        if sudo systemctl is-active --quiet "$u"; then
            printf '  %sPASS%s Service %s - running\n' "$C_GREEN" "$C_OFF" "$u"
        else
            printf '  %sFAIL%s Service %s - not running; see: journalctl -u %s\n' "$C_RED" "$C_OFF" "$u" "$u"
            SELFTEST_FAIL=1
        fi
    done

    local code
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "https://$PI_IP/login" || true)"
    if [ "$code" = "200" ]; then
        printf '  %sPASS%s Dashboard - answers on https://%s/\n' "$C_GREEN" "$C_OFF" "$PI_IP"
    else
        printf '  %sFAIL%s Dashboard - https://%s/login returned %s; check: sudo nginx -t and journalctl -u %s\n' \
            "$C_RED" "$C_OFF" "$PI_IP" "${code:-no answer}" "$DASH_UNIT"
        SELFTEST_FAIL=1
    fi
}

# ---------------------------------------------------------------- uninstall
talk_cleanup_text() {
    case "${1:-unknown}" in
        none)
            printf '  - UniFi Talk: this install had no zones, so there is nothing to delete there.' ;;
        unknown|"")
            printf '  - UniFi Talk: this install kept no list of its zones. Delete only the users and\n'
            printf '    third-party phones you created for this Pi; leave the ones another device uses.' ;;
        *)
            printf '  - UniFi Talk: delete the user and third-party phone of these extensions only\n'
            printf '    (created by this install): %s\n' "$1"
            printf '    Leave all other Talk users and phones; they may belong to another device.' ;;
    esac
}

uninstall() {
    local ver=""
    ver="$(sudo -n cat "$APP_DIR/VERSION" 2>/dev/null | tr -d '[:space:]')" || ver=""
    banner "$ver"
    check_not_root
    check_tools
    printf 'This removes VoIP to Horns: services, nginx site, sudoers rule, certificate,\n%s and the %s user.\n' "$APP_DIR" "$SVC_USER" >/dev/tty
    confirm "Uninstall VoIP to Horns?" || { info "Nothing was changed."; exit 0; }
    start_sudo "Admin rights are needed to remove the services, files and user."
    local own_zones=""
    own_zones="$(sudo python3 - "$APP_DIR/dashboard/state.json" <<'PY' 2>/dev/null
import json, sys
try:
    st = json.load(open(sys.argv[1]))
except Exception:
    print("unknown"); sys.exit(0)
own = st.get("owned") if isinstance(st.get("owned"), dict) else st.get("owned_exts")
if own is None:
    print("unknown"); sys.exit(0)
print(", ".join(sorted(str(e).strip().zfill(4) for e in own if str(e).strip().isdigit())) or "none")
PY
)" || own_zones=""

    step "Stopping and removing services"
    sudo systemctl disable --now "$DASH_UNIT" "$BRIDGE_UNIT" >/dev/null 2>&1 || true
    sudo rm -f "$UNIT_DIR/$BRIDGE_UNIT" "$UNIT_DIR/$DASH_UNIT"
    sudo systemctl daemon-reload || true

    step "Removing nginx site, certificate and sudoers rule"
    local restore_default=0
    sudo test -e "$APP_DIR/$MARK_NGINX_DEFAULT" && restore_default=1
    sudo rm -f "$NGINX_LINK" "$NGINX_SITE" "$SUDOERS_FILE"
    sudo rm -f "$SSL_DIR/vth.crt" "$SSL_DIR/vth.key" "$SSL_DIR/vth.mobileconfig"
    if [ "$restore_default" = 1 ] && [ -e /etc/nginx/sites-available/default ]; then
        sudo ln -sfn /etc/nginx/sites-available/default "$NGINX_DEFAULT_LINK"
        info "Re-enabled the nginx default site."
    fi
    if command -v nginx >/dev/null && sudo nginx -t >/dev/null 2>&1; then
        sudo systemctl reload nginx 2>/dev/null || sudo systemctl restart nginx 2>/dev/null || true
    fi

    step "Removing $APP_DIR and the $SVC_USER user"
    sudo rm -rf "$APP_DIR" "$APP_DIR".prev-*
    if id "$SVC_USER" >/dev/null 2>&1; then sudo userdel "$SVC_USER" 2>/dev/null || true; fi

    cat <<EOF

VoIP to Horns was removed.

Still to do by hand:
$(talk_cleanup_text "$own_zones")
  - Firewall rules (if ufw is installed) were left in place:
      sudo ufw delete allow 443/tcp; sudo ufw delete allow 5060/tcp; sudo ufw delete allow 10000/udp
  - System packages (nginx, ffmpeg, python3-venv) were left installed.
EOF
}

# ---------------------------------------------------------------- main
main() {
    parse_args "$@"
    if [ "$MODE" = uninstall ]; then uninstall; return; fi

    check_not_root
    resolve_version
    banner "$VERSION"
    check_tools
    if [ ! -d "$APP_DIR" ]; then accept_terms; fi

    step "Checking this computer"
    check_platform
    start_sudo

    download_release

    local upgrade=0
    if sudo test -f "$APP_DIR/.env"; then upgrade=1; fi
    if [ "$upgrade" = 1 ] && [ "$VTH_RECONFIGURE" != "1" ]; then
        local cur
        cur="$(sudo cat "$APP_DIR/VERSION" 2>/dev/null | tr -d '[:space:]')" || cur=""
        if [ "$cur" = "$VERSION" ]; then
            step "Reinstalling $VERSION (settings are kept)"
        else
            step "Upgrading from ${cur:-unknown} to $VERSION (settings are kept)"
        fi
        info "To change the dashboard password, run: curl -fsSL $VTH_BASE_URL/voiptohorns.sh | VTH_RECONFIGURE=1 bash"
        CONSOLE="$(sudo sed -n 's/^UNIFI_IP=//p' "$APP_DIR/.env" | head -n1 | tr -d "'\"")"
    else
        ask_questions
    fi

    install_packages
    ensure_user
    backup_current
    lay_down_files
    build_venv
    if [ "$upgrade" = 0 ] || [ "$VTH_RECONFIGURE" = "1" ]; then write_env; fi

    CONSOLE_IP_FOR_ROUTE="${CONSOLE:-1.1.1.1}"
    PI_IP="$(detect_ip)"
    [ -n "$PI_IP" ] || PI_IP="127.0.0.1"

    install_services
    setup_https
    setup_firewall

    if ! start_services; then
        if [ -n "$PREV_DIR" ]; then rollback; fi
        die "Services did not start. Logs: journalctl -u $DASH_UNIT -u $BRIDGE_UNIT"
    fi
    prune_backups

    self_test

    cat <<EOF

${C_BOLD}VoIP to Horns $VERSION is installed.${C_OFF}

  Dashboard:  https://$PI_IP/   (accept the self-signed certificate, or install it from Config)
  Log in as admin to connect your UniFi console.

Re-run the same command to upgrade. To remove it:
  curl -fsSL $VTH_BASE_URL/voiptohorns.sh | bash -s -- --uninstall
EOF
    if [ "$SELFTEST_FAIL" = 1 ]; then
        warn "Some self-test checks failed. Fix the items marked FAIL, then re-run the command to check again."
    fi
}

CONSOLE_IP_FOR_ROUTE=""
PI_IP=""
main "$@"
