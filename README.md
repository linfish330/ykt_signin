# 雨课堂助手 Web 版

- 基于 [RainClassroomAssitant](https://github.com/TrickyDeath/RainClassroomAssitant) 和 [THU-Yuketang-Helper](https://github.com/zhangchi2004/THU-Yuketang-Helper)

## 功能

- **自动签到** — 自动完成签到（**模拟通过 APP 扫二维码进入课堂**），支持设置发现课堂后的签到延迟
- **自动答题** — 支持单选、多选、投票、填空和简答题，可配置答题策略（AI、随机或关闭）；AI 和随机候选答案都会先在仪表盘等待确认
- **自动弹幕** — 当一段时间内出现超过特定条数的相同弹幕时自动跟发
- **自动抢红包** — 收到红包时自动抢
- **点名提醒** — 点名时发送通知提醒
- **语音通知** — 支持语音播报课程事件
- **手机推送** — 支持 PushDeer 将课堂事件推送到手机（支持自建服务器）
- **分课程设置** — 对每门课程进行精细化的自动化控制
- **多服务器支持** — 支持多个雨课堂服务器
- **多账号支持** — 支持同时登录多个账号
- **多平台支持** — 提供 Windows、macOS、Linux 可执行文件，支持 Python 源码运行和 Docker 部署
- **双语界面** — 支持中英文切换

## 签到来源

设置中的“签到来源”是进入课堂请求发送给雨课堂的 `source` 整数值。当前可选值来自公开客户端和社区实现的观察，并非雨课堂官方公开 API 枚举：

- `5` — 微信/小程序
- `14` — PC / Web
- `21` — 二维码（默认推荐）
- `22` — 暗号
- `23` — APP 点击课堂
- `1` — 自定义

每个账号有一个全局默认值，每门课程可以选择继承默认值或单独覆盖。旧版 `config.json` 没有该字段时仍使用 `21`。

## 动态二维码签到

登录后，在仪表盘的“动态二维码签到”中粘贴二维码内容或 URL：

`二维码内容 → /api/v3/app/scan → lessonId → /api/v3/lesson/checkin(source=21) → 现有课堂 WebSocket`

请求会使用当前账号配置的 domain、session 和 headers；二维码内容中的 host 不会改变请求目标。该功能遵循雨课堂服务端正常的二维码验证流程，不用于绕过服务器校验。若 Monitor 已经管理同一课堂，会复用已有 Lesson/WebSocket，不重复创建课堂连接。

为兼容主项目原有自动模式，Monitor 自动签到继续使用原请求字段；只有用户明确发起的 QR 入口会显式携带 `joinIfNotIn=true`。

## 签到延迟与 AI 答题确认

在设置页的“监听设置”中可配置“签到延迟”（0–300 秒）。自动发现新课堂后，助手会等待该时间再发起签到；手动动态二维码签到不受此延迟影响。

首页“全局答题模式”会覆盖所有课程的题型设置：AI 使用 DeepSeek，随机模式对单选、多选、投票随机选择，对填空和简答填入“1”，关闭则不答题。收到题目后仪表盘会立即弹出题目、题目截图和选项；AI 返回答案或随机候选生成后，弹窗会实时补充对应答案。AI 和随机模式可点击“确认作答”立即提交；若截止前 5 秒仍无人处理，自动使用随机规则作答并提交。关闭模式只显示题目并等待关闭。

## 快速开始

0. 在 GitHub 上为本项目点 Star（可选）

### 方式一：可执行文件（普通用户推荐）

1. 前往 [Releases 页面](https://github.com/linfish330/ytk_signin/releases)，下载对应平台的可执行文件（如有发布）
2. 运行可执行文件，浏览器会自动打开 <http://localhost:8500>

> [!TIP]
> macOS/linux 用户需先运行 `chmod +x YuketangHelper-<OS>-<VERSION>`（自行补全文件名）; macOS 用户如遇安全提示请前往 **系统设置 → 隐私与安全性** 点击"仍要打开"

### 方式二：Python（开发者推荐）

1. [下载源代码 ZIP](https://codeload.github.com/linfish330/ytk_signin/zip/refs/heads/main) 并解压，或使用 Git Clone
1. 安装 [Python 3](https://www.python.org/) 和 [Node.js](https://nodejs.org/)
1. 在项目根目录下运行：

   ```zsh
   python start.py
   ```

1. 在浏览器中打开 <http://localhost:5173> 即可使用

也可以在终端直接输入 `ykt_signin` 启动前后端并自动打开 UI。

### 方式三：Docker（服务器部署推荐）

1. 下载并安装 Docker
1. 打开 Docker 并拉取镜像 `docker pull dvdyyz/yuketang-helper:latest`
1. 运行：

   ```zsh
   docker run -d --name yuketang-helper --restart unless-stopped -p 8500:8500 -v yuketang-data:/data dvdyyz/yuketang-helper:latest
   ```

1. 在浏览器中打开 <http://localhost:8500> 即可使用

> [!TIP]
> 服务器部署可通过端口转发在本地浏览器访问；
> 服务器推荐使用账号密码登录，以获得更长久的登录状态；
> 如需24小时服务器运行，可联系作者。

## 停止

- **可执行文件**：关闭终端窗口
- **Python**：运行 `python stop.py`
- **Docker**：运行 `docker stop yuketang-helper`

## 获取 AI API密钥（免费）

### DeepSeek（推荐）

在设置页的 AI 设置中只需填写 DeepSeek API Key 并点击“保存并启用”。应用会自动使用 DeepSeek 的 OpenAI 兼容接口和视觉模型处理题目图片，不需要填写模型名或接口地址。

- [DeepSeek API Keys](https://platform.deepseek.com/api_keys)

- **ModelScope**: 登录 [ModelScope](https://modelscope.cn/)，前往[访问控制](https://modelscope.cn/my/access/token)，点击 **新建访问令牌**

> [!IMPORTANT]
> 使用 ModelScope API 需同时满足以下两点，否则即使创建了访问令牌也无法调用模型 API，本助手会调用失败：
>
> 1. **在 ModelScope [账号设置](https://modelscope.cn/my/settings/account) 中绑定阿里云账号**
> 2. **完成实名认证**

- **Google**: 登录 [Google AI Studio](https://aistudio.google.com/)，进入 [Get API Key page](https://aistudio.google.com/api-keys)，点击 **Create API Key**

## 待办

- [ ] 支持多种 LLM API
- [ ] 支持填空题答题
- [ ] 自动预习
- [ ] 自动刷回放

---

# Yuketang Helper Web

- Based on [RainClassroomAssitant](https://github.com/TrickyDeath/RainClassroomAssitant) and [THU-Yuketang-Helper](https://github.com/zhangchi2004/THU-Yuketang-Helper)

## Features

- **Auto Sign-in** — Automatically checks in (**simulates scanning the QR code via the app to enter the classroom**) with a configurable delay after lesson discovery
- **Auto Quiz Answering** — Handles single/multiple choice, voting, fill-in-blank, and short-answer questions with configurable strategies (AI, random, or off); AI and random candidates wait for dashboard confirmation
- **Auto Danmu** — Automatically follows up when more than a configured number of identical danmu appear within a period of time
- **Auto Red Packet** — Automatically grabs red packets when received
- **Roll Call Notifications** — Alerts you when roll call happens
- **Voice Notifications** — Text-to-speech announcements for lesson events
- **Mobile Push** — Push lesson events to your phone via PushDeer (self-hosted servers supported)
- **Per-Course Settings** — Fine-grained control over automation for each course
- **Multi-Server Support** — Supports multiple Yuketang servers
- **Multi-Account Support** — Supports logging in with multiple accounts simultaneously
- **Multi-Platform Support** — Provides Windows, macOS, and Linux executables, with support for Python source and Docker deployment
- **Bilingual UI** — English and Chinese interface

## Check-in Source

The “Check-in Source” setting is the integer `source` sent with the classroom entry request. These values are observed in public clients and community projects; they are not an official public Yuketang API enum:

- `5` — WeChat / Mini Program
- `14` — PC / Web
- `21` — QR code (recommended default)
- `22` — Passcode
- `23` — APP classroom button
- `1` — Custom

Each account has a default source, and each course can inherit it or override it. Older `config.json` files without this field continue to use `21`.

## Dynamic QR Check-in

After signing in, paste QR content or a URL into “Dynamic QR Check-in” on the dashboard:

`QR content → /api/v3/app/scan → lessonId → /api/v3/lesson/checkin(source=21) → existing classroom WebSocket`

The request uses the selected account's domain, session, and headers; the host inside the QR value does not change the request destination. This follows Yuketang's normal server-side QR validation flow and is not intended to bypass server verification. If Monitor already manages the same lesson, the existing Lesson/WebSocket is reused instead of creating another connection.

To preserve the existing automatic mode, Monitor keeps its legacy check-in fields; only an explicit QR entry opts into `joinIfNotIn=true`.

## Check-in Delay and AI Answer Review

In Settings → Monitor Settings, configure “Check-in Delay” from 0 to 300 seconds. Automatic check-in waits for this duration after discovering a new lesson; manual dynamic QR check-in is not delayed.

The dashboard’s “Global Answer Mode” overrides each course’s quiz mode: AI uses DeepSeek, Random randomly selects options for choice/voting questions and enters “1” for fill-in/short-answer questions, and Off skips answering. The dashboard immediately shows the question, screenshot, and options; the candidate answer is added when AI or the random policy is ready. DeepSeek failures automatically downgrade to a random candidate. AI and random modes support “Confirm answer”; if nobody responds by the final 5 seconds, the random policy submits automatically. Off only shows the question and never submits it.

## Quick Start

0. Star this project on GitHub (optional)

### Option 1: Executable (Recommended for Ordinary Users)

1. Go to the [Releases page](https://github.com/linfish330/ytk_signin/releases) and download the executable for your platform (when available)
2. Run the executable — your browser will automatically open <http://localhost:8500>

> [!TIP]
> macOS/Linux users: run `chmod +x YuketangHelper-<OS>-<VERSION>` first (replace with actual filename); macOS users: if you see a security warning, go to **System Settings → Privacy & Security** and click "Open Anyway"

### Option 2: Python (Recommended for Developers)

1. [Download source code ZIP](https://codeload.github.com/linfish330/ytk_signin/zip/refs/heads/main) and extract, or use Git Clone
1. Install [Python 3](https://www.python.org/) and [Node.js](https://nodejs.org/)
1. Run the following command in the project root directory:

   ```zsh
   python start.py
   ```

1. Open <http://localhost:5173> in your browser to use the app

You can also run `ykt_signin` from any terminal to start both services and open the UI automatically.

### Option 3: Docker (Recommended for Server Deployments)

1. Download and install Docker
1. Open Docker and pull the image `docker pull dvdyyz/yuketang-helper:latest`
1. Run:

   ```zsh
   docker run -d --name yuketang-helper --restart unless-stopped -p 8500:8500 -v yuketang-data:/data dvdyyz/yuketang-helper:latest
   ```

1. Open <http://localhost:8500> in your browser to use the app

> [!TIP]
> Server deployments can be accessed from a local browser via port forwarding;
> Username/password login is recommended on servers for a more persistent session;
> Contact the author if you need a 24/7 server.

## Stop

- **Executable**: Close the terminal window
- **Python**: Run `python stop.py`
- **Docker**: Run `docker stop yuketang-helper`

## Get AI API Key (Free)

### DeepSeek (recommended)

On the AI Settings page, enter only your DeepSeek API Key and click “Save and enable”. The app automatically uses DeepSeek’s OpenAI-compatible endpoint and vision model for image-based quiz questions; no model name or endpoint configuration is required.

- [DeepSeek API Keys](https://platform.deepseek.com/api_keys)

- **ModelScope**: Log in at [ModelScope](https://modelscope.cn/), go to [Access Control](https://modelscope.cn/my/access/token) and click **Create Your Token**

> [!IMPORTANT]
> Using the ModelScope API requires **both** of the following; otherwise model API calls will fail even with a valid access token, and this helper will not be able to answer with AI:
>
> 1. **Bind an Alibaba Cloud account in ModelScope [Account Settings](https://modelscope.cn/my/settings/account)**
> 2. **Complete real-name verification**

- **Google**: Log in at [Google AI Studio](https://aistudio.google.com/), go to the [Get API Key page](https://aistudio.google.com/api-keys), and click **Create API Key**

## TODO

- [ ] Support multiple LLM APIs
- [x] Support Fill-in-the-blank answering
- [ ] Auto preview
- [ ] Auto replay watching
