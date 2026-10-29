# 开发说明

唯一正式入口为 `desktop_main.py`，`main.py` 保留为兼容调用。版本号在 `desktop/__init__.py`。

支持 Python 3.12 x64、Windows。依赖分为运行范围文件、已验证的锁定文件和构建依赖。构建脚本只输出到 `dist/`，不操作安装目录或正式用户数据。

## 验证

- `python -m unittest discover -s tests -v`：离线模拟平台和 AI，验证并发、取消、批量操作、提交检查、状态恢复及设置保存。
- `python tests/ui_layout.py`：检查两个窗口尺寸下的控件边界、关键表格视口、滚动和选择保存。
- 打包后可在独立数据目录设置 `CHAOXING_DESKTOP_DATA`、`CHAOXING_SMOKE_BACKEND=1` 与 `CHAOXING_SMOKE_EXIT_MS=700`，验证启动、TLS 上下文和字体资源。不设置这些变量时正常独立运行。

测试不自动读取正式账号配置，不真实登录，不调用付费 API，不提交课程题目。旧的个人登录诊断与原版 EXE 基准脚本未纳入公共仓库。

## 发布

同一版本提供 Windows x64 目录包和对应源码包。程序 ZIP 中保留 `_internal`，源码 ZIP 不含 `.git`、`.venv`、构建产物、诊断、配置、Cookie 或数据库。

已发出的交卷请求无法撤回，结果不明确时保存待核验状态；禁止自动盲目重交。允许含空题交卷仅限用户明确选择的那一次手动操作，不能影响自动队列的完整性检查。

旧 CLI、未使用的 Flask/Celery、通知、旧题库和验证码模块不作为本项目维护入口。上游资料与署名保留，未重建上游历史。
