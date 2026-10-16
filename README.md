# Hopegoo 自动签到

使用本人账号抓包中的会话资料签到。自动任务直接提交一次签到，使用原 Token、Cookie 和实际域名，动态生成北京时间当天的 JSON 日期，不查询签到状态、不重试。Python 脚本只依赖标准库。

每天 **北京时间 00:19**（UTC `19 16 * * *`）运行，并在 **09:18**（UTC `18 1 * * *`）固定备用运行。每次运行中每个账号直接提交一次，按接口返回判定成功、已签或失败。支持最多 10 个独立账号。定时任务和手动 `checkin` 模式均通过 PushPlus **cmcc** 提交一份汇总，成功、已签和失败都通知，因此每天可能有两条定时汇总。

手动排查也可使用 [独立手动脚本](MANUAL.md)，直接读取 HAR，默认离线检查，无需 GitHub Secrets 或通知配置。

## 账号配置

初次抓包可先阅读 [Reqable 抓包与 HAR 导出指引（PDF，10 页）](docs/Reqable_Hopegoo_Guide.pdf)，按步骤完成 iPhone 与 Mac 连接、证书安装、Hopegoo 请求抓取和 HAR 导出。需要保存到本机时可使用 [PDF 直接下载链接](https://github.com/Innsly5232/Hoopego-Auto-check-in/raw/refs/heads/main/docs/Reqable_Hopegoo_Guide.pdf)。

为同一账号、同一次登录会话导出两条完整 HAR 请求，保留原始请求体：

- 查询：`/sea_shell/manta-you/memberCheckin/listCheckin`
- 签到：`/sea_shell/manta-you/memberCheckin/performCheckin`

已支持 `app.hopegoo.com` 和新抓包使用的 `app.reshg.com`。脚本保留真实域名；查询和签到须来自同一域名，Token 须一致，Cookie 中的会话值也须与 Token 匹配。两份 HAR 可以保留原文件名。

现有导入器生成的完整账号 JSON 继续兼容，无需因本次日期修复重新抓包或更新 Secret。抓包中的原始签到请求仍用于配置校验和手动排查；自动脚本实际发送的是 `{"date":"当天的 YYYY-MM-DD"}`，并移除旧的 `hopegoo-encryptkey`，通过 HTTPS 发送。

使用本地 [HAR 导入工具](tools/hopegoo-secret-importer.html)：下载 HTML 后在浏览器打开，选择账号编号和两个 HAR，点击“校验并生成配置”，再复制账号配置。页面只在本机处理文件，禁止网络访问，不保存浏览器持久数据。

在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中保存为：

| 名称 | 内容 |
| --- | --- |
| `HOPEGOO_ACCOUNT_01` | 导入工具生成的账号 01 完整配置 |
| `HOPEGOO_ACCOUNT_02` 至 `HOPEGOO_ACCOUNT_10` | 其他账号各自的配对配置，可选 |
| `PUSHPLUS_TOKEN` | 用于接收 cmcc 汇总的 PushPlus Token |

空账号槽位会被跳过；格式错误的账号会独立报告失败，不阻止其他账号。新增账号应捕获自己的查询和签到请求，不能只把旧账号请求中的 Token 替换成新账号 Token。

`PUSHPLUS_OPTION` 为可选的仓库 Variable；使用原 PushPlus 配置需要该项时再填写。cmcc 已固定，不需要配置 `PUSHPLUS_CHANNEL`。

账号配置、HAR、Token、Cookie、加密头和编码请求体都属于私有会话资料，只放在本机私有位置或 GitHub Secrets。不要提交到仓库，也不要写入脚本源码。GitHub 不提供 Secret 明文读取；查看代码或成功推送不能证明账号 Secret 已更新。

## 首次配置与排查

进入 **Actions → Hopegoo daily check-in → Run workflow**：

1. 正常使用选择默认的 `checkin`：直接提交一次签到，并尝试发送汇总。
2. 日志及 Summary 显示本次请求的 HTTP、业务码和结果。HTTP 200 不单独代表签到成功，还需业务码符合约定。
3. `query` 仅供主动排查时选择：每个账号只查询一次，不签到、不重试、不发送短信。自动任务不会调用它。
4. 保持工作流启用，供每天北京时间 00:19、09:18 的计划任务触发。两次任务每天分别由 GitHub 调度，与前一次是否成功无关；同一时间只执行一个任务。GitHub 可能延迟或漏触发，两个计划不能保证准点或必达。

手动启动默认使用 `checkin`；定时事件固定使用 `checkin`。正常运行直接使用 GitHub 注入的 Secret，不调用 GitHub API 检查账号、权限或 Secret 配置，也不查询 Hopegoo 签到状态。

运行状态以实际 GitHub 页面为准，修改或发布 Codex 云环境配置不会自动更新仓库 Secret，也不会自动启用 GitHub 工作流。

## 单次提交与备用运行

每个账号在每次 `checkin` 运行中只调用一次 `performCheckin`，不做前置或后置查询，不设置请求重试。单次网络请求超时设置为 30 秒；任务总时限为 10 分钟，以容纳最多 10 个账号、启动和通知步骤。网络超时后不会立即重发，也不会声称服务端一定未签到。

09:18 的备用任务每天固定提交一次。主任务已成功时，通常由接口返回 `80002`，按“今日已签到”通知，不虚构新增奖励。主任务失败或未触发时，备用任务有机会完成签到。短信提交同样不自动重试。

## 结果判定

- HTTP 200 且签到业务 `status=200` 按成功处理；奖励及连续天数仅在本次响应明确返回有效值时展示。
- **HTTP 200 且签到业务 `status=80002` 按已签到处理**，来源为用户已完成的重复签到实测；不再做后查，不计为新增奖励。
- 其他业务码按真实状态报告，不直接解释为 Token 过期。
- 超时、网络中断或响应不完整时，报告“签到响应未确认”，计入失败汇总并通知；不查询、不重发。
- 可选的 `query` 模式仍校验 HTTP 200、业务 `status=200` 及当天有效日历；它不会把 `80002` 当作查询成功。

`80002` 汇总为“今日已签到”。没有返回连续天数时省略天数，奖励未知时明确标记未知；不读取旧记录填充这些数值。

签到和通知分别报告：PushPlus 接受通知请求只表示通知已提交，最终短信送达以手机和 PushPlus 记录为准。工作流使用 `always()` 尝试提交 `checkin` 失败摘要；工作流本身无法解析时，GitHub 无法启动通知步骤。

## 日期与有效期

程序按 `Asia/Shanghai` 读取当天日期，没有固定业务日期。测试中的固定日期仅用于模拟。

2026-10-17 的实际排查确认：原样重放前一天的加密签到请求，返回 `80001`，提示“請簽到今日嘅日期，期待聽日再嚟呀！”。保留同一份资料中的 Token、Cookie，移除旧加密头并改发 `{"date":"2026-10-17"}` 后，接口返回成功，后查确认当天已签、连续 3 天。这是历史验证记录，运行时代码不固定该日期。

自动脚本每次签到根据北京时间当天生成 JSON 日期，不解密或修改旧密文，也不将旧加密头与 JSON 混用。现有账号 JSON 继续兼容，无需改 Secret。只有显式选择 `query` 时才使用配对的原始查询请求。登录会话仍可能过期，届时重新捕获并更新对应账号 Secret，脚本不自动重新登录。独立手动脚本用于原样重放，不会自动替换其请求体中的日期；其配对查询功能仅供主动排查。

日历查询只能验证接口接受当前会话，不足以证明它属于某个预期手机号或用户 ID；账号归属需要另外的、实际含账号标识的只读资料。

## 本地开发与排查

Python 3.10+；Node 18+ 用于导入器检查。GitHub Actions 使用 Python 3.13。无需安装 pip/npm 依赖。

```bash
python -m unittest discover -s tests -v
node tests/verify-importer.cjs
bash tools/validate-workflow.sh
```

工作流校验需要 Linux amd64，会下载固定版本的 actionlint 并验证 SHA-256。测试使用虚构资料及本地 HTTP 服务，不读取真实账号 Secret。

已有完整账号配置注入环境时：

```bash
python checkin.py --mode query --report /private/path/hopegoo-report.json
python checkin.py --mode checkin --report /private/path/hopegoo-report.json
python checkin.py --notify --report /private/path/hopegoo-report.json
```

`checkin` 直接提交一次，不查询、不重试；短信为独立命令，由工作流在签到步骤后用 `always()` 尝试执行。报告只保存脱敏结果，并核对 GitHub run/attempt 身份，避免复用旧任务报告；同一次任务跨午夜发送汇总时保留原报告日期。
