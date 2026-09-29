# fzu-checkin · 福州大学晚点名自动签到（服务器多用户版）

部署在自己的 Linux 服务器上，用手机网页管理，每晚定时替你和几位同学完成「智汇福大」晚点名签到。
只有在学校当天记录里复核到签到成功，才会显示成功；查不到就明确告诉你“待确认”，绝不重复提交。

> 基于 [cccccheyu/fzu-auto-checkin](https://github.com/cccccheyu/fzu-auto-checkin)（MIT）重写。上游是「GitHub Actions + 学号密码」的方案，本仓库改为「自有服务器 + Token + 多用户网页」，两者已不兼容；上游仓库现已不可访问。

## 特性

- **常驻服务器、systemd 定时**：默认每晚 21:35 / 21:40 / 21:50（北京时间）运行，时间可在网页里改；后面的时点只在前面未成功时复核重试。
- **手机网页管理**：Token、签到坐标（可手机定位并跳转高德核对）、签到时间、离校/请假日期、通知渠道；一键只读预检、暂停、恢复。
- **多用户自助**：管理员在网页里创建成员账户，每人独立档案、独立定时、独立记录，互不可见。
- **只用 Token 登录学校**：不保存任何学号密码，也不会替你触发学校的短信验证。Token 过期时页面会提示，重新粘贴链接即可。
- **宁可漏签也不误报**：日期、身份、计划、时段、校区范围全部核验通过才提交；提交后复查学校记录（学校异步入库，最多复查 4 次）；当天已确认后不再连接学校。
- **通知**：Bark（iPhone）/ Server酱 / 企业微信机器人。
- **安全设计**：签到进程以专用非 root 用户运行；网页服务只监听回环地址；root 控制代理只接受固定动作（预检/暂停/恢复/测试通知），不接受任何参数；私有数据目录 0700；CSRF + Origin 校验；页面没有“立即签到”按钮。

## 工作原理

1. 你在**自己的浏览器**登录智汇福大（学校要求短信验证码就照做），登录后地址栏会跳到晚点名页面；把整段链接粘贴到管理页，服务只保留其中的 `token`。
2. 每晚定时：读取你的档案 → 只读查询学校当天任务、时段和校区范围 → 全部满足才提交**一次** → 复查学校记录 → 记录结果并推送通知。
3. 验证码、人脸、二次验证等仍由本人在官方 App 或浏览器完成，本项目不做任何绕过。

## 部署要求与推荐

| 项目 | 要求 |
| --- | --- |
| 系统 | 任何能长期开机、带 systemd 的 Linux（Ubuntu 22.04/24.04、Debian 12 均可），Python 3.11+ |
| 资源 | 1 vCPU / 512 MB 内存足够；磁盘占用不到 200 MB |
| 网络 | 一个域名 + HTTPS（Caddy 自动证书最省事；Nginx + certbot、Cloudflare Tunnel 也可以） |
| 位置 | 国内或海外服务器都行，学校接口从海外也可正常访问 |

**推荐**：一台最便宜的云服务器（VPS）即可，几个同学共用一台。不推荐 GitHub Actions（没有持久状态、无法交互粘贴 Token、定时会整批延迟）和家用电脑（需要整晚开机）。服务器时区无关，程序内部固定按北京时间计算。

## 安装

```bash
git clone https://github.com/<你的账号>/fzu-checkin.git
cd fzu-checkin
sudo deploy/install.sh --origin https://checkin.example.com --base-path /fzu
```

安装脚本会：创建系统用户 `fzu-checkin`、把程序装到 `/opt/fzu-checkin`、创建虚拟环境并安装依赖、创建数据目录 `/var/lib/fzu-checkin`、渲染并启用 systemd 单元、安装 `fzu-checkinctl` 命令，最后交互式创建管理员账户（用户名默认 `admin`）。

然后在你的 HTTPS 反向代理里把 `/fzu/*` 转发到 `127.0.0.1:18779`（端口可用 `--port` 改）：

```caddyfile
checkin.example.com {
    handle /fzu* {
        reverse_proxy 127.0.0.1:18779
    }
}
```

```nginx
location /fzu/ {
    proxy_pass http://127.0.0.1:18779;
    proxy_set_header Host $host;
}
```

打开 `https://checkin.example.com/fzu/`，用管理员账户登录。管理页可以和你现有的站点共用一个域名（只占一个路径前缀）。

## 首次使用（每位成员）

1. 管理员在页面底部「成员管理」里填用户名和初始密码，把两者告诉同学；同学登录后先在「我的账户」改密码。
2. 按「学校登录（Token）」卡片里的四步说明，在浏览器登录学校并把地址栏链接粘贴进来。
3. 填写签到坐标（可点「手机获取当前位置」再「打开高德核对」）和真实地址，勾选本人确认。
4. 需要的话改签到时间、设置离校/请假日期、配置通知并发一条测试。
5. 勾选「授权按计划执行自动签到」→ 点「保存配置并暂停」→ 点「只读预检」→ 通过后点「恢复定时」。

之后每晚自动运行；第二天在「今日签到情况」看结果，第一晚建议再到学校 App 核对一次。

## 日常维护

| 命令（服务器上） | 作用 |
| --- | --- |
| `sudo fzu-checkinctl status [用户名]` | 查看某个档案的定时与配置状态（缺省 admin） |
| `sudo fzu-checkinctl preflight [用户名]` | 只读预检，不提交 |
| `sudo fzu-checkinctl pause / resume [用户名]` | 暂停 / 预检后恢复定时 |
| `sudo fzu-checkinctl logs [用户名]` | 查看该档案最近日志 |
| `sudo fzu-checkinctl user-list / user-add <名> / user-password <名> / user-remove <名>` | 本地管理账户（密码隐藏输入） |

- **Token 过期**：页面显示“学校认证未通过”，重新登录学校并粘贴链接，再预检、恢复。
- **离校 / 请假**：在页面设置跳过日期，或直接点“暂停定时”。
- **数据与备份**：所有私有数据都在 `/var/lib/fzu-checkin/`（`profiles/<用户名>/` 是各人的配置与记录，`users/users.json` 是账户）。备份就是打包这个目录。
- **升级**：`git pull` 后再次执行 `sudo deploy/install.sh --origin ...`（数据不会被覆盖），然后 `sudo systemctl restart fzu-checkin-control.service fzu-checkin-web.service`。
- **移除成员**：会暂停其定时、停用登录，配置档案改名保留，不会删除数据。

## 安全与隐私

- 学校 Token、推送密钥保存在 0600 的私有文件里，页面只显示“已保存/未保存”，不回显；不保存学号密码。
- 服务不向任何第三方上传数据；「打开高德核对」只发送当前坐标，不带账号、地址。
- 管理页登录有全局限流（10 次 / 10 分钟）、会话 8 小时上限、30 分钟闲置过期；管理员重置密码会让对方的会话立即失效。
- 详细的协议核查、fail-closed 规则与历史部署记录见 [docs/PROTOCOL_REVIEW.md](docs/PROTOCOL_REVIEW.md)、[docs/protocol.md](docs/protocol.md) 和 [docs/history/](docs/history/)。

## 限制与声明

- 固定坐标不能证明本人实时在校。请只在本人符合学校要求时启用，离校请及时暂停；使用本工具产生的后果由使用者自行承担。
- 不支持自动完成验证码、人脸或短信验证；Token 有效期由学校决定。
- 学校接口的字段格式来自真实观测（见 docs），学校改版后可能需要调整；程序遇到未知格式会停在“待确认”而不是猜测。
- 仅供个人学习与使用，请遵守学校相关规定。

## 开发与测试

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.lock
venv/bin/python -m unittest discover -s tests -p 'test_*.py'      # 全部离线，不访问学校
node --test tests/test_location.cjs                                 # 坐标换算
node --test tests/location_browser.cjs                              # 浏览器用例，需要 playwright-core + Chromium
```

目录说明：`main.py` 签到主流程；`src/` 配置、学校接口、登录态、通知、后端；`admin_server.py` 管理页 HTTP 服务；`admin_static/` 前端；`control_broker.py` root 控制代理；`deploy/` 安装脚本与 systemd 单元；`tests/` 离线测试。

## 致谢与许可

- 上游项目 [cccccheyu/fzu-auto-checkin](https://github.com/cccccheyu/fzu-auto-checkin) 提供了晚点名接口的逆向文档与最初实现。
- 本仓库以 [MIT License](LICENSE) 发布。
