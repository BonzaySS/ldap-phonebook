#!/usr/bin/env bash
#
#  ldap-phonebook — a phone book for SIP phones (Yealink / Fanvil) on OpenLDAP
#  Installer for a clean Debian / Ubuntu system.
#
#  What it does:
#    1. installs OpenLDAP and creates the directory (base DN, ou=people)
#    2. creates a read-only account used by the phones and by the web UI
#    3. applies access rules: the admin writes, the read account only reads
#    4. installs the web UI as a systemd service
#    5. prints the credentials and the URL
#
#  The script is idempotent: running it again only fills in what is missing.
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="ldap-phonebook"
APP_VERSION="$(tr -d '[:space:]' < "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo "0.0.0")"

# ----------------------------------------------------------------- options --
DOMAIN="${PB_DOMAIN:-phonebook.local}"
BASE_DN="${PB_BASE_DN:-}"
ORG="${PB_ORG:-Phonebook}"
ADMIN_CN="${PB_ADMIN_CN:-admin}"
READ_USER="${PB_READ_USER:-book}"
PEOPLE_OU="${PB_PEOPLE_OU:-people}"
ADMIN_PW="${PB_ADMIN_PW:-}"
READ_PW="${PB_READ_PW:-}"
APP_PORT="${PB_APP_PORT:-8500}"
APP_BIND="${PB_APP_BIND:-0.0.0.0}"
APP_USER="${PB_APP_USER:-phonebook}"
APP_DIR="${PB_APP_DIR:-/opt/ldap-phonebook}"
CONF_DIR="${PB_CONF_DIR:-/etc/ldap-phonebook}"
UI_TITLE="${PB_TITLE:-Телефонная книга}"
SERVICE="ldap-phonebook"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
LDAP_URI="ldap://127.0.0.1:389"
CRED_FILE="/root/ldap-phonebook-credentials.txt"

IMPORT_CSV=""
WITH_LDAP_UI="no"
LDAP_ONLY="no"
APP_ONLY="no"
DRY_RUN="no"
ASSUME_YES="no"
FORCE_ACL="no"
OPEN_FIREWALL="yes"

# ------------------------------------------------------------------- utils --
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi

