from typing import Dict, Optional

import requests


def update_biding_doc_status(
    api_url: str,
    uid: str,
    tender_uid: str,
    bid_doc_location_url: str = "",
    status: int = 3,
    memo: Optional[str] = None,
) -> Dict:
    """
    回写业务状态：
    - status=2：处理中（可配 memo，如“正在生成标书”）
    - status=3：已生成（可写下载地址与 memo）
    - status=4：生成失败（可写失败原因 memo）
    """
    # 先放公共字段。status=2（处理中）时不强制传 bidDocLocationUrl。
    payload = {
        "uid": uid,
        "tenderUid": tender_uid,
        "status": status,
    }
    if bid_doc_location_url:
        payload["bidDocLocationUrl"] = bid_doc_location_url
    if memo is not None:
        payload["memo"] = memo
    response = requests.put(api_url, json=payload, timeout=15)
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}

    print(f"[回写状态响应] uid={uid} status_code={response.status_code} body={body}")
    return body
