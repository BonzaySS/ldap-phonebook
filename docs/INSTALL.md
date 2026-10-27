# Установка и обслуживание

## Требования

| | |
|---|---|
| ОС | Debian 12, Ubuntu 22.04/24.04, Raspbian (любой `apt`-дистрибутив с systemd) |
| Права | root |
| Диск | ~200 МБ (пакеты, venv, каталог) |
| Сеть | исходящий доступ к репозиторию дистрибутива и PyPI во время установки |
| Порты | свободные 389 (LDAP) и 8500 (веб-интерфейс) |

Скрипт проверяет ОС и systemd и откажется работать на RHEL/Alma/Fedora.

## Установка

```bash
tar -xzf ldap-phonebook-1.0.0.tar.gz
cd ldap-phonebook-1.0.0
sudo ./install.sh
```

Без ключей скрипт создаёт каталог `dc=phonebook,dc=local` (домен
`phonebook.local`), генерирует пароли и поднимает интерфейс на порту 8500.

Полезные варианты:

```bash
# свой каталог и свой порт
sudo ./install.sh --base-dn dc=acme,dc=ru --domain acme.ru --org "АО Акме" --port 8080

# без вопросов, для автоматизации
sudo ./install.sh --yes --admin-password '***' --read-password '***'

# посмотреть, что будет сделано, и ничего не менять
sudo ./install.sh --dry-run

# только каталог, без веб-интерфейса
sudo ./install.sh --ldap-only

# только интерфейс, к уже существующему каталогу
sudo ./install.sh --app-only --base-dn dc=acme,dc=ru --admin-password '***'

# сразу загрузить телефонную книгу из CSV
sudo ./install.sh --import people.csv

# дополнительно поставить ldap-ui (редактор атрибутов) на 127.0.0.1:5000
sudo ./install.sh --with-ldap-ui
```

### Все ключи

| Ключ | По умолчанию | Что делает |
|---|---|---|
| `--base-dn DN` | из `--domain` | база каталога, например `dc=acme,dc=ru` |
| `--domain NAME` | `phonebook.local` | домен для нового OpenLDAP |
| `--org NAME` | `Phonebook` | организация в каталоге |
| `--admin-password PW` | генерируется | пароль `cn=admin,<base-dn>` |
| `--read-user NAME` | `book` | имя учётной записи только для чтения |
| `--read-password PW` | генерируется | её пароль |
| `--people-ou NAME` | `people` | OU, где лежат контакты |
| `--port N` | `8500` | порт веб-интерфейса |
| `--bind ADDR` | `0.0.0.0` | адрес прослушивания |
| `--app-user NAME` | `phonebook` | системный пользователь службы |
| `--import FILE` | — | импортировать CSV после установки |
| `--with-ldap-ui` | выкл | поставить ldap-ui на `127.0.0.1:5000` |
| `--ldap-only` | выкл | только каталог |
| `--app-only` | выкл | только интерфейс |
| `--no-firewall` | выкл | не трогать `ufw` |
| `--dry-run` | выкл | показать действия без изменений |
| `--yes` | выкл | без подтверждения |
| `--force-acl` | выкл | перезаписать нестандартные правила доступа |

Любой ключ можно задать переменной окружения с префиксом `PB_`:
`PB_BASE_DN`, `PB_APP_PORT`, `PB_ADMIN_PW`, `PB_READ_PW` и так далее.

## Что делает установщик

1. Проверяет ОС, systemd, права root.
2. Ставит пакеты: `slapd`, `ldap-utils`, `python3-venv`, `curl`.
3. Создаёт каталог (если slapd ещё не настроен) через preseed debconf —
   без интерактивных вопросов.
4. Создаёт `ou=people,<base-dn>`.
5. Применяет правила доступа:
   - `userPassword`: `book` — запрещено, владелец — запись, аноним — только bind;
   - остальное: `admin` — запись, `book` — чтение, прочие аутентифицированные —
     чтение, **аноним — ничего**.
6. Создаёт учётную запись только для чтения (`cn=book,<base-dn>`).
7. Ставит приложение в `/opt/ldap-phonebook`, создаёт venv, ставит `ldap3`,
   регистрирует службу `ldap-phonebook.service`, запускает её и проверяет
   `/health`.
8. Открывает порт в `ufw`, если он активен.
9. Пишет `/root/ldap-phonebook-credentials.txt` и печатает сводку.

Повторный запуск безопасен: уже созданное пропускается, пароли не меняются
(кроме случая, когда вы явно передали `--read-password`).

## Существующий OpenLDAP

Если в системе уже есть каталог с другим суффиксом, установщик остановится:

```
ERROR: the existing directory uses 'dc=old,dc=local', this install targets 'dc=phonebook,dc=local'
```

