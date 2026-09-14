import sys
from pathlib import Path
from PyQt5.QtWidgets import QApplication
from .core import ConfigManager, logger
from .ui import MainWindow

def main():
    config_path = Path(__file__).parent.parent / "configs" / "app_config.yaml"
    if not config_path.exists():
        example = config_path.with_suffix(".example.yaml")
        if example.exists():
            import shutil
            shutil.copy(example, config_path)
            logger.info(f"Created default config from {example}")
        else:
            logger.error(f"Config not found: {config_path}")
            sys.exit(1)
    cfg_mgr = ConfigManager(config_path)
    app = QApplication(sys.argv)
    win = MainWindow(cfg_mgr)
    win.show()
    sys.exit(app.exec_())