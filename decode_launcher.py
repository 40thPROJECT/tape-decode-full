#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Ensure Qt platform plugins (xcb/cocoa/...) are discoverable inside
# PyInstaller onefile bundles (critical for Linux AppImage GUI launch).
# This must run BEFORE any PyQt6 import.
if getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    if _base:
        for _rel in (
            os.path.join("PyQt6", "Qt6", "plugins"),
            os.path.join("PyQt6", "Qt", "plugins"),
            "plugins",
        ):
            _plug = os.path.join(_base, _rel)
            if os.path.isdir(os.path.join(_plug, "platforms")):
                os.environ.setdefault(
                    "QT_QPA_PLATFORM_PLUGIN_PATH", os.path.join(_plug, "platforms")
                )
                os.environ.setdefault("QT_PLUGIN_PATH", _plug)
                break


from decode_runtime import (
    MICROARCH_AUTO,
    MICROARCH_LEVELS,
    build_tape_decode_command,
    load_profiles,
    microarch_target_cpu,
    microarch_target_dir,
    native_host_arch,
    normalize_microarch_level,
    resolve_tape_decode_prefix,
)

try:
    from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
    from PyQt6.QtGui import QColor, QIcon, QPalette
    from PyQt6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMessageBox,
        QRadioButton,
        QPushButton,
        QSpinBox,
        QStyleFactory,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit("PyQt6 is required for Tape Decode Full.") from exc


ALIGN_TOP = Qt.AlignmentFlag.AlignTop


@dataclass(frozen=True)
class ToolSpec:
    label: str
    subcommand: str
    notes: str = ""


TOOLS = [
    ToolSpec(
        label="tape-decode-full decode (guided)",
        subcommand="decode",
        notes="Builds a decode command from the form fields and launches it in a terminal.",
    ),
    ToolSpec(
        label="tape-decode-full list-profiles (terminal)",
        subcommand="list-profiles",
        notes="Runs list-profiles; optional flags can be added in Extra arguments.",
    ),
    ToolSpec(
        label="tape-decode-full compare (terminal)",
        subcommand="compare",
        notes="Runs compare; provide required compare arguments in Extra arguments.",
    ),
    ToolSpec(
        label="tape-decode-full write-profile (terminal)",
        subcommand="write-profile",
        notes="Runs write-profile; provide required arguments in Extra arguments.",
    ),
    ToolSpec(
        label="tape-decode-full split (terminal)",
        subcommand="split",
        notes=(
            "Cuts a capture into standalone pieces to decode on other machines. "
            "Give the capture and an output directory, e.g. "
            "capture.ldf pieces/ --parts 4. Keep the .parts.json it writes: "
            "merge needs it to know where each decode belongs."
        ),
    ),
    ToolSpec(
        label="tape-decode-full merge (terminal)",
        subcommand="merge",
        notes=(
            "Joins .tbc decodes that follow on from each other, in tape order, "
            "e.g. pc1.tbc pc2.tbc -o tape -m pieces/capture.parts.json."
        ),
    ),
    ToolSpec(
        label="tape-decode-full insert (terminal)",
        subcommand="insert",
        notes=(
            "Fills a gap in the middle of a finished decode, for when one "
            "machine's piece had to be decoded again, e.g. "
            "--into tape.tbc --insert gap.tbc. Rewrites the file in place; "
            "run it with --dry-run first."
        ),
    ),
]

INPUT_FORMATS = ["u8", "s8", "s16le", "u16le", "f32le", "flac"]
DEFAULT_PROFILE = "PAL_VHS"

# UI labels for the microarchitecture selector. Keep the first entry as the
# empty "Auto" choice so the QComboBox `currentIndex` 0 stays the safe default.
MICROARCH_UI_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Auto (use host default)", MICROARCH_AUTO),
) + tuple((label, label) for label in MICROARCH_LEVELS)


def _rf_total_samples(body: bytes):
    """On-disk sample count from a FLAC VORBIS_COMMENT block, if it records one.

    Two schemas exist: when RF_SAMPLE_RATE is below 1 MHz both it and
    RF_TOTAL_SAMPLES are the "/1000" header values and need scaling by 1000.
    Getting that backwards would be wrong by three orders of magnitude.
    """
    try:
        off = 0
        vendor = int.from_bytes(body[off:off + 4], "little")
        off += 4 + vendor
        count = int.from_bytes(body[off:off + 4], "little")
        off += 4
        tags = {}
        for _ in range(count):
            size = int.from_bytes(body[off:off + 4], "little")
            off += 4
            text = body[off:off + size].decode("utf-8", "replace")
            off += size
            key, _, value = text.partition("=")
            tags[key.upper()] = value.strip()
        rate = float(tags["RF_SAMPLE_RATE"]) if "RF_SAMPLE_RATE" in tags else None
        scale = 1000.0 if rate and 0 < rate < 1.0e6 else 1.0
        if "RF_TOTAL_SAMPLES" in tags:
            return int(float(tags["RF_TOTAL_SAMPLES"]) * scale)
        if "DURATION_SECONDS" in tags and rate:
            return int(float(tags["DURATION_SECONDS"]) * rate * scale)
    except Exception:
        return None
    return None


def _split_user_args(extra_args: str, *, strict: bool = True) -> list[str]:
    if not extra_args.strip():
        return []
    try:
        parsed = shlex.split(extra_args, posix=os.name != "nt")
        if os.name == "nt":
            return [
                arg[1:-1]
                if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {'"', "'"}
                else arg
                for arg in parsed
            ]
        return parsed
    except ValueError:
        if strict:
            raise
        return [extra_args]


def _arg_writes_raw_output_to_stdout(args: list[str]) -> bool:
    output_flags = {"--luma-out", "--chroma-out"}
    i = 0
    while i < len(args):
        token = args[i]
        if token in output_flags:
            if i + 1 < len(args) and args[i + 1] == "-":
                return True
            i += 2
            continue
        if any(token == f"{flag}=-" for flag in output_flags):
            return True
        i += 1
    return False


def _extract_dropped_file_path(mime_data, *, suffix_filter: Optional[set[str]] = None) -> Optional[str]:
    if mime_data is None:
        return None

    paths: list[str] = []
    if mime_data.hasUrls():
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            local = url.toLocalFile().strip()
            if local:
                paths.append(local)
    elif mime_data.hasText():
        text = mime_data.text().strip()
        if text:
            local = QUrl(text).toLocalFile() if text.startswith("file:") else text
            if local:
                paths.append(local)

    for raw in paths:
        expanded = str(Path(raw).expanduser())
        if suffix_filter:
            suffix = Path(expanded).suffix.lower()
            if suffix not in suffix_filter:
                continue
        if Path(expanded).is_dir():
            continue
        return expanded
    return None


def _shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def _shell_join_windows(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts)


def _shell_join_platform(parts: list[str]) -> str:
    return _shell_join_windows(parts) if os.name == "nt" else _shell_join(parts)

