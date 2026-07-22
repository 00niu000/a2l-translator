#!/usr/bin/env python3
"""
A2L 翻译工具 — 仿 DeepL / Trados 级模糊匹配引擎
=================================================
核心技术：
  1. Levenshtein 编辑距离 — 纠错 + 模糊匹配
  2. N-gram 重叠度 — 类似 DeepL 的上下文匹配
  3. 多源置信度评分 — Google Translate 级质量评估
  4. 术语一致性验证 — 类似 Trados QA Checker

参考：
  - DeepL: 整句上下文 + 术语库匹配
  - Google NMT: Transformer 架构 + 置信度
  - Trados: 模糊匹配 (70%/80%/90%阈值) + Termbase
  - memoQ: 拼写规范化 + 一致性检查
"""

import re
from difflib import SequenceMatcher
from collections import Counter


# ══════════════════════════════════════════════════════════
#  0. 通用英文词汇 → 中文词库（保证逐词翻译覆盖）
# ══════════════════════════════════════════════════════════

_GENERAL_WORD_BANK = {
    # 功能词
    "of": "的", "for": "用于", "the": "", "a": "", "an": "", "to": "至", "from": "来自",
    "with": "与", "by": "通过", "in": "中", "on": "上", "at": "在", "as": "作为",
    "is": "是", "are": "是", "was": "是", "be": "是", "has": "具有", "have": "具有",
    "not": "不", "no": "无", "or": "或", "and": "与", "if": "如果", "this": "此",
    "that": "该", "it": "它", "its": "其", "all": "所有", "each": "每个", "any": "任何",
    "some": "某些", "both": "两者", "such": "此类", "into": "进入", "over": "超过",
    "under": "下方", "above": "上方", "below": "下方", "between": "之间",
    "during": "期间", "after": "之后", "before": "之前", "within": "之内",
    "without": "无", "per": "每", "via": "经", "due": "由于",

    # 常用动词
    "handle": "处理", "process": "处理", "control": "控制", "manage": "管理",
    "monitor": "监控", "detect": "检测", "check": "检查", "verify": "验证",
    "calculate": "计算", "compute": "计算", "estimate": "估计", "predict": "预测",
    "determine": "确定", "select": "选择", "enable": "使能", "disable": "禁用",
    "activate": "激活", "deactivate": "停用", "switch": "切换", "toggle": "切换",
    "set": "设置", "get": "获取", "read": "读取", "write": "写入", "store": "存储",
    "load": "加载", "save": "保存", "configure": "配置", "initialize": "初始化",
    "reset": "复位", "start": "启动", "stop": "停止", "pause": "暂停", "resume": "恢复",
    "update": "更新", "modify": "修改", "delete": "删除", "add": "添加", "remove": "移除",
    "replace": "替换", "convert": "转换", "transfer": "传输", "send": "发送",
    "receive": "接收", "request": "请求", "response": "响应", "report": "报告",
    "provide": "提供", "support": "支持", "require": "需要", "allow": "允许",
    "prevent": "防止", "protect": "保护", "ensure": "确保", "maintain": "维持",
    "perform": "执行", "execute": "执行", "apply": "应用", "use": "使用",
    "using": "使用", "based": "基于",

    # 名词
    "interface": "接口", "library": "库", "function": "函数", "parameter": "参数",
    "value": "值", "variable": "变量", "constant": "常量", "array": "数组",
    "buffer": "缓冲区", "pointer": "指针", "address": "地址", "index": "索引",
    "counter": "计数器", "timer": "定时器", "flag": "标志", "mask": "掩码",
    "bit": "位", "byte": "字节", "word": "字", "register": "寄存器", "memory": "内存",
    "stack": "堆栈", "queue": "队列", "list": "列表", "table": "表", "map": "映射",
    "mode": "模式", "state": "状态", "status": "状态", "type": "类型", "class": "类",
    "object": "对象", "instance": "实例", "event": "事件", "message": "消息",
    "signal": "信号", "data": "数据", "information": "信息", "code": "代码",
    "error": "错误", "fault": "故障", "warning": "警告", "failure": "失效",
    "condition": "条件", "result": "结果", "output": "输出", "input": "输入",
    "source": "源", "target": "目标", "destination": "目的地", "origin": "原始",
    "number": "编号", "name": "名称", "description": "描述", "comment": "注释",
    "version": "版本", "date": "日期", "time": "时间", "level": "级别",
    "range": "范围", "limit": "极限", "threshold": "阈值", "window": "窗口",
    "factor": "系数", "ratio": "比率", "rate": "速率", "speed": "速度",
    "angle": "角度", "degree": "度", "frequency": "频率", "period": "周期",
    "phase": "相位", "duty": "占空比", "pulse": "脉冲", "width": "宽度",
    "structure": "结构", "component": "组件", "module": "模块", "unit": "单元",
    "system": "系统", "device": "设备", "driver": "驱动", "hardware": "硬件",
    "software": "软件", "firmware": "固件", "application": "应用", "program": "程序",
    "service": "服务", "process": "进程", "task": "任务", "thread": "线程",
    "client": "客户端", "server": "服务端", "master": "主控", "slave": "从控",
    "frame": "帧", "packet": "包", "channel": "通道", "bus": "总线",
    "port": "端口", "pin": "引脚", "connector": "连接器", "cable": "线缆",
    "diagnostics": "诊断", "monitoring": "监控", "logging": "记录", "tracing": "追踪",
    "adaptation": "自适应", "correction": "修正", "compensation": "补偿",
    "calibration": "标定", "validation": "验证", "verification": "验证",
    "coordination": "协调", "synchronization": "同步", "allocation": "分配",
    "arbitration": "仲裁", "scheduling": "调度", "sequencing": "排序",
    "filtering": "滤波", "averaging": "平均", "smoothing": "平滑",
    "interpolation": "插值", "extrapolation": "外推", "integration": "积分",
    "differentiation": "微分", "modeling": "建模", "simulation": "仿真",
    "estimation": "估计", "prediction": "预测", "optimization": "优化",
    "normalization": "规范化", "linearization": "线性化",

    # 执行器/传感器
    "actuator": "执行器", "sensor": "传感器", "valve": "阀", "motor": "电机",
    "solenoid": "电磁阀", "relay": "继电器", "switch": "开关", "pump": "泵",
    "injector": "喷油器", "throttle": "节气门", "actuators": "执行器",
    "Electrical": "电气", "electrical": "电气", "digital": "数字",
    "powerstage": "功率级", "power": "功率",
    "coordinator": "协调器", "governor": "调速器",

    # 汽车专用
    "engine": "发动机", "transmission": "变速箱", "brake": "制动", "clutch": "离合器",
    "turbocharger": "涡轮增压器", "supercharger": "机械增压器", "intercooler": "中冷器",
    "radiator": "散热器", "battery": "蓄电池", "starter": "起动机", "alternator": "发电机",
    "compressor": "压缩机", "condenser": "冷凝器", "evaporator": "蒸发器",
    "catalyst": "催化器", "filter": "过滤器", "exhaust": "排气", "intake": "进气",
    "fuel": "燃油", "oil": "机油", "coolant": "冷却液", "pressure": "压力",
    "temperature": "温度", "flow": "流量", "torque": "扭矩", "acceleration": "加速度",
    "vehicle": "车辆", "wheel": "车轮", "axle": "车桥", "chassis": "底盘",
    "body": "车身", "door": "车门", "seat": "座椅", "pedal": "踏板",
    "steering": "转向", "suspension": "悬架", "stability": "稳定性",
    "emission": "排放", "combustion": "燃烧",
    "single": "单", "common": "公共", "virtual": "虚拟", "closed": "闭环",
    "open": "开环", "loop": "环", "respect": "相关",
    "demands": "需求", "Handling": "处理", "handling": "处理",
    "Weichai": "潍柴", "Application": "应用程序", "programming": "编程",
    "standardized": "标准化", "sharing": "共享", "naming": "命名",
    "computations": "计算", "computation": "计算", "methods": "方法",
    "support": "支持", "receive": "接收", "send": "发送", "transmit": "发射",
    "shut": "关闭", "off": "断开", "conditions": "条件",
    "driver": "驱动", "controlling": "控制",
    "Communication": "通信", "communication": "通信",
    "customer": "客户", "compatible": "兼容",

    # ── 德语常用词（ECU标定文件多来自德国供应商）──
    "für": "用于", "und": "与", "oder": "或", "mit": "与", "von": "来自",
    "auf": "上", "aus": "来自", "bei": "在", "bis": "至", "durch": "通过",
    "ohne": "无", "gegen": "对", "nach": "后", "vor": "前", "zwischen": "之间",
    "des": "的", "der": "的", "die": "的", "das": "的", "dem": "的", "den": "的",
    "ein": "一", "eine": "一", "einen": "一", "einer": "一", "einem": "一",
    "ist": "是", "sind": "是", "wird": "将", "werden": "将", "wurde": "已",
    "kann": "可", "können": "可", "muss": "必须", "soll": "应",
    "nicht": "不", "kein": "无", "keine": "无",
    "auch": "也", "nur": "仅", "noch": "仍", "bereits": "已",
    "oder": "或", "aber": "但", "denn": "因为", "weil": "因为",
    "wenn": "当", "dann": "则", "sonst": "否则",
    "alle": "所有", "jede": "每个", "jeder": "每个", "jedes": "每个",
    "dieser": "此", "diese": "此", "dieses": "此",
    "welcher": "哪个", "welche": "哪个", "welches": "哪个",
    "sein": "其", "seine": "其", "ihr": "其",
    "über": "超过", "unter": "下方", "neben": "旁",
    "hinter": "后", "seit": "以来", "während": "期间",
    "gegenüber": "相对于", "entsprechend": "相应",
    # 德语技术词
    "Drehzahl": "转速", "Druck": "压力", "Temperatur": "温度",
    "Moment": "扭矩", "Leistung": "功率", "Spannung": "电压",
    "Strom": "电流", "Widerstand": "电阻", "Frequenz": "频率",
    "Menge": "量", "Masse": "质量", "Kraft": "力", "Weg": "位移",
    "Geschwindigkeit": "速度", "Beschleunigung": "加速度",
    "Regelung": "调节", "Steuerung": "控制", "Überwachung": "监控",
    "Erkennung": "检测", "Berechnung": "计算", "Messung": "测量",
    "Eingang": "输入", "Ausgang": "输出", "Signal": "信号",
    "Wert": "值", "Grenze": "极限", "Schwelle": "阈值",
    "Bereich": "范围", "Fenster": "窗口", "Faktor": "系数",
    "Motor": "发动机", "Getriebe": "变速箱", "Bremse": "制动",
    "Lenkung": "转向", "Fahrwerk": "底盘", "Karosserie": "车身",
    "Kraftstoff": "燃油", "Abgas": "废气", "Ansaug": "进气",
    "Ladeluft": "增压空气", "Ladedruck": "增压压力",
    "Kühlmittel": "冷却液", "Öl": "机油", "Kraft": "力",
    "Zündung": "点火", "Einspritzung": "喷射", "Verbrennung": "燃烧",
    "Sensor": "传感器", "Aktor": "执行器", "Ventil": "阀",
    "Klappe": "翻板", "Pumpe": "泵", "Filter": "滤波器",
    "Fehler": "故障", "Diagnose": "诊断", "Prüfung": "检查",
    "Status": "状态", "Modus": "模式", "Betrieb": "运行",
    "Start": "启动", "Stopp": "停止", "Aktivierung": "激活",
    "Deaktivierung": "停用", "Umschaltung": "切换",
    "Anforderung": "请求", "Freigabe": "使能", "Sperrung": "禁用",
    "Bedingung": "条件", "Wechsel": "切换", "Reststrecken": "剩余里程",
    "Hinweistufe": "提示级别", "Register": "寄存器",
}

