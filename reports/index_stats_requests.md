# Index statistics — `requests`

Generated: 2026-10-23T16:39:17
Repository: `D:\codegraph\repos\requests`

## Scale

| Metric | Value |
|---|---:|
| Files indexed | 37 |
| Symbols extracted | 803 |
| Graph edges | 4,170 |
| — call edges | 3,805 |
| — import edges | 294 |
| — inherit/implements edges | 71 |
| SQLite DB size | 1.3 MB |
| Parse errors | 0 |

## Languages

| Language | Files |
|---|---:|
| python | 37 |

## Symbol kinds

| Kind | Count |
|---|---:|
| method | 542 |
| function | 165 |
| class | 96 |

## Call-graph resolution

Static analysis cannot resolve every call site (dynamic dispatch, trait objects, stdlib and macro-generated calls). This is measured and broken down by the evidence rule that produced each edge:

| Resolution strategy | Edges | Share |
|---|---:|---:|
| `unresolved` | 2,181 | 57.3% |
| `global-unique` | 1,069 | 28.1% |
| `import` | 286 | 7.5% |
| `scope` | 169 | 4.4% |
| `file-unique` | 76 | 2.0% |
| `dir-unique` | 24 | 0.6% |
| **resolved total** | **1,624** | **42.7%** |

| Metric | Value |
|---|---:|
| Edges resolved to a concrete symbol | 1,770 |
| Edges left unresolved (target text preserved) | 2,400 |
| **Overall resolution rate** | **42.4%** |
| **Call-edge resolution rate** | **1624 / 3,805 = 42.7%** |

Unresolved edges keep their textual target (`dst_name`), so dynamic dispatch, standard-library calls and macro-generated calls remain queryable rather than being silently dropped. The dominant unresolved targets are stdlib/macro symbols (`assert_eq!`, `Ok`, `Some`, `Vec`, `push`, ...) that have no definition inside the repository — the parser-level dynamic-call counter above isolates the genuinely un-nameable call sites.
