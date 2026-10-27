#!/usr/bin/env bash
#
#  ldap-phonebook — uninstaller.
#
#  By default it removes the web UI only. The directory (and therefore the phone
#  book itself) is left untouched: removing a working directory by accident is
#  much worse than leaving a package installed.
#
set -Eeuo pipefail

APP_NAME="ldap-phonebook"
VERSION="$(tr -d '[:space:]' < "$(dirname -- "${BASH_SOURCE[0]}")/VERSION" 2>/dev/null || echo "0.0.0")"

APP_USER="${PB_APP_USER:-phonebook}"
APP_DIR="${PB_APP_DIR:-/opt/ldap-phonebook}"
CONF_DIR="${PB_CONF_DIR:-/etc/ldap-phonebook}"
SERVICE="ldap-phonebook"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
CRED_FILE="/root/ldap-phonebook-credentials.txt"

# the installer records the real paths and user name here; trust it over defaults
STATE_FILE="${CONF_DIR}/install.state"
if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    . "${STATE_FILE}"
    APP_DIR="${PB_APP_DIR:-${APP_DIR}}"
    APP_USER="${PB_APP_USER:-${APP_USER}}"
fi

PURGE_LDAP="no"
KEEP_APP="no"
ASSUME_YES="no"

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'
else
    C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""
fi
step() { printf '\n%s==> %s%s\n' "${C_BOLD}" "$*" "${C_RESET}"; }
ok()   { printf '    %s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '    %s!%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '\n%sERROR:%s %s\n' "${C_RED}${C_BOLD}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
    cat <<EOF
${APP_NAME} ${VERSION} — uninstaller

Usage: sudo ./uninstall.sh [options]

  --purge-ldap     also purge slapd and DELETE THE DIRECTORY DATA (irreversible)
  --keep-app       keep ${APP_DIR} (code and virtualenv), remove only the service
  --yes            do not ask for confirmation
  -h, --help       this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge-ldap) PURGE_LDAP="yes"; shift ;;
        --keep-app)   KEEP_APP="yes"; shift ;;
        --yes|-y)     ASSUME_YES="yes"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
done

[[ "${EUID}" -eq 0 ]] || die "run as root: sudo ./uninstall.sh"

step "This will"
echo "  • stop and remove the service ${SERVICE}"
[[ "${KEEP_APP}" == "no" ]] && echo "  • delete ${APP_DIR}"
echo "  • delete ${CONF_DIR} and ${CRED_FILE}"
if [[ "${PURGE_LDAP}" == "yes" ]]; then
    echo "  • ${C_RED}purge slapd and delete all directory data${C_RESET}"
else
    echo "  • keep OpenLDAP and its data (use --purge-ldap to remove them)"
fi

if [[ "${ASSUME_YES}" != "yes" && -t 0 ]]; then
    echo
    read -r -p "Continue? [y/N] " answer
    [[ "${answer}" =~ ^[Yy]$ ]] || die "aborted by user"
fi

step "Removing the web UI"
if systemctl list-unit-files "${SERVICE}.service" >/dev/null 2>&1; then
    systemctl stop "${SERVICE}" 2>/dev/null || true
    systemctl disable "${SERVICE}" 2>/dev/null || true
fi
rm -f "${SERVICE_FILE}"
systemctl daemon-reload 2>/dev/null || true
ok "service removed"

if [[ "${KEEP_APP}" == "no" ]]; then
    rm -rf "${APP_DIR}"
    ok "removed ${APP_DIR}"
else
    warn "kept ${APP_DIR}"
fi

rm -rf "${CONF_DIR}" "${CRED_FILE}"
ok "removed configuration and credentials"

if id -u "${APP_USER}" >/dev/null 2>&1; then
    if ! pgrep -u "${APP_USER}" >/dev/null 2>&1; then
        userdel "${APP_USER}" 2>/dev/null || true
        ok "system user ${APP_USER} removed"
    else
        warn "user ${APP_USER} is still running a process, left in place"
    fi
fi

if [[ "${PURGE_LDAP}" == "yes" ]]; then
    step "Purging OpenLDAP"
    warn "this deletes every contact, ACL and the admin account"
    if [[ "${ASSUME_YES}" != "yes" && -t 0 ]]; then
        read -r -p "Type DELETE to confirm data loss: " answer
        [[ "${answer}" == "DELETE" ]] || die "aborted: data kept"
    fi
    systemctl stop slapd 2>/dev/null || true
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq slapd ldap-utils >/dev/null 2>&1 || true
    rm -rf /var/lib/ldap /etc/ldap/slapd.d
    ok "OpenLDAP purged"
else
    step "Done"
    warn "OpenLDAP and the phone book data are still on this machine"
    printf '    to remove them too: sudo ./uninstall.sh --purge-ldap\n'
fi
