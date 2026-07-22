#!/usr/bin/env python3
"""
百度翻译 API 模块
==================
供 GUI 和 CLI 共同使用。支持通用翻译 + 汽车领域模式。

标准版: 每月 100 万字符免费
高级版: 支持领域定制（汽车 auto），每月 100 万字符免费

使用前需要在 https://fanyi-api.baidu.com 注册获取 APP ID 和密钥。
"""

import json
import hashlib
import random
import ssl
import time
import urllib.request
import urllib.error
import urllib.parse


# API 端点
_BAIDU_API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

# ── 性能优化：连接复用 + 重试 ──
_MAX_RETRIES = 3
_RETRY_DELAY = 0.5  # 重试间隔秒数

# 语言代码映射：工具内部 → 百度翻译
_LANG_MAP = {
    "auto":  "auto",
    "en":    "en",
    "de":    "de",
    "zh-CN": "zh",
    "zh":    "zh",
    "ja":    "jp",
    "ko":    "kor",
    "fr":    "fra",
    "es":    "spa",
    "it":    "it",
    "nl":    "nl",
    "pl":    "pl",
    "pt":    "pt",
    "ru":    "ru",
}


def _to_baidu_lang(lang: str) -> str | None:
    """映射内部语言代码到百度语言代码"""
    return _LANG_MAP.get(lang, lang)


def _make_sign(appid: str, text: str, salt: str, secret: str) -> str:
    """生成百度翻译 API 签名: MD5(appid + text + salt + secret)"""
    raw = appid + text + salt + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def baidu_translate_one(
    text: str,
    src_lang: str,
    tgt_lang: str,
    appid: str,
    secret: str,
    ssl_ctx=None,
    timeout: int = 20,
) -> str | None:
    """调用百度翻译 API 翻译单条文本"""
    results = baidu_translate_batch([text], src_lang, tgt_lang, appid, secret, ssl_ctx, timeout)
    if results and len(results) > 0:
        return results[0]
    return None


def baidu_translate_batch(
    texts: list[str],
    src_lang: str,
    tgt_lang: str,
    appid: str,
    secret: str,
    ssl_ctx=None,
    timeout: int = 30,
) -> list[str] | None:
    """调用百度翻译 API 批量翻译多条文本

    百度翻译不原生支持批量，但可通过换行符 \n 拼接多条文本，
    API 返回时会按 \n 拆分结果。

    Args:
        texts: 待翻译文本列表
        src_lang: 源语言代码
        tgt_lang: 目标语言代码
        appid: 百度翻译 APP ID
        secret: 百度翻译密钥
        ssl_ctx: SSL 上下文
        timeout: 请求超时秒数

    Returns:
        翻译结果列表 (与输入顺序一致)，失败时返回 None
    """
    baidu_src = _to_baidu_lang(src_lang)
    baidu_tgt = _to_baidu_lang(tgt_lang)

    if baidu_tgt is None:
        print(f"  [百度翻译 错误] 不支持的目标语言: {tgt_lang}")
        return None

    # 用 \n 拼接多条文本（百度 API 会按 \n 分割返回）
    combined_text = "\n".join(texts)
    salt = str(random.randint(10000, 99999))
    sign = _make_sign(appid, combined_text, salt, secret)

    params = {
        "q":     combined_text,
        "from":  baidu_src,
        "to":    baidu_tgt,
        "appid": appid,
        "salt":  salt,
        "sign":  sign,
    }

    # 编码为 URL 参数
    encoded = urllib.parse.urlencode(params).encode("utf-8")

    req = urllib.request.Request(
        _BAIDU_API_URL,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "A2L-Translator/2.9.5",
            "Connection": "keep-alive",
        },
        method="POST",
    )

    # ── 带重试的请求（提升网络健壮性）──
    result = None
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            break  # 成功，退出重试循环
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            last_error = f"HTTP {e.code}: {e.reason}" + (f" — {body_text}" if body_text else "")
            if e.code in (429, 500, 502, 503, 504):  # 可重试的服务端错误
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY * attempt)
                    continue
        except urllib.error.URLError as e:
            err_msg = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in err_msg or "certificate" in err_msg.lower():
                print(f"  [百度翻译 SSL 错误] 证书验证失败，可启用'跳过SSL验证'")
                return None
            last_error = str(e)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
                continue
        except json.JSONDecodeError as e:
            print(f"  [百度翻译 解析错误] {e}")
            return None
        except Exception as e:
            last_error = str(e)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
                continue

    if result is None:
        print(f"  [百度翻译 请求失败（{_MAX_RETRIES}次重试后）] {last_error}")
        return None

    # 检查错误码
    error_code = result.get("error_code")
    if error_code:
        error_msg = result.get("error_msg", "")
        _report_error(error_code, error_msg)
        return None

    # 解析翻译结果
    trans_list = result.get("trans_result", [])
    if not trans_list:
        print(f"  [百度翻译 警告] 返回为空")
        return []

    output = []
    for t in trans_list:
        output.append(t.get("dst", ""))

    return output


def _report_error(code, msg):
    """友好的错误码提示"""
    errors = {
        "52000": "成功",
        "52001": "请求超时，请重试",
        "52002": "系统错误，请重试",
        "52003": "未授权用户 → 请检查 APP ID 是否正确",
        "54000": "必填参数为空",
        "54001": "签名错误 → 请检查密钥是否正确",
        "54003": "访问频率受限 → 请稍后重试",
        "54004": "账户余额不足",
        "54005": "长query请求频繁 → 请降低调用频率",
        "58000": "客户端IP非法 → 检查IP是否在白名单",
        "58001": "译文语言不支持",
        "58002": "服务当前已关闭",
        "90107": "认证未通过或未开通服务",
    }
    friendly = errors.get(code, msg or "未知错误")
    print(f"  [百度翻译 错误 {code}] {friendly}")


def baidu_usage(appid: str, secret: str, ssl_ctx=None, timeout: int = 15) -> dict | None:
    """查询百度翻译 API 使用量（近似：翻译一条短文本检测是否耗尽）

    百度翻译没有独立的用量查询接口，此函数仅检测 API 是否可用。
    """
    result = baidu_translate_one("test", "en", "zh", appid, secret, ssl_ctx, timeout)
    if result is not None:
        return {"status": "ok", "message": "API 可用"}
    return {"status": "error", "message": "API 不可用"}
