> 本文件是原始部署服务器上的过程记录（已去除域名等个人信息），保留作为协议核查与运维决策的依据；通用安装步骤请看仓库根目录 README。

# 智汇福大晚点名：本机部署与维护

首次部署日期：2026-09-18（UTC）。这是单人私有服务器部署。
后续按本人明确要求补充了管理员登录保护的手机Web页面：`https://<你的域名>/fzu/`，详见`UI_DEPLOYMENT.md`。
管理服务仅监听回环地址，经既有HTTPS反向代理访问；没有独立公网监听端口。

## 交付状态

安装和加固已完成；**尚缺本人配置，自动提交停用**。`enabled: false`，私有暂停标记存在，timer未启用。
账号/密码或Token、本人核对的GCJ-02经纬度/地址、位置确认与启用确认均未配置；通知未配置、未验证。
没有登录个人账号、没有读取个人签到任务、没有真实提交签到。不能把安装或离线测试通过当成学校实签成功。

若补齐配置并通过只读预检后启用，本次部署时计算的下一档为 **2026-09-19 21:35（北京时间）**。
timer停用时没有实际排队任务；未来请以状态命令重新计算时间，不把本说明中的日期当成实时信息。

## 路径、版本、权限

- 系统：Ubuntu 24.04.4，ARM64/aarch64；Python 3.12.3；systemd 255；服务器全局UTC、NTP同步，未修改全局时区。
- 源码与venv：`/opt/fzu-checkin/`，root持有，运行账号只读；venv不使用系统site-packages。
- 上游基线：`31325786f45ed604acf973bc65cc34520f058c9a`，本地修复已单独提交；实际本地SHA用下面命令查看。
- 运行账号：`fzu-checkin`，system user、nologin，无sudo权限。
- 配置：`/var/lib/fzu-checkin/config.yaml`，0600，fzu-checkin所有；父目录0700。
- 状态：`/var/lib/fzu-checkin/state/`，0700；状态文件0600。
- 私有备份：`/var/lib/fzu-checkin/backups/`，0700；任何配置/登录态备份也必须0600，不提交Git。
- 单元：`/etc/systemd/system/fzu-checkin.service`、`fzu-checkin-preflight.service`、`fzu-checkin.timer`。
- 运维命令：`/usr/local/sbin/fzu-checkinctl`；服务无常驻监听、无浏览器或数据库。
- 精确依赖：`requirements.lock`。本次安装 requests 2.34.2、PyYAML 6.0.3、pycryptodome 3.23.0、urllib3 2.8.0、certifi 2026.7.22、charset-normalizer 3.5.1、idna 3.20。

```bash
sudo git -C /opt/fzu-checkin rev-parse HEAD
sudo /opt/fzu-checkin/venv/bin/python -m pip freeze
sudo /opt/fzu-checkin/venv/bin/python -m pip check
```

状态持久化包括：登录缓存`session.json`（含秘密）、暂停标记`paused`、锁、`submission.json`（当天一次提交意图及结果）、`last_run.json`、`last_preflight.json`、通知去重状态与认证失败锁定。
除登录缓存外仅存最小状态、日期、受控枚举/布尔和必要私有指纹，不保存原始学校响应、个人ID或坐标。文件为固定数量，状态按天覆盖，不无限追加。
登录Token原子写入独立`session.json`并fsync，不重写用户编辑的YAML；配置凭据发生变化会使旧缓存失效。单Token过期后可能需要本人手工更新。

## 首次本地填写（请勿把秘密发进聊天）

先保持暂停，再在服务器终端打开私有配置。此编辑命令禁用用户vim配置、swap及vim历史：

```bash
sudo fzu-checkinctl pause
sudo -u fzu-checkin vim -Nu NONE -n -i NONE /var/lib/fzu-checkin/config.yaml
```

本人填写：