step()  { printf '\n%s==> %s%s\n' "${C_BOLD}${C_BLUE}" "$*" "${C_RESET}"; }
say()   { printf '    %s\n' "$*"; }
note()  { printf '    %s%s%s\n' "${C_DIM}" "$*" "${C_RESET}"; }
ok()    { printf '    %s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn()  { printf '    %s!%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()   { trap - ERR; printf '\n%sERROR:%s %s\n' "${C_RED}${C_BOLD}" "${C_RESET}" "$*" >&2; exit 1; }
trap 'die "unexpected failure at line ${LINENO}"' ERR

run() {
    if [[ "${DRY_RUN}" == "yes" ]]; then
        printf '    %s[dry-run]%s %s\n' "${C_DIM}" "${C_RESET}" "$*"
        return 0
    fi
    "$@"
}

gen_password() { head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-20; }

usage() {
    cat <<EOF
${APP_NAME} ${APP_VERSION} — phone book for SIP phones on OpenLDAP

Usage: sudo ./install.sh [options]

Directory:
  --base-dn DN            LDAP base, e.g. dc=example,dc=org (derived from --domain)
  --domain NAME           DNS domain for a fresh OpenLDAP (default: ${DOMAIN})
  --org NAME              organisation name for a fresh OpenLDAP
  --admin-password PW     password for cn=admin,<base-dn>
                          (generated for a fresh install, required for an existing one)
  --read-user NAME        read-only account name (default: ${READ_USER})
  --read-password PW      its password (default: generated)
  --people-ou NAME        OU that holds the contacts (default: ${PEOPLE_OU})

Web UI:
  --port N                HTTP port (default: ${APP_PORT})
  --bind ADDR             listen address (default: ${APP_BIND})
  --app-user NAME         system user for the service (default: ${APP_USER})
  --no-firewall           do not touch ufw

Scope:
  --ldap-only             only set up the directory
  --app-only              only set up the web UI against an existing directory
  --with-ldap-ui          also install ldap-ui (raw attribute editor) on :5000
  --import FILE           import a CSV phone book after installation

Behaviour:
  --dry-run               print every action, change nothing
  --yes                   do not ask for confirmation
  --force-acl             replace access rules even if they are not default
  -h, --help              this help
  -V, --version           print version

Environment variables with the PB_ prefix are accepted as defaults.

Examples:
  sudo ./install.sh
  sudo ./install.sh --base-dn dc=acme,dc=ru --port 8080
  sudo ./install.sh --app-only --base-dn dc=acme,dc=ru --admin-password '***'
  sudo ./install.sh --import people.csv
EOF
}

# ------------------------------------------------------------- arg parsing --
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-dn)        BASE_DN="${2:?--base-dn needs a value}"; shift 2 ;;
        --domain)         DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
        --org)            ORG="${2:?--org needs a value}"; shift 2 ;;
        --admin-password) ADMIN_PW="${2:?--admin-password needs a value}"; shift 2 ;;
        --read-user)      READ_USER="${2:?--read-user needs a value}"; shift 2 ;;
        --read-password)  READ_PW="${2:?--read-password needs a value}"; shift 2 ;;
        --people-ou)      PEOPLE_OU="${2:?--people-ou needs a value}"; shift 2 ;;
        --port)           APP_PORT="${2:?--port needs a value}"; shift 2 ;;
        --bind)           APP_BIND="${2:?--bind needs a value}"; shift 2 ;;
        --app-user)       APP_USER="${2:?--app-user needs a value}"; shift 2 ;;
        --import)         IMPORT_CSV="${2:?--import needs a value}"; shift 2 ;;
        --no-firewall)    OPEN_FIREWALL="no"; shift ;;
        --ldap-only)      LDAP_ONLY="yes"; shift ;;
        --app-only)       APP_ONLY="yes"; shift ;;
        --with-ldap-ui)   WITH_LDAP_UI="yes"; shift ;;
        --dry-run)        DRY_RUN="yes"; shift ;;
        --yes|-y)         ASSUME_YES="yes"; shift ;;
        --force-acl)      FORCE_ACL="yes"; shift ;;
        -h|--help)        usage; exit 0 ;;
        -V|--version)     echo "${APP_NAME} ${APP_VERSION}"; exit 0 ;;
        *)                die "unknown option: $1 (try --help)" ;;
    esac
done

if [[ -z "${BASE_DN}" ]]; then
    BASE_DN="$(echo "${DOMAIN}" | sed -E 's/^\.+//; s/\.+$//; s/\./,dc=/g; s/^/dc=/')"
fi
ADMIN_DN="cn=${ADMIN_CN},${BASE_DN}"
PEOPLE_DN="ou=${PEOPLE_OU},${BASE_DN}"
READ_DN="cn=${READ_USER},${BASE_DN}"
ADMIN_PW_FILE="${CONF_DIR}/admin.pw"

# ------------------------------------------------------------------ cleanup --
WORK_FILES=()
# must never fail: a non-zero status here would turn a successful install into
# a non-zero exit code (and would re-trigger the ERR trap on the way out)
cleanup() {
    trap - ERR
    local f
    for f in "${WORK_FILES[@]-}"; do
        [[ -n "${f}" ]] && rm -f "${f}"
    done
    return 0
}
trap cleanup EXIT

