# tech-db 完整工作逻辑文档
## （供其他Agent复刻参考）

---

## 一、系统架构

静态站点，无后端。数据从CSV → JSON → lite分片 → 浏览器加载。

```
源头仓库(CSV) → 后端JSON → lite数据(JSON+JS分片) → 前端(index.html+app.js+styles.css)
```

- GitHub Pages从main分支部署
- 单分支main，不使用PR
- file://双击index-local.html也能工作

---

## 二、数据源

三个GitHub仓库提供CSV数据：

| 仓库 | 内容 | 频率 |
|------|------|------|
| sbq9712/news-spider | 新闻资讯 | 每日 |
| sbq9712/literature-rss-spider | 学术文献RSS | 每日 |
| wodewoping-png/wechat-daily-news-csv | 微信公众号 | 每日 |

---

## 三、数据字段（lite格式，短键）

| 短键 | 全名 | 说明 |
|------|------|------|
| t | title | 标题 |
| b | body | 正文摘要（前500字） |
| d | date | 日期 YYYY-MM-DD |
| i | type | 类型: l=文献, 其他=新闻 |
| u | url | 原文链接 |
| c | category | 分类路径（如 AI与智能科技-AI硬件层-芯片） |
| a | authors | 发布者名称 |
| tg | tag | 标签: 技术突破/产业进展/政策监管/行业观察/研究论文/观点评论 |
| tp | topic | 主题 |
| kp | key_params | 关键参数数组 |
| lv | level | 手动等级: 1=精选, 2=重点, 3=预警 |
| dp | is_dup | 去重标记: 1=重复 |
| fb | full_body | 完整正文 |
| cm | comment | 人工评论 |
| as | ai_summary | AI摘要（100-200字中文） |
| sc | score | AI评分（0-10） |
| scd | score_dims | 五维度分: {b,i,r,d,t} |
| aip | ai_picked | AI精选标记: 1=入选 |
| cl | cluster_id | 聚类ID |
| cln | cluster_name | 聚类名称 |
| cp | cluster_parent | 聚类标记: 0=父条(显示), 1=子条(折叠) |

---

## 四、完整Pipeline（7步）

### Step 1: 数据导入
- 从三个源头仓库下载CSV
- 转为统一JSON格式，存入 data/processed/records-YYYY-MM.json
- 字段映射: source=CSV文件名(隐藏), authors=发布者名(展示)

### Step 2: 去重
- 判定标准: 标题归一化后完全相同 OR 正文前80字符完全相同
- dp=0=保留, dp=1=重复
- 重复记录保留在数据中（不删除），前端"信息筛选"排除dp+不相关
- **重要: lv>0的记录如果被标为dp=1，优先保留它，将另一条标为dp=1**

### Step 3: 分类（GLM-5.2）
- 对每条记录调GLM进行语义分类
- 输出: 大类(零碳产业/AI与智能科技/通用技术/不相关) + 子类路径 + 标签
- 严禁关键词匹配，必须用LLM语义理解
- "不相关"定义: 人物传记/纪念文、每日简报聚合链接、政治宣传/品牌营销/会议征稿、更正勘误

### Step 4: AI摘要（GLM-5.2）
- 对全部记录（含不相关）生成100-200字中文摘要
- 提炼核心技术内容、关键数据指标、主要结论
- 正文为空时基于标题+领域知识生成
- **重要: 有正文但暂未生成摘要的记录标记为pending，前端显示"AI摘要生成中…"，不可留空**
- 脚本: gen_summaries.py, batch=20, 6线程, 每30秒保存

### Step 5: 评分（GLM-5.2）
- 对非"不相关"记录调GLM输出五维度分（0-10）
- 维度:
  - b(突破性): 纯政策/市场=0, 渐进改进=5, 新机理/新材料=10
  - i(产业力): 实验室概念=1, 小规模验证=5, 量产落地=10
  - r(稀缺性): 转载旧闻=0, 常规跟踪=5, 独家首发=10
  - d(数据量): 纯定性=0, 定性+参数=5, 多个硬数据=10
  - t(时效性): 趋势综述=2, 近期进展=6, 突发事件=10

### Step 6: 评分公式（代码侧，不调LLM）

