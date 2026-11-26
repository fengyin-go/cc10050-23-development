"""历史页面文件 → 导入回执 → 同批模板试跑 的回归准备领域服务。

业务链路（与题目约定一一对应）：

1. 审核员选择“带站点和批次的历史页面文件”或直接上传回归材料；
2. ``apply_import`` 逐行校验，产出“导入回执”：已验证 / 跨站拒绝（行内原因）/
   损坏（半份 HTML，单独说明）/ 无法解析的损坏行，以及每行的后续动作；
3. 只有属于指定站点且完整的行才会变成 sample_store 中的合格样本；
   跨站同指纹绝不覆盖原样本；半份 HTML 不会成为已验证样本；
4. 同一“原导入请求号”重试上传同一页时，按 (导入请求, 页面编号) 对账，
   同一页始终只有一份合格来源（价格 70 → 79 走更新而不是新增）；
5. ``create_trial`` / ``execute_trial`` 从同一批领取合格样本，用页面解析模板
   （或通用价格提取策略）真实提取价格字段，生成带下载凭据的试跑回执；
6. 导入回执、样本、试跑批次三者通过 import_no / sample_id / trial_no 互相关联。

当前实现使用进程内内存结构，和 ``app.store`` 的示例数据方式保持一致。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import quote

from app.store import store

# ---------------------------------------------------------------------------
# 演练场景常量：I1（70 → 重传 79）、I2（一半传输失败）、跨站同指纹行
# ---------------------------------------------------------------------------

SCENARIO_SITE = "云街甄选"
SCENARIO_DOMAIN = "yunjie.example.com"
WRONG_SITE = "南岸杂货"
SCENARIO_BATCH = "RG-20261126-I1I2"
FP_I1 = "fp-i1-same-0001"
FP_I2 = "fp-i2-half-0002"

_ALIAS_GROUPS: dict[str, list[str]] = {
    "page_code": ["页面编号", "页码", "页面id", "编号", "记录编号", "试样编号", "page", "pagecode", "pageid", "code", "sku", "item", "key", "id"],
    "site": ["站点名称", "申报站点", "所属站点", "适用站点", "目标站点", "站点", "sitename", "site_name", "site", "domain", "站点域名", "host", "url", "链接"],
    "batch": ["样本批次", "回归批次", "批次号", "批次", "batchno", "batch_no", "batch"],
    "fingerprint": ["样本指纹", "指纹", "fingerprint", "fp", "hash", "sha1", "digest"],
    "price": ["当前价格", "提取价格", "价格", "pricevalue", "price_value", "amount", "price", "cost"],
    "title": ["商品名称", "页面标题", "标题", "title", "name", "itemname", "goodsname"],
    "html": ["原始html摘要", "原始html全文", "原始html", "html内容", "页面内容", "html源码", "rawhtml", "html_content", "html", "content", "内容", "源码", "source"],
    "transfer": ["传输状态", "上传状态", "传输情况", "传输", "transferstate", "transfer_state", "delivery", "transfer", "upload"],
    "declared_status": ["校验状态", "验证状态", "导入状态", "状态", "status", "result", "结论"],
    "remark": ["行内原因", "拒绝原因", "失败原因", "备注说明", "备注", "说明", "原因", "remark", "note", "reason", "message", "error", "detail"],
}


def _norm_key(value: Any) -> str:
    return re.sub(r"[\s_\-\[\]（）()：:./\\]+", "", str(value or "").strip().lower())


_ALIAS_LOOKUP: dict[str, str] = {}
for _field, _names in _ALIAS_GROUPS.items():
    for _name in _names:
        _ALIAS_LOOKUP.setdefault(_norm_key(_name), _field)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_seq(table: list[dict[str, Any]], key: str = "id") -> int:
    return max((int(row.get(key, 0) or 0) for row in table), default=0) + 1


def fingerprint_of(html: str, page_code: str = "") -> str:
    basis = html or page_code or secrets.token_hex(8)
    return "fp-" + hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ---------------------------------------------------------------------------
#  forgiving parser：JSON / JSONL / CSV / TSV / key=value 文本 / HTML 块
# ---------------------------------------------------------------------------


def _pick(record: dict[str, Any], field: str) -> str:
    for key, value in record.items():
        if _ALIAS_LOOKUP.get(_norm_key(key)) == field:
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value).strip()
    return ""


def _as_record(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        return obj
    return None


def _collect_json_records(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(node, dict):
            # 常见包裹键
            for wrap in ("lines", "rows", "records", "items", "samples", "data", "结果", "明细"):
                if wrap in node and isinstance(node[wrap], (list, dict)):
                    walk(node[wrap])
                    return
            # { "I1": {...}, "I2": {...} } 形式
            nested = [v for v in node.values() if isinstance(v, dict)]
            if nested and not _pick(node, "html") and not _pick(node, "page_code"):
                for key, value in node.items():
                    if isinstance(value, dict):
                        value.setdefault("page_code", key)
                        found.append(value)
                return
            found.append(node)

    walk(data)
    return found


def _parse_delimited(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], errors
    header = [h.strip() for h in rows[0]]
    norm_header = [_ALIAS_LOOKUP.get(_norm_key(h), h) for h in header]
    html_idx = next((i for i, h in enumerate(norm_header) if h == "html"), None)
    records: list[dict[str, Any]] = []
    for line_no, raw_cells in enumerate(rows[1:], start=2):
        if not any(str(c).strip() for c in raw_cells):
            continue
        cells = list(raw_cells)
        try:
            if len(cells) > len(header) and html_idx is not None and html_idx < len(header):
                merged = cells[html_idx:]
                cells = cells[:html_idx] + [delimiter.join(merged)]
            if len(cells) < len(header):
                cells = cells + [""] * (len(header) - len(cells))
            record = {header[i]: cells[i] for i in range(len(header))}
            record["_line_no"] = line_no
            records.append(record)
        except csv.Error as exc:  # pragma: no cover - 防御性
            errors.append({"line_no": line_no, "reason": f"分隔文件第 {line_no} 行无法解析：{exc}"})
    return records, errors


_KNOWN_KEY_PATTERN = "|".join(sorted({
    re.escape(name)
    for names in _ALIAS_GROUPS.values()
    for name in names
    if re.search(r"[A-Za-z]", name) or len(name) >= 2
}, key=len, reverse=True))


def _parse_key_value_lines(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    # 键名前不能紧贴字母/数字/下划线/连字符，避免把 HTML 属性 data-price= 当成 price 字段
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])(?P<key>" + _KNOWN_KEY_PATTERN + r")\s*[=:：]\s*",
        re.IGNORECASE,
    )
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        matches = list(pattern.finditer(line))
        if not matches:
            # 形如 I1|站点|fp|70|<html...> 的竖线记录
            if re.search(r"\bI\d+\b", stripped) and ("|" in stripped or "｜" in stripped):
                parts = [p.strip() for p in re.split(r"[|｜]", stripped)]
                record: dict[str, Any] = {"page_code": parts[0]}
                if len(parts) > 1:
                    record["site"] = parts[1]
                if len(parts) > 2:
                    record["fingerprint"] = parts[2]
                if len(parts) > 3:
                    record["price"] = parts[3]
                if len(parts) > 4:
                    record["html"] = "|".join(parts[4:])
                record["_line_no"] = idx
                records.append(record)
            elif re.search(r"损坏|CORRUPT|BADLINE", stripped, re.IGNORECASE):
                errors.append({"line_no": idx, "reason": f"第 {idx} 行损坏，无法识别为页面记录：{stripped[:80]}"})
            continue
        record = {"_line_no": idx}
        for j, match in enumerate(matches):
            field = _ALIAS_LOOKUP.get(_norm_key(match.group("key")))
            if field == "html":
                # HTML 键之后的全部内容都归 HTML，避免被内部的 price= 等键截断
                record["html"] = line[match.end():].strip().rstrip(",;；")
                break
            if not field:
                continue
            end = matches[j + 1].start() if j + 1 < len(matches) else len(line)
            record[field] = line[match.end():end].strip().rstrip(",;；")
        records.append(record)
    return records, errors


def _parse_html_marker_blocks(text: str) -> list[dict[str, Any]]:
    marker = re.compile(
        r"(?:<!--\s*PAGE[:：]?\s*)?"
        r"(?:"
        r"data-page|data-code|id"
        r")\s*=\s*[\"']?(?P<code>I\d+)[\"']?"
        r"[^>]*?(?:data-site\s*=\s*[\"'](?P<site>[^\"']*)[\"'])?"
        r"[^>]*?(?:data-fp\s*=\s*[\"'](?P<fp>[^\"']*)[\"'])?"
        r"[^>]*?(?:data-price\s*=\s*[\"']?(?P<price>[0-9.]+)[\"']?)?",
        re.IGNORECASE,
    )
    matches = list(marker.finditer(text))
    if not matches:
        return []
    records: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[match.start():end]
        records.append({
            "page_code": match.group("code"),
            "site": match.groupdict().get("site") or "",
            "fingerprint": match.groupdict().get("fp") or "",
            "price": match.groupdict().get("price") or "",
            "html": block,
            "_line_no": i + 1,
        })
    return records


def _parse_html_table(text: str) -> list[dict[str, Any]]:
    """把 <table><tr>... 形态的历史页面清单还原成记录，单元格保留内部 HTML。"""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.I | re.S)
    if len(rows) < 2:
        return []

    def cells(row_html: str) -> list[str]:
        return re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)

    header_cells = cells(rows[0])
    if not header_cells:
        return []
    headers = [_strip_tags(h).strip() for h in header_cells]
    if not any(headers):
        return []
    records: list[dict[str, Any]] = []
    for idx, row in enumerate(rows[1:], start=2):
        raw_cells = cells(row)
        if not raw_cells or not any(_strip_tags(c).strip() for c in raw_cells):
            continue
        record: dict[str, Any] = {"_line_no": idx}
        for i, header in enumerate(headers):
            if not header:
                continue
            record[header] = raw_cells[i] if i < len(raw_cells) else ""
        records.append(record)
    return records


def parse_history_content(content: str, file_name: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """把上传内容解析成原始 dict 记录列表，返回 (records, parse_errors, detected_format)。"""
    if content is None:
        return [], [{"line_no": 0, "reason": "文件内容为空"}], "empty"
    text = str(content).replace("﻿", "")
    stripped = text.strip()
    if not stripped:
        return [], [{"line_no": 0, "reason": "文件内容为空"}], "empty"

    # 1) 直接是记录数组 / JSON 包裹
    try:
        data = json.loads(stripped)
        raw = _collect_json_records(data)
        if raw:
            return raw, [], "json"
    except (json.JSONDecodeError, ValueError):
        pass

    # 2) JSONL：逐行受理，单行损坏只记为损坏行，不拖垮其他行
    jsonl_records: list[dict[str, Any]] = []
    jsonl_bad: list[dict[str, Any]] = []
    jsonl_lines = [ln.strip().rstrip(",") for ln in stripped.splitlines() if ln.strip()]
    if len(jsonl_lines) > 1:
        for idx, line in enumerate(jsonl_lines, start=1):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    obj.setdefault("_line_no", idx)
                    jsonl_records.append(obj)
                else:
                    jsonl_bad.append({"line_no": idx, "reason": f"第 {idx} 行不是 JSON 对象"})
            except ValueError as exc:
                jsonl_bad.append({"line_no": idx, "reason": f"第 {idx} 行 JSON 损坏（半行传输？）：{str(exc)[:80]}"})
        if jsonl_records:
            return jsonl_records, jsonl_bad, "jsonl"

    # 3) 带表头的 CSV / TSV
    first_line = stripped.splitlines()[0]
    if ("页面" in first_line or "page" in first_line.lower() or "站点" in first_line
            or "site" in first_line.lower() or "fingerprint" in first_line.lower()
            or "指纹" in first_line) and ("," in first_line or "\t" in first_line):
        records, errors = _parse_delimited(stripped)
        if records:
            return records, errors, "csv"

    # 4) HTML 标记块
    if re.search(r"data-page\s*=|data-code\s*=|<!--\s*PAGE", stripped, re.IGNORECASE):
        records = _parse_html_marker_blocks(stripped)
        if records:
            return records, [], "html-blocks"

    # 4.5) HTML <table> 清单
    if len(re.findall(r"<tr[^>]*>", stripped, flags=re.I)) >= 2:
        records = _parse_html_table(stripped)
        if records:
            return records, [], "html-table"

    # 5) 逐行 key=value / 竖线记录
    records, errors = _parse_key_value_lines(stripped)
    if records:
        return records, errors, "lines"

    # 6) 整个文件就是一份 HTML（单页）
    if "<html" in stripped.lower() or "<div" in stripped.lower():
        return [{"page_code": "", "html": stripped, "_line_no": 1}], [], "html-single"

    errors.append({"line_no": 0, "reason": "未能从文件中识别出任何页面记录（支持 JSON/CSV/TSV/键值行/HTML 块）"})
    return [], errors, "unknown"


# ---------------------------------------------------------------------------
# 完整性判定 + 真实价格提取
# ---------------------------------------------------------------------------

_INCOMPLETE_WORDS = [
    "传输失败", "传输中断", "传输不完整", "一半传输", "半份", "未传完", "补传",
    "截断", "损坏", "不完整", "缺半", "发送失败", "上传失败", "接收失败",
    "incomplete", "truncated", "partial html", "partial", "failed", "failure",
    "transfer_failed", "transfer failed", "half received", "half-transfer", "half",
    "corrupt", "error", "interrupted",
]


def assess_completeness(record: dict[str, str]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    state_blob = " ".join([
        record.get("transfer", ""), record.get("declared_status", ""), record.get("remark", "")
    ]).lower()
    hit_words = [w for w in _INCOMPLETE_WORDS if w.lower() in state_blob]
    if hit_words:
        reasons.append(f"传输/校验字段标记异常（命中：{ '、'.join(hit_words[:3]) }）")

    html = record.get("html", "") or ""
    low = html.lower()
    if html:
        if re.search(r"<!--\s*(incomplete|truncated|half[^>]*|传输中断|补传)[^>]*-->", low):
            reasons.append("HTML 中带有未完成/截断注释标记")
        if "<html" in low and "</html>" not in low:
            reasons.append("HTML 文档缺少 </html> 闭合标签")
        if "<body" in low and "</body>" not in low:
            reasons.append("HTML 文档缺少 </body> 闭合标签")
        if re.search(r"<[a-zA-Z][^>]*$", html.rstrip()):
            reasons.append("内容结尾停在未写完的标签上（典型的半份传输）")
        elif html.rstrip().endswith("<") or re.search(r"</?[a-zA-Z0-9]*$", html.rstrip()):
            reasons.append("内容在标签中途截断")
        if re.search(r"=\s*[\"'][^\"'>]*$", html.rstrip()):
            reasons.append("标签属性值未闭合，传输只收到一半")
        structural = ["html", "body", "div", "table", "tbody", "tr", "td", "ul", "ol", "section", "article"]
        opens = sum(len(re.findall(rf"<{tag}(?:\s|>)", low)) for tag in structural)
        closes = sum(len(re.findall(rf"</{tag}>", low)) for tag in structural)
        if opens >= 4 and opens - closes >= 3 and not html.rstrip().endswith(">"):
            reasons.append(f"结构性标签大量未闭合（开 {opens} / 闭 {closes}）")
        if re.search(r"[¥￥$]\s*$", html.strip()):
            reasons.append("价格符号后没有金额，价格节点只传输了一半")
    else:
        if not record.get("price"):
            reasons.append("既没有完整 HTML 也没有价格字段，回归模块不能受理")

    # 去重保序
    seen: set[str] = set()
    uniq = [r for r in reasons if not (r in seen or seen.add(r))]
    return (len(uniq) == 0), uniq


def _to_number(raw: str) -> float | None:
    if raw is None:
        return None
    text = str(raw)
    match = re.search(r"([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]{1,2})?)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "").replace("，", ""))
    except ValueError:
        return None


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def extract_fields(html: str, template: dict[str, Any] | None = None) -> dict[str, Any]:
    """从 HTML 中真实提取价格/标题/币种；模板选择器优先，通用策略兜底。"""
    result: dict[str, Any] = {
        "title": "", "price": None, "price_text": "", "currency": "CNY",
        "extractor": "none", "selector_used": "", "candidates": [],
    }
    if not html:
        return result

    selector_hint = str((template or {}).get("选择器路径", ""))
    price_class_hint = ""
    m = re.search(r"\.([A-Za-z0-9_-]*(?:price|jiage)[A-Za-z0-9_-]*)", selector_hint, re.I)
    if m:
        price_class_hint = m.group(1)

    candidates: list[tuple[float, str, str]] = []  # (priority, value, strategy)

    def add(priority: float, value: str | None, strategy: str, selector: str = "") -> None:
        num = _to_number(value or "")
        if num is not None and num > 0:
            candidates.append((priority, num, strategy))
            if selector and not result["selector_used"]:
                result["selector_used"] = selector

    # 1) meta / itemprop 标准价格节点
    for pattern in [
        r"<meta[^>]+(?:itemprop|property)=[\"'](?:price|product:price:amount|og:price:amount|business:price)[\"'][^>]*content=[\"']([0-9.,]+)[\"']",
        r"<meta[^>]+content=[\"']([0-9.,]+)[\"'][^>]+(?:itemprop|property)=[\"'](?:price|product:price:amount|og:price:amount)[\"']",
        r"itemprop=[\"']price[\"'][^>]*content=[\"']([0-9.,]+)[\"']",
        r"itemprop=[\"']price[\"'][^>]*>\s*([^<0-9]{0,6}[0-9][0-9.,]*)",
    ]:
        for hit in re.findall(pattern, html, flags=re.I | re.S):
            add(10, hit, "meta/itemprop[price]", "[itemprop=price]")

    # 2) JSON-LD
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, flags=re.I | re.S):
        for hit in re.findall(r'"(?:price|lowPrice|currentPrice)"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', block):
            add(20, hit, "json-ld:price", "script[type=application/ld+json]")

    # 3) data-price / content 在价格 class 节点上
    price_tag = re.compile(
        r"<(?P<tag>[a-zA-Z0-9]+)(?P<attrs>[^>]*?(?:class|id)=[\"'][^\"']*(?:price|jiage|product-price|sale-price|current-price)[^\"']*[\"'][^>]*?)>",
        re.I,
    )
    for match in price_tag.finditer(html):
        attrs = match.group("attrs")
        dp = re.search(r"data-price=[\"']([0-9.,]+)[\"']", attrs, re.I)
        ct = re.search(r"content=[\"']([0-9.,]+)[\"']", attrs, re.I)
        if dp:
            add(30 if price_class_hint and price_class_hint in attrs else 40, dp.group(1),
                "price-node[data-price]", ".price[data-price]")
        if ct:
            add(31, ct.group(1), "price-node[content]", "[itemprop=price][content]")
        tail = html[match.end():match.end() + 120]
        text_hit = re.search(r"([¥￥$]?\s*[0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]{1,2})?)", tail)
        if text_hit:
            add(50 if price_class_hint and price_class_hint in attrs else 60,
                text_hit.group(1), "price-node:text", ".price")

    # 4) 模板显式 class 提示再扫一遍
    if price_class_hint:
        for hit in re.findall(
            rf'class=["\'][^"\']*{re.escape(price_class_hint)}[^"\']*["\'][^>]*>\s*([^<]{{0,8}}[0-9][0-9.,]*)',
            html, flags=re.I,
        ):
            add(35, hit, f"template-selector:.{price_class_hint}", f".{price_class_hint}")

    # 5) 货币符号兜底
    for hit in re.findall(r"(?:¥|￥|RMB\s*|CNY\s*)([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]{1,2})?)", html, flags=re.I):
        add(80, hit, "currency:CNY")
    for hit in re.findall(r"(?:US\$|USD\s*|\$)([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]{1,2})?)", html):
        add(81, hit, "currency:USD")

    # 5.5) 中文“价格/售价/现价：79”标签兜底（允许标签与数字之间有标签嵌套）
    for hit in re.findall(
        r"(?:当前价|现价|售价|价格|单价|报价|到手价)[^0-9<]{0,12}(?:</?[a-zA-Z0-9]+[^>]*>){0,3}\s*[¥￥]?\s*"
        r"([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]{1,2})?)",
        html,
    ):
        add(70, hit, "label:价格")

    # 6) "price": 79 裸键
    for hit in re.findall(r"[\"']?price[\"']?\s*[:=]\s*[\"']?([0-9]+(?:\.[0-9]{1,2})?)", html, flags=re.I):
        add(90, hit, "bare:price")

    if candidates:
        candidates.sort(key=lambda c: c[0])
        _, number, strategy = candidates[0]
        result["price"] = number
        result["price_text"] = ("¥" + (f"{number:.2f}")) if result["currency"] == "CNY" else str(number)
        result["extractor"] = strategy
        result["candidates"] = sorted({f"{n:g}({s})" for _, n, s in candidates})[:6]

    # 标题
    title_patterns = [
        r"<meta[^>]+property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)[\"']",
        r"itemprop=[\"']name[\"'][^>]*content=[\"']([^\"']+)[\"']",
        r"<h1[^>]*>(.*?)</h1>",
        r"<title[^>]*>(.*?)</title>",
    ]
    for pattern in title_patterns:
        hit = re.search(pattern, html, flags=re.I | re.S)
        if hit:
            result["title"] = _strip_tags(hit.group(1))[:120]
            if result["title"]:
                break

    if re.search(r"US\$|USD|\$", html):
        result["currency"] = "USD"
        if result["price"] is not None:
            result["price_text"] = f"${result['price']:.2f}"
    elif re.search(r"€|EUR", html):
        result["currency"] = "EUR"
        if result["price"] is not None:
            result["price_text"] = f"€{result['price']:.2f}"
    return result


# ---------------------------------------------------------------------------
# 服务主体
# ---------------------------------------------------------------------------


class RegressionService:
    def __init__(self) -> None:
        self.history_files: dict[str, dict[str, Any]] = {}
        self.imports: dict[str, dict[str, Any]] = {}
        self.trials: dict[str, dict[str, Any]] = {}
        self._import_seq = 0
        self._trial_seq = 0
        self._file_seq = 0
        self.ensure_bootstrap()

    # ---------------- 启动数据：站点 / 模板 / 两份历史文件 ----------------

    def ensure_bootstrap(self) -> None:
        sites = store.rows("shop_site")
        if not any(s.get("站点名称") == SCENARIO_SITE for s in sites):
            sites.append({
                "id": _next_seq(sites), "status": "已验证", "pending": False, "abnormal": False,
                "站点名称": SCENARIO_SITE, "站点域名": SCENARIO_DOMAIN,
                "采集区域": "华东-演练区", "语言标识": "zh-CN", "生效货币": "CNY",
            })
        parsers = store.rows("page_parser")
        if not any(t.get("模板名称") == "商品页价格标准模板" for t in parsers):
            parsers.append({
                "id": _next_seq(parsers), "status": "已启用", "pending": False, "abnormal": False,
                "模板名称": "商品页价格标准模板", "适用站点": SCENARIO_SITE,
                "选择器路径": ".product-price [data-price], [itemprop=price], meta[property=product:price:amount]",
                "字段映射": json.dumps({"价格": "price", "商品名称": "title", "币种": "currency"}, ensure_ascii=False),
                "解析版本": "v3.2",
            })

        i1_html_70 = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>云街甄选 声阔无线耳机 I1 官方店</title>"
            "<meta property=\"product:price:amount\" content=\"70.00\"></head><body>"
            "<h1 class=\"product-name\">声阔降噪无线耳机 I1</h1>"
            "<div class=\"product-price\"><span class=\"price\" data-price=\"70.00\">¥70.00</span></div>"
            "<div class=\"sku\">规格：I1 曜石黑</div></body></html>"
        )
        i1_html_79 = i1_html_70.replace("70.00", "79.00").replace(">¥70.00<", ">¥79.00<")
        i2_half_html = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>云街甄选 便携保温杯 I2</title></head><body>"
            "<h1 class=\"product-name\">316 不锈钢保温杯 I2</h1>"
            "<div class=\"product-price\"><span class=\"price\" data-price=\"8"
        )
        cross_html = (
            "<html><body><h1>南岸杂货 同款耳机（跨站镜像）</h1>"
            "<div class=\"price\" data-price=\"70.00\">¥70.00</div></body></html>"
        )

        def payload(price_i1: str, i2_state: str) -> str:
            i2_transfer = "一半传输失败，等待补传" if i2_state == "half" else "已补传完整"
            i2_html = i2_half_html if i2_state == "half" else i2_half_html + "9.00\">¥89.00</span></div></body></html>"
            rows = [
                {"页面编号": "I1", "站点名称": SCENARIO_SITE, "样本批次": SCENARIO_BATCH,
                 "样本指纹": FP_I1, "价格": price_i1, "传输状态": "完整",
                 "html": i1_html_70 if price_i1 == "70" else i1_html_79},
                {"页面编号": "I2", "站点名称": SCENARIO_SITE, "样本批次": SCENARIO_BATCH,
                 "样本指纹": FP_I2, "价格": "", "传输状态": i2_transfer,
                 "备注": "TCP 连接中断，HTML 只收到一半", "html": i2_html},
                {"页面编号": "I1", "站点名称": WRONG_SITE, "样本批次": SCENARIO_BATCH,
                 "样本指纹": FP_I1, "价格": price_i1, "传输状态": "完整",
                 "备注": "该行站点被误填为外站", "html": cross_html.replace("70.00", f"{price_i1}.00")},
            ]
            return json.dumps({
                "站点名称": SCENARIO_SITE, "样本批次": SCENARIO_BATCH,
                "导入请求号": "IMP-SCENARIO-001", "lines": rows,
            }, ensure_ascii=False, indent=2)

        self._upsert_history_file(
            "HF-SCENARIO-70", "history_I1I2_首传_价格70.json", SCENARIO_SITE, SCENARIO_BATCH,
            "首传历史文件：I1=70 已验证，I2 一半传输失败，另有跨站同指纹行",
            payload("70", "half"), builtin=True,
        )
        self._upsert_history_file(
            "HF-SCENARIO-79", "history_I1_重传更正_价格79.json", SCENARIO_SITE, SCENARIO_BATCH,
            "审核员重试原导入：I1 价格已由 70 更正为 79，I2 仍在补传",
            payload("79", "half"), builtin=True,
        )

    def _upsert_history_file(self, file_id: str, file_name: str, site: str, batch: str,
                             note: str, content: str, builtin: bool = False) -> dict[str, Any]:
        item = {
            "id": file_id, "file_name": file_name, "site": site, "batch": batch,
            "note": note, "content": content, "size": len(content.encode("utf-8")),
            "updated_at": now_iso(), "builtin": builtin,
        }
        self.history_files[file_id] = item
        return item

    def list_history_files(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in f.items() if k != "content"} for f in self.history_files.values()]

    def get_history_file(self, file_id: str) -> dict[str, Any] | None:
        return self.history_files.get(file_id)

    # ---------------- 导入与逐行校验 ----------------

    def _normalize_raw_record(self, raw: dict[str, Any], line_no: int) -> dict[str, Any]:
        def g(field: str) -> str:
            return _pick(raw, field)

        html = unescape(g("html"))
        # 有些文件把 HTML 拆在多个键里，兜底拼接常见内容键
        if not html:
            for key, value in raw.items():
                if isinstance(value, str) and ("<" in value and ">" in value) and len(value) > 20:
                    html = value
                    break
        page_code = g("page_code").upper()
        m = re.search(r"\bI\d+\b", page_code)
        if not m:
            m2 = re.search(r"\bI\d+\b", json.dumps(raw, ensure_ascii=False))
            page_code = m2.group(0) if m2 else (f"L{line_no}")
        elif m.group(0) != page_code:
            page_code = m.group(0)
        fp = g("fingerprint") or fingerprint_of(html, page_code)
        return {
            "line_no": int(raw.get("_line_no") or line_no),
            "page_code": page_code,
            "site": g("site"),
            "batch": g("batch"),
            "fingerprint": fp,
            "declared_price": g("price"),
            "title": g("title"),
            "html": html,
            "transfer": g("transfer"),
            "declared_status": g("declared_status"),
            "remark": g("remark"),
        }

    def _resolve_site_batch(self, payload: dict[str, Any], records: list[dict[str, Any]],
                            hf: dict[str, Any] | None) -> tuple[str, str]:
        site = str(payload.get("site") or payload.get("站点名称") or "").strip()
        batch = str(payload.get("batch") or payload.get("样本批次") or payload.get("批次") or "").strip()
        if hf:
            site = site or hf.get("site", "")
            batch = batch or hf.get("batch", "")
        if not site and records:
            sites = [r["site"] for r in records if r["site"]]
            if sites:
                site = max(set(sites), key=sites.count)
        if not batch and records:
            batches = [r["batch"] for r in records if r["batch"]]
            if batches:
                batch = max(set(batches), key=batches.count)
        if not batch:
            self._import_seq += 1
            batch = f"RG-{datetime.now().strftime('%Y%m%d')}-{self._import_seq:03d}"
        return site, batch

    def apply_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        history_file_id = str(payload.get("history_file_id") or payload.get("file_id") or "").strip()
        hf = self.history_files.get(history_file_id) if history_file_id else None

        file_name = str(payload.get("file_name") or payload.get("filename") or (hf or {}).get("file_name") or "上传的历史页面文件")
        content = payload.get("content")
        if content is None and payload.get("content_base64"):
            import base64
            try:
                content = base64.b64decode(str(payload["content_base64"])).decode("utf-8", errors="ignore")
            except Exception:
                content = ""
        if (not content or not str(content).strip()) and hf:
            content = hf.get("content", "")
        explicit_records = payload.get("records") or payload.get("lines")

        import_no = str(
            payload.get("import_no") or payload.get("import_request_no")
            or payload.get("importRequestNo") or payload.get("request_no")
            or payload.get("request_id") or payload.get("导入请求号") or ""
        ).strip()

        existing = self.imports.get(import_no) if import_no else None
        retry_of = import_no if existing else ""
        if not import_no:
            self._import_seq += 1
            import_no = f"IMP-{datetime.now().strftime('%Y%m%d')}-{self._import_seq:03d}"

        parse_errors: list[dict[str, Any]] = []
        raw_records: list[Any] = []
        detected_format = "structured"
        if isinstance(explicit_records, list) and explicit_records:
            raw_records = explicit_records
        else:
            raw_records, parse_errors, detected_format = parse_history_content(str(content or ""), file_name)

        records = [
            self._normalize_raw_record(r if isinstance(r, dict) else {"value": r}, idx)
            for idx, r in enumerate(raw_records, start=1)
        ]

        site, batch = self._resolve_site_batch(payload, records, hf)

        lines = self._validate_and_upsert(import_no, site, batch, file_name, records, parse_errors, existing)

        valid = [l for l in lines if l["verdict"] == "已验证"]
        cross = [l for l in lines if l["verdict"] == "跨站拒绝"]
        broken = [l for l in lines if l["verdict"] in ("损坏", "无法解析")]
        duplicates = [l for l in lines if l["verdict"] == "重复来源"]

        if existing:
            attempt_no = int(existing.get("attempt_count", 1)) + 1
            attempts = existing.get("attempts", [])
            first_created = existing.get("created_at")
        else:
            attempt_no = 1
            attempts = []
            first_created = now_iso()

        attempts.append({
            "attempt": attempt_no,
            "at": now_iso(),
            "file_name": file_name,
            "format": detected_format,
            "total": len(records) + len(parse_errors),
            "valid": len(valid),
            "cross_site": len(cross),
            "broken": len(broken),
            "duplicate": len(duplicates),
            "price_by_page": {l["page_code"]: l.get("price") for l in valid},
        })

        sample_ids = [l["sample_id"] for l in lines if l.get("sample_id")]
        message = (
            f"导入回执 {import_no}（第 {attempt_no} 次）："
            f"{len(valid)} 行已验证，{len(cross)} 行跨站拒绝，"
            f"{len([b for b in broken if b['verdict'] == '损坏'])} 行内容损坏/半传，"
            f"{len([b for b in broken if b['verdict'] == '无法解析'])} 个损坏行无法解析"
        )

        receipt: dict[str, Any] = {
            "ok": True,
            "import_no": import_no,
            "import_request_no": import_no,
            "receipt_no": import_no,
            "retry_of": retry_of,
            "is_retry": bool(retry_of),
            "site": site,
            "batch": batch,
            "file_name": file_name,
            "history_file_id": history_file_id,
            "format": detected_format,
            "created_at": first_created,
            "updated_at": now_iso(),
            "attempt_count": attempt_no,
            "attempts": attempts,
            "lines": lines,
            "parse_errors": parse_errors,
            "sample_ids": sample_ids,
            "counts": {
                "total": len(lines) + len(parse_errors),
                "valid": len(valid),
                "cross_site": len(cross),
                "broken": len(broken),
                "duplicate": len(duplicates),
                "eligible_sources": len({l["page_code"] for l in valid}),
            },
            "message": message,
        }
        self.imports[import_no] = receipt

        # 同一原导入请求的更正送达后，自动跟进关联试跑，保持同一回归批次有效
        synced = self.sync_trials_for_import(import_no)
        receipt["synced_trials"] = [t["trial_no"] for t in synced]
        return receipt

    def _site_matches(self, declared: str, target: str) -> bool:
        if not target:
            return True
        if not declared:
            return True  # 未申报站点时继承指定站点
        d = declared.strip().lower()
        t = target.strip().lower()
        if d == t:
            return True
        # URL / 域名形态
        if "://" in d or "." in d:
            host = urlparse(d if "://" in d else "http://" + d).hostname or d
            if host.lower() == SCENARIO_DOMAIN and target == SCENARIO_SITE:
                return True
            d = host.lower()
        # 仅在名称足够长时允许包含匹配，避免短名误判同站
        if len(d) >= 3 and len(t) >= 3 and (t in d or d in t):
            return True
        return False

    def _validate_and_upsert(
        self, import_no: str, site: str, batch: str, file_name: str,
        records: list[dict[str, Any]], parse_errors: list[dict[str, Any]],
        previous: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        prev_by_page: dict[str, dict[str, Any]] = {}
        if previous:
            for line in previous.get("lines", []):
                prev_by_page.setdefault(line.get("page_code", ""), line)

        sample_rows = store.rows("sample_store")
        lines: list[dict[str, Any]] = []
        seen_pages_this_attempt: set[str] = set()

        for rec in records:
            page = rec["page_code"]
            declared_site = rec["site"] or site
            fp = rec["fingerprint"]
            prev = prev_by_page.get(page, {})
            first_price = prev.get("first_price")
            line: dict[str, Any] = {
                "key": f"{import_no}:{page}:{rec['line_no']}",
                "line_no": rec["line_no"],
                "page_code": page,
                "site": declared_site,
                "target_site": site,
                "batch": rec["batch"] or batch,
                "fingerprint": fp,
                "declared_price": _to_number(rec["declared_price"]),
                "title": rec["title"],
                "verdict": "",
                "eligible": False,
                "reason": "",
                "follow_up": "",
                "sample_id": None,
                "first_price": first_price,
                "price_history": prev.get("price_history", []),
            }

            # 1) 跨站校验：跨站同指纹只拒绝，绝不触碰原样本
            if site and not self._site_matches(declared_site, site):
                original = next((s for s in sample_rows if str(s.get("样本指纹")) == fp), None)
                reason = f"该行站点为「{declared_site}」，不属于本次指定站点「{site}」，已整行剔除。"
                if original is not None:
                    reason += (
                        f" 指纹 {fp} 与站点「{original.get('站点名称')}」的原样本 #"
                        f"{original.get('id')} 相同，跨站同指纹不得覆盖原样本，原样本保持不变。"
                    )
                else:
                    reason += f" 即使指纹 {fp} 与他站材料相同，也不会写入或覆盖站点「{site}」的样本。"
                line.update({
                    "verdict": "跨站拒绝", "eligible": False, "reason": reason,
                    "follow_up": "改回正确站点后，在原导入请求上重新上传；本批回归不接收该行。",
                })
                lines.append(line)
                continue

            # 2) 完整性校验：半份 HTML 不能成为已验证样本
            complete, completeness_reasons = assess_completeness(rec)
            extracted = extract_fields(rec["html"]) if rec["html"] else None
            price = (extracted or {}).get("price")
            if price is None:
                price = _to_number(rec["declared_price"])
            line["partial_price"] = price
            line["price"] = price if complete else None
            line["price_text"] = (extracted or {}).get("price_text") or (rec["declared_price"] or "")
            line["extract"] = extracted

            if not complete:
                detail = "；".join(completeness_reasons) or "HTML 不完整"
                line.update({
                    "verdict": "损坏", "eligible": False,
                    "reason": f"损坏行单独说明｜{page} 只收到半份材料：{detail}。回归模块不能拿半份 HTML 当成已验证样本。",
                    "follow_up": "保留原导入请求号，补齐后半段 HTML 后重传；补传完成前该页在试跑中保持「等待补传」。",
                    "completeness_detail": completeness_reasons,
                })
                self._route_to_quarantine(rec, detail)
                lines.append(line)
                continue

            # 3) 同一次文件内同一页只接收一份合格来源
            if page in seen_pages_this_attempt:
                line.update({
                    "verdict": "重复来源", "eligible": False,
                    "reason": f"同一文件内页面 {page} 已存在一份合格来源，回归报告只接收一份有资格的来源。",
                    "follow_up": "无需处理；如内容为更正，请在原导入请求上重传同一页。",
                })
                lines.append(line)
                continue
            seen_pages_this_attempt.add(page)

            # 4) 合格 → 按 (导入请求, 页面) / (站点, 页面) / (站点, 指纹) 对账 upsert
            sample = next((s for s in sample_rows
                            if str(s.get("来源导入回执")) == import_no and str(s.get("页面编号")) == page), None)
            if sample is None:
                sample = next((s for s in sample_rows
                               if s.get("站点名称") == site and str(s.get("页面编号")) == page), None)
            if sample is None:
                sample = next((s for s in sample_rows
                               if s.get("站点名称") == site and str(s.get("样本指纹")) == fp), None)

            price_for_summary = price if price is not None else rec["declared_price"]
            summary = f"[{page}] {rec['title'] or (extracted or {}).get('title') or '历史页面'} | 价格 {price_for_summary or '未识别'} | 指纹 {fp}"
            if sample is None:
                sample = {
                    "id": _next_seq(sample_rows),
                    "status": "已保存", "pending": True, "abnormal": False,
                    "样本批次": batch, "站点名称": site or declared_site,
                    "原始HTML摘要": summary[:240],
                    "抓取时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "样本指纹": fp,
                }
                sample_rows.append(sample)
            else:
                sample["样本批次"] = batch
                sample["站点名称"] = site or sample.get("站点名称")
                sample["原始HTML摘要"] = summary[:240]
                sample["样本指纹"] = fp
                # 更正后重新具备试跑资格
                if sample.get("status") == "已过期":
                    sample["status"] = "已保存"
                sample["pending"] = True

            sample.update({
                "页面编号": page,
                "商品标题": rec["title"] or (extracted or {}).get("title", ""),
                "申报价格": _to_number(rec["declared_price"]),
                "校验价格": price,
                "币种": (extracted or {}).get("currency", "CNY"),
                "原始HTML全文": rec["html"],
                "来源导入回执": import_no,
                "来源文件": file_name,
                "最近校验时间": now_iso(),
                "eligible": True,
            })
            if first_price is None and price is not None:
                first_price = price
            history = list(prev.get("price_history", []))
            if price is not None and (not history or history[-1].get("price") != price):
                history.append({"at": now_iso(), "price": price, "file_name": file_name})

            line.update({
                "verdict": "已验证", "eligible": True,
                "reason": f"站点「{site or declared_site}」一致，HTML 完整，指纹 {fp} 已留存。",
                "follow_up": "可从同一批启动页面解析模板试跑。",
                "sample_id": sample["id"],
                "first_price": first_price,
                "price_history": history,
            })
            lines.append(line)

        # 无法解析的损坏行也进入回执（单独分组）
        for err in parse_errors:
            lines.append({
                "key": f"parse-error-{err.get('line_no')}",
                "line_no": err.get("line_no", 0),
                "page_code": "—", "site": "", "target_site": site, "batch": batch,
                "fingerprint": "", "verdict": "无法解析", "eligible": False,
                "reason": err.get("reason", "损坏行无法解析"),
                "follow_up": "请按导入模板修正该行后，在原导入请求上重试。",
                "sample_id": None,
            })
        # 序列化别名：兼容按 status / 中文键读取逐行校验结果的调用方
        for line in lines:
            line["status"] = line.get("verdict", "")
            line["校验结果"] = line.get("verdict", "")
            line["页面编号"] = line.get("page_code", "")
            line["站点名称"] = line.get("site", "")
            line["后续动作"] = line.get("follow_up", "")
            line["行内原因"] = line.get("reason", "")
        return lines

    def _route_to_quarantine(self, rec: dict[str, Any], detail: str) -> None:
        qrows = store.rows("failure_quarantine")
        qrows.append({
            "id": _next_seq(qrows),
            "样本指纹": rec.get("fingerprint", ""),
            "失败阶段": "导入传输校验",
            "失败原因": f"{rec.get('page_code')} 半份 HTML：{detail}"[:200],
            "隔离时间": now_iso(),
            "重试次数": "0",
            "status": "待处理", "pending": True, "abnormal": True,
        })

    def get_import(self, import_no: str) -> dict[str, Any] | None:
        return self.imports.get(import_no)

    def list_imports(self) -> list[dict[str, Any]]:
        items = sorted(self.imports.values(), key=lambda x: x.get("updated_at", ""), reverse=True)
        # 列表不带超长行内 extract 细节之外的内容，本身已经足够小
        return [self._receipt_summary(i) for i in items]

    def _receipt_summary(self, receipt: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in receipt.items() if k != "lines"} | {
            "line_count": len(receipt.get("lines", [])),
        }

    # ---------------- 模板领取 + 试跑 ----------------

    def _resolve_template(self, site: str, template_id: Any = None) -> dict[str, Any] | None:
        rows = store.rows("page_parser")
        if template_id not in (None, "", 0):
            try:
                tid = int(template_id)
                tpl = next((t for t in rows if int(t.get("id", 0)) == tid), None)
                if tpl:
                    return tpl
            except (TypeError, ValueError):
                tpl = next((t for t in rows if str(t.get("模板名称")) == str(template_id)), None)
                if tpl:
                    return tpl
        enabled = [t for t in rows if t.get("status") == "已启用"]
        return next((t for t in enabled if t.get("适用站点") == site), None) or (enabled[0] if enabled else None)

    def claimable_samples(self, batch: str = "", site: str = "") -> dict[str, Any]:
        """页面解析模板侧“领取回归样本”的候选清单。"""
        rows = [s for s in store.rows("sample_store") if s.get("eligible")]
        if batch:
            rows = [s for s in rows if s.get("样本批次") == batch]
        if site:
            rows = [s for s in rows if s.get("站点名称") == site]
        batches = sorted({str(s.get("样本批次", "")) for s in store.rows("sample_store") if s.get("eligible")})
        return {
            "ok": True,
            "batches": batches,
            "samples": [{
                "sample_id": s.get("id"), "page_code": s.get("页面编号"),
                "batch": s.get("样本批次"), "site": s.get("站点名称"),
                "fingerprint": s.get("样本指纹"), "import_no": s.get("来源导入回执"),
                "price_hint": s.get("校验价格"), "status": s.get("status"),
            } for s in rows],
        }

    def create_trial(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch = str(payload.get("batch") or payload.get("样本批次") or "").strip()
        site = str(payload.get("site") or payload.get("站点名称") or "").strip()
        import_no = str(payload.get("import_no") or payload.get("import_request_no") or "").strip()
        template_id = payload.get("template_id")

        receipt = self.imports.get(import_no) if import_no else None
        if receipt is None and batch:
            receipt = self._latest_import_for_batch(batch)
            if receipt:
                import_no = receipt["import_no"]
        if receipt is not None:
            batch = batch or receipt.get("batch", "")
            site = site or receipt.get("site", "")

        template = self._resolve_template(site, template_id)
        self._trial_seq += 1
        trial_no = f"REG-{datetime.now().strftime('%Y%m%d')}-{self._trial_seq:03d}"
        trial = {
            "id": self._trial_seq,
            "trial_no": trial_no,
            "regression_batch": trial_no,
            "batch": batch,
            "site": site,
            "import_no": import_no,
            "template_id": (template or {}).get("id"),
            "template_name": (template or {}).get("模板名称", "通用价格提取模板（兜底）"),
            "template_version": (template or {}).get("解析版本", "generic-v1"),
            "credential": "dc-" + secrets.token_urlsafe(9),
            "status": "排队中",
            "acceptance": "已受理",
            "needs_reacceptance": False,
            "continue_processing": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "results": [], "rejected": [], "pending": [],
            "events": [],
            "message": "",
        }
        trial["events"].append({"at": now_iso(), "kind": "accept",
                                "text": f"试跑窗口已受理本次回归 {trial_no}，绑定批次 {batch or '—'} 与原导入请求 {import_no or '—'}。"})
        self.trials[trial_no] = trial
        self.execute_trial(trial)
        return trial

    def _latest_import_for_batch(self, batch: str) -> dict[str, Any] | None:
        candidates = [i for i in self.imports.values() if i.get("batch") == batch]
        return max(candidates, key=lambda i: i.get("updated_at", "")) if candidates else None

    def execute_trial(self, trial: dict[str, Any]) -> dict[str, Any]:
        receipt = self.imports.get(trial.get("import_no", ""))
        if receipt is None and trial.get("batch"):
            receipt = self._latest_import_for_batch(trial["batch"])
            if receipt:
                trial["import_no"] = receipt["import_no"]

        template = self._resolve_template(trial.get("site", ""), trial.get("template_id"))
        if template is not None:
            trial["template_id"] = template.get("id")
            trial["template_name"] = template.get("模板名称")
            trial["template_version"] = template.get("解析版本")

        # 合格来源：优先回执中的已验证行（每页一份），否则退回样本表
        eligible_lines: list[dict[str, Any]] = []
        if receipt is not None:
            eligible_lines = [l for l in receipt.get("lines", []) if l.get("verdict") == "已验证" and l.get("eligible")]
            trial["rejected"] = [
                self._outcome_from_line(l) for l in receipt.get("lines", []) if l.get("verdict") == "跨站拒绝"
            ]
            trial["pending"] = [
                self._outcome_from_line(l) for l in receipt.get("lines", []) if l.get("verdict") in ("损坏", "无法解析")
            ]
        else:
            rows = [s for s in store.rows("sample_store")
                    if s.get("eligible") and (not trial.get("batch") or s.get("样本批次") == trial["batch"])]
            eligible_lines = [{
                "page_code": s.get("页面编号") or f"#{s.get('id')}", "sample_id": s.get("id"),
                "site": s.get("站点名称"), "fingerprint": s.get("样本指纹"),
            } for s in rows]

        # 同一页只计算一次（回归报告只接收一份有资格的来源）
        dedup: dict[str, dict[str, Any]] = {}
        for line in eligible_lines:
            dedup.setdefault(line["page_code"], line)

        results: list[dict[str, Any]] = []
        sample_rows = store.rows("sample_store")
        for page, line in sorted(dedup.items()):
            sample = store.find("sample_store", int(line["sample_id"])) if line.get("sample_id") else None
            if sample is None:
                sample = next((s for s in sample_rows
                               if s.get("站点名称") == line.get("site") and str(s.get("页面编号")) == page), None)
            html = (sample or {}).get("原始HTML全文", "") or line.get("extract", {}).get("html", "")
            fields = extract_fields(html, template)
            price = fields["price"]
            if price is None:
                price = line.get("price") or line.get("declared_price") or (sample or {}).get("校验价格")
                if price is not None:
                    fields["price"] = float(price)
                    fields["extractor"] = fields["extractor"] if fields["extractor"] != "none" else "structured-field"
                    fields["price_text"] = f"¥{float(price):.2f}"
            if sample is not None:
                sample["status"] = "已解析"
                sample["pending"] = False
                sample["最近试跑批次"] = trial["trial_no"]
                sample["提取价格"] = fields.get("price")
                sample["提取字段"] = json.dumps(
                    {"价格": fields.get("price"), "商品名称": fields.get("title"),
                     "币种": fields.get("currency")}, ensure_ascii=False)
            results.append({
                "page_code": page,
                "sample_id": (sample or {}).get("id") or line.get("sample_id"),
                "import_no": (sample or {}).get("来源导入回执") or trial.get("import_no"),
                "site": line.get("site") or trial.get("site"),
                "fingerprint": line.get("fingerprint") or (sample or {}).get("样本指纹"),
                "title": fields.get("title") or (sample or {}).get("商品标题", ""),
                "price": fields.get("price"),
                "price_display": (f"{fields['price']:g}" if fields.get("price") is not None else "未提取到"),
                "price_text": fields.get("price_text", ""),
                "currency": fields.get("currency", "CNY"),
                "extractor": fields.get("extractor"),
                "selector_used": fields.get("selector_used"),
                "candidates": fields.get("candidates", []),
                "status": "已提取",
                # 显式“解析字段”，方便按字段核对回归批次
                "parsed_fields": {
                    "价格": fields.get("price"),
                    "商品名称": fields.get("title") or (sample or {}).get("商品标题", ""),
                    "币种": fields.get("currency", "CNY"),
                    "价格原文": fields.get("price_text", ""),
                },
                "fields": {
                    "price": fields.get("price"),
                    "title": fields.get("title") or (sample or {}).get("商品标题", ""),
                    "currency": fields.get("currency", "CNY"),
                },
            })

        trial["results"] = results
        trial["valid_count"] = len(results)
        trial["computed_pages"] = [r["page_code"] for r in results]
        i1 = next((r for r in results if r["page_code"] == "I1"), None)
        trial["adopted_i1_price"] = (i1 or {}).get("price")
        trial["adopted_price"] = (i1 or {}).get("price")
        trial["cross_site_count"] = len(trial["rejected"])
        trial["pending_count"] = len(trial["pending"])

        if trial["pending"]:
            trial["status"] = "等待补传"
            trial["state"] = "waiting_reupload"
            trial["acceptance"] = "已受理 · 等待补传"
            trial["message"] = (
                f"已真实提取 {len(results)} 条有效记录（计算页面：{'、'.join(trial['computed_pages']) or '无'}）；"
                f"{len(trial['pending'])} 个损坏/半传页面不能进入回归，状态为等待补传；"
                f"{len(trial['rejected'])} 行跨站数据已剔除并给出行内拒绝原因。"
            )
        else:
            trial["status"] = "已完成"
            trial["state"] = "completed"
            trial["acceptance"] = "已受理 · 已完成"
            trial["message"] = (
                f"本批回归完成，共 {len(results)} 条有效记录；"
                f"{len(trial['rejected'])} 行跨站数据已剔除。"
            )
        trial["updated_at"] = now_iso()
        return trial

    def _outcome_from_line(self, line: dict[str, Any]) -> dict[str, Any]:
        return {
            "line_no": line.get("line_no"),
            "page_code": line.get("page_code"),
            "site": line.get("site"),
            "fingerprint": line.get("fingerprint"),
            "sample_id": line.get("sample_id"),
            "reason": line.get("reason"),
            "follow_up": line.get("follow_up"),
            "verdict": line.get("verdict"),
            "completeness_detail": line.get("completeness_detail", []),
        }

    def sync_trials_for_import(self, import_no: str) -> list[dict[str, Any]]:
        """原导入请求送达更正后，让绑定的试跑窗口继续处理，而不是另起一批。"""
        synced: list[dict[str, Any]] = []
        receipt = self.imports.get(import_no)
        if receipt is None:
            return synced
        for trial in self.trials.values():
            linked = trial.get("import_no") == import_no or (
                not trial.get("import_no") and trial.get("batch") == receipt.get("batch")
            )
            if not linked:
                continue
            old_prices = {r["page_code"]: r.get("price") for r in trial.get("results", [])}
            old_status = trial.get("status")
            self.execute_trial(trial)
            new_prices = {r["page_code"]: r.get("price") for r in trial.get("results", [])}
            for page, new_price in new_prices.items():
                old_price = old_prices.get(page)
                if old_price is not None and new_price is not None and float(old_price) != float(new_price):
                    trial["events"].append({
                        "at": now_iso(), "kind": "correction",
                        "text": (f"原导入请求 {import_no} 送达 {page} 更正：采用价格由 {old_price:g} "
                                 f"更新为 {new_price:g}；同一回归批次 {trial['trial_no']} 继续处理，"
                                 f"无需重新受理，回归报告仍只保留一份合格来源。"),
                    })
                elif page not in old_prices:
                    trial["events"].append({
                        "at": now_iso(), "kind": "new-source",
                        "text": f"{page} 补传完成并通过校验，已在同一回归批次继续提取。",
                    })
            if old_status == "等待补传" and trial["status"] == "已完成":
                trial["events"].append({"at": now_iso(), "kind": "settled",
                                        "text": "全部待补传页面已补齐，回归批次处理完成。"})
            elif trial["status"] == "等待补传":
                trial["events"].append({
                    "at": now_iso(), "kind": "waiting",
                    "text": "试跑窗口保持开启：已跟进最新合格来源，损坏页面仍在等待补传，无需重新受理。",
                })
            synced.append(trial)
        return synced

    def refresh_trial(self, ref: str) -> dict[str, Any] | None:
        trial = self.get_trial(ref)
        if trial is None:
            return None
        self.execute_trial(trial)
        trial["events"].append({"at": now_iso(), "kind": "refresh", "text": "审核员在原试跑窗口手动刷新，已按最新导入回执重新核对合格来源。"})
        return trial

    def reaccept_trial(self, ref: str) -> dict[str, Any] | None:
        trial = self.get_trial(ref)
        if trial is None:
            return None
        trial["needs_reacceptance"] = False
        trial["acceptance"] = "重新受理 · 已受理"
        trial["events"].append({"at": now_iso(), "kind": "reaccept",
                                "text": "审核员选择重新受理：沿用同一试跑批次号与下载凭据，按当前最新材料重新试跑。"})
        self.execute_trial(trial)
        return trial

    def get_trial(self, ref: str) -> dict[str, Any] | None:
        if ref in self.trials:
            return self.trials[ref]
        try:
            tid = int(ref)
        except (TypeError, ValueError):
            return None
        return next((t for t in self.trials.values() if int(t.get("id", 0)) == tid), None)

    def list_trials(self, batch: str = "") -> list[dict[str, Any]]:
        items = sorted(self.trials.values(), key=lambda t: t.get("created_at", ""), reverse=True)
        if batch:
            items = [t for t in items if t.get("batch") == batch]
        return items

    # ---------------- 可下载试跑回执 ----------------

    def render_receipt_csv(self, trial: dict[str, Any]) -> str:
        output = io.StringIO()
        output.write("\ufeff")  # UTF-8 BOM，Excel 打开中文不乱码
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "回归批次", "导入回执号", "页面编号", "样本ID", "站点", "解析模板", "模板版本",
            "商品标题", "采用价格", "币种", "价格原文", "提取策略", "样本指纹",
            "校验结论", "拒绝/待补传原因", "后续动作", "下载凭据",
        ])
        common = [
            trial.get("trial_no", ""), trial.get("import_no", ""),
        ]
        tpl = [trial.get("template_name", ""), trial.get("template_version", "")]
        for r in trial.get("results", []):
            writer.writerow([
                *common, r.get("page_code", ""), r.get("sample_id", ""), r.get("site", ""),
                *tpl, r.get("title", ""),
                "" if r.get("price") is None else f"{float(r['price']):g}",
                r.get("currency", ""), r.get("price_text", ""), r.get("extractor", ""),
                r.get("fingerprint", ""), "有效-已真实提取价格字段", "", "",
                trial.get("credential", ""),
            ])
        for r in trial.get("rejected", []):
            writer.writerow([
                *common, r.get("page_code", ""), r.get("sample_id", ""), r.get("site", ""),
                *tpl, "", "", "", "", "", r.get("fingerprint", ""),
                "已剔除-跨站拒绝", r.get("reason", ""), r.get("follow_up", ""),
                trial.get("credential", ""),
            ])
        for r in trial.get("pending", []):
            writer.writerow([
                *common, r.get("page_code", ""), r.get("sample_id", ""), trial.get("site", ""),
                *tpl, "", "", "", "", "", r.get("fingerprint", ""),
                "待补传-损坏/半份HTML", r.get("reason", ""), r.get("follow_up", ""),
                trial.get("credential", ""),
            ])
        return output.getvalue()

    def receipt_headers(self, trial: dict[str, Any], fmt: str) -> dict[str, str]:
        ext = "json" if fmt == "json" else "csv"
        filename = f"试跑回执_{trial.get('trial_no')}.{ext}"
        return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


regression_service = RegressionService()
