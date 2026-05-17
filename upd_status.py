from typing import Dict, Optional

import httpx
import requests


def _build_payload(
    uid: str,
    tender_uid: str,
    status: int,
    bid_doc_location_url: str,
    memo: Optional[str],
) -> dict:
    payload = {
        "uid": uid,
        "tenderUid": tender_uid,
        "status": status,
    }
    if bid_doc_location_url:
        payload["bidDocLocationUrl"] = bid_doc_location_url
    if memo is not None:
        payload["memo"] = memo
    return payload


def update_biding_doc_status(
    api_url: str,
    uid: str,
    tender_uid: str,
    bid_doc_location_url: str = "",
    status: int = 3,
    memo: Optional[str] = None,
) -> Dict:
    """
    同步版本：给 server.py 等仍是同步调用的代码继续复用。
    状态约定：
    - status=2：处理中
    - status=3：已生成
    - status=4：生成失败
    """
    payload = _build_payload(
        uid=uid,
        tender_uid=tender_uid,
        status=status,
        bid_doc_location_url=bid_doc_location_url,
        memo=memo,
    )
    response = requests.put(api_url, json=payload, timeout=15)
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}

    print(f"[回写状态响应] uid={uid} status_code={response.status_code} body={body}")
    return body


async def update_biding_doc_status_async(
    api_url: str,
    uid: str,
    tender_uid: str,
    bid_doc_location_url: str = "",
    status: int = 3,
    memo: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict:
    payload = _build_payload(
        uid=uid,
        tender_uid=tender_uid,
        status=status,
        bid_doc_location_url=bid_doc_location_url,
        memo=memo,
    )

    # 如果外面已经传了 client，就沿用同一个连接池。
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        response = await client.put(api_url, json=payload, timeout=15.0)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": response.text}
        print(f"[回写状态响应] uid={uid} status_code={response.status_code} body={body}")
        return body
    finally:
        if own_client and client is not None:
            await client.aclose()
