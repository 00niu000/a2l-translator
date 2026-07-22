#!/usr/bin/env python3
"""
Google Cloud Translation API 模块
=================================
覆盖面最广 (135+ 语言)，质量稳定。

免费额度: 50万字符/月
注册: https://cloud.google.com/translate
需要创建服务账号并获取 JSON 密钥文件，或使用 API Key。
"""

import json
import time
import urllib.request
import urllib.error

# 免费版使用 API Key 方式（简单，不需要 OAuth）
_GOOGLE_API_URL = "https://translation.googleapis.com/language/translate/v2"

_MAX_RETRIES = 3
_RETRY_DELAY = 0.5


def google_translate_batch(texts, src_lang, tgt_lang, api_key, timeout=30):
    """
    Google Cloud Translation API v2 批量翻译。

    Args:
        texts: 待翻译文本列表
        src_lang: 源语言 (en, de, auto)
        tgt_lang: 目标语言 (zh-CN)
        api_key: Google API Key
        timeout: 超时秒数

    Returns:
        翻译结果列表，失败返回 None
    """
    lang_map = {"auto": None, "zh-CN": "zh-CN", "zh": "zh"}
    src = lang_map.get(src_lang, src_lang)
    tgt = lang_map.get(tgt_lang, tgt_lang)

    # Google API 用 q[] 数组传多条
    params = {"q": texts, "target": tgt, "format": "text"}
    if src:
        params["source"] = src
    params["key"] = api_key

    encoded = urllib.parse.urlencode(params, doseq=True).encode("utf-8")

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                _GOOGLE_API_URL,
                data=encoded,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "A2L-Translator/2.9.5",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            translations = result.get("data", {}).get("translations", [])
            output = [t.get("translatedText", "") for t in translations]
            return output

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except:
                pass
            if e.code == 403:
                print(f"  [Google 错误] API Key 无效或未启用 Cloud Translation API")
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
                continue
            print(f"  [Google HTTP {e.code}] {body}")
            return None
        except Exception as e:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
                continue
            print(f"  [Google 错误] {e}")
            return None

    return None
