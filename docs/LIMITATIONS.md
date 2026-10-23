# 局限性说明

> 本文如实记录 codegraph 的已知边界。这些不是待办清单，而是**工程结论**——
> 有些是静态分析的固有上限，有些是 8GB 显存下的主动取舍。

---

## 1. 静态调用解析天然不完整

### 实测数据

| 仓库 | 语言 | 调用边 | 已解析 | 解析率 |
|---|---|---:|---:|---:|
| ripgrep | Rust | 16,842 | 7,964 | **45.6%** |
| requests | Python | 3,805 | 1,770 | **42.4%** |
| execa | TS/JS | 22,840 | 8,775 | **35.7%** |

整体解析率（含导入/继承边）：

| 仓库 | 边总数 | 已解析 | 解析率 |
|---|---:|---:|---:|
| ripgrep | 17,468 | 7,964 | 45.6% |
| requests | 4,170 | 1,770 | 42.4% |
| execa | 24,565 | 8,775 | 35.7% |

### 未解析目标是什么

ripgrep 未解析边 Top-15（按出现次数）：

| 目标 | 次数 | 性质 |
|---|---:|---|
| `assert_eq!` | 1,167 | Rust 标准宏 |
| `assert` | 771 | 标准宏 |
| `new` | 544 | 构造器，跨 crate 多义 |
| `Ok` | 540 | 标准库枚举变体 |
| `Some` | 470 | 标准库枚举变体 |
| `path` | 380 | 字段/方法名，跨类型多义 |
| `join` | 327 | 标准库方法 |
| `vec!` | 251 | 标准宏 |
| `push` | 216 | 集合方法 |
| `as_bytes` | 161 | 标准库方法 |
| `map` | 143 | 迭代器方法 |
| `iter` | 132 | 迭代器方法 |
| `to_string` | 121 | 标准库方法 |
| `len` | 119 | 标准库方法 |
| `search_reader` | 115 | 测试辅助，跨 4 个定义 |

**结论：未解析的绝大多数是"仓库内没有定义"的符号**——标准库方法、宏、枚举变体。
它们不是解析器的失败，而是"仓库内不存在定义"这一事实本身。

真正无法静态解析的**动态调用点**（连目标文本都提取不出来）在解析阶段单独计数：

| 仓库 | 动态调用点 | 总调用点 | 占比 |
|---|---:|---:|---:|
| ripgrep | 29 | 17,088 | 0.17% |
| requests | 3 | 2,687 | 0.11% |
| execa | 521 | 33,297 | 1.56% |

### 为什么保留未解析边

未解析边不删除，而是保留 `dst_name` 原文 + `resolved=0`。这样：
- 仍可查询"哪些地方调用了 `assert_eq!`"
- 解析率是可报告的指标，而非隐藏失败
- 未来引入类型推断可从这些边重解析，无需重读源码

---

## 2. 各语言的额外限制

### Rust

- **trait object / 泛型分发**：`dyn Trait` 的方法调用无法确定具体实现
- **宏展开**：`macro_rules!` 生成的代码不在 AST 里
- **跨 crate 同名**：`new` / `default` 这类名字在多个 impl 里存在，无法唯一解析
- **`impl` 块的可见性**：`impl` 本身没有可见性概念，方法继承 `pub` 标记

### Python

- **动态属性**：`getattr(obj, name)()` 完全无法解析
- **猴子补丁**：运行时替换的方法，静态分析看不到
- **装饰器改写**：`@property` / `@staticmethod` / 自定义装饰器会改变调用语义
- **`*args`/`**kwargs` 转发**：调用链在此断裂
- **动态导入**：`importlib.import_module(x)` 无法静态确定模块

### TypeScript / JavaScript

- **原型链**：`obj.__proto__.method` 的解析需要运行时
- **动态属性访问**：`obj[key]()` 无法解析
- **commonjs 与 esm 混用**：`require()` 与 `import` 的模块映射规则不同
- **重导出链**：`export * from './x'` 需要追踪多层才能找到定义

### 语言覆盖

只支持 4 种：Python / Rust / TypeScript / JavaScript（含 TSX）。
不支持 Go、Java、C++、C#、Ruby 等。新增语言需要写一个 `_parse_xxx` visitor。

---

## 3. LLM 生成内容的可信度边界

这是**最重要的一条**，必须明确：

### 架构分析：符号引用已逐条核实

对 `output/architecture.md` 中引用的 8 个符号逐一回查 SQLite：

| 引用 | 是否真实 | 行号是否准确 |
|---|---|---|
| `main` in `build.rs` | ✅ | ✅ L1 |
| `set_git_revision_hash` in `build.rs` | ✅ | ✅ L36 |
| `set_windows_exe_options` in `build.rs` | ✅ | ✅ L12 |
| `TestCommand::output` in `tests/util.rs` | ✅ | ✅ L331 |
| `GramQuery::unwrap` in `crates/index/src/literal.rs` | ✅ | ✅ L57 |
| `strip_jemalloc_nonsense` in `tests/util.rs` | ✅ | ✅ L523 |
| `TestCommand::raw_output` in `tests/util.rs` | ✅ | ✅ L338 |
| `TestCommand::expect_success` in `tests/util.rs` | ✅ | ✅ L410 |

