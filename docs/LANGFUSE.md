# Langfuse 接入

安装 `pip install -r requirements-observability.txt`，重启后端。默认关闭，缺少配置时保持正常业务执行。

本地 `.env` 配置（密钥不要提交）：

```dotenv
ERP_LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=填写项目公钥
LANGFUSE_SECRET_KEY=填写项目密钥
ERP_TRACE_HASH_KEY=本地随机长字符串
ERP_TRACE_ENVIRONMENT=development
ERP_TRACE_RELEASE=填写Git版本
# 仅当本机代理不能访问该域名而直连可用时开启
ERP_LANGFUSE_DIRECT=false
```

网页每次 stream/resume 是独立 Trace，同一会话通过 HMAC 假名化的 session_id 关联。模型、工具、子链路通过回调 run_id 建立父子关系。记录锁等待、首个用户可见Token、结束状态、模型Token、审查决策、Grader事件、HITL中断/恢复、WARM注入字符数、Skills读取与目录耗时、沙箱创建/认领尝试/健康与恢复耗时。

## 数据边界

自定义 LangChain 回调不转发输入输出、工具参数、错误正文、订单内容、记忆正文或文件内容。不得同时加一个未脱敏的官方回调。业务拒绝和等待审批不是执行失败，完成状态也不是有标注的任务验收成功。工具错误识别覆盖异常、ToolMessage error以及顶层数值code>=400，不保证识别所有嵌套ERP错误。

SDK异步批量发送，网络失败不作为业务失败；退出最多等待3秒flush，等待超时不代表后台线程强制终止。未知价格不声明成本为零，也不据Token断言费用节省。遥测可能因断网/退出丢失，不能代替持久业务审计或权限校验。DIRECT开关只把Langfuse主机加入当前进程NO_PROXY，不改变其他域名代理。

认领埋点是一次尝试成功返回，不等于命中预热池；健康埋点可能包含节流跳过，不代表每次实际ping。恢复成功不证明所有依赖安装均成功。单独的记忆检索命中数、审批等待跨Trace时长和依赖恢复明细尚未覆盖，不能据现有事件推断这些指标。

## 失败可见性

遥测失败一律不向上抛——埋点不能拖垮请求。但「吞掉」不等于「不报」，否则升级一次 langfuse 就会变成零 span 且日志无一行：本模块曾因 `from langfuse import propagate_attributes` 不兼容而静默降级，排查花了半小时。

现在每个吞掉异常的位置都登记一个站点名，第一次失败按 WARNING 打完整 traceback，重复失败只累加计数，`shutdown` 时汇总一行：

| 站点 | 含义 | 常见原因 |
|---|---|---|
| `langfuse.config` | 开了开关但缺凭据 | 只填了部分 `LANGFUSE_*` |
| `langfuse.client` | 客户端构造失败 | 未安装 langfuse、构造参数不兼容 |
| `langfuse.root_span` | 根 span 建不出来 | SDK 缺少 `propagate_attributes`（版本过旧或过新） |
| `langfuse.span_start` / `span_end` | 子 span 建立或收尾失败 | SDK API 变更 |
| `langfuse.event` / `finish` | 事件与结束写入失败 | 同上 |
| `langfuse.flush` | 退出前 flush 失败 | 网络不可达、超时 |

判定顺序里没有「环境变量齐全且 SDK 可用」之外的硬前置：日志出现 `observability suppressed ...` 就是链路真的断了，不必再猜。`observability.suppressed_errors()` 返回各站点计数，可直接在 Python 里查。

开发测试：`python scripts/dev.py test src/test/test_observability.py src/test/test_observability_errors.py`。

## 评测报告导入

`python -m evals.upload_langfuse --suite business` 默认只读预览36条；`--suite routing` 预览 800 条路由合成样本的评测记录。确认后加 `--send` 上传。上传仅固定历史报告的指标和分数，不伪装新执行，不发送正文。重复运行会创建新的导入，比较时按batch、split和provider筛选。

本版优先实现元数据和评分；合成正文上传暂不开放，以避免误把文件路径中的业务内容当成测试资料。

## 验收

先用合成问候和模拟业务检查Trace与session，再检查审批中断/拒绝、故障恢复。真实业务上传前确认本地密钥仅在忽略文件中。已在聊天中展示的Secret Key建议撤销换新；本地运行不消除云端密钥泄露风险。

### 2026-10-23 接入验证记录

- 完成云端认证和 LangGraph 脚本化模型冒烟测试；云端可见请求、链路与模型的 4 条 observation，检查其 input/output 均为空。此次冒烟不调用真实 ERP，也不消耗对话模型额度。
- 已导入 36 条业务历史报告及 800 条路由历史报告，云端核对共 836 条历史 observation；这些是既有结果导入，不是新执行的 836 次任务。
- 回归覆盖遥测开关、数据脱敏、并发上下文隔离、异常状态，以及 Skills、沙箱生命周期、记忆、业务评测与路由逻辑。未以本次验证替代真实 Docker 故障切换或 ERP 写入全链路验收。

本地后端需要重启，运行中的旧进程不会自动加载这些修改。重启后发起一个简单对话，在 Langfuse 的 Traces 中按 `erp.request` 查看；同一会话的请求可通过 Sessions 关联。模型未返回 usage 时，不会凭空生成 Token 或成本数字。关闭 `ERP_LANGFUSE_ENABLED` 并重启即可停止应用遥测。
