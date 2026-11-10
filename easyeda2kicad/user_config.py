"""Optional per-user settings for where easyeda2kicad writes its library.

Settings come from environment variables first, then ~/.easyeda2kicad/config.json:

    {
      "library": "/path/to/hardware/libraries/easyeda2kicad/easyeda2kicad",
      "model_3d_path": "${KIPRJMOD}/../../libraries/easyeda2kicad/easyeda2kicad.3dshapes",
      "projects_dir": "/path/to/hardware/projects"
    }

library        Library base path, without extension. The tool writes <library>.kicad_sym,
               <library>.pretty and <library>.3dshapes. Env: EASYEDA2KICAD_LIBRARY
model_3d_path  The 3D model folder path stored inside footprints (may use KiCad
               variables such as ${KIPRJMOD}). Env: EASYEDA2KICAD_3D_PATH
projects_dir   Where the browser-extension dashboard looks for KiCad projects.
               Env: EASYEDA2KICAD_PROJECTS

With no settings, the original defaults apply (~/Documents/Kicad/easyeda2kicad).
"""
import json
import os

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".easyeda2kicad", "config.json")
_ENV = {
    "library": "EASYEDA2KICAD_LIBRARY",
    "model_3d_path": "EASYEDA2KICAD_3D_PATH",
    "projects_dir": "EASYEDA2KICAD_PROJECTS",
}


def load_user_config() -> dict:
    data = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    for key, env in _ENV.items():
        if os.environ.get(env):
            data[key] = os.environ[env]
    if data.get("library"):
        data["library"] = os.path.expanduser(data["library"])
    if data.get("projects_dir"):
        data["projects_dir"] = os.path.expanduser(data["projects_dir"])
    return data
