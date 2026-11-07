# dede工作台

一个**本机 Agent 编排器**：用网页聊天界面调度电脑上已安装的 **Codex CLI** 和 **DeepSeek Harness**，可以单独使用其中一个，也可以组合成“Codex 规划 → Harness 执行 → Codex 独立审核”的流程。

- 纯 Python 标准库，没有第三方依赖；前端是一个内联的 HTML 页面。
- 不直接调用模型 API，也不读取或复制任何凭据；模型和登录状态都沿用两个 CLI 已有的配置。
- 只监听 `127.0.0.1`，每次启动生成随机访问令牌。

> 这是一个学习项目，用来实践 Agent 编排。完整的实践笔记见 [`docs/agent-orchestration-notes.md`](docs/agent-orchestration-notes.md)。

## 功能

| 功能 | 说明 |
|---|---|
| Study | 一问一答，可在 Harness / Codex 之间切换，切换时自动把对方没看到的聊天记录带过去；每个对话有自己的文件夹，产出放在 `output\` |
| Work | 在选定的项目文件夹里执行任务：组合模式 / 仅 Harness / 仅 Codex |
| 引导 | 任务运行中可以发消息：排队（这一轮结束后带上）或“打断并发送”；不计入修补次数 |
| 审核与修补 | 组合模式下审核不通过最多修补 3 次；运行故障单独计数并暂停，不会无限重试 |
| 自动验收 | 根据项目文件识别测试命令（unittest / pytest / npm test / go test / cargo test），由工作台运行，结果交给审核者 |
| 权限 | 一般权限（只能写项目文件夹）/ 全部权限（需要确认）；被沙箱拦住时可以只为一轮放开 |
| 过程展示 | 每一轮可展开查看运行的命令、读写的文件、输出片段和 token 用量；模型的 thinking 不显示也不保存 |
| 调度 | Work 最多同时运行 3 个，Study 单独通道，同一文件夹同一时间只运行一个任务 |
| 输出上限 | 提示词要求大文件分批写；模型输出被截断时自动续写 1 次 |
| 产物与附件 | 临时文件单独折叠并可一键清理（只删本任务新建的）；产物卡片（双击默认打开、右键打开方式 / 在资源管理器中显示），图片缩略图和内联预览；拖入、粘贴或选择附件，Codex 可识图 |

## 环境要求

- Windows 10/11（沙箱、文件夹选择和进程管理依赖 Windows API；其他系统未测试）
- Python 3.11+（带 tkinter，用于原生“选择文件夹”窗口）
- 至少安装并登录好其中一个：
  - [Codex CLI](https://github.com/openai/codex)：`npm i -g @openai/codex`，然后运行一次 `codex` 完成登录；
  - DeepSeek Harness 桌面版，并在桌面端选好默认模型。

## 安装与启动

```powershell
git clone https://github.com/<你的用户名>/dede-workbench.git
cd dede-workbench
python -m unittest discover -s tests -q        # 可选：自检，不会调用模型
python -m agent_workbench serve --open         # 启动并打开浏览器
```

浏览器打开 `http://127.0.0.1:8765/`。关闭后台：点页面左下角“停止工作台”。只关闭浏览器标签页不会停止后台。

### 桌面快捷方式（隐藏启动、重复点击复用同一个服务）

```powershell
powershell -ExecutionPolicy Bypass -File .\create-desktop-shortcut.ps1
```

之后双击桌面的“dede工作台”即可；也可以直接双击 `start.cmd`。

### 配置（环境变量，均可选）

| 变量 | 作用 | 默认 |
|---|---|---|
| `WORKBENCH_HARNESS_INSTALL` | DeepSeek Harness 安装目录 | `D:\applications\dsh-desktop-guanfang` |
| `WORKBENCH_CODEX_EXE` | Codex 可执行文件路径 | 从 PATH 中查找 |
| `WORKBENCH_STUDY_ROOT` | Study 对话文件夹的根目录 | 有 D 盘时为 `D:\dede工作台`，否则为用户目录下的 `dede工作台` |
| `DSH_HOME` | Harness 配置目录 | `%USERPROFILE%\.dsh` |

数据（任务记录、运行日志）保存在项目目录的 `.agent-workbench\` 下，已被 `.gitignore` 排除。

## 关于“部署”

这个项目**设计上只在本机运行**：它要调用你电脑上的 Codex / Harness、读写你本地的项目文件，所以不适合部署到云服务器给别人访问。

- 服务只绑定 `127.0.0.1`，并校验 `Host`、`Origin` 和访问令牌，**不要**改成监听 `0.0.0.0` 或暴露到公网。否则别人就能用你的账号和额度在你的电脑上执行命令。
- “部署”在这里就是上面的安装步骤，加上桌面快捷方式。
- 想让其他人使用，就让对方在他自己的电脑上 clone 并安装两个 CLI。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `SetNamedSecurityInfoW failed (Win32 5)` | Harness 沙箱需要对项目文件夹拥有“完全控制”。D 盘根目录下新建的文件夹通常只有“修改”权限。换用用户目录下的文件夹，或在“属性 → 安全”里给自己加“完全控制”。页面会提前检测并提示 |
| `max-tokens` | 模型单次输出超过上限（思考和回答共用输出额度）。工作台会自动续写 1 次；仍失败时，发一句引导让它拆小步骤、分文件写，再点“恢复当前阶段” |
| Codex 规划时出现“工具失败” | 多半是 `rg` 搜索没有匹配（退出码 1），不影响结果 |
| 审核显示“无法验证” | 审核者在只读环境里跑不了测试。开启自动验收后点“重新审核”，不计入修补次数 |

## 学到的东西（摘要）

1. **多 Agent 不等于更快**：短任务用单个 Agent 加自动验收最快；规划加独立审核适合长任务、改错代价高的任务。
2. **审核需要客观证据**：只读的审核者跑不了测试，就只能保守地判“不通过”，修补会空转。应该由编排器运行测试，再把结果交给审核者。
3. **中途报错不等于失败**：Agent 改错后重试很常见，只有最终的验收命令失败才应该一票否决。
4. **先量再改**：Codex 慢不是因为 MCP，而是它在回答完之后还要花约 11 秒退出；在收到完成事件 1.5 秒后结束进程，单轮从 21 秒降到 11 秒，会话不受影响。
5. **输出有上限**：小任务被截断就续写，大任务先拆分。

## 测试

```powershell
python -m unittest discover -s tests -q
```

104 项测试，全部使用假的 Agent，不会调用真实模型。`tools/` 下的脚本会进行真实调用，会消耗额度。

## 许可

MIT，见 [LICENSE](LICENSE)。