# ---------------------------------------------------------------- preflight --
preflight() {
    step "Preflight checks"
    [[ "${EUID}" -eq 0 ]] || die "run as root: sudo ./install.sh"
    [[ -r /etc/os-release ]] || die "cannot read /etc/os-release"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        debian|ubuntu|raspbian) ok "distribution: ${PRETTY_NAME:-${ID}}" ;;
        *) die "unsupported distribution '${ID:-unknown}': this installer targets Debian/Ubuntu" ;;
    esac
    command -v systemctl >/dev/null || die "systemd is required"
    command -v apt-get  >/dev/null || die "apt-get is required"

    say "base DN      : ${BASE_DN}"
    say "admin DN     : ${ADMIN_DN}"
    say "read account : ${READ_DN}"
    say "people OU    : ${PEOPLE_DN}"
    [[ "${LDAP_ONLY}" != "yes" ]] && say "web UI       : ${APP_BIND}:${APP_PORT} as user ${APP_USER}"
    say "version      : ${APP_NAME} ${APP_VERSION}"
    [[ "${DRY_RUN}" == "yes" ]] && warn "DRY RUN: nothing will be changed"

    if [[ "${ASSUME_YES}" != "yes" && "${DRY_RUN}" != "yes" && -t 0 ]]; then
        echo
        read -r -p "Continue? [y/N] " answer
        [[ "${answer}" =~ ^[Yy]$ ]] || die "aborted by user"
    fi
}

# --------------------------------------------------------------- packages ---
install_packages() {
    step "Installing packages"
    run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    local pkgs=(ca-certificates curl)
    [[ "${APP_ONLY}" != "yes" ]] && pkgs+=(slapd ldap-utils)
    [[ "${LDAP_ONLY}" != "yes" ]] && pkgs+=(python3 python3-venv)
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}"
    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] would install: ${pkgs[*]}"
    else
        ok "installed: ${pkgs[*]}"
    fi
}

# ------------------------------------------------------------------ slapd ---
slapd_suffix() {
    ldapsearch -LLL -Q -Y EXTERNAL -H ldapi:/// -b "" -s base namingContexts 2>/dev/null \
        | sed -n 's/^namingContexts: //p' | head -n1
}

preseed_slapd() {
    [[ -n "${ADMIN_PW}" ]] || ADMIN_PW="$(gen_password)"
    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] would preseed debconf for domain ${DOMAIN}"
        return 0
    fi
    debconf-set-selections <<EOF
slapd slapd/password1 password ${ADMIN_PW}
slapd slapd/password2 password ${ADMIN_PW}
slapd slapd/internal/adminpw password ${ADMIN_PW}
slapd slapd/internal/generated_adminpw password ${ADMIN_PW}
slapd slapd/domain string ${DOMAIN}
slapd shared/organization string ${ORG}
slapd slapd/backend select MDB
slapd slapd/purge_database boolean false
slapd slapd/move_old_database boolean true
slapd slapd/allow_ldap_v2 boolean false
slapd slapd/no_configuration boolean false
EOF
}

setup_slapd() {
    step "Configuring OpenLDAP"
    local existing
    existing="$(slapd_suffix || true)"

    # a freshly installed slapd can configure itself from the machine domain
    # before the preseed lands; reconfiguring is safe only right after install,
    # because a directory that was already there may hold real data
    if [[ -n "${existing}" && "${existing}" != "${BASE_DN}" && "${SLAPD_FRESH}" == "yes" ]]; then
        warn "fresh slapd configured itself as '${existing}', re-running with domain ${DOMAIN}"
        if [[ "${DRY_RUN}" != "yes" ]]; then
            DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive slapd >/dev/null 2>&1 || true
            systemctl restart slapd >/dev/null 2>&1 || true
            sleep 2
        fi
        existing="$(slapd_suffix || true)"
    fi

    if [[ -n "${existing}" ]]; then
        note "slapd is already configured, suffix: ${existing}"
        if [[ "${existing}" != "${BASE_DN}" ]]; then
            die "the existing directory uses '${existing}', this install targets '${BASE_DN}'.
       pass --base-dn ${existing} to reuse it, or --app-only to attach only the web UI"
        fi
        [[ -n "${ADMIN_PW}" ]] || die "slapd already exists: pass --admin-password for ${ADMIN_DN}"
        systemctl enable --now slapd >/dev/null 2>&1 || true
        ok "existing directory reused"
        return 0
    fi

    say "creating a new directory: ${BASE_DN}"
    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] would configure slapd for ${DOMAIN}"
        return 0
    fi
    preseed_slapd
    DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive slapd >/dev/null 2>&1 || true
    systemctl enable --now slapd >/dev/null 2>&1 || true
    sleep 1
    systemctl is-active --quiet slapd || die "slapd failed to start (journalctl -u slapd)"
    ok "directory created: $(slapd_suffix)"
}

