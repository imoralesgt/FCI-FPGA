import sys

from PySide6.QtWidgets import QApplication

from controllers import AppController
from logger_config import initialize_logging
from ui.main_window import MainWindow

logger = initialize_logging()


def _exception_hook(exctype, value, traceback):
    logger.critical("Unhandled exception", exc_info=(exctype, value, traceback))
    sys.__excepthook__(exctype, value, traceback)


sys.excepthook = _exception_hook


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
    raise SystemExit(main())
