import asyncio
import os
import threading
import traceback
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

from chat2dingtalk import (
    DingTalkNetworkError,
    extract_output_text,
    extract_output_url,
    send_to_dingtalk,
)
from upd_status import update_biding_doc_status_async


# 打印本身不是并发安全的，这里保留锁，避免日志互相“穿插”难排查。
_print_lock = threading.Lock()
_FIXED_PUBLIC_DOWNLOAD_BASE_URL = "<招标文件接口>"
_DEFAULT_LOCAL_DOC_DIR_NAME = "招标文件"


def _safe_print(msg: str) -> None:
    with _print_lock:
        print(msg)


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少配置：{name}")
    return value


def _to_bool(text: str, default: bool = False) -> bool:
    if text is None:
        return default
    text = text.strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


def _get_public_download_base_url() -> str:
    return os.getenv("PUBLIC_DOWNLOAD_BASE_URL", _FIXED_PUBLIC_DOWNLOAD_BASE_URL).rstrip("/")


def _file_name_from_url(url: str, index: int) -> str:
    """
    从 URL 里提取文件名，提取失败就给一个兜底名，避免提示词里是空文件名。
    """
    name = Path(urlparse(url).path).name
    if name:
        return unquote(name)
    return f"附件{index}.bin"


def _unique_file_name(file_name: str, used_names: set[str], local_attachment_dir: Path) -> str:
    """
    如果文件名冲突，自动加序号后缀，避免覆盖之前下载的文件。
    """
    candidate = file_name
    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    counter = 2
    while candidate in used_names or (local_attachment_dir / candidate).exists():
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def _resolve_local_attachment_dir() -> Path:
    """
    决定附件落地目录：
    1) 优先读 GENERATED_DOCS_DIR（主流程和 MCP 服务共用）
    2) 兼容历史变量 ATTACHMENT_LOCAL_DIR
    3) 其次，如果是 root 用户，默认 /root/generated_docs
    4) 否则回退到项目内「招标文件」目录
    """
    env_dir = os.getenv("GENERATED_DOCS_DIR", "").strip()
    if not env_dir:
        env_dir = os.getenv("ATTACHMENT_LOCAL_DIR", "").strip()
    if env_dir:
        return Path(env_dir)

    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return Path("/root/generated_docs")
    except Exception:
        pass

    return Path(__file__).resolve().parent / _DEFAULT_LOCAL_DOC_DIR_NAME


def _normalize_source_doc_urls(source_doc_location: str, uid: str, tender_uid: str) -> list[str]:
    """
    biddingDocLocation 可能是逗号分隔，统一在这里做 URL 清洗：
    - 过滤空值
    - 只保留 http/https
    - 自动把 http 升级成 https
    """
    source_doc_urls: list[str] = []
    for raw_url in source_doc_location.split(","):
        url = raw_url.strip()
        _safe_print(f"[URL原始片段] uid={uid} tenderuid={tender_uid} raw={raw_url}")
        if not url:
            _safe_print(f"[URL跳过] uid={uid} tenderuid={tender_uid} 原因=空片段")
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            _safe_print(f"[URL跳过] uid={uid} tenderuid={tender_uid} url={url} 原因=不是http/https")
            continue
        if url.startswith("http://"):
            url = url.replace("http://", "https://", 1)
            _safe_print(f"[URL升级HTTPS] uid={uid} tenderuid={tender_uid} url={url}")
        source_doc_urls.append(url)
    return source_doc_urls


async def _write_failed_status_if_needed(
    *,
    status2_written: bool,
    upd_status_api_url: str,
    uid: str,
    tender_uid: str,
    reason: str,
    client: httpx.AsyncClient,
) -> None:
    """
    任务已经抢占成 status=2 后，如果后面任何步骤失败，就把它明确改成 4，
    """
    if not status2_written:
        return
    try:
        fail_resp = await update_biding_doc_status_async(
            api_url=upd_status_api_url,
            uid=uid,
            tender_uid=tender_uid,
            status=4,
            memo=reason[:1000],  # 防止异常信息过长导致回写接口拒绝
            client=client,
        )
        _safe_print(f"[status=4兜底回写完成] uid={uid} tenderuid={tender_uid} resp={fail_resp}")
    except Exception as write_exc:
        _safe_print(
            f"[status=4兜底回写失败] uid={uid} tenderuid={tender_uid} "
            f"type={type(write_exc).__name__} detail={write_exc!r}"
        )


