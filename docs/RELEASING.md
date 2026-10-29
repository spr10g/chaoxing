# 新版本同步到 GitHub

仓库：https://github.com/spr10g/chaoxing

版本同步分为两部分：源码推送到 `main`，程序包与对应源码包发布到 Releases。下面以 **v0.3.3** 为例，每次替换为实际版本号。

## 1. 更新版本信息

- 修改 `desktop/__init__.py` 中的 `__version__`。
- 在 `CHANGELOG.md` 添加本次修改。
- 更新 `README.md` 中的当前版本和下载链接。

## 2. 构建 EXE

在 PowerShell 中进入正式源码目录，使用现有开发环境：

```powershell
Set-Location -LiteralPath 'C:\学习通\chaoxing'
powershell -File scripts\build.ps1 -Python .\.venv\Scripts\python.exe
```

程序输出到 `dist/ChaoxingDesktop-v0.3.3-windows-x64/`。整个目录包含 EXE、`_internal` 和使用文档。

## 3. 提交源码与版本标签

```powershell
git add --all
git commit -m "发布学习通批量工作台 v0.3.3"
git push origin main
git tag -a v0.3.3 -m "学习通批量工作台 v0.3.3"
git push origin v0.3.3
```

源码版本由该标签确定，随后从相同标签导出源码包。

## 4. 生成两个下载包

```powershell
$releaseVersion = '0.3.3'
$projectPath = 'C:\学习通\chaoxing'
$releasePath = 'C:\学习通\releases'
New-Item -ItemType Directory -Path $releasePath -Force | Out-Null

Compress-Archive -LiteralPath "$projectPath\dist\ChaoxingDesktop-v${releaseVersion}-windows-x64" -DestinationPath "$releasePath\chaoxing-v${releaseVersion}-windows-x64.zip"
git archive --format=zip --prefix="chaoxing-v${releaseVersion}-source/" -o "$releasePath\chaoxing-v${releaseVersion}-source.zip" "v${releaseVersion}"
```

- `windows-x64.zip`：完整的可运行程序目录。
- `source.zip`：对应标签的源码、构建配置、文档与许可证。

账号与任务数据保存在 `%LOCALAPPDATA%\ChaoxingDesktop`；程序包从构建目录打包，源码包从 Git 标签导出。

## 5. 在 GitHub 发布

1. 打开仓库的 [Releases](https://github.com/spr10g/chaoxing/releases)。
2. 点击 **Draft a new release**（新建发布）。
3. 在 **Choose a tag** 中选择刚推送的 `v0.3.3`。
4. 填写中文标题，例如“学习通批量工作台 v0.3.3”。正文只列本次更新内容与要点，使用简短中文条目。
5. 上传上述程序 ZIP 和源码 ZIP。
6. 将本版设为最新版，点击 **Publish release**（发布）。

发布正文示例：

```markdown
- 答题结果自动保存本地草稿，支持一次上传答案并交卷。
- 合并测验状态，统一筛选和状态含义。
- 交卷成功后清理本地草稿，保留简要记录。
```

安装与使用说明统一维护在 `README.md` 和 `docs/USER_GUIDE.md`。

发布页面另外显示的 `Source code (zip)` 和 `Source code (tar.gz)` 由 GitHub 自动生成。

发布页面操作参见 [GitHub 官方说明](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。

## 6. 整理本地目录

- 最新 EXE 放入独立版本目录，继续使用本机原有数据目录。
- `releases/` 保留本次两个 ZIP，旧发布包移到 `archive/releases/<版本>/`。
- 旧版程序关闭后归档，清理重复展开目录和本次构建、诊断、缓存及临时工具。
- 更新 `C:\学习通\目录说明.md`。

日常交给助手处理时，可以直接说：**“将当前版本同步到 GitHub，并整理目录。”**
