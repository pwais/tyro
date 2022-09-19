import argparse
import contextlib
import functools
import shutil
from typing import Any, ContextManager, Generator, List, Optional

import termcolor
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.table import Table
from rich.text import Text

from . import _strings

BORDER_STYLE = Style()
DESCRIPTION_STYLE = Style()
INVOCATION_STYLE = Style()
METAVAR_STYLE = Style()


def set_accent_color(accent_color: Optional[str]) -> None:
    """Set an accent color to use in help messages. Takes any color supported by `rich`,
    see `python -m rich.color`. Experimental."""
    global BORDER_STYLE
    BORDER_STYLE = Style(color=accent_color, dim=True)
    global DESCRIPTION_STYLE
    DESCRIPTION_STYLE = Style(color=accent_color, bold=True)
    global INVOCATION_STYLE
    INVOCATION_STYLE = Style(bold=True)
    global METAVAR_STYLE
    METAVAR_STYLE = Style(color=accent_color, bold=True)


set_accent_color(None)


def monkeypatch_len(obj: Any) -> int:
    if isinstance(obj, str):
        return len(_strings.strip_ansi_sequences(obj))
    else:
        return len(obj)


def dummy_termcolor_context() -> ContextManager[None]:
    """Context for turning termcolor off."""

    def dummy_colored(*args, **kwargs) -> str:
        return args[0]

    @contextlib.contextmanager
    def inner() -> Generator[None, None, None]:
        orig_colored = termcolor.colored
        termcolor.colored = dummy_colored
        yield
        termcolor.colored = orig_colored

    return inner()


def ansi_context() -> ContextManager[None]:
    """Context for working with ANSI codes + argparse:
    - Applies a temporary monkey patch for making argparse ignore ANSI codes when
      wrapping usage text.
    - Enables support for Windows via colorama.
    """

    @contextlib.contextmanager
    def inner() -> Generator[None, None, None]:
        if not hasattr(argparse, "len"):
            # Sketchy, but seems to work.
            argparse.len = monkeypatch_len  # type: ignore
            try:
                # Use Colorama to support coloring in Windows shells.
                import colorama  # type: ignore

                # Notes:
                #
                # (1) This context manager looks very nice and local, but under-the-hood
                # does some global operations which look likely to cause unexpected
                # behavior if another library relies on `colorama.init()` and
                # `colorama.deinit()`.
                #
                # (2) SSHed into a non-Windows machine from a WinAPI terminal => this
                # won't work.
                #
                # Fixing these issues doesn't seem worth it: it doesn't seem like there
                # are low-effort solutions for either problem, and more modern terminals
                # in Windows (PowerShell, MSYS2, ...) do support ANSI codes anyways.
                with colorama.colorama_text():
                    yield

            except ImportError:
                yield

            del argparse.len  # type: ignore
        else:
            # No-op when the context manager is nested.
            yield

    return inner()


def str_from_rich(
    renderable: RenderableType, width: Optional[int] = None, soft_wrap: bool = False
) -> str:
    console = Console(width=width)
    with console.capture() as out:
        console.print(renderable, soft_wrap=soft_wrap)
    return out.get().rstrip("\n")


def make_formatter_class(field_count: int) -> Any:
    return functools.partial(_ArgparseHelpFormatter, field_count=field_count)