Варианты:

```bash
# подключиться к существующему каталогу
sudo ./install.sh --app-only --base-dn dc=old,dc=local --admin-password '***'

# или развернуть всё в тот же существующий суффикс (правила доступа будут применены)
sudo ./install.sh --base-dn dc=old,dc=local --admin-password '***'
```

Правила доступа установщик меняет только если они совпадают со стандартными
Debian-ными. Если у вас свои правила, он откажется и покажет их — тогда либо
добавьте нужные строки руками, либо запустите с `--force-acl`, понимая, что
свои правила вы замените.

## Отдельная учётная запись для персонала

По умолчанию пароль администратора каталога = пароль веб-интерфейса. Чтобы
персонал мог править телефонную книгу, но не весь каталог, заведите учётную
запись с правом записи только в `ou=people`:

```bash
BASE=dc=acme,dc=ru
PEOPLE=ou=people,$BASE

# 1. учётная запись
slappasswd -s 'НовыйПароль' > /tmp/hash.txt
ldapadd -x -D "cn=admin,$BASE" -W <<EOF
dn: cn=editor,$BASE
objectClass: organizationalRole
objectClass: simpleSecurityObject
cn: editor
description: Edits the phone book only
userPassword: $(cat /tmp/hash.txt)
EOF

# 2. правило: editor пишет только в людях, в остальном каталоге — читает
ldapmodify -Q -Y EXTERNAL -H ldapi:/// <<EOF
dn: olcDatabase={1}mdb,cn=config
changetype: modify
delete: olcAccess
olcAccess: {2}to * by dn.exact="cn=admin,$BASE" write by dn.exact="cn=book,$BASE" read by users read by * none
-
add: olcAccess
olcAccess: {2}to dn.subtree="$PEOPLE" by dn.exact="cn=editor,$BASE" write by dn.exact="cn=admin,$BASE" write by dn.exact="cn=book,$BASE" read by users read by * none
olcAccess: {3}to * by dn.exact="cn=admin,$BASE" write by users read by * none
-
EOF
```

После этого в веб-интерфейсе можно входить как `editor` — он увидит и изменит
только контакты, а открыть `ldap-ui` и удалить каталог уже не сможет.

## TLS перед веб-интерфейсом

Интерфейс сам слушает HTTP. Если он должен быть доступен извне, поставьте
обратный прокси:

```nginx
server {
    listen 443 ssl;
    server_name phonebook.example.org;
    ssl_certificate     /etc/letsencrypt/live/phonebook.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/phonebook.example.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8500;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

И закройте прямой порт: `sudo ufw deny 8500/tcp`. Приложению для работы прокси
ничего дополнительно не нужно: оно не зависит от внешнего адреса.

## Резервные копии

```bash
# выгрузить весь каталог
slapcat -n 1 > backup-$(date +%F).ldif

# восстановить (каталог должен быть пуст или удалён)
systemctl stop slapd
slapadd -n 1 -l backup-2026-10-27.ldif
systemctl start slapd
```

Достаточно и выгрузки CSV из интерфейса — она содержит все видимые поля.

## Обновление

```bash
tar -xzf ldap-phonebook-1.1.0.tar.gz
cd ldap-phonebook-1.1.0
sudo ./install.sh --app-only --base-dn dc=acme,dc=ru --admin-password "$(cat /etc/ldap-phonebook/admin.pw)"
```

`--app-only` заменит код и перезапустит службу, не трогая каталог. Каталог и
правила доступа останутся как есть.

## Диагностика

| Симптом | Причина | Что делать |
|---|---|---|
| `systemctl status ldap-phonebook` — failed | не стартует Python или занят порт | `journalctl -u ldap-phonebook -n 50` |
| Интерфейс открывается, список пуст | нет прав на чтение `ou=people` | проверить правила `olcAccess` (см. `apply_acls` в `install.sh`) |
| Вход не проходит | введён неверный логин/пароль | логины: `admin`, `book`; пароли — в `/root/ldap-phonebook-credentials.txt` |
| `install.sh` падает на preseed slapd | старая версия пакета или занятый 389 | `journalctl -u slapd`, проверить `ss -ltnp \| grep 389` |
| `ERROR: the existing directory uses ...` | в системе уже есть каталог | `--app-only` или `--base-dn` существующего |
| Телефон ничего не находит | не задан логин в телефоне | анонимно каталог закрыт — см. [PHONES.md](PHONES.md) |

Полезные команды:

```bash
ss -ltnp | grep -E ':(389|8500)'          # кто слушает порты
ldapsearch -LLL -Q -Y EXTERNAL -H ldapi:/// -b cn=config olcAccess
ldapwhoami -x -D "cn=book,dc=acme,dc=ru" -w 'пароль'
```
