# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import ast
import ssl

root = Path(SPECPATH)
tree = ast.parse((root / 'desktop' / '__init__.py').read_text(encoding='utf-8'))
version = next(ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '__version__' for t in node.targets))
# Some Python runtimes require an additional certificate file at SSLContext creation.
# Keep this optional runtime resource without weakening HTTPS verification.
runtime_pem = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
datas = [(str(root / 'resource'), 'resource')]
if runtime_pem.is_file():
    datas.append((str(runtime_pem), '.'))
a = Analysis([str(root / 'desktop_main.py')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=['api.font_decoder', 'api.cxsecret_font'], hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=['openai', 'celery', 'flask', 'ddddocr'], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='学习通工作台', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=True, console=False,
          disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, upx_exclude=[],
               name=f'ChaoxingDesktop-v{version}-windows-x64')