db_entry_dn() {
    ldapsearch -LLL -Q -Y EXTERNAL -H ldapi:/// -b cn=config \
        "(olcSuffix=${BASE_DN})" dn 2>/dev/null | sed -n 's/^dn: //p' | head -n1
}

prepare_admin_pwfile() {
    if [[ "${DRY_RUN}" == "yes" ]]; then
        # a dry run still needs a password file for the read-only LDAP checks,
        # but it must not create anything outside /tmp
        local tmp
        tmp="$(mktemp)"
        WORK_FILES+=("${tmp}")
        umask 077
        printf '%s' "${ADMIN_PW}" > "${tmp}"
        umask 022
        chmod 600 "${tmp}"
        ADMIN_PW_FILE="${tmp}"
        note "[dry-run] would create ${CONF_DIR} and ${CONF_DIR}/admin.pw"
        return 0
    fi
    install -d -m 0750 "${CONF_DIR}"
    umask 077
    printf '%s' "${ADMIN_PW}" > "${ADMIN_PW_FILE}"
    chmod 600 "${ADMIN_PW_FILE}"
    umask 022
}

# --------------------------------------------------------------- directory --
ensure_base() {
    step "Preparing the directory tree"
    if ldapsearch -LLL -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" -H "${LDAP_URI}" \
            -b "${BASE_DN}" -s base dn >/dev/null 2>&1; then
        ok "base entry exists: ${BASE_DN}"
    else
        warn "cannot read ${BASE_DN} as ${ADMIN_DN}"
        die "wrong --admin-password, or the directory is not ready"
    fi

    if ldapsearch -LLL -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" -H "${LDAP_URI}" \
            -b "${PEOPLE_DN}" -s base dn >/dev/null 2>&1; then
        ok "people OU exists: ${PEOPLE_DN}"
        return 0
    fi

    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] create ${PEOPLE_DN}"
        return 0
    fi
    ldapadd -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" -H "${LDAP_URI}" >/dev/null <<EOF
dn: ${PEOPLE_DN}
objectClass: organizationalUnit
ou: ${PEOPLE_OU}
EOF
    ok "created ${PEOPLE_DN}"
}

apply_acls() {
    step "Access rules"
    local db current
    db="$(db_entry_dn)"
    [[ -n "${db}" ]] || die "cannot find the database entry for ${BASE_DN} in cn=config"

    current="$(ldapsearch -LLL -Q -Y EXTERNAL -H ldapi:/// -b "${db}" olcAccess 2>/dev/null \
               | sed -n 's/^olcAccess: //p')"

    if grep -qF "cn=${READ_USER},${BASE_DN}" <<<"${current}"; then
        ok "rules already mention ${READ_DN}"
        return 0
    fi

    if ! grep -qF 'to * by * read' <<<"${current}" && [[ "${FORCE_ACL}" != "yes" ]]; then
        warn "the existing access rules do not look like the default ones:"
        sed 's/^/        /' <<<"${current}" >&2
        die "refusing to overwrite them; re-run with --force-acl if that is intended"
    fi

    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] replace olcAccess on ${db}"
        return 0
    fi

    ldapmodify -Q -Y EXTERNAL -H ldapi:/// >/dev/null <<EOF
