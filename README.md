# 学习通批量工作台

[下载程序](https://github.com/spr10g/chaoxing/releases/latest) · [使用指南](docs/USER_GUIDE.md) · [更新记录](CHANGELOG.md)

## 项目简介

学习通批量工作台是一款 Windows 桌面应用，支持多课程任务管理、视频与 AI 答题独立执行、跨章节答案审核及批量操作。

通过图形界面选择课程、章节和任务类型，集中查看执行进度、整理答案并管理队列。

## 下载与安装

当前版本：**v0.2.0**，适用于 **Windows x64**。

| 下载项 | 用途 |
| --- | --- |
| [Windows 程序包](https://github.com/spr10g/chaoxing/releases/download/v0.2.0/chaoxing-v0.2.0-windows-x64.zip) | 解压后直接运行 |
| [对应源码包](https://github.com/spr10g/chaoxing/releases/download/v0.2.0/chaoxing-v0.2.0-source.zip) | 开发、阅读源码与自行构建 |

1. 下载 Windows 程序包。
2. 完整解压，保持 `学习通工作台.exe` 与 `_internal` 目录位于同一文件夹。
3. 双击 `学习通工作台.exe` 启动。

历史版本见 [版本发布页](https://github.com/spr10g/chaoxing/releases)。

## 功能概览

| 功能 | 说明 |
| --- | --- |
| 多课程管理 | 同时勾选多门课程，按章节或整门课程添加任务 |
| 双队列执行 | 视频与答题分别调度，独立推进，支持配置并发数 |
| AI 辅助答题 | 接入 DeepSeek 等 OpenAI 兼容服务，处理文字单选、多选和判断题 |
| 跨章节审核 | 汇总多个课程、章节的题目，按题目状态筛选与多选 |
| 批量操作 | 批量保存正常题草稿、提交可交卷章节、让 AI 重做异常题 |
| 队列管理 | 分别暂停、恢复视频与答题队列，批量移出任务并保留已有结果 |
| 设置与记录 | 自动保存连接设置、登录状态、任务进度及答案历史 |

## 使用说明

1. **连接账号**：在“连接与设置”填写学习通账号，点击“登录并加载课程”。
2. **配置 AI**：填写 API 地址、API Key 和模型 ID。
3. **选择任务**：在“任务与进度”勾选课程及章节，选择视频、AI 答题或两者，点击“启动所选课程”。
4. **审核答案**：在“答案审核”集中查看题目，筛选异常题、修改答案或执行 AI 重做。
5. **批量处理**：选择题目或章节，保存草稿或提交可交卷章节。

程序默认的 AI 连接配置：

| 配置项 | 默认值 |
| --- | --- |
| 接口协议 | OpenAI 兼容协议 |
| API 地址 | `https://api.deepseek.com` |
| 模型 ID | `deepseek-flash` |

视频与答题的默认并发数分别为 **4** 和 **2**，可在“连接与设置”调整。

设置与任务数据保存在 `%LOCALAPPDATA%\ChaoxingDesktop`。升级时关闭程序、替换程序目录，继续沿用此数据目录。

详细操作与任务状态说明见 [使用指南](docs/USER_GUIDE.md)。

## 开发与构建

项目使用 **Python 3.12 x64** 和 **Tkinter** 开发，通过 **PyInstaller** 构建 Windows 程序包。

| 目录或文件 | 内容 |
| --- | --- |
| `desktop/` | 桌面界面、任务调度、AI 答题、审核与数据存储 |
| `api/` | 平台接口与页面解析 |
| `resource/` | 字体映射资源 |
| `tests/` | 功能与界面布局测试 |
| `scripts/` | 构建脚本 |
| `docs/` | 使用与开发文档 |

环境安装、源码运行及打包命令见 [开发说明](docs/DEVELOPMENT.md)。

## 许可与致谢

本项目采用 [GNU GPL v3](LICENSE) 许可证。

项目基于 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 扩展，感谢上游作者及 SocialSisterYi 等贡献者。代码与资源来源见 [来源与署名](NOTICE.md)，上游项目资料见 [上游说明](docs/UPSTREAM.md)。