def _resolve_icon_path() -> Optional[Path]:
    """Find a suitable icon PNG for the window/taskbar icon.

    Works in source tree, onefile PyInstaller bundles (_MEIPASS), next to the
    executable, and inside AppImages (checks typical hicolor locations).
    """
    candidates: list[Path] = []

    # PyInstaller onefile bundle
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.extend([
            mp / "resources" / "icon" / "tape-decode-full-256.png",
            mp / "tape-decode-full-256.png",
            mp / "tape-decode-full-gui.png",
        ])

    # Next to the frozen executable (AppImage mount, extracted, or onefile dir)
    if getattr(sys, "frozen", False) or meipass:
        try:
            exe_dir = Path(sys.executable).resolve().parent
            candidates.extend([
                exe_dir / "resources" / "icon" / "tape-decode-full-256.png",
                exe_dir / "tape-decode-full-256.png",
                exe_dir / "tape-decode-full-gui.png",
            ])
            # AppImage mount layout: exe at <mount>/usr/bin/tape-decode-full-gui
            # hicolor and .DirIcon live at <mount>/usr/share/... and <mount>/.DirIcon
            mount_root = exe_dir.parent  # <mount>/usr
            candidates.append(mount_root / "share" / "icons" / "hicolor" / "256x256" / "apps" / "tape-decode-full-gui.png")
            candidates.append(mount_root.parent / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "tape-decode-full-gui.png")
            # Walk up a few levels to find AppDir root or mount root (robustness)
            p = exe_dir
            for _ in range(6):
                candidates.append(p / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "tape-decode-full-gui.png")
                candidates.append(p / ".DirIcon")
                candidates.append(p / "tape-decode-full-gui.png")
                if (p / ".DirIcon").exists() or (p / "usr" / "share").exists():
                    break
                p = p.parent
        except Exception:
            pass

    # Source tree (development)
    try:
        here = Path(__file__).resolve().parent
        candidates.extend([
            here / "resources" / "icon" / "tape-decode-full-256.png",
            here.parent / "resources" / "icon" / "tape-decode-full-256.png",
            Path.cwd() / "resources" / "icon" / "tape-decode-full-256.png",
        ])
    except Exception:
        pass

    for c in candidates:
        try:
            if c and c.is_file():
                return c.resolve()
        except Exception:
            continue
    return None


def _open_linux_terminal(shell_command: str) -> None:
    shell = os.environ.get("SHELL", "/bin/bash")
    shell_args = [shell, "-lc", shell_command]
    terminal_candidates: list[tuple[str, list[str]]] = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", *shell_args]),
        ("gnome-terminal", ["gnome-terminal", "--", *shell_args]),
        ("kgx", ["kgx", "--", *shell_args]),
        ("konsole", ["konsole", "-e", *shell_args]),
        ("mate-terminal", ["mate-terminal", "--", *shell_args]),
        ("xfce4-terminal", ["xfce4-terminal", "--command", f"{shell} -lc {shlex.quote(shell_command)}"]),
        ("lxterminal", ["lxterminal", "-e", f"{shell} -lc {shlex.quote(shell_command)}"]),
        ("kitty", ["kitty", "--hold", *shell_args]),
        ("alacritty", ["alacritty", "-e", *shell_args]),
        ("xterm", ["xterm", "-hold", "-e", *shell_args]),
    ]

    for binary, command in terminal_candidates:
        if shutil.which(binary):
            subprocess.Popen(command)
            return

    raise RuntimeError(
        "Could not find a supported Linux terminal emulator (e.g. gnome-terminal, konsole, xterm)."
    )


def _open_terminal(
    command_parts: list[str],
    working_directory: Path,
    *,
    env_exports: Optional[dict[str, str]] = None,
) -> None:
    if os.name == "nt":
        # On Windows, a fresh console doesn't inherit any env we set here
        # unless we mutate os.environ before Popen.
        prior: dict[str, Optional[str]] = {}
        if env_exports:
            for key, value in env_exports.items():
                prior[key] = os.environ.get(key)
                os.environ[key] = value
        try:
            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                command_parts,
                cwd=str(working_directory),
                creationflags=creation_flags,
            )
        finally:
            if env_exports:
                for key, original in prior.items():
                    if original is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = original
        return

    env_prefix = ""
    if env_exports:
        exports = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in env_exports.items()
        )
        env_prefix = f"{exports} "

    command = _shell_join(command_parts)
    shell = os.environ.get("SHELL", "/bin/bash")
    shell_command = (
        f'echo "[decode-launcher] starting command..."; '
        f"cd {shlex.quote(str(working_directory))} && {env_prefix}{command}; "
        "status=$?; "
        "echo; "
        'echo "[decode-launcher] process finished with exit code $status"; '
        f"exec {shlex.quote(shell)} -l"
    )

    if sys.platform == "darwin":
        escaped = shell_command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(
            [
                "osascript",
                "-e",
                'tell application "Terminal" to activate',
                "-e",
                f'tell application "Terminal" to do script "{escaped}"',
            ]
        )
        return

    _open_linux_terminal(shell_command)


