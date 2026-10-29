import json
from pathlib import Path
import shutil
import sys
import time
import tkinter as tk
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desktop.storage import Store
from desktop.ui import App


class LoginUiTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent.parent / 'diagnostics' / ('ui-regression-' + uuid.uuid4().hex)
        self.store = Store(self.directory)
        self.root = tk.Tk()

    def tearDown(self):
        try:
            self.app.close()
        except AttributeError:
            self.root.destroy()
        self.store.db.close()
        expected = (Path(__file__).resolve().parent.parent / 'diagnostics').resolve()
        if self.directory.resolve().is_relative_to(expected):
            shutil.rmtree(self.directory)

    def test_login_failure_reason_is_visible_without_scrolling(self):
        self.app = App(self.root, self.store)
        self.app.connecting = True
        def failing_login():
            raise ValueError('TLS 初始化失败：缺少运行资源')
        self.app.background(failing_login, 'courses')
        deadline = time.monotonic() + 2
        while self.app.events.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.app.poll()
        self.root.update_idletasks()
        self.assertIn('TLS', self.app.connection.cget('text'))

    def test_restored_geometry_is_clamped_to_current_screen(self):
        self.store.save_settings({'geometry': '2400x1800+9000+9000'})
        self.app = App(self.root, self.store)
        self.root.update()
        self.assertLessEqual(self.root.winfo_width(), self.root.winfo_screenwidth())
        self.assertLessEqual(self.root.winfo_height(), self.root.winfo_screenheight() - 60)
        self.assertLess(self.root.winfo_x(), self.root.winfo_screenwidth())


if __name__ == '__main__':
    unittest.main()
