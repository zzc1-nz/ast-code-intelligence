# Index statistics — `execa`

Generated: 2026-10-23T16:39:19
Repository: `D:\codegraph\repos\execa`

## Scale

| Metric | Value |
|---|---:|
| Files indexed | 639 |
| Symbols extracted | 1,496 |
| Graph edges | 24,565 |
| — call edges | 22,840 |
| — import edges | 1,725 |
| — inherit/implements edges | 0 |
| SQLite DB size | 3.8 MB |
| Parse errors | 0 |

## Languages

| Language | Files |
|---|---:|
| javascript | 476 |
| typescript | 163 |

## Symbol kinds

| Kind | Count |
|---|---:|
| function | 1,487 |
| class | 7 |
| interface | 1 |
| method | 1 |

## Call-graph resolution

Static analysis cannot resolve every call site (dynamic dispatch, trait objects, stdlib and macro-generated calls). This is measured and broken down by the evidence rule that produced each edge:

| Resolution strategy | Edges | Share |
|---|---:|---:|
| `unresolved` | 14,376 | 62.9% |
| `global-unique` | 7,480 | 32.7% |
| `scope` | 673 | 2.9% |
| `dir-unique` | 311 | 1.4% |
| **resolved total** | **8,464** | **37.1%** |

| Metric | Value |
|---|---:|
| Edges resolved to a concrete symbol | 8,775 |
| Edges left unresolved (target text preserved) | 15,790 |
| **Overall resolution rate** | **35.7%** |
| **Call-edge resolution rate** | **8464 / 22,840 = 37.1%** |

Unresolved edges keep their textual target (`dst_name`), so dynamic dispatch, standard-library calls and macro-generated calls remain queryable rather than being silently dropped. The dominant unresolved targets are stdlib/macro symbols (`assert_eq!`, `Ok`, `Some`, `Vec`, `push`, ...) that have no definition inside the repository — the parser-level dynamic-call counter above isolates the genuinely un-nameable call sites.
