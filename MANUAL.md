# 单账号手动检查与签到

`manual_checkin.py` 用于本机手动排查一组抓包资料，使用 Python 3.10+ 标准库。它直接读取本地文件，不需要 GitHub Secret、定时任务或通知服务。默认只做离线检查；`query` 只查询；`submit` 才会提交签到。

## 输入资料

任选一种格式：

- 一份包含对应接口完整请求的 HAR，包括请求头及 `postData.text`。同一接口有多条记录时，应重新导出单条记录，避免选错。
- 两个 UTF-8 文件：每行一个请求头的文本，以及配套原始请求体。支持 `:method: POST` 等 HTTP/2 伪请求头和多条 `cookie:`。多个 Cookie 会按原顺序合并；传输层重新计算 Content-Length。

Token、Cookie、`hopegoo-encryptkey` 与编码请求体应来自同一条捕获记录。保留原始请求体字节，包括可能存在的末尾换行，不要解码后重编码，不要将它改成 JSON。请求头里的 `content-type: application/json` 不能证明捕获的编码文本是可编辑 JSON。

已确认的请求域名是 `app.hopegoo.com` 和新抓包使用的 `app.reshg.com`。脚本保留捕获记录中的实际 HTTPS 域名，不会自动把新抓包改发到旧域名；查询和签到必须来自同一域名。其他域名会被拒绝。运行环境需允许访问实际使用的域名。

将资料保存在仓库外的私有目录，限制文件读取权限。下例仅引用文件路径；请不要把凭据写入脚本源码或命令行。抓包文件含会话资料，不能提交到 Git。

## 先检查，不联网

在仓库目录运行：

```bash
python manual_checkin.py inspect --har /private/path/signin.har
```

或使用请求头与请求体文件；省略 `inspect` 也只做离线检查：

```bash
python manual_checkin.py inspect \
  --headers-file /private/path/signin-headers.txt \
  --body-file /private/path/signin-body.txt
```

离线检查会验证接口、请求方法、必要请求头、请求体长度，并比较 `hopegoo-token` 与 Cookie 中的 `sectoken`、`cnUser=sectoken=...`。缺少可比较的 Cookie 时报告 `not_available`。如有查询资料，可加 `--query-har /private/path/query.har`，一并检查两条请求的 Token 一致性。单独检查查询 HAR 使用 `inspect --kind query --har /private/path/query.har`。

`inspected_offline` 只表示本地资料检查通过，不能证明 Token 有效或属于指定手机号。

## 再做只读查询

需要真实捕获的 `listCheckin` 请求，其请求体不能用签到请求体代替：

```bash
python manual_checkin.py query --har /private/path/query.har
```

也支持 `query --headers-file /private/path/query-headers.txt --body-file /private/path/query-body.txt`。

`query_ok` 表示查询接口接受本次会话，且业务状态及当天日历校验通过。输出包含北京时间日期、当天是否已签、连续天数。脚本不会调用签到或通知接口。HTTP 或业务状态失败会按实际结果报告；未知业务码不会被直接解释为 Token 过期。

账号身份始终报告 `account_identity: unverified`：现有已知查询协议只解析签到日历，不足以证明它属于预期用户。进一步核对身份需要服务实际返回的、含账号标识的只读接口资料，不能根据不透明 Token 猜测账号。

## 单次提交

已准备好完整的查询与签到请求时：

```bash
python manual_checkin.py submit \
  --har /private/path/signin.har \
  --query-har /private/path/query.har
```

也可以使用四个文本文件：

```bash
python manual_checkin.py submit \
  --headers-file /private/path/signin-headers.txt \
  --body-file /private/path/signin-body.txt \
  --query-headers-file /private/path/query-headers.txt \
  --query-body-file /private/path/query-body.txt
```

流程是：离线检查 → 前置查询 → 未签到时提交一次 → 后置查询。前置查询失败就停止；已签到就返回 `already_signed` 并跳过提交。超时或响应不完整时不会重发签到，仍会尝试后置查询；查询显示已签也不会掩盖提交响应未确认的事实。

只有签到资料、需要单独重放这条已捕获请求时：

```bash
python manual_checkin.py submit --har /private/path/signin.har
```

该命令会实际提交一次；不具备在线预检和后查条件。接口接受时只能报告 `accepted_unverified`，不能据此宣称当天签到状态或新增奖励已核实。

| result | 含义 |
| --- | --- |
| `inspected_offline` | 仅离线检查通过 |
| `query_ok` | 只读查询及当日日历校验通过 |
| `already_signed` | 前查已签而跳过，或接口返回 `80002` 后经后查确认 |
| `already_reported_unverified` | 接口返回 `80002` 已签到，但没有后查资料 |
| `already_verification_failed` | 接口返回 `80002`，后查却未能确认已签 |
| `accepted_unverified` | 提交接口接受，尚无查询确认 |
| `verified_signed` | 提交接口接受且后查确认已签 |
| `submission_rejected` | HTTP 或业务状态明确拒绝 |
| `submission_unconfirmed` | 超时、响应解析等问题导致提交响应未确认 |
| `accepted_verification_failed` | 接口接受，但后查未能确认已签 |
| `query_failed` | 查询或当日日历校验失败，未提交 |
| `input_error` | 输入文件、请求配对或参数校验失败 |

输出只包含脱敏诊断，不打印凭据、编码请求体或原始响应。退出码 0 表示对应操作完成，1 表示失败或响应未确认，2 表示命令行参数错误。`accepted_unverified` 的退出码为 0，仅表示提交接口接受。

签到业务码 `80002` 按用户已实测的重复签到结果处理，不计新增奖励。有查询资料时仍须后查确认当天已签，才返回 `already_signed`；没有查询资料时仅返回 `already_reported_unverified`（退出码 0）。查询接口的 `80002` 仍按异常处理。

## 关于日期和抓包有效期

脚本每次查询都重新读取北京时间当天日期，跨午夜后的后查也按新日期核对。测试中的固定日期只是模拟数据。

原始请求体始终保持不变。编码请求体中是否存在日期、时间戳、随机数或签名，目前没有可核实的协议资料；脚本不会声称自动刷新这些值。Base64 解码后的二进制不能当作可编辑 JSON。HAR 的采集日期仅是文件元数据，不是内部业务日期。

若新抓包可用、旧抓包失败，可据两次真实响应进一步判断有效期和绑定关系；仅凭这一现象仍不能确定是 Token、日期还是加密参数过期。

## 验证

```bash
python -m unittest discover -s tests -v
```

手动脚本的测试使用虚构会话及本地 HTTP 服务，覆盖原始字节与重复 Cookie 保留、配对失败拦截、单次请求、前后查、跨午夜以及响应未确认的结果分类，不调用真实账号服务。
