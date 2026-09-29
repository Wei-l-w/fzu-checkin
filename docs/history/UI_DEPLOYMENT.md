> 本文件是原始部署服务器上的过程记录（已去除域名等个人信息），保留作为协议核查与运维决策的依据；通用安装步骤请看仓库根目录 README。

# 手机管理页面

入口：**https://<你的域名>/fzu/**

2026-09-19 已追加“手机获取当前位置”和“打开高德核对”；使用方法、定位权限范围和本次回滚见`LOCATION_DEPLOYMENT.md`。

这是用户在原部署完成后明确要求追加的UI。独立管理员密码用于保护页面，与学校账号密码不是一回事。
学校账号、密码或Token、本人核对的GCJ-02位置、通知、离校日期可在手机页面填写；保存后自动暂停，预检通过后再明确恢复定时。
**没有即时签到按钮或对应API**。恢复只启动未来排档；“只读预检”可以登录/更新登录态、查询学校状态和范围，但不能提交签到。

## 首次登录

初始管理员密码使用随机生成的高熵字符串，未发到聊天、命令参数、日志或Git。只保存在本服务私有文件中，本人在服务器终端查看：

```bash
sudo cat /var/lib/fzu-checkin/admin/initial-password.txt
```

密码文件目录0700、文件0600。不要转发截图或粘贴到聊天。管理员服务只读取scrypt哈希文件，初始明文是供本人首次取用的私有交付文件；取用并更换密码后可自行妥善清除该旧初始文件。
本地重设管理员密码（隐藏输入，不把新密码写到命令行）：

```bash
sudo -u fzu-checkin /opt/fzu-checkin/venv/bin/python /opt/fzu-checkin/admin_server.py init-auth
```

重设后旧会话在下一次请求时失效；原初始密码文件不会随之更新，以新设密码为准。

## 页面行为

- 学校密码、Token和推送密钥不会回显；只显示已保存/未保存。空输入保留原值，需勾选清除才清除。
- 可保存未填完整的草稿；非法日期、非有限坐标、错误坐标系等会被拒绝。
- 保存配置先暂停，再原子写入0600私有配置，保留一份`backups/config-before-ui-save.json`；学校自动签到仍需本人授权、位置核对与只读预检。
- UI保存文件使用JSON形式（也是合法YAML），原来的`config.yaml`路径和权限不变；模板注释仍可在源码`config.example.yaml`查看。
- 配置并发编辑以不暴露秘密的随机密钥HMAC版本校验，过期页面不能覆盖新配置。已保存配置变化后，旧记录不能代表新配置，需重新预检。
- 最近运行记录最多保存120条；界面区分学校确认、待确认、失败、暂停、跳过及预检，不以退出0当作已签。历史只包括本服务留存事件，不是学校历史签到数据库。
- 记录明确使用北京时间和数据日期，昨天的成功不会显示成今天成功。固定坐标仍然不能证明本人实时在校。
- 页面无外部CDN、统计脚本、第三方表单；密码不进入localStorage/sessionStorage或URL。
- 定位只在本人点击后请求浏览器授权，转换后先填入草稿；地图也须主动确认后才外跳。位置变更需重新核对确认，不自动保存、启用或提交。

## 运行与安全边界

- Web：`fzu-checkin-web.service`，运行账号`fzu-checkin`，仅`127.0.0.1:18779`，NoNewPrivileges，源码只读，管理员哈希目录在该单元内只读。
- 控制：`fzu-checkin-control.service`，root运行的小型固定动作代理；只通过root:fzu/0660 Unix socket和SO_PEERCRED接收本账号请求，无公网监听。
- 控制动作仅status、preflight、pause、resume、notify-test，不接受路径/环境/命令参数；网页进程没有sudo权限。暂停有独立通道，恢复末尾重查新的暂停标记。
- 管理Cookie为独立名称、`Secure/HttpOnly/SameSite=Strict`、`Path=/fzu/`；会话最长8小时、闲置30分钟。密码验证使用scrypt，全局10次/10分钟尝试限制，不信任可伪造的转发IP。
- API写操作验证精确Origin和CSRF；所有响应no-store、严格CSP、不允许跨站嵌入。请求体64KiB、请求头16KiB、绝对15秒读取超时和连接并发限制。
- 入口复用现有站点的HTTPS，**仍是共享origin而不是独立子域隔离**；该站点其余路径的原认证保持不变。未改DNS、HAProxy、Cloudflare Tunnel、防火墙或服务器时区。
- 修改Caddy文件：`<你的 Caddyfile 路径>`，仅新增`/fzu`跳转与`/fzu/*`精确分流，并热reload。

```bash
sudo systemctl status fzu-checkin-web.service fzu-checkin-control.service --no-pager
sudo journalctl -u fzu-checkin-web.service -n 60 --no-pager
sudo fzu-checkinctl status
sudo fzu-checkinctl pause
```