```
权重: b=0.15, i=0.20, r=0.25, d=0.10, t=0.30

基础分 = b×0.15 + i×0.20 + r×0.25 + d×0.10 + t×0.30

加分项:
  t≥8 → +0.3
  t≥7 → +0.15
  tag=政策监管/行业观察 → +0.5
  b≥7 → +0.4（峰值加分）
  r≥7 → +0.3
  i≥7 → +0.3
  文献类(literature) → −0.4（文献惩罚）

最终分 = round(基础分 + 所有加分, 1)

入选规则（满足任一即为AI精选 aip=1）:
  ① 最终分 ≥ 领域阈值
     - 零碳产业 ≥ 6.3
     - AI与智能科技 ≥ 6.5
     - 通用技术 ≥ 6.8
  ② 任一维度 ≥ 8（新闻）或 ≥ 9（文献）→ 自动入选
  ③ tag=技术突破 且 b≥6.5 且 最终分≥5.5（仅新闻）→ 自动入选
```

当前效果: AI精选 ~6,500条, 对手动精选召回率 ~76%

### Step 7: 聚类

#### 去重与聚类的关系（重要!）
- **去重(dp)**: 完全相同内容 → dp=1，从聚类中排除
- **聚类(cl/cp)**: 语义相似但不完全相同 → 归入同一事件簇
- **两者独立运行，但聚类前必须先完成去重**
- **dp=1的记录不参与聚类（清除cl/cp/cln）**

#### 聚类规则
- 同一日期范围内，embedding相似度 >0.85 → 归为同一事件簇
- 每簇选一条作为父条(cp=0)，其余为子条(cp=1)
- **重要: lv>0的记录永远标为cp=0（父条），不可被标为聚类子条**
- 父条缺失时，选子条中lv最高/分数最高的升为父条
- **评分继承: cp=1子条若无评分，继承同簇cp=0父条的sc/scd/aip**

#### 前端聚类交互
- 父条标题右侧显示"展开事件聚类 · N"badge
- 点击展开: 用DOM操作在父条后面插入子条卡片（不重新渲染整个列表）
- 子条卡片样式: 紧凑（仅标题+日期+来源），有左边框缩进
- 点击收起: 移除子条DOM
- **不调用renderRecords()，避免滚动位置丢失**

---

## 五、前端架构

### 文件结构
- index.html: GitHub Pages用（?v=NN缓存破坏）
- index-local.html: 本地file://用
- app.js: 主逻辑
- styles.css: 样式
- data/processed/lite-part-0.js ~ lite-part-17.js: 数据分片（3000条/个）
- data/processed/all-records-lite.json: 完整数据（仅开发用）
- data/category-order-data.js: 分类排序
- data/processed/manifest-data.js: 元数据

### 关键约束
1. index.html和index-local.html必须同步
2. __LITE_DATA__扁平化那行必须在所有lite-part-N.js之后
3. 所有addEventListener必须用null-guard: `on(id, evt, fn)`
4. 默认主题: light（强制，不读取localStorage）
5. 默认筛选: alertLevel='curated'（精选情报）
6. 搜索输入: 200ms防抖
7. alert level计数: 单遍历（不重复.filter()）
8. cluster childCount: 启动时预计算Map
9. PAGE_SIZE=50

### 统计面板（ALERT_LEVELS）
6行，从上到下:
1. 信息爬取: 全量（含重复）
2. 信息筛选: 去除不相关+重复
3. AI精选: aip=1
4. 精选情报: lv≥1（手动）
5. 重点情报: lv≥2
6. 预警情报: lv≥3

---

## 六、GLM调用规范

```
命令: hermes -z "prompt" --provider zai -m glm-5.2 --cli
并发: ThreadPoolExecutor, MAX_WORKERS=6
Batch: 分类/评分=10条/批, 摘要=20条/批, 重试=5条/批
Timeout: 180s（重试240s）
保存: 每30秒写一次输出文件
```

---

## 七、数据完整性校验

脚本: scripts/check_integrity.py

检查项:
1. 有正文但无AI摘要的记录数（应为0，否则标记pending）
2. 非不相关记录中缺评分的数量（应为0）
3. 未分类记录数
4. lv>0但cp=1的记录（必须为0）

---

## 八、脚本索引

| 脚本 | 用途 |
|------|------|
| classify_score_new.py | 分类+评分（新记录） |
| retry_classify_score.py | 补跑失败的分类+评分 |
| gen_summaries.py | 生成AI摘要 |
| final_summaries.py | 补跑缺失摘要（小batch） |
| rescore_all.py | 全量重新评分（公式变更时） |
| merge_dedup_cluster.py | 去重+聚类合并（dp=1移出聚类） |
| fix_cluster_parents.py | 修复无父条的聚类 |
| scripts/check_integrity.py | 数据完整性校验 |

---

## 九、Git规范

- 单分支main
- 推送: git push https://sbq9712:${GH_TOKEN}@github.com/sbq9712/tech-db.git main
- 不硬编码token，用GH_TOKEN环境变量
- push后等30-90秒GitHub Pages部署，curl确认线上版本
