"""模型输出的 JSON 宽松解析。

单独抽出来是因为 M2 的大纲与 M3 的图谱抽取都要用，而两边都在
`app/api` 与 `app/studio` 里——不抽出来的话，studio 模块要反过来 import api 层。

两个坑：
1. 模型经常在字符串里直接写换行/制表符（合法 JSON 要求转义成 \\n），
   `strict=False` 能救回一大半「看起来对但解析失败」的输出。
2. 输出前后常带解释、``` 围栏、省略号，需要按括号配对截出真正的 JSON 片段。
"""
from __future__ import annotations

import json
import re
from typing import Any


def loads_loose(text: str) -> Any:
    """带 strict=False 的 json.loads；解析不出来返回 None。"""
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def parse_json_loose(text: str) -> Any:
    """从一坨模型输出里尽力抠出 JSON 对象/数组。失败返回 None。"""
    raw = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    got = loads_loose(raw)
    if got is not None:
        return got

    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    opening = raw[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return loads_loose(raw[start:i + 1])
    return None


def as_list(value: Any) -> list:
    """模型有时给单个对象而不是数组，统一成数组。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_str(value: Any, limit: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.strip()
    return text[:limit] if limit else text