class _ArgparseHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog, *, field_count: int):
        indent_increment = 4
        width = shutil.get_terminal_size().columns - 2

        # Try to make helptext more concise when we have a lot of fields!
        if field_count > 64:  # pragma: no cover
            # When there are more fields, make helptext more compact.
            max_help_position = 8
        else:
            max_help_position = 36  # Usual is 24.

        super().__init__(prog, indent_increment, max_help_position, width)

    def _format_args(self, action, default_metavar):
        """Override _format_args() to ignore nargs and always expect single string
        metavars."""
        get_metavar = self._metavar_formatter(action, default_metavar)

        out = get_metavar(1)[0]
        if isinstance(out, str):
            # Can result in an failed argparse assertion if we turn off soft wrapping.
            return str_from_rich(Text(out, style=METAVAR_STYLE), soft_wrap=True)
        return out

    def add_argument(self, action):  # pragma: no cover
        # Patch to avoid super long arguments from shifting the helptext of all of the
        # fields.
        prev_max_length = self._action_max_length
        super().add_argument(action)
        if self._action_max_length > self._max_help_position + 2:
            self._action_max_length = prev_max_length

    def _split_lines(self, text, width):
        text = self._whitespace_matcher.sub(" ", text).strip()
        # The textwrap module is used only for formatting help.
        # Delay its import for speeding up the common usage of argparse.
        import textwrap as textwrap

        # Sketchy, but seems to work.
        textwrap.len = monkeypatch_len  # type: ignore
        out = textwrap.wrap(text, width)
        del textwrap.len  # type: ignore
        return out

    def _fill_text(self, text, width, indent):
        return "".join(indent + line for line in text.splitlines(keepends=True))

    class _Section(object):
        def __init__(self, formatter, parent, heading=None):
            self.formatter = formatter
            self.parent = parent
            self.heading = heading
            self.items = []

        def format_help(self):
            if self.parent is None:
                return self._dcargs_format_root()
            else:
                return self._dcargs_format_nonroot()

        def _dcargs_format_root(self):
            console = Console(width=self.formatter._width)
            with console.capture() as capture:
                # Get rich renderables from items.
                top_parts = []
                column_parts = []
                column_parts_lines = []
                for func, args in self.items:
                    item_content = func(*args)
                    if item_content is None:
                        pass

                    # Add strings. (usage, description, etc)
                    elif isinstance(item_content, str):
                        if item_content.strip() == "":
                            continue
                        top_parts.append(Text.from_ansi(item_content))

                    # Add panels. (argument groups, subcommands, etc)
                    else:
                        assert isinstance(item_content, Panel)
                        column_parts.append(item_content)
                        # Estimate line count. This won't correctly account for
                        # wrapping, as we don't know the column layout yet.
                        column_parts_lines.append(
                            str_from_rich(item_content, width=65).strip().count("\n")
                            + 1
                        )

                # Split into columns.
                min_column_width = 65
                height_breakpoint = 50
                column_count = max(
                    1,
                    min(
                        sum(column_parts_lines) // height_breakpoint + 1,
                        self.formatter._width // min_column_width,
                    ),
                )
                if column_count > 1:
                    column_width = self.formatter._width // column_count - 1
                    # Correct the line count for each panel using the known column
                    # width. This will account for word wrap.
                    column_parts_lines = map(
                        lambda p: str_from_rich(p, width=column_width)
                        .strip()
                        .count("\n")
                        + 1,
                        column_parts,
                    )
                else:
                    column_width = None

                column_lines = [0 for i in range(column_count)]
                column_parts_grouped = [[] for i in range(column_count)]
                for p, l in zip(column_parts, column_parts_lines):
                    chosen_column = column_lines.index(min(column_lines))
                    column_parts_grouped[chosen_column].append(p)
                    column_lines[chosen_column] += l
                columns = Columns(
                    [Group(*g) for g in column_parts_grouped],
                    column_first=True,
                    width=column_width,
                )

                console.print(Group(*top_parts))
                console.print(columns)
            return capture.get()

        def _format_action(self, action: argparse.Action):
            invocation = self.formatter._format_action_invocation(action)
            help_position = min(
                self.formatter._action_max_length + 4, self.formatter._max_help_position
            )
            indent = self.formatter._current_indent

            item_parts: List[RenderableType] = []

            # Put invocation and help side-by-side.
            if (
                action.help
                and len(_strings.strip_ansi_sequences(invocation)) < help_position - 1
            ):
                table = Table(show_header=False, box=None, padding=0)
                table.add_column(width=help_position - indent)
                table.add_column()
                table.add_row(
                    Text.from_ansi(
                        invocation,
                        style=INVOCATION_STYLE,
                    ),
                    # Unescape % signs, which need special handling in argparse.
                    Text.from_ansi(action.help.replace("%%", "%")),
                )
                item_parts.append(table)

            # Put invocation and help on separate lines.
            else:
                item_parts.append(
                    Text.from_ansi(
                        invocation + "\n",
                        style=INVOCATION_STYLE,
                    )
                )
                if action.help:
                    item_parts.append(
                        Padding(
                            # Unescape % signs, which need special handling in argparse.
                            Text.from_ansi(action.help.replace("%%", "%")),
                            pad=(0, 0, 0, help_position),
                        )
                    )

            # Add subactions, indented.
            try:
                subaction: argparse.Action
                for subaction in action._get_subactions():  # type: ignore
                    self.formatter._indent()
                    item_parts.append(
                        Padding(
                            Group(*self._format_action(subaction)),
                            pad=(0, 0, 0, self.formatter._indent_increment),
                        )
                    )
                    self.formatter._dedent()
            except AttributeError:
                pass

            return item_parts

        def _dcargs_format_nonroot(self):
            # Add each child item as a rich renderable.
            description_part = None
            item_parts = []
            for func, args in self.items:
                item_content = func(*args)
                if (
                    getattr(func, "__func__", None)
                    is _ArgparseHelpFormatter._format_action
                ):
                    (action,) = args
                    assert isinstance(action, argparse.Action)
                    item_parts.extend(self._format_action(action))

                else:
                    assert isinstance(item_content, str)
                    if item_content.strip() != "":
                        assert (
                            description_part is None
                        )  # Should only have one description part.
                        description_part = Text.from_ansi(
                            item_content.strip() + "\n",
                            style=DESCRIPTION_STYLE,
                        )

            if len(item_parts) == 0:
                return None

            # Get heading.
            if self.heading is not argparse.SUPPRESS and self.heading is not None:
                current_indent = self.formatter._current_indent
                heading = "%*s%s:\n" % (current_indent, "", self.heading)
                # Remove colon from heading.
                heading = heading.strip()[:-1]
            else:
                heading = ""

            if description_part is not None:
                item_parts = [description_part, Rule(style=BORDER_STYLE)] + item_parts

            return Panel(
                Group(*item_parts),
                title=heading,
                title_align="left",
                border_style=BORDER_STYLE,
                # padding=(1, 1, 0, 1),
            )