"""Gold standard evaluation set for hybrid retrieval.

30 questions with known-correct record indices.
Mix of: direct (uses record terms) and colloquial (paraphrased, no exact terms).
"""

# Each entry: question text, list of correct record indices, question type
GOLDEN_SET = [
    # ── 直接提问 (20题): 使用记录中的关键词 ──
    {"q": "方钠石晶体的零热膨胀特性", "correct": [10708], "type": "direct"},
    {"q": "压缩空气储能结合飞轮的项目", "correct": [54766], "type": "direct"},
    {"q": "宁德时代可持续电池联盟", "correct": [3557], "type": "direct"},
    {"q": "压电COF促进固态电池锂离子传输", "correct": [27557], "type": "direct"},
    {"q": "tRNA启发固体电解质界面层", "correct": [27217], "type": "direct"},
    {"q": "光无线量子密钥分发与Li-Fi", "correct": [18027], "type": "direct"},
    {"q": "嵌合抗原受体星形胶质细胞治疗阿尔茨海默病", "correct": [35522], "type": "direct"},
    {"q": "反铁磁体皮秒超低功耗开关", "correct": [21417], "type": "direct"},
    {"q": "亚纳米铜簇CO₂电还原C-C偶联", "correct": [44435], "type": "direct"},
    {"q": "共价有机框架锌电池隔膜", "correct": [38113], "type": "direct"},
    {"q": "DSS-RFS压裂裂缝动态监测", "correct": [56793], "type": "direct"},
    {"q": "STL投资1亿美元美国供应链", "correct": [17203], "type": "direct"},
    {"q": "安时级锌离子电池钒正极", "correct": [10737], "type": "direct"},
    {"q": "工业AI具身智能量产", "correct": [58178], "type": "direct"},
    {"q": "英伟达开放安全AI联盟NOOA", "correct": [59613], "type": "direct"},
    {"q": "无人机空中实体智能自主规划", "correct": [638], "type": "direct"},
    {"q": "Cursor开发AI Agent编程工具", "correct": [582], "type": "direct"},
    {"q": "CuO异质结构光催化分解水效率", "correct": [11832], "type": "direct"},
    {"q": "河北省地热供暖项目地热井", "correct": [6139], "type": "direct"},
    {"q": "物理过程与区块链加密链接记录设备", "correct": [32007], "type": "direct"},

    # ── 口语化/转述提问 (10题): 刻意避开记录中的原词 ──
    {"q": "有没有不用膨胀的特殊晶体材料，用在精密仪器上的", "correct": [10708], "type": "colloquial"},
    {"q": "储能电站除了电池还有什么技术路线", "correct": [54766], "type": "colloquial"},
    {"q": "电池回收和绿色供应链有什么新进展", "correct": [3557], "type": "colloquial"},
    {"q": "固态电池离子传导太慢怎么解决", "correct": [27557, 27217], "type": "colloquial"},
    {"q": "不可伪造的数据采集技术，防止造假", "correct": [32007], "type": "colloquial"},
    {"q": "治老年痴呆的新方法，不用传统抗体", "correct": [35522], "type": "colloquial"},
    {"q": "超快又省电的电子开关器件", "correct": [21417], "type": "colloquial"},
    {"q": "把二氧化碳变成有用化学品的新型催化剂", "correct": [44435], "type": "colloquial"},
    {"q": "工厂里的智能机器人最近有什么趋势", "correct": [58178], "type": "colloquial"},
    {"q": "石油开采怎么监测压裂效果", "correct": [56793], "type": "colloquial"},
]

if __name__ == "__main__":
    print(f"金标准集: {len(GOLDEN_SET)} 题")
    direct = [e for e in GOLDEN_SET if e["type"] == "direct"]
    colloquial = [e for e in GOLDEN_SET if e["type"] == "colloquial"]
    print(f"  直接提问: {len(direct)} 题")
    print(f"  口语化提问: {len(colloquial)} 题")
    print(f"  总相关记录: {sum(len(e['correct']) for e in GOLDEN_SET)} 条")
