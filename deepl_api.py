#!/usr/bin/env python3
"""
DeepL API 翻译模块
==================
DeepL 是欧洲语言翻译质量最高的专用 NMT 引擎。
德语→中文 远超百度/Google，特别适合汽车 ECU 标定文件。

免费额度: 50万字符/月
注册: https://www.deepl.com/pro-api
"""

import json
import time
import urllib.request
import urllib.error

_DEEPL_API_URL = "https://api-free.deepl.com/v2/translate"  # 免费版
# _DEEPL_API_URL = "https://api.deepl.com/v2/translate"     # 付费版

_MAX_RETRIES = 3
_RETRY_DELAY = 0.5


def deepl_translate_batch(texts, src_lang, tgt_lang, api_key, timeout=30):
    """
    DeepL API 批量翻译。

    Args:
        texts: 待翻译文本列表
        src_lang: 源语言 (EN, DE, auto)
        tgt_lang: 目标语言 (ZH)
        api_key: DeepL API key
        timeout: 超时秒数

    Returns:
        翻译结果列表，失败返回 None
    """
    # 语言代码映射
    lang_map = {"auto": None, "en": "EN", "de": "DE", "zh-CN": "ZH", "zh": "ZH"}
    src = lang_map.get(src_lang, src_lang.upper())
    tgt = lang_map.get(tgt_lang, tgt_lang.upper())

    # DeepL 用 \n 分隔多条文本
    combined = "\n".join(texts)

    params = {"text": combined, "target_lang": tgt}
    if src:
        params["source_lang"] = src

    data = urllib.parse.urlencode(params).encode("utf-8")

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                _DEEPL_API_URL,
                data=data,
                headers={
                    "Authorization": f"DeepL-Auth-Key {api_key}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "A2L-Translator/2.9.5",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            translations = result.get("translations", [])
            output = [t.get("text", "") for t in translations]
            return output

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except:
                pass
            if e.code == 403:
                print(f"  [DeepL 错误] API Key 无效或无权访问")
                return None
            if e.code == 456:
                print(f"  [DeepL 错误] 免费额度已用完")
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
                continue
            print(f"  [DeepL HTTP {e.code}] {body}")
            return None
        except Exception as e:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
                continue
            print(f"  [DeepL 错误] {e}")
            return None

    return None
