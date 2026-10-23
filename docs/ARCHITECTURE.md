# 架构设计

> codegraph — AST 驱动的本地 LLM 代码智能引擎
> 本文描述的是代码的实际结构，所有论断可在 `src/codegraph/` 中验证。

---

## 1. 分层设计

```
┌──────────────────────────────────────────────────────────────┐
│ 接口层      cli.py (Typer)          api.py (FastAPI)          │
│             static/{index.html,app.css,app.js}               │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│ 编排层      pipeline.py                                      │
│             summarize / analyze_architecture / generate_guide │
│             / review + 缓存 + token 记账                      │
│             reports.py — 从 SQL 聚合生成 Markdown             │
└──────────┬───────────────────────────────┬───────────────────┘
           │                               │
┌──────────▼──────────────┐   ┌────────────▼──────────────────┐
│ 检索层   query.py        │   │ 上下文层 prompts.py            │
│ search/callers/callees   │   │ build_summary_prompt          │
│ call_chain/dep_closure   │   │ build_architecture_prompt     │
│ entry_points             │   │ build_guide_prompt            │
└──────────┬───────────────┘   └────────────┬──────────────────┘
           │                               │
┌──────────▼───────────────────────────────▼──────────────────┐
│ 图构建层  graph.py                                            │
│ scan_repo → parse → 符号入库 → 6 级边解析 → SQLite           │
└──────────┬──────────────────────────────────────────────────┘
           │
┌──────────▼──────────────┐   ┌────────────────────────────────┐
│ 解析层   parser.py       │   │ LLM 层 llm.py                  │
│ Tree-sitter visitor      │   │ OllamaClient.generate          │
│ python/rust/ts/tsx/js    │   │ token 记账（server/estimate）  │
└──────────┬──────────────┘   └────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────┐
│ 存储层  db.py — SQLite schema / FTS5 / 触发器                 │
│ config.py — 路径、配置、prompt 模板版本                       │
└─────────────────────────────────────────────────────────────┘
```

### 各层职责

| 模块 | 职责 | 关键函数 |
|---|---|---|
| `config.py` | 路径常量、LLM/索引配置、prompt 模板版本号 | `LLMConfig`, `IndexConfig`, `ensure_dirs` |
| `db.py` | SQLite schema、FTS5 索引、触发器、连接管理 | `connect`, `init_db`, `SCHEMA_SQL` |
| `parser.py` | Tree-sitter AST → `ParsedFile`（符号/调用/导入/继承） | `parse_source`, `get_language`, `_collect_calls` |
| `graph.py` | 扫描仓库、写图、6 级边解析、增量索引 | `GraphBuilder.index`, `_resolve_call`, `retarget_repo` |
| `query.py` | 符号搜索、callers/callees、调用链、依赖闭包、入口点 | `QueryEngine.search`, `call_chain`, `dependency_closure` |
| `prompts.py` | AST-grounded prompt 构造 + 字符预算 | `build_summary_prompt`, `build_architecture_prompt` |
| `llm.py` | Ollama HTTP 客户端 + token 记账 | `OllamaClient.generate`, `estimate_tokens` |
| `pipeline.py` | 四个 LLM 阶段的编排、缓存、账本、`_fit_arch_prompt` | `Pipeline.summarize`, `analyze_architecture` |
| `reports.py` | 从 SQLite 聚合生成 Markdown 报告 | `write_index_stats`, `write_full_analysis` |
| `cli.py` | 13 个 Typer 命令 | `index`, `search`, `trace`, `summary`, `report`, `serve` |
| `api.py` | 13 个 REST 端点 + 静态看板 | `stats`, `search`, `resolution`, `analyze` |

---

## 2. 为什么手写 AST visitor 而不用 Tree-sitter Query DSL

Tree-sitter 提供了 `Query` / `QueryCursor` 的模式匹配 DSL，对"找出所有函数定义"这类
扁平任务很方便。但本项目需要的信息是**带作用域语义的**，Query 在这个维度上表达力不足：

### 2.1 需要追踪父作用域

调用 `foo()` 究竟属于哪个符号？必须先知道它被哪个函数/方法/类包裹。Query 的 capture
不携带"我位于哪个祖先节点内"的信息，需要在 Python 侧再做一次祖先回溯，反而更绕。

本项目在 `parser.py::_collect_calls` 里用**字节区间包含关系**解决：

```python
scopes = [(s.start_byte, s.end_byte, s.qualname) for s in pf.symbols]
def enclosing(byte):
    # 取包含该字节且区间最小的符号 = 最内层作用域
    return min((q for s, e, q in scopes if s <= byte < e), key=...)
```

