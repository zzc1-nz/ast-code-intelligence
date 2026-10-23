# Token 埋点方法论

> 本文说明 codegraph 如何测量 LLM token 消耗，以及每个报告数字的口径。
> 核心原则：**能实测就实测，不能实测就明确标注为估算，绝不混用。**

---

## 1. 为什么用服务端计数而非字符估算

Ollama 的 `/api/generate` 响应包含两个字段：

| 字段 | 含义 |
|---|---|
| `prompt_eval_count` | 输入 prompt 的 token 数（模型实际分词结果） |
| `eval_count` | 生成输出的 token 数 |

`llm.py::OllamaClient.generate` 直接读取它们：

```python
in_tok = data.get("prompt_eval_count")
out_tok = data.get("eval_count")
if in_tok is None or out_tok is None:
    source = "estimate"
    in_tok = estimate_tokens(prompt) + (estimate_tokens(system) if system else 0)
    out_tok = estimate_tokens(text)
else:
    source = "server"
```

### 字符估算为什么不可靠

`estimate_tokens()` 用 `ceil(chars / 3.5)`。这个比例对英文散文大致成立，但：

- **代码**：`fn(&self) -> Result<T>` 里标点和符号密度高，实际 token 比字符数比值更高
- **中文**：一个汉字常占 1–2 个 token，比例完全不同于英文
- **JSON**：`{"a": 1}` 的括号引号各占 token，比例更高

本项目在 ripgrep 上实测：单文件 prompt 字符数 17,500，服务端返回 input token 约 5,000，
比例 3.5 恰好接近；但架构 prompt 48,753 字符对应 9,733 token，比例 5.0——**同一比例在
不同内容上误差可达 40%**。因此估算只能作兜底。

### 实测结果：本项目 0 次估算

| 项 | 值 |
|---|---:|
| 真实 LLM 调用总数 | 156 |
| `token_source='server'` | **156（100%）** |
| `token_source='estimate'` | **0** |

即所有报告数字都来自模型实际分词，没有一个字是估出来的。

---

## 2. 缓存命中时 avoided_tokens 的口径

缓存命中时**没有发生 HTTP 调用**，所以没有真实的 `prompt_eval_count`。此时"省了多少
token"不能凭空造，本项目采用**历史真实值**：

```python
def _cached_call_tokens(self, rel_path: str) -> tuple[int, int]:
    row = self.conn.execute(
        """SELECT input_tokens, output_tokens FROM llm_calls
           WHERE repo_id=? AND file_path=? AND phase='summary' AND cache_hit=0
           AND error IS NULL ORDER BY id DESC LIMIT 1""",
        (self.repo_id, rel_path),
    ).fetchone()
    return (row["input_tokens"], row["output_tokens"]) if row else (0, 0)
```

即：取该文件**上一次真实调用**消耗的 token 数，作为本次"若重算将产生"的估计，写入
`llm_calls.avoided_input_tokens` / `avoided_output_tokens`，同时该行 `cache_hit=1`。

### 为什么不是"当前 prompt 长度估算"

因为 prompt 内容会随邻居摘要更新而变化，用当前长度估算会引入不可复现的漂移。用历史
真实值意味着：**缓存节省比例完全可以从数据库重算，且结果稳定**。

### 节省比例公式

```
would_have_cost = 实际消耗 + avoided 总和
saved_ratio     = avoided 总和 / would_have_cost
```

ripgrep 实测：

| 项 | 值 |
|---|---:|
| 实际消耗（115 次真实调用） | 360,370 |
| avoided（98 次缓存命中） | 312,342 |
| 若全部重算 | 672,712 |
| **节省比例** | **312,342 / 672,712 = 46.4%** |

> 注意：这里的 46.4% 是"摘要阶段累计视角"。若只看**第二次全量重跑**（93 文件命中缓存），
> 命中率是 84.55%，本次仅消耗 48,028 tokens。两个数字口径不同，报告中分别标注。

---

## 3. P95 的计算方法

`pipeline.py::token_report` 用 **nearest-rank** 法：

```python
totals_sorted = sorted(input_tokens + output_tokens for each real summary call)
n = len(totals_sorted)

def pct(p):
    idx = min(n - 1, max(0, int(round((p / 100.0) * n + 0.5)) - 1))
    return totals_sorted[idx]

p95 = pct(95)
```

即 `sorted[ceil(0.95 * n) - 1]`。样本量 n=115 时，P95 取第 110 个值。

### 实测值

