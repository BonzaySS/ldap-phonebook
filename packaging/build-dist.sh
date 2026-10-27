#!/usr/bin/env bash
#
#  Builds dist/ldap-phonebook-<version>.tar.gz and a SHA256 checksum.
#
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
NAME="ldap-phonebook-${VERSION}"
DIST="${ROOT}/dist"
STAGE="${DIST}/${NAME}"

[[ -n "${VERSION}" ]] || { echo "VERSION is empty" >&2; exit 1; }

echo "==> building ${NAME}"
rm -rf "${STAGE}"
mkdir -p "${STAGE}"

for item in install.sh uninstall.sh VERSION LICENSE README.md CHANGELOG.md \
            Makefile .gitattributes app config docs examples systemd tests packaging screenshots; do
    [[ -e "${ROOT}/${item}" ]] || { echo "missing: ${item}" >&2; exit 1; }
    cp -a "${ROOT}/${item}" "${STAGE}/"
done

find "${STAGE}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name '*.pyc' -delete 2>/dev/null || true
chmod +x "${STAGE}/install.sh" "${STAGE}/uninstall.sh" "${STAGE}/tests/smoke.sh" \
         "${STAGE}/packaging/build-dist.sh"

tar -czf "${DIST}/${NAME}.tar.gz" -C "${DIST}" "${NAME}"
rm -rf "${STAGE}"

cd "${DIST}"
if command -v sha256sum >/dev/null; then
    sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256"
elif command -v shasum >/dev/null; then
    shasum -a 256 "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256"
fi

echo "==> ${DIST}/${NAME}.tar.gz"
ls -lh "${DIST}/${NAME}.tar.gz"
[[ -f "${DIST}/${NAME}.tar.gz.sha256" ]] && cat "${DIST}/${NAME}.tar.gz.sha256"
