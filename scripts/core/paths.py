from pathlib import Path
import os
import sys


def private_home(root, value):
    path = Path(value).expanduser().absolute()
    resolved = path.resolve()
    root = Path(root).resolve()
    if resolved.is_relative_to(root):
        parts = resolved.relative_to(root).parts
        if not parts or parts[0] not in {'derived_private', 'private_sources'}:
            raise ValueError('Keep agent data outside the checkout, or in the ignored derived_private directory.')
    return path


def default_home(root, environment=None, platform=None, user=None):
    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    user = Path.home() if user is None else Path(user)
    if value := environment.get('TOPCLASS_HOME'):
        return private_home(root, value)
    legacy = Path(root) / 'derived_private' / 'agents'
    if legacy.exists():
        return legacy
    if platform == 'darwin':
        return user / 'Library' / 'Application Support' / 'Topclass' / 'agents'
    if platform == 'win32':
        base = Path(environment.get('LOCALAPPDATA') or user / 'AppData' / 'Local').expanduser()
        if not base.is_absolute():
            raise ValueError('LOCALAPPDATA must be an absolute path')
        return base / 'Topclass' / 'agents'
    base = Path(environment.get('XDG_DATA_HOME') or user / '.local' / 'share').expanduser()
    if not base.is_absolute():
        raise ValueError('XDG_DATA_HOME must be an absolute path')
    return base / 'topclass' / 'agents'
