# Installation

```sh
$ git clone <this repo>
$ cd agentfoo/opencode
$ pip install .

# or dev install
$ pip install -e .
```

or run CLI commands directly with
[`uvx`](https://docs.astral.sh/uv/guides/tools)

```sh
$ uvx --from=git+https://github.com/elcorto/agentfoo#subdirectory=opencode oc-sessions
```

# Commands

## oc-sessions

```
usage: oc-sessions [-h] [--db DB] [-s]

Browse and launch opencode sessions. We query the opencode SQLite database to
access all sessions: global (directory=$HOME most likely) and project
(directory=/path/to/some/git/repo).

options:
  -h, --help  show this help message and exit
  --db DB     Path to opencode SQLite database [default:
              ~/.local/share/opencode/opencode.db]
  -s, --sub   Include subagent sessions in list
```

> [!NOTE]
> Only tested up to `opencode` v1.19.5

> [!NOTE]
> `oc-sessions` is "vibe engineered" :-) so
> [YMMV](https://www.urbandictionary.com/define.php?term=ymmv).
