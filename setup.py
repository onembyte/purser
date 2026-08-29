"""py2app build: `.venv/bin/python setup.py py2app` -> dist/Purser.app

Notes that matter:
  * `csm` is listed under `packages` (not `includes`) so py2app copies it to disk
    rather than into the zipped site-packages. config.WEB_DIR resolves
    `Path(__file__).parent / "web"` at runtime and needs real files on disk.
  * LSUIElement stays 0: this is a normal windowed app with a Dock icon.
"""
from setuptools import setup

APP = ["app.py"]

OPTIONS = {
    "iconfile": "assets/icon.icns",
    "argv_emulation": False,          # True breaks under modern macOS + PyObjC
    "packages": ["csm"],              # kept unzipped: web/ must exist as files
    "includes": ["objc", "AppKit", "WebKit", "Foundation", "Quartz", "PyObjCTools"],
    "excludes": ["py2app", "setuptools", "pip", "tkinter", "test", "unittest"],
    "plist": {
        "CFBundleName": "Purser",
        "CFBundleDisplayName": "Purser",
        "CFBundleIdentifier": "com.francomichetti.purser",
        "CFBundleShortVersionString": "1.2.0",
        "CFBundleVersion": "5",
        "NSHumanReadableCopyright": "",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
        # The app reads ~/.claude and opens Terminal to resume a session.
        "NSAppleEventsUsageDescription":
            "Purser opens Terminal to resume a Claude Code session.",
    },
}

setup(
    name="Purser",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
