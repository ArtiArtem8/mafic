# Contributing to mafic

Thank you for your interest in contributing to mafic! We welcome contributions from the community. This document outlines the process to help get your contribution accepted.

## I Have a Question

For questions and support, please use the [Discussions Page](https://github.com/ooliver1/mafic/discussions).

## I Found a Bug

Please report bugs to the [Issues Page](https://github.com/nextcord/nextcord/issues/new/choose). Before reporting a bug, please do the following:

- Search the issue tracker to see if someone has already reported the bug.
- If your issue involves a traceback, please include **all** of it. It contains important information that can help us diagnose the problem including where the issue occured.

- Please provide as much information as possible about your issue. The issue template will help you with this, but more details are listed here
  - A **short summary** of your issue. This is a quick overview of what your issue is about.
  - Reproduction steps. This is a list of steps that can be followed to **reproduce the issue**. If you can, please provide a **minimal** code example that we can run to reproduce the issue.
  - Expected results. What did you **expect to happen?** This helps us see if your bug is, actually a bug.
  - Actual results. What happened instead. Do not use **"it doesn't work"** as a description. This is not helpful. Instead, explain what happened such as "<> error was thrown", "the music stopped playing", etc.
  - Information **about your environment.** This includes your operating system, Python version, version of mafic, version of lavalink, and any other relevant information.

Without providing this information, solving your issue is harder, and may be impossible, so please help us help you.

## Creating a Pull Request

Please make sure your PRs are properly scoped. This means that your PR should only contain one feature or bug fix. If you want to add multiple features or bug fixes, please submit multiple PRs.

Here is some helpful information and guidelines to contribute:

### Installing Dependencies Locally

The maintained fork supports Python 3.12–3.14 and targets Lavalink 4.2.2.
Use [Poetry 2.4.3](https://python-poetry.org/) and the committed `poetry.lock`:

```sh
pipx install poetry==2.4.3
poetry env use 3.12
poetry sync --extras speedups
poetry run python -m unittest discover -s tests -t .
```

The default `lint` group installs current Nextcord. To test another Discord
library, create a fresh virtual environment and select exactly one group with
`poetry sync --only main,dev,<group> --extras speedups`:

| Group | Library |
| --- | --- |
| `lint` | nextcord |
| `disnake` | disnake |
| `discordpy` | discord.py |
| `pycord` | py-cord with its required `voice` extra |

Pycord 2.8 requires `py-cord[voice]` to import `VoiceProtocol`; the group includes
this extra even though the tests need no Discord token.

Do not install discord.py and py-cord in the same environment: both own the
`discord` package. Compatibility tests use separate environments and do not set
`MAFIC_IGNORE_LIBRARY_CHECK`. CI runs import, all unit tests and slotscheck for
all four libraries on each supported Python version.

The optional `docs` group retains the existing Sphinx toolchain. Use
`poetry sync --with docs --extras speedups` on Python 3.12 for documentation work.

### Code Style and Type Checking

Black remains the formatter. Ruff checks code without imposing a migration to
PEP 695 generics, StrEnum, or sorted exports/slots. Only formatting required by
the pinned Black version is applied. Tool versions and hook revisions must be
updated together; regenerate and commit the lock with `poetry lock`.

```sh
poetry run task lint
poetry run pre-commit run --all-files
poetry run black --check .
poetry run ruff check .
poetry run python -m slotscheck -m mafic
```

Both lint commands run the same hooks. Pre-commit creates its own environment
with the pinned Nextcord dependencies for slotscheck. To install Git hooks, run
`poetry run task pre-commit`.

For static analysis, the library adapter needs all three import namespaces:

```sh
poetry sync --only main,dev,lint,disnake,discordpy --extras speedups
poetry run task pyright
```

This environment is for type checking; use an isolated library environment for
runtime tests. The `discord` namespace is supplied by discord.py here. Pycord's
runtime compatibility is tested separately. CI checks types for Python 3.12,
3.13 and 3.14 on Linux. A duplicate Windows type-check pass adds no coverage of
Mafic platform branches, since there are none; local Windows runtime tests still
exercise the event loop and imports.

### Lavalink Integration

CI has one separate job using Java 21 and the exact Lavalink 4.2.2 release.
It needs no Discord token and verifies REST track metadata round-tripping,
player updates and stopping. It does not test Discord voice transport.

To run locally, start Lavalink 4.2.2 with its YouTube source enabled (the fixture
is a pre-encoded YouTube track). Set `LAVALINK_INTEGRATION_URL` and
`LAVALINK_INTEGRATION_PASSWORD`, then run in an isolated library environment:

```sh
poetry run python -m unittest tests.integration_lavalink -v
```

Without these variables the integration test is explicitly skipped. Ordinary
unit-test discovery never downloads or starts Lavalink.

### Type Annotations

Mafic uses [Pyright](https://github.com/microsoft/pyright) for type checking. To use it, run `task pyright` in the root directory of the project, or `python -m task pyright` (`py -m` etc) if that does not work.

If type annotations are new to you, here is a [guide](https://decorator-factory.github.io/typing-tips/) to help you with it.

## Commits

Mafic follows the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) style. This means that your commit messages and PR titles should be formatted in a specific way. This is to help us generate changelogs and release notes, whilst also helping us review your pull requests as we can see what each commit does.

Your commit messages should be in the present (imperative, 2nd person) tense, such as `Add`, not `Added`.

More specifically, we use the [Angular Types List](https://github.com/angular/angular/blob/22b96b9/CONTRIBUTING.md#type) for commit types. This means that your commit messages should be formatted like this:

```text
<type>([scope]): <subject>
[BLANK LINE]
[body]
[BLANK LINE]
[footer]
```

> **Note**
> The type and subject are mandatory.

Examples include:

```text
feat: add support for some feature
```

```text
refactor(track)!: use a different method to get track info

This is a breaking change because the method used to get track info has changed.
```

```text
fix(node): use a different method to connect to the node

This fixes an issue where the node would not connect if the host was an IPV6 address.

Co-Authored-By: Some Person <email@example.com>
```

### Type

`<type>` is one of the following:

- **build**: Changes that affect the build system or external dependencies
- **ci**: Changes to our CI configuration files and scripts such as GitHub Actions
- **docs**: Documentation only changes
- **feat**: A new feature
- **fix**: A bug fix
- **perf**: A code change that improves performance
- **refactor**: A code change that neither fixes a bug nor adds a feature
- **style**: Changes that do not affect the meaning of the code (white-space, formatting, etc)
- **test**: Adding missing tests or correcting existing tests

### Scope

`[scope]` is the name of the module affected (as perceived by the person reading the changelog generated from commit messages). Scope is not required when the change affects multiple modules. Some examples are:

- `node.py` - `node` is the scope
- `track.py` - `track` is the scope
- `typing/` - `typing` is the scope
- `utils/` - `utils` is the scope

### Subject

The subject should be a short summary of the commit, the body can be used for more info, so do not cram so much into the subject. Ideally this should be 50 characters or less, but it is not a hard limit, 72 characters is fine if necessary.

### Body

The body is an optional long description about the commit, this can be used to explain the motivation for the change, and can be used to give more context about the change. It is not required, but it is recommended.

### Footer

The footer contains optional metadata about the commit. These are sometimes added by git or similar tools, and examples include `Co-authored-by`, `Signed-off-by`, `Fixes`.