> 早期版本这里取了"区间最大"的符号，导致所有调用都归属到模块级，是解析率虚低的根因之一。

### 2.2 需要按语言差异化处理

| 语言 | 特殊处理 |
|---|---|
| Python | `decorated_definition` 需要 unwrap 才能拿到真正的 `function_definition`；docstring 在 body 第一个 expression_statement 里 |
| Rust | `impl Foo` 是**容器**而非符号，必须用 `impl Foo` 作 qualname 前缀，否则与 `struct Foo` 冲突；默认可见性是 private（只有 `pub` 才 public） |
| Rust | `Self::new()` / `Foo::bar()` / `e.run()` 三种 receiver 形态解析规则不同 |
| TS/JS | `const f = () => {}` 需要从 `variable_declarator` 里提取；`extends`/`implements` 在 `heritage` 节点里 |
| 全部 | 泛型参数 `Engine<'a>` 需要剥离才能匹配到 `Engine` |

用 Query DSL 要为每种语言写一套 query 字符串，还要在 Python 侧补语义——代码量不比
visitor 少，且调试时看不到中间状态。visitor 是显式递归，可以在任意节点打断点。

### 2.3 代价

visitor 需要为每种语言写一个 `_parse_xxx` 函数（约 200 行/语言）。这是可接受的，
因为新增语言就是新增一个函数，且结构清晰。

---

## 3. 6 级边解析策略

`graph.py::_resolve_call` 按优先级依次尝试，返回 `(dst_id, resolved, strategy)`。
`strategy` 写入 `edges.via` 字段，使解析率可按证据类型拆解。

| 优先级 | 策略 | 匹配条件 | ripgrep 命中 |
|---:|---|---|---:|
| 1 | `scope` | 调用者的父作用域 + 方法名 = 已存在 qualname（`self.foo()` → `Class.foo`） | 2,613 |
| 2 | `receiver` | receiver 是已知类型名或 `self`/`Self`/`this`/`cls`，拼接后命中 | 397 |
| 3 | `scope` | 裸名 `name` 直接是文件内 qualname | （并入上行） |
| 4 | `file-unique` | 同文件内该名字的符号唯一 | 1,491 |
| 5 | `import` | 文件 import 了该名字，且能映射到目标文件中的同名符号 | 少量 |
| 6 | `global-unique` | 全仓库该名字唯一 | 2,910 |
| 7 | `callable-unique` | 在 function/method 中唯一（排除同名 struct/enum 构造） | 22 |
| 8 | `dir-unique` | 同目录内唯一 | 398 |
| — | 未解析 | 以上都不满足 → `resolved=0`，保留 `dst_name` 文本 | 9,504 |

### 为什么保留未解析边

未解析边不删除，而是存 `dst_name` 原文。这样：
- 仍可查询"哪些地方调用了 `assert_eq!`"
- 解析率成为可报告的工程指标，而非隐藏的失败
- 未来若引入类型推断，可从这些边重新解析，无需重新解析源码

### 导入边用 `dst_file_id` 而非 `dst_id`

导入的目标是**文件**不是符号，所以 `edges` 表有独立的 `dst_file_id` 列（指向 `files`）。
这避免了把文件 id 塞进符号外键导致的 `FOREIGN KEY constraint failed`。

---

## 4. SQLite schema 职责

| 表 | 职责 | 关键约束 |
|---|---|---|
| `meta` | schema 版本等键值对 | PK(key) |
| `repos` | 已索引仓库，`root_path` 唯一 | UNIQUE(root_path) |
| `files` | 每个源文件的路径、语言、内容哈希、行数、解析状态 | UNIQUE(repo_id, path) |
| `symbols` | 符号：name/qualname/kind/起止行/字节/签名/docstring/可见性/复杂度 | UNIQUE(repo_id, file_id, qualname, start_line) |
| `edges` | 边：src_id / dst_id / dst_file_id / kind / dst_name / line / resolved / **via** | UNIQUE(repo_id, src_id, dst_id, dst_file_id, kind, dst_name, line) |
| `symbols_fts` | FTS5 外部内容索引（name/qualname/signature/docstring） | 由触发器同步 |
| `file_summaries` | 摘要缓存，键 = file_id + content_hash + prompt_version + model | UNIQUE 四元组 |
| `llm_calls` | **token 账本**，每次调用一行（含缓存命中行） | 索引 (run_id, phase) |
| `runs` | 一次分析运行（full/incremental/arch/guide/review）的汇总 | PK(run_id) |
| `arch_artifacts` | 生成的架构分析 / 指南 / 审查内容 | — |

