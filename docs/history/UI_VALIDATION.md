> 本文件是原始部署服务器上的过程记录（已去除域名等个人信息），保留作为协议核查与运维决策的依据；通用安装步骤请看仓库根目录 README。

# UI验证记录

2026-09-18 UTC（北京时间2026-09-19凌晨）。源码更改已记录为本地提交，版本用`sudo git -C /opt/fzu-checkin rev-parse HEAD`查看。

## 已验证

- 全套161项离线测试通过：原85项+配置/历史API10项+HTTP/管理员认证34项+受限控制代理32项；学校请求和用户通知均未真实发送。
- 相同161项再次在实际fzu-checkin账号、生产venv、systemd文件系统/权限隔离中通过，验证时禁止外网、仅放行测试用回环连接。
- Chromium纯本机mock验证320/390/768/1280宽度无横向溢出；表单草稿不会被状态轮询覆盖；切换通知显式清除非选中渠道；空秘密保留、保存后清空；XSS安全文本渲染；旧日期和旧配置成功记录不当作当前有效结果。
- Chromium经实际HTTPS入口登录验证通过；四种宽度无横向溢出，页面脚本错误0、CSP错误0；退出后Cookie会话失效、配置API重新返回401。
- 管理登录Cookie已实测Secure、HttpOnly、SameSite=Strict、Path=/fzu/；成功响应和测试输出未打印密码/Token/会话值。
- 真实未登录配置API返回401；已登录但缺CSRF的配置写入403；错误Origin动作403；即使已登录且CSRF正确，`actions/run`仍404。
- 真实页面“只读预检”通过受限代理启动学校预检单元；当前无账号/坐标，实际返回config_error、退出20。没有学校认证或签到提交。该记录是缺配置的安全失败，不是学校预检通过。
- 实际Unix代理status正常；pause成功；缺配置时resume返回20，timer仍disabled/inactive。根代理内runuser真正以fzu-checkin身份运行，降权后没有有效或ambient能力；只有根代理保留切换身份/控制自身子进程所需的有限能力。
- Web单元低权限运行、NoNewPrivileges、源码只读，admin认证目录在Web单元内只读；仅127.0.0.1:18779监听。Unix目录root:fzu 0750，socket0660；root锁0600、O_NOFOLLOW且不截断，不能被Web用户替换。
- 管理认证目录0700、哈希文件和初始密码交付文件0600，学校配置也继续保留0700/0600私有权限。没有向Git、聊天或日志写入真实凭据。
- Caddy配置验证和热reload通过；实际/fzu/返回200，CSP含default-src/script-src/style-src/connect-src白名单，Cache-Control=no-store。该站点原根页面仍401及原Basic认证/安全响应头，checkout根页面200；原服务PID未变。
- 两个新增systemd单元配置验证通过，shell/JS语法和git diff检查通过。原系统unified-monitoring-agent单元的可执行位警告未被扩大处理。
- 回滚资料保存在root-only指定目录，归档和当前Caddy检查和已核对；回滚脚本语法检查通过，尚未执行生产回滚演练。

## 明确未验证

- 未填学校账号/Token、GCJ-02坐标/地址及本人确认；正常账号只读联调尚未完成。
- 没有启用真实签到timer，没有任何真实签到成功记录；恢复成功分支仅离线验证。
- 没有真实发送Bark/Server酱/企业微信通知，手机通知送达未验证。
- Chromium移动尺寸验证不等于真实iPhone Safari设备测试。
- UI与现有站点共享origin，不等于独立子域隔离；固定坐标不等于手机实时定位；内部通知仍无法发现整台服务器停机。
