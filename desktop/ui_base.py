import json
import os
from pathlib import Path
from queue import Queue, Empty
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from . import __version__
from .storage import Store
from .backend import Engine, course_key
from .answers import validate
from .errors import capture_error

STATUS = {'queued': '等待执行', 'running': '运行中', 'done': '已完成',
          'draft': '草稿 · 待审核', 'local_saved': '本地已保存', 'submitted': '已提交',
          'failed': '失败 · 已保留', 'blocked': '未开放', 'paused': 'API 故障 · 已暂停',
          'interrupted': '已中断 · 可恢复', 'unknown': '提交结果待核验',
          'submitting': '提交中', 'platform_done': '平台已完成'}


class BaseApp:
    def __init__(self, root, store=None):
        self.root = root
        self.store = store or Store()
        self.events = Queue()
        self.engine = Engine(self.store, lambda kind, value: self.events.put((kind, value)))
        self.courses, self.points = [], []
        self.checked = set()
        self.course = None
        self.vars = {}
        self.save_timer = None
        self.closed = False
        self.last_error = None
        self.connecting = False
        self.records = {r['id']: r for r in self.store.tasks()}
        self.progress = {}
        self.selected_record = None
        self.shown_rows = None
        self.root.title(f'学习通 · 批量工作台 {__version__}')
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        max_w, max_h = max(600, screen_w - 48), max(480, screen_h - 100)
        import re
        match = re.fullmatch(r'(\d+)x(\d+)(?:[+-]\d+[+-]\d+)?', self.store.settings['geometry'])
        width, height = (int(match[1]), int(match[2])) if match else (1180, 820)
        width, height = min(width, max_w), min(height, max_h)
        self.root.geometry(f'{width}x{height}+{max(0, (screen_w-width)//2)}+{max(0, (screen_h-height)//2-20)}')
        self.root.minsize(min(980, max_w), min(600, max_h))
        self._theme()
        self._build()
        for record in self.records.values():
            self.render_task(record)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self.poll)


    def _theme(self):
        self.root.configure(bg='#eef2f7')
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#eef2f7', foreground='#16283c')
        style.configure('TButton', padding=(12, 7))
        style.configure('Accent.TButton', background='#1769c2', foreground='white')
        style.map('Accent.TButton', background=[('active', '#12569f')])
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Muted.TLabel', foreground='#586b80')
        style.configure('Treeview', rowheight=29, background='white', fieldbackground='white')
        style.configure('Treeview.Heading', padding=7, font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('TNotebook.Tab', padding=(18, 10))
        style.configure('TLabelframe', padding=12)


    def _build(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x', pady=(0, 14))
        ttk.Label(header, text='学习通工作台', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='视频与答题独立执行', style='Muted.TLabel').pack(side='left', padx=20)
        self.connection = ttk.Label(header, text='尚未登录', style='Muted.TLabel', wraplength=340)
        self.connection.pack(side='right')
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill='both', expand=True)
        run = ttk.Frame(self.notebook, padding=14)
        review = ttk.Frame(self.notebook, padding=14)
        settings_container = ttk.Frame(self.notebook)
        settings_canvas = tk.Canvas(settings_container, highlightthickness=0, bg='#eef2f7')
        settings_scrollbar = ttk.Scrollbar(settings_container, orient='vertical', command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_scrollbar.pack(side='right', fill='y')
        settings_canvas.pack(side='left', fill='both', expand=True)
        settings = ttk.Frame(settings_canvas, padding=18)
        settings_window = settings_canvas.create_window((0, 0), window=settings, anchor='nw')
        settings.bind('<Configure>', lambda event: settings_canvas.configure(scrollregion=settings_canvas.bbox('all')))
        settings_canvas.bind('<Configure>', lambda event: settings_canvas.itemconfigure(settings_window, width=event.width))
        self.notebook.add(run, text='任务')
        self.notebook.add(review, text='答题')
        self.notebook.add(settings_container, text='设置')
        self._settings(settings)
        def bind_scroll(widget):
            widget.bind('<MouseWheel>', lambda event: settings_canvas.yview_scroll(-int(event.delta / 120), 'units'))
            for child in widget.winfo_children():
                bind_scroll(child)
        bind_scroll(settings)
        self._run(run)
        self._review(review)
        self.notice = tk.StringVar(value='在“设置”登录后，点击“添加任务”选择课程。')
        banner = ttk.Frame(outer)
        banner.pack(fill='x', pady=(0, 10), before=self.notebook)
        self.error_details = ttk.Button(banner, text='错误详情', command=self.show_error, state='disabled')
        self.error_details.pack(side='right', padx=(10, 0))
        notice_label = ttk.Label(banner, textvariable=self.notice, style='Muted.TLabel', wraplength=900)
        notice_label.pack(side='left', fill='x', expand=True)
        banner.bind('<Configure>', lambda event: notice_label.configure(wraplength=max(300, event.width - 130)))


    def variable(self, key, boolean=False):
        if key not in self.vars:
            factory = tk.BooleanVar if boolean else tk.StringVar
            self.vars[key] = factory(value=self.store.settings[key])
            self.vars[key].trace_add('write', self.schedule_save)
        return self.vars[key]


    def _settings(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        login = ttk.LabelFrame(parent, text='学习通账号')
        login.grid(row=0, column=0, sticky='nsew', padx=(0, 10), pady=(0, 12))
        ai = ttk.LabelFrame(parent, text='答题 API · OpenAI 兼容协议')
        ai.grid(row=0, column=1, sticky='nsew', pady=(0, 12))
        self.field(login, '手机号', 'username', 0)
        self.field(login, '密码', 'password', 1, secret=True)
        ttk.Checkbutton(login, text='优先使用已保存的 Cookie 登录', variable=self.variable('use_cookies', True)).grid(row=2, column=0, columnspan=2, sticky='w', pady=12)
        self.login_button = ttk.Button(login, text='登录并加载课程', command=self.connect, style='Accent.TButton')
        self.login_button.grid(row=3, column=0, columnspan=2, sticky='ew')
        self.field(ai, 'API 地址', 'endpoint', 0)
        self.field(ai, 'API Key', 'key', 1, secret=True)
        self.field(ai, '模型 ID', 'model', 2)
        self.field(ai, '代理（可选）', 'http_proxy', 3)
        ttk.Label(ai, text='默认：deepseek-flash；AI 仅用于答题。', style='Muted.TLabel').grid(row=4, column=0, columnspan=2, sticky='w', pady=8)
        options = ttk.LabelFrame(parent, text='运行参数')
        options.grid(row=1, column=0, columnspan=2, sticky='ew')
        self.field(options, '视频并发', 'video_workers', 0)
        self.field(options, '答题并发（测验数）', 'answer_workers', 0, column=2)
        self.field(options, '视频倍速（1～2）', 'speed', 1)
        self.field(options, 'AI 请求超时（秒）', 'request_timeout', 1, column=2)
        self.field(options, 'AI 请求最小间隔（秒）', 'min_interval', 2)
        ttk.Label(options, text='API 设置在下一次请求时应用；并发设置在下一轮任务中应用。', style='Muted.TLabel').grid(row=3, column=0, columnspan=4, sticky='w', pady=8)
        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, columnspan=2, sticky='ew', pady=12)
        ttk.Label(actions, text='设置自动保存', style='Muted.TLabel').pack(side='left', padx=(0, 18))
        ttk.Button(actions, text='导入旧版配置', command=self.import_legacy).pack(side='left', padx=10)
        ttk.Button(actions, text='打开数据目录', command=lambda: os.startfile(self.store.directory)).pack(side='left')
        ttk.Label(parent, text=f'固定数据目录：{self.store.directory}\n升级只替换程序文件，设置、题目与记录独立保存。',
                  style='Muted.TLabel', wraplength=1000).grid(row=3, column=0, columnspan=2, sticky='w', pady=8)


    def field(self, parent, label, key, row, secret=False, column=0):
        parent.columnconfigure(column + 1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky='w', padx=(12 if column else 0, 14), pady=6)
        ttk.Entry(parent, textvariable=self.variable(key), show='●' if secret else '').grid(row=row, column=column + 1, sticky='ew', pady=6)


    def schedule_save(self, *args):
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(600, self.save)


    def save(self, notify=False):
        self.save_timer = None
        try:
            values = {key: var.get() for key, var in self.vars.items()}
            for key, low, high in (('video_workers', 1, 16), ('answer_workers', 1, 8), ('request_timeout', 5, 180)):
                values[key] = int(values[key])
                if not low <= values[key] <= high:
                    raise ValueError(f'{key} 必须在 {low}～{high} 之间')
            values['speed'] = float(values['speed'])
            values['min_interval'] = float(values['min_interval'])
            if not 1 <= values['speed'] <= 2 or not 0 <= values['min_interval'] <= 120:
                raise ValueError('视频倍速应为 1～2，请求间隔应为 0～120 秒')
            values['geometry'] = self.root.geometry()
            self.store.save_settings(values)
            self.engine.configure()
            if notify:
                self.notice.set('设置已保存。')
            return True
        except (ValueError, tk.TclError) as exc:
            if notify:
                messagebox.showerror('设置有误', str(exc), parent=self.root)
            return False


    def background(self, function, event):
        def work():
            try:
                self.events.put((event, function()))
            except Exception as exc:
                self.events.put(('error', capture_error(self.store.directory, event, exc, self.store.settings)))
            finally:
                if event == 'courses':
                    self.events.put(('connect_finished', None))
        threading.Thread(target=work, daemon=True).start()


    def connect(self):
        if not self.save(True) or self.connecting:
            return
        if self.engine.busy() or self.engine.manual:
            messagebox.showinfo('任务运行中', '请先取消当前任务，再切换登录账号。')
            return
        self.connecting = True
        self.login_button.configure(state='disabled')
        self.connection.configure(text='正在登录…')
        self.notice.set('正在连接学习通并读取课程；此过程不调用 AI。')
        self.background(self.engine.connect, 'courses')


    def stop(self):
        self.engine.stop()
        self.notice.set('正在取消本轮任务，已有结果保留在历史记录。')


    def import_legacy(self):
        if self.engine.busy() or self.engine.manual:
            messagebox.showinfo('任务运行中', '请先取消当前任务，再导入旧版设置。')
            return
        folder = filedialog.askdirectory(title='选择包含 config.ini / cookies.txt 的旧版目录')
        if not folder:
            return
        try:
            self.store.import_legacy(folder, explicit=True)
            for key, var in self.vars.items():
                var.set(self.store.settings[key])
            self.notice.set('旧版设置已导入并备份；请重新登录。')
        except Exception as exc:
            messagebox.showerror('导入失败', str(exc))


    def close(self):
        if not self.save(True):
            return
        self.engine.stop()
        self.closed = True
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        for token in self.root.tk.call('after', 'info'):
            self.root.after_cancel(token)
        self.root.destroy()


    def show_error(self):
        if self.last_error:
            messagebox.showerror('错误详情', self.last_error['reason'] + '\n\n脱敏诊断记录：' + str(self.store.directory / 'last-error.json'), parent=self.root)


