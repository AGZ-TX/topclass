import errno
import os
from pathlib import Path
import subprocess
import time


def venv_python(root, platform=None):
    platform = os.name if platform is None else platform
    return Path(root) / '.venv' / ('Scripts/python.exe' if platform == 'nt' else 'bin/python')


def lock_file(stream, blocking=True, platform=None):
    platform = os.name if platform is None else platform
    if platform != 'nt':
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt
    if os.fstat(stream.fileno()).st_size == 0:
        stream.write('\0')
        stream.flush()
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(error.errno, 'Another process holds this lock') from error
            time.sleep(.05)


def worker_options(platform=None):
    platform = os.name if platform is None else platform
    return {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if platform == 'nt' else {'start_new_session': True}


def skill_content(root, name, platform=None):
    platform = os.name if platform is None else platform
    root = Path(root)
    text = (root / 'docs' / 'skills' / name / 'SKILL.md').read_text(encoding='utf-8')
    if platform == 'nt':
        text = text.replace('"__TOPCLASS_ROOT__/scripts/topclass"', '& "__TOPCLASS_ROOT__/scripts/topclass.cmd"')
    return text.replace('__TOPCLASS_ROOT__', root.as_posix())