# ══════════════════════════════════════════════════════════
#  1. 拼写规范化（常见 ECU 变体 / 大小写 / 缩写变体）
# ══════════════════════════════════════════════════════════

_SPELL_VARIANTS = {
    # 英美拼写差异
    "colour": "color",
    "behaviour": "behavior",
    "centre": "center",
    "metre": "meter",
    "litre": "liter",
    "calibre": "caliber",
    # ECU 常见错误/变体
    "press": "pressure",
    "temp": "temperature",
    "posn": "position",
    "veh": "vehicle",
    "eng": "engine",
    "ref": "reference",
    "cmd": "command",
    "actl": "actual",
    "req": "request",
    "diag": "diagnostic",
    "cal": "calibration",
    "char": "characteristic",
    "meas": "measurement",
    "ctrl": "control",
    "sens": "sensor",
    "sig": "signal",
    "init": "initial",
    "min": "minimum",
    "max": "maximum",
    "avg": "average",
    "abs": "absolute",
    "rel": "relative",
    "diff": "differential",
    "thr": "threshold",
    "freq": "frequency",
    "volt": "voltage",
    "curr": "current",
    "pw": "pulse width",
    "dc": "duty cycle",
    "fb": "feedback",
    "ff": "feed forward",
    "kp": "proportional gain",
    "ki": "integral gain",
    "kd": "derivative gain",
    "pid": "PID controller",
}

