# 验收指南

> 本文档告诉你如何在 5 分钟内验证这个项目是**真的跑通了**，而不只是"看起来写了代码"。

---

## 快速验收（3 分钟，不需要 GPU）

只验证 AST 解析和 SQLite 知识图，**不调用 LLM**。

```powershell
cd D:\codegraph

# 1. 跑测试（应看到 45 passed）
.venv\Scripts\python.exe -m pytest tests -q

# 2. 索引一个仓库（用项目自身源码，零依赖）
.venv\Scripts\codegraph.exe index D:\codegraph --full
```

预期输出：

```
| Files                | 19                                                  |
| Symbols              | 210                                                 |
| Edges (total)        | 2,586                                               |
|   call               | 2,452                                               |
|   import             | 128                                                 |
|   inherit/implements | 6                                                   |
| Resolved edges       | 905 (35.0%)                                         |
| Parse errors         | 0                                                   |
| This run             | 19 parsed, 0 unchanged, 0 removed in 0.18s          |
```

```powershell
# 3. 符号搜索（FTS5）
.venv\Scripts\codegraph.exe search prompt --repo D:\codegraph

# 4. 调用链追踪
.venv\Scripts\codegraph.exe trace build_summary_prompt --repo D:\codegraph --dir both --depth 2
```

**验收点**：能搜到符号、能看到 callers/callees、SQLite 文件可被外部工具打开。

```powershell
# 5. 直接用 Python 打开 SQLite 验证（不依赖本项目代码）
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'D:\codegraph\.codegraph\graph.db'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])"
```

---

## 完整验收（需要 Ollama + 7B 模型）

### 前置条件

| 项 | 要求 |
|---|---|
| GPU | ≥ 6GB 可用显存（本项目实测 RTX 4060 8GB，模型占 5.1GB） |
| Ollama | 已安装并在 `127.0.0.1:11434` 运行 |
| 模型 | `qwen2.5-coder:7b`（Q4_K_M，约 4.4GB） |
| 测试仓库 | 任选一个已 clone 的源码仓库 |

### 步骤

```powershell
# 1. 确认 Ollama 与模型就绪
.venv\Scripts\codegraph.exe doctor

# 2. 索引目标仓库（换成你自己的路径）
.venv\Scripts\codegraph.exe index <你的仓库路径> --full

# 3. 生成 5 个文件的摘要，观察 token 消耗（约 1-2 分钟）
.venv\Scripts\python.exe scripts\run_stage.py summary <你的仓库路径> --limit 5

# 4. 再跑一次，验证缓存命中（应瞬间完成，0 token）
.venv\Scripts\python.exe scripts\run_stage.py summary <你的仓库路径> --limit 5

# 5. 查看 token 账本
.venv\Scripts\codegraph.exe stats --repo <你的仓库路径>
```

**验收点**：
- 第 3 步有真实 token 数字（非 0）
- 第 4 步 `files_cached` = 5、`total_tokens` = 0、`avoided_tokens` > 0
- `stats` 表格里的数字与第 3、4 步一致

```powershell
# 6. 架构分析（单次调用，token 应显著高于单文件摘要）
.venv\Scripts\codegraph.exe analyze --repo <你的仓库路径> --entry main
# 产物：output/architecture.md

# 7. 开发者指南
.venv\Scripts\codegraph.exe guide --repo <你的仓库路径> --entry main
# 产物：output/developer_guide.md
```

**验收点**：架构分析的 input token 应 ≥ 单文件摘要的 2 倍。

---

## 可视化验收

```powershell
.venv\Scripts\codegraph.exe serve --port 8765
```

浏览器打开 <http://127.0.0.1:8765/>，应看到：

| 面板 | 内容 |
|---|---|
| KPI 卡片 | 文件数 / 符号数 / 边数 / DB 大小 / 缓存摘要数 |
| 调用关系图 | 力导向布局，节点按 kind 着色，红色虚线 = 未解析目标 |
| Token 趋势 | 每次 run 的 input/output 双折线 |
| 符号类型环形图 | function/method/class/struct/... 分布 |
| 解析策略条形图 | scope / receiver / global-unique / ... 的边数 |

**验收点**：页面无外部网络请求（F12 → Network，全部同源），图表有数据。

---

## 数字交叉验证

项目所有报告数字都能从 SQLite 复现。挑几个验证：

```powershell
# 验证索引统计
.venv\Scripts\python.exe -c @"
import sqlite3
c = sqlite3.connect(r'D:\codegraph\repos\ripgrep\.codegraph\graph.db')
c.row_factory = sqlite3.Row
rid = c.execute('SELECT id FROM repos LIMIT 1').fetchone()['id']
for q, label in [
    ('SELECT COUNT(*) FROM files WHERE repo_id=?', 'files'),
    ('SELECT COUNT(*) FROM symbols WHERE repo_id=?', 'symbols'),
    ('SELECT COUNT(*) FROM edges WHERE repo_id=?', 'edges'),
]:
    print(label, c.execute(q, (rid,)).fetchone()[0])
"@
```

```powershell
# 验证 token 总账（应等于报告里的 360,370）
.venv\Scripts\python.exe -c @"
import sqlite3
c = sqlite3.connect(r'D:\codegraph\repos\ripgrep\.codegraph\graph.db')
r = c.execute('''SELECT SUM(input_tokens) i, SUM(output_tokens) o, COUNT(*) n
                 FROM llm_calls WHERE phase='summary' AND cache_hit=0 AND error IS NULL''').fetchone()
print(f'{r[2]} calls, input={r[0]:,}, output={r[1]:,}, total={r[0]+r[1]:,}')
"@
```

预期：`115 calls, input=329,238, output=31,132, total=360,370`

---

## 数据可信度自查

这个项目在报告里写了不少"自我批评"，你可以逐条验证：

| 报告里的说法 | 怎么验证 |
|---|---|
| "解析率只有 45.6%" | `SELECT resolved, COUNT(*) FROM edges WHERE kind='call' GROUP BY resolved` |
| "未解析的主要是 stdlib 宏" | `SELECT dst_name, COUNT(*) FROM edges WHERE resolved=0 GROUP BY dst_name ORDER BY 2 DESC LIMIT 15` |
| "架构分析的符号引用全部真实" | 打开 `output/architecture.md`，挑 3 个符号名，用 `codegraph search` 查是否存在、行号是否一致 |
| "开发者指南的代码示例不可编译" | 打开 `output/developer_guide.md` 的 "How to Add a Feature"，会发现代码是模型构造的，未经验证 |
| "156 次调用全部服务端计数" | `SELECT token_source, COUNT(*) FROM llm_calls GROUP BY token_source`（应只有 server） |

---

## 常见问题

**Q: 没有 GPU 能验收吗？**
能。快速验收部分（3 分钟）完全不需要 LLM，AST 解析和 SQLite 是纯 CPU 的。

**Q: 索引报错 "repo not indexed yet"？**
先跑 `codegraph index <路径>`，DB 才会创建。

**Q: 模型加载失败 / OOM？**
检查可用显存是否 ≥ 6GB。本项目在 8GB 卡上模型占 5.1GB，桌面占 1.7GB，余量很小。
关闭浏览器和其他占显存程序。

**Q: 想换小一点的模型？**
`codegraph summary --model qwen2.5-coder:1.5b` 可用，但摘要质量会下降。
模型名是缓存键的一部分，换模型不会误用旧缓存。
