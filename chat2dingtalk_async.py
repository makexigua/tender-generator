import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import requests


class DingTalkNetworkError(requests.exceptions.RequestException):
    """
    钉钉调用过程中的网络/协议异常。
    大白话：底层虽然用 httpx 发请求，但主流程已经按 requests 的网络异常分支处理失败回写，
    这里包一层是为了让 httpx.RemoteProtocolError 这类错误也走“网络问题”分支。
    """


def _file_name_from_url(url: str, index: int) -> str:
    name = Path(urlparse(url).path).name
    return unquote(name) if name else f"attachment_{index + 1}.bin"


def extract_output_text(response_body: dict) -> Optional[str]:
    """
    从钉钉返回里提取 output_text 文本，多段内容会拼接成一个字符串。
    """
    if not isinstance(response_body, dict):
        return None

    output_list = response_body.get("output")
    if not isinstance(output_list, list):
        return None

    parts: List[str] = []
    for output_item in output_list:
        if not isinstance(output_item, dict):
            continue
        content_list = output_item.get("content")
        if not isinstance(content_list, list):
            continue
        for content_item in content_list:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("type") != "output_text":
                continue
            text = content_item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())

    return "\n\n".join(parts) if parts else None


def extract_output_url(response_body: dict, source_doc_urls: Optional[List[str]] = None) -> Optional[str]:
    """
    从钉钉返回里提取第一个 http/https 链接。
    兼容"纯链接"或"文字里夹链接"两种输出。
    """
    output_text = extract_output_text(response_body)
    if not output_text:
        return None

    # 先提取所有链接，再按优先级挑最像"生成文件"的那个。
    matches = re.findall(r"https?://[^\s\"'<>]+", output_text)
    if not matches:
        return None

    cleaned_urls = [url.rstrip("，。；;)") for url in matches]

    # 过滤掉和输入附件相同的链接，避免误把原始附件链接当成生成结果。
    source_url_set = set(source_doc_urls or [])
    if source_url_set:
        cleaned_urls = [url for url in cleaned_urls if url not in source_url_set]
    if not cleaned_urls:
        return None

    # 优先级规则：
    # 1) 明确下载路由
    # 2) 静态目录路由
    # 3) 看起来像 doc/docx 文件
    priority_rules = [
        lambda u: "/download/" in u,
        lambda u: "/generated_docs/" in u,
        lambda u: ".docx" in u.lower() or ".doc" in u.lower(),
    ]
    for rule in priority_rules:
        preferred = [u for u in cleaned_urls if rule(u)]
        if preferred:
            # 通常生成链接会在靠后位置，取最后一个更稳妥。
            return preferred[-1]

    # 都不匹配时兜底取最后一个链接。
    return cleaned_urls[-1]


def _fetch_url_content(url: str, max_chars: int = 10000, timeout: int = 30) -> str:
    """
    下载 URL 指向的文件内容，返回文本（截断到 max_chars）。
    对 docx/pdf 等非纯文本，只做简单二进制读取后尝试解码。
    """
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        # 先尝试按文本读取
        content_type = resp.headers.get("content-type", "").lower()
        if "text" in content_type or "json" in content_type or "xml" in content_type:
            text = resp.text
        else:
            # 二进制文件，尝试读取前 max_chars*2 字节，然后按 utf-8 解码
            raw = resp.content[:max_chars * 2]
            try:
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                text = raw.decode("gbk", errors="ignore")
        return text[:max_chars] if len(text) > max_chars else text
    except Exception as exc:
        return f"[下载失败: {type(exc).__name__}: {exc}]"


def _build_attachments_with_content(
    source_doc_urls: List[str],
    max_chars_per_file: int = 10000,
) -> tuple[List[Dict], str]:
    """
    策略：
    1) 先尝试把文件下载到本地，然后作为附件上传（如果钉钉 API 支持 multipart 上传）。
    2) 如果不支持 multipart，则把文件内容读取后拼接到 input 文本中（受 1万字符限制）。

    返回：(attachments 列表, 附加文本内容)
    """
    attachments: List[Dict] = []
    content_parts: List[str] = []

    for index, source_url in enumerate(source_doc_urls):
        file_name = _file_name_from_url(source_url, index)
        attachments.append({"file_name": file_name, "file_url": source_url})

        # 同时读取内容，作为 input 的备选
        content = _fetch_url_content(source_url, max_chars=max_chars_per_file)
        content_parts.append(
            f"--- 附件 {index + 1}: {file_name} ---\n{content}"
        )

    # 合并所有内容，再截断到总限制（留一些余量给 base_input_text）
    combined_content = "\n\n".join(content_parts)
    return attachments, combined_content


def send_to_dingtalk(
    source_doc_urls: List[str],
    api_url: str,
    bearer_token: str,
    assistant_id: str,
    union_id: str,
    input_text: str,
    stream: bool = False,
    thread_id: str = "",
) -> Dict:
    """
    调用钉钉 assistant 接口。
    入参都由主流程传进来，这里只做请求，不重复读 .env。

    """
    attachments = [
        {"file_name": _file_name_from_url(source_doc_url, index), "file_url": source_doc_url}
        for index, source_doc_url in enumerate(source_doc_urls)
    ]
    payload = {
        "input": input_text,
        "assistant_id": assistant_id,
        "union_id": union_id,
        "stream": stream,
        "attachments": attachments,
    }
    if thread_id:
        payload["thread_id"] = thread_id

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
    }


    import httpx
    # 只对网络异常做重试，业务 4xx 不重试，避免无意义打接口。
    max_attempts = 3
    response = None
    for attempt in range(1, max_attempts + 1):
        print(f"[第{attempt}次钉钉agent调用]")
        try:
            # 钉钉网关虽然支持 HTTP/2，但这条长任务链路经代理时容易被远端断开。
            # 这里固定走 HTTP/1.1，优先保证请求能稳定发出去。
            with httpx.Client(http2=False, timeout=httpx.Timeout(30.0, read=1000.0)) as client:
                response = client.post(api_url, headers=headers, json=payload)
                print(response.status_code)
                break

        except httpx.RequestError as exc:
            print(
                f"[钉钉网络异常] 第{attempt}/{max_attempts}次 "
                f"错误类型={type(exc).__name__} 详情={exc!r}"
            )
            if attempt >= max_attempts:
                print("[钉钉网络异常] 已达到最大重试次数，仍然失败。")
                raise DingTalkNetworkError(f"{type(exc).__name__}: {exc}") from exc
            # 指数退避：1.5s -> 3s
            wait_seconds = 1.5 * (2 ** (attempt - 1))
            print(f"[钉钉重试等待] {wait_seconds:.1f}s 后重试")
            time.sleep(wait_seconds)

    if response is None:
        raise RuntimeError("钉钉请求异常：response 为空")
    print(f"[钉钉响应状态码] {response.status_code}")
    # 打印关键响应头，方便排查网关/服务端链路问题（不同环境头名可能不一样）。
    print(
        "[钉钉响应头] "
        f"x-request-id={response.headers.get('x-request-id') or response.headers.get('X-Request-Id')} "
        f"trace-id={response.headers.get('x-trace-id') or response.headers.get('X-Trace-id')} "
        f"content-type={response.headers.get('content-type')}"
    )

    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}

    print(json.dumps(body, ensure_ascii=False, indent=2))
    if response.status_code >= 400:
        raise RuntimeError(f"钉钉接口调用失败：{json.dumps(body, ensure_ascii=False)}")
    return body
