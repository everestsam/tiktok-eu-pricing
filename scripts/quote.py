"""TikTok 欧盟定价速算 · 客户端（薄壳，不含任何费率与算法）

用法：
  python quote.py --country 德国 --cost 40 --weight 600 --category "Home Supplies" --margin 0.25
  python quote.py --country 法国 --cost 40 --weight 600 --category "Kitchenware" --price 33.25
  python quote.py --country 德国 --cost 40 --weight 300 --dims 40x30x30
  python quote.py --submit-sample sample.json
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("EU_PRICING_API", "https://api.geovantage.net")
# 本机凭据文件：只存「本机专属密钥」，不存任何全局密钥。
# 包里不携带任何密钥 —— 首次运行向服务端登记本机，换回一把仅属于本机的密钥。
_CONF = os.environ.get("EU_PRICING_CONF") or os.path.join(
    os.path.expanduser("~"), ".tiktok-eu-pricing.json")


def _canon(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_conf() -> dict:
    try:
        with open(_CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_conf(d: dict) -> None:
    try:
        with open(_CONF, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def _install() -> tuple:
    """返回 (install_id, secret)。没登记过就去服务端登记，结果缓存在本机。"""
    c = _load_conf()
    if c.get("install_id") and c.get("secret"):
        return c["install_id"], c["secret"]
    iid = c.get("install_id") or ("eu-" + secrets.token_hex(12))
    req = urllib.request.Request(
        API + "/eu/handshake",
        data=json.dumps({"install_id": iid}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    c = {"install_id": d["install_id"], "secret": d["secret"]}
    _save_conf(c)
    return c["install_id"], c["secret"]


def _sign(body: dict) -> dict:
    """用本机密钥签名。登记失败时退回无签名（服务端会给取整价，不报错）。"""
    try:
        iid, sec = _install()
    except Exception:
        return {"Content-Type": "application/json"}
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    sig = hmac.new(sec.encode(),
                   f"{_canon(body)}|{ts}|{nonce}".encode(),
                   hashlib.sha256).hexdigest()[:16]
    return {"Content-Type": "application/json", "X-EU-TS": ts,
            "X-EU-NONCE": nonce, "X-EU-SIGN": sig, "X-EU-INSTALL": iid}


def quote(body: dict, _retry: int = 1) -> dict:
    """带一次自动重试：网络抖动/临时 5xx 时重试，429 不重试（额度已扣）"""
    for attempt in range(_retry + 1):
        try:
            req = urllib.request.Request(API + "/quote", data=_canon(body).encode(),
                                         headers=_sign(body), method="POST")
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or attempt >= _retry:
                raise
        except Exception:
            if attempt >= _retry:
                raise
            time.sleep(1.5)
    raise RuntimeError("unreachable")


def submit_sample(path: str) -> dict:
    data = json.loads(open(path, encoding="utf-8").read())
    _retry = 1
    for attempt in range(_retry + 1):
        try:
            req = urllib.request.Request(API + "/eu/sample",
                                         data=json.dumps(data, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or attempt >= _retry:
                raise
        except Exception:
            if attempt >= _retry:
                raise
            time.sleep(1.5)
    raise RuntimeError("unreachable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", help="目的国，如 德国/法国/意大利/DE/FR")
    ap.add_argument("--cost", type=float, help="成本价 CNY")
    ap.add_argument("--weight", type=int, help="重量 g")
    ap.add_argument("--category", default="Home Supplies", help="类目")
    ap.add_argument("--margin", type=float, default=0.25, help="目标利润率（占售价），默认 0.25")
    ap.add_argument("--price", type=float, help="给定售价（验算模式）")
    ap.add_argument("--shipping", default="1", help="1=包邮（默认），0=买家付运费")
    ap.add_argument("--promo", type=float, help="Smart Promotion 费率，默认 2%%")
    ap.add_argument("--new-seller", action="store_true", help="新卖家优惠费率")
    ap.add_argument("--cargo", default="普货", help="货物类型：普货/特货/敏货")
    ap.add_argument("--dims", help="包装尺寸 长x宽x高（cm），如 40x30x30；按体积重计（cm³÷8000）")
    ap.add_argument("--qty", type=int, default=1, help="件数（配合 --unit-net 自动折算包裹重量）")
    ap.add_argument("--unit-net", type=float, help="单件净重(g)；给了就按 件数×净重+包装 折算")
    ap.add_argument("--pack", type=float, help="包装重量(g)，固定值")
    ap.add_argument("--pack-rule", help="分段包装规则，如 '1-2:10,3+:20'")
    ap.add_argument("--origin", default="DE", help="保留参数（直邮价卡不分发货国）")
    ap.add_argument("--submit-sample", metavar="JSON", help="提交客户已同意的结算单费用明细（需先征得客户同意）")
    a = ap.parse_args()

    try:
        if a.submit_sample:
            r = submit_sample(a.submit_sample)
            print(f"已提交，样本编号 {r.get('id')}（已用于核对费率口径）")
            for k, v in (r.get("derived") or {}).items():
                print(f"  {k}: {v*100:.2f}%")
            return

        if not (a.country and a.cost and a.weight):
            sys.exit("缺参数：--country --cost --weight（用 --help 看用法）")
        if a.cost <= 0 or a.weight <= 0:
            sys.exit("成本和重量必须大于 0（单位：元 / 克）")
        if a.qty < 1:
            sys.exit("件数必须 ≥ 1")
        if a.dims:
            parts = a.dims.replace("x", "*").replace("X", "*").replace("×", "*").split("*")
            if len(parts) != 3 or not all(p.replace(".", "", 1).isdigit() for p in parts):
                sys.exit(f"尺寸格式不对：「{a.dims}」。要三个数，用 x 连接，例如 40x30x30（单位 cm）")
        if a.pack_rule and not re.match(r"^\s*\d+(\s*-\s*\d+|\+)?\s*:\s*\d+(\.\d+)?\s*(,\s*\d+(\s*-\s*\d+|\+)?\s*:\s*\d+(\.\d+)?\s*)*$", a.pack_rule):
            sys.exit(f"包装规则格式不对：「{a.pack_rule}」。写法如 1-2:10,3-5:20,6+:30（件数范围:包装克数）")

        body = {"country": a.country, "cost": float(a.cost), "weight": int(a.weight),
                "category": a.category, "margin": float(a.margin),
                "shipping": a.shipping, "origin": a.origin, "cargo": a.cargo}
        if a.promo is not None:
            body["promo"] = float(a.promo)
        if a.price:
            body["price"] = float(a.price)
        if a.new_seller:
            body["new_seller"] = "1"
        if a.dims:
            body["dims"] = a.dims
        if a.qty and a.qty > 1:
            body["qty"] = int(a.qty)
        if a.unit_net:
            body["qty"] = int(a.qty or 1)
            body["unit_net"] = float(a.unit_net)
        if a.pack is not None:
            body["pack"] = float(a.pack)
        if a.pack_rule:
            body["pack_rule"] = a.pack_rule
        r = quote(body)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": str(e)}
        if e.code == 429:
            print(f"{err.get('error')}")
            print("明天 0 点重置。要不限次、多国对比、批量算 → 用完整版。")
        else:
            print(f"算价失败：{err.get('error')}")
        sys.exit(1)
    except Exception as e:
        sys.exit(f"连不上算价服务：{e}")

    q = r.get("quota") or {}
    print(f"【{r['country']} · {r['category']}】应定价 €{r['price']:.2f}（约 {r['price_cny']:.0f} 元）")
    for b in r.get("breakdown", []):
        print(f"  {b['name']:<12}{b['amount']:>8.2f}  {b['pct']:>5.1f}%")
    print(f"  {'你的利润':<12}{r['profit_cny']:>8.1f} 元（{r['margin_actual']*100:.1f}%）")
    notes = []
    if r.get("commission_note"):
        notes.append(f"佣金 {r['commission_rate']*100:.0f}%（{r['commission_note']}）")
    notes.append(f"VAT {r['vat_rate']*100:.0f}%")
    if r.get("freight_note"):
        notes.append(r["freight_note"])
    if r.get("weight_used"):
        notes.insert(0, f"计费重量 {r['weight_used']}g" + ("（未计包装）" if r.get("pack_missing") else ""))
    print("  " + " | ".join(notes))
    if q:
        print(f"  今日额度：{q.get('used')}/{q.get('limit')}（剩余 {q.get('remaining')} 次）")


if __name__ == "__main__":
    main()
