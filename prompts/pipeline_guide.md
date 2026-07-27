# 技术情报处理流程提示词

## 到新电脑后的完整操作流程

### 1. 环境准备

```bash
# 安装 Python 依赖
pip install openpyxl --break-system-packages

# 确认 hermes CLI 可用（用于 GLM 5.2 分类）
hermes --version
```

### 2. 导入新情报（如有 Excel）

```bash
python3 scripts/import_excel.py /path/to/精选重点预警.xlsx
```

### 3. 处理未分类情报

```bash
python3 scripts/process_unclassified.py
```

这个脚本会：
- 找到所有 category="未分类" 的记录
- 调用 GLM 5.2 进行领域分类 + 情报类型 + 标签
- 如果领域分类不是"不相关"，继续提取参数
- 增量保存（每5批一次），防止中断丢数据

### 4. 重建离线版

```bash
python3 scripts/build_local.py
```

### 5. 验证

- 双击 `index-local.html` 确认页面正常
- 检查情报等级筛选器数字是否正确
- 检查领域分类树是否有新增分类

## OpenCode 一键导入提示词

如果使用 OpenCode（而非命令行），粘贴以下提示词：

---

本目录是一个技术情报数据库项目。请完成以下任务：

1. 找到桌面上的 Excel 文件（*.xlsx），用 Python openpyxl 读取，先打印列名和前3行确认结构。

2. 列名应包含：分类（值：精选/重点/预警/重要）、标题、正文、日期、链接地址、来源、作者。

3. 运行 `python3 scripts/import_excel.py <Excel文件路径>` 导入数据。

4. 运行 `python3 scripts/process_unclassified.py` 对新导入的"未分类"记录进行LLM分类。

5. 运行 `python3 scripts/build_local.py` 重建离线版HTML。

6. 打开 index-local.html 验证页面正常显示，确认新数据已分类。

---

## 重要约定

1. 新导入的情报 category 必须是 "未分类"，不是 "不相关"
2. "不相关"仅指经 LLM 分类后判定为不相关的记录
3. 等级标签（精选/重点/预警）与分类无关，只要有 lv 字段就算
4. 预警（lv=3）同时具有重点和精选属性
5. 分类/标签/参数提取只用 GLM 5.2，禁止关键词匹配