dn: ${db}
changetype: modify
replace: olcAccess
olcAccess: {0}to attrs=userPassword by dn.exact="${READ_DN}" none by self write by anonymous auth by * none
olcAccess: {1}to attrs=shadowLastChange by self write by * read
olcAccess: {2}to * by dn.exact="${ADMIN_DN}" write by dn.exact="${READ_DN}" read by users read by * none
-
EOF
    ok "access rules applied"
    note "admin: write everything, ${READ_USER}: read everything, anonymous: nothing"
}

ensure_read_account() {
    step "Read-only account for phones"
    if [[ -z "${READ_PW}" ]] && ! ldapsearch -LLL -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" \
            -H "${LDAP_URI}" -b "${READ_DN}" -s base dn >/dev/null 2>&1; then
        READ_PW="$(gen_password)"
    fi

    if ldapsearch -LLL -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" -H "${LDAP_URI}" \
            -b "${READ_DN}" -s base dn >/dev/null 2>&1; then
        if [[ -n "${READ_PW}" && "${DRY_RUN}" != "yes" ]]; then
            local hash
            hash="$(slappasswd -h '{SSHA}' -s "${READ_PW}")"
            ldapmodify -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" -H "${LDAP_URI}" >/dev/null <<EOF
dn: ${READ_DN}
changetype: modify
replace: userPassword
userPassword: ${hash}
-
EOF
            ok "password of ${READ_DN} updated"
        else
            ok "account exists: ${READ_DN}"
        fi
        return 0
    fi

    [[ "${DRY_RUN}" == "yes" ]] && { note "[dry-run] create ${READ_DN}"; return 0; }
    local hash
    hash="$(slappasswd -h '{SSHA}' -s "${READ_PW}")"
    ldapadd -x -D "${ADMIN_DN}" -y "${ADMIN_PW_FILE}" -H "${LDAP_URI}" >/dev/null <<EOF
dn: ${READ_DN}
objectClass: organizationalRole
objectClass: simpleSecurityObject
cn: ${READ_USER}
description: Read-only account for SIP phones and the web UI
userPassword: ${hash}
EOF
    ok "created ${READ_DN}"
}

# ---------------------------------------------------------------- web app ---
install_app() {
    step "Installing the web UI"
    if ! id -u "${APP_USER}" >/dev/null 2>&1; then
        run useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
        ok "system user ${APP_USER} created"
    else
        ok "system user ${APP_USER} exists"
    fi

    run install -d -m 0755 "${APP_DIR}"
    run install -d -m 0750 "${CONF_DIR}"
    if [[ "${DRY_RUN}" != "yes" ]]; then
        chown "root:${APP_USER}" "${CONF_DIR}"
        chmod 0750 "${CONF_DIR}"
    fi

    if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
        run python3 -m venv "${APP_DIR}/.venv"
        ok "virtualenv created"
    fi
    run "${APP_DIR}/.venv/bin/pip" install -q --upgrade pip
    run "${APP_DIR}/.venv/bin/pip" install -q "ldap3>=2.9,<3"
    ok "python dependencies installed"

    run install -m 0644 "${SCRIPT_DIR}/app/app.py" "${APP_DIR}/app.py"
    run install -m 0644 "${SCRIPT_DIR}/app/import_phonebook.py" "${APP_DIR}/import_phonebook.py"

    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] write ${CONF_DIR}/${SERVICE}.env and ${SERVICE_FILE}"
        return 0
    fi

    local host_ip
    host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    cat > "${CONF_DIR}/${SERVICE}.env" <<EOF
# generated by ${APP_NAME} ${APP_VERSION} on $(date -Is)
PB_HOST=${APP_BIND}
PB_PORT=${APP_PORT}
PB_LDAP_HOST=127.0.0.1
PB_LDAP_PORT=389
PB_BASE_DN=${BASE_DN}
PB_PEOPLE_DN=${PEOPLE_DN}
PB_BIND_PATTERN=cn=%s,${BASE_DN}
PB_TITLE=${UI_TITLE}
PB_SUBTITLE=${host_ip}:${APP_PORT}
PB_SESSION_TTL=43200
EOF
    chown root:"${APP_USER}" "${CONF_DIR}/${SERVICE}.env"
    chmod 0640 "${CONF_DIR}/${SERVICE}.env"

    # remember where things went: uninstall.sh reads this file, so it works even
    # when the paths were changed through PB_APP_DIR / --app-user
    cat > "${CONF_DIR}/install.state" <<EOF
