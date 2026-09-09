"""Project workspace: a directory that owns one acquisition campaign's settings AND its data.

Modeled on CAEN CoMPASS, deliberately: a project is a FOLDER, not a settings file. Opening one
restores the whole instrument state (every subsystem's register values, the energy calibration, the
file-naming convention) and redirects every recorded artifact into that folder's own subdirectories,
so a campaign is self-describing after the fact -- the settings that produced a dataset sit next to
the dataset rather than in whatever the GUI happened to be showing that afternoon.

    <project>/
        settings.json     everything below, one JSON document
        RAW/              scope_traces CSVs (raw ADC frames)
        LIST/             fci_live CSVs (one row per paired event: the list-mode data)
        SPECTRA/          .spe exports

The three data directories mirror CoMPASS's own RAW/FILTERED/UNFILTERED split in intent (separate
folders per artifact KIND, so a glob never has to discriminate by filename), not in name: this
instrument produces different kinds. RAW is raw traces here rather than raw list data, because the
oscilloscope capture is the closest thing this design has to an unprocessed record.

Only WRITABLE device fields are stored. Read-only telemetry (BLR's live baseline and gate_open) is
excluded because writing it back is meaningless -- the device would answer `!XX 1` -- and storing it
would suggest a project pins a value it cannot pin. Which fields those are is decided by
SubsystemPanel.get_values(), not here: the field lists live in ui/config_panel.py and this module
deliberately does not duplicate them (the same one-place-per-fact rule that section 8d of the
project log exists to remember).

Forward compatibility: the loaded JSON document is kept whole and written back whole, so a key this
version does not understand survives a load/save round trip through an older GUI instead of being
silently dropped.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_FORMAT = "fci-fpga-project"
"""Written into every settings.json and checked on load. A directory containing some other tool's
settings.json is a plausible mistake to make with a folder-picker, and opening one would otherwise
present its contents as this instrument's configuration."""

PROJECT_VERSION = 1

SETTINGS_FILENAME = "settings.json"
RAW_DIRNAME = "RAW"
LIST_DIRNAME = "LIST"
SPECTRA_DIRNAME = "SPECTRA"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ProjectError(Exception):
    """Raised for anything the user needs to be told about by name: the directory already exists,
    settings.json is absent or unparseable, the format tag does not match."""


