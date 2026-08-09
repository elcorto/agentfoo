import argparse
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

HOME_DIRECTORY = str(Path.home())
TITLE_COLUMN_WIDTH = 512
DIRECTORY_DISPLAY_MAX_CHARS = 48
PRIMARY_AGENTS = ("plan", "build")


@dataclass(frozen=True)
class SessionRow:
    id: str
    directory: str
    model: str
    agent: str
    title: str
    time_updated: int
    display_updated: str
    display_directory: str
    agent_models: tuple[tuple[str, str], ...]
    search_text: str


def format_directory(path_value: str, max_chars: int) -> str:
    session_path = Path(path_value)
    home_path = Path(HOME_DIRECTORY)
    try:
        relative_path = session_path.relative_to(home_path)
    except ValueError:
        pass
    else:
        if str(relative_path) == ".":
            path_value = "~"
        else:
            path_value = f"~/{relative_path.as_posix()}"

    if len(path_value) <= max_chars:
        return path_value

    parts = path_value.split("/")
    if len(parts) > 2:
        first = parts[0]
        for tail_count in range(len(parts) - 1, 0, -1):
            candidate = "/".join([first, "..."] + parts[-tail_count:])
            if len(candidate) <= max_chars:
                return candidate
        candidate = f"{first}/..."
        if len(candidate) <= max_chars:
            return candidate

    return f"...{path_value[-(max_chars - 3) :]}"


def format_date(updated_timestamp_ms: int) -> str:
    dt = datetime.fromtimestamp(
        updated_timestamp_ms / 1000,
        tz=timezone.utc,
    ).astimezone()
    if dt.year == datetime.now(tz=dt.tzinfo).year:
        return dt.strftime("%m-%d %H:%M")

    return dt.strftime("%Y-%m-%d %H:%M")