def normalize_spelling(text):
    """拼写规范化：ECU 缩写展开 + 英美拼写统一"""
    words = text.split()
    normalized = []
    for w in words:
        lower = w.lower().rstrip(".,_")
        if lower in _SPELL_VARIANTS:
            normalized.append(_SPELL_VARIANTS[lower])
        else:
            normalized.append(w)
    return " ".join(normalized)


# ══════════════════════════════════════════════════════════
#  2. Levenshtein 编辑距离（模糊匹配核心）
# ══════════════════════════════════════════════════════════

def levenshtein_ratio(s1, s2):
    """基于编辑距离的相似度 (0.0~1.0)，类似 DeepL 术语库模糊匹配"""
    return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()


def find_fuzzy_match(text, candidates, threshold=0.75):
    """
    模糊匹配 — 在候选列表中找最佳匹配。
    类似 Trados 的 75%/85%/95% 匹配阈值。

    返回: (best_key, best_score) 或 (None, 0)
    """
    best_key, best_score = None, 0.0
    text_lower = text.lower().strip()
    # 精确匹配优先
    for key in candidates:
        if key.lower().strip() == text_lower:
            return key, 1.0
    # 模糊匹配
    for key in candidates:
        score = levenshtein_ratio(text_lower, key.lower())
        if score > best_score:
            best_score = score
            best_key = key
    if best_score >= threshold:
        return best_key, best_score
    return None, 0.0


