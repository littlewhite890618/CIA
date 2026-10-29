"""教練回饋連結的簽章（daily-coach-report-link 任務 4.1）。

與設定連結共用同一把密鑰與格式，但用途前綴不同：兩種 token 互相不能冒用。
單一瀏覽器限制（任務 4.2）走真的 HTTP 請求，見 tests/test_report_api.py。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.settings_link import (
    REPORT_PURPOSE,
    SettingsLinkExpired,
    SettingsLinkInvalid,
    build_report_url,
    issue_token,
    verify_token,
)
from app.config import Settings

SECRET = "s" * 40
NOW = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)
WEEK = 7 * 24 * 3600


def test_回饋連結可驗證並帶回每日回饋的id():
    token = issue_token(42, secret=SECRET, ttl_seconds=WEEK, now=NOW, purpose=REPORT_PURPOSE)
    assert verify_token(token, secret=SECRET, now=NOW + timedelta(days=6), purpose=REPORT_PURPOSE) == 42


def test_回饋連結7天後過期():
    token = issue_token(42, secret=SECRET, ttl_seconds=WEEK, now=NOW, purpose=REPORT_PURPOSE)
    with pytest.raises(SettingsLinkExpired):
        verify_token(token, secret=SECRET, now=NOW + timedelta(days=7), purpose=REPORT_PURPOSE)


def test_兩種連結互相不能冒用():
    """設定連結綁 link_id、回饋連結綁 digest id，數字可能相同；前綴不同才分得開。"""
    settings_token = issue_token(42, secret=SECRET, ttl_seconds=WEEK, now=NOW)
    report_token = issue_token(42, secret=SECRET, ttl_seconds=WEEK, now=NOW, purpose=REPORT_PURPOSE)
    assert settings_token != report_token
    with pytest.raises(SettingsLinkInvalid):
        verify_token(settings_token, secret=SECRET, now=NOW, purpose=REPORT_PURPOSE)
    with pytest.raises(SettingsLinkInvalid):
        verify_token(report_token, secret=SECRET, now=NOW)


def test_竄改回饋連結指向別人():
    token = issue_token(42, secret=SECRET, ttl_seconds=WEEK, now=NOW, purpose=REPORT_PURPOSE)
    other = issue_token(43, secret=SECRET, ttl_seconds=WEEK, now=NOW, purpose=REPORT_PURPOSE)
    forged = other.split(".")[0] + "." + token.split(".")[1]
    with pytest.raises(SettingsLinkInvalid):
        verify_token(forged, secret=SECRET, now=NOW, purpose=REPORT_PURPOSE)


def test_回饋網址():
    assert build_report_url("https://x.dev/", "abc") == "https://x.dev/report?t=abc"


@pytest.mark.parametrize("value", ["4:00", "04:00"])
def test_送出時刻接受HHMM(value):
    assert Settings(coach_digest_time=value).coach_digest_time == "04:00"


@pytest.mark.parametrize("field,value", [("coach_digest_time", "25:00"), ("coach_digest_time", "四點"),
                                         ("coach_digest_timezone", "Mars/Base")])
def test_送出時刻或時區寫錯時啟動就報錯(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})
