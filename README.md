# 自动化写标系统

## 主流程
主入口是 `loop_check.py`，当前逻辑如下：

1. 从 `.env` 读取配置。  
2. 调列表接口 `GET /admin/tender/biding_doc/list`。  
3. 遍历 `status == 1` 的记录。  
4. 把 `biddingDocLocation` 作为附件发给钉钉 Agent。  
5. 从钉钉返回文本中提取生成文件的下载 URL。  
6. 直接调回写接口 `PUT /admin/tender/biding_doc/upd_status`，把 `status` 改为 `3`，并将该下载 URL 回写到 `bidDocLocationUrl`。

## 文件说明
- `loop_check.py`：主调度脚本（拉列表、调钉钉、下载文件、回写状态）。  
- `chat2dingtalk.py`：钉钉调用与结果解析（提取文件 URL）。  
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

## 运行
```bash
python3 loop_check.py
```

## 注意
- `DINGTALK_INPUT_TEXT` 不支持多行 `.env` 写法，必须单行。  
- 当前链路不再本地落文件，是否能下载取决于钉钉返回的 URL 是否可访问。  