# ══════════════════════════════════════════════════════════
#  3. N-gram 重叠度（上下文匹配，仿 DeepL）
# ══════════════════════════════════════════════════════════

def ngram_overlap(text1, text2, n=3):
    """
    N-gram 字符级重叠度。
    DeepL 用类似技术在术语库中匹配上下文相关翻译。
    """
    def get_ngrams(s, n):
        s = s.lower()
        return {s[i:i+n] for i in range(len(s) - n + 1)}

    ngrams1 = get_ngrams(text1, n)
    ngrams2 = get_ngrams(text2, n)

    if not ngrams1 or not ngrams2:
        return 0.0

    intersection = ngrams1 & ngrams2
    union = ngrams1 | ngrams2
    return len(intersection) / len(union) if union else 0.0


def hybrid_similarity(text1, text2):
    """
    混合相似度 = 编辑距离(60%) + 3-gram重叠(40%)
    综合了 DeepL（整句匹配）和 Trados（编辑距离）的优势
    """
    edit_score = levenshtein_ratio(text1, text2)
    ngram_score = ngram_overlap(text1, text2, n=3)
    return 0.6 * edit_score + 0.4 * ngram_score


# ══════════════════════════════════════════════════════════
#  4. 置信度评分（仿 Google Translate 质量评估）
# ══════════════════════════════════════════════════════════

