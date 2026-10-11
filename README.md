# Hopegoo 自动签到

使用本人账号在 iPhone Reqable 捕获的请求，由 GitHub Actions 每天北京时间 **00:08** 查询并签到。先验证 1 个账号，确认跨日可用后可配置至 10 个。每次签到运行结束都向 PushPlus **cmcc** 提交一份短信汇总，包括成功、已签到、部分失败和全部失败。

当前已确认 App 正常签到接口及 Mac 同日查询重发成功。**提交请求的云端执行、跨日复用和长期有效期需要通过实际运行确认。**初版原样使用捕获的编码请求体及配套请求头；过期时需要重新抓包并更新 Secret。项目不处理邮箱验证码或自动重新登录。

## 一次性配置：今天完成

### 1. 准备同一账号的两个 HAR

你已保存的文件可以直接使用：

- `hopegoo-query.har`：仅包含 `POST https://app.hopegoo.com/sea_shell/manta-you/memberCheckin/listCheckin`。
- `hopegoo-signin.har`：仅包含 `POST https://app.hopegoo.com/sea_shell/manta-you/memberCheckin/performCheckin`。

需要重新导出时，在 Mac Reqable 对对应记录右键 → **导出 → HAR**。从同一个账号、同一次登录会话中各选一条记录；不要仅导出响应体。两个文件都含登录资料，只保存在本机，不提交到仓库或贴到聊天。

### 2. 下载本地导入工具