async def _download_source_docs_and_build_attachment_urls(
    source_doc_urls: list[str],
    local_attachment_dir: Path,
    client: httpx.AsyncClient,
) -> list[str]:
    """
    把招标文件先下载到本地目录，再生成可回传给钉钉的附件 URL。
    附件 URL 格式：<PUBLIC_DOWNLOAD_BASE_URL>/<文件名>
    """
    local_attachment_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    attachment_urls: list[str] = []
    public_download_base_url = _get_public_download_base_url()

    for index, source_url in enumerate(source_doc_urls):
        raw_name = _file_name_from_url(source_url, index + 1)
        safe_name = _unique_file_name(raw_name, used_names, local_attachment_dir)
        local_path = local_attachment_dir / safe_name

        # 先把文件下载到本地，确保 attachments 指向的是我们可控地址。
        async with client.stream("GET", source_url, timeout=60.0) as resp:
            resp.raise_for_status()
            with local_path.open("wb") as file_obj:
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    if chunk:
                        file_obj.write(chunk)

        public_url = f"{public_download_base_url}/{quote(safe_name)}"
        attachment_urls.append(public_url)

    return attachment_urls


def _build_agent_input_text(
    base_input_text: str,
    uid: str,
    tender_uid: str,
) -> str:
    """
    给钉钉 Agent 补充业务上下文。
    主流程只负责派单，真正生成文件和回写状态要交给 MCP 的 generate-docx 工具完成，
    所以这里必须把 uid/tenderUid 明确告诉 Agent。
    """
    return (
        f"{base_input_text}\n\n"
        "【业务回写信息】\n"
        f"- uid: {uid}\n"
        f"- tender_uid: {tender_uid}"
    )


