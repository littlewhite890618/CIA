"""使用者設定頁 API（add-persona-authoring-console 第 6 群）。

走真的 HTTP 請求：讀取、保存（含各種驗證錯誤）、系統底線、自動內容檢查、
教練設定、恢復預設，以及 token 失效的各種情況。最後確認保存後下一則
私訊的回覆真的套用了新設定。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.settings_link import issue_token
from tests.test_reply_generation import IG_ACCOUNT_ID, _post, make_client  # noqa: F401

SECRET = "s" * 40  # 與 make_client(settings_link=True) 相同


def _link_id(igsid: str) -> int:
    from app.db.models import Persona, UserPersonaLink
    from app.db.session import db_session

    with db_session() as db:
        persona = db.query(Persona).filter(Persona.ig_account_id == IG_ACCOUNT_ID).one()
        return (
            db.query(UserPersonaLink)
            .filter(UserPersonaLink.persona_id == persona.id, UserPersonaLink.igsid == igsid)
            .one()
            .id
        )


@pytest.fixture
def api(make_client):
    """回傳 (client, token)：已有一位聊過天的使用者，token 屬於他。"""
    client = make_client(replies=["嗨呀。", "我在呀。"], settings_link=True)
    # 對外網址是 https，登入代碼的 cookie 帶 Secure；測試用戶端改走 https 才會送回去
    client.base_url = "https://testserver"
    _post(client, "mid_1", "嗨", "IGSID_API")
    token = issue_token(_link_id("IGSID_API"), secret=SECRET, ttl_seconds=1800)
    return client, token


def _get(client, token):
    return client.get("/api/settings", params={"t": token})


def _payload(client, token, **card_changes):
    data = _get(client, token).json()
    card = copy.deepcopy(data["current"]["card"])
    for path, value in card_changes.items():
        section, key = path.split("__")
        card[section][key] = value
    return {"card": card, "coaching": copy.deepcopy(data["current"]["coaching"])}


def _save(client, token, payload):
    return client.post("/api/settings", params={"t": token}, json=payload)


# ═══════════════════════════════════════════════════════════
#  讀取
# ═══════════════════════════════════════════════════════════

def test_讀取_沒有版本時目前設定等於平台預設(api):
    client, token = api
    resp = _get(client, token)
    assert resp.status_code == 200
    data = resp.json()
    assert data["personaName"] == "晴晴"
    assert data["igDisplayName"] == "溫柔晴晴 Sunny"
    assert data["current"] == data["defaults"]
    assert data["defaults"]["card"]["identity"]["age"] == "26"
    assert data["defaults"]["card"]["runtime"] == {"delay_min": "8", "delay_max": "20", "typing_indicator": "true"}
    assert "索取電話 / LINE / 其他聯絡方式" in data["baselineNeverEngages"]
    assert data["chatUrl"] == "https://www.instagram.com/direct/inbox/"  # 未設定 IG_USERNAME


def test_讀取_回應不快取也不帶出Referer(api):
    """網址裡有 token，不能被快取，也不能被當成 Referer 送去字型服務或 Instagram。"""
    client, token = api
    resp = _get(client, token)
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_讀取_到期時間與token一致(api):
    client, token = api
    expires = datetime.fromisoformat(_get(client, token).json()["expiresAt"])
    remaining = expires - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)


# ═══════════════════════════════════════════════════════════
#  token 失效
# ═══════════════════════════════════════════════════════════

def test_過期的連結(api):
    client, _ = api
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token = issue_token(_link_id("IGSID_API"), secret=SECRET, ttl_seconds=60, now=past)
    resp = _get(client, token)
    assert resp.status_code == 401
    # 簽章正確的過期連結：持有者本來就拿過連結，告訴他要傳哪個關鍵字
    assert resp.json() == {"error": "expired", "keyword": "芝麻開門"}


@pytest.mark.parametrize("token", ["", "garbage", "a.b"])
def test_無效的連結(api, token):
    client, _ = api
    resp = _get(client, token)
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid"}


def test_簽章正確但對應不到使用者也回無效(api):
    """不透露「這個編號的使用者存不存在」：與竄改同一個回應。"""
    client, _ = api
    resp = _get(client, issue_token(999_999, secret=SECRET, ttl_seconds=1800))
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid"}


def test_沒有token的保存與恢復預設都被拒絕(api):
    client, token = api
    payload = _payload(client, token)
    assert client.post("/api/settings", json=payload).status_code == 401
    assert client.delete("/api/settings").status_code == 401


def test_功能未啟用時回404(make_client):
    client = make_client()
    assert client.get("/api/settings", params={"t": "x"}).status_code == 404


# ═══════════════════════════════════════════════════════════
#  保存
# ═══════════════════════════════════════════════════════════

def test_保存後讀回新設定且下一則回覆套用(api):
    client, token = api
    payload = _payload(client, token, personality__core_traits="活潑外向、愛分享", identity__age="28")
    resp = _save(client, token, payload)
    assert resp.status_code == 200, resp.text

    current = _get(client, token).json()["current"]["card"]
    assert current["personality"]["core_traits"] == "活潑外向、愛分享"
    assert current["identity"]["age"] == "28"

    _post(client, "mid_2", "在嗎", "IGSID_API")
    prompt = client._fake_provider.calls[-1]["system_prompt"]
    assert "活潑外向、愛分享" in prompt
    assert "28歲" in prompt


def test_偷改名字不會生效(api):
    """名字與 IG 名稱沿用共用角色；請求裡帶了別的名字也不會被套用。"""
    client, token = api
    payload = _payload(client, token, identity__name="別人")
    assert _save(client, token, payload).status_code == 200
    assert _get(client, token).json()["current"]["card"]["identity"]["name"] == "晴晴"


@pytest.mark.parametrize(
    ("changes", "path", "message"),
    [
        ({"background__interests": []}, "background.interests", "至少留一項。"),
        ({"background__recent_life": "  "}, "background.recent_life", "這一欄要填。"),
        ({"identity__age": "二十六"}, "identity.age", "寫一個數字。"),
        ({"language_style__typo_rate": "5"}, "language_style.typo_rate", "寫一個 0 到 1 之間的數字，例如 0.03。"),
        ({"runtime__delay_min": "30", "runtime__delay_max": "10"}, "runtime.delay_min", "最快不能比最慢還慢。"),
    ],
)
def test_驗證失敗時指出欄位且原設定不變(api, changes, path, message):
    client, token = api
    before = _get(client, token).json()["current"]
    resp = _save(client, token, _payload(client, token, **changes))
    assert resp.status_code == 422
    assert resp.json()["errors"][path] == message
    assert _get(client, token).json()["current"] == before


def test_移除系統底線被拒絕(api):
    client, token = api
    payload = _payload(client, token, boundaries__never_engages=["前三次對話就約見面"])
    resp = _save(client, token, payload)
    assert resp.status_code == 422
    assert "系統底線" in resp.json()["errors"]["boundaries.never_engages"]


def test_在系統底線之外加項目可以保存(api):
    client, token = api
    data = _get(client, token).json()
    never = data["current"]["card"]["boundaries"]["never_engages"] + ["要求傳照片"]
    assert _save(client, token, _payload(client, token, boundaries__never_engages=never)).status_code == 200


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("boundaries__warms_up_to", ["使用者願意談自己真實的感受", "對方主動要她的 LINE"]),
        ("background__recent_life", "最近很想約炮"),
        ("personality__initiative", "高 —— 喜歡開黃腔"),
    ],
)
def test_自動檢查擋下不當內容(api, path, value):
    client, token = api
    resp = _save(client, token, _payload(client, token, **{path: value}))
    assert resp.status_code == 422
    assert "不適合放進角色設定" in resp.json()["errors"][path.replace("__", ".")]


def test_自動檢查不擋角色會拒絕的項目(api):
    """「會變冷淡」「絕不接」列的是角色拒絕的事，出現性暗示、聯絡方式是正常的。"""
    client, token = api
    data = _get(client, token).json()
    cools = data["current"]["card"]["boundaries"]["cools_down_on"] + ["一直要她的 LINE"]
    assert _save(client, token, _payload(client, token, boundaries__cools_down_on=cools)).status_code == 200


def test_平台預設內容本身不會被自動檢查擋下(api):
    client, token = api
    assert _save(client, token, _payload(client, token)).status_code == 200


def test_原樣保存後讀回仍等於預設(api):
    """存一次沒改過的設定，讀回來不能變成「跟預設不同」，否則頁籤會一直標記改過。"""
    client, token = api
    assert _save(client, token, _payload(client, token)).status_code == 200
    data = _get(client, token).json()
    assert data["current"] == data["defaults"]


# ═══════════════════════════════════════════════════════════
#  教練設定
# ═══════════════════════════════════════════════════════════

def test_教練設定保存後讀回(api):
    client, token = api
    payload = _payload(client, token)
    payload["coaching"]["disabledRuleIds"] = ["L9", "K2", "不存在的規則"]
    payload["coaching"]["customRules"] = [
        {"id": "custom-1", "text": "一直問對方有沒有男女朋友", "type": "landmine", "dimension": "分寸與界線", "severity": "medium"}
    ]
    payload["coaching"]["reportTone"] = "直接具體：重點先講，不繞圈子"
    payload["coaching"]["reportSections"] = payload["coaching"]["reportSections"][:2]
    assert _save(client, token, payload).status_code == 200

    coaching = _get(client, token).json()["current"]["coaching"]
    assert coaching["disabledRuleIds"] == ["K2", "L9"]  # 不存在的規則被濾掉
    assert coaching["customRules"][0]["text"] == "一直問對方有沒有男女朋友"
    assert coaching["reportTone"] == "直接具體：重點先講，不繞圈子"
    assert [s["key"] for s in coaching["reportSections"]] == ["摘要", "做得好"]


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"text": "x", "type": "landmine", "dimension": "分寸與界線"}, "地雷要選嚴重程度。"),
        ({"text": "x", "type": "landmine", "dimension": "不存在", "severity": "low"}, "自訂規則要歸在六項之一。"),
        ({"text": "", "type": "highlight", "dimension": "情緒感知"}, "自訂規則要寫要看的行為，最多 200 字。"),
    ],
)
def test_自訂規則不完整被拒絕(api, rule, message):
    client, token = api
    payload = _payload(client, token)
    payload["coaching"]["customRules"] = [rule]
    resp = _save(client, token, payload)
    assert resp.status_code == 422
    assert resp.json()["errors"]["coaching.customRules"] == message


def test_報告段落全部停用被拒絕(api):
    client, token = api
    payload = _payload(client, token)
    payload["coaching"]["reportSections"] = []
    resp = _save(client, token, payload)
    assert resp.status_code == 422
    assert resp.json()["errors"]["coaching.reportSections"] == "至少留一個段落。"


# ═══════════════════════════════════════════════════════════
#  恢復預設
# ═══════════════════════════════════════════════════════════

def test_恢復預設後回到平台預設版本(api):
    client, token = api
    payload = _payload(client, token, personality__core_traits="活潑外向、愛分享")
    payload["coaching"]["disabledRuleIds"] = ["L9"]
    assert _save(client, token, payload).status_code == 200

    resp = client.delete("/api/settings", params={"t": token})
    assert resp.status_code == 200
    data = _get(client, token).json()
    assert data["current"] == data["defaults"]


def test_沒有版本時恢復預設也成功(api):
    client, token = api
    assert client.delete("/api/settings", params={"t": token}).status_code == 200


# ═══════════════════════════════════════════════════════════
#  頁面
# ═══════════════════════════════════════════════════════════

def test_設定頁回傳打包好的頁面且不帶出Referer(api, tmp_path, monkeypatch):
    import app.web.settings_api as settings_api

    (tmp_path / "index.html").write_text("<!doctype html><title>晴晴的設定</title>", encoding="utf-8")
    monkeypatch.setattr(settings_api, "FRONTEND_DIST", tmp_path)
    client, token = api
    resp = client.get("/settings", params={"t": token})
    assert resp.status_code == 200
    assert "晴晴的設定" in resp.text
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


def test_設定頁的預覽圖片換成完整網址(api, tmp_path, monkeypatch):
    """Instagram 私訊的連結預覽：沒有圖片時卡片上只會出現一個重新整理的圓圈。"""
    import app.web.settings_api as settings_api

    (tmp_path / "index.html").write_text(
        '<meta property="og:image" content="__PUBLIC_BASE_URL__/assets/og-sunny.jpg" />',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_api, "FRONTEND_DIST", tmp_path)
    client, token = api
    resp = client.get("/settings", params={"t": token})
    assert 'content="https://example.ngrok-free.dev/assets/og-sunny.jpg"' in resp.text
    assert "__PUBLIC_BASE_URL__" not in resp.text


def test_尚未打包時設定頁回503(api, tmp_path, monkeypatch):
    import app.web.settings_api as settings_api

    monkeypatch.setattr(settings_api, "FRONTEND_DIST", tmp_path / "missing")
    client, _ = api
    assert client.get("/settings").status_code == 503
