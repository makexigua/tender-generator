import os
import traceback
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import requests

from chat2dingtalk_async import send_to_dingtalk
from upd_status import update_biding_doc_status


# 线程安全的打印锁
_print_lock = threading.Lock()
_FIXED_PUBLIC_DOWNLOAD_BASE_URL = "https://ai-assistant.4-xiang.com/download"

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
    1) 优先读环境变量 ATTACHMENT_LOCAL_DIR
    2) 其次，如果是 root 用户，默认 /root/generated_docs（和 server.py 下载路由一致）
    3) 否则回退到项目内「招标文件」目录
    """
    env_dir = os.getenv("ATTACHMENT_LOCAL_DIR", "").strip()
    if env_dir:
        return Path(env_dir)

    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return Path("/root/generated_docs")
    except Exception:
        pass

    return Path(__file__).resolve().parent / "招标文件"


def _download_source_docs_and_build_attachment_urls(
    source_doc_urls: list[str],
    local_attachment_dir: Path,
) -> list[str]:
    """
    把招标文件先下载到本地目录，再生成可回传给钉钉的附件 URL。
    附件 URL 固定格式：https://ai-assistant.4-xiang.com/download/<文件名>
    """
    local_attachment_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    attachment_urls: list[str] = []

    for index, source_url in enumerate(source_doc_urls):
        raw_name = _file_name_from_url(source_url, index + 1)
        safe_name = _unique_file_name(raw_name, used_names, local_attachment_dir)
        local_path = local_attachment_dir / safe_name

        # 先把文件下载到本地，确保 attachments 指向的是我们可控地址。
        with requests.get(source_url, timeout=60, stream=True) as resp:
            resp.raise_for_status()
            with local_path.open("wb") as file_obj:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        file_obj.write(chunk)

        public_url = f"{_FIXED_PUBLIC_DOWNLOAD_BASE_URL}/{quote(safe_name)}"
        attachment_urls.append(public_url)

    return attachment_urls


def _build_agent_input_text(
    base_input_text: str,
    uid: str,
    tender_uid: str,
) -> str:
    """
    给钉钉 Agent 补充业务上下文。
    大白话：主流程只负责派单，真正生成文件和回写状态要交给 MCP 的 generate-docx 工具完成，
    所以这里必须把 uid/tenderUid 明确告诉 Agent。
    """
    return (
        f"{base_input_text}\n\n"
        "【业务回写信息】\n"
        f"- uid: {uid}\n"
        f"- tender_uid: {tender_uid}\n\n"
        "【生成和回写要求】\n"
        "1. 请根据附件内容生成完整投标文件正文。\n"
        "2. 生成完成后必须调用 MCP 工具 generate-docx。\n"
        "3. 调用 generate-docx 时必须传 uid、tender_uid、filename、content 四个参数。\n"
        "4. generate-docx 执行完成后会生成 Word 文件、回写业务状态，并返回可下载地址。\n"
        "5. 不要自行编造下载地址，必须以 generate-docx 工具返回的下载地址为准。"
    )


def _process_single_item(
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
) -> None:
    """
    处理单条任务（供线程池调用）。
    """
    step = "init"
    try:
        if item.get("status") != 1:
            return

        uid = str(item.get("uid", "")).strip()
        tender_uid = str(item.get("tenderUid", "")).strip()
        source_doc_location = str(item.get("biddingDocLocation", "")).strip()
        if not uid or not tender_uid or not source_doc_location:
            return

        # biddingDocLocation 可能是逗号分隔的多个地址，先拆开再逐个标准化为 https。
        source_doc_urls = []
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
        if not source_doc_urls:
            _safe_print(f"[任务跳过] uid={uid} tenderuid={tender_uid} 原因=没有可用附件URL")
            return
        _safe_print(f"[URL处理结果] uid={uid} tenderuid={tender_uid} count={len(source_doc_urls)} urls={source_doc_urls}")

        # ========== 修改点1：先下载源文件到本地，再构造 attachments 的对外访问地址 ==========
        step = "prepare_attachments"
        attachment_urls = _download_source_docs_and_build_attachment_urls(
            source_doc_urls=source_doc_urls,
            local_attachment_dir=local_attachment_dir,
        )
        _safe_print(
            f"[附件准备完成] uid={uid} tenderuid={tender_uid} attachment_count={len(attachment_urls)} "
            f"local_dir={local_attachment_dir}"
        )
        _safe_print(f"[附件URL] uid={uid} tenderuid={tender_uid} urls={attachment_urls}")

        # 先把任务状态从 1 改成 2（处理中），用于"抢占"任务，减少重复处理。
        step = "update_status_2"
        _safe_print(f"[正在写标书] uid={uid} tenderuid={tender_uid} status=2")
        status2_resp = update_biding_doc_status(
            api_url=upd_status_api_url,
            uid=uid,
            tender_uid=tender_uid,
            status=2,
            memo="正在生成标书",
        )
        _safe_print(f"[status=2回写完成] uid={uid} tenderuid={tender_uid} resp={status2_resp}")

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

        # ========== 修改点2：只负责把任务发给钉钉助理 ==========
        # 主流程不再解析钉钉返回的文件地址，也不再回写 status=3/status=4。
        # 文件生成、下载地址返回、成功/失败回写都由 MCP server 的 generate-docx 工具闭环处理。
        try:
            send_to_dingtalk(
                source_doc_urls=attachment_urls,
                api_url=dingtalk_api_url,
                bearer_token=dingtalk_bearer_token,
                assistant_id=dingtalk_assistant_id,
                union_id=dingtalk_union_id,
                input_text=agent_input_text,
                stream=dingtalk_stream,
                thread_id=dingtalk_thread_id,
            )
        except requests.exceptions.RequestException as net_exc:
            _safe_print(
                f"[钉钉网络问题] uid={uid} tenderuid={tender_uid} step=call_dingtalk "
                f"type={type(net_exc).__name__} detail={net_exc!r}"
            )
            return
        except Exception as call_exc:
            _safe_print(
                f"[钉钉接口报错] uid={uid} tenderuid={tender_uid} step=call_dingtalk "
                f"type={type(call_exc).__name__} detail={call_exc!r}"
            )
            return

        _safe_print(f"[钉钉请求发送完成] uid={uid} tenderuid={tender_uid} 后续由MCP generate-docx回写状态")

    except Exception as exc:
        _safe_print(
            f"[处理失败] uid={item.get('uid')} tenderuid={item.get('tenderUid')} "
            f"step={step}，错误：{exc!r}"
        )
        _safe_print(
            f"[处理失败堆栈] uid={item.get('uid')} tenderuid={item.get('tenderUid')} "
            f"traceback={traceback.format_exc()}"
        )


def main() -> None:
    """
    主流程：
    1) 拉取列表
    2) 对 status=1 的记录用线程池并行调用钉钉
    3) 先把招标文件下载到本地目录，再把对外下载地址作为 attachments 传给钉钉
    4) 只负责发送钉钉请求；文件生成、下载地址、status=3/status=4 回写交给 MCP generate-docx
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
        f"public_download_base_url={_FIXED_PUBLIC_DOWNLOAD_BASE_URL}"
    )

    upd_status_api_url = _require_env("BIDING_DOC_UPD_STATUS_URL")

    # 可配置线程数，默认 5
    max_workers = int(os.getenv("MAX_WORKERS", "5").strip() or "5")

    list_api_url = "https://api.4-xiang.com/admin/tender/biding_doc/list"
    list_resp = requests.get(list_api_url, timeout=10)
    list_resp.raise_for_status()
    list_payload = list_resp.json()
    if list_payload.get("code") != 0 or list_payload.get("success") is not True:
        return

    rows = list_payload.get("data")
    if not isinstance(rows, list):
        return

    # 过滤出 status=1 的任务
    pending_items = [item for item in rows if item.get("status") == 1]
    if not pending_items:
        print("[主流程] 没有 status=1 的待处理任务")
        return

    print(f"[主流程] 发现 {len(pending_items)} 个待处理任务，启动 {max_workers} 线程并行处理")

    # ========== 修改点3：多线程并行处理 ==========
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_single_item,
                item,
                dingtalk_api_url,
                dingtalk_bearer_token,
                dingtalk_assistant_id,
                dingtalk_union_id,
                dingtalk_input_text,
                dingtalk_stream,
                dingtalk_thread_id,
                upd_status_api_url,
                local_attachment_dir,
            ): item
            for item in pending_items
        }

        for future in as_completed(futures):
            item = futures[future]
            uid = item.get("uid", "unknown")
            try:
                future.result()
            except Exception as exc:
                _safe_print(f"[线程异常] uid={uid} 任务执行失败: {exc!r}")

    print("[主流程] 所有任务处理完成")


if __name__ == "__main__":
    main()
