#!/usr/bin/env python3
"""One-off migration to ldap-phonebook 1.2.0.

1. Moves every contact into its organisation branch:
       uid=...,ou=<организация>,ou=people,<base>
   Contacts without an organisation stay in ou=people.
2. Rewrites displayName to the caption the phones show:
       "<ФИО> — <должность>, <организация>"

Usage: migrate_tree.py [--apply]   (без --apply только показывает план)
"""
from __future__ import annotations

import argparse
import os
import sys

import ldap3
from ldap3 import Connection, MODIFY_REPLACE, Server
from ldap3.utils.dn import escape_rdn

LDAP_HOST = os.environ.get("PB_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.environ.get("PB_LDAP_PORT", "389"))
BASE_DN = os.environ.get("PB_BASE_DN", "dc=pbx,dc=office")
PEOPLE_DN = os.environ.get("PB_PEOPLE_DN", f"ou=people,{BASE_DN}")
ADMIN_DN = os.environ.get("PB_ADMIN_DN", f"cn=admin,{BASE_DN}")
PW_FILE = os.environ.get("PB_ADMIN_PW_FILE", "/etc/ldap-phonebook/admin.pw")
ATTRS = ["uid", "cn", "sn", "givenName", "displayName", "o", "title"]


def display_caption(cn: str, title: str, company: str) -> str:
    parts = [p for p in (title, company) if p]
    short = cn
    words = cn.split()
    if len(words) > 1:
        short = f"{words[0]} " + "".join(w[0].upper() + "." for w in words[1:])
    extra = ", ".join(parts)
    return f"{short} — {extra}" if short and extra else (short or extra)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pw = open(PW_FILE, encoding="utf-8").read().strip()
    conn = Connection(Server(LDAP_HOST, port=LDAP_PORT, get_info=ldap3.NONE),
                      user=ADMIN_DN, password=pw)
    if not conn.bind():
        print("cannot bind as", ADMIN_DN, file=sys.stderr)
        return 2

    conn.search(PEOPLE_DN, "(objectClass=inetOrgPerson)", attributes=ATTRS,
                size_limit=0, paged_size=500)
    entries = list(conn.entries)
    moved = renamed = 0
    planned_branches: set[str] = set()
    for entry in entries:
        data = {k: [str(v) for v in vals] for k, vals in entry.entry_attributes_as_dict.items()}
        cn = (data.get("cn") or [""])[0]
        title = (data.get("title") or [""])[0]
        company = (data.get("o") or [""])[0]
        uid = (data.get("uid") or [""])[0]
        dn = entry.entry_dn

        target_parent = PEOPLE_DN
        if company:
            target_parent = f"ou={escape_rdn(company)},{PEOPLE_DN}"
            conn.search(target_parent, "(objectClass=*)", attributes=["ou"], size_limit=1)
            if not conn.entries and target_parent not in planned_branches:
                planned_branches.add(target_parent)
                print(f"  + создаю ветку: {target_parent}")
                if args.apply:
                    conn.add(target_parent, ["organizationalUnit"], {"ou": company})

        caption = display_caption(cn, title, company)
        old_caption = (data.get("displayName") or [""])[0]
        current_parent = dn.split(",", 1)[1]

        need_move = current_parent != target_parent
        need_caption = caption and caption != old_caption
        if not (need_move or need_caption):
            print(f"  = {cn:30s} без изменений")
            continue

        print(f"  ~ {cn:30s}")
        if need_move:
            print(f"      ветка: {current_parent}")
            print(f"        -->  {target_parent}")
        if need_caption:
            print(f"      подпись: {old_caption!r} --> {caption!r}")
        renamed += 1
        if not args.apply:
            continue
        if need_caption:
            conn.modify(dn, {"displayName": [(MODIFY_REPLACE, [caption])]})
        if need_move:
            ok = conn.modify_dn(dn, f"uid={uid}", new_superior=target_parent)
            if not ok:
                print("      ОШИБКА переноса:", conn.result.get("description"))
                return 3
            moved += 1

    print(f"\n{'обработано' if args.apply else 'к изменению'}: {renamed} из {len(entries)}"
          f"{f', перенесено: {moved}' if args.apply else ''}")
    if not args.apply and renamed:
        print("повторите с --apply")
    conn.unbind()
    return 0


if __name__ == "__main__":
    sys.exit(main())
