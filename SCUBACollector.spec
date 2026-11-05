# Build this specification ON WINDOWS. PyInstaller does not cross-compile Windows executables.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['run_collector.py'], pathex=[],
    binaries=[], datas=collect_data_files('praw'),
    hiddenimports=collect_submodules('collector') + collect_submodules('google_play_scraper'),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SCUBACollector',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SCUBACollector')
