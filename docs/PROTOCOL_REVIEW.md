# 晚点名协议核查与安全边界（2026-09-18）

本次只对学校公开 H5 页面和其静态 JavaScript 做了无凭据 GET。
没有登录任何学校账户，没有调用 `init.action`、`check.action`、
`currentTimestamp.action` 或 `clockIn.action`，没有发送签到。
下述“已核实”指当前公开前端代码，不代表已实测个人账户或服务端业务结果。

## 公开证据

- 页面：`https://yzsxg.fzu.edu.cn/plug-in/livecloud/project/fzu/attn/index.action`
- 页面引用脚本：`./assets/index-cb879b31.js`
- 脚本 SHA-256：`bc95b6708df975d24f01a8b002f22092d5a63246a4f7249f75cb879c192c78f7`
- 脚本包含演示数据；不得把其中账户、地点、校区范围或任务样例当作真实配置。

| 用途 | 公开前端的实际依据 | 本客户端处理 |
| --- | --- | --- |
| 当天显示日期 | `paramsData.FToday` | 必须能完整解析为日历日期，且与学校时间、本机北京时间的当天一致 |
| 当前计划 | `userData.FPlanID`，前端 `!planId` 时 `no_plan` | 字段缺失/类型未知为 `pending`；明确为空且无状态记录才是 `no_task` |
| 成功/处理中 | `statusData.status`；枚举 `SUCCESS` / `PROCESSING` / `FAILED` | 只有 `SUCCESS` 可进入成功核验；`PROCESSING` 和 `FAILED` 一律 `pending`，不重签 |
| 签到日期和时间 | `statusData.createTime` 传给 `new Date(...)` | 必须具有日期、当日、在当日计划窗口内且不晚于当前学校时间（容忍5秒） |
| 方式 | `userData.FWay`：数值 1 校内、2 外勤、3 无需 | 自动提交只支持 1；2 拦截；3 明确无需时 `no_task` |
| 学校时段 | `paramsData.FStartTime` / `FEndTime` | 必须是合法 `HH:MM` 且同日正序；不使用本地硬编码代替 |
| 迟到线 | `paramsData.FLateTime`；前端比较“当前分钟 > 迟到线分钟” | 必須有合法字段且位于开始/结束之间；缺失不猜默认值 |
| 范围 | 遍历 `schoolData[].FPosition` JSON 的 `range` 并合并 | 所有输入范围必须可验证；目前只支持公开样例中的 `polygon`/`paths` 结构 |
| 范围命中 | `check.action` 返回非空数组，前端取第一个对象 | 必须是对象且 `id` 属于学校下发范围ID或校区ID；未知结果拒绝 |
| 校区列表 | `schoolData[].ID` / `FName` | 只使用学校下发数据，不退回自编校区ID |
| 身份稳定性 | 演示 `userData` 有 `FUser` / `UserId` | 至少一个标量非空，提交前后存在的身份字段和值必须一致；不对外输出值 |

原仓库使用 `userData.FState` / `FCheckInTime` 判断已签，与当前 H5 的成功判断不符。
当前 H5 虽把 `PROCESSING` 也显示为成功，本部署不采用这种乐观显示。
任意非空文字、旧字段的 `SUCCESS`、单独的接口返回成功或单独的时分秒，均不能证明已签到。

`statusData` 缺失或为 `null` 时，公开 H5 不认为已签，公开演示的未签结构也没有该记录；
本客户端只在当天日期、身份、计划、方式和完整时段均已验证后，才把这一情形列为 `ready`。
空对象、未知状态值、异常结构一律 `pending`。

## HTTP 与参数

`createCommonApi` 对 `init.action` 发 POST `{}`；`createEncryptApi` 对其他三个接口发 GET。
`currentTimestamp.action` 当前实际调用为 `createEncryptApi(path)({})`，因此 **不传 param**；
返回 `data.timestamp`，前端按 Unix 毫秒时间戳使用。
`check.action` 和 `clockIn.action` 使用 `param=encodeURIComponent(Encrypt(JSON))`，之后 Axios 再编码查询参数。
AES 为 AES-128-CBC / PKCS7 / Base64，KEY 和 IV 均为公开静态常量 `apexinfoapexinfo`。
认证头是 `token`。业务包必须明确 `success: true` 且整数 `code: 1`；其他包不当成无任务。

