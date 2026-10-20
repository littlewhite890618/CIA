"""M1 真機驗證工具（tasks 9.2、9.5、9.6）。

自動化測試能證明「我們的邏輯正確」，但證明不了「Meta 的實際行為符合預期」。
這支腳本補的就是後者，需要 uvicorn 與 ngrok 都在執行、且 App 未被封鎖。

**刻意不做成 HTTP 端點**：ngrok 網址是公開的，在上面掛一個能任意發訊的
debug 端點等於把發訊能力對外開放。本機 CLI 沒有這個問題。

用法（在專案根目錄執行）：

    .venv/Scripts/python.exe scripts/m1_verify.py db
    .venv/Scripts/python.exe scripts/m1_verify.py send --text "測試"
    .venv/Scripts/python.exe scripts/m1_verify.py send --raw --text "測試"
    .venv/Scripts/python.exe scripts/m1_verify.py send --long
    .venv/Scripts/python.exe scripts/m1_verify.py webhook bad-signature
    .venv/Scripts/python.exe scripts/m1_verify.py webhook unknown-account
    .venv/Scripts/python.exe scripts/m1_verify.py webhook duplicate
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func

# 讓腳本能從專案根目錄以外的位置執行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels.instagram import InstagramClient, InstagramSendError  # noqa: E402
from app.channels.rate_limit import RateLimiter  # noqa: E402
from app.channels.sender import MessageSender  # noqa: E402
from app.channels.splitter import split_message  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import Message, Persona, RateBudget  # noqa: E402
from app.db.models import Session as SessionModel  # noqa: E402
from app.db.models import UserPersonaLink  # noqa: E402
from app.db.session import db_session  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

settings = get_settings()

NGROK_API = "http://localhost:4040/api/tunnels"

# 去重驗證專用的假 IGSID。命名可辨識，方便日後從資料庫清掉。
SYNTHETIC_IGSID = "SYNTHETIC_DEDUP_TEST"

# 同時包含段落分隔、句末標點、逗號與 emoji，才能一次驗到三種切分優先序。
LONG_TEXT = (
    "這是第一段內容，用來測試訊息切分是否正確運作。"
    "中文一個字佔三個位元組，所以一千個位元組大約只能放三百三十個字。"
    "如果切分邏輯有問題，你會看到句子被從中間斬斷，或是出現亂碼方框。" * 3
    + "\n\n"
    + "這是第二段內容 🌿。這一段刻意加入表情符號，因為 emoji 通常佔四個位元組，"
    "是最容易在位元組邊界上被切壞的字元。若切分正確，每個 emoji 都應該完整顯示。" * 3
    + "\n\n"
    + "這是第三段內容，用來確認多則訊息的抵達順序沒有錯亂。"
    "你應該依序看到第一段、第二段、第三段，而不是顛倒或交錯。" * 3
)


# ═══════════════════════════════════════════════════════════
#  共用
# ═══════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public_url() -> str:
    """從 ngrok 本機 API 取公開網址，避免把網域寫死在腳本裡。"""
    resp = httpx.get(NGROK_API, timeout=5.0)
    resp.raise_for_status()
    tunnels = resp.json().get("tunnels", [])
    if not tunnels:
        raise SystemExit("ngrok 沒有可用的隧道，請先啟動 ngrok")
    return tunnels[0]["public_url"]


def _sign(body: bytes) -> str:
    """以 Instagram 專屬密鑰簽章。

    注意是 IG_APP_SECRET，不是 META_APP_SECRET —— 兩者在 Meta 後台是
    不同欄位，webhook 用的是前者（見 docs/開發環境設定.md A-2 警告框）。
    """
    secret = settings.webhook_signing_secret.encode()
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _context() -> tuple[int, int, str, str]:
    """從資料庫取出 (session_id, persona_id, igsid, entry_id)。"""
    with db_session() as db:
        persona = db.query(Persona).first()
        if persona is None:
            raise SystemExit("personas 表是空的，請先啟動一次服務讓它 seed 角色")
        link = db.query(UserPersonaLink).filter(UserPersonaLink.persona_id == persona.id).first()
        if link is None:
            raise SystemExit("還沒有任何使用者連結，請先用測試帳號私訊角色一次")
        session = (
            db.query(SessionModel)
            .filter(SessionModel.user_id == link.user_id, SessionModel.persona_id == persona.id)
            .order_by(SessionModel.id.desc())
            .first()
        )
        if session is None:
            raise SystemExit("找不到對話場次")
        return session.id, persona.id, link.igsid, persona.ig_account_id


def _message_count() -> int:
    with db_session() as db:
        return db.query(Message).count()


# ═══════════════════════════════════════════════════════════
#  db：印出當前狀態
# ═══════════════════════════════════════════════════════════

def cmd_db(_args: argparse.Namespace) -> None:
    with db_session() as db:
        print("=== 角色 ===")
        for p in db.query(Persona).all():
            print(f"  id={p.id} slug={p.slug} ig_account_id={p.ig_account_id} status={p.status}")

        print("\n=== 使用者連結 ===")
        for link in db.query(UserPersonaLink).all():
            print(f"  user_id={link.user_id} persona_id={link.persona_id} igsid={link.igsid}")

        print("\n=== 對話場次 ===")
        now = _now()
        for s in db.query(SessionModel).all():
            expires = s.window_expires_at
            if expires is None:
                state = "無視窗"
            else:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                hours = (now - expires).total_seconds() / 3600
                state = f"已過期 {hours:.1f} 小時" if hours > 0 else f"尚有 {-hours:.1f} 小時"
            print(f"  id={s.id} state={s.state} count={s.message_count}")
            print(f"     last_message_at   = {s.last_message_at}")
            print(f"     window_expires_at = {s.window_expires_at}  → {state}")

        print("\n=== 訊息統計 ===")
        total = db.query(Message).count()
        print(f"  總計 {total} 筆")
        for direction, count in (
            db.query(Message.direction, func.count())
            .group_by(Message.direction)
            .all()
        ):
            print(f"    {direction}: {count}")

        undelivered = [
            m for m in db.query(Message).all() if (m.meta or {}).get("delivered") is False
        ]
        if undelivered:
            print(f"\n  未送達 {len(undelivered)} 筆：")
            for m in undelivered:
                print(f"    id={m.id} reason={(m.meta or {}).get('reason')} sent_at={m.sent_at}")

        print("\n=== 最後 6 筆訊息 ===")
        for m in db.query(Message).order_by(Message.id.desc()).limit(6).all():
            mid = (m.platform_message_id or "(無)")[:24]
            print(f"  id={m.id} {m.direction:8} mid={mid:24} {m.content[:24]!r}")

        print("\n=== rate_budget（4.8 觀測資料）===")
        rows = db.query(RateBudget).all()
        if not rows:
            print("  (空)")
        for r in rows:
            print(
                f"  persona_id={r.persona_id} window_start={r.window_start} "
                f"sent={r.sent_count} rate_limited={r.rate_limited_count}"
            )

        print(f"\n現在 UTC：{now.strftime('%Y-%m-%d %H:%M:%S')}")


# ═══════════════════════════════════════════════════════════
#  send：驗證發訊路徑
# ═══════════════════════════════════════════════════════════

def cmd_send(args: argparse.Namespace) -> None:
    session_id, persona_id, igsid, _entry_id = _context()
    text = LONG_TEXT if args.long else args.text
    if not text:
        raise SystemExit("請以 --text 指定內容，或用 --long 送預設的長訊息")

    chunks = split_message(text)
    print(f"session_id={session_id} persona_id={persona_id} igsid={igsid}")
    print(f"內容 {len(text.encode('utf-8'))} bytes → 預期切成 {len(chunks)} 則")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] {len(c.encode('utf-8')):4} bytes  {c[:30]!r}…")
    print()

    before = _message_count()

    if args.raw:
        asyncio.run(_send_raw(igsid, chunks))
    else:
        asyncio.run(_send_via_sender(session_id, persona_id, igsid, text))

    print(f"\nmessages 筆數：{before} → {_message_count()}")


async def _send_via_sender(session_id: int, persona_id: int, igsid: str, text: str) -> None:
    """走正式路徑，驗證我方的平台規則。"""
    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    sender = MessageSender(
        client,
        RateLimiter(settings.send_rate_limit_per_hour),
        max_retries=settings.send_max_retries,
        retry_base_seconds=settings.send_retry_base_seconds,
        max_wait_seconds=settings.send_max_wait_seconds,
    )
    try:
        started = time.monotonic()
        outcome = await sender.send_reply(
            session_id=session_id, persona_id=persona_id, igsid=igsid, text=text
        )
    finally:
        await client.aclose()

    print("--- MessageSender 結果 ---")
    print(f"  ok               = {outcome.ok}")
    print(f"  blocked_reason   = {outcome.blocked_reason}")
    print(f"  delivered        = {len(outcome.delivered)} 則")
    print(f"  rate_limited_hits= {outcome.rate_limited_hits}")
    print(f"  耗時             = {time.monotonic() - started:.2f} 秒")


async def _send_raw(igsid: str, chunks: list[str]) -> None:
    """繞過 MessageSender 直接呼叫 API，用來觀察 Meta 的實際行為。

    這是 9.6 的關鍵一步：我方的視窗檢查會擋下逾期發訊，所以要繞過它
    才能看到 Meta 自己怎麼回應。
    """
    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    try:
        print("--- 直接呼叫 Graph API（繞過所有我方檢查）---")
        try:
            message_id = await client.send_text(igsid, chunks[0])
            print(f"  ✅ Meta 接受了，message_id = {message_id}")
            print("  ⚠️ 注意：這表示 Meta 並未拒絕，與預期不符——請記錄此結果")
        except InstagramSendError as exc:
            print(f"  ❌ Meta 拒絕：{exc}")
            print(f"     例外型別：{type(exc).__name__}")
    finally:
        await client.aclose()


# ═══════════════════════════════════════════════════════════
#  webhook：驗證 9.2 三條異常路徑
# ═══════════════════════════════════════════════════════════

def _payload(entry_id: str, igsid: str, mid: str, text: str = "驗證用訊息") -> bytes:
    return json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": entry_id,
                    "time": int(time.time() * 1000),
                    "messaging": [
                        {
                            "sender": {"id": igsid},
                            "recipient": {"id": entry_id},
                            "timestamp": int(time.time() * 1000),
                            "message": {"mid": mid, "text": text},
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _post(url: str, body: bytes, signature: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["X-Hub-Signature-256"] = signature
    return httpx.post(url, content=body, headers=headers, timeout=15.0)


def cmd_webhook(args: argparse.Namespace) -> None:
    _session_id, _persona_id, igsid, entry_id = _context()
    url = f"{_public_url()}/webhook"
    print(f"目標：{url}\n")

    before = _message_count()

    if args.case == "bad-signature":
        # 2a：預期 403，且完全不落庫
        body = _payload(entry_id, igsid, f"mid_badsig_{int(time.time())}")
        resp = _post(url, body, "sha256=" + "0" * 64)
        print(f"HTTP {resp.status_code}（預期 403）")
        after = _message_count()
        print(f"messages 筆數：{before} → {after}（預期不變）")
        _verdict(resp.status_code == 403 and after == before)

    elif args.case == "unknown-account":
        # 2b：預期 200（不中斷批次），但不落庫
        body = _payload("99999999999999", igsid, f"mid_unknown_{int(time.time())}")
        resp = _post(url, body, _sign(body))
        print(f"HTTP {resp.status_code}（預期 200）")
        after = _message_count()
        print(f"messages 筆數：{before} → {after}（預期不變）")
        print("→ 請同時檢查 uvicorn log 是否出現「未知角色帳號，略過此事件」")
        _verdict(resp.status_code == 200 and after == before)

    elif args.case == "duplicate":
        # 2c：同一 payload 送兩次，預期都 200 但只落庫一筆。
        #
        # 刻意用合成的 IGSID 而非真實測試帳號的：真實 IGSID 會落在
        # session 1 上，而入站訊息會把該場次的 24 小時視窗重置——那個
        # 「已自然過期的視窗」是花一整天才形成的，是 9.6 的驗證前提。
        # 合成 IGSID 會建立獨立的使用者與場次，驗證效力不變（去重靠的是
        # platform_message_id 的唯一約束，與發送者無關），但不動到視窗。
        igsid = SYNTHETIC_IGSID
        mid = f"mid_dup_{int(time.time())}"
        body = _payload(entry_id, igsid, mid)
        print(f"使用合成 IGSID：{igsid}（保護 session 1 的過期視窗）\n")
        signature = _sign(body)

        first = _post(url, body, signature)
        print(f"第一次：HTTP {first.status_code}（預期 200）")
        time.sleep(3)  # 等背景回覆處理完，避免與第二次交錯

        mid_count = before
        second = _post(url, body, signature)
        print(f"第二次：HTTP {second.status_code}（預期 200）")
        time.sleep(3)

        with db_session() as db:
            dup = db.query(Message).filter(Message.platform_message_id == mid).count()
        after = _message_count()
        print(f"\nmid={mid} 的筆數：{dup}（預期 1）")
        print(f"messages 總筆數：{before} → {after}")
        print("→ 總筆數會多於 1，因為第一次會觸發固定回覆（出站訊息）")
        _verdict(first.status_code == 200 and second.status_code == 200 and dup == 1)
        _ = mid_count


def _verdict(passed: bool) -> None:
    print("\n" + ("✅ 符合預期" if passed else "❌ 不符預期"))


# ═══════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="M1 真機驗證工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("db", help="印出資料庫當前狀態").set_defaults(func=cmd_db)

    p_send = sub.add_parser("send", help="發訊驗證")
    p_send.add_argument("--text", default="")
    p_send.add_argument("--long", action="store_true", help="送預設的長訊息（超過 1000 bytes）")
    p_send.add_argument("--raw", action="store_true", help="繞過 MessageSender 直接打 API")
    p_send.set_defaults(func=cmd_send)

    p_hook = sub.add_parser("webhook", help="webhook 異常路徑驗證")
    p_hook.add_argument("case", choices=["bad-signature", "unknown-account", "duplicate"])
    p_hook.set_defaults(func=cmd_webhook)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
