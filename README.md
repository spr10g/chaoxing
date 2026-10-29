# 学习通批量工作台

Windows 桌面程序，提供独立的视频与 AI 答题队列、多课程选择、跨章节审核和批量操作。基于 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 提供的接口与页面解析实现扩展。

## 下载与运行

前往 [Releases](https://github.com/spr10g/chaoxing/releases) 下载：

- **Windows x64 程序包**：解压后双击 `学习通工作台.exe`。必须保留旁边的 `_internal` 目录；不需要 Python、源码或 CMD。
- **对应源码包**：供开发、检查和自行构建使用，与程序包分开下载，使用相同版本号。

首次运行，在“连接与设置”填写自己的学习通账号和 AI API Key，点击登录并选择课程、章节和任务类型。默认先预览答案，不自动交卷。

## 功能

- 多门课程分别选择章节，或一键选择整门课程。
- 视频与答题独立推进，默认并发分别为 4、2，可配置。
- DeepSeek 及其他 OpenAI 兼容服务接入；AI 只负责答题。
- 文字单选、多选、判断题的解析、结构校验和选项映射。
- 跨课程、跨章节汇总题目，筛选异常、多选、批量保存草稿与交卷。
- 选题后让 AI 重做，跳过缓存、保留历史，结果先待审核。
- 移出指定队列任务，保留已有结果，其他任务继续。
- 设置、登录状态和任务记录独立保存在本机，更新程序后继续沿用。

默认只交卷完整且校验通过的测验。“校验通过”不等于答案知识一定正确。用户可以明确选择含空题交卷，平台是否接受及后续规则以平台返回为准。图片、暂不支持的题型和无法可靠识别的题目会保留原因。

## 设置与个人数据

固定数据目录为 `%LOCALAPPDATA%\ChaoxingDesktop`。账号、API Key、Cookie 和数据库仅保存于运行者本机，当前为未加密存储。

本仓库及发行包不包含维护者的登录数据、真实配置或数据库。升级时关闭程序、替换程序目录，保留用户数据目录；请勿将自己的数据目录提交到 Git。

DeepSeek 的 OpenAI 兼容地址为 `https://api.deepseek.com`，默认模型 `deepseek-flash`。不要填写 `/anthropic` 或完整 `/chat/completions` 接口路径。

详细操作见 [用户指南](docs/USER_GUIDE.md)。

## 开发与测试

使用 **Python 3.12 x64、Windows**：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe desktop_main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tests\ui_layout.py
```

```powershell
powershell -File scripts\build.ps1 -Python .\.venv\Scripts\python.exe
```

构建产物位于 `dist/`，不会覆盖日常使用的程序。`requirements.lock.txt` 固定已验证的运行依赖；构建版本从 `desktop.__version__` 读取。GUI 启动、测试和源码导入均不自动登录或执行真实课程任务。

## 源码结构

```text
desktop/       界面、双队列、AI、审核与本地数据
api/           复用并调整的平台接口与页面解析
resource/      必需字体映射表
tests/         离线回归与布局测试
scripts/       构建与打包
docs/          使用、开发及上游说明
chaoxing.spec  PyInstaller 目录版构建配置
```

## 许可与来源

沿用 GNU GPL v3，见 [LICENSE](LICENSE)。复用代码保留原作者署名，详见 [NOTICE](NOTICE.md) 与 [上游说明](docs/UPSTREAM.md)。每次发布同时提供与可执行程序对应的源码下载。