| 仓库 | n | mean | p50 | P95 | max |
|---|---:|---:|---:|---:|---:|
| ripgrep | 115 | 3,134 | 2,956 | **5,812** | 6,021 |
| requests | 37 | 2,405 | 1,895 | **5,530** | 5,586 |

P95 显著高于均值，说明 token 分布右偏——少数大型文件（如 `crates/searcher/src/searcher/mod.rs`
有 74 个符号、1088 行）消耗了不成比例的 token。

> 若改用线性插值，requests 的 P95 为 5,504.4。本项目报告统一用 nearest-rank，因为它是
> 真实存在的样本值，不需要假设分布。

---

## 4. 从 SQLite 复现任何报告数字

所有报告由 `reports.py` 从 SQL 聚合生成，可用 `codegraph report` 重新产出。以下 SQL 可
手工验证报告里的每个数字。

### 4.1 索引统计

```sql
-- 文件 / 符号 / 边
SELECT COUNT(*) FROM files   WHERE repo_id=1;
SELECT COUNT(*) FROM symbols WHERE repo_id=1;
SELECT COUNT(*) FROM edges   WHERE repo_id=1;

-- 按 kind 拆边
SELECT kind, COUNT(*) FROM edges WHERE repo_id=1 GROUP BY kind;

-- 解析率
SELECT resolved, COUNT(*) FROM edges WHERE repo_id=1 AND kind='call' GROUP BY resolved;

-- 解析策略分布（报告中的策略表）
SELECT COALESCE(via,'unresolved') AS strategy, COUNT(*) AS n
FROM edges WHERE repo_id=1 AND kind='call'
GROUP BY via ORDER BY n DESC;
```

### 4.2 全量分析 token

```sql
-- 总消耗（只算真实调用，排除缓存命中行和失败行）
SELECT SUM(input_tokens) AS input,
       SUM(output_tokens) AS output,
       COUNT(*)            AS calls
FROM llm_calls
WHERE repo_id=1 AND phase='summary' AND cache_hit=0 AND error IS NULL;

-- 单文件 token 列表（用于算 mean / P95）
SELECT file_path, input_tokens + output_tokens AS total
FROM llm_calls
WHERE repo_id=1 AND phase='summary' AND cache_hit=0 AND error IS NULL
ORDER BY total;
```

### 4.3 缓存节省

```sql
-- 缓存命中次数与避免量
SELECT COUNT(*)                          AS hits,
       SUM(avoided_input_tokens)         AS avoided_in,
       SUM(avoided_output_tokens)        AS avoided_out
FROM llm_calls
WHERE repo_id=1 AND cache_hit=1;

-- 节省比例（用上面的实际消耗 + avoided 计算）
-- saved_ratio = (avoided_in + avoided_out) / (actual_total + avoided_in + avoided_out)
```

### 4.4 增量分析

```sql
-- 每轮增量 run
SELECT run_id, files_total, files_called, files_cached,
       input_tokens, output_tokens, started_at
FROM runs
WHERE repo_id=1 AND kind='incremental'
ORDER BY started_at;

-- 某轮的调用明细
SELECT file_path, input_tokens, output_tokens, latency_ms
FROM llm_calls WHERE run_id='incremental-20261023-161246-c5598a';
```

### 4.5 架构/指南

```sql
-- 单次平均
SELECT phase, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o
FROM llm_calls
WHERE repo_id=1 AND phase IN ('arch','guide') AND cache_hit=0
GROUP BY phase;

-- 产物
SELECT kind, entry_symbol, LENGTH(content) FROM arch_artifacts WHERE repo_id=1;
```

---

## 5. 口径边界（重要）

| 场景 | 口径 | 说明 |
|---|---|---|
| 全量分析累计 token | 只算 `cache_hit=0 AND error IS NULL` | 失败调用不消耗（或消耗极少），不计入 |
| 第二次全量重跑的缓存命中率 | `files_cached / (files_called + files_cached)` | 93/110 = 84.55% |
| 摘要阶段累计节省比例 | `avoided / (actual + avoided)` | 46.4%，跨全部历史 run |
| 增量 vs 全量节省 | `1 - 增量tokens / 全量基线tokens` | 97.4%，用副本仓库的 5 轮均值 |
| P95 | nearest-rank | 真实样本值，非插值 |
| 架构倍数 | `arch单次均值 / summary单次均值` | 2.6× |

**不同口径的数字不能直接相减或相除。** 报告中每个数字都标注了它属于哪个口径。
