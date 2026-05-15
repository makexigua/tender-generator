# 自动化写标系统

## 主流程
主入口是 `main_async_threaded.py`，当前逻辑如下：

1. 从 `.env` 读取配置。  
2. 调列表接口 `GET /admin/tender/biding_doc/list`。  
3. 遍历 `status == 1` 的记录。  
4. 先将任务回写为 `status=2`（处理中，防止并发重复处理）。  
5. 下载 `biddingDocLocation` 文件到本地共享目录，并构造 `/download/{filename}` 附件地址发给钉钉 Agent。  
6. 由钉钉侧调用 MCP 的 `generate-docx` 工具生成标书。  
7. MCP 在 `generate-docx` 成功后回写 `status=3` 与 `bidDocLocationUrl`；失败时不回写状态。  

## 文件说明
- `main_async_threaded.py`：主调度脚本（拉列表、抢占任务、调钉钉）。  
- `chat2dingtalk_async.py`：钉钉调用与响应解析。  
- `server.py`：MCP 服务（`generate-docx` 生成文档并回写最终状态）。  
- `upd_status.py`：业务状态回写接口调用。  

## 环境变量
- `DINGTALK_API_URL`：钉钉接口地址。  
- `DINGTALK_BEARER_TOKEN`：钉钉鉴权 token。  
- `DINGTALK_ASSISTANT_ID`：钉钉助手 ID。  
- `DINGTALK_UNION_ID`：调用用户 unionId。  
- `DINGTALK_INPUT_TEXT`：发给钉钉的提示词（必须是单行）。  
- `DINGTALK_STREAM`：是否流式（`true/false`）。  
- `DINGTALK_THREAD_ID`：可选会话 ID。  
- `BIDING_DOC_UPD_STATUS_URL`：回写状态接口。  
- `GENERATED_DOCS_DIR`：主流程和 MCP 共享的文档目录（推荐显式配置，避免目录不一致）。  
- `PUBLIC_DOWNLOAD_BASE_URL`：MCP 回写下载地址前缀（默认 `https://ai-assistant.4-xiang.com/download`）。  

## 运行
```bash
python3 main_async_threaded.py
```

## 注意
- `DINGTALK_INPUT_TEXT` 不支持多行 `.env` 写法，必须单行。  
- 当前链路会本地落文件并通过 `/download/{filename}` 提供下载。  
- 建议 `GENERATED_DOCS_DIR` 与 `server.py` 服务进程权限匹配。  
