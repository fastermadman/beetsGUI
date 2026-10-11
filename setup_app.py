"""py2app recipe for beetsGUI.app — run via scripts/build-app.sh, not directly.

    python setup_app.py py2app -A     # alias build (what build-app.sh does)

An alias build is a real .app bundle (own name, icon and Dock entry) whose
code stays where it is: it runs app.py from this checkout on the Python
that built it — the beets pipx venv, with whatever plugins you've injected
and your own config.yaml. `git pull` updates the app; nothing to rebuild
unless this file or the icon changes.

Dropping -A would build a self-contained bundle (Python, beets and plugins
copied in). That is the eventual goal for people without pipx/Homebrew,
but it needs beets' plugins and ffmpeg bundled explicitly first, so it
isn't wired up yet.
"""
from setuptools import setup

setup(
    name='beetsGUI',
    app=['app.py'],
    options={'py2app': {
        'iconfile': 'assets/beetsGUI.icns',
        'plist': {
            'CFBundleName': 'beetsGUI',
            'CFBundleDisplayName': 'beetsGUI',
            'CFBundleIdentifier': 'com.beetsgui.app',
            'CFBundleShortVersionString': '0.1.0',
            'LSApplicationCategoryType': 'public.app-category.music',
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,
        },
    }},
    setup_requires=['py2app'],
)
