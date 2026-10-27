# Публикация на GitHub

Репозиторий уже подготовлен: лицензия, `.gitignore`, `.gitattributes` (принудительно
LF, чтобы скрипты не сломались при коммите из Windows), CI и сборка релиза.

## 1. Поставить git

Windows:

```powershell
winget install --id Git.Git -e
```

Linux: `sudo apt install git` (и `gh` — по желанию:
`sudo apt install gh`, затем `gh auth login`).

## 2. Инициализировать репозиторий

```bash
cd ldap-phonebook
git init -b main
git add .
git commit -m "ldap-phonebook 1.0.0: телефонная книга на OpenLDAP для SIP-телефонов"
```

Проверить, что лишнее не попало в коммит:

```bash
git status --short
git ls-files | grep -E 'dist/|\.tar\.gz|__pycache__' || echo "чисто"
```

Каталог `dist/` и архивы в `.gitignore`, в репозиторий они не попадают —
tarball собирает CI и прикладывает к релизу.

## 3. Создать репозиторий на GitHub

Вариант с GitHub CLI:

```bash
gh repo create ldap-phonebook --public --source=. --push
```

Вариант вручную: создать пустой репозиторий `ldap-phonebook` на github.com,
затем

```bash
git remote add origin https://github.com/<ВАШ_ЛОГИН>/ldap-phonebook.git
git push -u origin main
```

В этом репозитории ссылка в `README.md` уже указывает на `BonzaySS`. Если будете
публиковать форк — замените логин на свой, от него зависит ссылка на скачивание
релиза.

## 4. Выпустить релиз

```bash
git tag -a v1.0.0 -m "ldap-phonebook 1.0.0"
git push origin v1.0.0
```

Тег `v*` запускает workflow `.github/workflows/ci.yml`:

1. `lint` — `bash -n`, shellcheck, `py_compile`, сборка tarball;
2. `install-test` — полная установка на чистый `ubuntu-24.04` и прогон
   `tests/smoke.sh` (создание, правка, удаление, права, импорт CSV);
3. `release` — сборка `dist/ldap-phonebook-1.0.0.tar.gz` и прикрепление её
   вместе с `.sha256` к релизу.

Ссылка на скачивание всегда одна и та же:

```
https://github.com/<ВАШ_ЛОГИН>/ldap-phonebook/releases/latest/download/ldap-phonebook-1.0.0.tar.gz
```

## 5. Следующие версии

1. поднять версию в `VERSION` и `CHANGELOG.md`;
2. `git commit -am "1.1.0"`, `git tag v1.1.0`, `git push --follow-tags`.

Сборка локально, без CI:

```bash
bash packaging/build-dist.sh      # dist/ldap-phonebook-<version>.tar.gz + .sha256
```

## Что стоит проверить в настройках репозитория

- **About**: короткое описание, ссылка на `docs/INSTALL.md`, темы
  `ldap`, `openldap`, `yealink`, `fanvil`, `phonebook`, `sip`.
- **Releases**: включены (по умолчанию включены).
- **Actions**: разрешены (Settings → Actions → General → Allow all actions).
- **Секреты**: не нужны, workflow использует встроенный `GITHUB_TOKEN`.
