# 自动化写标系统

## 当前架构
- 主入口是 `main.py`，采用 `asyncio + httpx.AsyncClient` 的纯异步并发方案。  
- MCP 入口是 `server.py`，负责 `generate-docx` 生成文件并回写最终状态。  

## 主流程
1. 从 `.env` 读取配置。  
2. 调招标文件接口。  
3. 过滤 `status == 1` 的待处理任务。  
4. 主流程先回写 `status=2`（抢占任务，防止重复处理）。  
5. 下载 `biddingDocLocation` 到本地共享目录。  
6. 把本地文件映射为 `/download/{filename}` 附件地址发给钉钉 Agent。  
7. 钉钉侧调用 MCP 的 `generate-docx` 生成标书。  
8. 成功时回写 `status=3 + bidDocLocationUrl`；失败时回写 `status=4`。  

## 状态流转
- `status=1`：待处理。  
- `status=2`：处理中（主流程抢占后立即写入）。  
- `status=3`：生成成功（MCP `generate-docx` 成功后写入）。  
- `status=4`：生成失败（主流程或 MCP 发生异常时写入）。  

## 并发配置
- `MAX_CONCURRENCY`：主流程异步并发上限，推荐优先配置。  
- `MAX_WORKERS`：兼容旧变量；当 `MAX_CONCURRENCY` 未配置时，会回退读取它。  
- 默认值：`10`。  

## 文件说明
- `main.py`：主调度脚本（异步拉列表、并发处理任务、失败兜底回写）。  
- `chat2dingtalk.py`：钉钉调用与响应解析（异步请求与重试）。  
- `upd_status.py`：状态回写工具（同时提供同步/异步函数）。
- `server.py`：MCP 服务（`generate-docx` 生成文档并回写最终状态）。  