def format_full_date(updated_timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(
            updated_timestamp_ms / 1000,
            tz=timezone.utc,
        )
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def sort_agent_models(
    agent_models: list[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            agent_models,
            key=lambda agent_model: (
                agent_model[0] not in PRIMARY_AGENTS,
                PRIMARY_AGENTS.index(agent_model[0])
                if agent_model[0] in PRIMARY_AGENTS
                else agent_model[0],
            ),
        )
    )


def load_sessions(
    database_path: Path,
    include_subagent_sessions: bool,
) -> list[SessionRow]:
    subagent_clause = ""
    if not include_subagent_sessions:
        subagent_clause = "AND session.parent_id IS NULL"

    session_query = f"""
    SELECT
        session.id,
        session.directory,
        session.title,
        session.time_updated
    FROM session
    WHERE session.time_archived IS NULL
        {subagent_clause}
    ORDER BY session.time_updated DESC
    """
    agent_query = f"""
    WITH latest_agent_model AS (
        SELECT
            message.session_id,
            COALESCE(
                json_extract(message.data, '$.agent'),
                json_extract(message.data, '$.mode')
            ) AS agent,
            json_extract(message.data, '$.modelID') AS model_id,
            message.time_updated,
            message.id AS message_id,
            ROW_NUMBER() OVER (
                PARTITION BY
                    message.session_id,
                    COALESCE(
                        json_extract(message.data, '$.agent'),
                        json_extract(message.data, '$.mode')
                    )
                ORDER BY message.time_updated DESC, message.id DESC
            ) AS row_num
        FROM message
        JOIN session
            ON session.id = message.session_id
        WHERE session.time_archived IS NULL
            {subagent_clause}
            AND json_extract(message.data, '$.role') = 'assistant'
            AND json_extract(message.data, '$.modelID') IS NOT NULL
    )
    SELECT
        session_id,
        agent,
        model_id,
        time_updated,
        message_id
    FROM latest_agent_model
    WHERE row_num = 1
    """
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(session_query).fetchall()
        agent_rows = connection.execute(agent_query).fetchall()

    agent_rows_by_session: dict[str, list[sqlite3.Row]] = {}
    for agent_row in agent_rows:
        agent_rows_by_session.setdefault(agent_row["session_id"], []).append(
            agent_row
        )

    session_rows = []
    for database_row in rows:
        session_id = database_row["id"]
        directory = database_row["directory"]
        title = database_row["title"]
        time_updated = database_row["time_updated"]
        session_agent_rows = agent_rows_by_session.get(session_id, [])
        latest_agent_row = max(
            session_agent_rows,
            key=lambda agent_row: (
                agent_row["time_updated"],
                agent_row["message_id"],
            ),
            default=None,
        )
        if latest_agent_row is None:
            model = "-"
            agent = ""
        else:
            model = latest_agent_row["model_id"]
            agent = latest_agent_row["agent"] or ""

        display_updated = format_date(time_updated)
        display_directory = format_directory(
            directory, DIRECTORY_DISPLAY_MAX_CHARS
        )
        agent_models = sort_agent_models(
            [
                (agent_row["agent"], agent_row["model_id"])
                for agent_row in session_agent_rows
                if agent_row["agent"] is not None
            ]
        )
        search_text = f"{display_updated} {directory} {model} {title}"

        session_rows.append(
            SessionRow(
                id=session_id,
                directory=directory,
                model=model,
                agent=agent,
                title=title,
                time_updated=time_updated,
                display_updated=display_updated,
                display_directory=display_directory,
                agent_models=agent_models,
                search_text=search_text,
            )
        )
    return session_rows


def filter_sessions(
    session_rows: list[SessionRow],
    query: str,
) -> list[SessionRow]:
    stripped_query = query.strip()
    if not stripped_query:
        return list(session_rows)

    rex = re.compile(stripped_query, re.IGNORECASE)
    return [row for row in session_rows if rex.search(row.search_text)]


def validate_session_directory(directory: str) -> str | None:
    try:
        directory_stat = os.stat(directory)
    except OSError as error:
        reason = error.strerror or str(error)
        return f"can't access {directory}: {reason}"

    if not stat.S_ISDIR(directory_stat.st_mode):
        return f"can't access {directory}: not a directory"

    if not os.access(directory, os.R_OK | os.X_OK):
        return f"can't access {directory}: permission denied"

    return None


def get_opencode_command() -> str:
    for command_name in ["opencode-cli", "opencode"]:
        if command_path := shutil.which(command_name):
            return command_path

    raise FileNotFoundError("could not find 'opencode-cli' or 'opencode'")


class DeleteSessionScreen(ModalScreen[SessionRow | None]):
    CSS = """
    DeleteSessionScreen {
        align: center middle;
    }

    #delete-dialog {
        width: 72;
        height: auto;
        padding: 1 2;
        border: round $panel;
        background: $surface;
    }

    #delete-actions {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: right middle;
    }

    #delete-actions Button {
        margin-left: 1;
    }

    #delete-cancel {
        background: #2d4636;
        border-top: tall #3d5d46;
        border-bottom: tall #213327;
        color: #c9c0a7;
    }

    #delete-confirm {
        background: #4c292f;
        border-top: tall #693943;
        border-bottom: tall #351d21;
        color: #c9c0a7;
    }

    #delete-actions Button:focus {
        outline: heavy #c9c0a7;
        text-style: bold reverse;
    }

    #delete-key-hint {
        margin-top: 1;
        color: #c9c0a7;
        text-align: center;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(self, session_row: SessionRow) -> None:
        super().__init__()
        self.session_row = session_row

    def compose(self) -> ComposeResult:
        yield Container(
            Static("Delete this OpenCode session?"),
            Static(self.session_row.title, markup=False),
            Static(self.session_row.id, markup=False),
            Static("This permanently removes its recorded session data."),
            Horizontal(
                Button("Cancel", id="delete-cancel"),
                Button("Delete", id="delete-confirm"),
                id="delete-actions",
            ),
            Static(
                "Tab: select | Enter: confirm | Esc: cancel",
                id="delete-key-hint",
            ),
            id="delete-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#delete-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete-confirm":
            self.dismiss(self.session_row)
            return

        self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()


class SessionApp(App[SessionRow | None]):
    CSS = """
    Screen {
        layout: vertical;
        color: #c9c0a7;
    }

    #query {
        margin: 0 1;
        border: round $panel;
        color: #c9c0a7;
    }

    #query:focus {
        border: round $panel;
        background-tint: 0%;
    }

    #sessions {
        height: 1fr;
        margin: 0 1;
        color: #c9c0a7;
        scrollbar-size-horizontal: 0;
        overflow-x: hidden;
    }

    #sessions > .datatable--header,
    #sessions > .datatable--fixed {
        color: #c9c0a7;
    }

    #sessions > .datatable--cursor,
    #sessions:focus > .datatable--cursor,
    #sessions > .datatable--fixed-cursor,
    #sessions:focus > .datatable--fixed-cursor {
        background: #4d4d4d;
        color: #c9c0a7;
    }

    #status {
        height: 1;
        margin: 0 1;
        color: #c9c0a7;
    }

    #details {
        height: 7;
        margin: 0 1;
        border: round $panel;
        color: #c9c0a7;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "launch_selected", "Launch", priority=True),
        Binding(
            "up", "select_previous", "Previous", show=False, priority=True
        ),
        Binding("down", "select_next", "Next", show=False, priority=True),
        Binding(
            "tab", "focus_query", "Focus query", show=False, priority=True
        ),
        Binding(
            "shift+tab",
            "focus_query_previous",
            "Focus query",
            show=False,
            priority=True,
        ),
        Binding("ctrl+d", "request_delete", "Delete", priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        database_path: Path,
        include_subagent_sessions: bool,
        opencode_command: str,
    ) -> None:
        super().__init__()
        self.database_path = database_path
        self.include_subagent_sessions = include_subagent_sessions
        self.opencode_command = opencode_command
        self.all_sessions: list[SessionRow] = []
        self.visible_sessions: list[SessionRow] = []
        self.query_input: Input
        self.sessions_table: DataTable
        self.details_pane: Static
        self.status_line: Static

    def compose(self) -> ComposeResult:
        query_input = Input(
            placeholder="Filter sessions (regex: date dir model title) ...",
            id="query",
        )
        query_input.cursor_blink = False
        yield query_input
        yield DataTable(id="sessions")
        yield Static("", id="details", markup=False)
        yield Static("", id="status", markup=False)

    def on_mount(self) -> None:
        self.query_input = self.query_one("#query", Input)
        self.sessions_table = self.query_one("#sessions", DataTable)
        self.details_pane = self.query_one("#details", Static)
        self.status_line = self.query_one("#status", Static)

        self.sessions_table.cursor_type = "row"
        self.sessions_table.add_column("updated")
        self.sessions_table.add_column("directory")
        self.sessions_table.add_column("model")
        self.sessions_table.add_column("title", width=TITLE_COLUMN_WIDTH)

        try:
            self.all_sessions = load_sessions(
                self.database_path,
                include_subagent_sessions=self.include_subagent_sessions,
            )
        except sqlite3.Error as error:
            self.set_status(f"database error: {error}")
            self.visible_sessions = []
            self.sessions_table.clear()
            self.update_details()
            self.query_input.focus()
            return
        else:
            self.visible_sessions = list(self.all_sessions)

        self.refresh_table()
        self.query_input.focus()

    def on_input_changed(self, _event: Input.Changed) -> None:
        self.apply_filter()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_launch_selected()

    def on_data_table_row_highlighted(
        self, _event: DataTable.RowHighlighted
    ) -> None:
        self.set_default_status()
        self.update_details()

    def refresh_table(self) -> None:
        self.sessions_table.clear()
        for session_row in self.visible_sessions:
            self.sessions_table.add_row(
                session_row.display_updated,
                session_row.display_directory,
                session_row.model,
                session_row.title,
            )

        if self.visible_sessions:
            self.sessions_table.move_cursor(row=0, column=0)
        else:
            self.update_details()

        self.set_default_status()

    def set_status(self, message: str) -> None:
        self.status_line.update(
            f"{message} | open session: Enter | delete: Ctrl-d | exit: Ctrl-c"
        )

    def set_default_status(self) -> None:
        if self.visible_sessions:
            self.set_status(f"{len(self.visible_sessions)} sessions")
            return

        self.set_status("no matches")

    def get_selected_session(self) -> SessionRow | None:
        if not self.visible_sessions:
            return None

        selected_index = self.sessions_table.cursor_row
        if selected_index < 0 or selected_index >= len(self.visible_sessions):
            return None

        return self.visible_sessions[selected_index]

    def update_details(self) -> None:
        session_row = self.get_selected_session()
        if session_row is None:
            self.details_pane.update("No session selected")
            return

        details_text = Text()
        details_text.append(
            f"updated: {format_full_date(session_row.time_updated)}\n"
        )
        details_text.append(f"directory: {session_row.directory}\n")
        details_text.append("models: ")
        if session_row.agent_models:
            for index, (agent, model) in enumerate(session_row.agent_models):
                if index > 0:
                    details_text.append(" ")
                details_text.append(
                    f"{agent}:{model}",
                    style="italic"
                    if agent == session_row.agent
                    and model == session_row.model
                    else "",
                )
        else:
            details_text.append("-")
        details_text.append("\n")
        details_text.append(f"title: {session_row.title}\n")
        details_text.append(f"id: {session_row.id}")
        self.details_pane.update(details_text)

    def apply_filter(self) -> None:
        query_value = self.query_input.value
        try:
            self.visible_sessions = filter_sessions(
                self.all_sessions,
                query_value,
            )
        except re.error as error:
            self.visible_sessions = []
            self.refresh_table()
            self.set_status(f"invalid regex: {error.msg}")
            return

        self.refresh_table()

    def action_request_delete(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        session_row = self.get_selected_session()
        if session_row is None:
            self.set_status("no session selected")
            return

        self.push_screen(DeleteSessionScreen(session_row), self.delete_session)

    def delete_session(self, session_row: SessionRow | None) -> None:
        if session_row is None:
            return

        try:
            result = subprocess.run(
                [self.opencode_command, "session", "delete", session_row.id],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            reason = error.strerror or str(error)
            self.set_status(f"delete failed: {reason}")
            return

        if result.returncode != 0:
            reason = result.stderr.strip() or result.stdout.strip()
            if not reason:
                reason = f"exit status {result.returncode}"
            self.set_status(f"delete failed: {' '.join(reason.split())}")
            return

        try:
            self.all_sessions = load_sessions(
                self.database_path,
                include_subagent_sessions=self.include_subagent_sessions,
            )
        except sqlite3.Error as error:
            self.set_status(f"database error: {error}")
            return

        self.apply_filter()

    def action_launch_selected(self) -> None:
        if isinstance(self.screen, ModalScreen):
            focused_widget = self.screen.focused
            if isinstance(focused_widget, Button):
                focused_widget.press()
            return

        session_row = self.get_selected_session()
        if not session_row:
            self.set_status(
                "no session selected"
                if not self.visible_sessions
                else "invalid selection"
            )
            return

        directory_error = validate_session_directory(session_row.directory)
        if directory_error is not None:
            self.set_status(directory_error)
            return

        self.exit(session_row)

    def action_select_previous(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        self.sessions_table.action_cursor_up()
        self.query_input.focus()

    def action_select_next(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        self.sessions_table.action_cursor_down()
        self.query_input.focus()

    def action_focus_query(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return

        self.query_input.focus()

    def action_focus_query_previous(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return

        self.query_input.focus()

    def action_quit(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss()
            return

        self.exit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="""Browse and launch opencode sessions. We query the
        opencode SQLite database to access all sessions: global (directory=$HOME
        most likely) and project (directory=/path/to/some/git/repo)."""
    )
    parser.add_argument(
        "--db",
        default="~/.local/share/opencode/opencode.db",
        help="Path to opencode SQLite database [default: %(default)s]",
    )
    parser.add_argument(
        "-s",
        "--sub",
        action="store_true",
        help="Include subagent sessions in list",
    )

    arguments = parser.parse_args()
    database_path = Path(arguments.db).expanduser()

    if not database_path.is_file():
        parser.error(f"database not found: {database_path}")

    try:
        opencode_command = get_opencode_command()
    except FileNotFoundError as error:
        parser.error(str(error))

    session_app = SessionApp(
        database_path=database_path,
        include_subagent_sessions=arguments.sub,
        opencode_command=opencode_command,
    )
    selected_session = session_app.run()

    if selected_session is None:
        return 0

    try:
        result = subprocess.run(
            [opencode_command, "-s", selected_session.id],
            cwd=selected_session.directory,
            check=False,
        )
    except OSError as error:
        reason = error.strerror or str(error)
        parser.exit(1, f"{parser.prog}: failed to launch session: {reason}\n")

    return result.returncode