class FileDropLineEdit(QLineEdit):
    fileDropped = pyqtSignal(str)

    def __init__(self, *, suffix_filter: Optional[set[str]] = None):
        super().__init__()
        self._suffix_filter = suffix_filter
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        dropped = _extract_dropped_file_path(
            event.mimeData(), suffix_filter=self._suffix_filter
        )
        if dropped:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        dropped = _extract_dropped_file_path(
            event.mimeData(), suffix_filter=self._suffix_filter
        )
        if dropped:
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        dropped = _extract_dropped_file_path(
            event.mimeData(), suffix_filter=self._suffix_filter
        )
        if dropped:
            self.setText(dropped)
            self.fileDropped.emit(dropped)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class DecodeLauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._tools = TOOLS
        self.setWindowTitle("Tape Decode Full")
        self.resize(860, 360)

        self.tool_combo = QComboBox()
        for tool in self._tools:
            self.tool_combo.addItem(tool.label)
        self.tool_combo.setCurrentIndex(0)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)

        self.input_edit = FileDropLineEdit()
        self.input_edit.setPlaceholderText("Drop RF input file here")
        self.input_browse_button = QPushButton("Input…")

        self.output_edit = QLineEdit("")
        self.output_browse_button = QPushButton("Output…")

        self.profile_combo = QComboBox()
        # Only names from the list are valid; a typed one fails at launch
        # with nothing on screen to say which are accepted.
        self.profile_combo.setEditable(False)
        self.profile_combo.addItem(DEFAULT_PROFILE)
        self.refresh_profiles_button = QPushButton("Refresh profiles")

        self.use_profile_file_check = QCheckBox("Use profile JSON file")
        self.profile_file_edit = FileDropLineEdit(suffix_filter={".json"})
        self.profile_file_edit.setPlaceholderText("Drop profile JSON file here")
        self.profile_file_browse_button = QPushButton("Profile JSON…")

        self.frequency_edit = QLineEdit("40")
        self.input_format_combo = QComboBox()
        self.input_format_combo.addItems(INPUT_FORMATS)
        self.input_format_combo.setCurrentText("flac")

        self.microarch_combo = QComboBox()
        for ui_label, _value in MICROARCH_UI_OPTIONS:
            self.microarch_combo.addItem(ui_label)
        self.microarch_combo.setCurrentIndex(0)
        self.microarch_locate_button = QPushButton("Locate binary")
        self.microarch_locate_button.setToolTip(
            "Show the on-disk path of the tape-decode binary that will be used\n"
            "for the selected x86-64 microarchitecture level."
        )
        non_x86 = native_host_arch() != "x86_64"
        if non_x86:
            self.microarch_combo.setEnabled(False)
            self.microarch_locate_button.setEnabled(False)
            self.microarch_combo.setToolTip(
                "x86-64 microarchitecture selection is only relevant on x86_64 hosts.\n"
                "On this host, compile from source with RUSTFLAGS=\"-C target-cpu=native\"."
            )

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 64)
        self.threads_spin.setValue(4)
        self.mt_distance_size_spin = QSpinBox()
        self.mt_distance_size_spin.setRange(1, 1000000)
        self.mt_distance_size_spin.setValue(60)

        self.include_chroma_check = QCheckBox("Write chroma output (_chroma.tbc)")
        self.include_chroma_check.setChecked(True)
        self.include_metadata_check = QCheckBox("Write metadata output (.tbc.json)")
        self.include_metadata_check.setChecked(True)
        self.ire0_adjust_check = QCheckBox("Adjust RF IRE0 (--ire0-adjust)")
        self.ire0_adjust_check.setChecked(True)

        self.overwrite_check = QCheckBox("Allow overwrite (--overwrite)")
        self.debug_check = QCheckBox("Enable debug logging (--debug)")

        self.extra_args_edit = QLineEdit("")

        # -- split ------------------------------------------------------
        self.parts_spin = QSpinBox()
        self.parts_spin.setRange(1, 999)
        self.parts_spin.setValue(2)
        self.overlap_spin = QDoubleSpinBox()
        self.overlap_spin.setRange(0.0, 600.0)
        self.overlap_spin.setSingleStep(0.5)
        self.overlap_spin.setDecimals(2)
        self.overlap_spin.setValue(2.0)
        # The three sources of the capture length are mutually exclusive, so a
        # button group enforces that rather than leaving it to the user.
        self.length_auto_radio = QRadioButton("Read from the file")
        self.length_samples_radio = QRadioButton("Samples")
        self.length_seconds_radio = QRadioButton("Seconds")
        self.length_auto_radio.setChecked(True)
        self.length_group = QButtonGroup(self)
        for button in (
            self.length_auto_radio,
            self.length_samples_radio,
            self.length_seconds_radio,
        ):
            self.length_group.addButton(button)
        self.length_value_edit = QLineEdit("")
        self.length_value_edit.setPlaceholderText("length")
        self.split_info_label = QLabel("")
        self.split_info_label.setWordWrap(True)

        # -- merge ------------------------------------------------------
        # Order is the semantics here: passed out of order the tape interleaves,
        # so the list is explicit and reorderable rather than a text field.
        self.tbc_list = QListWidget()
        self.tbc_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.tbc_list.setMaximumHeight(120)
        self.tbc_add_button = QPushButton("Add files")
        self.tbc_up_button = QPushButton("Up")
        self.tbc_down_button = QPushButton("Down")
        self.tbc_remove_button = QPushButton("Remove")
        self.tbc_sort_button = QPushButton("Sort by tape position")
        self.manifest_edit = FileDropLineEdit(suffix_filter={".json"})
        self.manifest_browse_button = QPushButton("Browse")
        self.merge_info_label = QLabel("")
        self.merge_info_label.setWordWrap(True)

        # -- insert -----------------------------------------------------
        self.into_edit = FileDropLineEdit(suffix_filter={".tbc"})
        self.into_browse_button = QPushButton("Browse")
        self.piece_edit = FileDropLineEdit(suffix_filter={".tbc"})
        self.piece_browse_button = QPushButton("Browse")
        self.fps_combo = QComboBox()
        for label, value in (("29.97 (NTSC)", 30000.0 / 1001.0), ("25 (PAL)", 25.0)):
            self.fps_combo.addItem(label, value)
        self.dry_run_check = QCheckBox("Dry run - report what would happen, write nothing")
        self.dry_run_check.setChecked(True)
        self.insert_info_label = QLabel("")
        self.insert_info_label.setWordWrap(True)

        # Shown whenever the form is not fit to launch.
        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        self.command_preview = QLineEdit("")
        self.command_preview.setReadOnly(True)

        self.launch_button = QPushButton("Launch selected tool")
        self.launch_tbc_tools_button = QPushButton("Launch tbc-tools / ld-analyse")
        self.close_button = QPushButton("Close")

        self._output_manually_set = False

        self._build_layout()
        self._wire_events()
        self._refresh_tool_state()

        QTimer.singleShot(0, self._refresh_profiles)

    def _build_layout(self) -> None:
        root = QVBoxLayout()
        root.setAlignment(ALIGN_TOP)

        launch_group = QGroupBox("")
        launch_layout = QGridLayout()
        launch_group.setLayout(launch_layout)

        launch_layout.addWidget(QLabel("Tool"), 0, 0)
        launch_layout.addWidget(self.tool_combo, 0, 1, 1, 3)

        launch_layout.addWidget(QLabel("Input file"), 1, 0)
        launch_layout.addWidget(self.input_edit, 1, 1, 1, 2)
        launch_layout.addWidget(self.input_browse_button, 1, 3)

        launch_layout.addWidget(QLabel("Output base"), 2, 0)
        launch_layout.addWidget(self.output_edit, 2, 1, 1, 2)
        launch_layout.addWidget(self.output_browse_button, 2, 3)

        launch_layout.addWidget(QLabel("Profile"), 3, 0)
        launch_layout.addWidget(self.profile_combo, 3, 1, 1, 2)
        launch_layout.addWidget(self.refresh_profiles_button, 3, 3)

        launch_layout.addWidget(self.use_profile_file_check, 4, 0, 1, 4)

        launch_layout.addWidget(QLabel("Profile file"), 5, 0)
        launch_layout.addWidget(self.profile_file_edit, 5, 1, 1, 2)
        launch_layout.addWidget(self.profile_file_browse_button, 5, 3)

        launch_layout.addWidget(QLabel("Frequency (MHz)"), 6, 0)
        launch_layout.addWidget(self.frequency_edit, 6, 1)
        launch_layout.addWidget(QLabel("Input format"), 6, 2)
        launch_layout.addWidget(self.input_format_combo, 6, 3)

        launch_layout.addWidget(QLabel("Threads (0 = serial)"), 7, 0)
        launch_layout.addWidget(self.threads_spin, 7, 1)
        launch_layout.addWidget(QLabel("MT distance size"), 7, 2)
        launch_layout.addWidget(self.mt_distance_size_spin, 7, 3)

        launch_layout.addWidget(QLabel("x86-64 microarch level"), 8, 0)
        launch_layout.addWidget(self.microarch_combo, 8, 1, 1, 2)
        launch_layout.addWidget(self.microarch_locate_button, 8, 3)

        launch_layout.addWidget(self.include_chroma_check, 9, 0, 1, 2)
        launch_layout.addWidget(self.include_metadata_check, 10, 2, 1, 2)
        launch_layout.addWidget(self.overwrite_check, 11, 0, 1, 2)
        launch_layout.addWidget(self.ire0_adjust_check, 11, 2, 1, 2)
        launch_layout.addWidget(self.debug_check, 12, 0, 1, 2)

        # Rows owned by one tool each; hidden unless that tool is selected, so
        # the window keeps the shape it had before these were added.
        self.split_rows = [
            self._add_row(launch_layout, 16, "Pieces", self.parts_spin,
                          QLabel("Overlap (s)"), self.overlap_spin),
            self._add_row(launch_layout, 17, "Capture length",
                          self.length_auto_radio, self.length_samples_radio,
                          self.length_seconds_radio, self.length_value_edit),
            self._add_row(launch_layout, 18, None, self.split_info_label),
        ]
        self.merge_rows = [
            self._add_row(launch_layout, 19, "Decoded .tbc files", self.tbc_list),
            self._add_row(launch_layout, 20, None, self.tbc_add_button,
                          self.tbc_up_button, self.tbc_down_button,
                          self.tbc_remove_button, self.tbc_sort_button),
            self._add_row(launch_layout, 21, "Manifest", self.manifest_edit,
                          self.manifest_browse_button),
            self._add_row(launch_layout, 22, None, self.merge_info_label),
        ]
        self.insert_rows = [
            self._add_row(launch_layout, 23, "Decode with the gap",
                          self.into_edit, self.into_browse_button),
            self._add_row(launch_layout, 24, "Piece to insert",
                          self.piece_edit, self.piece_browse_button),
            self._add_row(launch_layout, 25, "Frame rate", self.fps_combo),
            self._add_row(launch_layout, 26, None, self.dry_run_check),
            self._add_row(launch_layout, 27, None, self.insert_info_label),
        ]
        self._add_row(launch_layout, 28, None, self.problem_label)

        launch_layout.addWidget(QLabel("Extra arguments"), 13, 0)
        launch_layout.addWidget(self.extra_args_edit, 13, 1, 1, 3)

        launch_layout.addWidget(QLabel("Terminal preview"), 14, 0)
        launch_layout.addWidget(self.command_preview, 14, 1, 1, 3)

        launch_layout.addWidget(self.note_label, 15, 0, 1, 4)

        action_row = QHBoxLayout()
        action_row.addWidget(self.launch_button)
        action_row.addWidget(self.launch_tbc_tools_button)
        action_row.addWidget(self.close_button)

        root.addWidget(launch_group)
        root.addLayout(action_row)
        self.setLayout(root)

    def _add_row(self, layout, row: int, label, *widgets) -> list:
        """Add one labelled row and return its widgets, so it can be hidden."""
        owned = []
        column = 0
        if label is not None:
            tag = QLabel(label)
            layout.addWidget(tag, row, 0)
            owned.append(tag)
            column = 1
        for widget in widgets:
            span = 4 - column if widget is widgets[-1] and len(widgets) == 1 else 1
            layout.addWidget(widget, row, column, 1, max(1, span))
            owned.append(widget)
            column += max(1, span)
        return owned

    @staticmethod
    def _set_row_visible(rows, visible: bool) -> None:
        for row in rows:
            for widget in row:
                widget.setVisible(visible)

    def _tbc_paths(self) -> list[str]:
        return [
            self.tbc_list.item(i).text() for i in range(self.tbc_list.count())
        ]

    @staticmethod
    def _tbc_first_loc(path: str):
        """First field position of a decode, or None if it cannot be read."""
        try:
            with open(path + ".json", encoding="utf-8") as handle:
                fields = json.load(handle).get("fields") or []
            return fields[0]["fileLoc"] if fields else None
        except Exception:
            return None

    def _sort_tbc_by_position(self) -> None:
        paths = self._tbc_paths()
        ordered = sorted(
            paths, key=lambda p: (self._tbc_first_loc(p) is None,
                                  self._tbc_first_loc(p) or 0)
        )
        self.tbc_list.clear()
        self.tbc_list.addItems(ordered)
        self._refresh_tool_state()

    def _add_tbc_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select decoded .tbc files", "", "TBC files (*.tbc);;All files (*)"
        )
        existing = set(self._tbc_paths())
        for path in paths:
            if path not in existing:
                self.tbc_list.addItem(path)
        self._refresh_tool_state()

    def _move_tbc(self, delta: int) -> None:
        row = self.tbc_list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.tbc_list.count():
            return
        item = self.tbc_list.takeItem(row)
        self.tbc_list.insertItem(target, item)
        self.tbc_list.setCurrentRow(target)
        self._refresh_tool_state()

    def _remove_tbc(self) -> None:
        for item in self.tbc_list.selectedItems():
            self.tbc_list.takeItem(self.tbc_list.row(item))
        self._refresh_tool_state()

    def _browse_into(self, target, filter_text: str) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select file", "", filter_text)
        if selected:
            target.setText(selected)
            self._refresh_tool_state()

    def _validate_tool(self, tool: ToolSpec) -> list[str]:
        """Everything wrong with the form, in the order worth fixing it."""
        problems: list[str] = []
        sub = tool.subcommand

        if sub == "split":
            capture = self.input_edit.text().strip()
            if not capture:
                problems.append("Choose the capture to split.")
            elif not Path(capture).is_file():
                problems.append(f"Capture not found: {capture}")
            if not self.output_edit.text().strip():
                problems.append("Choose a folder for the pieces.")
            if self.parts_spin.value() < 2:
                problems.append("Splitting into one piece does nothing; use 2 or more.")
            if not self.length_auto_radio.isChecked():
                text = self.length_value_edit.text().strip()
                unit = "samples" if self.length_samples_radio.isChecked() else "seconds"
                try:
                    if float(text) <= 0:
                        raise ValueError
                except ValueError:
                    problems.append(f"Enter the capture length in {unit}.")
            elif (
                self.input_format_combo.currentText().strip().lower() == "flac"
                and capture
                and Path(capture).is_file()
                and self._capture_length(capture)[0] is None
            ):
                problems.append(
                    "Neither this capture nor a .json beside it records its "
                    "length, so it cannot be split automatically. Enter the "
                    "length in samples or seconds."
                )

        elif sub == "merge":
            paths = self._tbc_paths()
            if len(paths) < 2:
                problems.append("Add at least two .tbc files to join.")
            for path in paths:
                if not Path(path).is_file():
                    problems.append(f"Not found: {path}")
                elif not Path(path + ".json").is_file():
                    problems.append(f"No .tbc.json beside {Path(path).name}")
            locs = [self._tbc_first_loc(p) for p in paths]
            known = [loc for loc in locs if loc is not None]
            if len(known) == len(locs) and known != sorted(known):
                problems.append(
                    "These are not in tape order - joining them would interleave "
                    "the tape. Use 'Sort by tape position'."
                )
            manifest = self.manifest_edit.text().strip()
            if manifest:
                if not Path(manifest).is_file():
                    problems.append(f"Manifest not found: {manifest}")
                else:
                    count = self._manifest_count(manifest)
                    if count is not None and count != len(paths):
                        problems.append(
                            f"The manifest describes {count} piece(s) but "
                            f"{len(paths)} file(s) are listed; there must be one "
                            "for each."
                        )
            if not self.output_edit.text().strip():
                problems.append("Choose an output base name.")

        elif sub == "insert":
            into = self.into_edit.text().strip()
            piece = self.piece_edit.text().strip()
            for label, path in (("decode with the gap", into), ("piece to insert", piece)):
                if not path:
                    problems.append(f"Choose the {label}.")
                elif not Path(path).is_file():
                    problems.append(f"Not found: {path}")
                elif not Path(path + ".json").is_file():
                    problems.append(f"No .tbc.json beside {Path(path).name}")
            if into and piece and Path(into) == Path(piece):
                problems.append("The two files must be different.")
        return problems

    @staticmethod
    def _manifest_count(path: str):
        try:
            with open(path, encoding="utf-8") as handle:
                return len(json.load(handle).get("parts") or [])
        except Exception:
            return None

    def _capture_length(self, path: str):
        """Sample count and where it came from, or (None, None).

        The two capture tools record it in different places: MISRC writes RF
        Vorbis tags inside the FLAC, the DomesDay Duplicator writes a .json
        sidecar beside it.  Both are worth reading.
        """
        tagged = self._flac_length(path)
        if tagged:
            return tagged, "the capture's own tags"
        try:
            sidecar = Path(path).with_suffix(".json")
            with open(sidecar, encoding="utf-8") as handle:
                info = json.load(handle).get("captureInfo") or {}
            ms = float(info.get("durationInMilliseconds") or 0)
            rate = float(self.frequency_edit.text() or 40) * 1e6
            if ms > 0 and rate > 0:
                return int(ms / 1000.0 * rate), sidecar.name
        except Exception:
            pass
        return None, None

    @staticmethod
    def _flac_length(path: str):
        """Sample count from the capture's own RF tags, if it carries them."""
        try:
            with open(path, "rb") as handle:
                if handle.read(4) != b"fLaC":
                    return None
                while True:
                    head = handle.read(4)
                    if len(head) < 4:
                        return None
                    last = head[0] & 0x80
                    kind = head[0] & 0x7F
                    length = int.from_bytes(head[1:4], "big")
                    body = handle.read(length)
                    if kind == 4:
                        return _rf_total_samples(body)
                    if last:
                        return None
        except OSError:
            return None

    def _describe_selection(self, tool: ToolSpec) -> str:
        """The one-line summary shown under each tool's fields."""
        sub = tool.subcommand
        if sub == "split":
            capture = self.input_edit.text().strip()
            if capture and Path(capture).is_file():
                total, source = self._capture_length(capture)
                if total:
                    rate = float(self.frequency_edit.text() or 40) * 1e6
                    secs = total / rate if rate else 0
                    each = secs / max(1, self.parts_spin.value())
                    return (
                        f"Detected {total:,} samples ({secs / 60:.0f} min) from "
                        f"{source}. Each piece is about {each / 60:.0f} min."
                    )
                return (
                    "Neither this capture nor a .json beside it records its "
                    "length; state it below."
                )
            return ""
        if sub == "merge":
            paths = self._tbc_paths()
            if not paths:
                return ""
            known = [p for p in paths if self._tbc_first_loc(p) is not None]
            return f"{len(paths)} file(s) listed, {len(known)} with readable metadata."
        if sub == "insert":
            into = self.into_edit.text().strip()
            if into and Path(into + ".json").is_file():
                try:
                    with open(into + ".json", encoding="utf-8") as handle:
                        fields = json.load(handle).get("fields") or []
                    locs = [f["fileLoc"] for f in fields]
                    if len(locs) > 1:
                        gap = max(b - a for a, b in zip(locs, locs[1:]))
                        rate = float(self.frequency_edit.text() or 40) * 1e6
                        return f"Largest gap in the target: {gap / rate / 60:.1f} min."
                except Exception:
                    return ""
        return ""

    def _wire_events(self) -> None:
        self.tool_combo.currentIndexChanged.connect(self._refresh_tool_state)
        self.input_edit.textChanged.connect(self._on_input_changed)
        self.output_edit.textChanged.connect(self._refresh_tool_state)
        self.output_edit.textEdited.connect(self._on_output_edited)
        self.profile_combo.currentTextChanged.connect(self._refresh_tool_state)
        self.use_profile_file_check.toggled.connect(self._refresh_tool_state)
        self.profile_file_edit.textChanged.connect(self._refresh_tool_state)
        self.frequency_edit.textChanged.connect(self._refresh_tool_state)
        self.input_format_combo.currentIndexChanged.connect(self._refresh_tool_state)
        self.threads_spin.valueChanged.connect(self._refresh_tool_state)
        self.mt_distance_size_spin.valueChanged.connect(self._refresh_tool_state)
        self.include_chroma_check.toggled.connect(self._refresh_tool_state)
        self.include_metadata_check.toggled.connect(self._refresh_tool_state)
        self.ire0_adjust_check.toggled.connect(self._refresh_tool_state)
        self.overwrite_check.toggled.connect(self._refresh_tool_state)
        self.debug_check.toggled.connect(self._refresh_tool_state)
        self.extra_args_edit.textChanged.connect(self._refresh_tool_state)

        self.refresh_profiles_button.clicked.connect(self._refresh_profiles)
        self.input_browse_button.clicked.connect(self._browse_input_file)
        self.output_browse_button.clicked.connect(self._browse_output_path)
        self.profile_file_browse_button.clicked.connect(self._browse_profile_file)
        self.microarch_combo.currentIndexChanged.connect(self._refresh_tool_state)
        self.microarch_locate_button.clicked.connect(self._locate_level_binary)
        self.tbc_add_button.clicked.connect(self._add_tbc_files)
        self.tbc_up_button.clicked.connect(lambda: self._move_tbc(-1))
        self.tbc_down_button.clicked.connect(lambda: self._move_tbc(1))
        self.tbc_remove_button.clicked.connect(self._remove_tbc)
        self.tbc_sort_button.clicked.connect(self._sort_tbc_by_position)
        self.manifest_browse_button.clicked.connect(
            lambda: self._browse_into(self.manifest_edit, "Manifest (*.json)")
        )
        self.into_browse_button.clicked.connect(
            lambda: self._browse_into(self.into_edit, "TBC files (*.tbc)")
        )
        self.piece_browse_button.clicked.connect(
            lambda: self._browse_into(self.piece_edit, "TBC files (*.tbc)")
        )
        for widget in (self.parts_spin, self.overlap_spin):
            widget.valueChanged.connect(self._refresh_tool_state)
        for widget in (self.length_value_edit, self.manifest_edit,
                       self.into_edit, self.piece_edit):
            widget.textChanged.connect(self._refresh_tool_state)
        for button in (self.length_auto_radio, self.length_samples_radio,
                       self.length_seconds_radio, self.dry_run_check):
            button.toggled.connect(self._refresh_tool_state)
        self.launch_button.clicked.connect(self._launch_selected_tool)
        self.launch_tbc_tools_button.clicked.connect(self._launch_tbc_tools)
        self.close_button.clicked.connect(self.close)

    def _selected_tool(self) -> ToolSpec:
        return self._tools[self.tool_combo.currentIndex()]

    def _is_decode_tool(self) -> bool:
        return self._selected_tool().subcommand == "decode"

    def _refresh_profiles(self) -> None:
        current = self.profile_combo.currentText().strip()
        profiles = load_profiles()
        if not profiles:
            if self.profile_combo.count() == 0:
                self.profile_combo.addItem(DEFAULT_PROFILE)
            if not self.profile_combo.currentText().strip():
                self.profile_combo.setCurrentText(DEFAULT_PROFILE)
            self._refresh_tool_state()
            return

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(profiles)
        if current and current in profiles:
            self.profile_combo.setCurrentText(current)
        elif DEFAULT_PROFILE in profiles:
            self.profile_combo.setCurrentText(DEFAULT_PROFILE)
        elif profiles:
            self.profile_combo.setCurrentIndex(0)
        self.profile_combo.blockSignals(False)
        self._refresh_tool_state()

    def _refresh_tool_state(self) -> None:
        tool = self._selected_tool()
        decode_selected = tool.subcommand == "decode"
        profile_file_selected = decode_selected and self.use_profile_file_check.isChecked()

        for widget in (
            self.input_edit,
            self.input_browse_button,
            self.output_edit,
            self.output_browse_button,
            self.frequency_edit,
            self.input_format_combo,
            self.threads_spin,
            self.mt_distance_size_spin,
            self.include_chroma_check,
            self.include_metadata_check,
            self.ire0_adjust_check,
            self.overwrite_check,
            self.debug_check,
            self.use_profile_file_check,
            self.refresh_profiles_button,
        ):
            widget.setEnabled(decode_selected)

        self.profile_combo.setEnabled(decode_selected and not profile_file_selected)
        self.profile_file_edit.setEnabled(profile_file_selected)
        self.profile_file_browse_button.setEnabled(profile_file_selected)

        sub = tool.subcommand
        self._set_row_visible(self.split_rows, sub == "split")
        self._set_row_visible(self.merge_rows, sub == "merge")
        self._set_row_visible(self.insert_rows, sub == "insert")

        # The new tools drive the shared fields too, so re-enable the ones each
        # of them actually uses instead of leaving everything off.
        for widget in (self.input_edit, self.input_browse_button):
            widget.setEnabled(sub in ("decode", "split"))
        for widget in (self.output_edit, self.output_browse_button):
            widget.setEnabled(sub in ("decode", "split", "merge"))
        for widget in (self.frequency_edit, self.overwrite_check):
            widget.setEnabled(sub in ("decode", "split", "merge", "insert"))
        self.input_format_combo.setEnabled(sub in ("decode", "split"))
        # Only meaningful when a length is actually being typed in.
        self.length_value_edit.setEnabled(not self.length_auto_radio.isChecked())

        for label, owner in (
            (self.split_info_label, "split"),
            (self.merge_info_label, "merge"),
            (self.insert_info_label, "insert"),
        ):
            label.setText(self._describe_selection(tool) if sub == owner else "")

        problems = self._validate_tool(tool) if sub in ("split", "merge", "insert") else []
        self.problem_label.setText(
            "" if not problems else "Fix before launching:\n- " + "\n- ".join(problems)
        )
        self.problem_label.setVisible(bool(problems))
        self.launch_button.setEnabled(not problems)

        self.command_preview.setText(self._terminal_preview_command(tool))
        self.note_label.setText(tool.notes)

    def _infer_default_output_base(self, input_path: str) -> str:
        if not input_path.strip():
            return ""
        path = Path(input_path.strip()).expanduser()
        parent = path.parent if path.parent.as_posix() != "." else Path.cwd()
        stem = path.stem or path.name
        return str(parent / stem)

    def _on_input_changed(self, value: str) -> None:
        if not self._output_manually_set:
            self.output_edit.setText(self._infer_default_output_base(value))
        self._refresh_tool_state()

    def _on_output_edited(self, value: str) -> None:
        self._output_manually_set = bool(value.strip())

    def _derive_output_paths(self, output_base: str) -> tuple[str, str, str]:
        output_base = output_base.strip()
        if not output_base:
            return "", "", ""
        if output_base.lower().endswith(".tbc"):
            luma = output_base
            base = output_base[: -len(".tbc")]
        else:
            luma = f"{output_base}.tbc"
            base = output_base
        chroma = f"{base}_chroma.tbc"
        metadata = f"{base}.tbc.json"
        return luma, chroma, metadata

    def _build_decode_args(self, *, strict: bool) -> list[str]:
        args = ["decode"]
        input_path = self.input_edit.text().strip()
        output_base = self.output_edit.text().strip()
        luma_out, chroma_out, metadata_out = self._derive_output_paths(output_base)

        if strict:
            if not input_path:
                raise RuntimeError("Select an input file.")
            if not output_base:
                raise RuntimeError("Select an output base path.")

        if luma_out:
            args += ["--luma-out", luma_out]
        if chroma_out and self.include_chroma_check.isChecked():
            args += ["--chroma-out", chroma_out]
        if metadata_out and self.include_metadata_check.isChecked():
            args += ["--metadata-out", metadata_out]

        frequency = self.frequency_edit.text().strip()
        if frequency:
            args += ["--frequency", frequency]

        input_format = self.input_format_combo.currentText().strip()
        if input_format:
            args += ["--input-format", input_format]

        threads = self.threads_spin.value()
        if threads > 0:
            args += ["--mt-threads", str(threads)]
            args += ["--mt-distance-size", str(self.mt_distance_size_spin.value())]

        if self.overwrite_check.isChecked():
            args.append("--overwrite")
        if self.ire0_adjust_check.isChecked():
            args.append("--ire0-adjust")
        if self.debug_check.isChecked():
            args.append("--debug")

        if self.use_profile_file_check.isChecked():
            profile_file = self.profile_file_edit.text().strip()
            if strict and not profile_file:
                raise RuntimeError("Select a profile JSON file or disable profile file mode.")
            if profile_file:
                args += ["--profile-file", profile_file]
        else:
            profile = self.profile_combo.currentText().strip()
            if strict and not profile:
                raise RuntimeError("Select a profile.")
            if profile:
                args += ["--profile", profile]

        extra = self.extra_args_edit.text().strip()
        if extra:
            extra_args = _split_user_args(extra, strict=strict)
            if _arg_writes_raw_output_to_stdout(extra_args):
                raise RuntimeError(
                    "Tape Decode Full cannot use --luma-out - / --chroma-out - in Extra arguments. "
                    "Use Output base file paths in the form, or run a manual shell pipeline outside launcher."
                )
            args += extra_args

        if input_path:
            args.append(input_path)

        return args

    def _selected_microarch_level(self) -> str:
        """Return the normalized level for the currently-selected combo entry,
        or MICROARCH_AUTO for index 0."""
        index = self.microarch_combo.currentIndex()
        if 0 <= index < len(MICROARCH_UI_OPTIONS):
            return normalize_microarch_level(MICROARCH_UI_OPTIONS[index][1])
        return MICROARCH_AUTO

    def _build_command(self, tool: ToolSpec, *, strict: bool) -> list[str]:
        level = self._selected_microarch_level()
        if tool.subcommand == "decode":
            return build_tape_decode_command(
                self._build_decode_args(strict=strict), level=level
            )

        extra = self.extra_args_edit.text().strip()
        extra_args = _split_user_args(extra, strict=strict) if extra else []
        return build_tape_decode_command(
            [tool.subcommand] + self._build_subcommand_args(tool) + extra_args,
            level=level,
        )

    def _build_subcommand_args(self, tool: ToolSpec) -> list[str]:
        """Arguments the guided fields contribute for the new subcommands."""
        sub = tool.subcommand
        if sub == "split":
            args = [self.input_edit.text().strip(), self.output_edit.text().strip()]
            args += ["--parts", str(self.parts_spin.value())]
            args += ["--overlap", "%g" % self.overlap_spin.value()]
            args += ["--input-format", self.input_format_combo.currentText().strip()]
            frequency = self.frequency_edit.text().strip()
            if frequency:
                args += ["--frequency", frequency]
            value = self.length_value_edit.text().strip()
            if value and self.length_samples_radio.isChecked():
                args += ["--total-samples", value]
            elif value and self.length_seconds_radio.isChecked():
                args += ["--duration", value]
            return [a for a in args if a]
        if sub == "merge":
            args = self._tbc_paths()
            args += ["-o", self.output_edit.text().strip()]
            manifest = self.manifest_edit.text().strip()
            if manifest:
                args += ["-m", manifest]
            if self.overwrite_check.isChecked():
                args.append("--overwrite")
            return [a for a in args if a]
        if sub == "insert":
            args = ["--into", self.into_edit.text().strip(),
                    "--insert", self.piece_edit.text().strip()]
            frequency = self.frequency_edit.text().strip()
            if frequency:
                args += ["--frequency", frequency]
            args += ["--fps", "%.6f" % self.fps_combo.currentData()]
            if self.dry_run_check.isChecked():
                args.append("--dry-run")
            return [a for a in args if a]
        return []

    def _terminal_preview_command(self, tool: ToolSpec) -> str:
        level = self._selected_microarch_level()
        try:
            command = self._build_command(tool, strict=False)
            joined = _shell_join_platform(command)
        except Exception as exc:
            preview = f"[preview unavailable] {exc}"
        else:
            suffix_parts = []
            if level:
                try:
                    resolved = resolve_tape_decode_prefix(level=level)
                    if resolved:
                        suffix_parts.append(f"bin: {resolved[0]}")
                except FileNotFoundError:
                    suffix_parts.append(
                        f"binary for {level} not built"
                    )
            if level:
                suffix_parts.append(
                    f"RUSTFLAGS=-C target-cpu={microarch_target_cpu(level)}"
                )
                target_dir = microarch_target_dir(level)
                if target_dir:
                    suffix_parts.append(f"CARGO_TARGET_DIR={target_dir}")
            preview = joined
            if suffix_parts:
                preview = f"{joined}    [{'  '.join(suffix_parts)}]"
        return preview

    def _effective_working_directory(self) -> Path:
        input_path = self.input_edit.text().strip()
        if input_path:
            candidate = Path(input_path).expanduser()
            parent = candidate.parent
            if parent.is_dir():
                return parent.resolve()
        return Path(os.getcwd()).resolve()

    def _browse_input_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select input RF file",
            self.input_edit.text().strip() or str(self._effective_working_directory()),
            "RF captures (*.ldf *.flac *.lds *.r30 *.u8 *.s8 *.s16le *.u16le *.f32le "
            "*.raw *.bin);;FLAC captures (*.ldf *.flac);;All files (*)",
        )
        if selected:
            self.input_edit.setText(selected)

    def _browse_output_path(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Select output base name",
            self.output_edit.text().strip() or str(self._effective_working_directory()),
        )
        if selected:
            self._output_manually_set = True
            self.output_edit.setText(selected)

    def _browse_profile_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select profile JSON file",
            self.profile_file_edit.text().strip() or str(self._effective_working_directory()),
            "JSON files (*.json);;All files (*)",
        )
        if selected:
            self.profile_file_edit.setText(selected)

    def _output_to_tbc_candidate(self, output_value: str) -> Optional[Path]:
        if not output_value.strip():
            return None

        output_path = Path(output_value.strip()).expanduser()
        if not output_path.is_absolute():
            output_path = self._effective_working_directory() / output_path
        output_path = output_path.resolve(strict=False)

        if output_path.suffix.lower() in {".tbc", ".lds"}:
            return output_path
        return Path(str(output_path) + ".tbc")

    def _candidate_tbc_path(self) -> Optional[Path]:
        tbc_path = self._output_to_tbc_candidate(self.output_edit.text())
        if tbc_path is None:
            return None
        if tbc_path.exists():
            return tbc_path
        if self._is_decode_tool():
            return tbc_path
        return None

    def _candidate_tbc_tool_names(self) -> list[str]:
        if os.name == "nt":
            return ["ld-analyse.exe", "tbc-analyse.exe", "tbc-tools.exe"]
        if sys.platform == "darwin":
            return ["ld-analyse", "tbc-analyse", "tbc-tools"]
        return [
            "ld-analyse",
            "tbc-analyse",
            "tbc-tools",
            "tbc-tools.AppImage",
            "tbc-tools.appimage",
            "tbc-tools-x86_64.AppImage",
            "tbc-tools-x86_64.appimage",
            "tbc-tools-aarch64.AppImage",
            "tbc-tools-aarch64.appimage",
        ]

    def _existing_parent_dir(self, raw_path: str) -> Optional[Path]:
        if not raw_path.strip():
            return None
        candidate = Path(raw_path.strip()).expanduser()
        parent = candidate.parent
        if parent.is_dir():
            return parent.resolve()
        return None

    def _candidate_tbc_search_roots(self) -> list[Path]:
        roots: list[Path] = [self._effective_working_directory()]
        input_parent = self._existing_parent_dir(self.input_edit.text())
        output_parent = self._existing_parent_dir(self.output_edit.text())
        if input_parent is not None:
            roots.append(input_parent)
        if output_parent is not None:
            roots.append(output_parent)

        if os.name == "nt":
            roots.extend(
                [
                    Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
                    Path.home() / "AppData" / "Local" / "Programs",
                ]
            )
        elif sys.platform == "darwin":
            roots.extend(
                [
                    Path("/Applications"),
                    Path.home() / "Applications",
                    Path("/opt/homebrew/bin"),
                    Path("/usr/local/bin"),
                    Path("/usr/bin"),
                ]
            )
        else:
            roots.extend(
                [
                    Path("/usr/local/bin"),
                    Path("/usr/bin"),
                    Path("/opt"),
                    Path.home() / "Applications",
                    Path.home() / "bin",
                ]
            )

        deduped: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(root)
        return deduped

    def _find_tbc_tools_executable(self) -> Optional[Path]:
        names = self._candidate_tbc_tool_names()

        for name in names:
            on_path = shutil.which(name)
            if on_path:
                return Path(on_path)

        for root in self._candidate_tbc_search_roots():
            for name in names:
                direct = root / name
                if direct.is_file():
                    return direct

                in_tbc_tools_dir = root / "tbc-tools" / name
                if in_tbc_tools_dir.is_file():
                    return in_tbc_tools_dir

            if sys.platform == "darwin":
                mac_candidates = [
                    root / "tbc-tools.app" / "Contents" / "MacOS" / "ld-analyse",
                    root / "tbc-tools.app" / "Contents" / "MacOS" / "tbc-tools",
                    root / "ld-analyse.app" / "Contents" / "MacOS" / "ld-analyse",
                ]
                for candidate in mac_candidates:
                    if candidate.is_file():
                        return candidate

        return None

    def _macos_app_bundle_for_binary(self, executable: Path) -> Optional[Path]:
        if sys.platform != "darwin":
            return None
        for parent in executable.resolve(strict=False).parents:
            if parent.suffix.lower() == ".app":
                return parent
        return None

    def _locate_level_binary(self) -> None:
        level = self._selected_microarch_level()
        try:
            prefix = resolve_tape_decode_prefix(level=level)
        except FileNotFoundError as exc:
            QMessageBox.information(
                self,
                "Binary location",
                f"No tape-decode binary found for level {level or 'Auto'}.\n\n"
                f"{exc}\n\n"
                "Use the x86-64 microarch level selector to pick Auto, or build the binary for the chosen level outside the launcher.",
            )
            return
        path = prefix[0] if prefix else ""
        if not path:
            QMessageBox.information(self, "Binary location", "No binary resolved.")
            return
        QMessageBox.information(
            self,
            "Binary location",
            f"Level: {level or 'Auto'}\nBinary: {path}",
        )

    def _launch_tbc_tools(self) -> None:
        executable = self._find_tbc_tools_executable()
        if executable is None:
            QMessageBox.critical(
                self,
                "tbc-tools not found",
                "Could not find tbc-tools / ld-analyse in PATH or standard install locations.",
            )
            return

        tbc_candidate = self._candidate_tbc_path()
        app_bundle = self._macos_app_bundle_for_binary(executable)
        if app_bundle is not None:
            command = ["open", "-a", str(app_bundle)]
            if tbc_candidate is not None:
                command += ["--args", str(tbc_candidate)]
        else:
            command = [str(executable)]
            if tbc_candidate is not None:
                command.append(str(tbc_candidate))

        try:
            subprocess.Popen(command, cwd=str(self._effective_working_directory()))
        except Exception as exc:
            QMessageBox.critical(self, "Launch failed", str(exc))

    def _selected_microarch_env(self) -> Optional[dict[str, str]]:
        """Return env exports for the selected level, or None on Auto."""
        level = self._selected_microarch_level()
        if not level:
            return None
        env: dict[str, str] = {
            "RUSTFLAGS": f"-C target-cpu={microarch_target_cpu(level)}",
        }
        target_dir = microarch_target_dir(level)
        if target_dir:
            env["CARGO_TARGET_DIR"] = target_dir
        return env

    def _launch_selected_tool(self) -> None:
        tool = self._selected_tool()
        working_directory = self._effective_working_directory()
        if not working_directory.is_dir():
            QMessageBox.critical(
                self,
                "Invalid working directory",
                f"Directory does not exist:\n{working_directory}",
            )
            return

        try:
            command = self._build_command(tool, strict=True)
        except Exception as exc:
            QMessageBox.critical(self, "Invalid arguments", str(exc))
            return

        try:
            _open_terminal(
                command,
                working_directory,
                env_exports=self._selected_microarch_env(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Launch failed", str(exc))


def _apply_fusion_dark_mode(app: QApplication) -> None:
    fusion_style = QStyleFactory.create("Fusion")
    if fusion_style is not None:
        app.setStyle(fusion_style)
    else:
        app.setStyle("Fusion")

    role = QPalette.ColorRole if hasattr(QPalette, "ColorRole") else QPalette
    group = QPalette.ColorGroup if hasattr(QPalette, "ColorGroup") else QPalette

    palette = QPalette()
    palette.setColor(role.Window, QColor(53, 53, 53))
    palette.setColor(role.WindowText, QColor(225, 225, 225))
    palette.setColor(role.Base, QColor(35, 35, 35))
    palette.setColor(role.AlternateBase, QColor(53, 53, 53))
    palette.setColor(role.ToolTipBase, QColor(30, 30, 30))
    palette.setColor(role.ToolTipText, QColor(225, 225, 225))
    palette.setColor(role.Text, QColor(225, 225, 225))
    palette.setColor(role.Button, QColor(53, 53, 53))
    palette.setColor(role.ButtonText, QColor(225, 225, 225))
    palette.setColor(role.BrightText, QColor(255, 80, 80))
    palette.setColor(role.Highlight, QColor(42, 130, 218))
    palette.setColor(role.HighlightedText, QColor(20, 20, 20))
    palette.setColor(group.Disabled, role.Text, QColor(120, 120, 120))
    palette.setColor(group.Disabled, role.ButtonText, QColor(120, 120, 120))
    palette.setColor(group.Disabled, role.WindowText, QColor(120, 120, 120))
    app.setPalette(palette)
    app.setStyleSheet(
        "QToolTip { color: #e1e1e1; background-color: #2b2b2b; border: 1px solid #4a4a4a; }"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tape Decode Full (Qt) for running tape-decode-full commands"
    )
    parser.parse_args(argv)

    app = QApplication(sys.argv)
    _apply_fusion_dark_mode(app)
    window = DecodeLauncherWindow()

    # Set window + app icon for taskbar/dock/titlebar on all platforms.
    # This is especially important for Linux AppImage/taskbar integration.
    icon_path = _resolve_icon_path()
    if icon_path:
        try:
            ico = QIcon(str(icon_path))
            app.setWindowIcon(ico)
            window.setWindowIcon(ico)
        except Exception:
            pass

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