PB_APP_DIR=${APP_DIR}
PB_APP_USER=${APP_USER}
PB_APP_PORT=${APP_PORT}
PB_BASE_DN=${BASE_DN}
PB_PEOPLE_DN=${PEOPLE_DN}
PB_SERVICE=${SERVICE}
EOF
    chown root:root "${CONF_DIR}/install.state"
    chmod 0640 "${CONF_DIR}/install.state"

    sed -e "s|@APP_DIR@|${APP_DIR}|g" \
        -e "s|@APP_USER@|${APP_USER}|g" \
        -e "s|@ENV_FILE@|${CONF_DIR}/${SERVICE}.env|g" \
        "${SCRIPT_DIR}/systemd/${SERVICE}.service" > "${SERVICE_FILE}"
    chmod 0644 "${SERVICE_FILE}"

    systemctl daemon-reload
    systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
    systemctl restart "${SERVICE}"
    sleep 2
    if systemctl is-active --quiet "${SERVICE}"; then
        ok "service ${SERVICE} is running"
    else
        warn "service ${SERVICE} failed to start:"
        journalctl -u "${SERVICE}" -n 20 --no-pager >&2 || true
        die "web UI did not start"
    fi

    if curl -fsS --max-time 5 "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
        ok "health check passed"
    else
        warn "health endpoint did not answer — check journalctl -u ${SERVICE}"
    fi
}

install_ldap_ui() {
    step "Installing ldap-ui (optional raw editor)"
    local dir="/opt/ldap-ui"
    run install -d -m 0755 "${dir}"
    if [[ ! -x "${dir}/.venv/bin/python" ]]; then
        run python3 -m venv "${dir}/.venv"
    fi
    run "${dir}/.venv/bin/pip" install -q --upgrade pip
    run "${dir}/.venv/bin/pip" install -q ldap-ui
    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] ldap-ui would be served on 127.0.0.1:5000"
        return 0
    fi
    cat > "${CONF_DIR}/ldap-ui.env" <<EOF
LDAP_URL=ldap://127.0.0.1:389/
BASE_DN=${BASE_DN}
BIND_PATTERN=cn=%s,${BASE_DN}
BIND_AS_USER=true
EOF
    cat > /etc/systemd/system/ldap-ui.service <<EOF
[Unit]
Description=ldap-ui (raw LDAP editor)
After=network-online.target slapd.service
Requires=slapd.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${dir}
EnvironmentFile=${CONF_DIR}/ldap-ui.env
ExecStart=${dir}/.venv/bin/ldap-ui --host 127.0.0.1 --port 5000
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes

[Install]
WantedBy=multi-user.target
EOF
    chown -R "${APP_USER}":"${APP_USER}" "${dir}"
    systemctl daemon-reload
    systemctl enable --now ldap-ui >/dev/null 2>&1 || true
    ok "ldap-ui on http://127.0.0.1:5000/ (bind it to LAN via a reverse proxy)"
}

open_firewall() {
    step "Firewall"
    if [[ "${OPEN_FIREWALL}" != "yes" ]]; then
        note "skipped (--no-firewall)"
        return 0
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        run ufw allow "${APP_PORT}/tcp" >/dev/null
        ok "ufw: allowed ${APP_PORT}/tcp"
    else
        note "ufw is not active, nothing to do"
    fi
}

