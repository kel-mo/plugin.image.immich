#!/usr/bin/env python3
"""Build a sideloadable zip of the add-on into dist/."""
import os
import re
import subprocess
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ID = os.path.basename(HERE)
EXCLUDE_DIRS = {'.git', 'dist', '__pycache__', '.ruff_cache', '.claude', 'tests'}
EXCLUDE_FILES = {'build.py', '.gitignore', 'README.md'}


def version():
    with open(os.path.join(HERE, 'addon.xml'), encoding='utf-8') as f:
        return re.search(r'<addon[^>]*\sversion="([^"]+)"', f.read()).group(1)


def tracked_files():
    """Ship only what git tracks; fall back to a directory walk outside a checkout."""
    try:
        out = subprocess.check_output(['git', 'ls-files', '-z'], cwd=HERE)
        return [p for p in out.decode().split('\0') if p]
    except (OSError, subprocess.CalledProcessError):
        found = []
        for base, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            found += [os.path.relpath(os.path.join(base, n), HERE) for n in files if not n.endswith('.pyc')]
        return found


def main():
    out_dir = os.path.join(HERE, 'dist')
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, '{}-{}.zip'.format(ADDON_ID, version()))
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in tracked_files():
            if os.path.basename(rel) in EXCLUDE_FILES or EXCLUDE_DIRS.intersection(rel.split('/')[:-1]):
                continue
            z.write(os.path.join(HERE, rel), os.path.join(ADDON_ID, rel))
    print(out)


if __name__ == '__main__':
    main()
