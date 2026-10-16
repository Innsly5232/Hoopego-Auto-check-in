# Hopegoo 自动签到

使用本人账号抓包中的会话资料进行查询与签到。查询保留原始请求；签到使用原 Token、Cookie 和实际域名，动态生成北京时间当天的 JSON 日期，不再重放旧的加密签到请求体。Python 脚本只依赖标准库。

每天 **北京时间 00:08**（UTC `8 16 * * *`）运行，并在 **01:08**（UTC `8 17 * * *`）备用运行：先查询当天状态；已经签到则跳过；未签到时提交一次，再查询确认。支持最多 10 个独立账号。每次定时签到和手动 `checkin` 模式通过 PushPlus **cmcc** 提交一份汇总；备用运行已签跳过时也会通知，因此每天可能有两条定时汇总。

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

1. 首先选择 `query`。它只查询所有已配置账号，不提交签到、不发送短信。
2. 确认日志显示当天日期和预期签到状态，Summary 无失败。
3. 需要运行完整流程时选择 `checkin`。前查已签则跳过提交，否则提交一次后查询，并尝试发送汇总。
4. 账号及通知配置确认后，再保持工作流为启用状态，供每天北京时间 00:08、01:08 的计划任务触发。GitHub Actions 可能延迟或漏触发；备用计划降低漏跑风险，但两次计划均依赖 GitHub 调度，不能保证准点或必达。同一时间只执行一个签到任务，后续任务等待前一个结束。

手动启动默认使用 `checkin`；定时事件固定使用 `checkin`。`query` 是首次配置或排障时的可选模式。正常运行直接使用 GitHub 注入的 Secret，不重复调用 GitHub API 检查账号、权限或 Secret 配置；签到前的业务查询用于判断当天是否已签。

运行状态以实际 GitHub 页面为准，修改或发布 Codex 云环境配置不会自动更新仓库 Secret，也不会自动启用 GitHub 工作流。

## 查询重试与备用运行

自动脚本的前置查询和提交后查询，在超时、网络连接失败、传输不完整或 HTTP `408/429/500/502/503/504` 时最多尝试 **3 次**，两次等待分别为 **5 秒、10 秒**。每次使用原始配对请求；日志只记录脱敏原因和尝试次数。HTTP 登录拒绝、业务错误、日期或日历校验失败不重试；重试耗尽后仍报告失败或提交结果未确认。

签到提交和短信提交不会因此自动重试。备用运行重新查询当天状态，确认已签则跳过提交；未签才提交一次。为容纳最多 10 个账号的查询重试，单次签到任务超时上限为 50 分钟。独立手动脚本仍保持原来的单次查询行为。

## 结果判定

- 查询必须返回 HTTP 200、业务 `status=200`，并包含与北京时间当天一致的有效日历。
- 签到业务 `status=200` 表示接口接受，仍需后查确认今天已签。
- **签到业务 `status=80002` 按已签到处理**，来源为用户已完成的重复签到实测；仍需后查确认今天已签。它不用于放行查询接口，也不计为新增奖励。
- 其他业务码按真实状态报告，不直接解释为 Token 过期。
- 提交超时或响应不完整时不重试签到，仍尝试后查；即使后查已签，也保留“提交响应未确认”。
- `80002` 后查失败或仍未签到，不会被标记为成功。

前查已签的账号会汇总为“今日已签到，连续 N 天”。奖励未知时明确标记未知。重复提交及已签到结果均不虚构新增 G-coins。

签到和通知分别报告：PushPlus 接受通知请求只表示通知已提交，最终短信送达以手机和 PushPlus 记录为准。工作流使用 `always()` 尝试提交 `checkin` 失败摘要；工作流本身无法解析时，GitHub 无法启动通知步骤。

## 日期与有效期

程序按 `Asia/Shanghai` 读取当天日期，没有固定业务日期。测试中的固定日期仅用于模拟。

2026-10-17 的实际排查确认：原样重放前一天的加密签到请求，返回 `80001`，提示“請簽到今日嘅日期，期待聽日再嚟呀！”。保留同一份资料中的 Token、Cookie，移除旧加密头并改发 `{"date":"2026-10-17"}` 后，接口返回成功，后查确认当天已签、连续 3 天。这是历史验证记录，运行时代码不固定该日期。

自动脚本每次签到根据北京时间当天生成 JSON 日期，不解密或修改旧密文，也不将旧加密头与 JSON 混用。查询请求仍使用配对的原始请求体和加密头。登录会话仍可能过期；届时重新捕获并更新对应账号 Secret，脚本不自动重新登录。独立手动脚本用于原样重放，不会自动替换其请求体中的日期。

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
python checkin.py --mode checkin --skip-signed --report /private/path/hopegoo-report.json
python checkin.py --notify --report /private/path/hopegoo-report.json
```

省略 `--skip-signed` 的 `checkin` 模式可用于有意重放一次已签请求的排查。正常自动工作流带有该参数。每次运行中每个账号最多提交一次；短信为独立命令。报告只保存脱敏结果，并核对 GitHub run/attempt 身份，避免复用旧任务报告；同一次任务跨午夜发送汇总时保留原报告日期。
