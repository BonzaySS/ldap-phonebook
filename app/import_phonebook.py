#!/usr/bin/env python3
"""Import a phonebook CSV into the LDAP directory used as a SIP phone book.

Design notes
------------
* DNs stay pure ASCII (uid=<transliterated-surname>-<initials>), so renaming a
  person never changes its DN and no client has to handle Cyrillic in a DN.
* Cyrillic lives only in attribute values: cn / sn / givenName / displayName /
  ou / departmentNumber / title / o. Non-ASCII values are written to LDIF in
  base64 (``attr:: <b64>``) so the file is byte-safe regardless of locale.
* Numbers: telephoneNumber is multi-valued, so extension + city number both go
  there; mobile, homePhone and facsimileTelephoneNumber map to their standard
  inetOrgPerson attributes (these are what Yealink/Fanvil read by default).
* ``o`` (organizationName, "предприятие") and ``title`` ("должность") are both
  MAY attributes of inetOrgPerson per RFC 2798, so no schema extension is needed.

Usage
-----
  import_phonebook.py --csv people.csv [--dry-run] [--update]

  default   : add new entries, skip rows whose uid already exists
  --update  : modify the listed attributes of existing entries instead
              (a column absent from the CSV or left empty leaves that
              attribute untouched)

CSV: separator ; , or tab is auto-detected, UTF-8 (with/without BOM) and
cp1251 are both handled. Header names are matched case-insensitively; both
English and Russian spellings are accepted:

  fio / name / фио                  full name (required)
  dept / department / отдел         department     -> ou + departmentNumber
  title / должжность                job title      -> title
  company / предприятие             company        -> o
  ext / внутренний                  extension      -> telephoneNumber
  work / phone / рабочий            work number    -> telephoneNumber
  mobile / мобильный                mobile         -> mobile
  home / домашний                   home           -> homePhone
  fax                               fax            -> facsimileTelephoneNumber
  uid / login                       explicit uid (optional)

Repeatable: work2, work3, mobile2, phone3 ... are all appended in order.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import os
import re
import subprocess
import sys

LDAP_URI = os.environ.get("PB_LDAP_URI", "ldap://127.0.0.1:389")
DEFAULT_BASE = os.environ.get("PB_BASE_DN", "dc=example,dc=org")
DEFAULT_PEOPLE = os.environ.get("PB_PEOPLE_OU", "ou=people")
ADMIN_CN = os.environ.get("PB_ADMIN_CN", "admin")
ADMIN_DN = f"cn={ADMIN_CN},{DEFAULT_BASE}"
ADMIN_PW_FILE = os.environ.get("PB_ADMIN_PW_FILE", "/etc/ldap-phonebook/admin.pw")

# ---------------------------------------------------------------- translit ---
_TRANSLIT = {
    "\u0430": "a", "\u0431": "b", "\u0432": "v", "\u0433": "g", "\u0434": "d",
    "\u0435": "e", "\u0451": "e", "\u0436": "zh", "\u0437": "z", "\u0438": "i",
    "\u0439": "i", "\u043a": "k", "\u043b": "l", "\u043c": "m", "\u043d": "n",
    "\u043e": "o", "\u043f": "p", "\u0440": "r", "\u0441": "s", "\u0442": "t",
    "\u0443": "u", "\u0444": "f", "\u0445": "kh", "\u0446": "ts", "\u0447": "ch",
    "\u0448": "sh", "\u0449": "shch", "\u044a": "", "\u044b": "y", "\u044c": "",
    "\u044d": "e", "\u044e": "yu", "\u044f": "ya",
}

# column header -> internal field
ALIASES = {
    "fio": "fio", "name": "fio", "fullname": "fio",
    "\u0444\u0438\u043e": "fio",
    "dept": "dept", "department": "dept",
    "\u043e\u0442\u0434\u0435\u043b": "dept",
    "title": "title", "position": "title",
    "\u0434\u043e\u043b\u0436\u043d\u043e\u0441\u0442\u044c": "title",
    "company": "company", "organisation": "company", "organization": "company",
    "\u043f\u0440\u0435\u0434\u043f\u0440\u0438\u044f\u0442\u0438\u0435": "company",
    "\u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u044f": "company",
    "uid": "uid", "login": "uid",
    "\u043b\u043e\u0433\u0438\u043d": "uid",
}
SIMPLE_FIELDS = ("fio", "dept", "title", "company", "uid")

NUMBER_PREFIXES = (
    ("ext", "telephoneNumber"),
    ("\u0432\u043d\u0443\u0442\u0440", "telephoneNumber"),
    ("work", "telephoneNumber"),
    ("phone", "telephoneNumber"),
    ("tel", "telephoneNumber"),
    ("\u0442\u0435\u043b\u0435\u0444\u043e\u043d", "telephoneNumber"),
    ("\u0440\u0430\u0431\u043e\u0447", "telephoneNumber"),
    ("mobile", "mobile"),
    ("mob", "mobile"),
    ("\u043c\u043e\u0431", "mobile"),
    ("home", "homePhone"),
    ("\u0434\u043e\u043c", "homePhone"),
    ("\u0434\u0440\u0443\u0433", "homePhone"),
    ("fax", "facsimileTelephoneNumber"),
)


# ---------------------------------------------------------------- helpers ---
def translit(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            out.append(_TRANSLIT[low])
        elif ch.isalnum() and ch.isascii():
            out.append(low)
        elif ch in " -_":
            out.append("-")
    return re.sub(r"-+", "-", "".join(out)).strip("-")


def norm_name(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").replace("\u00a0", " ")).strip()


def split_fio(fio: str) -> tuple[str, str, str]:
    parts = [p for p in norm_name(fio).split(" ") if p]
    if not parts:
        return "", "", ""
    sn = parts[0]
    rest = parts[1:]
    given = rest[0] if rest else ""
    initials = "".join(p[0].upper() + "." for p in rest if p)
    return sn, given, f"{sn} {initials}".strip()


def make_uid(sn: str, rest: list[str]) -> str:
    base = translit(sn)
    ini = "".join(translit(p)[:1] for p in rest if p)
    uid = (base + ("-" + ini if ini else "")).strip("-")
    return uid or "user"


def ldif_value(attr: str, value: str) -> str:
    if value == "":
        return ""
    plain_ok = (
        value.isascii()
        and not re.search(r"[\\\";<>\x00-\x1f]", value)
        and value[0] not in " #:"
        and not value.endswith(" ")
    )
    if plain_ok:
        return f"{attr}: {value}"
    b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{attr}:: {b64}"


def number_attr(col: str) -> str | None:
    for prefix, attr in NUMBER_PREFIXES:
        if col.startswith(prefix):
            return attr
    return None


# ------------------------------------------------------------------- input ---
def read_rows(path: str) -> list[dict]:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"cannot decode {path}")
    lines = text.splitlines()
    sample = lines[0] if lines else ""
    delim = ";" if ";" in sample and sample.count(";") >= sample.count(",") else ","
    if "\t" in sample:
        delim = "\t"
    return [
        {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        for r in csv.DictReader(io.StringIO(text), delimiter=delim)
    ]


def existing_uids(people_dn: str) -> set[str]:
    out = subprocess.run(
        ["ldapsearch", "-LLL", "-x", "-D", ADMIN_DN, "-y", ADMIN_PW_FILE,
         "-H", LDAP_URI, "-b", people_dn, "(objectClass=inetOrgPerson)", "uid"],
        capture_output=True, text=True,
    )
    return {
        line.split(":", 1)[1].strip().lower()
        for line in out.stdout.splitlines()
        if line.lower().startswith("uid:")
    }


# ------------------------------------------------------------- build model ---
def build_records(rows, base, people, skip_existing: set[str]):
    """Return [(dn, uid, [(attr, value), ...], summary_line)]."""
    people_dn = f"{people},{base}"
    used: set[str] = set()
    records, skipped = [], []

    for row in rows:
        if not row or not any(row.values()):
            continue
        cols = {ALIASES.get(k, k): v for k, v in row.items() if v}

        fio = norm_name(cols.get("fio", ""))
        if not fio:
            skipped.append(f"SKIP (no name): {row}")
            continue

        sn, given, display = split_fio(fio)
        rest = [p for p in norm_name(fio).split(" ")[1:] if p]
        uid = re.sub(r"[^a-z0-9-]", "", (cols.get("uid") or make_uid(sn, rest)).lower()) or "user"
        if uid in used or uid in skip_existing:
            skipped.append(f"SKIP (uid exists): {uid}  {fio}")
            continue
        used.add(uid)

        numbers: dict[str, list[str]] = {}
        for col, val in row.items():
            if not val or ALIASES.get(col) in SIMPLE_FIELDS:
                continue
            attr = number_attr(col)
            if attr:
                # a cell may hold a list: "101, +7 495 ..." (legacy "телефоны")
                for part in re.split(r"[\n,;]+", val):
                    value = norm_name(part)
                    if value:
                        numbers.setdefault(attr, []).append(value)

        # the phones show one number per attribute, so the three slots are kept
        # single-valued: telephoneNumber / mobile / homePhone, extras overflow
        slots = {"telephoneNumber": "", "mobile": "", "homePhone": ""}
        spare: list[str] = []
        for attr in slots:
            values = [v for v in dict.fromkeys(numbers.get(attr, [])) if v]
            if values:
                slots[attr] = values[0]
                spare += values[1:]
        for attr in ("telephoneNumber", "mobile", "homePhone"):
            if not slots[attr] and spare:
                slots[attr] = spare.pop(0)

        attrs: list[tuple[str, str]] = [("uid", uid), ("cn", fio), ("sn", sn)]
        if given:
            attrs.append(("givenName", given))
        if display:
            attrs.append(("displayName", display))
        if cols.get("dept"):
            dept = norm_name(cols["dept"])
            attrs += [("ou", dept), ("departmentNumber", dept)]
        if cols.get("title"):
            attrs.append(("title", norm_name(cols["title"])))
        if cols.get("company"):
            attrs.append(("o", norm_name(cols["company"])))
        for attr, value in slots.items():
            if value:
                attrs.append((attr, value))
        for value in dict.fromkeys(numbers.get("facsimileTelephoneNumber", [])):
            if value:
                attrs.append(("facsimileTelephoneNumber", value))

        dn = f"uid={uid},{people_dn}"
        extra = " ".join(
            f"[{v}]" for v in (cols.get("dept"), cols.get("title"), cols.get("company")) if v
        )
        records.append((dn, uid, attrs, f"{uid:<16} {fio}  {extra}".rstrip()))

    return records, skipped


def render_add(records) -> str:
    blocks = []
    for dn, _uid, attrs, _s in records:
        lines = [f"dn: {dn}", "objectClass: inetOrgPerson",
                 "objectClass: organizationalPerson", "objectClass: person",
                 "objectClass: top"]
        lines += [ldif_value(a, v) for a, v in attrs]
        blocks.append("\n".join(l for l in lines if l))
    return "\n\n".join(blocks) + "\n"


def render_modify(records) -> str:
    """modify LDIF: replace every attribute present in the CSV row.

    Values are grouped per attribute: ``replace`` swaps the whole attribute,
    so a multi-valued attribute such as telephoneNumber must carry all its
    values inside ONE modification block, not one block per value.
    """
    blocks = []
    for dn, _uid, attrs, _s in records:
        grouped: dict[str, list[str]] = {}
        for attr, value in attrs:
            if attr == "uid" or value == "":
                continue
            grouped.setdefault(attr, []).append(value)
        lines = [f"dn: {dn}", "changetype: modify"]
        for attr, values in grouped.items():
            lines.append(f"replace: {attr}")
            lines += [ldif_value(attr, v) for v in values]
            lines.append("-")
        blocks.append("\n".join(l for l in lines if l))
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--base-dn", default=DEFAULT_BASE)
    ap.add_argument("--people-ou", default=DEFAULT_PEOPLE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--update", action="store_true",
                    help="modify existing entries instead of adding new ones")
    ap.add_argument("--ldif-out", help="also write the LDIF to this path")
    args = ap.parse_args()

    people_dn = f"{args.people_ou},{args.base_dn}"
    rows = read_rows(args.csv)
    existing = existing_uids(people_dn)
    records, skipped = build_records(rows, args.base_dn, args.people_ou,
                                     existing if not args.update else set())
    ldif = render_modify(records) if args.update else render_add(records)

    print(f"-- {len(records)} entries --")
    for *_rest, summary in records:
        print("  " + summary)
    for line in skipped:
        print("  " + line)

    if args.ldif_out:
        open(args.ldif_out, "w", encoding="utf-8").write(ldif)
        print(f"LDIF written to {args.ldif_out}")

    if args.dry_run:
        print("---- dry run, LDIF follows ----")
        print(ldif)
        return 0

    tool = "ldapmodify" if args.update else "ldapadd"
    proc = subprocess.run(
        [tool, "-c", "-x", "-D", ADMIN_DN, "-y", ADMIN_PW_FILE, "-H", LDAP_URI],
        input=ldif, text=True, capture_output=True,
    )
    print(proc.stdout.strip() or "(no stdout)")
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    changed = proc.stdout.count("adding new entry") + proc.stdout.count("modifying entry")
    print(f"== {tool}: {changed} of {len(records)} applied ==")
    return 0 if changed else 2


if __name__ == "__main__":
    sys.exit(main())