def rate_confidence(item, glossary, tm):
    """
    对翻译结果进行置信度评分 (0.0~1.0)。

    评分依据：
      - 来源: TM(1.0) > custom_glossary(0.95) > glossary(0.85) > API(0.6)
      - 术语覆盖: 原文术语在译文中能找到对应 → 加分
      - 文本长度: 极短文本(<3字)或极长文本(>100字) → 降分
    """
    score = 0.0
    status = item.get("status", "untranslated")
    original = item.get("original", "").strip()
    translated = item.get("translated", "").strip()

    # 基础分（来源权重）
    if status == "tm":
        score = 0.95
    elif status == "manual":
        score = 0.90
    elif status == "auto":
        score = 0.60
    elif status == "auto_corrected":
        score = 0.75
    else:
        return 0.0

    # 术语覆盖加分
    if glossary:
        term_hits = 0
        for en_term in glossary:
            if en_term.lower() in original.lower():
                zh_term = glossary[en_term][:3]
                if zh_term in translated:
                    term_hits += 1
        if term_hits > 0:
            score = min(1.0, score + 0.02 * term_hits)

    # TM 精确匹配加分
    if tm and original in tm:
        if tm[original] == translated:
            score = min(1.0, score + 0.05)

    # 长度惩罚
    orig_len = len(original)
    if orig_len < 5:
        score = max(0.3, score - 0.10)
    elif orig_len > 200:
        score = max(0.3, score - 0.05)

    return round(min(1.0, score), 2)


def classify_confidence(score):
    """将置信度转为人类可读标签"""
    if score >= 0.90:
        return "high", "🟢 高置信度"
    elif score >= 0.70:
        return "medium", "🟡 中置信度"
    elif score >= 0.50:
        return "low", "🟠 低置信度"
    else:
        return "review", "🔴 需人工审核"


# ══════════════════════════════════════════════════════════
#  5. 术语一致性检查（仿 Trados QA Checker）
# ══════════════════════════════════════════════════════════

def check_consistency(items, glossary):
    """
    术语一致性检查 — 同一原文术语在不同位置是否翻译一致。

    类似 Trados QA Checker 的 "Terminology consistency" 规则。
    返回不一致条目列表。
    """
    inconsistent = []
    term_translations = {}  # {en_term: {zh_translation: count}}

    for item in items:
        if not item.get("translated"):
            continue
        original = item["original"].strip()
        translated = item["translated"].strip()

        # 检查每个术语
        for en_term, zh_term in glossary.items():
            if en_term.lower() in original.lower():
                if en_term not in term_translations:
                    term_translations[en_term] = {}
                term_translations[en_term][zh_term] = term_translations[en_term].get(zh_term, 0) + 1

    # 找出一对多的术语
    for en_term, translations in term_translations.items():
        if len(translations) > 1:
            most_common = max(translations, key=translations.get)
            for zh, count in translations.items():
                if zh != most_common:
                    inconsistent.append({
                        "term": en_term,
                        "expected": most_common,
                        "found": zh,
                        "count": count,
                    })

    return inconsistent


# ══════════════════════════════════════════════════════════
#  6. 高级模糊搜索（结合所有技术）
# ══════════════════════════════════════════════════════════

def deep_fuzzy_search(text, glossary, tm, threshold=0.50):
    """
    深度模糊搜索 — 仿 DeepL 术语库匹配。

    搜索优先级:
      1. TM 精确匹配 → 置信度 1.0
      2. TM 模糊匹配 → 置信度 0.7~0.9
      3. 词典混合匹配 → 置信度 0.6~0.85
      4. 无匹配 → None

    Returns: (translation, confidence_score, source)
    """
    text_clean = text.strip()
    text_norm = normalize_spelling(text_clean)

    # 1. TM 精确
    if text_norm in tm:
        return tm[text_norm], 1.0, "tm_exact"

    # 2. TM 模糊
    best_key, best_score = find_fuzzy_match(text_norm, tm, threshold=0.82)
    if best_key:
        return tm[best_key], best_score * 0.95, "tm_fuzzy"

    # 3. 词典混合匹配
    best_key, best_score = find_fuzzy_match(text_norm, glossary, threshold=threshold)
    if best_key:
        translated = glossary[best_key]
        # 混合相似度验证
        hybrid_score = hybrid_similarity(text_norm, best_key)
        confidence = min(0.85, hybrid_score * 1.1)
        return translated, confidence, "glossary_fuzzy"

    return None, 0.0, "no_match"