`check` 参数为 `mapType: amap`、`longitude`、`latitude` 和字符串 JSON `range`。
`clockIn` 参数经静态核实为 `campus`、`lon`、`lat`、`startTime`、`endTime`、
`actualLocation`、`caIsNo`、`collUnit`、`way`、`isLate`。
`collUnit` 来自 `userData.FCollUnit`，不是范围命中对象。
`caIsNo` 对本部署普通路径采用 H5 的默认字符串 `0`；不支持其他特殊审批入口。

## 比前端更保守的执行规则

1. `AttnClient(token)` 默认严格只读。只有显式 `read_only=False` 才允许写接口；
   直接调用加密请求方法也不能越过写保护。端点固定白名单，禁跨站重定向、环境代理。
2. 只读网络超时、连接中断和 HTTP 429/502/503/504 最多追加两次重试（总计三次）。
   证书错误不重试；其他 HTTP/业务错误不重试。所有写请求只发一次，底层自动重试为零。
3. 全部时间采用 `ZoneInfo("Asia/Shanghai")`。学校时钟必需，不默默退回本机时钟；
   本机与学校日期不同或相差超过5分钟即阻止。窗口按前端分钟粒度含端点，
   例如结束 `23:59` 覆盖至 `23:59:59`；跨午夜/逆序窗口不支持，返回 `pending`。
4. `do_checkin` 先重新查询当天任务和身份，再只读校验范围，再重新校验学校时间。
   写请求前必须调用 `before_submit`，且只有确切 `True` 才放行。
   主程序须在该回调重新检查暂停/跳过/配置指纹、原子持久化当天提交意图，并保持跨进程锁。
   缺少回调、不在学校时段、配置或任务变化都不签到。
5. 单次提交后，无论接口正常返回还是超时/异常，都先重新 `init` 查询并核验学校时间。
   只有当日窗口中的 `SUCCESS + createTime`，且日期、计划、身份前后相同才是 `confirmed`。
   其他结果包括 `PROCESSING`、确认查询不可用、任务已改变，一律 `pending`。
   主程序见到已持久化的当天提交意图，只能查询，不得再次调用签到接口。
6. 日志仅允许输出 `evidence` 或安全的 `window`。`init` 是内存中的敏感工作数据，
   不打印、不写调试日志、不写完整响应。异常只有静态错误码和中文说明，不附URL、响应文字或值。

## 尚未证明的事项 / 启用条件

没有个人账户返回样本，不能声称已经证明以下事项：真实 `FToday` 的显示格式、
真实 `createTime` 的传输格式、任务是否有额外审批字段、`check` 对所有校区的命中结构、
或服务端如何在数据库中把状态记录关联到当前计划。
公开 H5 没有访问 `statusData` 内的计划关联字段；本实现**没有编造**该字段，
而是使用同次已认证 `init` 的当前计划、当日窗口内时间，以及前后计划/身份稳定性。
这不是独立的服务端数据库关联证明。若实际 schema 不符，必须停在 `pending` 后人工核查，不能添加“非空即成功”的兜底。

日期解析只接受完整年-月-日、年/月/日或年月日；签到时间只接受严格数值 Unix 毫秒或具有完整日期的 ISO 兼容时间，
不接受仅时分秒、数值字符串、任意文字。上述格式支持是 `new Date` 的保守兼容子集，
不是声称已经观察到实际个人账户使用每一种格式。

**上线前必须由用户自行填入真实凭据和本人真实位置，再运行只读预检。**
只读预检需要当天 `ready` 或已核验 `already`、完整学校时段，以及学校 `check` 范围命中；
没有任务、未知 schema、未确认处理状态、网络错误和缺少凭据都不能据此开启定时任务。
本次无凭据静态核查与离线测试，不等于通过这项真实账户预检。

## 离线测试

`python -m unittest discover -s tests -p test_checkin.py -v`

测试全部使用合成数据并禁止真正网络请求，覆盖日期/状态严格核验、21:30/21:34/21:35/23:59/午夜边界、
学校时钟异常、范围结构、只读不可写、写入不重试、提交超时后先查询、最终暂停回调、
处理中不成功、同日不同计划不成功及异常脱敏。