1. `user.username/password`或`user.token`，不要使用截图猜账号。
2. `checkin.longitude/latitude/actual_location`，只能是本人提供、核对过的GCJ-02坐标和地址；将`checkin.confirmed`改为`true`。
3. 确认适用条件后把`enabled`改为`true`。只编辑此项仍不会自动解除暂停，需要下方resume。
4. `skip_dates`填写个人离校/请假单日；`vacation.skip_ranges`填需要暂停的起止日期，包含首尾。没有预填未经核实的学校假期。
5. 可选通知三选一：Bark（iPhone）、Server酱、企业微信；其他密钥留空。不要把密钥放命令参数。

以下为不含秘密的日期格式示例，只展示结构，请替换成本人实际日期：

```yaml
skip_dates: ["2026-10-01"]
vacation:
  skip_ranges:
    - name: 个人离校
      start: "2026-10-02"
      end: "2026-10-07"
```

日期无效会明确失败而不会忽略；优先级是停用/暂停 > 个人跳过日期 > 学校状态/时间/范围 > 提交。
旧版`campus.bounds`自定义范围被拒绝，不允许扩大范围。现在以学校当天返回范围及`check.action`校验为准，原仓库宽泛矩形不作为校园在场证明。

## 只读检查与启用

以下命令**不会提交签到**；预检可登录并更新登录缓存，也会执行学校只读定位范围校验：

```bash
sudo fzu-checkinctl status
sudo fzu-checkinctl preflight
sudo fzu-checkinctl notify-test
sudo fzu-checkinctl resume
```

`notify-test`仅在配置通知后使用，标题明确为“部署测试”；渠道业务响应成功只能证明获受理，需本人确认手机收到。
`resume`要求`enabled:true`、位置确认、配置有效，并重新完成学校只读预检后才解除暂停并启用timer；当日任务/协议/范围无法核实会拒绝启用。
若当日确实无任务，程序正常运行可跳过，但首次启用仍需等有可验证计划时再预检，避免未验证范围就放行。
原`FZU_CHECKIN_FORCE`兼容模式只会把run降为预检，不能绕过只读限制。主程序不带参数也默认预检。

以下命令**可能真实提交签到**，安装验证阶段没有用它做试签：

```bash
sudo fzu-checkinctl run
```

只有本人启用、未暂停/请假、学校当天计划允许、学校校验接受坐标、未签且无未确认提交意图时才会提交。请勿同时在GitHub Actions、其他电脑或服务器配置同账号提交；本机锁不能约束外部实例。

## 定时、重试与暂停

北京时间21:35首次执行，21:40和21:50兜底；每档先查学校记录。学校实际时段和迟到线优先于本地排档。
不启用开机补跑（`Persistent=false`），应用层仍检查当日学校窗口和日期；源代码已移除原21:30–21:34混用naive/aware datetime的等待分支。
只读网络请求对超时/临时错误最多总计3次；密码提交每次登录最多1次，已知认证/人工验证失败对相同凭据持久阻止重复尝试。
已有Token遇明确认证失效，至多自动重登一次；通用HTTP500等错误既不会被当成“无任务”，也不会据此反复提交密码。若学校用500表示Token失效，需要本人在官方应用检查后更新登录态。
签到写请求最多一次，禁用重定向和自动重试；提交前持久化意图，响应超时也重新查学校记录。仍未确认则全天不再次提交，后续档只查状态；宁可提醒人工处理，不冒重复签到风险。
锁覆盖手动、预检与定时入口；暂停标记在最后提交前重查。并发暂停优先于较早开始的恢复。已发往学校的请求无法撤回，暂停后可用手机确认是否已签。

```bash
sudo fzu-checkinctl pause
sudo fzu-checkinctl status
sudo fzu-checkinctl resume
systemd-analyze calendar '*-*-* 21:35,40,50:00 Asia/Shanghai'
sudo systemctl list-timers fzu-checkin.timer --no-pager
```