单独停止管理页面不会等于暂停已启用的签到，若要全部暂停，请先执行`sudo fzu-checkinctl pause`再停页面。

## 回滚

本次改动前的源码、运维脚本和Caddy文件保存在root-only目录：
`/opt/fzu-checkin/rollback/ui-20260918T161349Z/`。
私有学校配置、登录缓存和签到提交意图不在源码回滚包里，不覆盖这些状态。

快速撤销UI且暂停签到：

```bash
sudo fzu-checkinctl pause
sudo systemctl disable --now fzu-checkin-web.service fzu-checkin-control.service
```

此时`/fzu/`入口返回不可用，不暴露任何账号；该站点其他路径不受影响。
完整恢复路由和改动前源码可用本次固定回滚脚本：

```bash
sudo /opt/fzu-checkin/rollback/ui-20260918T161349Z/rollback-ui.sh
```

脚本会检查Caddy是否仍与本次部署版本一致；若后来有其他改动，不覆盖新配置，要求人工核对。本次验证情况见`UI_VALIDATION.md`；没有用部署或页面测试冒充学校真实签到。

## 2026-09-29 Token 字段接受整段链接

“学校 Token”可以直接粘贴登录后地址栏里带`token=`的整段链接；前端保存时只提取 token 查询值（去空白、URL 解码），纯 token 原样通过。后端校验未改（非空、≤4096、无空白）。
静态文件按请求读取，无需重启`fzu-checkin-web.service`。改动前文件：`rollback/token-paste-20260929T122730Z/`（admin.js、index.html、location_browser.cjs）。
离线浏览器测试新增 1 项（粘贴链接只提交 token 值），共 20 项通过。认证诊断结论见`DEPLOYMENT.md`同日章节。
同日补充：用户实际粘贴的是地址栏可见的末尾片段（token 尾部 + `&contextPath=`），预检报`school_http`。前端对不含`token=`的粘贴按片段处理（去掉开头的`?`/`=`、截掉`&`之后的参数）；配置校验新增 Token 字符集/长度规则（`[A-Za-z0-9._~-]{20,4096}`，与`src/login.py`提取规则一致），不合规时保存被拒并给出“请粘贴完整链接”的提示。片段本身是否完整无法检测，页面提示改为要求复制整段链接。
后端同样提取：`src/config.py: normalize_token` 在保存`user.token`时把整段链接或片段归一为 token（旧页面未刷新也不会再存入 URL）。改后端需重启`fzu-checkin-web.service`，会话会失效需重新登录。

## 2026-09-29 晚：签到时间分区与更明确的提示

配置表单新增“签到时间”分区（`#schedule-times-input`，空格分隔，留空即默认），前端与后端同一规则校验；“自动签到计划”卡片的计划时间改为读取状态接口`schedule_times`。修改真实地址不再取消位置确认，只有坐标改动或重新定位才会取消并提示。今日卡片与运行控制说明会列出`validation`中的具体缺项。改前文件在`rollback/schedule-20260929T131500Z/`。
同晚补充：运行控制按钮上方新增“当前状态”一行（`#control-state`：定时已启用+下次运行 / 已暂停+缺项 / 未授权 / 未正常启用），定时已启用时“恢复定时”按钮显示为“定时已启用”并禁用，已暂停时“暂停定时”显示“（已暂停）”但保持可点。纯前端改动，无需重启；浏览器测试新增 1 项。

## 2026-09-29 重构后的页面结构

`index.html`/`admin.css`整体重写，`admin.js`仅新增窄屏默认折叠记录一行。布局：`.status-strip`（3 卡）→`.config-grid`（3 列，右列`.form-column`叠放 03/04/05）→授权卡→保存栏→`#records-details`（`<details>`+`.history-scroll` 最大高度 280px 滚动）→`.fine-print`。断点：≤1100px 两列、≤700px 单列。配色变量见`:root`（`--brand` 福大红）。回滚：`rollback/redesign-20260929T133300Z/`（index.html、admin.css、admin.js、location_browser.cjs）。

## 2026-09-29 深夜：多用户页面

登录表单增加用户名（管理员为 admin）；页头显示“当前登录：<名> · 管理员/成员”。页底新增“我的账户”（改本页密码）与仅管理员可见的“成员管理”（列表、添加、重置密码、移除）。接口：`GET/POST users`、`POST users/<名>/password|remove`（仅 owner）、`PUT password`（本人）。浏览器测试新增成员/管理员视图用例。
同晚补充：“学校登录（Token）”卡片内新增“怎么获取 Token”四步说明，含可点开的学校登录页链接（`#sso-link`，noopener/noreferrer，service 参数指向晚点名回调）和“复制网址”按钮；纯前端，无需重启。
