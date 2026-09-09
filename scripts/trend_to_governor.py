import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NATION = os.environ["NS_NATION"].strip()
AUTLOGIN = os.environ["NS_AUTLOGIN"].strip()
NS_API = "https://www.nationstates.net/cgi-bin/api.cgi"
UA = "Noetivara-AI-Governor/2.1 (by:Noetivara; contact:https://github.com/NSntnt/noetivara-ai-governor; usedBy:Noetivara)"
V = "13"
TREND_TITLE = "Noetivara National Trend Monitor"
CHANGE_TITLE = "Noetivara Government Change Log"
START = "NOETIVARA_TREND_CONTEXT_BEGIN"
END = "NOETIVARA_TREND_CONTEXT_END"


def clean(v):
    return re.sub(r"\s+", " ", v or "").strip()


def lname(tag):
    return tag.rsplit("}", 1)[-1].upper()


def get_api(params, headers=None):
    q = dict(params)
    q["v"] = V
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(f"{NS_API}?{urllib.parse.urlencode(q)}", headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8"), r.headers


def post_api(params, pin):
    q = dict(params)
    q["v"] = V
    req = urllib.request.Request(
        NS_API,
        data=urllib.parse.urlencode(q).encode("utf-8"),
        headers={"User-Agent": UA, "X-Pin": pin, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8"), r.headers


def authenticate():
    _, auth = get_api({"nation": NATION, "q": "ping"}, {"X-Autologin": AUTLOGIN})
    pin = auth.get("X-Pin")
    if not pin:
        raise RuntimeError("X-Pinを取得できませんでした")
    return pin


def find_dispatch_id(xml_text, title):
    root = ET.fromstring(xml_text)
    for e in root.iter():
        if lname(e.tag) != "DISPATCH":
            continue
        t = ""
        did = None
        for child in e.iter():
            tag = lname(child.tag)
            if tag == "TITLE":
                t = clean("".join(child.itertext()))
            elif tag == "DISPATCHID" and clean(child.text):
                did = clean(child.text)
        if not did:
            for attr in ("id", "dispatchid", "ID", "DISPATCHID"):
                if e.attrib.get(attr):
                    did = e.attrib[attr]
                    break
        if t == title and did:
            return str(did)
    return None


def read_dispatch_text(dispatch_id, pin):
    queries = [
        {"q": f"dispatch;dispatchid={dispatch_id}"},
        {"q": "dispatch", "dispatchid": str(dispatch_id)},
    ]
    for params in queries:
        try:
            xml_text, _ = get_api(params, {"X-Pin": pin})
            root = ET.fromstring(xml_text)
            texts = []
            for e in root.iter():
                if lname(e.tag) == "TEXT":
                    text = "".join(e.itertext()).strip()
                    if text:
                        texts.append(text)
            if texts:
                return max(texts, key=len)
        except Exception:
            pass
    return ""


def write_dispatch(command, pin):
    try:
        prepared, _ = post_api({**command, "mode": "prepare"}, pin)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        print("X-Pinが無効化された可能性があるため、再認証します。")
        fresh_pin = authenticate()
        prepared, _ = post_api({**command, "mode": "prepare"}, fresh_pin)
        pin = fresh_pin
    root = ET.fromstring(prepared)
    token = None
    for e in root.iter():
        if lname(e.tag) in {"TOKEN", "SUCCESS"} and clean(e.text):
            token = clean(e.text)
            break
    if not token:
        raise RuntimeError("Dispatch prepare returned no token")
    try:
        result, _ = post_api({**command, "mode": "execute", "token": token}, pin)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        print("実行時にX-Pinが無効化されたため、再認証してPrepareからやり直します。")
        fresh_pin = authenticate()
        prepared, _ = post_api({**command, "mode": "prepare"}, fresh_pin)
        root = ET.fromstring(prepared)
        token = None
        for e in root.iter():
            if lname(e.tag) in {"TOKEN", "SUCCESS"} and clean(e.text):
                token = clean(e.text)
                break
        if not token:
            raise RuntimeError("再認証後のDispatch prepare returned no token")
        result, _ = post_api({**command, "mode": "execute", "token": token}, fresh_pin)
    if re.search(r"<ERROR\b", result, re.I):
        raise RuntimeError("NationStates rejected Dispatch operation")


print("========================================")
print("Noetivara Trend-to-Governor Bridge")
print("========================================")
print("NationStatesへ接続しています...")

try:
    pin = authenticate()
except Exception as exc:
    print(f"NationStates接続エラー: {exc}")
    sys.exit(1)

try:
    dispatchlist, _ = get_api(
        {"q": "dispatchlist", "dispatchauthor": NATION, "dispatchsort": "new"},
        {"X-Pin": pin},
    )
    trend_id = find_dispatch_id(dispatchlist, TREND_TITLE)
    change_id = find_dispatch_id(dispatchlist, CHANGE_TITLE)
except Exception as exc:
    print(f"Dispatch一覧取得エラー: {exc}")
    sys.exit(1)

if not trend_id:
    print("National Trend MonitorのDispatchが見つかりません。安全のため更新しません。")
    sys.exit(0)
if not change_id:
    print("Government Change LogのDispatchが見つかりません。安全のため更新しません。")
    sys.exit(0)

trend_text = read_dispatch_text(trend_id, pin)
change_text = read_dispatch_text(change_id, pin)
if not trend_text or not change_text:
    print("TrendまたはChange Log本文を取得できません。安全のため更新しません。")
    sys.exit(0)

context = trend_text[:9000].strip()
block = f"\n\n{START}\n{context}\n{END}"

cleaned = re.sub(rf"\n*{re.escape(START)}.*?{re.escape(END)}", "", change_text, flags=re.S)
new_text = cleaned.rstrip() + block

if len(new_text) > 12000:
    print("Trend統合後のChange Logが長すぎます。安全のため更新しません。")
    sys.exit(1)

if new_text == change_text:
    print("Trend Contextに変更はありません。NationStatesへの書き込みは行いません。")
    sys.exit(0)

command = {
    "nation": NATION,
    "c": "dispatch",
    "dispatch": "edit",
    "dispatchid": change_id,
    "title": CHANGE_TITLE,
    "text": new_text,
    "category": "1",
    "subcategory": "105",
}

try:
    write_dispatch(command, pin)
except Exception as exc:
    print(f"Trend Context統合に失敗しました: {exc}")
    sys.exit(1)

print("National Trend Monitorの最新分析をGovernment Change Logへ接続しました。")
