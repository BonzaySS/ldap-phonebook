#!/usr/bin/env bash
#
#  ldap-phonebook — smoke test.
#
#  Checks that the web UI and the directory behave as documented. Safe to run
#  against a production instance: it creates one temporary contact named
#  "Smoke Test" and deletes it again.
#
#  Environment (all have defaults matching the installer):
#     PB_URL            http://127.0.0.1:8500
#     PB_ADMIN_USER     admin
#     PB_ADMIN_PW       (required)
#     PB_READ_USER      book
#     PB_READ_PW        (required)
#     PB_BASE_DN        dc=example,dc=org
#     PB_PEOPLE_DN      ou=people,<base>
#     PB_LDAP_URI       ldap://127.0.0.1:389
#
set -uo pipefail

PB_URL="${PB_URL:-http://127.0.0.1:8500}"
ADMIN_USER="${PB_ADMIN_USER:-admin}"
ADMIN_PW="${PB_ADMIN_PW:-}"
READ_USER="${PB_READ_USER:-book}"
READ_PW="${PB_READ_PW:-}"
BASE_DN="${PB_BASE_DN:-dc=example,dc=org}"
PEOPLE_DN="${PB_PEOPLE_DN:-ou=people,${BASE_DN}}"
LDAP_URI="${PB_LDAP_URI:-ldap://127.0.0.1:389}"

[[ -n "${ADMIN_PW}" ]] || { echo "PB_ADMIN_PW is required" >&2; exit 2; }
[[ -n "${READ_PW}" ]]  || { echo "PB_READ_PW is required" >&2;  exit 2; }

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
else
    C_RESET=""; C_GREEN=""; C_RED=""; C_BOLD=""; C_DIM=""
fi

PASS=0; FAIL=0; JAR="$(mktemp)"; TMP="$(mktemp)"
cleanup() {
    rm -f "${JAR}" "${TMP}"
    curl -sS -o /dev/null -m 10 -u "${ADMIN_USER}:${ADMIN_PW}" -X POST \
        --data-urlencode "uid=smoke-t" "${PB_URL}/delete" 2>/dev/null || true
}
trap cleanup EXIT

section() { printf '\n%s== %s%s\n' "${C_BOLD}" "$*" "${C_RESET}"; }
check() {  # check "description" expected actual
    if [[ "$2" == "$3" ]]; then
        printf '  %sPASS%s %s (got %s)\n' "${C_GREEN}" "${C_RESET}" "$1" "$3"
        PASS=$((PASS + 1))
    else
        printf '  %sFAIL%s %s (expected %s, got %s)\n' "${C_RED}" "${C_RESET}" "$1" "$2" "$3"
        FAIL=$((FAIL + 1))
    fi
}
code() { curl -sS -o "${TMP}" -m 15 -w '%{http_code}' "$@" 2>/dev/null; }

section "HTTP endpoints"
check "GET /health returns 200"       200 "$(code "${PB_URL}/health")"
check "GET / without session -> 303"  303 "$(code "${PB_URL}/")"
check "GET /login returns 200"        200 "$(code "${PB_URL}/login")"

section "Authentication"
check "wrong password is rejected"    401 "$(code -X POST -d "username=${ADMIN_USER}&password=wrong" "${PB_URL}/login")"
check "admin login succeeds"          303 "$(code -c "${JAR}" -X POST \
        -d "username=${ADMIN_USER}&password=${ADMIN_PW}" "${PB_URL}/login")"
check "list page with session"        200 "$(code -b "${JAR}" "${PB_URL}/")"

if grep -q '<table id="tbl">' "${TMP}"; then
    printf '  %sPASS%s list page contains a table\n' "${C_GREEN}" "${C_RESET}"; PASS=$((PASS + 1))
else
    printf '  %sFAIL%s list page has no table\n' "${C_RED}" "${C_RESET}"; FAIL=$((FAIL + 1))
fi

section "Create, export, delete"
check "create a contact"              303 "$(code -u "${ADMIN_USER}:${ADMIN_PW}" -X POST \
        --data-urlencode "uid=" --data-urlencode "fio=Smoke Test" \
        --data-urlencode "company=Test Org" --data-urlencode "title=Tester" \
        --data-urlencode $'numbers=1000\n+7 000 000-00-00' "${PB_URL}/save")"

