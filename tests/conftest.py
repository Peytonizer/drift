import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
pytest_plugins = ["pytest_homeassistant_custom_component"]

import custom_components  # noqa: E402  (the plugin's testing_config package)

if str(ROOT / "custom_components") not in list(custom_components.__path__):
    custom_components.__path__.append(str(ROOT / "custom_components"))
