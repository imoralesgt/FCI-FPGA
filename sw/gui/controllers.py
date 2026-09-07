"""AppController: wires MainWindow's UI signals to AcquisitionWorker requests, and worker signals
back to widget updates. Owns the connection lifecycle (including a simple reconnect-on-unexpected-
drop state machine, mirroring the reference GUI's own), the CSV logger, and the open project.

Projects (project.py) are owned here rather than by MainWindow because opening one touches
everything this class already coordinates: it repopulates every subsystem panel, optionally writes
those values to the device, and redirects where recordings are written. MainWindow only provides the
menu actions and the panels.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

import config
from acquisition_worker import AcquisitionWorker, RemoteFciClient
from csv_logger import CsvLogger, TraceCsvLogger
from fci_api import FciError, list_matching_ports
from project import Project, ProjectError, read_last_project, write_last_project
from ui.calibration_wizard import CalibrationWizard
from ui.fom_wizard import FomWizard
from ui.project_dialog import NewProjectDialog

logger = logging.getLogger(__name__)


class AppController(QObject):
    def __init__(self, view):
        super().__init__()
        self.view = view

        self.config_client: RemoteFciClient | None = None
        """A RemoteFciClient, not a plain fci_api.FciClient -- the transport now lives only
        inside the reader process (AcquisitionWorker), reached over IPC. See
        acquisition_worker.py's module docstring."""
        self.worker: AcquisitionWorker | None = None
        self.csv_logger: CsvLogger | None = None
        self.scope_csv_logger: TraceCsvLogger | None = None
        self.project: Project | None = None
        """The open project, or None. While one is open it owns where recordings go (_list_dir()/
        _raw_dir()) and holds the settings restored into every subsystem panel."""

        self.is_connected = False
        self._expect_connection = False
        self._reconnect_count = 0

        self._live_acq_running = False
        self._scope_running = False
        """Tracked here (not read off the views) so on_record_toggled() can tell whether
        re-checking Record mid-acquisition should resume recording immediately -- see there."""

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._attempt_reconnect)

        self._connect_signals()
        self.scan_ports()
        self.view.txt_file_prefix.setText(time.strftime("%Y%m%d"))
        self._restore_last_project()  # may open a project and unlock the rest of the window
        if self.project is None:
            self.view.set_project_open(False)

        logger.info("AppController initialized.")

    def _connect_signals(self) -> None:
        self.view.btn_refresh_ports.clicked.connect(self.scan_ports)
        self.view.btn_connect.clicked.connect(self.toggle_connection)
        self.view.chk_record.toggled.connect(self.on_record_toggled)
        self.view.txt_file_prefix.textChanged.connect(self._update_filename_preview)
        self.view.chk_autoincrement.toggled.connect(self._update_filename_preview)
        self.view.live_view.confirm_start = self._confirm_and_maybe_record
        self.view.scope_view.confirm_start = self._confirm_and_maybe_record
        self.view.live_view.start_clicked.connect(self.start_acquisition)
        self.view.live_view.stop_clicked.connect(self.stop_acquisition)
        self.view.scope_view.start_clicked.connect(self.scope_start)
        self.view.scope_view.stop_clicked.connect(self.scope_stop)
        self.view.scope_view.single_clicked.connect(self.scope_single)
        self.view.scope_view.calibrate_clicked.connect(self.open_calibration_wizard)
        self.view.live_view.fom_wizard_clicked.connect(self.open_fom_wizard)
        self.view.histogram_view.run_clicked.connect(self._on_spectrum_run)
        self.view.histogram_view.stop_clicked.connect(self._on_spectrum_stop)
        self.view.act_new_project.triggered.connect(self.new_project)
        self.view.act_open_project.triggered.connect(self.open_project)
        self.view.act_save_project.triggered.connect(self.save_project)
        self.view.act_save_project_as.triggered.connect(self.save_project_as)
        self.view.act_close_project.triggered.connect(self.close_project)

    # ---------------------------------------------------------------------------------- projects

    def _restore_last_project(self) -> None:
        """Reopens whatever project was open when the application last closed, silently. A missing
        or unreadable one is not an error worth a dialog at startup -- the folder may simply have
        been moved or deleted since -- so it is logged, the pointer cleared, and the GUI starts with
        no project, which is a fully usable state."""
        last = read_last_project(config.APP_STATE_PATH)
        if last is None:
            return
        try:
            project = Project.load(last)
        except ProjectError as e:
            logger.warning(f"Could not reopen last project {last}: {e}")
            write_last_project(config.APP_STATE_PATH, None)
            return
        self._set_project(project)

    def new_project(self) -> None:
        if not self._can_switch_project():
            return
        dlg = NewProjectDialog(config.DEFAULT_PROJECT_DIR, self.view)
        if dlg.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        try:
            project = Project.create(dlg.project_location(), dlg.project_name(),
                                      dlg.project_description())
        except (ProjectError, OSError) as e:
            QMessageBox.warning(self.view, "Could Not Create Project", str(e))
            return
        # A new project starts from what is on screen right now rather than from defaults: the user
        # has usually just tuned the instrument and then decided to record the campaign, and
        # blanking that back to power-on values would throw the tuning away.
        self._capture_ui_into_project(project)
        try:
            project.save()
        except ProjectError as e:
            QMessageBox.warning(self.view, "Could Not Save Project", str(e))
            return
        self._set_project(project)

    def open_project(self) -> None:
        if not self._can_switch_project():
            return
        chosen = QFileDialog.getExistingDirectory(
            self.view, "Open Project Folder",
            str(self.project.path.parent if self.project else config.DEFAULT_PROJECT_DIR),
        )
        if not chosen:
            return
        try:
            project = Project.load(Path(chosen))
        except ProjectError as e:
            QMessageBox.warning(self.view, "Could Not Open Project", str(e))
            return
        self._set_project(project)
        self._load_project_into_ui(project)
        self._offer_apply_to_device()

    def save_project(self) -> None:
        if self.project is None:
            return
        self._capture_ui_into_project(self.project)
        try:
            self.project.save()
        except ProjectError as e:
            QMessageBox.warning(self.view, "Could Not Save Project", str(e))
            return
        self.view.lbl_status.setText(f"\U0001F7E2 Saved project '{self.project.name}'"
                                      if self.is_connected else
                                      f"Saved project '{self.project.name}'")

    def save_project_as(self) -> None:
        """Creates a NEW project folder holding the current settings, and switches to it. Data
        already recorded stays in the old project -- copying it would silently duplicate potentially
        gigabytes of traces, and a campaign's data belongs with the settings that produced it."""
        if self.project is None or not self._can_switch_project():
            return
        dlg = NewProjectDialog(self.project.path.parent, self.view)
        if dlg.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        try:
            project = Project.create(dlg.project_location(), dlg.project_name(),
                                      dlg.project_description())
            self._capture_ui_into_project(project)
            project.save()
        except (ProjectError, OSError) as e:
            QMessageBox.warning(self.view, "Could Not Create Project", str(e))
            return
        self._set_project(project)

    def close_project(self) -> None:
        if self.project is None or not self._can_switch_project():
            return
        if not self._offer_save_before_leaving():
            return
        logger.info(f"Closed project {self.project.path}")
        self._set_project(None)

    def _can_switch_project(self) -> bool:
        """Blocks every project change while a recording session is open. A project owns where its
        files go, so switching mid-recording would split one dataset across two projects with no
        record of the break -- worse than making the user stop first."""
        if self.csv_logger is None:
            return True
        QMessageBox.information(
            self.view, "Recording in Progress",
            "Stop recording (uncheck Record) before creating, opening or closing a project -- the "
            "project decides where recorded files are written.",
        )
        return False

    def _offer_save_before_leaving(self, allow_cancel: bool = True) -> bool:
        """Asked when a project is about to stop being the open one (Close Project, application
        exit). Returns False only if the user cancelled the whole action. Deliberately a question
        rather than a silent write: settings.json is what documents an existing dataset, and a
        project opened only to look at it must not be rewritten just because it was opened.

        allow_cancel=False on application exit: cleanup() runs from closeEvent after the window has
        already been told to close, so there is no longer a close to cancel -- offering the button
        would imply the shutdown could be called off."""
        if self.project is None:
            return True
        buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        if allow_cancel:
            buttons |= QMessageBox.StandardButton.Cancel
        reply = QMessageBox.question(
            self.view, "Save Project Settings",
            f"Save the current instrument settings into project '{self.project.name}'?",
            buttons,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Yes:
            self._capture_ui_into_project(self.project)
            try:
                self.project.save()
            except ProjectError as e:
                QMessageBox.warning(self.view, "Could Not Save Project", str(e))
        return True

    def _set_project(self, project: Project | None) -> None:
        """Makes `project` the open one: window/label, menu state, and everywhere data goes. Does
        NOT load its settings into the controls -- that is _load_project_into_ui(), kept separate so
        Save As (which creates a project FROM the current UI) does not immediately read it back."""
        if project is None and self.is_connected:
            # A live connection has nowhere valid to write once its project is gone (there is no
            # fallback directory any more -- see _list_dir()'s docstring), so closing a project
            # closes the connection with it rather than leaving one dangling that the user could
            # not have opened in the first place without a project.
            self._disconnect(user_requested=True)
        self.project = project
        write_last_project(config.APP_STATE_PATH, project.path if project else None)
        self.view.set_project_actions_enabled(project is not None)
        self.view.set_project_open(project is not None)
        if project is None:
            self.view.set_project_label(None, None)
            self.view.file_view.set_project_dirs(None, None)
            self.view.histogram_view.set_export_directory(None)
        else:
            self.view.set_project_label(project.name, str(project.path))
            self.view.file_view.set_project_dirs(project.list_dir, project.raw_dir)
            self.view.histogram_view.set_export_directory(project.spectra_dir)
            # set_project_open() just re-enabled combo_ports; recompute btn_connect against
            # whatever ports are actually plugged in rather than leaving it at whatever state it
            # was forced to while the window was locked.
            self.scan_ports()
        self._update_filename_preview()

    def _load_project_into_ui(self, project: Project) -> None:
        """Populates the controls from the project. Nothing is written to the device here -- see
        _offer_apply_to_device()."""
        panels = self.view.subsystem_panels()
        for key, values in project.device.items():
            panel = panels.get(key)
            if panel is None:
                logger.warning(f"Project has settings for unknown subsystem '{key}'; ignored")
                continue
            panel.set_values(values)
        self.view.histogram_view.apply_project_settings(project.spectrum)
        prefix = project.acquisition.get("file_prefix")
        if prefix:
            self.view.txt_file_prefix.setText(str(prefix))
        autoincrement = project.acquisition.get("autoincrement")
        if isinstance(autoincrement, bool):
            self.view.chk_autoincrement.setChecked(autoincrement)
        self._update_filename_preview()

    def _capture_ui_into_project(self, project: Project) -> None:
        """The inverse: current control values into the project document, ready to save."""
        device = project.device
        for key, panel in self.view.subsystem_panels().items():
            values = panel.get_values()
            if values:
                device[key] = values
        project.spectrum.update(self.view.histogram_view.project_settings())
        project.acquisition.update({
            "file_prefix": self.view.txt_file_prefix.text(),
            "autoincrement": self.view.chk_autoincrement.isChecked(),
        })

    def _offer_apply_to_device(self) -> None:
        """Asks once, then writes every subsystem the project carries. The confirmation is not
        ceremony: the device may be sitting on a live detector, and this changes VGA gain and
        trigger threshold among other things -- values a project opened for reference should not be
        able to alter without the user saying so. Declining leaves the settings loaded in the forms,
        where each panel's own Apply can still push them one at a time."""
        if self.project is None or self.config_client is None or not self.is_connected:
            return
        if not self.project.device:
            return
        reply = QMessageBox.question(
            self.view, "Apply Project Settings",
            f"Write project '{self.project.name}' settings to the connected device?\n\n"
            "This changes trigger, PSD, FCI, baseline restorer and VGA gain registers immediately. "
            "Declining leaves the values loaded in the configuration forms, unapplied.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        panels = self.view.subsystem_panels()
        for key in self.project.device:
            panel = panels.get(key)
            if panel is not None:
                # SubsystemPanel.apply() writes only the fields that differ from the device's own
                # last-read values and refreshes afterward, so this is the same path (and the same
                # per-panel error reporting) as clicking every Apply button in turn.
                panel.apply()
        logger.info(f"Applied project '{self.project.name}' settings to device.")

    # ---------------------------------------------------------------------------- port discovery

    def scan_ports(self) -> None:
        self.view.combo_ports.clear()
        matches = list_matching_ports(config.TARGET_VID_HEX, config.TARGET_PID_HEX)
        for p in matches:
            label = f"{p.device} ({p.description})" if p.description else p.device
            self.view.combo_ports.addItem(label, p.device)
        if not matches:
            self.view.combo_ports.addItem(
                f"No device found (VID:PID {config.TARGET_VID_HEX}:{config.TARGET_PID_HEX})", None
            )
            self.view.btn_connect.setEnabled(False)
        else:
            self.view.btn_connect.setEnabled(True)

    # ---------------------------------------------------------------------------- connect/disconnect

    def toggle_connection(self) -> None:
        if not self.is_connected:
            # Reset here, not in _connect(): this is the one place a connection attempt is a
            # deliberate, fresh user action rather than an automatic retry. _connect() is also
            # called from _attempt_reconnect() after it has just incremented the counter -- if
            # _connect() itself reset it to 0, every retry would log its own bumped number and
            # then immediately zero it again before the next failure was even detected, so the
            # count could never advance past 1 and "give up after N attempts" would never fire.
            # That bug shipped and sat unnoticed until the first real reconnect episode.
            self._reconnect_count = 0
            self._connect()
        else:
            self._disconnect(user_requested=True)

    def _connect(self) -> None:
        port = self.view.combo_ports.currentData()
        if not port:
            return
        self._expect_connection = True
        self._reconnect_timer.stop()

        # AcquisitionWorker now spawns and supervises the reader process itself (see its module
        # docstring) -- it takes the port string, not a live FciTransport, since the transport is
        # constructed INSIDE that child process and never exists here.
        self.worker = AcquisitionWorker(port, config.BATCH_POLL_INTERVAL_MS / 1000.0,
                                         config.STATS_POLL_INTERVAL_MS / 1000.0,
                                         config.BATCH_POLL_BUSY_MS / 1000.0,
                                         config.SCOPE_INTERVAL_MS / 1000.0)
        self.config_client = self.worker.make_client()
        self.worker.batch_received.connect(self.on_batch_received)
        self.worker.amplitude_batch_received.connect(self.on_amplitude_batch_received)
        self.worker.trace_received.connect(self.on_trace_received)
        self.worker.stats_received.connect(self.view.live_view.update_stats)
        self.worker.connection_changed.connect(self.on_connection_changed)
        self.worker.error_occurred.connect(self.on_error)

        self.view.btn_connect.setText("Connecting...")
        self.view.btn_connect.setEnabled(False)
        self.view.combo_ports.setEnabled(False)
        self.view.btn_refresh_ports.setEnabled(False)
        self.worker.start()

    def _disconnect(self, user_requested: bool) -> None:
        if user_requested:
            self._expect_connection = False
            self._reconnect_count = 0
            self._reconnect_timer.stop()

        self.view.btn_connect.setText("Disconnecting...")
        self.view.btn_connect.setEnabled(False)
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()  # blocks briefly: joins the reader process, then this thread

    def _attempt_reconnect(self) -> None:
        if not self._expect_connection or self.is_connected:
            return
        port = self.view.combo_ports.currentData()
        if not port or (self.worker is not None and self.worker.isRunning()):
            return
        self._reconnect_count += 1
        logger.warning(
            f"Reconnect attempt {self._reconnect_count}/{config.MAX_RECONNECT_ATTEMPTS} to {port}"
        )
        self.view.btn_connect.setText(f"Retrying ({self._reconnect_count}/"
                                       f"{config.MAX_RECONNECT_ATTEMPTS})...")
        self._connect()

    @Slot(bool)
    def on_connection_changed(self, online: bool) -> None:
        self.is_connected = online
        if online:
            self._reconnect_timer.stop()
            self._reconnect_count = 0
            self.view.lbl_status.setText("\U0001F7E2 Connected")
            self.view.btn_connect.setText("Disconnect")
            self.view.btn_connect.setEnabled(True)
            self.view.set_connected_controls_enabled(True)
            self.view.config_panel.set_client(self.config_client)
            self.view.live_view.set_client(self.config_client)
            self.view.scope_view.set_client(self.config_client)
            # Spectrum's Run/Stop is local UI state that predates this connection (it defaults to
            # running -- see HistogramView), so a fresh worker needs to be told where it already
            # stands rather than only learning about it on the NEXT click. Harmless if Live FCI/PSD
            # also starts running afterwards: the reader process's own mode selection prioritizes
            # that over this regardless of what was sent here.
            self.worker.request_spectrum_poll(self.view.histogram_view.is_running())
            # Every set_client() above ran a refresh(), overwriting the forms with whatever the
            # device currently holds -- so a project opened while disconnected would have just been
            # silently discarded from the UI. Reload it and offer to push it, which is also the
            # right behavior for connecting a device to an already-open project: the project, not
            # whatever was last flashed, is what defines this campaign's settings.
            if self.project is not None:
                self._load_project_into_ui(self.project)
                self._offer_apply_to_device()
        else:
            self.view.set_connected_controls_enabled(False)
            self.view.config_panel.set_client(None)
            self.view.live_view.set_client(None)
            self.view.live_view.set_controls_enabled(False)
            self.view.scope_view.set_client(None)
            self.view.scope_view.set_trigger_level(None)
            # Closes out any in-progress recording session directly, without touching the Record
            # checkbox itself -- it's a lasting preference (armed by default), not something a
            # disconnect should reset, so it stays exactly as the user left it for next time.
            if self.csv_logger is not None:
                logger.info(f"Recording stopped by disconnect ({self.csv_logger.event_count} "
                            f"events logged).")
            self.csv_logger = None
            self.scope_csv_logger = None
            self.view.set_recording_active(False)
            self.worker = None
            self.config_client = None
            self._live_acq_running = False
            self._scope_running = False

            if self._expect_connection:
                if self._reconnect_count >= config.MAX_RECONNECT_ATTEMPTS:
                    self.view.lbl_status.setText("\U0001F534 Link lost (giving up)")
                    self.view.btn_connect.setText("Connect")
                    self.view.btn_connect.setEnabled(True)
                    self.view.combo_ports.setEnabled(True)
                    self.view.btn_refresh_ports.setEnabled(True)
                    self._expect_connection = False
                else:
                    self.view.lbl_status.setText("\U0001F7E0 Link lost, retrying...")
                    self._reconnect_timer.start(config.RECONNECT_INTERVAL_MS)
            else:
                self.view.lbl_status.setText("\U0001F534 Disconnected")
                self.view.btn_connect.setText("Connect")
                self.view.btn_connect.setEnabled(True)
                self.view.combo_ports.setEnabled(True)
                self.view.btn_refresh_ports.setEnabled(True)

    @Slot(str)
    def on_error(self, message: str) -> None:
        logger.warning(message)
        self.view.lbl_status.setText(f"⚠ {message[:70]}")

    # ---------------------------------------------------------------------------------- acquisition

    def start_acquisition(self) -> None:
        if self.worker is not None:
            self.worker.request_start_acquisition()
        self._live_acq_running = True

    def stop_acquisition(self) -> None:
        if self.worker is not None:
            self.worker.request_stop_acquisition()
        self._live_acq_running = False

    @Slot(list)
    def on_batch_received(self, events) -> None:
        self.view.live_view.add_events(events)
        self.view.histogram_view.add_events(events)
        if self.csv_logger is not None:
            self.csv_logger.append_many(self.view.live_view.filter_for_recording(events))

    @Slot(list)
    def on_amplitude_batch_received(self, events) -> None:
        """$RA batches (list[AmpEvent]): amplitude-only data the reader process polls in place of
        $RQ while Live FCI/PSD acquisition is not running (see reader_process.py's mode selection).
        Goes to the Spectrum tab only -- LiveView needs real fci/psd/energy_long, which these
        events do not carry, and is not recorded to CSV for the same reason (that log's schema is
        one row per PAIRED event)."""
        self.view.histogram_view.add_events(events)

    # ---------------------------------------------------------------------------------- spectrum

    def _on_spectrum_run(self) -> None:
        if self.worker is not None:
            self.worker.request_spectrum_poll(True)

    def _on_spectrum_stop(self) -> None:
        if self.worker is not None:
            self.worker.request_spectrum_poll(False)

    @Slot(object)
    def on_trace_received(self, trace) -> None:
        self.view.scope_view.show_trace(trace)
        if self.scope_csv_logger is not None and trace is not None:
            self.scope_csv_logger.append(trace)

    # ---------------------------------------------------------------------------------- recording

    def on_record_toggled(self, checked: bool) -> None:
        """The Record checkbox is a standing preference (armed by default), not itself the
        trigger for writing files -- that happens in _ensure_recording_session(), gated behind
        the confirmation shown when Start is actually pressed. Unchecking it, though, closes any
        session already in progress immediately: turning Record off must always mean "stop
        writing now", not "stop next time Start happens to be pressed".

        Symmetrically, re-checking it while live/scope acquisition is already running (Start was
        pressed before Record got toggled off) has no future Start press to hang a fresh
        _ensure_recording_session() off of -- resume immediately instead. Skips the confirmation
        dialog on purpose: that dialog guards *starting* acquisition with recording armed, not
        re-arming a preference the user just disabled a moment ago on already-running acquisition."""
        if not checked and self.csv_logger is not None:
            logger.info(f"Recording stopped ({self.csv_logger.event_count} events logged).")
            self.csv_logger = None
            self.scope_csv_logger = None
            self.view.set_recording_active(False)
            self._update_filename_preview()
        elif checked and self.csv_logger is None and (self._live_acq_running or self._scope_running):
            if not self._ensure_recording_session():
                self.view.chk_record.setChecked(False)  # user declined the overwrite warning

    def _confirm_and_maybe_record(self) -> bool:
        """Consulted by LiveView/ScopeView before their Start button does anything. Returns
        whether Start should proceed at all -- declining the recording warning (or a follow-on
        overwrite warning from _ensure_recording_session()) cancels the whole Start action, not
        just recording, since the warning is framed as "Start will begin recording" rather than
        as a separate, skippable prompt."""
        if not self.view.chk_record.isChecked():
            return True
        reply = QMessageBox.question(
            self.view,
            "Start Acquisition",
            "Recording is enabled -- starting will begin writing data to CSV in project "
            f"'{self.project.name}' ({self.project.list_dir}).\n\nContinue?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return False
        return self._ensure_recording_session()

    # ---- filename prefix / index (File Management tab) ----

    FILENAME_INDEX_DIGITS = 4

    def _sanitized_prefix(self) -> str:
        prefix = self.view.txt_file_prefix.text().strip().replace("/", "_").replace("\\", "_")
        return prefix or "recording"

    def _list_dir(self) -> Path | None:
        """Where per-event CSVs go: the project's LIST/. None with no project open -- there is no
        other destination any more (File Management's own directory picker is gone; see
        file_management_view.py's docstring), and every caller of this treats None as "nothing to
        do", which is correct since recording is unreachable without a project open in the first
        place (MainWindow disables the whole window below the Project menu until then)."""
        return self.project.list_dir if self.project is not None else None

    def _raw_dir(self) -> Path | None:
        """Where scope-trace CSVs go: the project's RAW/. See _list_dir()'s docstring."""
        return self.project.raw_dir if self.project is not None else None

    def _next_free_index(self, prefix: str) -> int:
        """Lowest index for which neither the fci_live nor the scope_traces file exists yet -- 1 if
        neither directory exists or nothing matching `prefix` is in them. Both are scanned, each for
        its own suffix, because with a project open the pair lives in LIST/ and RAW/ respectively
        and an index free in only one of them would still collide."""
        used: set[int] = set()
        for out_dir, suffix in ((self._list_dir(), "fci_live"), (self._raw_dir(), "scope_traces")):
            if out_dir is None or not out_dir.exists():
                continue
            pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)_{suffix}\.csv$")
            for p in out_dir.iterdir():
                m = pattern.match(p.name)
                if m:
                    used.add(int(m.group(1)))
        index = 1
        while index in used:
            index += 1
        return index

    def _would_overwrite(self, prefix: str, index: int) -> bool:
        list_dir, raw_dir = self._list_dir(), self._raw_dir()
        if list_dir is None:
            return False
        stem = f"{prefix}_{index:0{self.FILENAME_INDEX_DIGITS}d}"
        return (list_dir / f"{stem}_fci_live.csv").exists() or \
               (raw_dir / f"{stem}_scope_traces.csv").exists()

    def _update_filename_preview(self) -> None:
        if self.project is None:
            self.view.lbl_filename_preview.setText("")
            return
        prefix = self._sanitized_prefix()
        autoincrement = self.view.chk_autoincrement.isChecked()
        index = self._next_free_index(prefix) if autoincrement else 1
        stem = f"{prefix}_{index:0{self.FILENAME_INDEX_DIGITS}d}"
        warning = ""
        if not autoincrement and self._would_overwrite(prefix, index):
            warning = "  ⚠ already exists -- will be overwritten"
        self.view.lbl_filename_preview.setText(
            f"Next files: {stem}_fci_live.csv, {stem}_scope_traces.csv{warning}"
        )

    def _device_settings_lines(self) -> list[str] | None:
        """One formatted line per subsystem (Trigger/PSD/FCI/BLR), for the CSV header -- see
        csv_logger.py's module docstring for why this exists. Reads through config_client rather
        than the worker thread: SubsystemPanel already does the same off the GUI thread (its own
        docstring explains why that is safe), and this runs once, synchronously, at the moment
        recording starts, not on every poll.

        Returns None (not a partial list) if the device cannot be reached at all, so the header
        can say plainly "not available" rather than mixing real settings with silence. A single
        subsystem's read failing (e.g. FCI absent from this bitstream) still produces a line for
        it, since that absence is itself worth recording."""
        if self.config_client is None:
            return None
        lines = []
        for label, getter in (
            ("trigger", self.config_client.get_trigger),
            ("psd", self.config_client.get_psd),
            ("fci", self.config_client.get_fci),
            ("blr", self.config_client.get_blr),
        ):
            try:
                cfg = getter()
            except FciError as e:
                lines.append(f"{label}: read failed ({e})")
                continue
            # dataclasses.asdict(), not vars(): every Config dataclass uses slots=True (no
            # instance __dict__), so vars(cfg) raises TypeError here.
            #
            # watermark excluded: PsdConfig/FciConfig's own docstrings note it has no observable
            # effect on anything this firmware build does (no ISR is registered for it; $RB/$RV
            # drain by polling instead) and that the config panel already omits it from its UI for
            # the same reason -- recording it here would contradict that and suggest a dataset
            # depends on a value that, in fact, does nothing.
            fields = ", ".join(f"{k}={v}" for k, v in dataclasses.asdict(cfg).items()
                                if k != "watermark")
            lines.append(f"{label}: {fields}")
        return lines

    def _ensure_recording_session(self) -> bool:
        """Returns whether a session is (now) active. False only means the user declined an
        overwrite warning -- callers (Start confirmation, on_record_toggled's resume-mid-run
        path) must treat that the same as declining to record at all."""
        if self.csv_logger is not None:
            return True  # already recording (e.g. Stop then Start again) -- keep the same files
        list_dir = self._list_dir()
        raw_dir = self._raw_dir()
        if list_dir is None or raw_dir is None:
            # Unreachable in practice: Start requires a connection, which requires a project open
            # (MainWindow.set_project_open()). Guarded anyway rather than trusting that invariant
            # silently -- an AssertionError here would be a worse failure mode than declining.
            logger.warning("_ensure_recording_session() called with no project open")
            return False
        prefix = self._sanitized_prefix()
        autoincrement = self.view.chk_autoincrement.isChecked()

        if autoincrement:
            index = self._next_free_index(prefix)
        else:
            index = 1
            if self._would_overwrite(prefix, index):
                stem = f"{prefix}_{index:0{self.FILENAME_INDEX_DIGITS}d}"
                where = str(list_dir) if list_dir == raw_dir else f"{list_dir} / {raw_dir}"
                reply = QMessageBox.warning(
                    self.view,
                    "File Will Be Overwritten",
                    f"{stem}_fci_live.csv and/or {stem}_scope_traces.csv already exist in "
                    f"{where} and Autoincrement is off -- continuing will overwrite them."
                    "\n\nContinue?",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if reply != QMessageBox.StandardButton.Ok:
                    return False

        settings_lines = self._device_settings_lines()
        self.csv_logger = CsvLogger(list_dir, prefix, index, settings_lines)
        self.scope_csv_logger = TraceCsvLogger(raw_dir, prefix, index, settings_lines)
        self.view.set_recording_active(True)
        logger.info(f"Recording started: {self.csv_logger.path}, {self.scope_csv_logger.path}")
        self._update_filename_preview()
        return True

    # ---------------------------------------------------------------------------------- scope

    def scope_start(self, n: int) -> None:
        if self.worker is not None:
            self.worker.request_scope_start(n)
        self._scope_running = True

    def scope_stop(self) -> None:
        if self.worker is not None:
            self.worker.request_scope_stop()
        self._scope_running = False

    def scope_single(self, n: int) -> None:
        if self.worker is not None:
            self.worker.request_trace(n)

    def open_calibration_wizard(self) -> None:
        if self.config_client is None:
            return
        dlg = CalibrationWizard(self.config_client, self.view)
        if dlg.exec() != CalibrationWizard.DialogCode.Accepted:
            return
        try:
            dlg.apply_to_device()
        except FciError as e:
            logger.warning(f"calibration apply failed: {e}")
            QMessageBox.warning(self.view, "Apply Failed", f"Could not write trigger config: {e}")
            return
        self.view.scope_view.trigger_config.refresh()

    def open_fom_wizard(self) -> None:
        dlg = FomWizard(self.config_client, self.worker, self.view.live_view.get_accumulated_events,
                         self.view.histogram_view.calibration(), self.view)
        dlg.exec()

    # -------------------------------------------------------------------------------------- misc

    def cleanup(self) -> None:
        logger.info("Shutting down.")
        # Asked before the worker is torn down: the panels still hold the settings being saved
        # either way, but a dialog raised after the reader process has gone would appear over a
        # half-dismantled window.
        self._offer_save_before_leaving(allow_cancel=False)
        self._expect_connection = False
        self._reconnect_timer.stop()
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
