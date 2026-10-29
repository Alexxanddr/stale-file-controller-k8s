"""A real FUSE mount and a running client with an empty, shell-less root."""
import os
import subprocess
import time
from pathlib import Path

for path in ('/branches/data/torrents', '/sandbox/mnt/merged'):
    os.makedirs(path, exist_ok=True)
Path('/branches/data/torrents/probe').write_text('namespace test')
subprocess.Popen(['mergerfs', '-f', '-o', 'allow_other', '/branches', '/sandbox/mnt/merged'])
for _ in range(100):
    if os.path.ismount('/sandbox/mnt/merged'):
        break
    time.sleep(0.1)
else:
    raise RuntimeError('FUSE mount did not appear')
os.chroot('/sandbox')
os.chdir('/')
assert not os.path.exists('/bin/sh')
assert not os.path.exists('/usr/bin/stat')
assert Path('/mnt/merged/data/torrents/probe').read_text() == 'namespace test'
print('READY', flush=True)
while True:
    time.sleep(1)
