import hashlib, os, runpy, sys, tempfile
from pathlib import Path
package_root = Path(sys.argv[1])
package_digest = sys.argv[2]
configured_snapshots = os.environ.get('TJ_AGENTS_STARTUP_SNAPSHOT_DIRECTORY', '')
plugin_data = Path(os.environ.get('PLUGIN_DATA', '')).resolve()
snapshots = Path(configured_snapshots) if configured_snapshots else plugin_data / 'hook-snapshots'
snapshot_key = hashlib.sha256((str(package_root) + chr(0) + package_digest).encode()).hexdigest()
loader = snapshots / snapshot_key / 'hooks' / 'codex' / 'snapshot.py'
loader = loader if loader.is_file() else package_root / 'hooks' / 'codex' / 'snapshot.py'
newline = chr(10).encode()
source = loader.read_bytes().replace(chr(13).encode() + newline, newline).replace(chr(13).encode(), newline) if loader.is_file() else b''
hashlib.sha256(source).hexdigest() == LOADER_DIGEST or (sys.stderr.write('Codex hook snapshot loader integrity check failed' + chr(10)), sys.exit(2))
workspace = tempfile.TemporaryDirectory()
staged = Path(workspace.name) / 'loader.py'
staged.write_bytes(source)
runpy.run_path(str(staged), run_name='__main__')