pause同时写入全入口暂停标记、停用timer和终止本服务正在运行的实例；不影响服务器其他服务。恢复只启用未来排档，不立即补签。

## 日志、故障与验证边界

```bash
sudo fzu-checkinctl logs
sudo journalctl -u fzu-checkin.service --since today --no-pager
sudo systemctl show fzu-checkin.service -p Result -p ExecMainStatus
sudo stat -c '%a %U:%G %n' /var/lib/fzu-checkin /var/lib/fzu-checkin/config.yaml /var/lib/fzu-checkin/state
```

退出码：0为可判定正常流程（包括已签、无需、暂停、跳过、窗口外），20配置问题，21认证问题，22执行失败，23待确认，24并发锁，25通知测试失败。**0不代表签到成功**，必须看状态和学校当天记录。
日志进入journald，程序不打印秘密、完整URL、个人坐标或原始异常/响应。服务设置日志速率限制，使用服务器现有journal容量管理（本次约378MiB，默认上限约4GiB），未更改全局留存设置。
通知按当天状态去重；失败通知尝试间隔至少10分钟。配置解析损坏时无法安全读取通知密钥，只能通过单元失败/本机日志发现。
本服务自身通知不能发现整台服务器停机或通知通道故障，未擅自添加外部/收费监控；iPhone上仍应核对学校App，尤其首次几晚。
验证码/人脸/密码变更可能要求本人处理；自动登录不是永久免维护。人工处理后若认证锁定仍在，请先暂停，保存私有`auth_failure.json`至备份目录再清除该锁定，或更新正确凭据/有效Token后重新预检；不要反复尝试同一错误密码。

验证证据分层记录在`VALIDATION.md`：离线mock测试≠个人接口实测。学校公开页面/静态JS的实际读取用于协议核查，见`docs/PROTOCOL_REVIEW.md`；它不证明本人的认证、位置或签到状态。
只有提交后重查学校当天SUCCESS记录、核对日期、任务及有效签到时间，才会发送“签到成功”。PROCESSING、未知格式、仅HTTP200、接口受理和非空时间均不足以确认。

**固定配置坐标不具备手机实时定位能力，校验接受坐标不能证明本人实时在校。** 请只在本人符合学校条件时启用，离校/请假及时暂停；未扩大范围、未构造学校计划或成功记录。

## 更新与回滚

没有定时git pull，没有启用GitHub Actions。不直接执行原README里的自动安装/更新脚本。
上游原始基线归档及部署补丁保存在`/opt/fzu-checkin/rollback/`；原始基线存在已知风险，仅供审计，不可直接替换后启用。
本次是新安装，没有旧运行版本需要恢复。撤销自动运行的安全回滚是：

```bash
sudo fzu-checkinctl pause
```

这会停用提交而保留源码、配置、状态和回滚资料，不删除账号或秘密。更新前也必须先pause、私有备份配置/状态、保存当前源码与单元，重新审查、安装锁定依赖、离线测试和只读预检。
未来更新要恢复到本次加固快照，可运行：

```bash
sudo /opt/fzu-checkin/rollback/restore-hardened.sh
sudo fzu-checkinctl preflight
# 核查配置和学校状态后再：sudo fzu-checkinctl resume
```

恢复脚本只恢复本项目代码与三个systemd单元、运维脚本；旧venv会移到rollback目录保留，使用已保存的ARM64依赖wheel离线重建。它不覆盖私有配置或签到状态，不自动启用timer；未来版本新增的源码文件不会自动删除（若有导入冲突需单独审查）。版本回滚不能撤销已产生的学校签到记录。

## 2026-09-29 认证诊断：学校 SSO 在本服务器上要求二次验证

用户首次真实预检（20:16 北京时间）返回`auth_error / auth_protocol_changed`。两次脱敏的分步诊断（只记录状态码、域名、路径、字段长度和关键词计数，不记录页面内容、凭据或 Token）结论：