打开 [导入工具文件](https://github.com/Innsly5232/Hoopego-Auto-check-in/blob/main/tools/hopegoo-secret-importer.html)，点击文件页面的 **Download raw file** 按钮，下载 HTML 到 Mac。用 Chrome 或 Safari 打开下载的 HTML 文件。若下载功能找不到，也可用聊天中提供的本地文件下载链接。

1. 账号编号选 **账号 01**。
2. “查询文件”选择 `hopegoo-query.har`。
3. “签到文件”选择 `hopegoo-signin.har`。
4. 点击 **校验并生成配置**。
5. 显示校验通过后，点击 **复制账号配置**。

页面只在本机浏览器读取文件，内容安全策略禁止网络连接，无外部资源或浏览器持久存储；它不会验证在线签到。复制内容含会话资料，请仅粘贴到下面的 GitHub Secret 表单。

### 3. 添加账号 Secret

打开 [New repository secret](https://github.com/Innsly5232/Hoopego-Auto-check-in/settings/secrets/actions/new)，登录你有权管理这个仓库的 GitHub 账号。

| 表单项目 | 填写内容 |
|---|---|
| Name | `HOPEGOO_ACCOUNT_01` |
| Secret | 粘贴导入工具刚复制的完整配置 |

点击 **Add secret**。保存后不会再显示 Secret 明文，这是正常的。不要上传 HAR 到 GitHub 的文件页面，也不要把配置放在普通 Variables。

### 4. 添加 PushPlus Secret

再次打开 [New repository secret](https://github.com/Innsly5232/Hoopego-Auto-check-in/settings/secrets/actions/new)。

| 表单项目 | 填写内容 |
|---|---|
| Name | `PUSHPLUS_TOKEN` |
| Secret | 你现在使用的 PushPlus Token |

点击 **Add secret**。Token 可从你已有的安全保存位置或 [PushPlus 官网](https://www.pushplus.plus/) 登录后查看。GitHub 无法读取原仓库 Secret 的明文，需重新安全填入新仓库。继续使用原来可发送 cmcc 的 PushPlus 账号及设置。

在 [Secrets 列表](https://github.com/Innsly5232/Hoopego-Auto-check-in/settings/secrets/actions) 确认存在这两个名称即可，不需要展示值。

cmcc 已固定在脚本里，**不需要填写 `PUSHPLUS_CHANNEL`**。如果原项目使用了 `PUSHPLUS_OPTION`，在 [原项目 Variables](https://github.com/Innsly5232/Glados-Railgun-checkin/settings/variables/actions) 查看该项，将相同值填入 [新项目 Variables](https://github.com/Innsly5232/Hoopego-Auto-check-in/settings/variables/actions)，Name 为 `PUSHPLUS_OPTION`；原项目没有这项则跳过。

## 今天验证云端查询和短信

1. 打开 [Hopegoo daily check-in 工作流](https://github.com/Innsly5232/Hoopego-Auto-check-in/actions/workflows/hopegoo-checkin.yml)。
2. 如果 GitHub 提示 Actions 未启用，点击允许启用。若工作流被禁用，先点击 **Enable workflow**。
3. 点击右侧 **Run workflow**。
4. Branch 选 `main`；mode 选 **`query`**；点击绿色 **Run workflow**。
5. 等待出现新运行记录，点击进入。
6. 查看 **Check in and send SMS** 中的 **Query or check in** 和 **Always submit cmcc SMS summary**。

`query` 模式只查询，不提交签到。今天已经签到时，正常结果应为“今日已签到”，并生成短信。若查询成功但通知步骤失败，签到与通知分别排查；“PushPlus 已接受通知请求”不等于短信已实际送达，最终结果可在 PushPlus 查询并以手机收到的消息为准。

可以将结果摘要或步骤状态截图发给协助者，不要发送 Secret、请求头、HAR、Token 或编码请求体。

## 明天及以后

定时 cron 为 UTC `8 16 * * *`，对应北京时间每日 **00:08**。GitHub Actions 可能排队延迟。任务在 GitHub 上运行，Mac 和 iPhone 均无需保持在线。

每个账号先查询，只有当天日历日期正确、业务状态正常且 `todayStatus=0` 时才提交一次签到，再查询确认 `todayStatus=1`。当天已签到则跳过。提交请求超时也不盲目重试，优先查询核对；奖励无法确定时写“奖励未知”。各账号独立处理，一个失败不阻止其他账号。

通知步骤使用 `always()`：签到脚本、离线验证或启动步骤失败时生成失败摘要并尝试提交短信。未配置 `PUSHPLUS_TOKEN`、通知服务拒绝请求等情况会在运行记录明确报错。定时工作流保活独立执行，权限仅用于保活；账号和通知密钥不交给该步骤。

在收到跨日结果后再判断初版是否可用。登录会话或编码请求过期时，重新登录、正常操作捕获新的同账号查询和签到 HAR，再用导入工具更新该账号 Secret。短信会提示脱敏错误，不会暴露会话值。

## 已确认的短信格式

成功示例：

```text
Hopegoo签到 10/12
成功1｜已签0｜失败0
01：成功，+5 G-coins，连续2天
```

已经签到示例：

```text
Hopegoo签到 10/12
成功0｜已签1｜失败0
01：今日已签到，连续2天
```

失败示例：

```text
Hopegoo签到 10/12
成功0｜已签0｜失败1
01：失败，请求超时
```

奖励和天数使用实际核实结果，无法确认时显示未知。查询模式遇到尚未签到的账号时，显示“待签”数量及“尚未签到（仅查询）”。扩容后每个账号占一行，一次向 PushPlus 提交全部已配置账号的汇总。

## 扩至 10 个账号

对每个账号捕获各自的两个 HAR，用工具选择对应编号，分别添加 `HOPEGOO_ACCOUNT_02` 至 `HOPEGOO_ACCOUNT_10`。单个 Secret 包含该账号的完整配对请求，不能与其他账号混用。空编号不会被执行；已有 Secret 格式错误会作为该账号失败报告。无需修改工作流。

## 本地开发

Python 3.10+，Node 18+ 用于导入工具检查。运行脚本只使用 Python 标准库，无需安装 pip 包。

```bash
python -m unittest discover -s tests -v
node tests/verify-importer.cjs
```

这些检查使用模拟会话和本地 HTTP 服务，不验证真实账号或短信送达。工作流运行时用 Python 3.13。

本地 CLI 默认只查询：`python checkin.py --mode query --report hopegoo-report.json`。签到需显式使用 `--mode checkin`。通知为独立命令：`python checkin.py --notify --report hopegoo-report.json`。账号配置从环境变量读取；不要将真实值写进命令行、仓库文件或测试。