async def _process_single_item(
    item: dict,
    dingtalk_api_url: str,
    dingtalk_bearer_token: str,
    dingtalk_assistant_id: str,
    dingtalk_union_id: str,
    dingtalk_input_text: str,
    dingtalk_stream: bool,
    dingtalk_thread_id: str,
    upd_status_api_url: str,
    local_attachment_dir: Path,
    client: httpx.AsyncClient,
) -> None:
    """
    处理单条任务（纯异步版本）。
    """
    step = "init"
    uid = str(item.get("uid", "")).strip()
    tender_uid = str(item.get("tenderUid", "")).strip()
    status2_written = False
    try:
        if item.get("status") != 1:
            return

        source_doc_location = str(item.get("biddingDocLocation", "")).strip()
        if not uid or not tender_uid or not source_doc_location:
            return

        source_doc_urls = _normalize_source_doc_urls(source_doc_location, uid, tender_uid)
        if not source_doc_urls:
            _safe_print(f"[任务跳过] uid={uid} tenderuid={tender_uid} 原因=没有可用附件URL")
            return
        _safe_print(f"[URL处理结果] uid={uid} tenderuid={tender_uid} count={len(source_doc_urls)} urls={source_doc_urls}")

        # 先把任务状态从 1 改成 2（处理中），用于“抢占”任务，减少重复处理。
        # 放在下载附件之前，避免多实例并发时重复下载、重复派单。
        step = "update_status_2"
        _safe_print(f"[正在写标书] uid={uid} tenderuid={tender_uid} status=2")
        status2_resp = await update_biding_doc_status_async(
            api_url=upd_status_api_url,
            uid=uid,
            tender_uid=tender_uid,
            status=2,
            memo="正在生成标书",
            client=client,
        )
        status2_written = True
        _safe_print(f"[status=2回写完成] uid={uid} tenderuid={tender_uid} resp={status2_resp}")

        # 再下载源文件到本地，并构造 attachments 的对外访问地址。
        step = "prepare_attachments"
        attachment_urls = await _download_source_docs_and_build_attachment_urls(
            source_doc_urls=source_doc_urls,
            local_attachment_dir=local_attachment_dir,
            client=client,
        )
        _safe_print(
            f"[附件准备完成] uid={uid} tenderuid={tender_uid} attachment_count={len(attachment_urls)} "
            f"local_dir={local_attachment_dir}"
        )
        _safe_print(f"[附件URL] uid={uid} tenderuid={tender_uid} urls={attachment_urls}")

        step = "call_dingtalk"
        _safe_print(
            f"[调用钉钉前] uid={uid} tenderuid={tender_uid} attachment_count={len(attachment_urls)} "
            f"api_url={dingtalk_api_url} thread_id={dingtalk_thread_id or '(empty)'} stream={dingtalk_stream}"
        )
        _safe_print(f"[检测到招标文件] {source_doc_urls}")

        agent_input_text = _build_agent_input_text(
            base_input_text=dingtalk_input_text,
            uid=uid,
            tender_uid=tender_uid,
        )

        try:
            dingtalk_resp = await send_to_dingtalk(
                source_doc_urls=attachment_urls,
                api_url=dingtalk_api_url,
                bearer_token=dingtalk_bearer_token,
                assistant_id=dingtalk_assistant_id,
                union_id=dingtalk_union_id,
                input_text=agent_input_text,
                stream=dingtalk_stream,
                thread_id=dingtalk_thread_id,
                client=client,
            )
        except (DingTalkNetworkError, httpx.RequestError) as net_exc:
            _safe_print(
                f"[钉钉网络问题] uid={uid} tenderuid={tender_uid} step=call_dingtalk "
                f"type={type(net_exc).__name__} detail={net_exc!r}"
            )
            await _write_failed_status_if_needed(
                status2_written=status2_written,
                upd_status_api_url=upd_status_api_url,
                uid=uid,
                tender_uid=tender_uid,
                reason=f"调用钉钉网络失败：{type(net_exc).__name__}: {net_exc}",
                client=client,
            )
            return
        except Exception as call_exc:
            _safe_print(
                f"[钉钉接口报错] uid={uid} tenderuid={tender_uid} step=call_dingtalk "
                f"type={type(call_exc).__name__} detail={call_exc!r}"
            )
            await _write_failed_status_if_needed(
                status2_written=status2_written,
                upd_status_api_url=upd_status_api_url,
                uid=uid,
                tender_uid=tender_uid,
                reason=f"调用钉钉失败：{type(call_exc).__name__}: {call_exc}",
                client=client,
            )
            return

        # 主流程不负责把 status 改 3，还是由 MCP 的 generate-docx 来做。
        # 但这里会校验“是否看起来真的走到了 generate-docx 成功结果”，
        # 否则把状态兜底改成 4，避免任务一直卡在 2。
        output_text = extract_output_text(dingtalk_resp) or ""
        output_url = extract_output_url(dingtalk_resp, source_doc_urls=attachment_urls)
        has_success_marker = bool(output_url) or ("状态已回写为生成成功" in output_text) or ("文件已生成" in output_text)
        if not has_success_marker:
            _safe_print(
                f"[钉钉返回疑似未完成] uid={uid} tenderuid={tender_uid} "
                f"未检测到generate-docx成功标记，output_text={output_text!r}"
            )
            await _write_failed_status_if_needed(
                status2_written=status2_written,
                upd_status_api_url=upd_status_api_url,
                uid=uid,
                tender_uid=tender_uid,
                reason="钉钉已响应，但未检测到 generate-docx 成功标记",
                client=client,
            )
            return

        _safe_print(
            f"[钉钉请求发送完成] uid={uid} tenderuid={tender_uid} "
            f"已检测到MCP成功标记 output_url={output_url or '(none)'}"
        )

    except Exception as exc:
        _safe_print(
            f"[处理失败] uid={item.get('uid')} tenderuid={item.get('tenderUid')} "
            f"step={step}，错误：{exc!r}"
        )
        _safe_print(
            f"[处理失败堆栈] uid={item.get('uid')} tenderuid={item.get('tenderUid')} "
            f"traceback={traceback.format_exc()}"
        )
        await _write_failed_status_if_needed(
            status2_written=status2_written,
            upd_status_api_url=upd_status_api_url,
            uid=uid,
            tender_uid=tender_uid,
            reason=f"主流程异常：step={step} {type(exc).__name__}: {exc}",
            client=client,
        )