- 登录页正常返回，`login-croypto`（24 字符）与`login-page-flowkey`（14941 字符）解析结果和上游原版正则完全一致，解析没有问题。
- 提交账号密码后 SSO 返回 200 HTML（标题“统一身份认证平台”），**不是**账号密码错误页：页面不再含`login-croypto`，仍含`login-page-flowkey`，并出现“验证码 / captcha / 短信”字样。即密码步骤已被接受，学校要求继续完成短信或验证码等二次验证。该步骤不能、也不应由服务器自动完成。
- 服务器不能凭学号密码自动换取 Token。`src/login.py` 已把这种“保留流程键、无新密钥、含验证字样”的后续步骤页明确报为`AUTH_INTERACTIVE_REQUIRED`，不再误报协议变更；上游仓库已 404，无可参考的修复。
- 私有`state/auth_failure.json`保留当前锁定（code=AUTH_PROTOCOL_CHANGED）：这样 Token 失效时不会每晚再对 SSO 发起密码登录并触发学校短信。只有确认学校侧不再要求二次验证时才按上文流程清除。

当前启用路径：本人在自己的浏览器中打开
`https://sso.fzu.edu.cn/login?service=https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/oauth2/callback.action`，
完成学校要求的登录与验证，登录后地址栏落在`yzsxg.fzu.edu.cn`且带`token=`；把整段链接或 token 粘贴到管理页“学校 Token”。Token 有效期未知，失效后预检/运行会报认证失败，需重新粘贴。

回滚：`rollback/login-step-20260929T123129Z/`（login.py、test_config_auth.py）。Python 离线测试 162 项通过。

## 2026-09-29 首次真实预检通过并恢复定时

Token 路线在 20:56 北京时间通过只读预检（`ready / unsigned_current_plan`，当天日期、身份、计划、时段、学校范围全部核验），随后`fzu-checkinctl resume`启用 timer，首个排档 21:35。过程中按真实返回修正了三处过严的核验（仅放宽到实际观察到的格式，其余仍 fail-closed，均补了离线测试）：

- `paramsData.FToday` 实为“YYYY-MM-DD  星期X”（两个空格加星期）；`_task_date`允许可选的“星期X/周X”后缀，其他多余文字仍拒绝。
- `FStartTime/FLateTime/FEndTime`实为“HH:MM”加若干尾随空格；`_minute`仅去除首尾空白。学校今日时段 21:00–23:59，迟到线 22:30。
- `schoolData[].FPosition.range`混有`polygon`（9 点）与`rectangle`（恰 2 个角点）；`_location`接受 2 点矩形，几何判断仍由学校`check.action`完成。其他类型或点数仍视为不可信。

Token 由用户浏览器登录后的地址栏链接提取（36 位）；用户实际保存过“尾段+&contextPath=”和整段 URL 两种错误形式，因此配置校验新增 Token 字符集规则，`normalize_token`在后端保存时自动提取。已用真实 Token 读取`init`一次核对结构（只记录字段名与数字掩码形状）。

回滚：`rollback/school-format-20260929T125331Z/`（checkin.py、test_checkin.py）；`rollback/login-step-20260929T123129Z/`另含 config.py、admin_backend.py 改前副本。私有配置改前备份：`/var/lib/fzu-checkin/backups/config-before-token-fix-*.json`。
注意：通知渠道仍未配置，失败只能在管理页或学校 App 看到。Token 有效期未知，失效后运行会报认证失败并停在待处理，需重新粘贴。

## 2026-09-29 晚：可自定义签到时间、地址修改不再清除确认

用户反馈：改“真实地址”后位置确认被清掉、页面只显示“配置缺失或无效”不说缺什么、恢复按钮灰着不知原因、时间不能自定义。当晚 21:02 的保存正是地址文字改动触发确认清除（坐标 0 米未动），已按备份核对后恢复确认并重新预检、恢复定时。