**8/8 全部真实，行号全部准确。** 这是 AST grounding 生效的直接证据——prompt 里给了
精确的 `qualname (path:line)`，模型无法凭空编造。

### 开发者指南：代码示例不可直接使用

`output/developer_guide.md` 的 "How to Add a Feature" 一节给出了 Rust 代码片段，例如：

```rust
pub const CASE_INSENSITIVE: Flag = Flag::new(
    "i", "case-insensitive", "Search case-insensitively",
    Category::Search, FlagValue::Bool(false),
);
```

**这段代码的参数签名未经代码库验证，不能直接编译。** 模型根据 prompt 中的符号名
（`Flag`、`Category`、`FlagValue`）推断出了合理但未验证的 API 形状。

> 这是 7B 模型在"从符号名推断 API"任务上的固有能力边界。架构分析因为只引用
> 已有符号而不受影响；但要求模型"写新代码"就会暴露这个限制。

**缓解措施**：指南的 prompt 明确要求"cite real file paths and symbol names"，
但无法阻止模型为演示目的构造示例代码。使用者应把指南当作**导航**而非**代码参考**。

### 单文件摘要：结构化字段可信度较高

摘要输出为严格 JSON（`responsibility` / `key_functions` / `public_interface` /
`design_notes` / `risk_flags`），且 prompt 中注入了完整符号表。实测 110 个 ripgrep
摘要中，`public_interface` 字段的内容全部来自 prompt 中真实存在的符号。

但 `risk_flags` 字段的"可疑点"是模型判断，可能误报或漏报。

---

## 4. 8GB 显存下的模型选择权衡

### 硬件约束

| 项 | 值 |
|---|---|
| GPU | RTX 4060 Laptop，8188 MiB |
| 桌面常驻占用 | ~1,700 MiB |
| **实际可用** | **~6,200 MiB**（原计划按 6.7GB 规划，实测更紧） |

### 模型选择

| 候选 | 量化 | 大小 | 可行性 |
|---|---|---:|---|
| qwen2.5-coder:7b | Q4_K_M | 4.36 GB | ✅ **采用**，加载后占 5.1 GB |
| qwen2.5-coder:7b | Q5_K_M | 5.44 GB | ⚠️ 临界，KV cache 会溢出 |
| qwen2.5-coder:7b | Q8_0 | 8.10 GB | ❌ 超显存 |
| qwen2.5-coder:14b | Q4_K_M | ~9 GB | ❌ 超显存 |
| qwen2.5-coder:1.5b | Q4_K_M | ~1 GB | ✅ 可行但质量不足 |

**7B Q4_K_M 是唯一能同时满足"装得下"和"分析质量够用"的选择。**

### 由此带来的约束

1. **上下文窗口 16k 上限**：模型 4.7GB + KV cache ~0.9GB ≈ 5.6GB，16k context 是上限。
   架构分析的 prompt 必须裁剪到 51,744 字符以内（`_fit_arch_prompt`）。
2. **串行执行**：索引进程和 LLM 调用不能并行，否则内存/显存双爆。
   实测同时跑会导致可用内存降到 1.1GB、负载 92%。
3. **输出长度受限**：`num_predict` 对摘要是 700，对架构是 1400，对指南是 1800。
   超长输出会被截断，需要 `_repair_truncated_json` 兜底修复 JSON。
4. **推理速度**：实测单文件摘要平均 11.6s，架构分析 40–50s，指南 46–58s。
   110 文件全量摘要耗时约 30 分钟。

### 显存实测

```
模型加载后：nvidia-smi → 6,607 MiB used / 1,350 MiB free
ollama ps  → qwen2.5-coder:7b  5.1 GB  100% GPU  CONTEXT 8192
```

100% 在 GPU 上，没有 CPU offload——这是 7B Q4 在 8GB 卡上的理想状态。

---

## 5. 明确不做的事

按项目定位，以下功能**刻意不实现**：

| 不做 | 原因 |
|---|---|
| **MCP server** | 项目定位是独立引擎，不做 IDE 生态集成 |
| **IDE 插件** | 同上；CLI + REST + Web 已覆盖使用场景 |
| **云同步** | 核心卖点是"完全本地运行" |
| **Neo4j / 独立图数据库** | 嵌入式 SQLite 单文件足够，零部署成本 |
| **Docker** | Windows 原生优先，不引入容器开销 |
| **自己加载模型权重** | 用 Ollama HTTP，避免和推理框架耦合 |
| **多用户 / 权限系统** | 单机工具，无此需求 |

---

## 6. 其他已知问题

| 问题 | 影响 | 状态 |
|---|---|---|
| 装饰器符号的 `start_line` 指向装饰器行而非 `def` 行 | 行号偏差 1–N 行 | 已知行为，未修（可作为"符号起始位置"的合理定义） |
| 架构分析 token 分布不稳定（1,528 → 9,733 input） | 早期版本 prompt 注入不足，修复后稳定 | 已修 |
| 增量分析的"缓存命中率"存在两种口径 | 闭包内命中率 vs 全仓库命中率 | 报告中分别标注 |
| `execa` 仓库无 LLM 摘要数据 | 仅用于验证 TS/JS 解析能力 | 有意为之 |
| 报告文件曾有路径冲突 | 多仓库互相覆盖 | 已修为按仓库名隔离 |
