"""Installer and setup helper for Antigravity Stream Deck plugin.

Copies or symlinks the plugin to %APPDATA%/Elgato/StreamDeck/Plugins/
and handles uninstallation.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def get_streamdeck_plugins_dir() -> Path:
    """Return the Elgato Stream Deck plugins directory on Windows."""
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise RuntimeError("APPDATA environment variable not found.")
    return Path(app_data) / "Elgato" / "StreamDeck" / "Plugins"


def install_plugin(source_dir: Path | None = None, symlink: bool = False) -> Path:
    """Install the plugin to the Stream Deck plugins folder."""
    if source_dir is None:
        project_root = Path(__file__).resolve().parent.parent.parent
        source_dir = project_root / "sdplugin" / "com.user.antigravity.sdPlugin"

    if not source_dir.is_dir():
        raise FileNotFoundError(f"Plugin source directory not found: {source_dir}")

    target_plugins_dir = get_streamdeck_plugins_dir()
    target_plugins_dir.mkdir(parents=True, exist_ok=True)

    dest_dir = target_plugins_dir / source_dir.name

    if dest_dir.exists() and dest_dir.is_symlink():
        dest_dir.unlink()

    if symlink:
        try:
            print(f"Creating symlink from {source_dir} to {dest_dir}...")
            dest_dir.symlink_to(source_dir, target_is_directory=True)
            print("Successfully installed via symlink!")
            return dest_dir
        except OSError:
            print("Symlink creation failed. Falling back to directory copy...")

    print(f"Copying {source_dir} to {dest_dir}...")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, dest_dir, dirs_exist_ok=True)

    # Save project root directory to project_path.txt for run.bat
    project_root = Path(__file__).resolve().parent.parent.parent
    config_file = dest_dir / "project_path.txt"
    try:
        config_file.write_text(str(project_root), encoding="utf-8")
        print(f"Configured project path: {project_root}")
    except Exception as e:
        print(f"Warning: Failed to write project_path.txt: {e}")

    print("Successfully copied plugin!")
    return dest_dir


def uninstall_plugin(plugin_name: str = "com.user.antigravity.sdPlugin") -> bool:
    """Remove plugin from Stream Deck plugins folder."""
    target_plugins_dir = get_streamdeck_plugins_dir()
    dest_dir = target_plugins_dir / plugin_name

    if not dest_dir.exists():
        print(f"Plugin not found at {dest_dir}")
        return False

    print(f"Uninstalling {dest_dir}...")
    if dest_dir.is_symlink():
        dest_dir.unlink()
    else:
        shutil.rmtree(dest_dir)
    print("Uninstalled successfully.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Antigravity Stream Deck plugin")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall the plugin")
    parser.add_argument("--symlink", action="store_true", help="Try symlinking instead of copying")
    args = parser.parse_args()

    if args.uninstall:
        uninstall_plugin()
    else:
        dest = install_plugin(symlink=args.symlink)
        print(f"Plugin installed to: {dest}")
        print("Please restart Elgato Stream Deck software to load the plugin.")


if __name__ == "__main__":
    main()