- 签到时间：私有配置新增`schedule.times`（1–6 个北京时间 HH:MM，21:00–23:55，升序不重复；缺省 21:35/21:40/21:50）。校验在`src/config.py`（`valid_schedule_times`/`schedule_times`），UI 在“签到时间”分区填写，状态接口返回`schedule_times`。
- 落地方式：`fzu-checkinctl resume`（网页恢复也走它）以 root 重新读取并校验私有配置的`schedule.times`，写入 drop-in `/etc/systemd/system/fzu-checkin.timer.d/schedule.conf`（`OnCalendar=`清空后逐条列出），`daemon-reload`，`enable --now`后`restart`timer 以生效；任一步失败即重新暂停。控制代理不接受任何参数，时间只来自私有配置。手写 YAML 时时间必须加引号（'21:35'），否则 YAML 会解析成整数。
- 页面：改地址不再清除位置确认（只有坐标变化才清除）；“今日签到情况”和运行控制说明会列出已保存配置到底缺什么；恢复不可用时直接写明原因。
- 回滚：`rollback/schedule-20260929T131500Z/`（config.py、admin_backend.py、control_broker.py、deploy/fzu-checkinctl、admin.js、index.html 及测试）。恢复旧 CLI 后可删除 drop-in 目录再`daemon-reload`。
- 验证：Python 172 项、浏览器 22 项离线测试通过；真实执行`resume`生成默认 drop-in 后 timer 下次运行仍为当日 21:35。

## 2026-09-29 首次真实签到与两处提交路径修正

- 21:35 档：`do_checkin`提交前校验要求`userData.FCollUnit`为字符串，真实返回为整数，于是停在`submission_schema_unverified`，**未提交**（无`submission.json`）。已改为接受 str/int（非 bool、非空），H5 亦原样转发该值。
- 21:40 档：全部护栏通过并真正提交一次（`submit_attempted: true`），但提交后立即复查未见 SUCCESS，记为`post_submit_unconfirmed`并写入当日提交意图，阻止再次提交。21:42 只读预检返回`already / school_success_verified`：学校记录`statusData.status=SUCCESS`、`createTime`为 13 位毫秒整数、`FCheckInTime`显示“已签到”、`resultData`为“批量插入成功”，说明学校异步入库，记录滞后于响应。
- 修正：提交后复查最多 4 次、间隔 3 秒（`POST_SUBMIT_QUERIES`/`POST_SUBMIT_DELAY_SECONDS`），只读不再提交；查到明确 FAILED 即停止。后续 timer 档在学校已 SUCCESS 时由`authenticated_query`直接返回`already`，不会重复提交。
- 离线测试 174 项通过（新增：整数 FCollUnit 转发、异步 PROCESSING→SUCCESS 复核、超时不重提）。改前副本：`rollback/school-format-20260929T125331Z/checkin.py.before-collunit`、`checkin.py.before-postsubmit-retry`。

## 2026-09-29 管理页重构（一页配置、记录可折叠滚动、福大红白）

按用户要求重做布局与配色：状态三卡并排（今日情况 / 计划 / 运行控制），配置区三列（账号｜位置｜时间+请假+通知），授权与保存栏置底；运行记录改为可折叠的固定高度滚动窗口（窄屏默认折叠）；长篇隐私与说明收进页底“更多说明”。配色改为福州大学红（#a6192e）与白底，状态语义色（绿/琥珀/红/蓝）保留以区分成功、警告、失败、信息。所有元素 id、data-action 与脚本使用的 class 保持不变，浏览器离线测试 23 项通过；离线截图核对 1280/390 宽度均无横向溢出、无脚本错误。Token 字段默认展开（当前主要凭据路径）。改前文件：`rollback/redesign-20260929T133300Z/`。

## 2026-09-29 深夜：确认后跳过、状态段按钮、仅 Token 登录

