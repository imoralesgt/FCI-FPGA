import sys

from PySide6.QtWidgets import QApplication

from controllers import AppController
from logger_config import initialize_logging
from ui.main_window import MainWindow

logger = None
"""Bound below, inside the __main__ guard, not here at module level -- see that guard's comment
for why. Any code that can run before main() (there is none today) must not assume this is set."""


def _exception_hook(exctype, value, traceback):
    logger.critical("Unhandled exception", exc_info=(exctype, value, traceback))
    sys.__excepthook__(exctype, value, traceback)


def main() -> int:
    logger.info("---- FCI-FPGA client starting ----")
    app = QApplication(sys.argv)

    window = MainWindow()
    controller = AppController(window)

    def close_intercept(event):
        controller.cleanup()
        event.accept()
        logger.info("---- FCI-FPGA client stopped ----")

    window.closeEvent = close_intercept
    window.show()
    return app.exec()


if __name__ == "__main__":
    # AcquisitionWorker spawns the reader process via multiprocessing's "spawn" start method
    # (fci_api/reader_process.py's module docstring explains why), which re-executes THIS file
    # in the child -- under __name__ == "__mp_main__", never "__main__", precisely so this guard
    # does not fire there. initialize_logging() and sys.excepthook therefore stay inside it: they
    # must run exactly once, in the real application process, not a second time in every reader
    # process spawned over the session's lifetime (two RotatingFileHandlers racing over the same
    # file would corrupt it). The reader process gets its own separate log file instead -- see
    # acquisition_worker.py's _READER_LOG_PATH.
    logger = initialize_logging()
    sys.excepthook = _exception_hook
    raise SystemExit(main())
