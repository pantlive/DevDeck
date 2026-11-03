# DevDeck

本地开发项目中枢：在一个网页里管理 WSL / Linux 上的前后端开发服务。

## 启动

使用 conda 创建环境，无需第三方 Python 或前端依赖：

```bash
conda env create -f environment.yml
conda activate devdeck
python -m devdeck
```

打开 <http://localhost:7310>。已有 Python 3.11+ 环境也可直接运行 `python -m devdeck`。

## MVP 功能

- 添加项目，为前端和后端分别配置工作目录、启动命令、可选端口，支持仅配置一个服务。
- 单独或一键启动、停止、重启服务；项目卡片上的“全部重启”会重启该项目的所有服务。
- 页面右上角一键重启 DevDeck，自动等待控制台恢复并刷新页面。
- 展示进程状态、PID、退出码，每 2 秒刷新。
- 查看自动刷新的服务日志（最近 64 KB），通过端口链接访问应用。
- 保存项目配置；删除配置时停止服务，不删除项目源文件。
- 端口管理：查看本机 TCP 监听、IPv4/IPv6 地址、PID、进程名和所属项目，支持搜索与停止托管服务。
- 检查端口冲突，修改停止服务的端口和命令，通过 `{port}` 或 `PORT` 将端口传给应用。
- 识别服务目录所属的 Git 仓库，列出本地和已获取的远程跟踪分支，按最后一次提交时间标出最近更新的分支；停止服务后可切换分支再启动。
- 控制台自身默认使用 `feat` 分支，页面右上角可切换 `feat`、`dev` 或 `main` 并重启；不包含控制台入口代码的分支不会列出，避免切到只有 README 的旧 `main`。

例如，前端目录填写 `/home/yourname/projects/demo/web`，命令填写 `npm run dev -- --host 127.0.0.1`，端口填写 `5173`；后端目录填写 `/home/yourname/projects/demo/api`，命令填写 `conda run --no-capture-output -n my-api python -u app.py`，端口填写 `8000`。

请先在各项目中安装好依赖。命令通过 `bash -lc` 执行，继承控制台环境；需要特定 conda 环境时使用 `conda run --no-capture-output -n 环境名 ...`。命令应以前台方式运行，不要使用 `nohup`、`&` 或自行 daemonize。Python 服务建议添加 `-u`，避免日志缓冲。

## 端口管理

页面下方的端口列表展示当前 WSL/Linux 网络环境中的 TCP 监听，每 2 秒刷新。可按端口、PID、进程名或项目名称搜索。通过监听进程的进程组识别托管服务，点击“停止服务”会停止该服务的整个进程组；未托管进程只展示信息。进程信息不可读取时显示“未知 / 无权限查看”。

服务卡片分别显示启动进程状态和端口监听状态：本服务已监听、被其他进程占用或归属未知、未监听、检测不可用。进程运行但端口未监听时，可能仍在启动，也可能实际使用了其他端口；可结合日志和下方端口列表排查。TCP 监听不等于应用健康检查通过。

停止服务后点击“配置端口”，可同时修改端口和启动命令。推荐使用 `{port}` 占位符，例如：

```bash
# Vite 项目：固定使用配置端口，冲突时不自动跳到下一个端口
npm run dev -- --host 127.0.0.1 --port {port} --strictPort

# Python 静态文件服务
python -u -m http.server {port} --bind 127.0.0.1
```

DevDeck 在启动时替换 `{port}`，并设置 `PORT` 环境变量。应用需要支持相应参数或读取该环境变量。已固定在命令或项目配置文件中的端口不会自动改写。端口留空则不做端口管理；使用 `{port}` 时必须填写端口。

启动前检查 IPv4/IPv6 占用、批量启动中的重复端口，以及正在启动但尚未监听的其他托管服务预留端口；发现冲突时提示端口和可读取的进程信息。允许保存重复端口配置以便交替运行，但会提示，且禁止同时启动。为避免误绑定，只要本网络环境中任意地址已有相同 TCP 端口，就按冲突处理。检查与应用绑定之间仍有时间窗口，最终是否启动成功以状态和日志为准。

端口检测基于 Linux `/proc`，不包含 Windows 主机独立进程、其他网络命名空间或 UDP 端口。不自动修改防火墙或 WSL 端口转发。

## 数据与生命周期

默认配置位于当前工作目录下的 `.devdeck/projects.json`，日志位于 `.devdeck/<服务 ID>.log`，均已加入 Git 忽略规则。日志追加写入，首版尚无自动轮转，需要自行清理停止服务的日志。

```bash
python -m devdeck --port 7310 --data-dir /home/yourname/.local/share/devdeck
```

默认只监听 `127.0.0.1`，用于可信的本机单用户环境。工作目录填写 WSL/Linux 内的绝对路径。“运行中”表示启动进程仍存活。