async def main() -> None:
    """
    主流程：
    1) 拉取列表
    2) 对 status=1 的记录并发处理（Semaphore 限流）
    3) 先把任务改成 status=2 抢占，再下载招标文件到本地目录并组装 attachments
    4) 调钉钉触发 MCP generate-docx；失败回写 status=4，成功仍由 MCP 回写 status=3
    """
    _load_env()

    dingtalk_api_url = _require_env("DINGTALK_API_URL")
    dingtalk_bearer_token = _require_env("DINGTALK_BEARER_TOKEN")
    dingtalk_assistant_id = _require_env("DINGTALK_ASSISTANT_ID")
    dingtalk_union_id = _require_env("DINGTALK_UNION_ID")
    dingtalk_input_text = _require_env("DINGTALK_INPUT_TEXT")
    dingtalk_stream = _to_bool(os.getenv("DINGTALK_STREAM", "false"), default=False)
    dingtalk_thread_id = os.getenv("DINGTALK_THREAD_ID", "").strip()
    local_attachment_dir = _resolve_local_attachment_dir()
    _safe_print(
        f"[附件配置] local_attachment_dir={local_attachment_dir} "
        f"public_download_base_url={_get_public_download_base_url()}"
    )

    upd_status_api_url = _require_env("BIDING_DOC_UPD_STATUS_URL")

    # 并发度仍可通过环境变量调，兼容原来的 MAX_WORKERS。
    max_concurrency = int(
        os.getenv("MAX_CONCURRENCY", os.getenv("MAX_WORKERS", "10")).strip() or "10"
    )
    if max_concurrency <= 0:
        raise ValueError("MAX_CONCURRENCY/MAX_WORKERS 必须大于 0")

    list_api_url = "<回写接口>"
    client_limits = httpx.Limits(
        max_connections=max(20, max_concurrency * 4),
        max_keepalive_connections=max(10, max_concurrency * 2),
    )
    client_timeout = httpx.Timeout(30.0, connect=10.0, read=1000.0)

    async with httpx.AsyncClient(http2=True, timeout=client_timeout, limits=client_limits) as client:
        list_resp = await client.get(list_api_url, timeout=10.0)
        list_resp.raise_for_status()
        list_payload = list_resp.json()
        if list_payload.get("code") != 0 or list_payload.get("success") is not True:
            _safe_print(f"[主流程] 列表接口返回异常：{list_payload}")
            return

        rows = list_payload.get("data")
        if not isinstance(rows, list):
            _safe_print("[主流程] 列表接口 data 不是数组，跳过处理")
            return

        # 过滤出 status=1 的任务
        pending_items = [item for item in rows if item.get("status") == 1]
        if not pending_items:
            _safe_print("[主流程] 没有 status=1 的待处理任务")
            return

        _safe_print(f"[主流程] 发现 {len(pending_items)} 个待处理任务，并发上限={max_concurrency}")

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _bounded_process(item: dict) -> None:
            async with semaphore:
                await _process_single_item(
                    item=item,
                    dingtalk_api_url=dingtalk_api_url,
                    dingtalk_bearer_token=dingtalk_bearer_token,
                    dingtalk_assistant_id=dingtalk_assistant_id,
                    dingtalk_union_id=dingtalk_union_id,
                    dingtalk_input_text=dingtalk_input_text,
                    dingtalk_stream=dingtalk_stream,
                    dingtalk_thread_id=dingtalk_thread_id,
                    upd_status_api_url=upd_status_api_url,
                    local_attachment_dir=local_attachment_dir,
                    client=client,
                )

        # return_exceptions=True：保证某一单失败不会中断整批任务。
        results = await asyncio.gather(
            *(_bounded_process(item) for item in pending_items),
            return_exceptions=True,
        )

        for item, result in zip(pending_items, results):
            if isinstance(result, Exception):
                _safe_print(f"[任务异常] uid={item.get('uid', 'unknown')} detail={result!r}")

    _safe_print("[主流程] 所有任务处理完成")


if __name__ == "__main__":
    asyncio.run(main())