class Project:
    """One opened project. Holds the parsed settings document and knows where its data goes.

    Construct through create() or load(), never directly -- both guarantee the directory layout
    exists before any caller can hand a path to a CSV logger.
    """

    def __init__(self, path: Path, doc: dict[str, Any]):
        self.path = path
        self._doc = doc

    # ------------------------------------------------------------------------------ construction

    @classmethod
    def create(cls, parent_dir: Path, name: str, description: str = "") -> "Project":
        """Creates <parent_dir>/<name>/ with the full layout and an empty settings document.

        Refuses an existing directory rather than adopting it: adopting one would either overwrite
        an unrelated folder's settings.json or silently reopen a project the user believes they are
        creating fresh, and the two are indistinguishable from here.
        """
        path = parent_dir / name
        if path.exists():
            raise ProjectError(f"{path} already exists -- pick another name, or use Open Project.")
        now = time.strftime(_TIME_FORMAT)
        doc: dict[str, Any] = {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "name": name,
            "description": description,
            "created": now,
            "modified": now,
            "device": {},
            "acquisition": {},
            "spectrum": {},
        }
        project = cls(path, doc)
        project._ensure_layout()
        project.save()
        logger.info(f"Created project {path}")
        return project

    @classmethod
    def load(cls, path: Path) -> "Project":
        settings_path = path / SETTINGS_FILENAME
        if not settings_path.is_file():
            raise ProjectError(f"{path} is not a project: no {SETTINGS_FILENAME} in it.")
        try:
            doc = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ProjectError(f"Could not read {settings_path}: {e}") from e
        if not isinstance(doc, dict) or doc.get("format") != PROJECT_FORMAT:
            raise ProjectError(
                f"{settings_path} is not an FCI-FPGA project file "
                f"(expected \"format\": \"{PROJECT_FORMAT}\")."
            )
        version = doc.get("version", 0)
        if version > PROJECT_VERSION:
            # Not fatal: unknown keys round-trip intact (see the module docstring), so a newer
            # project stays usable for everything this version does understand.
            logger.warning(f"{settings_path} was written by a newer version "
                           f"(v{version} > v{PROJECT_VERSION}); unknown settings are preserved but "
                           "not applied.")
        project = cls(path, doc)
        project._ensure_layout()
        logger.info(f"Opened project {path}")
        return project

    def _ensure_layout(self) -> None:
        """Creates the directory tree. Called on load() too, not only create(): a project whose
        SPECTRA/ was deleted (or which was committed to git without its empty data folders) must
        still be usable, and recreating an empty directory costs nothing."""
        for d in (self.path, self.raw_dir, self.list_dir, self.spectra_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------------- properties

    @property
    def name(self) -> str:
        return str(self._doc.get("name") or self.path.name)

    @property
    def description(self) -> str:
        return str(self._doc.get("description", ""))

    @description.setter
    def description(self, value: str) -> None:
        self._doc["description"] = value

    @property
    def settings_path(self) -> Path:
        return self.path / SETTINGS_FILENAME

    @property
    def raw_dir(self) -> Path:
        return self.path / RAW_DIRNAME

    @property
    def list_dir(self) -> Path:
        return self.path / LIST_DIRNAME

    @property
    def spectra_dir(self) -> Path:
        return self.path / SPECTRA_DIRNAME

    # ---------------------------------------------------------------------------------- sections

    def _section(self, key: str) -> dict[str, Any]:
        section = self._doc.get(key)
        if not isinstance(section, dict):
            section = {}
            self._doc[key] = section
        return section

    @property
    def device(self) -> dict[str, dict[str, Any]]:
        """Per-subsystem writable register values, keyed by SubsystemPanel.key ("trigger", "psd",
        "fci", "blr", "vga", "shaper"). A subsystem absent from this dict simply was not captured
        (an older project, or one saved while that panel had never been read) -- loading leaves that
        panel's controls untouched rather than resetting them to zeros."""
        return self._section("device")

    @property
    def acquisition(self) -> dict[str, Any]:
        """File-naming state from the File Management tab: `file_prefix`, `autoincrement`. The
        output DIRECTORY is deliberately not stored -- it is the project's own LIST/RAW, derived
        from wherever the project folder currently sits, so a project stays valid after being moved
        or copied to another machine."""
        return self._section("acquisition")

    @property
    def spectrum(self) -> dict[str, Any]:
        """Spectrum tab state: `calibration` [c0, c1, c2], `display_channels`, `log_scale`. The
        accumulated counts themselves are NOT part of a project -- a spectrum is data, and belongs
        in SPECTRA/ as an .spe export."""
        return self._section("spectrum")

    # ------------------------------------------------------------------------------------- saving

    def save(self) -> None:
        self._doc["format"] = PROJECT_FORMAT
        self._doc["version"] = PROJECT_VERSION
        self._doc["modified"] = time.strftime(_TIME_FORMAT)
        self._ensure_layout()
        # Written through a temporary file in the same directory and then renamed: os.replace is
        # atomic within a filesystem, so an interrupted save (or a crash mid-write) leaves the
        # previous settings.json intact instead of a truncated one that load() would then reject.
        tmp = self.settings_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._doc, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.settings_path)
        except OSError as e:
            raise ProjectError(f"Could not write {self.settings_path}: {e}") from e
        logger.info(f"Saved project settings to {self.settings_path}")


# ------------------------------------------------------------------------------------- app state

def read_last_project(state_path: Path) -> Path | None:
    """The project to reopen at startup. Deliberately stored OUTSIDE any project (in the user's
    config directory): it is a property of this installation, not of a campaign, and a project
    folder copied to another machine must not carry a stale "this was open" flag with it."""
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # absent or corrupt: start with no project, which is a valid state
    last = doc.get("last_project")
    return Path(last) if last else None


def write_last_project(state_path: Path, project_path: Path | None) -> None:
    """None clears it -- Close Project must not leave the next launch reopening what the user just
    closed."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"last_project": str(project_path) if project_path is not None else None}
        state_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        # Never fatal: failing to remember the last project is a lost convenience, not a lost
        # measurement, and the application must still open.
        logger.warning(f"Could not write {state_path}: {e}")
