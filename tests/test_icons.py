"""アイコンのスプライト（docs/design/extensions.md 3 章・10 章）。"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from docseed.web import helpers as h

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "docseed" / "web" / "templates"


def test_icons_txt_matches_sprite():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_icons.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_icon_used_in_templates_exists():
    used = set()
    for p in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r'icon\("([a-z0-9_]+)"', p.read_text()))
    used |= set(h.KIND_ICON.values()) | set(h.STATUS_ICON.values())
    assert used and used <= h.ICON_NAMES, used - h.ICON_NAMES


def test_icon_markup():
    s = str(h.icon("search"))
    assert s.startswith('<svg class="icon" aria-hidden="true"') and f'href="{h.ICONS_URL}#i-search"' in s
    assert 'role="img" aria-label="PDF &amp; 画像"' in str(h.icon("image", "PDF & 画像"))
    with pytest.raises(ValueError):
        h.icon("no_such_icon")


def test_license_shipped():
    lic = ROOT / "src" / "docseed" / "web" / "static" / "LICENSE-material-symbols.txt"
    assert "Apache License" in lic.read_text()
