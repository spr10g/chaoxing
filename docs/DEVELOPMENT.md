# 开发说明

程序入口为 `desktop_main.py`，`main.py` 提供兼容调用。版本号定义在 `desktop/__init__.py`。

## 环境与依赖

开发环境为 Windows、Python 3.12 x64。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

`requirements-desktop.txt` 定义运行依赖范围，`requirements.lock.txt` 固定运行依赖版本，`requirements-build.txt` 包含构建依赖。

## 启动与构建

运行源码：

```powershell
.\.venv\Scripts\python.exe desktop_main.py
```

构建 Windows 程序包：

```powershell
powershell -File scripts\build.ps1 -Python .\.venv\Scripts\python.exe
```

产物位于 `dist/ChaoxingDesktop-v<版本号>-windows-x64/`，包含 `学习通工作台.exe`、`_internal` 和说明文档。构建配置为 `chaoxing.spec`。

## 测试命令

- `python -m unittest discover -s tests -v`：离线模拟平台和 AI，验证并发、取消、批量操作、提交检查、状态恢复及设置保存。
- `python tests/ui_layout.py`：检查两个窗口尺寸下的控件边界、关键表格视口、滚动和选择保存。
- 打包后可在独立数据目录设置 `CHAOXING_DESKTOP_DATA`、`CHAOXING_SMOKE_BACKEND=1` 与 `CHAOXING_SMOKE_EXIT_MS=700`，验证启动、TLS 上下文和字体资源。不设置这些变量时正常独立运行。

## 发布

同一版本分别提供 Windows x64 程序包和对应源码包。程序包保留完整的 EXE 与 `_internal` 目录，源码包从对应版本标签导出。

已发出的交卷请求无法撤回，结果不明确时保存待核验状态；禁止自动盲目重交。允许含空题交卷仅限用户明确选择的那一次手动操作，不能影响自动队列的完整性检查。

上游资料与署名见 `NOTICE.md`、`docs/UPSTREAM.md` 和各模块说明。
