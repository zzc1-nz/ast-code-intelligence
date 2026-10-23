# codegraph — 阶段 0 验收报告

> **这是阶段 0（环境与最小索引）的历史快照，数字反映当时的代码状态。**
> 项目最终状态请看 [`FINAL_REPORT.md`](FINAL_REPORT.md)：
> 测试从当时的 19 个增至 **45 个**，索引能力扩展到 3 个仓库 / 786 文件。
> 本文件保留是为了展示"每阶段暂停验收"的过程，其中数字不再更新。

> 生成时间：2026-10-23 · 环境：Windows 11 / Python 3.11.9 / uv 0.13.0 / RTX 4060 Laptop 8GB

## 1. 环境搭建

| 项目 | 结果 |
|---|---|
| Python 环境 | `uv venv --python 3.11 D:\codegraph\.venv` → CPython 3.11.9 |
| 依赖安装 | `uv pip install` 成功（tree-sitter 0.26.0 等 37 个包） |
| 包安装 | `uv pip install -e D:\codegraph` → `codegraph==0.1.0` |
| CLI 入口 | `D:\codegraph\.venv\Scripts\codegraph.exe` |

### Tree-sitter 语言绑定（Windows 预编译 wheel，全部可用）

| 语言 | 包 | 版本 | 状态 |
|---|---|---|---|
| Python | tree-sitter-python | 0.25.0 | OK |
| Rust | tree-sitter-rust | 0.24.2 | OK |
| JavaScript | tree-sitter-javascript | 0.25.0 | OK |
| TypeScript | tree-sitter-typescript | 0.23.2 | OK |
| TSX | tree-sitter-typescript | 0.23.2 | OK |

无编译失败，全部为预编译 wheel。

## 2. 最小索引验证（项目自身源码）

```
codegraph index D:\codegraph --full
```

| 指标 | 实测值 |
|---|---:|
| 文件数 | 19 |
| 符号数 | 210 |
| 调用边 | 2,452 |
| 导入边 | 128 |
| 继承边 | 6 |
| 边总数 | 2,586 |
| 解析错误 | 0 |
| 索引耗时 | 0.18 s |
| 符号构成 | function:128, method:58, class:24 |

## 3. SQLite 可直接查询

`sqlite3` 直接打开 `D:\codegraph\.codegraph\graph.db`：

```
tables: arch_artifacts, edges, file_summaries, files, llm_calls, meta,
        repos, runs, symbols, symbols_fts (+ FTS5 shadow tables)
files: 19    symbols: 210    edges: 2,586
```

FTS5 符号搜索验证：

```sql
SELECT name, qualname FROM symbols_fts WHERE symbols_fts MATCH 'prompt*' LIMIT 5;
-- summary | summary
-- generate | OllamaClient.generate
-- _log_call | Pipeline._log_call
-- FileContext | FileContext
-- build_summary_prompt | build_summary_prompt
```

## 4. 多语言解析能力（阶段 1 提前验证）

已在 ripgrep（Rust, 110 文件）与 requests（Python, 37 文件）上跑通：

| 仓库 | 文件 | 符号 | 边 | 解析错误 | 耗时 |
|---|---:|---:|---:|---:|---:|
| ripgrep | 110 | 4,112 | 17,468 | 0 | 2.6 s |
| requests | 37 | 803 | 4,170 | 0 | 0.33 s |

## 5. 测试

```
.venv\Scripts\python.exe -m pytest tests -q
19 passed
```

覆盖：Python/Rust/TS 符号提取、调用作用域、继承边、图构建 + FTS 搜索、
callers/callees、多跳调用链、依赖闭包、增量重建、JSON 抽取、prompt 预算。

## 6. 产出文件

| 路径 | 说明 |
|---|---|
| `src/codegraph/{config,db,parser,graph,query,prompts,llm,pipeline,reports,cli,api}.py` | 引擎源码 |
| `tests/test_core.py`, `tests/test_pipeline.py` | 19 个测试（阶段 0 时；最终为 45 个） |
| `scripts/{run_stage,simulate_incremental,smoke_test}.py` | 阶段驱动脚本 |
| `reports/index_stats.md` | 索引统计报告（自动生成） |
| `.codegraph/graph.db` | SQLite 知识图 |

## 7. 阶段 0 结论

- ✅ CLI 可输出文件数 / 符号数 / 调用边数
- ✅ SQLite 文件可被直接打开查询，FTS5 可用
- ✅ 四种语言绑定在 Windows 上全部可用（预编译 wheel，零编译）
- ⏳ Ollama 与 qwen2.5-coder:7b 正在准备（见阶段 1 报告）
