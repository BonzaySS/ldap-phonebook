#!/usr/bin/env python3
"""Phone-book web UI for service staff, backed by OpenLDAP.

The staff sees four columns only: company, title, full name, phone numbers.
The LDAP record stays the same, so the SIP phones keep reading it directly.

Fields are mapped to standard attributes:
    предприятие  -> o              (organizationName)
    должность    -> title
    ФИО          -> cn (+ sn, givenName, displayName generated)
    телефоны     -> telephoneNumber (multi-valued, one number per line)

Auth: a login form stores an in-memory session; HTTP Basic is still accepted
for scripts and monitoring. Writes are enforced by the directory itself, so the
`book` account can view and export but not modify.

Run:  python3 app.py        (config through PB_* environment variables)
"""

from __future__ import annotations

import base64
import csv
import html
import io
import os
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import ldap3
from ldap3 import MODIFY_DELETE, MODIFY_REPLACE, Connection, Server
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

# -------------------------------------------------------------------- config
HOST = os.environ.get("PB_HOST", "0.0.0.0")
PORT = int(os.environ.get("PB_PORT", "8500"))
LDAP_HOST = os.environ.get("PB_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.environ.get("PB_LDAP_PORT", "389"))
BASE_DN = os.environ.get("PB_BASE_DN", "dc=pbx,dc=office")
PEOPLE_DN = os.environ.get("PB_PEOPLE_DN", f"ou=people,{BASE_DN}")
BIND_PATTERN = os.environ.get("PB_BIND_PATTERN", f"cn=%s,{BASE_DN}")
# One number per attribute. Fanvil (and Yealink) render each attribute as its own
# field and cannot show several values of telephoneNumber, so the three slots are
# kept single-valued: telephoneNumber / mobile / homePhone.
NUMBER_FIELDS = (("work", "telephoneNumber"),
                 ("mobile", "mobile"),
                 ("other", "homePhone"))
NUMBER_LABELS = {"work": "Рабочий", "mobile": "Мобильный", "other": "Другой"}
NUMERIC_ATTRS = tuple(attr for _key, attr in NUMBER_FIELDS)
READ_ATTRS = ["uid", "cn", "sn", "title", "o", *NUMERIC_ATTRS]
SESSION_TTL = int(os.environ.get("PB_SESSION_TTL", str(12 * 3600)))
COOKIE = "pb_sid"
TITLE = os.environ.get("PB_TITLE", "Телефонная книга")
SUBTITLE = os.environ.get("PB_SUBTITLE", "")
# caption written to displayName: o=организация, t=должность, n=короткое ФИО, c=полное
DISPLAY_ORDER = os.environ.get("PB_DISPLAY_ORDER", "n,o,t")
DISPLAY_SEP = os.environ.get("PB_DISPLAY_SEP", "·")
# the handset fits about this many characters per line; longer captions are
# split into two lines (PB_DISPLAY_MULTILINE=false keeps one line)
DISPLAY_MAX = int(os.environ.get("PB_DISPLAY_MAX", "35"))
DISPLAY_MULTILINE = os.environ.get("PB_DISPLAY_MULTILINE", "false").lower() not in (
    "0", "false", "no", "off")


def _decode_newline(raw: str) -> str:
    """"\\n" / "\\r\\n" / " " / "" -> the actual characters used for the break."""
    return raw.replace("\\r", "\r").replace("\\n", "\n")


DISPLAY_NL = _decode_newline(os.environ.get("PB_DISPLAY_NL", "\\n"))

# ------------------------------------------------------------------ translit
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


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


def norm(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").replace("\u00a0", " ")).strip()


def split_fio(fio: str) -> tuple[str, str, str]:
    parts = [p for p in norm(fio).split(" ") if p]
    if not parts:
        return "", "", ""
    sn, rest = parts[0], parts[1:]
    given = rest[0] if rest else ""
    initials = "".join(p[0].upper() + "." for p in rest if p)
    return sn, given, f"{sn} {initials}".strip()


def parse_numbers(raw: str) -> list[str]:
    parts = re.split(r"[\n,;]+", raw or "")
    seen, numbers = set(), []
    for part in parts:
        value = norm(part)
        if value and value not in seen:
            seen.add(value)
            numbers.append(value)
    return numbers


# ------------------------------------------------------------------ sessions
SESSIONS: dict[str, dict] = {}


def new_session(user: str, password: str) -> str:
    for sid, data in list(SESSIONS.items()):
        if data["exp"] < time.time():
            SESSIONS.pop(sid, None)
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = {"user": user, "password": password,
                     "exp": time.time() + SESSION_TTL}
    return sid


def get_session(sid: str | None) -> dict | None:
    if not sid:
        return None
    data = SESSIONS.get(sid)
    if not data:
        return None
    if data["exp"] < time.time():
        SESSIONS.pop(sid, None)
        return None
    return data


# --------------------------------------------------------------------- ldap
def connect(user: str, password: str) -> Connection | None:
    server = Server(LDAP_HOST, port=LDAP_PORT, get_info=ldap3.NONE,
                    connect_timeout=5)
    conn = Connection(server, user=BIND_PATTERN % user, password=password)
    try:
        return conn if conn.bind() else None
    except ldap3.core.exceptions.LDAPException:
        return None


def wrap_caption(text: str) -> str:
    """Break a long caption into two lines at a separator boundary.

    The handset fits about PB_DISPLAY_MAX characters per line, so the caption is
    split where a separator falls inside that width — words are never cut.
    Set PB_DISPLAY_MULTILINE=false to keep a single line (the phone truncates).
    """
    if not DISPLAY_MULTILINE or len(text) <= DISPLAY_MAX:
        return text
    sep = f" {DISPLAY_SEP} "
    cut = text.rfind(sep, 0, DISPLAY_MAX + 1)
    if cut > 0:
        return text[:cut] + DISPLAY_NL + text[cut + len(sep):]
    cut = text.rfind(" ", 0, DISPLAY_MAX + 1)
    if cut > 0:
        return text[:cut] + DISPLAY_NL + text[cut + 1:]
    return text


def compose_display(short: str, title: str, company: str, full: str = "") -> str:
    """Caption for the phone screen.

    Fanvil shows a single LDAP attribute as the contact caption, so everything
    that has to be visible on the handset goes into displayName. The order and
    the separator come from PB_DISPLAY_ORDER / PB_DISPLAY_SEP:
        o = организация, t = должность, n = короткое ФИО, c = полное ФИО
    Default: "Энерком · Инженер · Чирcкий С.", длинное переносится на вторую
    строку по границе разделителя.
    """
    values = {"o": company, "t": title, "n": short, "c": full}
    parts = [values.get(key.strip(), "") for key in DISPLAY_ORDER.split(",")]
    parts = [part for part in parts if part]
    return wrap_caption(f" {DISPLAY_SEP} ".join(parts))


def org_container(conn: Connection, company: str) -> str:
    """DN of the organisation branch, created on demand.

    Contacts live under ou=<organization>,ou=people,... so the directory is
    browsable by organisation; contacts without one stay in ou=people itself.
    """
    if not company:
        return PEOPLE_DN
    dn = f"ou={escape_rdn(company)},{PEOPLE_DN}"
    conn.search(dn, "(objectClass=*)", attributes=["ou"], size_limit=1)
    if not conn.entries:
        conn.add(dn, ["organizationalUnit"], {"ou": company})
    return dn


def find_dn(conn: Connection, uid: str) -> str:
    """Real DN of a contact by uid (they are nested, so the uid is not enough)."""
    if not uid:
        return ""
    conn.search(PEOPLE_DN, f"(uid={escape_filter_chars(uid)})",
                attributes=["uid"], size_limit=1)
    return conn.entries[0].entry_dn if conn.entries else ""


def entry_numbers(clean: dict[str, list[str]]) -> dict[str, str]:
    """Map the three attributes onto the three form fields.

    Older records may still carry several values of telephoneNumber; the extra
    ones are moved into free mobile/other slots so that nothing is lost when the
    record is saved again (the phone shows a single value per attribute).
    """
    values: dict[str, list[str]] = {}
    for key, attr in NUMBER_FIELDS:
        values[key] = [v for v in clean.get(attr, []) if v]
    slots = ["work", "mobile", "other"]
    extra = values["work"][1:]
    values["work"] = values["work"][:1]
    for key in ("mobile", "other"):
        if not values[key] and extra:
            values[key] = [extra.pop(0)]
    out = {key: (values[key][0] if values[key] else "") for key in slots}
    out["_extra"] = extra  # leftovers that had nowhere to go (rare)
    return out


def fetch(conn: Connection) -> list[dict]:
    conn.search(PEOPLE_DN, "(objectClass=inetOrgPerson)", attributes=READ_ATTRS,
                size_limit=0, paged_size=500)
    people = []
    for entry in conn.entries:
        attrs = entry.entry_attributes_as_dict
        clean = {k: [str(v) for v in vals] for k, vals in attrs.items()}
        slots = entry_numbers(clean)
        numbers = [slots[key] for key in ("work", "mobile", "other") if slots[key]]
        people.append({
            "dn": entry.entry_dn,
            "uid": (clean.get("uid") or [""])[0],
            "fio": (clean.get("cn") or [""])[0],
            "title": (clean.get("title") or [""])[0],
            "company": (clean.get("o") or [""])[0],
            "work": slots["work"],
            "mobile": slots["mobile"],
            "other": slots["other"],
            "numbers": numbers,
        })
    people.sort(key=lambda p: (p["company"].casefold(), p["fio"].casefold()))
    return people


def existing_uids(conn: Connection) -> set[str]:
    conn.search(PEOPLE_DN, "(objectClass=inetOrgPerson)", attributes=["uid"],
                size_limit=0, paged_size=500)
    return {(e.entry_attributes_as_dict.get("uid") or [""])[0].lower()
            for e in conn.entries}


def add_person(conn: Connection, fio: str, company: str, title: str,
               numbers: dict[str, str]) -> tuple[bool, str]:
    sn, given, short = split_fio(fio)
    taken = existing_uids(conn)
    base = translit(sn) or "user"
    initials = "".join(translit(p)[:1] for p in norm(fio).split(" ")[1:] if p)
    slug = (base + ("-" + initials if initials else "")).strip("-") or "user"
    uid, n = slug, 2
    while uid in taken:
        uid, n = f"{slug}-{n}", n + 1
    attrs = {"cn": fio, "sn": sn}
    if given:
        attrs["givenName"] = given
    caption = compose_display(short, title, company, fio)
    if caption:
        attrs["displayName"] = caption
    if company:
        attrs["o"] = company
    if title:
        attrs["title"] = title
    for key, attr in NUMBER_FIELDS:
        if numbers.get(key):
            attrs[attr] = numbers[key]
    parent = org_container(conn, company)
    ok = conn.add(f"uid={uid},{parent}", ["inetOrgPerson"], attrs)
    return ok, conn.result.get("description", "")


def update_person(conn: Connection, uid: str, fio: str, company: str,
                  title: str, numbers: dict[str, str]) -> tuple[bool, str]:
    dn = find_dn(conn, uid)
    if not dn:
        return False, "запись не найдена"
    conn.search(dn, "(objectClass=*)", attributes=READ_ATTRS, size_limit=1)
    if not conn.entries:
        return False, "запись не найдена"
    current = {k: [str(v) for v in vals]
               for k, vals in conn.entries[0].entry_attributes_as_dict.items()}

    sn, given, short = split_fio(fio)
    changes: dict[str, list] = {}

    def put(attr: str, value: str) -> None:
        """replace, or delete when the new value is empty and the attr exists."""
        if value:
            changes[attr] = [(MODIFY_REPLACE, [value])]
        elif current.get(attr):
            changes[attr] = [(MODIFY_DELETE, [])]

    put("cn", fio)
    put("sn", sn)
    put("givenName", given)
    put("displayName", compose_display(short, title, company, fio))
    put("o", company)
    put("title", title)

    # Exactly one value per attribute: the phones show a single number per field.
    # Numbers that older records keep beyond the first one are preserved in
    # telephoneNumber instead of being thrown away on save.
    work = norm(numbers.get("work", ""))
    legacy_extra = [v for v in current.get("telephoneNumber", [])[1:] if v]
    if work or legacy_extra:
        changes["telephoneNumber"] = [(MODIFY_REPLACE,
                                       ([work] if work else []) + legacy_extra)]
    elif current.get("telephoneNumber"):
        changes["telephoneNumber"] = [(MODIFY_DELETE, [])]
    for key in ("mobile", "other"):
        put(dict(NUMBER_FIELDS)[key], norm(numbers.get(key, "")))

    if changes:
        ok = conn.modify(dn, changes)
        if not ok:
            return False, conn.result.get("description", "")

    # a change of organisation moves the contact to the matching branch
    parent = org_container(conn, company)
    if dn.split(",", 1)[1] != parent:
        moved = conn.modify_dn(dn, f"uid={uid}", new_superior=parent)
        if not moved:
            return False, conn.result.get("description", "")
    return True, "no changes" if not changes else "success"


def delete_person(conn: Connection, uid: str) -> tuple[bool, str]:
    dn = find_dn(conn, uid)
    if not dn:
        return False, "запись не найдена"
    ok = conn.delete(dn)
    return ok, conn.result.get("description", "")


# -------------------------------------------------------------------- import
HEADER_MAP = {
    "предприятие": "company", "организация": "company", "company": "company",
    "o": "company", "org": "company",
    "должность": "title", "title": "title", "position": "title",
    "фио": "fio", "ф.и.о.": "fio", "фио сотрудника": "fio", "name": "fio",
    "cn": "fio", "сотрудник": "fio",
    "телефоны": "numbers", "телефон": "work", "номера": "numbers",
    "номера телефонов": "numbers", "phones": "numbers", "phone": "work",
    "numbers": "numbers", "телефонные номера": "numbers",
    "рабочий": "work", "рабочий телефон": "work", "work": "work", "tel": "work",
    "мобильный": "mobile", "мобильный телефон": "mobile", "mobile": "mobile",
    "моб": "mobile", "mob": "mobile", "cell": "mobile",
    "другой": "other", "другой номер": "other", "домашний": "other",
    "other": "other", "home": "other", "homephone": "other",
}


def decode_csv(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    sample = lines[0]
    delim = ";" if sample.count(";") >= sample.count(",") and ";" in sample else ","
    if "\t" in sample:
        delim = "\t"
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    out = []
    for row in rows:
        item: dict = {"company": "", "title": "", "fio": "",
                      "work": "", "mobile": "", "other": "", "numbers": []}
        for key, value in row.items():
            field = HEADER_MAP.get((key or "").strip().lower())
            if not field:
                continue
            if field in ("numbers", "work", "mobile", "other"):
                # a multi-value column is distributed over the free slots
                item["numbers"] += parse_numbers(value or "") if field == "numbers" else []
                if field != "numbers":
                    item[field] = (parse_numbers(value or "") or [""])[0]
            else:
                item[field] = norm(value or "")
        for value in item["numbers"]:            # legacy "телефоны" column
            for slot in ("work", "mobile", "other"):
                if not item[slot]:
                    item[slot] = value
                    break
        if item["fio"]:
            item.pop("numbers", None)
            out.append(item)
    return out


def import_rows(conn: Connection, rows: list[dict]) -> tuple[int, int, int, list[str]]:
    """Upsert by full name. Returns (added, updated, failed, errors)."""
    current = {p["fio"].casefold(): p for p in fetch(conn)}
    added = updated = failed = 0
    errors: list[str] = []
    for row in rows:
        existing = current.get(row["fio"].casefold())
        numbers = {key: row.get(key, "") for key, _attr in NUMBER_FIELDS}
        try:
            if existing:
                ok, msg = update_person(conn, existing["uid"], row["fio"],
                                        row["company"], row["title"], numbers)
                updated += 1 if ok else 0
            else:
                ok, msg = add_person(conn, row["fio"], row["company"],
                                     row["title"], numbers)
                added += 1 if ok else 0
            if not ok:
                failed += 1
                errors.append(f"{row['fio']}: {msg}")
        except Exception as exc:  # noqa: BLE001 - report, never die mid-import
            failed += 1
            errors.append(f"{row['fio']}: {exc}")
    return added, updated, failed, errors


# ---------------------------------------------------------------------- ui
CSS = """
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0; background: #f4f5f8; color: #101828;
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; }

/* ---------- header ---------- */
.topbar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 14px;
  padding: 0 28px; height: 64px;
  background: rgba(255,255,255,.92); backdrop-filter: saturate(180%) blur(8px);
  border-bottom: 1px solid #e4e7ec;
}
.brand { display: flex; align-items: center; gap: 11px; }
.brand-mark {
  width: 34px; height: 34px; border-radius: 9px; display: grid; place-items: center;
  background: linear-gradient(135deg, #6366f1, #4f46e5);
  box-shadow: 0 2px 6px rgba(79,70,229,.35); color: #fff;
}
.brand h1 { margin: 0; font-size: 16.5px; font-weight: 650; letter-spacing: -.01em; }
.brand small { display: block; font-size: 11.5px; color: #98a2b3; font-weight: 450; }
.topbar .spacer { flex: 1; }
.userchip {
  display: flex; align-items: center; gap: 9px;
  padding: 4px 4px 4px 10px; border-radius: 999px; background: #f2f4f7;
}
.avatar {
  width: 26px; height: 26px; border-radius: 50%; display: grid; place-items: center;
  background: #e0e7ff; color: #3730a3; font-size: 12px; font-weight: 650;
}
.userchip span { font-size: 13.5px; color: #475467; }
.logout {
  display: inline-grid; place-items: center; width: 28px; height: 28px;
  border: 0; border-radius: 50%; background: transparent; color: #667085;
  cursor: pointer; transition: background .15s, color .15s;
}
.logout:hover { background: #e4e7ec; color: #101828; }

/* ---------- page ---------- */
main { max-width: 1120px; margin: 0 auto; padding: 26px 28px 60px; }
.toolbar {
  display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap;
}
.searchbox { position: relative; flex: 1 1 300px; max-width: 420px; }
.searchbox svg {
  position: absolute; left: 12px; top: 50%; transform: translateY(-50%);
  color: #98a2b3; pointer-events: none;
}
.searchbox input {
  width: 100%; padding: 10px 12px 10px 38px; font: inherit; color: inherit;
  background: #fff; border: 1px solid #e4e7ec; border-radius: 10px;
  transition: border-color .15s, box-shadow .15s;
}
.searchbox input::placeholder { color: #98a2b3; }
.searchbox input:focus {
  outline: 0; border-color: #a5b4fc; box-shadow: 0 0 0 3px rgba(99,102,241,.14);
}
.counter { font-size: 13px; color: #98a2b3; margin-left: 2px; }
.tools { display: flex; gap: 10px; margin-left: auto; flex-wrap: wrap; }

/* ---------- buttons ---------- */
.btn {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 9px 14px; font: inherit; font-size: 14px; font-weight: 500;
  border-radius: 10px; border: 1px solid #e4e7ec; background: #fff; color: #344054;
  cursor: pointer; text-decoration: none; white-space: nowrap;
  transition: background .15s, border-color .15s, color .15s, box-shadow .15s;
}
.btn:hover { background: #f9fafb; border-color: #d0d5dd; }
.btn:active { transform: translateY(.5px); }
.btn.primary {
  background: linear-gradient(180deg, #5b54ea, #4f46e5);
  border-color: #4f46e5; color: #fff;
  box-shadow: 0 1px 2px rgba(16,24,40,.12), 0 3px 10px rgba(79,70,229,.22);
}
.btn.primary:hover { background: linear-gradient(180deg, #4f46e5, #4338ca); }
.btn.ghost { background: transparent; border-color: transparent; color: #475467; }
.btn.ghost:hover { background: #eef0f4; }
.btn.danger { color: #d92d20; border-color: #fecdca; background: #fff; }
.btn.danger:hover { background: #fef3f2; border-color: #fda29b; }
.btn svg { flex: none; }

/* ---------- table ---------- */
.card {
  background: #fff; border: 1px solid #98a2b3; border-radius: 12px;
  box-shadow: 0 1px 2px rgba(16,24,40,.05), 0 6px 20px -12px rgba(16,24,40,.18);
  overflow: hidden;
}
table { width: 100%; border-collapse: separate; border-spacing: 0; table-layout: fixed; min-width: 660px; }
.tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
.tablewrap::-webkit-scrollbar { height: 8px; }
.tablewrap::-webkit-scrollbar-thumb { background: #d0d5dd; border-radius: 4px; }
thead th:nth-child(1) { width: 19%; }
thead th:nth-child(2) { width: 21%; }
thead th:nth-child(3) { width: 24%; }
thead th:nth-child(5) { width: 92px; }
thead th {
  padding: 8px 12px; text-align: left; background: #f2f4f7;
  border-bottom: 1px solid #98a2b3; font-size: 11px; font-weight: 650;
  letter-spacing: .04em; text-transform: uppercase; color: #475467;
}
tbody td { padding: 8px 12px; border-bottom: 1px solid #d0d5dd; vertical-align: top; }
thead th + th, tbody td + td { border-left: 1px solid #d0d5dd; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr { transition: background .12s; }
tbody tr:hover { background: #f8faff; }
tbody tr.group td {
  background: #eef2f7; border-bottom: 1px solid #98a2b3; border-left: 0;
  padding: 6px 12px; font-size: 12px; font-weight: 650; color: #344054;
  letter-spacing: .01em;
}
tbody tr.group:hover td { background: #eef2f7; }
td.fio { font-weight: 600; color: #101828; }
td.muted, .muted { color: #98a2b3; }
td.actions { text-align: right; white-space: nowrap; }
.iconbtn {
  display: inline-grid; place-items: center; width: 32px; height: 32px;
  border: 1px solid transparent; border-radius: 8px; background: transparent;
  color: #667085; cursor: pointer; text-decoration: none;
  transition: background .14s, color .14s, border-color .14s;
}
.iconbtn:hover { background: #f2f4f7; color: #101828; border-color: #e4e7ec; }
.iconbtn.danger:hover { background: #fef3f2; color: #d92d20; border-color: #fecdca; }
form.inline { display: inline; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; min-width: 0; }
.chip {
  padding: 1px 8px; border-radius: 7px; background: #f2f4f7; color: #344054;
  font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap;
}
.empty { padding: 56px 24px; text-align: center; }
.empty svg { color: #d0d5dd; }
.empty p { margin: 12px 0 18px; color: #98a2b3; }

/* ---------- forms ---------- */
.panel {
  background: #fff; border: 1px solid #cbd5e1; border-radius: 12px;
  box-shadow: 0 1px 2px rgba(16,24,40,.05), 0 6px 20px -12px rgba(16,24,40,.18);
  padding: 24px; max-width: 660px; margin: 8px auto 0;
}
.field label.filebox {
  position: relative; display: flex; flex-direction: column; align-items: center;
  gap: 4px; padding: 20px; text-align: center;
  border: 1.5px dashed #d0d5dd; border-radius: 12px; background: #fcfcfd;
  color: #667085; font-size: 13.5px; cursor: pointer;
  transition: border-color .15s, background .15s;
}
.filebox:hover { border-color: #a5b4fc; background: #f8f8ff; }
.filebox svg { color: #98a2b3; margin-bottom: 6px; }
.filebox strong { color: #4f46e5; font-weight: 550; }
.filebox input[type=file] {
  position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%;
}
.filebox .filename { display: block; margin-top: 6px; font-size: 13px; color: #344054; }
.panel h2 { margin: 0 0 20px; font-size: 18px; font-weight: 650; letter-spacing: -.01em; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.field { margin-bottom: 16px; }
.field label { display: block; margin-bottom: 6px; font-size: 13px; font-weight: 500; color: #344054; }
.field input, .field textarea {
  width: 100%; padding: 10px 12px; font: inherit; color: inherit; background: #fff;
  border: 1px solid #e4e7ec; border-radius: 10px;
  transition: border-color .15s, box-shadow .15s;
}
.field input:focus, .field textarea:focus {
  outline: 0; border-color: #a5b4fc; box-shadow: 0 0 0 3px rgba(99,102,241,.14);
}
.field textarea { min-height: 108px; resize: vertical; line-height: 1.55; }
.hint { margin-top: 6px; font-size: 12.5px; color: #98a2b3; }
.formactions { display: flex; gap: 10px; margin-top: 22px; }

/* ---------- login ---------- */
.login-wrap { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
.login-card {
  width: 100%; max-width: 392px; background: #fff; border: 1px solid #e4e7ec;
  border-radius: 16px; padding: 30px;
  box-shadow: 0 1px 2px rgba(16,24,40,.04), 0 20px 40px -24px rgba(16,24,40,.3);
}
.login-card .brand { justify-content: center; margin-bottom: 22px; }
.login-card h2 { margin: 0 0 4px; font-size: 19px; font-weight: 650; text-align: center; }
.login-card .sub { margin: 0 0 22px; text-align: center; font-size: 13.5px; color: #667085; }
.login-card .btn { width: 100%; justify-content: center; margin-top: 6px; }

/* ---------- flash ---------- */
.flash {
  display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
  padding: 11px 14px; border-radius: 10px; font-size: 14px;
  border: 1px solid #a6f4c5; background: #ecfdf3; color: #027a48;
}
.flash.err { border-color: #fecdca; background: #fef3f2; color: #b42318; }
.flash .close {
  margin-left: auto; border: 0; background: transparent; cursor: pointer;
  color: inherit; opacity: .55; font-size: 16px; line-height: 1; padding: 2px 4px;
}
.flash .close:hover { opacity: 1; }

@media (max-width: 780px) {
  .topbar { padding: 0 16px; }
  main { padding: 18px 16px 48px; }
  .grid2, .grid3 { grid-template-columns: 1fr; }
  .userchip span { display: none; }
  .panel { padding: 20px; }
  .toolbar { flex-direction: column; align-items: stretch; gap: 8px; }
  .searchbox { max-width: none; flex: 1 1 auto; }
  .counter { margin: 0 0 2px 2px; }
  .tools { margin-left: 0; }
  .tools .btn { flex: 1 1 0; justify-content: center; }
  /* the table stays a table: it scrolls sideways instead of becoming cards */
  thead th { padding: 10px 12px; }
  tbody td { padding: 11px 12px; }
}
"""


def esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def icon(name: str, size: int = 18) -> str:
    paths = {
        "phone": '<path d="M7 3.5h3l1.4 3.6-1.9 1.4a12 12 0 0 0 5.9 5.9l1.4-1.9 3.6 1.4v3a2 2 0 0 1-2.2 2A16.5 16.5 0 0 1 4.5 5.7 2 2 0 0 1 6.5 3.5z"/>',
        "plus": '<path d="M12 5v14M5 12h14"/>',
        "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
        "download": '<path d="M12 4v11m0 0 4-4m-4 4-4-4M5 19h14"/>',
        "upload": '<path d="M12 16V5m0 0 4 4m-4-4L8 9M5 19h14"/>',
        "pencil": '<path d="M4 20h4L20 8l-4-4L4 16v4z"/>',
        "trash": '<path d="M4 7h16M10 7V5h4v2M6 7l1 12h10l1-12"/>',
        "logout": '<path d="M15 17l5-5-5-5M20 12H9M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3"/>',
        "book": '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H12v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M12 4h6.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H12"/>',
        "check": '<path d="M20 6 9 17l-5-5"/>',
    }
    body = paths.get(name, "")
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


def topbar(user: str = "") -> str:
    chip = ""
    if user:
        chip = (
            f'<div class="userchip"><div class="avatar">{esc(user[:1].upper())}</div>'
            f'<span>{esc(user)}</span>'
            f'<form class="inline" method="post" action="/logout">'
            f'<button class="logout" type="submit" title="Выйти">{icon("logout", 16)}</button>'
            f"</form></div>"
        )
    subtitle = f"<small>{esc(SUBTITLE)}</small>" if SUBTITLE else ""
    return f"""<header class="topbar">
  <div class="brand">
    <div class="brand-mark">{icon("book", 19)}</div>
    <div><h1>{esc(TITLE)}</h1>{subtitle}</div>
  </div>
  <div class="spacer"></div>{chip}
</header>"""


def page(title: str, body: str, user: str = "", bare: bool = False) -> bytes:
    shell = body if bare else f"{topbar(user)}<main>{body}</main>"
    doc = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body>{shell}</body></html>"""
    return doc.encode("utf-8")


def flash(message: str, kind: str = "ok") -> str:
    if not message:
        return ""
    mark = icon("check" if kind == "ok" else "search", 16)
    css = "flash" if kind == "ok" else "flash err"
    return (f'<div class="{css}">{mark}<span>{esc(message)}</span>'
            f'<button class="close" onclick="this.parentNode.remove()">×</button></div>')


def render_login(error: str = "") -> bytes:
    banner = flash(error, "err") if error else ""
    body = f"""<div class="login-wrap"><div class="login-card">
  <div class="brand"><div class="brand-mark">{icon("book", 19)}</div></div>
  <h2>Телефонная книга</h2>
  <p class="sub">Войдите, чтобы управлять контактами</p>
  {banner}
  <form method="post" action="/login">
    <div class="field"><label for="u">Логин</label>
      <input id="u" type="text" name="username" autocomplete="username"
             autofocus required></div>
    <div class="field"><label for="p">Пароль</label>
      <input id="p" type="password" name="password" autocomplete="current-password"
             required></div>
    <button class="btn primary" type="submit">Войти</button>
  </form>
</div></div>"""
    return page("Вход", body, bare=True)


def render_list(people: list[dict], user: str, message: str = "",
                kind: str = "ok") -> bytes:
    rows = []
    current_group = None
    for p in people:
        chips = "".join(
            f'<span class="chip" title="{esc(NUMBER_LABELS[key])}">{esc(p[key])}</span>'
            for key in ("work", "mobile", "other") if p.get(key)
        ) or '<span class="muted">—</span>'
        label = esc(" ".join([p['company'], p['title'], p['fio'], " ".join(p['numbers'])]).lower())
        # the directory is a tree by organisation, so the list is grouped the same way
        if p["company"] != current_group:
            current_group = p["company"]
            title = esc(current_group) if current_group else "Без организации"
            rows.append(f'<tr class="group"><td colspan="5">{title}</td></tr>')
        rows.append(f"""<tr data-search="{label}">
<td data-label="Предприятие">{esc(p['company']) or '<span class="muted">—</span>'}</td>
<td data-label="Должность">{esc(p['title']) or '<span class="muted">—</span>'}</td>
<td class="fio" data-label="ФИО">{esc(p['fio'])}</td>
<td data-label="Телефоны"><div class="chips">{chips}</div></td>
<td class="actions">
  <a class="iconbtn" href="/edit?uid={quote(p['uid'])}" title="Изменить">{icon("pencil", 17)}</a>
  <form class="inline" method="post" action="/delete"
        onsubmit="return confirm('Удалить запись «{esc(p['fio'])}»?')">
    <input type="hidden" name="uid" value="{esc(p['uid'])}">
    <button class="iconbtn danger" type="submit" title="Удалить">{icon("trash", 17)}</button>
  </form>
</td></tr>""")

    if people:
        table = ('<div class="card"><div class="tablewrap"><table id="tbl"><thead><tr>'
                 "<th>Предприятие</th><th>Должность</th><th>ФИО</th><th>Телефоны</th><th></th>"
                 "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>")
    else:
        table = ('<div class="card empty">' + icon("book", 44) +
                 "<p>Записей пока нет</p>"
                 '<a class="btn primary" href="/new">' + icon("plus", 17) +
                 "Добавить</a></div>")

    body = f"""{flash(message, kind)}
<div class="toolbar">
  <div class="searchbox">{icon("search", 17)}
    <input type="text" id="q" placeholder="Поиск по любым полям" autocomplete="off"></div>
  <span class="counter" id="count"></span>
  <div class="tools">
    <a class="btn" href="/export.csv">{icon("download", 17)}Экспорт</a>
    <a class="btn" href="/import">{icon("upload", 17)}Импорт</a>
    <a class="btn primary" href="/new">{icon("plus", 17)}Добавить</a>
  </div>
</div>
{table}
<script>
const q = document.getElementById('q'), count = document.getElementById('count');
function applyFilter() {{
  const needle = q.value.trim().toLowerCase();
  let shown = 0, header = null, headerHit = false;
  const flush = () => {{ if (header) header.style.display = headerHit ? '' : 'none'; }};
  document.querySelectorAll('#tbl tbody tr').forEach(tr => {{
    if (tr.classList.contains('group')) {{
      flush();
      header = tr; headerHit = false;
      return;
    }}
    const hit = !needle || (tr.dataset.search || '').includes(needle);
    tr.style.display = hit ? '' : 'none';
    if (hit) {{ shown++; headerHit = true; }}
  }});
  flush();
  count.textContent = needle ? `найдено: ${{shown}}` : `всего: ${{shown}}`;
}}
q.addEventListener('input', applyFilter);
applyFilter();
</script>"""
    return page("Телефонная книга", body, user)


def render_form(person: dict | None, user: str, message: str = "") -> bytes:
    p = person or {"uid": "", "fio": "", "company": "", "title": "",
                   "work": "", "mobile": "", "other": ""}
    heading = "Изменить запись" if p["uid"] else "Новая запись"
    banner = flash(message, "err") if message else ""
    body = f"""{banner}
<div class="panel">
  <h2>{heading}</h2>
  <form method="post" action="/save">
    <input type="hidden" name="uid" value="{esc(p['uid'])}">
    <div class="grid2">
      <div class="field"><label for="c">Предприятие</label>
        <input id="c" type="text" name="company" value="{esc(p['company'])}" autofocus></div>
      <div class="field"><label for="t">Должность</label>
        <input id="t" type="text" name="title" value="{esc(p['title'])}"></div>
    </div>
    <div class="field"><label for="f">ФИО</label>
      <input id="f" type="text" name="fio" value="{esc(p['fio'])}" required></div>
    <div class="grid3">
      <div class="field"><label for="w">Рабочий</label>
        <input id="w" type="text" name="work" value="{esc(p['work'])}"
               placeholder="внутренний или городской"></div>
      <div class="field"><label for="m">Мобильный</label>
        <input id="m" type="text" name="mobile" value="{esc(p['mobile'])}"></div>
      <div class="field"><label for="o">Другой</label>
        <input id="o" type="text" name="other" value="{esc(p['other'])}"></div>
    </div>
    <div class="hint" style="margin:-6px 0 16px">По одному номеру в каждом поле:
      телефоны показывают эти три значения как «Telephone», «Mobile», «Other».</div>
    <div class="formactions">
      <button class="btn primary" type="submit">{icon("check", 17)}Сохранить</button>
      <a class="btn ghost" href="/">Отмена</a>
    </div>
  </form>
</div>"""
    return page(heading, body, user)


def render_import(user: str, message: str = "", kind: str = "ok") -> bytes:
    banner = flash(message, kind) if message else ""
    body = f"""{banner}
<div class="panel">
  <h2>Импорт CSV</h2>
  <form method="post" action="/import" enctype="application/x-www-form-urlencoded">
    <div class="field"><label>Файл CSV</label>
      <label class="filebox">{icon("upload", 22)}
        <span><strong>Выберите файл</strong> или перетащите его сюда</span>
        <input id="file" type="file" accept=".csv,text/csv">
        <span class="filename" id="fname"></span></label>
      <div class="hint">…или вставьте содержимое в поле ниже.</div></div>
    <div class="field"><label for="csv">Содержимое</label>
      <textarea id="csv" name="csv" spellcheck="false"
        placeholder="предприятие;должность;фио;телефоны"></textarea></div>
    <div class="formactions">
      <button class="btn primary" type="submit">{icon("upload", 17)}Импортировать</button>
      <a class="btn ghost" href="/">Отмена</a>
    </div>
  </form>
  <p class="hint" style="margin-top:18px">Колонки: предприятие, должность, ФИО, телефоны.
  Разделитель — «;», «,» или таб, кодировка UTF-8 или Windows-1251.
  Совпадение ищется по ФИО: существующая запись обновляется, новая добавляется.</p>
</div>
<script>
document.getElementById('file').addEventListener('change', async (e) => {{
  const f = e.target.files[0];
  if (!f) return;
  document.getElementById('fname').textContent = f.name;
  const buf = await f.arrayBuffer();
  let text = new TextDecoder('utf-8').decode(buf);
  if (text.includes('\\uFFFD')) {{ try {{ text = new TextDecoder('windows-1251').decode(buf); }} catch (_) {{}} }}
  document.getElementById('csv').value = text.replace(/^\\uFEFF/, '');
}});
</script>"""
    return page("Импорт CSV", body, user)


def send_csv(people: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Предприятие", "Должность", "ФИО", "Телефон", "Мобильный", "Другой"])
    for p in people:
        writer.writerow([p["company"], p["title"], p["fio"],
                         p.get("work", ""), p.get("mobile", ""), p.get("other", "")])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")  # BOM for Excel


# -------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "PhonebookUI/1.1"

    # -- helpers
    def _cookie_sid(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return None

    def _credentials(self) -> tuple[str, str] | None:
        """Session cookie first, then HTTP Basic (for scripts)."""
        data = get_session(self._cookie_sid())
        if data:
            return data["user"], data["password"]
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = raw.partition(":")
                return user, password
            except Exception:  # noqa: BLE001
                return None
        return None

    def _auth(self) -> tuple[Connection | None, str]:
        creds = self._credentials()
        if not creds:
            return None, ""
        conn = connect(*creds)
        return conn, (creds[0] if conn is not None else "")

    def _require_auth(self) -> tuple[Connection | None, str]:
        conn, user = self._auth()
        if conn is None:
            self._redirect("/login")
            return None, ""
        return conn, user

    def _body(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _origin_ok(self) -> bool:
        """Cheap CSRF guard: a cross-site form post carries a foreign Origin."""
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        return origin.split("://")[-1].rstrip("/") == self.headers.get("Host", "")

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/login":
            self._send(200, render_login())
            return
        conn, user = self._require_auth()
        if conn is None:
            return
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/":
                self._send(200, render_list(fetch(conn), user,
                                            query.get("msg", [""])[0],
                                            query.get("kind", ["ok"])[0]))
            elif path == "/new":
                self._send(200, render_form(None, user))
            elif path == "/edit":
                uid = query.get("uid", [""])[0]
                person = next((p for p in fetch(conn) if p["uid"] == uid), None)
                self._send(200 if person else 404,
                           render_form(person, user) if person
                           else page("Не найдено", "<div class='card empty'><p>Запись не найдена.</p></div>", user))
            elif path == "/import":
                self._send(200, render_import(user, query.get("msg", [""])[0],
                                              query.get("kind", ["ok"])[0]))
            elif path == "/export.csv":
                self._send(200, send_csv(fetch(conn)), "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="phonebook.csv"'})
            else:
                self._send(404, page("Не найдено",
                                     "<div class='card empty'><p>Страница не найдена.</p></div>", user))
        finally:
            conn.unbind()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._send(403, page("Отказ", "<div class='card empty'><p>Запрос отклонён.</p></div>"))
            return
        path = urlparse(self.path).path
        data = self._body()

        if path == "/login":
            user = (data.get("username") or "").strip()
            password = data.get("password") or ""
            conn = connect(user, password) if user else None
            if conn is None:
                self._send(401, render_login("Неверный логин или пароль."))
                return
            conn.unbind()
            sid = new_session(user, password)
            cookie = (f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax; "
                      f"Max-Age={SESSION_TTL}")
            self._redirect("/", cookie)
            return

        if path == "/logout":
            SESSIONS.pop(self._cookie_sid() or "", None)
            self._redirect("/login", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            return

        conn, user = self._require_auth()
        if conn is None:
            return
        try:
            if path == "/save":
                fio = norm(data.get("fio", ""))
                if not fio:
                    self._send(400, render_form(None, user, "Укажите ФИО."))
                    return
                company = norm(data.get("company", ""))
                title = norm(data.get("title", ""))
                # one number per field; the legacy "numbers" textarea is still
                # accepted and distributed over the free slots
                numbers = {key: norm(data.get(key, "")) for key, _attr in NUMBER_FIELDS}
                if not any(numbers.values()):
                    for slot, value in zip(("work", "mobile", "other"),
                                           parse_numbers(data.get("numbers", ""))):
                        numbers[slot] = value
                uid = data.get("uid", "").strip()
                if uid:
                    ok, msg = update_person(conn, uid, fio, company, title, numbers)
                    action = "изменена"
                else:
                    ok, msg = add_person(conn, fio, company, title, numbers)
                    action = "добавлена"
                if ok:
                    self._redirect(f"/?msg={quote('Запись ' + action + '.')}")
                elif msg in ("insufficientAccessRights", "insufficient_access_rights"):
                    self._send(403, render_list(
                        fetch(conn), user,
                        "Нет прав на изменение: вы вошли под учёткой только для чтения.", "err"))
                else:
                    self._send(400, render_form(
                        {"uid": uid, "fio": fio, "company": company,
                         "title": title, **numbers}, user,
                        f"Каталог отклонил изменение: {msg}"))
            elif path == "/delete":
                uid = data.get("uid", "").strip()
                ok, msg = delete_person(conn, uid) if uid else (False, "не указан uid")
                if ok:
                    self._redirect(f"/?msg={quote('Запись удалена.')}")
                else:
                    self._send(403, render_list(fetch(conn), user,
                                                f"Удалить не удалось: {msg}", "err"))
            elif path == "/import":
                rows = parse_csv(data.get("csv", ""))
                if not rows:
                    self._send(400, render_import(
                        user, "Не удалось разобрать CSV: нет строк с ФИО.", "err"))
                    return
                added, updated, failed, errors = import_rows(conn, rows)
                note = f"Добавлено: {added}, обновлено: {updated}"
                if failed:
                    note += f", с ошибками: {failed} — " + "; ".join(errors[:3])
                self._redirect(f"/?msg={quote(note)}&kind={'err' if failed else 'ok'}")
            else:
                self._send(404, page("Не найдено",
                                     "<div class='card empty'><p>Страница не найдена.</p></div>", user))
        finally:
            conn.unbind()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"phonebook UI on http://{HOST}:{PORT}  base={BASE_DN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
