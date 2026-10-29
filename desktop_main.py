"""Fast Tk entry point: no API/HTTP/AI modules loaded before the window appears."""
import os
from pathlib import Path
import sys
import time


def main():
    started = time.perf_counter()
    # The original progress/logger code expects streams even in a windowed EXE.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    import tkinter as tk
    from desktop import __version__
    from desktop.storage import Store, user_data_dir
    from desktop.ui import App
    root = tk.Tk()
    # A second process must not recover live tasks as if the first one had crashed.
    import msvcrt
    directory = user_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    instance_lock = open(directory / '.instance.lock', 'a+b')
    instance_lock.seek(0)
    instance_lock.write(b'0')
    instance_lock.flush()
    instance_lock.seek(0)
    try:
        msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        from tkinter import messagebox
        root.withdraw()
        messagebox.showinfo('程序已运行', '学习通工作台已在运行，请切换到已有窗口。')
        root.destroy()
        instance_lock.close()
        return
    store = Store()
    app = App(root, store)
    def ready():
        root.update_idletasks()
        elapsed = time.perf_counter() - started
        (store.directory / 'startup.json').write_text(
            __import__('json').dumps({'window_ready_seconds': round(elapsed, 4), 'version': __version__, 'packaged': bool(getattr(sys, 'frozen', False))}), encoding='utf-8')
        # Deferred legacy import; do not load the network stack at UI startup.
        root.after(200, migrate)
        if os.environ.get('CHAOXING_SMOKE_BACKEND'):
            import ssl
            ssl.create_default_context()
            from desktop.backend import Platform
            Platform(store)
            from api.cxsecret_font import resource_path
            (store.directory / 'backend-smoke.json').write_text(__import__('json').dumps({
                'platform_import': True, 'font_table_exists': Path(resource_path('resource/font_map_table.json')).is_file(),
                'ai_sdk_loaded': 'openai' in sys.modules,
                'tls_context_created': True,
            }), encoding='utf-8')
        auto_exit = os.environ.get('CHAOXING_SMOKE_EXIT_MS')
        if auto_exit:
            root.after(int(auto_exit), app.close)
    def migrate():
        if store.get('legacy_imported'):
            return
        base = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
        candidates = [base]
        for candidate in candidates:
            if (candidate / 'config.ini').exists() or (candidate / 'cookies.txt').exists():
                try:
                    store.import_legacy(candidate)
                    for key, var in app.vars.items():
                        var.set(store.settings[key])
                    app.notice.set('已导入旧版设置并备份，请检查连接设置后登录。')
                except Exception as exc:
                    app.notice.set('旧版设置导入失败：' + str(exc))
                break
    root.after_idle(ready)
    root.mainloop()
    instance_lock.close()


if __name__ == '__main__':
    main()