### 级联删除

`files` 被删时，`symbols`（`ON DELETE CASCADE`）→ `edges`（CASCADE）→ `file_summaries`（CASCADE）
全部级联清理。这是增量索引"删除文件"路径正确性的基础。

### FTS5 同步

用三个触发器（`symbols_ai` / `symbols_ad` / `symbols_au`）维护外部内容索引。
外部内容模式（`content='symbols'`）不复制数据，节省空间；但 `UPDATE` 必须先
`INSERT ... VALUES('delete', ...)` 再插入新行，否则索引会残留旧 token。

---

## 5. token 账本数据模型

`llm_calls` 是项目所有 token 数字的**唯一来源**。字段语义：

| 字段 | 含义 |
|---|---|
| `run_id` | 属于哪次运行（关联 `runs`） |
| `phase` | `summary` / `arch` / `guide` / `review` |
| `file_path` | 该调用针对的文件（repo-wide 阶段为 NULL 或 entry 文件） |
| `model` | 模型名（缓存键的一部分） |
| `prompt_version` | prompt 模板版本（缓存键的一部分） |
| `prompt_chars` | prompt 字符数（用于估算与审计） |
| `input_tokens` | Ollama `prompt_eval_count` |
| `output_tokens` | Ollama `eval_count` |
| `token_source` | `server`（服务端返回）或 `estimate`（兜底估算） |
| `latency_ms` | 调用耗时 |
| `cache_hit` | 1 表示缓存命中，**没有发生 HTTP 调用** |
| `avoided_input_tokens` / `avoided_output_tokens` | 缓存命中时，若重新调用将产生的 token（取该文件历史真实值） |
| `error` | 调用失败信息（失败调用也记账，但不计入消耗） |

### 缓存键设计

```
(file_id, content_hash, prompt_version, model)
```

四个维度缺一不可：
- `file_id`：哪个文件
- `content_hash`：文件内容变了要重算
- `prompt_version`：改了 prompt 模板，旧缓存必须失效（否则报告数字是假的）
- `model`：换模型后旧摘要不可复用

---

## 6. 架构/指南 prompt 的上下文预算

架构分析是**单次 repo-wide 调用**，prompt 天然很大。8GB 显存下 16k context 是实际上限
（模型 4.7GB + KV cache ~0.9GB）。因此：

```python
ARCH_MAX_CTX = 16_384
ARCH_PROMPT_BUDGET_CHARS = int((ARCH_MAX_CTX - 1_600) * 3.5)  # 51,744，预留输出

def _arch_num_ctx(prompt):
    need = int(len(prompt) / 3.5 * 1.15) + 512
    rounded = max(8192, -(-need // 2048) * 2048)
    return min(rounded, ARCH_MAX_CTX)          # ← 硬 clamp

def _fit_arch_prompt(build, summaries, **kw):
    items = list(summaries.items())
    prompt = build(items, **kw)
    while len(prompt) > ARCH_PROMPT_BUDGET_CHARS and len(items) > 10:
        items = items[:max(10, int(len(items) * 0.9))]   # 每轮裁掉尾部 10%
        prompt = build(items, **kw)
    return prompt
```

`_fit_arch_prompt` 裁掉的是**排在尾部的低价值摘要**（摘要按 entry 文件优先、再按
fan-in 排序），永远不裁指令部分。

> 这两个函数曾经是脱节的：`_arch_num_ctx` 没有 clamp，`_fit_arch_prompt` 写了却
> 没有任何调用点，导致长 prompt 会让 num_ctx 涨到 18432 而 OOM。已修复并有回归测试
> 锚定（`test_arch_num_ctx_stays_within_ceiling_budget`、`test_fit_arch_prompt_trims_to_budget`）。

---

## 7. 增量索引的正确性要点

1. **内容哈希判定变更**：只有 `content_hash` 不同的文件才重新解析。
2. **边重解析必须双向**：变更文件的**出边**要重建，指向变更文件的**入边**也要重建——
   否则 A→B 中 B 变了而 A 没变时，这条边会消失。这是开发中发现并修复的真实 bug。
3. **副本仓库要重定向**：把仓库连同 `.codegraph` 复制到新路径时，DB 里的 `abs_path`
   仍指向旧路径，会导致 `ensure_repo` 创建第二个 repo 行、缓存全部失效。
   `GraphBuilder.retarget_repo()` 负责改写 `repos.root_path` 和所有 `files.abs_path`。
4. **报告路径按仓库隔离**：`reports.report_path(name, repo)` 生成
   `reports/<name>_<repo-slug>.md`，避免多仓库互相覆盖。