按 Ctrl+C 或发送 SIGTERM 会停止托管进程组。关闭浏览器不会停止服务。重新启动控制台会恢复配置，但不会自动启动项目。首版不把各个项目服务注册为 systemd；控制台本身在 Windows 登录启动时使用用户级 systemd。不支持强制杀死控制台后的进程恢复、项目名称和目录编辑、Windows 原生进程管理、Docker 管理。

## Git 分支

服务工作目录位于 Git 仓库内时，项目卡片会显示当前分支。最近更新的分支按本地分支和已获取的远程跟踪分支的最后一次提交时间计算；当前分支不是最新时会提示分支名。点击分支可在停止服务后切换：工作区有已跟踪的未提交改动时会拒绝切换，以免覆盖本地修改。未跟踪文件不阻止切换，但如果会挡住检出，Git 仍会失败。

选择分支后会记入项目配置，下次启动先检出该分支再运行命令，并设置 `DEVDECK_BRANCH` 环境变量。同一 Git 仓库的前后端必须选择同一分支。没有配置分支时，沿用目录里当前检出的内容启动。

控制台自己的代码仓库另有 feat / dev / main 切换：尚未选择时页面把 `feat` 标为默认，不会在启动时强行改当前分支；选定后写入 `.devdeck/console.json`，下次启用会先检出该分支再提供页面。切换会停止托管服务并重启控制台。DevDeck 自身仓库作为项目添加时也只提供 `feat`、`dev`、`main` 三个分支，且只保留真正包含控制台入口代码的分支（例如当前仓库只有 README 的旧 `main` 会被排除），不会列出或切换到 `agents/` 任务分支。任务 worktree（`agents/` 分支）上运行时拒绝切换，以免改掉执行中的 Agent 目录。工作区有未提交改动、或目标分支已被其他 worktree 占用时，与项目分支切换相同，会直接失败。

远程是否有更新取决于本机已经 `fetch` 到的跟踪引用。对话框里的「刷新远程」会执行 `git fetch --all --prune`，不会改当前分支；未配置远程、需要交互式凭据或超时时会提示失败。DevDeck 不会自动 fetch，也不会用 `git reset --hard` 或强制检出丢弃改动。远程分支没有本地同名分支时，切换会创建跟踪分支。分支已被其他 worktree 占用、或仓库 hooks 被忽略（切换时禁用）时，以 Git 返回的错误为准。

点击右上角“重启 DevDeck”会先停止所有托管服务，再使用原 Python 解释器、启动参数、环境和工作目录重启控制台，兼容终端启动和 systemd 登录自启。项目配置和日志保留，项目服务需手动再次启动。重启期间页面暂停操作，确认新实例就绪后自动刷新；等待超过 60 秒会显示恢复提示，可手动刷新页面，仍无法连接时从终端重新启动 DevDeck。首次更新到此版本后，需要手动重启已有控制台一次以加载重启接口。

## Windows 登录启动

DevDeck 运行在 WSL 时，可以通过 Windows 任务计划程序在用户登录后自动后台启动控制台。注册脚本只启动 DevDeck 本身，不会自动启动已保存的项目服务；启动输出写入 `.devdeck/devdeck-startup.log`。

不要在 WSL 里用 `nohup` 或 `systemd-run --no-block` 然后立刻退出 `wsl.exe`：用户 systemd 会进入 `exit.target`，刚拉起的服务会被停掉。脚本会安装并 enable 持久的用户单元 `devdeck.service`（`WantedBy=default.target`），再留一个隐藏的 `wsl.exe` 会话防止用户 systemd 退出：

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File "$(wslpath -w scripts/windows/register-startup.ps1)" \
  -Distro Ubuntu \
  -LinuxProjectDir /home/lsl/project/github/DevDeck \
  -Python /home/lsl/miniconda3/bin/python \
  -Port 7310
```

脚本可以重复执行，后一次配置会覆盖同名任务。取消登录启动：

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File "$(wslpath -w scripts/windows/unregister-startup.ps1)"
```

## 验证

```bash
python -m unittest discover -s tests -v
```

测试覆盖真实服务启停、重复启动、端口释放、子进程停止、配置恢复、HTTP 来源限制、IPv4/IPv6 占用检测、绑定未监听的端口冲突、批量启动重复端口、启动中的端口预留、端口修改、占位符和环境变量生效、Git 分支列表与最近更新检测、脏工作区拒绝切换、按配置分支启动、远程跟踪分支检出、控制台默认 feat 与页面切换 feat/dev/main、控制台仓库过滤 agents 分支和缺少代码的旧 main、任务 worktree 拒绝切换控制台分支、控制台真实自重启与进程清理，以及登录自启使用的持久 systemd 用户单元。
