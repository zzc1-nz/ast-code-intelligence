# Index statistics — `ripgrep`

Generated: 2026-10-23T16:47:53
Repository: `D:\codegraph\repos\ripgrep`

## Scale

| Metric | Value |
|---|---:|
| Files indexed | 110 |
| Symbols extracted | 4,112 |
| Graph edges | 17,468 |
| — call edges | 16,842 |
| — import edges | 394 |
| — inherit/implements edges | 232 |
| SQLite DB size | 4.2 MB |
| Parse errors | 0 |

## Languages

| Language | Files |
|---|---:|
| rust | 110 |

## Symbol kinds

| Kind | Count |
|---|---:|
| method | 2,151 |
| function | 826 |
| impl | 445 |
| struct | 311 |
| module | 129 |
| const | 83 |
| enum | 77 |
| type | 45 |
| macro | 38 |
| trait | 7 |

## Call-graph resolution

Static analysis cannot resolve every call site (dynamic dispatch, trait objects, stdlib and macro-generated calls). This is measured and broken down by the evidence rule that produced each edge:

| Resolution strategy | Edges | Share |
|---|---:|---:|
| `unresolved` | 9,011 | 53.5% |
| `global-unique` | 2,910 | 17.3% |
| `scope` | 2,613 | 15.5% |
| `file-unique` | 1,491 | 8.9% |
| `dir-unique` | 398 | 2.4% |
| `receiver` | 397 | 2.4% |
| `callable-unique` | 22 | 0.1% |
| **resolved total** | **7,831** | **46.5%** |

| Metric | Value |
|---|---:|
| Edges resolved to a concrete symbol | 7,964 |
| Edges left unresolved (target text preserved) | 9,504 |
| **Overall resolution rate** | **45.6%** |
| **Call-edge resolution rate** | **7831 / 16,842 = 46.5%** |

Unresolved edges keep their textual target (`dst_name`), so dynamic dispatch, standard-library calls and macro-generated calls remain queryable rather than being silently dropped. The dominant unresolved targets are stdlib/macro symbols (`assert_eq!`, `Ok`, `Some`, `Vec`, `push`, ...) that have no definition inside the repository — the parser-level dynamic-call counter above isolates the genuinely un-nameable call sites.