FOUND="$(ldapsearch -LLL -x -D "cn=${ADMIN_USER},${BASE_DN}" -w "${ADMIN_PW}" -H "${LDAP_URI}" \
        -b "${PEOPLE_DN}" "(cn=Smoke Test)" uid 2>/dev/null | grep -c '^uid:')"
check "contact exists in LDAP"        1 "${FOUND}"

EXPORT="$(curl -sS -m 15 -u "${ADMIN_USER}:${ADMIN_PW}" "${PB_URL}/export.csv" 2>/dev/null)"
case "${EXPORT}" in
    *Предприятие\;Должность\;ФИО\;Телефоны*) printf '  %sPASS%s export CSV header\n' "${C_GREEN}" "${C_RESET}"; PASS=$((PASS + 1)) ;;
    *) printf '  %sFAIL%s export CSV header\n' "${C_RED}" "${C_RESET}"; FAIL=$((FAIL + 1)) ;;
esac
case "${EXPORT}" in
    *"Smoke Test"*) printf '  %sPASS%s export contains the contact\n' "${C_GREEN}" "${C_RESET}"; PASS=$((PASS + 1)) ;;
    *) printf '  %sFAIL%s export misses the contact\n' "${C_RED}" "${C_RESET}"; FAIL=$((FAIL + 1)) ;;
esac

check "delete the contact"            303 "$(code -u "${ADMIN_USER}:${ADMIN_PW}" -X POST \
        --data-urlencode "uid=smoke-t" "${PB_URL}/delete")"
GONE="$(ldapsearch -LLL -x -D "cn=${ADMIN_USER},${BASE_DN}" -w "${ADMIN_PW}" -H "${LDAP_URI}" \
        -b "${PEOPLE_DN}" "(cn=Smoke Test)" uid 2>/dev/null | grep -c '^uid:')"
check "contact removed"               0 "${GONE}"

section "Directory permissions"
BIND="$(ldapwhoami -x -D "cn=${READ_USER},${BASE_DN}" -w "${READ_PW}" -H "${LDAP_URI}" 2>&1)"
case "${BIND}" in
    *"cn=${READ_USER}"*) printf '  %sPASS%s %s can bind\n' "${C_GREEN}" "${C_RESET}" "${READ_USER}"; PASS=$((PASS + 1)) ;;
    *) printf '  %sFAIL%s %s cannot bind: %s\n' "${C_RED}" "${C_RESET}" "${READ_USER}" "${BIND}"; FAIL=$((FAIL + 1)) ;;
esac

READS="$(ldapsearch -LLL -x -D "cn=${READ_USER},${BASE_DN}" -w "${READ_PW}" -H "${LDAP_URI}" \
        -b "${BASE_DN}" "(objectClass=*)" dn 2>/dev/null | grep -c '^dn:')"
if [[ "${READS}" -ge 1 ]]; then
    printf '  %sPASS%s %s can read the tree (%s entries)\n' "${C_GREEN}" "${C_RESET}" "${READ_USER}" "${READS}"
    PASS=$((PASS + 1))
else
    printf '  %sFAIL%s %s cannot read the tree\n' "${C_RED}" "${C_RESET}" "${READ_USER}"; FAIL=$((FAIL + 1))
fi

ldapadd -x -D "cn=${READ_USER},${BASE_DN}" -w "${READ_PW}" -H "${LDAP_URI}" >/dev/null 2>&1 <<EOF
dn: cn=smoke-should-fail,${BASE_DN}
objectClass: organizationalRole
cn: smoke-should-fail
EOF
if [[ $? -ne 0 ]]; then
    printf '  %sPASS%s %s cannot write\n' "${C_GREEN}" "${C_RESET}" "${READ_USER}"; PASS=$((PASS + 1))
else
    printf '  %sFAIL%s %s was able to write\n' "${C_RED}" "${C_RESET}" "${READ_USER}"; FAIL=$((FAIL + 1))
fi

ANON="$(ldapsearch -LLL -x -H "${LDAP_URI}" -b "${BASE_DN}" "(objectClass=*)" dn 2>/dev/null | grep -c '^dn:')"
check "anonymous read denied"         0 "${ANON}"

printf '\n%sRESULT: %d passed, %d failed%s\n' "${C_BOLD}" "${PASS}" "${FAIL}" "${C_RESET}"
[[ "${FAIL}" -eq 0 ]] || exit 1