- 学校当天已确认后跳过后续档：`finish()`在状态为`already/confirmed`时写`state/confirmed.json`={date}；run 模式开始若`confirmed.json.date==今天`直接返回`already`（code=`already_confirmed_today`），不再连接学校也不提交。preflight/resume 仍可查询。次日按日期自动失效。
- 运行控制改为状态段按钮：仅当前状态高亮填充（运行中→“定时已启用”填充；已停→“暂停定时”填充），其余为同色描边按钮；“只读预检”永不作为状态填充。纯前端（admin.css `.action-buttons .button.is-current*` + admin.js class 切换）。
- 仅 Token 登录：账号卡去掉学号/密码/清除密码字段，只留 Token（主凭据）。`secretFields`去掉 user.password；`buildConfigPayload`不再发送 username。已清空私有配置里的 username/password（备份`backups/config-before-token-only-*.json`），因此`authenticated_query`的密码兜底永不触发，Token 过期只报认证失败等待重新粘贴，不会再触发学校短信。后端仍保留 username/password 支持（未在 UI 暴露）。
- 未实现学校短信验证码登录：需要向学校认证接口发起短信/交互流程并逆向其端点，风险高且不稳定，改用仅 Token（用户二选一中的第二项）。
- 测试：Python 174、浏览器 23 全通过；离线截图核对 1280/390 无横向溢出、无脚本错误。回滚：`rollback/followup-20260929T140000Z/`。

## 2026-09-29 深夜：多用户自助（同一页面，各自登录，各管各的档案）

- 布局：`/var/lib/fzu-checkin/profiles/<用户名>/{config.yaml,state/,backups/}`（0700，fzu-checkin）；账户文件 `/var/lib/fzu-checkin/users/users.json`（v2，逐用户 scrypt，0600）。原单档案已原地迁移为 `profiles/admin`，管理员用户名 **admin**，密码不变（由旧 `admin/auth.json` 转换）。迁移脚本 `deploy/migrate-profiles.sh`（幂等）。
- 角色：`owner`（admin）可在页面“成员管理”新增成员（用户名 + 初始密码）、重置密码、移除成员（暂停其定时、停用登录、档案改名保留为 `.removed-<名>-<时间>`）；`member` 只能看到和管理自己的配置，并在“我的账户”修改密码（修改后本会话保持）。用户名规则 `[a-z][a-z0-9_]{1,23}`；密码最少 10 字符。登录限流仍为全局 10 次/10 分钟。
- systemd：改为模板单元 `fzu-checkin@<名>.service/.timer`、`fzu-checkin-preflight@<名>.service`（`%i` 指向各自档案）；每个档案的签到时间写入 `/etc/systemd/system/fzu-checkin@<名>.timer.d/schedule.conf`。旧的 `fzu-checkin.service/.timer/-preflight.service` 与其 drop-in 已删除，避免重复运行。
- 控制通道：代理请求增加可选 `profile`（严格校验且必须已在 profiles/ 下存在），CLI 变为 `sudo fzu-checkinctl <命令> [档案名]`（缺省 admin），新增 `user-list | user-add <名> | user-password <名> | user-remove <名>`（本地隐藏输入）。网页服务不再设置单档案环境变量，改为 `FZU_ADMIN_USERS_FILE`。
- 未实现：成员自行注册（必须由管理员创建）。学校 Token 仍由每位成员自己在浏览器登录后粘贴。
- 验证：Python 178、浏览器 24 项离线测试通过；线上用一次性成员账户经 HTTPS 验证登录、配置/状态隔离、成员无法访问成员管理（403）、改密后会话保持、移除后会话失效；`fzu-checkin@admin.timer` 下次运行 09-30 21:35。回滚：`rollback/multiuser-20260929T143000Z/`（含旧单元文件；回滚需把 profiles/admin 下的三项移回原位并恢复旧单元）。
