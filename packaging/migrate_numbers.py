#!/usr/bin/env python3
"""One-off migration to ldap-phonebook 1.1.0.

Phone endpoints show a single value per attribute, so every record must carry
at most one value in telephoneNumber / mobile / homePhone. This script rewrites
existing records, keeping the original order:

    work  = first telephoneNumber
    mobile = existing mobile, or the next telephoneNumber
    other  = existing homePhone, or the next remaining number

Numbers beyond the third stay in telephoneNumber so nothing is lost.

Usage: migrate_numbers.py [--apply]   (без --apply только показывает план)
"""
from __future__ import annotations

import argparse
import os
import sys

import ldap3
from ldap3 import MODIFY_DELETE, MODIFY_REPLACE, Connection, Server

LDAP_HOST = os.environ.get("PB_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.environ.get("PB_LDAP_PORT", "389"))
BASE_DN = os.environ.get("PB_BASE_DN", "dc=pbx,dc=office")
PEOPLE_DN = os.environ.get("PB_PEOPLE_DN", f"ou=people,{BASE_DN}")
ADMIN_DN = os.environ.get("PB_ADMIN_DN", f"cn=admin,{BASE_DN}")
PW_FILE = os.environ.get("PB_ADMIN_PW_FILE", "/etc/ldap-phonebook/admin.pw")
ATTRS = ["uid", "cn", "telephoneNumber", "mobile", "homePhone"]


def plan(entry: dict) -> tuple[dict[str, list[str]], bool]:
    """Return the target attribute values and whether anything changes."""
    tn = [v for v in entry.get("telephoneNumber", []) if v]
    mobile = [v for v in entry.get("mobile", []) if v]
    home = [v for v in entry.get("homePhone", []) if v]

    spare = tn[1:]
    work = tn[:1]
    if not mobile and spare:
        mobile = [spare.pop(0)]
    if not home and spare:
        home = [spare.pop(0)]
    extras = spare  # keep them, but after the first value they are invisible

    target = {
        "telephoneNumber": work + extras,
        "mobile": mobile[:1],
        "homePhone": home[:1],
    }
    changed = (target["telephoneNumber"] != tn
               or target["mobile"] != mobile
               or target["homePhone"] != home)
    return target, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    args = ap.parse_args()

    pw = open(PW_FILE, encoding="utf-8").read().strip()
    conn = Connection(Server(LDAP_HOST, port=LDAP_PORT, get_info=ldap3.NONE),
                      user=ADMIN_DN, password=pw)
    if not conn.bind():
        print("cannot bind as", ADMIN_DN, file=sys.stderr)
        return 2

    conn.search(PEOPLE_DN, "(objectClass=inetOrgPerson)", attributes=ATTRS,
                size_limit=0, paged_size=500)
    changed_total = 0
    for entry in conn.entries:
        data = {k: [str(v) for v in vals] for k, vals in entry.entry_attributes_as_dict.items()}
        dn = entry.entry_dn
        target, changed = plan(data)
        before = (data.get("telephoneNumber", []), data.get("mobile", []),
                  data.get("homePhone", []))
        if not changed:
            print(f"  = {data.get('cn', [''])[0]:32s} без изменений")
            continue
        changed_total += 1
        print(f"  ~ {data.get('cn', [''])[0]:32s}")
        print(f"      было : tn={before[0]} mobile={before[1]} home={before[2]}")
        print(f"      станет: tn={target['telephoneNumber']} "
              f"mobile={target['mobile']} home={target['homePhone']}")
        if args.apply:
            changes = {}
            for attr in ("telephoneNumber", "mobile", "homePhone"):
                if target[attr]:
                    changes[attr] = [(MODIFY_REPLACE, target[attr])]
                elif data.get(attr):
                    changes[attr] = [(MODIFY_DELETE, [])]
            if changes:
                conn.modify(dn, changes)
                if conn.result.get("description") != "success":
                    print("      ОШИБКА:", conn.result.get("description"))
                    return 3

    print(f"\n{'записано' if args.apply else 'к изменению'}: {changed_total} из "
          f"{len(conn.entries)} записей")
    if not args.apply and changed_total:
        print("повторите с --apply, чтобы применить")
    conn.unbind()
    return 0


if __name__ == "__main__":
    sys.exit(main())
