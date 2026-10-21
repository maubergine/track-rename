# Contributing

track-rename fixes classical music movement names in Apple Music, using scripts that run on macOS.

Bug reports and feature requests are welcome as [issues](https://github.com/maubergine/track-rename/issues). Pull requests are
welcome too; CI on a pull request from a fork may wait for the maintainer to approve it.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `fix:`, `feat:`, `chore:`, `docs:` and so on. They
decide the next version: `fix` releases a patch, `feat` a minor version, and a breaking change a major one. CI checks
every commit (`commitlint.yml`). To check them locally before you push:

```sh
npm install   # installs commitlint and turns on .githooks/commit-msg
```

Commits to `main` must be signed.

## Tests

There's no test suite. CI compiles every script and lints for syntax errors and undefined names:

```sh
python3 -m py_compile *.py
uvx ruff check --isolated --select E9,F63,F7,F82 .
```