import_csv() {
    [[ -z "${IMPORT_CSV}" ]] && return 0
    step "Importing ${IMPORT_CSV}"
    [[ -f "${IMPORT_CSV}" ]] || die "file not found: ${IMPORT_CSV}"
    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] import ${IMPORT_CSV}"
        return 0
    fi
    PB_ADMIN_PW_FILE="${ADMIN_PW_FILE}" PB_BASE_DN="${BASE_DN}" \
    PB_PEOPLE_OU="${PEOPLE_OU}" PB_LDAP_URI="${LDAP_URI}" \
        "${APP_DIR}/.venv/bin/python" "${APP_DIR}/import_phonebook.py" \
        --csv "${IMPORT_CSV}" || warn "import finished with warnings"
}

# ---------------------------------------------------------------- summary ---
write_credentials() {
    local host_ip read_pw_show
    host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    read_pw_show="${READ_PW:-(unchanged, set manually)}"
    if [[ "${DRY_RUN}" == "yes" ]]; then
        note "[dry-run] would write ${CRED_FILE}"
        return 0
    fi
    umask 077
    cat > "${CRED_FILE}" <<EOF
==== ${APP_NAME} ${APP_VERSION} — access details ====
Installed : $(date -Is)
Host      : $(hostname) (${host_ip})

Web UI    : http://${host_ip}:${APP_PORT}/
  admin   : ${ADMIN_PW}    (full control: add, edit, delete)
  ${READ_USER}    : ${read_pw_show}    (read-only, used by phones)

LDAP      : ldap://${host_ip}:389/
  BASE_DN : ${BASE_DN}
  BindDN  : ${ADMIN_DN}
  Password: ${ADMIN_PW}
  ReadDN  : ${READ_DN}
  Password: ${read_pw_show}

Paths:
  app     : ${APP_DIR}
  config  : ${CONF_DIR}
  service : ${SERVICE}.service
  slapd   : /etc/ldap/slapd.d, data in /var/lib/ldap

Phone settings: BASE_DN=${PEOPLE_DN}, BindDN=${READ_DN}, see docs/PHONES.md
EOF
    chmod 600 "${CRED_FILE}"
    umask 022
}

summary() {
    local host_ip read_pw_show
    host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    read_pw_show="${READ_PW:-(unchanged)}"
    step "Done"
    if [[ "${LDAP_ONLY}" != "yes" ]]; then
        say "Web UI      ${C_BOLD}http://${host_ip}:${APP_PORT}/${C_RESET}"
        say "  login     admin / ${ADMIN_PW}"
        say "  read-only ${READ_USER} / ${read_pw_show}"
    fi
    say "LDAP        ldap://${host_ip}:389/  base ${BASE_DN}"
    say "Phones      BindDN ${READ_DN}, base ${PEOPLE_DN}, password ${read_pw_show}"
    say "Details     ${CRED_FILE}"
    [[ -n "${IMPORT_CSV}" ]] && say "Imported    ${IMPORT_CSV}"
    echo
    note "next: add contacts in the web UI, then configure the phones (docs/PHONES.md)"
}

# ------------------------------------------------------------------- main ---
preflight

# The preseed must be in place BEFORE slapd is installed, otherwise Debian
# creates the directory from the machine's domain (this is what CI caught).
SLAPD_FRESH="no"
if [[ "${APP_ONLY}" != "yes" ]] && ! dpkg -s slapd >/dev/null 2>&1; then
    SLAPD_FRESH="yes"
    step "Preseeding OpenLDAP"
    preseed_slapd
    ok "debconf prepared: domain ${DOMAIN}, base ${BASE_DN}"
fi

install_packages

if [[ "${APP_ONLY}" != "yes" ]]; then
    setup_slapd
fi

if [[ -z "${ADMIN_PW}" ]]; then
    die "--app-only requires --admin-password (the web UI needs an account with write access)"
fi
prepare_admin_pwfile

if [[ "${APP_ONLY}" != "yes" ]]; then
    ensure_base
    apply_acls
    ensure_read_account
fi

if [[ "${LDAP_ONLY}" != "yes" ]]; then
    install_app
fi

[[ "${WITH_LDAP_UI}" == "yes" ]] && install_ldap_ui
open_firewall
import_csv
write_credentials
summary
